"""``RecordingChannel``: a notifier ``Channel`` that records sends and can be switched off (TS §0.3)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from stockmanager.notify.deliver import Receipt, Rendered, Transient
from stockmanager.sim.clock import FakeClock


class RecordingChannel:
    """Delivers to memory. While ``down`` every send raises ``Transient``, as a network outage would."""

    def __init__(self, name: Literal["discord", "mainchat"], clock: FakeClock) -> None:
        self.name = name
        self.down = False
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
        return Receipt(self.name, str(len(self.sent)), self._clock())
