"""Opt-in current-frame assistant. Existing camera and OCR contracts are untouched."""
import asyncio
import base64
import hashlib
import os
import re
import time
from typing import Literal
from uuid import uuid4
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.security import HTTPBearer
from pydantic import BaseModel, ConfigDict

from app.agent_routes import AgentError, AgentRoute, ObjectRequest, object_recognition
from app.camera_preview import preview

router = APIRouter(prefix='/api/v1/assistant', tags=['多功能视觉助手'], route_class=AgentRoute,
                   dependencies=[Depends(HTTPBearer(auto_error=False))])
analysis_lock = asyncio.Lock()


class AssistantRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    mode: Literal['read_text', 'objects', 'scene']


def ocr_base():
    # Operator configuration only; never accept a destination URL from the browser.
    base = os.getenv('ASSISTANT_OCR_BASE_URL',
                     'https://medicine-vision-ocr.onrender.com').strip().rstrip('/')
    try:
        parsed = urlsplit(base)
        _ = parsed.port
        url = httpx.URL(base)
    except (httpx.InvalidURL, ValueError):
        raise AgentError('configuration_error', 'OCR 地址格式不正确。') from None
    if url.scheme != 'https' or not url.host or url.username or url.password or url.query or url.fragment:
        raise AgentError('configuration_error', 'OCR 地址必须是无凭证和查询参数的 HTTPS 地址。')
    return base


def ocr_headers(request):
    # Browser credentials stay in memory; never read a local test-token file on the server.
    token = request.headers.get('x-ocr-token', '').strip() or os.getenv('ASSISTANT_OCR_TOKEN', '').strip()
    if not token or any(not 33 <= ord(c) <= 126 for c in token):
        raise AgentError('ocr_token_required', '请填写有效的 OCR 访问令牌，或配置 ASSISTANT_OCR_TOKEN。', 422)
    return {'Authorization': 'Bearer ' + token}


async def ocr_call(method, path, headers, **kwargs):
    try:
        async with asyncio.timeout(35):
            async with httpx.AsyncClient(timeout=35, follow_redirects=False) as client:
                response = await client.request(method, ocr_base() + path, headers=headers, **kwargs)
    except (httpx.TimeoutException, TimeoutError):
        raise AgentError('ocr_timeout', 'OCR 请求超时；请按原任务编号继续查询，不要重复提交。', 504) from None
    except httpx.RequestError:
        raise AgentError('ocr_unavailable', '暂时无法连接 OCR 服务。', 502) from None
    if response.status_code in (401, 403):
        raise AgentError('ocr_unauthorized', 'OCR 令牌无效；与本网站访问令牌可能不同。', 401)
    if not response.is_success:
        raise AgentError('ocr_upstream_error', f'OCR 服务返回 HTTP {response.status_code}。', 502)
    try:
        data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get('status'), str):
            raise ValueError
        return data
    except ValueError:
        raise AgentError('ocr_invalid_output', 'OCR 服务返回格式异常。', 502) from None


@router.get('/capabilities')
async def capabilities():
    return {'modes': ['read_text', 'objects', 'scene'],
            'vision_configured': bool(os.getenv('VISION_QWEN_API_KEY', '').strip()),
            'ocr_token_configured': bool(os.getenv('ASSISTANT_OCR_TOKEN', '').strip()),
            'ocr_destination': ocr_base(), 'automatic_analysis': False}


@router.post('/analyze')
async def analyze(body: AssistantRequest, request: Request):
    if analysis_lock.locked():
        raise AgentError('assistant_busy', '已有识别请求正在提交，请稍后再试。', 429)
    async with analysis_lock:
        frame = preview.latest
        if frame is None:
            raise AgentError('no_camera_frame', '尚未收到摄像头画面。', 409)
        age = time.monotonic() - frame.received_monotonic
        if age > 5:
            raise AgentError('stale_camera_frame', '最近画面已超过5秒未更新，请恢复硬件上传后重试。', 409)
        data, content_type = frame.data, frame.content_type
        metadata = {'sequence': frame.sequence, 'received_at': frame.received_at,
                    'source': frame.source,
                    'original_sha256': hashlib.sha256(frame.data).hexdigest(),
                    'submitted_sha256': hashlib.sha256(data).hexdigest()}
        if body.mode == 'read_text':
            if content_type != 'image/jpeg':
                raise AgentError('jpeg_required', 'OCR 需要 JPEG 画面，请使用硬件 JPEG 上传。', 415)
            if len(data) > 8 * 1024 * 1024:
                raise AgentError('ocr_image_too_large', 'OCR 图片不能超过8 MiB。', 413)
            headers = ocr_headers(request)
            task_id = 'assistant-' + uuid4().hex
            await ocr_call('POST', '/api/v1/vision/tasks', headers,
                           json={'task_id': task_id, 'question': '请读出图片中的文字', 'expected_frames': 1})
            try:
                task = await ocr_call('PUT', f'/api/v1/vision/tasks/{task_id}/frames/f0',
                                      {**headers, 'Content-Type': 'image/jpeg'}, content=data)
            except AgentError as exc:
                raise AgentError(exc.code, exc.message + f' 本次任务编号：{task_id}', exc.status) from None
            return {'mode': body.mode, 'frame': metadata, 'task_id': task_id, 'ocr': task}
        question = ('请识别图片中可确认的主要物品，说明外观和相对位置；无法确认时明确说无法确认。'
                    if body.mode == 'objects' else
                    '请描述图片中可见的场景、主要物体和相对位置；不要推断不可见信息或保证通行安全。')
        if len(data) > 8 * 1024 * 1024:
            raise AgentError('image_too_large', '视觉识别图片不能超过8 MiB。', 413)
        image = f'data:{content_type};base64,' + base64.b64encode(data).decode('ascii')
        answer = await object_recognition(ObjectRequest(question=question, images=[image]))
        return {'mode': body.mode, 'frame': metadata, **answer}


@router.get('/ocr/{task_id}')
async def ocr_result(task_id: str, request: Request):
    if not re.fullmatch(r'assistant-[0-9a-f]{32}', task_id):
        raise AgentError('invalid_task_id', '只能查询本助手创建的 OCR 任务编号。', 422)
    return await ocr_call('GET', '/api/v1/vision/tasks/' + task_id, ocr_headers(request))
