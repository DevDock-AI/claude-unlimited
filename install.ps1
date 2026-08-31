# Claude Unlimited - one-line Windows installer.
#
# This is the Windows analogue of install.sh. It bootstraps EVERYTHING on a
# bare machine: if Python is missing it downloads and installs it for your
# user (no admin needed), then installs Claude Unlimited straight from GitHub
# (no git needed), registers it to run in the background, and opens the
# dashboard.
#
# Run it from PowerShell:
#     irm https://raw.githubusercontent.com/DevDock-AI/claude-unlimited/main/install.ps1 | iex
#
# ...or from the classic Command Prompt / the Win+R "Run" box:
#     powershell -ExecutionPolicy Bypass -NoProfile -Command "irm https://raw.githubusercontent.com/DevDock-AI/claude-unlimited/main/install.ps1 | iex"
#
# Nothing here needs administrator rights. (If you DO run it from an elevated
# terminal, it will also set the daemon to auto-start at logon.)
#
# Optional environment overrides, set before running:
#     $env:CLAUDE_UNLIMITED_BRANCH = "main"   # git branch/tag to install
#     $env:CLAUDE_UNLIMITED_PORT   = "4317"   # dashboard/proxy port

function Install-ClaudeUnlimited {
    $ErrorActionPreference = 'Stop'
    # Old Windows PowerShell defaults to TLS 1.0, which github.com and
    # python.org reject - opt into TLS 1.2 so the downloads below work.
    try {
        $sp = [Net.ServicePointManager]::SecurityProtocol
        foreach ($n in 'Tls12', 'Tls13') {
            try { $sp = $sp -bor [Net.SecurityProtocolType]::$n } catch {}
        }
        [Net.ServicePointManager]::SecurityProtocol = $sp
    } catch {}

    # Honour a corporate proxy for the downloads below, the way pip already
    # does for its own.
    if ($env:HTTPS_PROXY -or $env:HTTP_PROXY) {
        try {
            $prox = if ($env:HTTPS_PROXY) { $env:HTTPS_PROXY } else { $env:HTTP_PROXY }
            [Net.WebRequest]::DefaultWebProxy = New-Object Net.WebProxy($prox, $true)
        } catch {}
    }

    $Repo   = 'https://github.com/DevDock-AI/claude-unlimited'
    $Branch = if ($env:CLAUDE_UNLIMITED_BRANCH) { $env:CLAUDE_UNLIMITED_BRANCH } else { 'main' }
    $Port   = if ($env:CLAUDE_UNLIMITED_PORT)   { $env:CLAUDE_UNLIMITED_PORT }   else { '4317' }
    $PyVer  = '3.12.7'   # only used when Python has to be installed; bump freely

    # PROCESSOR_ARCHITECTURE reports x86 inside a 32-bit PowerShell running on
    # 64-bit Windows; PROCESSOR_ARCHITEW6432 carries the truth in that case.
    function Get-OSArch {
        $a = $env:PROCESSOR_ARCHITEW6432
        if (-not $a) { $a = $env:PROCESSOR_ARCHITECTURE }
        if ($a -eq 'ARM64') { return 'arm64' }
        if ([Environment]::Is64BitOperatingSystem) { return 'amd64' }
        return 'x86'
    }
    $OSArch = Get-OSArch

    function Say($m)  { Write-Host $m -ForegroundColor Cyan }
    function Ok($m)   { Write-Host $m -ForegroundColor Green }
    function Warn($m) { Write-Host $m -ForegroundColor Yellow }

    Say ""
    Say "Claude Unlimited - Windows installer"
    Say "===================================="

    # --- 0. A wrong PC clock breaks every HTTPS download ---------------------
    # Certificates are only valid inside a date range, so a skewed clock makes
    # python.org/github.com look "expired" and pip dies with an opaque
    # SSLCertVerificationError. Catch it here and say so in plain words.
    function Get-InternetTime {
        # Deliberately plain HTTP: TLS is what a bad clock breaks, so a check
        # that needed TLS would fail exactly when we need an answer.
        foreach ($u in @('http://www.msftconnecttest.com/connecttest.txt',
                         'http://cp.cloudflare.com/')) {
            try {
                $h = (Invoke-WebRequest -Uri $u -Method Head -UseBasicParsing -TimeoutSec 8).Headers['Date']
                if ($h) {
                    return [datetime]::Parse($h, [Globalization.CultureInfo]::InvariantCulture,
                        [Globalization.DateTimeStyles]::AdjustToUniversal -bor
                        [Globalization.DateTimeStyles]::AssumeUniversal)
                }
            } catch {}
        }
        return $null
    }

    function Get-ClockSkew {
        $net = Get-InternetTime
        if (-not $net) { return $null }
        [pscustomobject]@{
            Network = $net
            Minutes = [math]::Abs((($(Get-Date).ToUniversalTime()) - $net).TotalMinutes)
        }
    }

    function Show-ClockProblem($c) {
        Warn ""
        Warn "This PC's clock is wrong - it is off by about $([math]::Round($c.Minutes)) minute(s)."
        Warn ("  This PC: " + (Get-Date).ToUniversalTime().ToString('yyyy-MM-dd HH:mm') + " UTC")
        Warn ("  Actual : " + $c.Network.ToString('yyyy-MM-dd HH:mm') + " UTC")
        Warn ""
        Warn "HTTPS certificates are only valid inside a date range, so with a wrong"
        Warn "clock every download (python.org, github.com, pip) is rejected as"
        Warn "'certificate not within its validity period'."
        Warn ""
        Warn "Fix it from an *Administrator* PowerShell, then re-run this installer:"
        Warn "    net start w32time"
        Warn "    w32tm /resync /force"
        Warn "Or: Settings > Time & language > Date & time > turn on 'Set time"
        Warn "automatically', then click 'Sync now'."
        Warn ""
    }

    function Test-Admin {
        try {
            (New-Object Security.Principal.WindowsPrincipal(
                [Security.Principal.WindowsIdentity]::GetCurrent())
            ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
        } catch { $false }
    }

    # Changing the system clock needs admin rights, so this runs in two modes:
    # already elevated -> just do it; otherwise -> one UAC prompt for a helper
    # that does nothing but set the time, then we carry on unelevated.
    function Repair-Clock($c) {
        $utc = $c.Network.ToString('yyyy-MM-ddTHH:mm:ssZ')
        # Ask Windows Time to sync properly first; fall back to the clock we
        # read from the HTTP Date header (accurate to the second, and
        # certificate validity is measured in days).
        $fix = @"
try { Set-Service w32time -StartupType Manual -ErrorAction SilentlyContinue } catch {}
try { Start-Service w32time -ErrorAction SilentlyContinue } catch {}
try { & w32tm /resync /force 2>&1 | Out-Null } catch {}
`$net = [datetime]::Parse('$utc', [Globalization.CultureInfo]::InvariantCulture,
    [Globalization.DateTimeStyles]::AdjustToUniversal -bor [Globalization.DateTimeStyles]::AssumeUniversal)
if ([math]::Abs(((Get-Date).ToUniversalTime() - `$net).TotalMinutes) -gt 2) {
    Set-Date -Date `$net.ToLocalTime() | Out-Null
}
"@
        if (Test-Admin) {
            try { Invoke-Expression $fix } catch {}
        } else {
            Say "Windows needs administrator rights to set the clock - approve the prompt,"
            Say "then wait up to 60s for this script to continue."
            try {
                $enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($fix))
                Start-Process powershell -Verb RunAs -Wait -WindowStyle Hidden `
                    -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-EncodedCommand',$enc
            } catch {
                Warn "The clock fix was not approved."
                return $false
            }
        }
        $after = Get-ClockSkew
        return ($after -and $after.Minutes -le 5)
    }

    $clock = Get-ClockSkew
    if ($clock -and $clock.Minutes -gt 5) {
        Show-ClockProblem $clock
        Say "Trying to correct the clock automatically..."
        if (Repair-Clock $clock) {
            Ok ("Clock corrected - it is now " + (Get-Date).ToString('yyyy-MM-dd HH:mm') + " local time.")
            Say ""
        } else {
            Warn "Could not set the clock automatically."
            throw "Set this PC's date and time correctly, then run the installer again."
        }
    }

    # --- 1. Find a usable Python, or install one -----------------------------
    # Where to look for an interpreter. PATH alone is not enough: a terminal
    # opened *before* Python was installed still carries the old PATH, so a
    # perfectly good 3.12 install looks missing and we would download it again.
    function Get-PyCandidate {
        $found = New-Object System.Collections.ArrayList
        function Add-Cand($exe, $pre) {
            if ($exe -and (Test-Path $exe)) {
                [void]$found.Add([pscustomobject]@{
                    Exe = (Resolve-Path $exe).Path
                    Args = @($pre | Where-Object { $_ })
                })
            }
        }

        # 1. Whatever is on PATH right now. -CommandType Application, and
        #    keeping the resolved .Source path, matters: PowerShell resolves a
        #    bare name to a *function* before an executable (case-insensitively),
        #    so a helper named `Py` would otherwise shadow py.exe and recurse.
        foreach ($n in @('py', 'python', 'python3')) {
            foreach ($c in @(Get-Command $n -CommandType Application -ErrorAction SilentlyContinue)) {
                if ($n -eq 'py') { Add-Cand $c.Source @('-3') }
                Add-Cand $c.Source @()
            }
        }

        # 2. The registry, where every python.org install records itself - this
        #    is what makes a stale PATH harmless.
        foreach ($root in @('HKCU:\SOFTWARE\Python\PythonCore',
                            'HKLM:\SOFTWARE\Python\PythonCore',
                            'HKLM:\SOFTWARE\WOW6432Node\Python\PythonCore')) {
            foreach ($k in @(Get-ChildItem $root -ErrorAction SilentlyContinue)) {
                $ip = (Get-ItemProperty (Join-Path $k.PSPath 'InstallPath') -ErrorAction SilentlyContinue).'(default)'
                if ($ip) { Add-Cand (Join-Path $ip 'python.exe') @() }
            }
        }

        # 3. Default install locations, in case even the registry is missing.
        foreach ($g in @("$env:LocalAppData\Programs\Python\Python*\python.exe",
                         "$env:ProgramFiles\Python*\python.exe",
                         "${env:ProgramFiles(x86)}\Python*\python.exe",
                         "$env:SystemDrive\Python3*\python.exe")) {
            foreach ($f in @(Get-ChildItem $g -ErrorAction SilentlyContinue)) { Add-Cand $f.FullName @() }
        }
        Add-Cand (Join-Path $env:LocalAppData 'Programs\Python\Launcher\py.exe') @('-3')
        Add-Cand (Join-Path $env:WinDir 'py.exe') @('-3')

        return $found
    }

    function Find-Py {
        # Probe every candidate and keep the newest one that clears 3.10. The
        # Microsoft Store stub named python.exe is filtered out for free: it
        # prints nothing usable, so the version gate rejects it.
        $seen = @{}
        $best = $null; $bestVer = $null
        foreach ($cand in (Get-PyCandidate)) {
            $key = ($cand.Exe + ' ' + ($cand.Args -join ' ')).ToLower()
            if ($seen.ContainsKey($key)) { continue }
            $seen[$key] = $true
            try {
                # Filter nulls: an empty hashtable-stored array can read back
                # as $null, and @($null) is a 1-element array, not empty -
                # which would pass a stray blank arg to the interpreter.
                $pre = @($cand.Args | Where-Object { $_ })
                $v = & $cand.Exe @pre -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null
                if ($v -and [version]$v -ge [version]'3.10') {
                    if (-not $best -or [version]$v -gt $bestVer) { $best = $cand; $bestVer = [version]$v }
                }
            } catch {}
        }
        return $best
    }

    $py = Find-Py
    if (-not $py) {
        Warn "Python 3.10+ wasn't found - downloading the official installer from python.org..."
        # python.org names the 32-bit build with no architecture suffix at all.
        $suffix = if ($OSArch -eq 'x86') { '' } else { "-$OSArch" }
        $url  = "https://www.python.org/ftp/python/$PyVer/python-$PyVer$suffix.exe"
        $tmp  = Join-Path $env:TEMP "python-$PyVer$suffix.exe"
        Invoke-WebRequest -Uri $url -OutFile $tmp -UseBasicParsing
        Say "Installing Python $PyVer for your user (no admin required)..."
        # Per-user install (InstallAllUsers=0) needs no elevation; PrependPath
        # puts python + the py launcher on PATH; Include_launcher gives us `py`.
        # InstallLauncherAllUsers=0 keeps even the `py` launcher per-user, so
        # the whole thing runs without a UAC/elevation prompt.
        $proc = Start-Process -FilePath $tmp -Wait -PassThru -ArgumentList @(
            '/quiet', 'InstallAllUsers=0', 'PrependPath=1',
            'Include_launcher=1', 'InstallLauncherAllUsers=0',
            'Include_pip=1', 'Include_test=0'
        )
        Remove-Item $tmp -ErrorAction SilentlyContinue
        if ($proc.ExitCode -eq 3010) {
            Warn "Python installed, but Windows wants a restart to finish the job."
        } elseif ($proc.ExitCode -ne 0) {
            # 1602 = the user cancelled; anything else is a real installer error.
            Warn "The Python installer exited with code $($proc.ExitCode)."
            throw "Install Python 3.10 or newer from python.org, then run this installer again."
        }
        # PrependPath only edits the persisted environment; refresh THIS session
        # so the fresh interpreter is visible without reopening the terminal.
        $env:Path = ((@(
            [Environment]::GetEnvironmentVariable('Path','Machine'),
            [Environment]::GetEnvironmentVariable('Path','User'),
            $env:Path
        ) | Where-Object { $_ }) -join ';')
        $py = Find-Py
        if (-not $py) {
            # Fall back to the known per-user install location.
            $tag   = $PyVer.Split('.')[0] + $PyVer.Split('.')[1]   # e.g. 312
            $guess = Join-Path $env:LocalAppData "Programs\Python\Python$tag\python.exe"
            if (Test-Path $guess) { $py = [pscustomobject]@{ Exe = $guess; Args = @() } }
        }
        if (-not $py) {
            Warn "Python was installed but isn't visible in this session yet."
            Warn "Close this window, open a NEW PowerShell, and run the installer again -"
            Warn "it will pick up from here and finish."
            return
        }
    }

    # Named Invoke-Py, not Py: a function called `Py` would shadow the `py`
    # launcher it is trying to run (see the note in Find-Py above).
    function Invoke-Py { $pre = @($py.Args | Where-Object { $_ }); & $py.Exe @pre @args }
    Ok ("Python: " + (Invoke-Py -c "import sys;print(sys.version.split()[0])"))

    # --- 2. Install Claude Unlimited from the branch zip (no git needed) -----
    Say "Installing Claude Unlimited..."

    # pip refuses --user inside an active virtualenv, and someone will run this
    # from one. Ask the interpreter which case we are in rather than guessing.
    $userFlag = @('--user')
    try {
        if ((Invoke-Py -c "import sys;print(int(sys.prefix!=sys.base_prefix))").Trim() -eq '1') {
            $userFlag = @()
        }
    } catch {}
    # cryptography is the one native dependency, and on Windows ARM64 its
    # newest releases publish win_amd64 wheels only - pip then falls back to
    # building the Rust extension from source, which dies without MSVC's
    # link.exe. --only-binary makes pip choose the newest version that DOES
    # have a wheel for THIS platform (no pin to go stale), and the resolver
    # below leaves it alone because it already satisfies `cryptography>=42`.
    Invoke-Py -m pip install @userFlag --disable-pip-version-check --only-binary=:all: -q "cryptography>=42"
    if ($LASTEXITCODE -ne 0) {
        Warn "Couldn't pre-fetch a prebuilt 'cryptography' wheel - trying anyway."
    }

    # --log instead of 2>&1: redirecting a native command's stderr in Windows
    # PowerShell turns each line into an error record, which $ErrorActionPreference
    # = 'Stop' would treat as a failure even on success.
    $pipLog = Join-Path $env:TEMP 'claude-unlimited-pip.log'
    Invoke-Py -m pip install @userFlag --upgrade --upgrade-strategy only-if-needed `
        --disable-pip-version-check --log $pipLog "$Repo/archive/refs/heads/$Branch.zip"
    if ($LASTEXITCODE -ne 0) {
        # Re-check the clock here too: it can be fine at startup and still be
        # the culprit (skew below the threshold, or NTP moved it mid-run), and
        # a certificate error is the single most common way pip fails here.
        $c = Get-ClockSkew
        if ($c -and $c.Minutes -gt 1) {
            Show-ClockProblem $c
            Say "Trying to correct the clock and install again..."
            if (Repair-Clock $c) {
                Ok "Clock corrected - retrying."
                Invoke-Py -m pip install --user --upgrade --disable-pip-version-check "$Repo/archive/refs/heads/$Branch.zip"
            }
        }
        if ($LASTEXITCODE -ne 0) {
            $log = ''
            if (Test-Path $pipLog) { $log = (Get-Content $pipLog -Raw -ErrorAction SilentlyContinue) }
            if ($log -match 'link\.exe|Visual C\+\+|maturin|cargo|Failed building wheel|Rust') {
                Warn ""
                Warn "pip had to BUILD a dependency from source instead of downloading a"
                Warn "prebuilt wheel, and this PC has no C/Rust compiler installed."
                if ($OSArch -eq 'arm64') {
                    Warn "This is a Windows-on-ARM machine, where some packages publish x64"
                    Warn "wheels only. The easiest fix is to install the x64 build of Python"
                    Warn "(it runs fine under emulation) and re-run this installer:"
                    Warn "    https://www.python.org/downloads/windows/  ->  'Windows installer (64-bit)'"
                } else {
                    Warn "Install the 'Build Tools for Visual Studio' with the C++ workload,"
                    Warn "then re-run this installer:"
                    Warn "    https://visualstudio.microsoft.com/visual-cpp-build-tools/"
                }
                Warn ""
                Warn "Full pip log: $pipLog"
            }
            throw "pip install failed (exit code $LASTEXITCODE)."
        }
    }

    # --- 3. Put the `claude-unlimited` command on PATH -----------------------
    $scripts = (Invoke-Py -c "import sysconfig,sys;print(sysconfig.get_path('scripts') if sys.prefix!=sys.base_prefix else sysconfig.get_path('scripts','nt_user'))").Trim()
    $exe = Join-Path $scripts 'claude-unlimited.exe'
    # Kept even when the .exe is missing: it is what we tell people to type if
    # PATH does not take effect, and the module form always works.
    $exeHint = if (Test-Path $exe) { $exe } else { "$($py.Exe) -m claude_unlimited" }
    if (-not (Test-Path $exe)) { $exe = $null }
    # Go through the registry, preserving the value's KIND. Reading PATH with
    # [Environment]::GetEnvironmentVariable EXPANDS any %VARS% in it, so writing
    # that back would permanently flatten a REG_EXPAND_SZ user PATH into literal
    # text - silently breaking entries like %USERPROFILE%\bin for that person.
    # A registry write alone is invisible to processes that are already running,
    # and that includes Explorer - which hands its own cached copy of the
    # environment to every terminal it launches. Without this broadcast even a
    # freshly opened window still has the OLD PATH until the next sign-out.
    # [Environment]::SetEnvironmentVariable sends it for us; the registry write
    # we need (to preserve REG_EXPAND_SZ) does not.
    function Publish-EnvironmentChange {
        try {
            if (-not ('CU.NativeEnv' -as [type])) {
                Add-Type -Namespace CU -Name NativeEnv -MemberDefinition @'
[DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Auto)]
public static extern IntPtr SendMessageTimeout(IntPtr hWnd, uint Msg, UIntPtr wParam,
    string lParam, uint fuFlags, uint uTimeout, out UIntPtr lpdwResult);
'@
            }
            $res = [UIntPtr]::Zero
            # HWND_BROADCAST, WM_SETTINGCHANGE, SMTO_ABORTIFHUNG, 5s timeout.
            [void][CU.NativeEnv]::SendMessageTimeout(
                [IntPtr]0xffff, 0x1A, [UIntPtr]::Zero, 'Environment', 2, 5000, [ref]$res)
        } catch {
            # Not fatal: the PATH entry is written either way, it just takes a
            # sign-out to become visible. Never fail the install over this.
        }
    }

    function Add-ToUserPath($dir) {
        $path = 'Registry::HKEY_CURRENT_USER\Environment'
        $raw = ''; $kind = 'ExpandString'
        try {
            $k = Get-Item $path
            $raw = [string]$k.GetValue('Path', '', 'DoNotExpandEnvironmentNames')
            $kind = $k.GetValueKind('Path')
        } catch {}
        $norm = $dir.TrimEnd('\').ToLowerInvariant()
        if (@($raw -split ';' | ForEach-Object { $_.TrimEnd('\').ToLowerInvariant() }) -contains $norm) {
            return $false
        }
        $new = (@($raw.TrimEnd(';'), $dir) | Where-Object { $_ }) -join ';'
        Set-ItemProperty -Path $path -Name 'Path' -Value $new -Type $kind
        Publish-EnvironmentChange
        return $true
    }

    # Would a BRAND NEW terminal find the command? Environment blocks are copied
    # at process creation, so the only honest check is against the persisted
    # machine+user PATH - not this session's, which we edit by hand below.
    function Test-CommandVisible($name) {
        $saved = $env:Path
        try {
            $env:Path = ((@([Environment]::GetEnvironmentVariable('Path', 'Machine'),
                            [Environment]::GetEnvironmentVariable('Path', 'User')) |
                          Where-Object { $_ }) -join ';')
            return [bool](Get-Command $name -CommandType Application -ErrorAction SilentlyContinue)
        } catch {
            return $false
        } finally {
            $env:Path = $saved
        }
    }

    if ($scripts -and (Test-Path $scripts)) {
        try {
            if (Add-ToUserPath $scripts) { Ok "Added $scripts to your PATH." }
        } catch {
            Warn "Couldn't update your PATH automatically - add this folder yourself: $scripts"
        }
        # This session, so the steps below can call the command by name.
        $env:Path = $env:Path.TrimEnd(';') + ';' + $scripts
    }

    # A second, PATH-proof way in. The Scripts directory above only helps
    # sessions that pick up our new PATH entry, and nothing can push PATH into
    # a window that is already open. %LOCALAPPDATA%\Microsoft\WindowsApps is on
    # every user's PATH by default, so a launcher dropped there works even in
    # terminals that were open before this ran.
    function Install-Launcher {
        $dir = Join-Path $env:LocalAppData 'Microsoft\WindowsApps'
        if (-not (Test-Path $dir)) { return $null }
        $pre  = @($py.Args | Where-Object { $_ })
        $args = if ($pre) { ($pre -join ' ') + ' ' } else { '' }
        # Absolute paths, resolved here at install time, on purpose: `-m
        # claude_unlimited` would make the launcher depend on Python locating
        # the per-user site-packages directory, which it derives from %APPDATA%
        # - and a session with a different or missing APPDATA then fails with
        # "No module named claude_unlimited". The module form stays only as a
        # fallback for when pip generated no .exe. CRLF endings are required:
        # cmd.exe misparses a batch file with bare LF and reports "cannot find
        # the path specified".
        $exePath = Join-Path $scripts 'claude-unlimited.exe'
        # PYTHONPATH pinned to the absolute install directory. A --user install
        # is only importable via the per-user site-packages path, which Python
        # derives from %APPDATA% - so a session where that variable is missing
        # or different fails with "No module named claude_unlimited", and pip's
        # own generated .exe fails identically. Naming the directory outright
        # removes that dependency.
        # Asked of the package itself rather than assumed from sysconfig, so it
        # is right for a normal install (site-packages) and an editable one
        # (the source tree) alike.
        # Two directories: where the package itself lives (asked of the package,
        # so an editable install pointing at a source tree is handled too), and
        # the per-user site-packages its DEPENDENCIES live in.
        $site = (Invoke-Py -c "import os,sysconfig,claude_unlimited;pkg=os.path.dirname(os.path.dirname(os.path.abspath(claude_unlimited.__file__)));deps=sysconfig.get_path('purelib','nt_user');print(pkg if pkg==deps else pkg+';'+deps)").Trim()
        $text = "@echo off`r`nrem Claude Unlimited launcher (installer-generated).`r`n" +
                ("if not defined PYTHONPATH (set `"PYTHONPATH=$site`") else (set `"PYTHONPATH=$site;%PYTHONPATH%`")`r`n") +
                ("if exist `"$exePath`" (`r`n") +
                ("  `"$exePath`" %*`r`n") +
                (") else (`r`n") +
                ('  "{0}" {1}-m claude_unlimited %*' -f $py.Exe, $args) + "`r`n" +
                (")`r`n")
        $file = Join-Path $dir 'claude-unlimited.cmd'
        try {
            [IO.File]::WriteAllText($file, $text, [Text.Encoding]::ASCII)
            return $file
        } catch {
            return $null
        }
    }

    $launcher = Install-Launcher
    if ($launcher) { Ok "Installed launcher: $launcher" }

    if (Test-CommandVisible 'claude-unlimited') {
        Ok "'claude-unlimited' is on your PATH - open a NEW terminal to use it (windows"
        Ok "that are already open keep the PATH they started with)."
    } else {
        Warn "'claude-unlimited' is not on your PATH yet. Run it by full path:"
        Warn "    $exeHint"
        Warn "...or sign out and back in, which reloads the environment everywhere."
    }

    # Invoke the CLI whether or not this session's PATH picked up the new
    # Scripts dir: prefer the resolved .exe, else the module form.
    function Invoke-CU { if ($exe) { & $exe @args } else { Invoke-Py -m claude_unlimited @args } }

    # --- 4. Sanity-check the install -----------------------------------------
    Say ""
    Invoke-CU doctor

    # --- 5. Register the background service ----------------------------------
    Say ""
    Say "Setting Claude Unlimited to run in the background..."
    $registered = $false
    try {
        Invoke-CU install --port $Port
        if ($LASTEXITCODE -eq 0) { $registered = $true }
    } catch {}
    if (-not $registered) {
        Warn "Couldn't register the auto-start task - that step needs an elevated terminal."
        Warn "Starting Claude Unlimited for this session instead. To have it start"
        Warn "automatically every time you log in, re-run this installer from an"
        Warn "Administrator PowerShell."
        # Detached so the daemon outlives this window.
        $pre = @($py.Args | Where-Object { $_ })
        Start-Process -WindowStyle Hidden -FilePath $py.Exe `
            -ArgumentList ($pre + @('-m','claude_unlimited','start','--port',$Port))
    }

    # --- 6. Wait for it to answer, then open the dashboard -------------------
    $dash = "http://127.0.0.1:$Port/"
    $up = $false
    for ($i = 0; $i -lt 20; $i++) {
        try {
            $r = Invoke-WebRequest -Uri ($dash + 'health') -TimeoutSec 1 -UseBasicParsing
            if ($r.StatusCode -eq 200) { $up = $true; break }
        } catch {}
        Start-Sleep -Milliseconds 500
    }

    Say ""
    if ($up) {
        Ok  "Claude Unlimited is running."
        Ok  "Dashboard: $dash"
        try { Start-Process $dash } catch {}
    } else {
        Warn "The daemon didn't answer yet - give it a moment, then open: $dash"
        Warn "or start it yourself with:  claude-unlimited start"
    }

    Say ""
    Say "Next steps:"
    Say "  1. Add an account:            claude-unlimited add-account   (or use the dashboard)"
    Say "  2. Run Claude Code through it: claude-unlimited code"
    Say ""
    Say "(If 'claude-unlimited' isn't recognized, open a NEW terminal first, or use"
    Say " 'py -m claude_unlimited' instead.)"
    Say ""
}

Install-ClaudeUnlimited
