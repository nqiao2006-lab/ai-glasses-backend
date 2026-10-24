"""Tencent Cloud GeneralAccurateOCR adapter for the medicine vision service.

This module is the only place that knows about Tencent Cloud. It exposes the
same two entry points as the local PaddleOCR path in ``medicine_ocr``::

    create_engine()                 -> engine object
    recognize(engine, image, src)   -> list of line dicts

Returned line dicts are byte-for-byte compatible with the PaddleOCR path::

    {"text", "line_index", "ocr_confidence", "bbox", "source_image"}

so ``ocr_layout``, ``extract_fields`` and the API layer need no changes.
Coordinates are reported in the original (EXIF-upright) pixel space even when
the image had to be scaled down before upload, because ``ocr_layout`` applies
absolute pixel thresholds such as height > 35 px.

Engine selection is driven by the environment, see ``ENGINE_NAME`` in
``medicine_ocr``.
"""

from __future__ import annotations

import base64
import io
import os
import time
from pathlib import Path

ENGINE_LABEL = "TencentCloud-GeneralAccurateOCR"

# The API rejects images whose Base64 payload exceeds 10 MB. Keep headroom so
# that the final encode rarely has to fall through to a lower quality.
MAX_BASE64_BYTES = 9 * 1024 * 1024
# Guard rail for very large uploads; also caps what we pay to upload.
MAX_LONG_EDGE = 2000
JPEG_QUALITIES = (95, 88, 80, 72, 64)
HTTP_TIMEOUT = 20
MAX_ATTEMPTS = 2

# WordsType is a STRING in this API, not an integer. Sending int 2 makes the
# server answer InvalidParameter; "2" is what it wants.
#   "0" = auto (all kinds of text)  "1" = handwriting only  "2" = printed only
# It only takes effect while ConfigID is OCR and IsWords is false, both of
# which hold here. Set VISION_OCR_WORDS_TYPE= to omit the parameter entirely.
DEFAULT_WORDS_TYPE = "2"

# Deterministic failures (bad parameter, bad signature, exhausted quota) fail
# the same way every time, so retrying them only wastes the caller's patience.
NON_RETRYABLE_CODES = ("InvalidParameter", "InvalidParameterValue",
                       "AuthFailure", "UnauthorizedOperation", "LimitExceeded",
                       "MissingParameter", "UnsupportedOperation")


class ConfigurationError(RuntimeError):
    """Raised when the Tencent Cloud credentials are missing or unusable."""


def _setting(name, default=""):
    """Read one OCR setting, preferring the short ``OCR_*`` name.

    The main backend owns the ``VISION_*`` namespace, and the local launcher
    (``scripts/local_deploy.py``) deliberately strips every ``VISION_*``
    variable before starting this service, so that the main backend's
    cloud-model settings (``VISION_BASE_URL``, ``ARK_API_KEY``, ...) can never
    bleed into the OCR process. A ``VISION_OCR_*`` name therefore cannot reach
    us through that launcher. The short form is the one that survives; the long
    form is still honoured when something else starts the service.
    """
    short = "OCR_" + name[len("VISION_OCR_"):] if name.startswith("VISION_OCR_") else name
    for key in (short, name):
        value = os.environ.get(key)
        if value is not None:
            return value.strip()
    return default.strip()


