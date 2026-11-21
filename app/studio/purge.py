"""Purge: give memory back and clear ClipKar's own temporary files, without touching any clip, video or setting."""
import gc
import os
import shutil
import time
from pathlib import Path

from . import config


def _rss() -> float:
    if os.name != "nt":
        return 0.0
    try:
        import ctypes
        from ctypes import wintypes

        class PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
        c = PMC()
        c.cb = ctypes.sizeof(PMC)
        k32, psapi = ctypes.windll.kernel32, ctypes.windll.psapi
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
        psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(c), c.cb)
        return c.WorkingSetSize / 1e6
    except Exception:  # noqa: BLE001
        return 0.0


def _trim():
    """Ask Windows to move this process's unused memory out of RAM (what 'RAM cleaner' tools do)."""
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes
        k32, psapi = ctypes.windll.kernel32, ctypes.windll.psapi
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.EmptyWorkingSet.argtypes = [wintypes.HANDLE]
        psapi.EmptyWorkingSet(k32.GetCurrentProcess())
    except Exception:  # noqa: BLE001
        pass


def _clear_dir(d: Path, older_than=0) -> int:
    """Delete files inside d (older than N seconds). Returns bytes freed."""
    freed = 0
    if not d.is_dir():
        return 0
    now = time.time()
    for f in d.rglob("*"):
        try:
            if f.is_file() and now - f.stat().st_mtime > older_than:
                freed += f.stat().st_size
                f.unlink()
        except OSError:
            continue
    for sub in sorted((x for x in d.rglob("*") if x.is_dir()), key=lambda x: -len(x.parts)):
        try:
            sub.rmdir()
        except OSError:
            pass
    return freed


def run() -> dict:
    before = _rss()
    freed_models = 0
    try:
        from . import transcribe
        freed_models = transcribe.release_idle()
    except Exception:  # noqa: BLE001
        pass
    disk = 0
    disk += _clear_dir(config.TMP_DIR, older_than=600)                 # leftovers from finished work
    disk += _clear_dir(config.DATA_DIR / "update-backup", older_than=3 * 86400)
    import tempfile
    for d in Path(tempfile.gettempdir()).glob("pk*"):                   # render work folders left by a crash
        try:
            if d.is_dir() and time.time() - d.stat().st_mtime > 3600:
                disk += sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            continue
    try:                                                                # cached AI model lists etc. are tiny: kept
        from . import updater
        updater._cache["at"] = 0
    except Exception:  # noqa: BLE001
        pass
    gc.collect()
    _trim()
    after = _rss()
    return {"ram_before_mb": round(before), "ram_after_mb": round(after), "ram_freed_mb": max(0, round(before - after)),
            "models_unloaded": freed_models, "disk_freed_mb": round(disk / 1e6, 1)}
