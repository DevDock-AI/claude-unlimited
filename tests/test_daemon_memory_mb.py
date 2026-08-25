"""_memory_mb() per platform, with ctypes.windll faked in for the Windows
branch.

The Windows case proves the ctypes plumbing (struct layout, handle, function
names, byte math) is wired correctly, not that GetProcessMemoryInfo behaves
this way on a real Windows box.
"""

import ctypes
from unittest.mock import MagicMock, patch

import claude_unlimited.daemon as daemon


def test_memory_mb_darwin_divides_by_bytes_per_mb(monkeypatch):
    monkeypatch.setattr(daemon.platform, "system", lambda: "Darwin")
    fake_usage = MagicMock(ru_maxrss=200 * 1024 * 1024)  # macOS: bytes
    monkeypatch.setattr(daemon.resource, "getrusage", lambda who: fake_usage)
    assert daemon._memory_mb() == 200.0


def test_memory_mb_linux_divides_by_kb_per_mb(monkeypatch):
    monkeypatch.setattr(daemon.platform, "system", lambda: "Linux")
    fake_usage = MagicMock(ru_maxrss=200 * 1024)  # Linux: KB
    monkeypatch.setattr(daemon.resource, "getrusage", lambda who: fake_usage)
    assert daemon._memory_mb() == 200.0


def test_memory_mb_windows_reads_working_set_via_ctypes(monkeypatch):
    monkeypatch.setattr(daemon.platform, "system", lambda: "Windows")

    fake_windll = MagicMock()
    fake_windll.kernel32.GetCurrentProcess.return_value = 12345

    def fake_get_process_memory_info(handle, counters_byref, cb):
        assert handle == 12345
        assert cb == ctypes.sizeof(daemon._PROCESS_MEMORY_COUNTERS)
        # counters_byref is the ctypes.byref() of the struct instance
        # _memory_mb() constructed — fill in WorkingSetSize the way the Win32
        # API call would.
        ptr = ctypes.cast(counters_byref, ctypes.POINTER(daemon._PROCESS_MEMORY_COUNTERS))
        ptr.contents.WorkingSetSize = 300 * 1024 * 1024  # 300 MB, in bytes
        return 1

    fake_windll.psapi.GetProcessMemoryInfo.side_effect = fake_get_process_memory_info

    with patch("ctypes.windll", fake_windll, create=True):
        result = daemon._memory_mb()

    assert result == 300.0
    assert fake_windll.kernel32.GetCurrentProcess.called
    assert fake_windll.psapi.GetProcessMemoryInfo.called