def _b64_size(raw_bytes):
    return ((raw_bytes + 2) // 3) * 4


def _load_picture(image):
    """Return an EXIF-upright RGB PIL image.

    Accepts a filesystem path, a BGR numpy array (what the service worker
    passes, because that is what PaddleOCR expects) or a PIL image.
    """
    import numpy as np
    from PIL import Image, ImageOps

    if isinstance(image, Image.Image):
        return ImageOps.exif_transpose(image).convert("RGB")
    if isinstance(image, (str, Path)):
        with Image.open(image) as handle:
            return ImageOps.exif_transpose(handle).convert("RGB")
    array = np.asarray(image)
    if array.ndim == 3 and array.shape[2] == 3:
        array = array[:, :, ::-1]
    return Image.fromarray(array)


def _encode(image):
    """Encode for upload. Returns (jpeg bytes, scale applied to coordinates)."""
    from PIL import Image

    picture = _load_picture(image)
    original_width, original_height = picture.size
    longest = max(picture.size)
    if longest > MAX_LONG_EDGE:
        factor = MAX_LONG_EDGE / longest
        resized = (max(1, round(original_width * factor)),
                   max(1, round(original_height * factor)))
        picture = picture.resize(resized, Image.LANCZOS)
        scale = resized[0] / original_width
    else:
        scale = 1.0

    payload = None
    for quality in JPEG_QUALITIES:
        buffer = io.BytesIO()
        picture.save(buffer, format="JPEG", quality=quality, optimize=True)
        candidate = buffer.getvalue()
        payload = candidate
        if _b64_size(len(candidate)) <= MAX_BASE64_BYTES:
            return payload, scale
    return payload, scale


def _field(item, name):
    """Read a field from either a SDK model object or a plain dict."""
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


def _rectangle(polygon, scale):
    """Convert a Tencent polygon into the axis-aligned form used downstream."""
    points = []
    for point in polygon or []:
        x = _field(point, "X")
        y = _field(point, "Y")
        if isinstance(x, (int, float)) and isinstance(y, (int, float)):
            points.append((float(x), float(y)))
    if len(points) < 4:
        return None
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    left, right = min(xs), max(xs)
    top, bottom = min(ys), max(ys)
    if scale and scale != 1.0:
        inverse = 1.0 / scale
        left, right = left * inverse, right * inverse
        top, bottom = top * inverse, bottom * inverse
    return [[left, top], [right, top], [right, bottom], [left, bottom]]


def _confidence(value):
    """Tencent reports 0-100; the Paddle path reports 0-1."""
    if not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number > 1.0:
        number = number / 100.0
    return min(max(number, 0.0), 1.0)


def create_engine():
    """Build a credentialed OCR client. Raises ConfigurationError if unusable."""
    secret_id = _setting("TENCENTCLOUD_SECRET_ID")
    secret_key = _setting("TENCENTCLOUD_SECRET_KEY")
    region = _setting("TENCENTCLOUD_OCR_REGION", "ap-guangzhou")
    if not secret_id or not secret_key:
        raise ConfigurationError(
            "缺少腾讯云密钥。请设置环境变量 TENCENTCLOUD_SECRET_ID 与 "
            "TENCENTCLOUD_SECRET_KEY，或改用本地引擎 VISION_OCR_ENGINE=paddle。")
    try:
        from tencentcloud.common import credential
        from tencentcloud.common.profile import client_profile, http_profile
        from tencentcloud.ocr.v20181119 import ocr_client
    except ImportError as error:
        raise ConfigurationError(
            "未安装腾讯云 SDK。请先执行 pip install tencentcloud-sdk-python-ocr。") from error

    timeout = int(_setting("VISION_OCR_HTTP_TIMEOUT", str(HTTP_TIMEOUT)) or HTTP_TIMEOUT)
    profile = http_profile.HttpProfile()
    profile.endpoint = "ocr.tencentcloudapi.com"
    profile.reqTimeout = max(5, timeout)
    engine = ocr_client.OcrClient(credential.Credential(secret_id, secret_key), region,
                                  client_profile.ClientProfile(httpProfile=profile))
    engine.engine_label = ENGINE_LABEL
    return engine


def _is_retryable(error):
    """A bad parameter or a bad signature will not fix itself on the 2nd try."""
    code = str(getattr(error, "code", "") or "")
    return not code.startswith(NON_RETRYABLE_CODES)


def _call(engine, request):
    from tencentcloud.common.exception.tencent_cloud_sdk_exception import (
        TencentCloudSDKException)

    attempts = int(_setting("VISION_OCR_ATTEMPTS", str(MAX_ATTEMPTS)) or MAX_ATTEMPTS)
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return engine.GeneralAccurateOCR(request)
        except TencentCloudSDKException as error:
            if attempt >= max(1, attempts) or not _is_retryable(error):
                raise
            time.sleep(1.0)


def recognize(engine, image, source):
    """Run one image through GeneralAccurateOCR and adapt the result."""
    from tencentcloud.ocr.v20181119 import models

    payload, scale = _encode(image)
    request = models.GeneralAccurateOCRRequest()
    request.ImageBase64 = base64.b64encode(payload).decode("ascii")
    # Medicine boxes are printed. Filtering out handwriting keeps a pharmacist's
    # scribble on the box from being read as content. Note the value is a string.
    words_type = _setting("VISION_OCR_WORDS_TYPE", DEFAULT_WORDS_TYPE)
    if words_type:
        request.WordsType = words_type
    request.IsWords = False
    request.EnableDetectSplit = False

    response = _call(engine, request)
    detections = _field(response, "TextDetections") or []
    lines = []
    for index, item in enumerate(detections):
        text = _field(item, "DetectedText")
        if text is None:
            continue
        lines.append(dict(text=str(text), line_index=index,
                          ocr_confidence=_confidence(_field(item, "Confidence")),
                          bbox=_rectangle(_field(item, "Polygon"), scale),
                          source_image=str(source)))
    return lines


def probe(engine):
    """Spend one paid call to prove the credentials and network path work.

    Any parsed response without an API-level error counts as a pass. Drawing
    text at an explicit size requires Pillow >= 10.1.
    """
    from PIL import Image, ImageDraw, ImageFont

    try:
        font = ImageFont.load_default(size=48)
    except TypeError:
        font = ImageFont.load_default()
    canvas = Image.new("RGB", (800, 600), "white")
    ImageDraw.Draw(canvas).text((40, 40), "2026-10-24", fill="black", font=font)
    lines = recognize(engine, canvas, "startup-probe")
    print(f"腾讯云 OCR 启动自检通过，样本返回 {len(lines)} 行。", flush=True)
    return True
