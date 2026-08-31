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
    try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}

    $Repo   = 'https://github.com/DevDock-AI/claude-unlimited'
    $Branch = if ($env:CLAUDE_UNLIMITED_BRANCH) { $env:CLAUDE_UNLIMITED_BRANCH } else { 'main' }
    $Port   = if ($env:CLAUDE_UNLIMITED_PORT)   { $env:CLAUDE_UNLIMITED_PORT }   else { '4317' }
    $PyVer  = '3.12.7'   # only used when Python has to be installed; bump freely

    function Say($m)  { Write-Host $m -ForegroundColor Cyan }
    function Ok($m)   { Write-Host $m -ForegroundColor Green }
    function Warn($m) { Write-Host $m -ForegroundColor Yellow }

    Say ""
    Say "Claude Unlimited - Windows installer"
    Say "===================================="

    # --- 1. Find a usable Python, or install one -----------------------------
    function Find-Py {
        # Preference order: the `py` launcher (handles versions cleanly), then
        # plain python/python3. Each candidate is version-gated at >= 3.10.
        foreach ($spec in @(
            @{ Exe = 'py';      Pre = @('-3') },
            @{ Exe = 'py';      Pre = @() },
            @{ Exe = 'python';  Pre = @() },
            @{ Exe = 'python3'; Pre = @() }
        )) {
            if (Get-Command $spec.Exe -ErrorAction SilentlyContinue) {
                try {
                    # Filter nulls: an empty hashtable-stored array can read back
                    # as $null, and @($null) is a 1-element array, not empty -
                    # which would pass a stray blank arg to the interpreter.
                    $pre = @($spec.Pre | Where-Object { $_ })
                    $v = & $spec.Exe @pre -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null
                    if ($v -and [version]$v -ge [version]'3.10') {
                        return [pscustomobject]@{ Exe = $spec.Exe; Args = $pre }
                    }
                } catch {}
            }
        }
        return $null
    }

    $py = Find-Py
    if (-not $py) {
        Warn "Python 3.10+ wasn't found - downloading the official installer from python.org..."
        $arch = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { 'arm64' } else { 'amd64' }
        $url  = "https://www.python.org/ftp/python/$PyVer/python-$PyVer-$arch.exe"
        $tmp  = Join-Path $env:TEMP "python-$PyVer-$arch.exe"
        Invoke-WebRequest -Uri $url -OutFile $tmp -UseBasicParsing
        Say "Installing Python $PyVer for your user (no admin required)..."
        # Per-user install (InstallAllUsers=0) needs no elevation; PrependPath
        # puts python + the py launcher on PATH; Include_launcher gives us `py`.
        # InstallLauncherAllUsers=0 keeps even the `py` launcher per-user, so
        # the whole thing runs without a UAC/elevation prompt.
        Start-Process -FilePath $tmp -Wait -PassThru -ArgumentList @(
            '/quiet', 'InstallAllUsers=0', 'PrependPath=1',
            'Include_launcher=1', 'InstallLauncherAllUsers=0',
            'Include_pip=1', 'Include_test=0'
        ) | Out-Null
        Remove-Item $tmp -ErrorAction SilentlyContinue
        # PrependPath only edits the persisted environment; refresh THIS session
        # so the fresh interpreter is visible without reopening the terminal.
        $env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' +
                    [Environment]::GetEnvironmentVariable('Path','User')
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

    function Py { $pre = @($py.Args | Where-Object { $_ }); & $py.Exe @pre @args }
    Ok ("Python: " + (Py -c "import sys;print(sys.version.split()[0])"))

    # --- 2. Install Claude Unlimited from the branch zip (no git needed) -----
    Say "Installing Claude Unlimited..."
    Py -m pip install --user --upgrade --disable-pip-version-check "$Repo/archive/refs/heads/$Branch.zip"
    if ($LASTEXITCODE -ne 0) { throw "pip install failed (exit code $LASTEXITCODE)." }

    # --- 3. Put the `claude-unlimited` command on PATH -----------------------
    $scripts = (Py -c "import sysconfig;print(sysconfig.get_path('scripts','nt_user'))").Trim()
    $exe = Join-Path $scripts 'claude-unlimited.exe'
    if (-not (Test-Path $exe)) { $exe = $null }
    if ($scripts -and (Test-Path $scripts)) {
        $userPath = [Environment]::GetEnvironmentVariable('Path','User')
        if (-not $userPath) { $userPath = '' }
        if (($userPath -split ';') -notcontains $scripts) {
            [Environment]::SetEnvironmentVariable('Path', ($userPath.TrimEnd(';') + ';' + $scripts), 'User')
            Ok "Added $scripts to your PATH (open a new terminal to use 'claude-unlimited' directly)."
        }
        $env:Path = $env:Path.TrimEnd(';') + ';' + $scripts
    }

    # Invoke the CLI whether or not this session's PATH picked up the new
    # Scripts dir: prefer the resolved .exe, else the module form.
    function CU { if ($exe) { & $exe @args } else { Py -m claude_unlimited @args } }

    # --- 4. Sanity-check the install -----------------------------------------
    Say ""
    CU doctor

    # --- 5. Register the background service ----------------------------------
    Say ""
    Say "Setting Claude Unlimited to run in the background..."
    $registered = $false
    try {
        CU install --port $Port
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
