"""Single-worker luminance enhancement. No pixel loops or frame queue."""

import time
import os
from dataclasses import dataclass

os.environ.setdefault("OPENCV_IO_MAX_IMAGE_PIXELS", "12000000")
import cv2
import numpy as np
from pydantic import BaseModel, ConfigDict, Field

# Avoid oversubscribing this small backend with OpenCV's internal worker pool.
cv2.setNumThreads(1)


class EnhancementConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enable_brightness_stabilization: bool = True
    enable_clahe: bool = True
    enable_sharpen: bool = False
    brightness_alpha: float = Field(default=0.15, gt=0, le=1)
    clahe_clip_limit: float = Field(default=2.0, ge=0.1, le=4)
    tile_grid_size: int = Field(default=8, ge=2, le=16)
    sharpen_strength: float = Field(default=0.4, ge=0, le=1)


@dataclass
class EnhancedImage:
    data: bytes
    decode_ms: float
    enhance_ms: float
    encode_ms: float
    width: int
    height: int
    fallback: bool = False


class ImageEnhancer:
    def __init__(self, config: EnhancementConfig):
        self.config = config
        self.ema = None
        self.previous_small = None
        self.previous_mean = None
        self.previous_std = None
        self.last_time = 0.0
        self.shape = None
        self.clahe = cv2.createCLAHE(config.clahe_clip_limit,
                                   (config.tile_grid_size, config.tile_grid_size))
        values = np.arange(256, dtype=np.float32)
        self.luma_values = values
        # A byte LUT replaces full-frame signed/float arrays. Weak differences
        # are ignored; each edge correction is bounded to strength * 16.
        self.sharpen_lut = np.rint(
            np.where(values < 3, 0, np.minimum(values, 16) * config.sharpen_strength)
        ).astype(np.uint8)

    def apply(self, bgr):
        cfg = self.config
        if not (cfg.enable_brightness_stabilization or cfg.enable_clahe or cfg.enable_sharpen):
            return bgr
        ycc = cv2.cvtColor(bgr, cv2.COLOR_BGR2YCrCb)
        y = cv2.extractChannel(ycc, 0)
        if cfg.enable_brightness_stabilization:
            small = cv2.resize(y, (64, 48), interpolation=cv2.INTER_AREA)
            mean_array, std_array = cv2.meanStdDev(small)
            mean, std = float(mean_array[0, 0]), float(std_array[0, 0])
            now = time.monotonic()
            # Centered correlation ignores global exposure offsets and scales.
            # Flat frames cannot distinguish motion from exposure: skip this
            # heuristic there instead of treating a histogram shift as a cut.
            structure_changed = (
                self.previous_small is not None
                and min(std, self.previous_std) >= 8
                and float(cv2.matchTemplate(small, self.previous_small,
                                            cv2.TM_CCOEFF_NORMED)[0, 0]) < 0.75
            )
            reset = (self.ema is None or self.shape != y.shape or now-self.last_time > 2
                     or (self.previous_mean is not None and abs(mean-self.previous_mean) > 45)
                     or structure_changed)
            self.ema = mean if reset else cfg.brightness_alpha*mean+(1-cfg.brightness_alpha)*self.ema
            gain = float(np.clip(self.ema/max(mean, 1), 0.8, 1.25))
            values = self.luma_values
            mapped = values*gain
            if gain > 1:
                # Linear midtones plus a rational highlight shoulder. It is
                # monotone, C1 at the knee, preserves 0/255 and never hard clips.
                # A few white pixels must not veto exposure correction elsewhere.
                knee = 220.0
                threshold = knee/gain
                u = np.maximum(values-threshold, 0)/(255-threshold)
                slope = gain*(255-threshold)/(255-knee)
                shoulder = knee+(255-knee)*slope*u/(1+(slope-1)*u)
                mapped = np.where(values <= threshold, mapped, shoulder)
            y = cv2.LUT(y, np.clip(mapped, 0, 255).astype(np.uint8))
            self.previous_small, self.previous_mean, self.previous_std = small, mean, std
            self.shape, self.last_time = y.shape, now
        if cfg.enable_clahe:
            # Blending softens tile contrast changes and JPEG noise amplification.
            y = cv2.addWeighted(y, 0.5, self.clahe.apply(y), 0.5, 0)
        if cfg.enable_sharpen and cfg.sharpen_strength > 0:
            blur = cv2.GaussianBlur(y, (0, 0), 1.0)
            positive = cv2.subtract(y, blur)
            negative = cv2.subtract(blur, y)
            cv2.LUT(positive, self.sharpen_lut, dst=positive)
            cv2.LUT(negative, self.sharpen_lut, dst=negative)
            cv2.add(y, positive, dst=y)
            cv2.subtract(y, negative, dst=y)
        cv2.insertChannel(y, ycc, 0)
        return cv2.cvtColor(ycc, cv2.COLOR_YCrCb2BGR)

    def process(self, data: bytes) -> EnhancedImage:
        start = time.perf_counter()
        # imdecode has its own global pixel cap; also reject implausible output sizes.
        image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        decoded = time.perf_counter()
        if image is None or image.size == 0:
            raise ValueError("invalid image")
        h, w = image.shape[:2]
        if w*h > 12_000_000 or min(h, w) < 8:
            raise ValueError("unsupported image dimensions")
        output = self.apply(image)
        enhanced = time.perf_counter()
        enabled = any((self.config.enable_brightness_stabilization,
                       self.config.enable_clahe, self.config.enable_sharpen))
        if enabled:
            ok, encoded = cv2.imencode('.jpg', output, [cv2.IMWRITE_JPEG_QUALITY, 90])
            if not ok:
                raise ValueError("JPEG encoding failed")
            result = encoded.tobytes()
        else:
            result = data
        end = time.perf_counter()
        return EnhancedImage(result, (decoded-start)*1000, (enhanced-decoded)*1000,
                             (end-enhanced)*1000, w, h)
