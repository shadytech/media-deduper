"""Can a file actually go to the Windows Recycle Bin?

"Send to the Recycle Bin" quietly becomes a permanent delete when

* the drive has no Recycle Bin (network shares, USB sticks, memory cards),
* the Recycle Bin is switched off for that drive ("Don't move files to the
  Recycle Bin" in its properties), or
* the file is larger than that drive's Recycle Bin size limit.

Explorer asks before deleting in those cases. The app tells Windows not to
show its own prompts, so it has to warn about them itself.
"""

from __future__ import annotations

import os
import sys
from typing import Callable

DRIVE_FIXED = 3   # GetDriveType: internal or external hard disk / SSD

DriveInfo = Callable[[str], "tuple[bool, int | None]"]


def _drive_info(root: str) -> tuple[bool, int | None]:
    """(has a working Recycle Bin, its size limit in bytes or None if unknown) for a
    drive root such as 'C:\\' or '\\\\server\\share\\'."""
    import ctypes
    import winreg
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    k32.GetDriveTypeW.restype = wintypes.UINT
    k32.GetVolumeNameForVolumeMountPointW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    k32.GetVolumeNameForVolumeMountPointW.restype = wintypes.BOOL

    if k32.GetDriveTypeW(root) != DRIVE_FIXED:
        return False, None

    def dword(key, name: str) -> int:
        try:
            return int(winreg.QueryValueEx(key, name)[0])
        except (OSError, TypeError, ValueError):
            return 0

    try:   # "Don't move files to the Recycle Bin" set by policy for every drive
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Policies\Explorer") as k:
            if dword(k, "NoRecycleFiles"):
                return False, None
    except OSError:
        pass

    buf = ctypes.create_unicode_buffer(64)
    if not k32.GetVolumeNameForVolumeMountPointW(root, buf, len(buf)):
        return True, None
    guid = buf.value[buf.value.find("{"): buf.value.find("}") + 1]   # \\?\Volume{guid}\
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket\Volume"
                            "\\" + guid) as k:
            if dword(k, "NukeOnDelete"):
                return False, None
            cap_mb = dword(k, "MaxCapacity")
            return True, (cap_mb << 20) if cap_mb > 0 else None
    except OSError:
        return True, None   # no settings saved for this drive yet: Windows defaults apply


def not_recyclable(paths: list[str], sizes: dict[str, int],
                   drive_info: DriveInfo | None = None) -> list[str]:
    """The paths that Windows would delete permanently instead of recycling."""
    if drive_info is None:
        if sys.platform != "win32":
            return []
        drive_info = _drive_info
    known: dict[str, tuple[bool, int | None]] = {}
    out = []
    for p in paths:
        root = os.path.splitdrive(os.path.abspath(p))[0].upper() + "\\"
        if root not in known:
            try:
                known[root] = drive_info(root)
            except Exception:
                known[root] = (True, None)   # can't tell: assume the normal case
        has_bin, limit = known[root]
        if not has_bin or (limit is not None and sizes.get(p, 0) > limit):
            out.append(p)
    return out
