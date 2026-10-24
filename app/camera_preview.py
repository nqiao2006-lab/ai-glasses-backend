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
    client_sequence: int | None = None


class CameraPreview:
    def __init__(self):
        self.latest: Frame | None = None
        self.sequence = 0
        self.arrivals = deque(maxlen=1000)
        self.jpeg_arrivals = deque(maxlen=1000)
        self.ws_arrivals = deque(maxlen=1000)
        self.ws_connections = 0
        self.ws_sequence: int | None = None
        self.ws_received_total = 0
        self.ws_dropped_out_of_order = 0

    def publish(self, data: bytes, content_type: str, source: str,
                client_sequence: int | None = None) -> Frame:
        self.sequence += 1
        frame = Frame(data, content_type, source, datetime.now(timezone.utc).isoformat(),
                      self.sequence, client_sequence=client_sequence)
        self.latest = frame
        self.arrivals.append(frame.received_monotonic)
        if content_type == "image/jpeg":
            self.jpeg_arrivals.append(frame.received_monotonic)
        return frame

    def publish_ws(self, data: bytes, sequence: int) -> bool:
        # This method contains no awaits: comparison and replacement are atomic
        # on the single ASGI event loop, including across multiple connections.
        self.ws_arrivals.append(time.monotonic())
        self.ws_received_total += 1
        if self.ws_sequence is not None and sequence <= self.ws_sequence:
            self.ws_dropped_out_of_order += 1
            return False
        self.ws_sequence = sequence
        self.publish(data, "image/jpeg", "/ws/camera/upload", sequence)
        return True

    def ws_frame_fps(self):
        now = time.monotonic()
        return sum(now-t <= 5 for t in self.ws_arrivals)/5

    def receive_fps(self):
        now = time.monotonic()
        return sum(now-t <= 5 for t in self.arrivals)/5

    def jpeg_receive_fps(self):
        now = time.monotonic()
        return sum(now-t <= 5 for t in self.jpeg_arrivals)/5


preview = CameraPreview()
