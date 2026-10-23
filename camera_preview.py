"""Single-process preview: retain only the latest frame in memory."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import time
from collections import deque


@dataclass(frozen=True)
class Frame:
    data: bytes
    content_type: str
    source: str
    received_at: str
    sequence: int
    received_monotonic: float = field(default_factory=time.monotonic)


class CameraPreview:
    def __init__(self):
        self.latest: Frame | None = None
        self.sequence = 0
        self.arrivals = deque(maxlen=1000)
        self.jpeg_arrivals = deque(maxlen=1000)

    def publish(self, data: bytes, content_type: str, source: str) -> Frame:
        self.sequence += 1
        frame = Frame(data, content_type, source, datetime.now(timezone.utc).isoformat(), self.sequence)
        self.latest = frame
        self.arrivals.append(frame.received_monotonic)
        if content_type == "image/jpeg":
            self.jpeg_arrivals.append(frame.received_monotonic)
        return frame

    def receive_fps(self):
        now = time.monotonic()
        return sum(now-t <= 5 for t in self.arrivals)/5

    def jpeg_receive_fps(self):
        now = time.monotonic()
        return sum(now-t <= 5 for t in self.jpeg_arrivals)/5


preview = CameraPreview()
