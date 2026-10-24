"""HTTP API for the AI glasses image-upload and intent-analysis prototype."""

import logging
import asyncio
import math
import os
import struct
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from uuid import uuid4

from fastapi import (FastAPI, File, Form, HTTPException, Request, Response,
                     UploadFile, WebSocket, WebSocketDisconnect)
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

from app.ai_service import AnalysisError, AgentResponse, analyze_image
from app.agent_routes import router as agent_router
from app.assistant_routes import router as assistant_router
from app.vision_service import VisionError, VisionFailure, VisionResponse, analyze_vision
from app.camera_preview import preview
from app.upload_cleanup import cleanup_expired
from app.enhancement_runtime import runtime
from app.image_enhancement import EnhancementConfig, ImageEnhancer

logger = logging.getLogger("ai_glasses_backend")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
MAX_IMAGE_BYTES = 10 * 1024 * 1024
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "uploads"))
ALLOWED_SIGNATURES = {
    "jpeg": (b"\xff\xd8\xff", ".jpg"),
    "png": (b"\x89PNG\r\n\x1a\n", ".png"),
}

def positive_seconds(name: str, default: str) -> float:
    value = float(os.getenv(name, default))
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return value


@asynccontextmanager
async def lifespan(app: FastAPI):
    retention = positive_seconds("UPLOAD_RETENTION_SECONDS", "30")
    interval = positive_seconds("UPLOAD_CLEANUP_INTERVAL_SECONDS", "5")

    async def sweep():
        while True:
            await asyncio.sleep(interval)
            await asyncio.to_thread(cleanup_expired, UPLOAD_DIR, retention)

    task = asyncio.create_task(sweep())
    enhancement_task = asyncio.create_task(runtime.run())
    logger.info("Upload expiry enabled: retention=%ss interval=%ss", retention, interval)
    try:
        yield
    finally:
        task.cancel()
        enhancement_task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        with suppress(asyncio.CancelledError):
            await enhancement_task


app = FastAPI(title="AI 老人辅助眼镜后端", version="0.2.0", lifespan=lifespan)
app.include_router(agent_router)
app.include_router(assistant_router)

# Empty by default: the bundled page uses same-origin relative URLs.
# Cross-origin browser clients must be explicitly listed, without URL paths.
cors_origins = [origin.strip() for origin in os.getenv("CORS_ALLOW_ORIGINS", "").split(",") if origin.strip()]
if any("*" in origin or origin == "null" for origin in cors_origins):
    raise ValueError("CORS_ALLOW_ORIGINS must contain explicit frontend origins, not wildcards or null")
if cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT"],
        allow_headers=["Content-Type", "If-None-Match", "Authorization"],
        expose_headers=["X-AI-Mode", "ETag", "X-Frame-Sequence", "X-Frame-Received-At",
                        "X-Frame-Source", "X-Enhancement", "X-Enhancement-Ms"],
    )


@app.post("/vision/analyze", response_model=VisionResponse, summary="独立视觉分析（无需语音或意图）",
          responses={code: {"model": VisionResponse} for code in (422, 502, 503, 504)})
async def vision_analyze(response: Response, image: UploadFile = File(...)) -> VisionResponse:
    """只分析原图，不保存到 uploads；上传临时文件在模型调用前关闭。"""
    data, content_type, _ = await read_valid_image(image)
    preview.publish(data, content_type, "/vision/analyze")
    try:
        result = await analyze_vision(data, content_type)
    except VisionFailure as exc:
        response.status_code = exc.http_status
        result = VisionResponse(status="unrecognized" if exc.code == "unrecognized" else "error",
                                mode="cloud", error=VisionError(code=exc.code, message=exc.message))
        logger.warning("Vision request failed: code=%s", exc.code)
    response.headers["X-AI-Mode"] = result.mode
    return result


@app.get("/", include_in_schema=False)
async def chinese_home() -> FileResponse:
    return FileResponse(Path(__file__).parent / "index.html", media_type="text/html")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/camera/performance", summary="接收和增强性能统计")
async def camera_performance():
    return runtime.stats()


@app.websocket("/ws/camera/upload")
async def camera_upload_stream(websocket: WebSocket):
    """One binary WS message = little-endian uint32 magic, uint32 seq, JPEG.

    No per-frame acknowledgements, disk writes or AI calls. Sequence numbers
    must increase within a connection; reconnecting may restart at zero.
    """
    await websocket.accept()
    session = preview.open_ws_session()
    preview.ws_connections += 1
    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            packet = message.get("bytes")
            if packet is None:
                await websocket.close(code=1003, reason="Binary frames required")
                break
            if len(packet) > MAX_IMAGE_BYTES + 8:
                await websocket.close(code=1009, reason="JPEG must not exceed 10 MB")
                break
            if len(packet) < 12:
                await websocket.close(code=1008, reason="Missing frame header or JPEG")
                break
            magic, sequence = struct.unpack_from("<II", packet)
            if magic != 0x4B564630:
                await websocket.close(code=1008, reason="Invalid frame magic")
                break
            if packet[8:11] != b"\xff\xd8\xff" or not packet.endswith(b"\xff\xd9"):
                await websocket.close(code=1008, reason="Complete JPEG required")
                break
            preview.publish_ws(packet[8:], sequence, session)
            # Buffered WS messages must not starve HTTP readers or the worker.
            await asyncio.sleep(0)
    except WebSocketDisconnect:
        pass
    finally:
        preview.ws_connections -= 1


