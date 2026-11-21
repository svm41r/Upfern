"""Upfern."""
try:
    from . import jsrt as _jsrt
    _jsrt.install()          # YouTube's challenge solver and the PC's JavaScript runtime, for every download
except Exception:  # noqa: BLE001 - never stops Upfern from starting
    pass
