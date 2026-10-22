"""Audible failure alert (repeats until acknowledged)."""

from __future__ import annotations

import sys
import threading


def _beep_once(freq: int, ms: int, fallback) -> None:
    if sys.platform.startswith("win"):
        try:
            import winsound
            winsound.Beep(freq, ms)
            return
        except Exception:  # noqa: BLE001 - no sound device
            pass
    if fallback is not None:
        fallback()


class AlertSounder:
    """Plays a repeating alarm on a background thread until ``acknowledge()``.

    ``fallback`` (e.g. Tk ``bell``) is used where winsound is unavailable; it
    must be thread safe or marshalled by the caller.
    """

    def __init__(self, fallback=None, interval_s: float = 1.2, max_repeats: int = 30):
        self.fallback = fallback
        self.interval_s = interval_s
        self.max_repeats = max_repeats
        self._ack = threading.Event()
        self._thread: threading.Thread | None = None
        self.enabled = True

    @property
    def active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def alarm(self, freq: int = 1500) -> None:
        if not self.enabled or self.active:
            return
        self._ack.clear()
        self._thread = threading.Thread(target=self._run, args=(freq,), daemon=True, name="alert")
        self._thread.start()

    def _run(self, freq: int) -> None:
        for _ in range(self.max_repeats):
            if self._ack.is_set():
                break
            _beep_once(freq, 250, self.fallback)
            _beep_once(int(freq * 0.75), 250, None)
            if self._ack.wait(self.interval_s):
                break

    def acknowledge(self) -> None:
        self._ack.set()
