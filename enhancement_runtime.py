"""Latest-only worker; raw reception never waits for OpenCV processing."""

import asyncio
import logging
import time
from collections import deque
from dataclasses import replace

from app.camera_preview import preview
from app.image_enhancement import EnhancementConfig, ImageEnhancer

logger = logging.getLogger("ai_glasses_backend.enhancement")


class EnhancementRuntime:
    def __init__(self):
        self.config = EnhancementConfig()
        self.generation = 0
        self.pair = None
        self.processed = 0
        self.dropped = 0
        self.failures = 0
        self.samples = deque(maxlen=120)
        self.finished = deque(maxlen=120)
        self.last_log = 0.0
        self.in_flight_sequence = None

    def configure(self, config):
        self.config = config
        self.generation += 1
        self.pair = None
        self.samples.clear()
        self.finished.clear()

    async def run(self):
        last, generation = 0, -1
        enhancer = None
        while True:
            frame = preview.latest
            if frame is None or (frame.sequence == last and generation == self.generation):
                await asyncio.sleep(0.005)
                continue
            if generation != self.generation:
                enhancer = ImageEnhancer(self.config.model_copy())
                generation = self.generation
            self.dropped += max(0, frame.sequence-last-1)
            last = frame.sequence
            self.in_flight_sequence = last
            started = time.perf_counter()
            try:
                result = await asyncio.to_thread(enhancer.process, frame.data)
                # A config change can arrive while OpenCV runs. Discard both
                # its output and timing sample before publishing the new mode.
                if generation != self.generation:
                    continue
                cfg = enhancer.config
                enabled = any((cfg.enable_brightness_stabilization, cfg.enable_clahe, cfg.enable_sharpen))
                output = replace(frame, data=result.data, content_type="image/jpeg" if enabled else frame.content_type)
                self.samples.append((result.decode_ms, result.enhance_ms, result.encode_ms,
                                     (time.perf_counter()-started)*1000, result.width, result.height,
                                     (time.monotonic()-frame.received_monotonic)*1000))
            except Exception:
                if generation != self.generation:
                    continue
                self.failures += 1
                output = frame
                if self.failures == 1 or self.failures % 100 == 0:
                    logger.warning("Enhancement failed; original image retained")
            finally:
                self.in_flight_sequence = None
            if generation == self.generation:
                self.pair = (frame, output)
            self.processed += 1
            self.finished.append(time.monotonic())
            if time.monotonic()-self.last_log >= 5:
                self.last_log = time.monotonic()
                stats = self.stats()
                logger.info("Frames: receive_fps=%.2f processed_fps=%.2f decode_ms=%.2f enhance_ms=%.2f encode_ms=%.2f dimensions=%s dropped=%d fallback=%d",
                            stats['receive_fps'], stats['processed_fps_last_5s'], stats['mean_decode_ms'],
                            stats['mean_enhance_ms'], stats['mean_encode_ms'], stats['dimensions'],self.dropped,self.failures)

    def stats(self):
        now = time.monotonic()
        recent = [t for t in self.finished if now-t <= 5]
        count = len(self.samples)
        averages = [sum(row[i] for row in self.samples)/count if count else 0 for i in range(4)]
        return {"config": self.config.model_dump(), "receive_fps": preview.receive_fps(),
                "jpeg_receive_fps": preview.jpeg_receive_fps(),
                "processed_fps_last_5s": len(recent)/5, "processed_total": self.processed,
                "dropped_before_processing": self.dropped, "failures": self.failures,
                "pending_latest_only": bool(preview.latest and (not self.pair or preview.latest.sequence != self.pair[0].sequence)),
                "mean_decode_ms": averages[0], "mean_enhance_ms": averages[1],
                "mean_encode_ms": averages[2], "mean_worker_ms": averages[3],
                "dimensions": list(self.samples[-1][4:6]) if count else None,
                "mean_receive_to_ready_ms": sum(row[6] for row in self.samples)/count if count else 0,
                "processing_in_flight": self.in_flight_sequence is not None,
                "waiting_latest_only": bool(preview.latest
                    and preview.latest.sequence != self.in_flight_sequence
                    and (not self.pair or preview.latest.sequence != self.pair[0].sequence)),
                "queue_capacity": 1,
                "latest_received_sequence": preview.sequence,
                "note": "Last 120 processed frames; rates use a 5s window. Browser display FPS measured on page."}


runtime = EnhancementRuntime()