@app.put("/camera/enhancement", summary="调整增强参数（仅当前进程生效）")
async def configure_enhancement(config: EnhancementConfig):
    runtime.configure(config)
    return config


@app.get("/camera/preview/{variant}", summary="同一帧原图/增强图")
async def enhanced_preview(variant: str, request: Request):
    if variant not in {"original", "enhanced"}:
        raise HTTPException(404, "Unknown preview variant")
    headers = {"Cache-Control": "no-store"}
    pair = runtime.pair
    if pair is None:
        return Response(status_code=204, headers=headers)
    original, enhanced = pair
    # Serve only a matched sequence when browser asks for the companion image.
    expected = request.query_params.get("sequence")
    if expected is not None and expected != str(original.sequence):
        return Response(status_code=409, headers=headers)
    frame = original if variant == "original" else enhanced
    tag = f'"{variant}-{runtime.generation}-{frame.sequence}"'
    headers.update({"ETag": tag, "X-Frame-Sequence": str(frame.sequence),
                    "X-Frame-Received-At": frame.received_at, "X-Frame-Source": frame.source})
    if request.headers.get("if-none-match") == tag:
        return Response(status_code=304, headers=headers)
    return Response(frame.data, media_type=frame.content_type, headers=headers)


@app.post("/image/enhance", summary="单张图片增强测试；失败回退原图")
async def enhance_uploaded_image(image: UploadFile = File(...)):
    data, content_type, _ = await read_valid_image(image)
    config = runtime.config.model_copy()
    try:
        result = await asyncio.to_thread(ImageEnhancer(config).process, data)
        enabled = any((config.enable_brightness_stabilization,
                       config.enable_clahe, config.enable_sharpen))
        return Response(result.data, media_type="image/jpeg" if enabled else content_type,
                        headers={"Cache-Control": "no-store", "X-Enhancement": "ok",
                                 "X-Enhancement-Ms": str(round(result.enhance_ms, 3))})
    except Exception:
        logger.warning("Single image enhancement failed; returning original")
        return Response(data, media_type=content_type,
                        headers={"Cache-Control": "no-store", "X-Enhancement": "fallback-original"})


@app.get("/camera/latest", summary="查看最近收到的图片（内存缓存）")
async def latest_camera_frame(request: Request) -> Response:
    frame = preview.latest
    headers = {"Cache-Control": "no-store"}
    if frame is None:
        return Response(status_code=204, headers=headers)
    headers.update({"ETag": f'"frame-{frame.sequence}"',
                    "X-Frame-Sequence": str(frame.sequence),
                    "X-Frame-Received-At": frame.received_at,
                    "X-Frame-Source": frame.source})
    if request.headers.get("if-none-match") == headers["ETag"]:
        return Response(status_code=304, headers=headers)
    return Response(frame.data, media_type=frame.content_type, headers=headers)


@app.post("/camera/frame", summary="仅接收预览帧，不保存、不调用 AI")
async def camera_frame(file: UploadFile = File(...)) -> dict[str, object]:
    data, content_type, _ = await read_valid_image(file)
    frame = preview.publish(data, content_type, "/camera/frame")
    return {"success": True, "sequence": frame.sequence, "size": len(data)}


async def read_valid_image(file: UploadFile) -> tuple[bytes, str, str]:
    content_type = (file.content_type or "").lower()
    if content_type not in {"image/jpeg", "image/png"}:
        await file.close()
        raise HTTPException(status_code=415, detail="Only JPEG or PNG images are supported")

    try:
        data = await file.read(MAX_IMAGE_BYTES + 1)
    finally:
        await file.close()

    if not data:
        raise HTTPException(status_code=400, detail="Uploaded image must not be empty")
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="Image must not exceed 10 MB")

    image_kind = next(
        (kind for kind, (signature, _) in ALLOWED_SIGNATURES.items() if data.startswith(signature)),
        None,
    )
    expected_kind = "jpeg" if content_type == "image/jpeg" else "png"
    if image_kind != expected_kind:
        raise HTTPException(status_code=415, detail="Image content does not match its JPEG/PNG type")
    return data, content_type, image_kind


@app.post("/upload")
async def upload_image(file: UploadFile = File(...)) -> dict[str, object]:
    """Save an image using the existing multipart field name `file`."""
    data, content_type, image_kind = await read_valid_image(file)
    preview.publish(data, content_type, "/upload")
    _, extension = ALLOWED_SIGNATURES[image_kind]
    saved_name = f"{uuid4().hex}{extension}"
    destination = UPLOAD_DIR / saved_name
    try:
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    except OSError:
        logger.exception("Unable to save uploaded image")
        raise HTTPException(status_code=500, detail="Unable to save uploaded image") from None

    logger.info("Image uploaded: filename=%s size=%d", saved_name, len(data))
    return {"success": True, "filename": saved_name, "size": len(data)}


@app.post("/analyze", response_model=AgentResponse)
async def analyze_uploaded_image(
    response: Response,
    image: UploadFile = File(...),
    intent: str = Form(..., max_length=1000),
) -> AgentResponse:
    """Analyze the original image and a simulated/transcribed user intent."""
    data, content_type, _ = await read_valid_image(image)
    preview.publish(data, content_type, "/analyze")
    if not intent.strip():
        raise HTTPException(status_code=400, detail="intent must not be empty")
    response.headers["X-AI-Mode"] = os.getenv("AI_MODE", "mock").strip().lower()

    try:
        result = await analyze_image(data, content_type, intent.strip())
    except AnalysisError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    logger.info("Image intent analyzed: size=%d", len(data))
    return result
