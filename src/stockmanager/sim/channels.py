"""``RecordingChannel``: a notifier ``Channel`` that records sends and can be switched off (TS §0.3)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from stockmanager.notify.deliver import Receipt, Rendered, Transient
from stockmanager.sim.clock import FakeClock


class RecordingChannel:
    """Delivers to memory. While ``down`` every send raises ``Transient``, as a network outage would."""

    def __init__(self, name: Literal["discord", "mainchat"], clock: FakeClock, *, threads: bool = False) -> None:
        self.name = name
        self.down = False
        self.threads = threads  # open a thread per delivered alert, in the same send (v2 H5)
        self.thread_ids: list[str] = []
        self._clock = clock
        self.sent: list[tuple[str, Rendered]] = []  # (nonce, rendered), in send order
        self.sent_at: list[datetime] = []

    def send(self, rendered: Rendered, *, nonce: str) -> Receipt:
        """Record the send, or raise ``Transient`` while ``down``. A repeated nonce is dropped, as Discord would."""
        if self.down:
            raise Transient(f"{self.name} unreachable")
        if all(n != nonce for n, _ in self.sent):
            self.sent.append((nonce, rendered))
            self.sent_at.append(self._clock())
        thread_id = None
        if self.threads:
            thread_id = f"thread-{nonce}"
            if thread_id not in self.thread_ids:
                self.thread_ids.append(thread_id)
        return Receipt(self.name, str(len(self.sent)), self._clock(), thread_id)
