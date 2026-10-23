from __future__ import annotations

from contextlib import asynccontextmanager
import hashlib
import json
import logging
import re
import secrets
import sqlite3
import uuid

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.concurrency import run_in_threadpool

from .config import Settings
from .quality import inspect_jpeg
from .runtime import VisionRuntime
from .store import Problem, WAITING

ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$')
LOG = logging.getLogger('vision_service')


def check_id(value):
    if not ID.fullmatch(value):
        raise Problem('invalid_id','编号须为1–64位英文字母、数字、下划线或短横线。',422)
    return value


class NewTask(BaseModel):
    model_config = ConfigDict(extra='forbid',strict=True)
    task_id: str | None = Field(default=None,max_length=64)
    question: str | None = Field(default=None,max_length=1000)
    expected_frames: int = Field(default=1,ge=1,le=4)

    @field_validator('question')
    @classmethod
    def strip_question(cls,value):
        return value.strip() or None if value is not None else None


class Question(BaseModel):
    model_config = ConfigDict(extra='forbid',strict=True)
    question: str = Field(min_length=1,max_length=1000)

    @field_validator('question')
    @classmethod
    def not_empty(cls,value):
        if not value.strip():
            raise ValueError('Question cannot be blank')
        return value.strip()


def create_app(settings=None,backend=None):
    settings = settings or Settings.from_env()
    runtime = VisionRuntime(settings,backend)

    @asynccontextmanager
    async def lifespan(app):
        runtime.start()
        yield
        runtime.stop()

    app = FastAPI(title='智能眼镜视觉服务',version='1.0.0',lifespan=lifespan,
                  description='PaddleOCR + explicit-label extraction. No object-recognition or generative VQA model.')
    app.state.runtime = runtime

    @app.exception_handler(Problem)
    async def problem_handler(request,exc):
        return JSONResponse(status_code=exc.status,content=dict(error=dict(code=exc.code,message=exc.message)))

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request,exc):
        # Do not echo user data or internal Pydantic exception context.
        return JSONResponse(status_code=422,content=dict(error=dict(code='invalid_request',message='请求字段或格式不正确，请参阅接口文档。')))

    @app.exception_handler(sqlite3.Error)
    async def database_handler(request,exc):
        LOG.error('Database request failed: %s',type(exc).__name__)
        return JSONResponse(status_code=503,content=dict(error=dict(code='database_unavailable',message='任务数据库暂不可用。')))

    @app.exception_handler(OSError)
    async def storage_handler(request,exc):
        LOG.error('Storage request failed: %s',type(exc).__name__)
        return JSONResponse(status_code=503,content=dict(error=dict(code='storage_unavailable',message='服务存储暂不可用。')))

    async def authorize(request: Request):
        if settings.api_token:
            provided = request.headers.get('authorization','')
            if not secrets.compare_digest(provided.encode(),('Bearer '+settings.api_token).encode()):
                raise Problem('unauthorized','需要有效的访问凭证。',401)

    @app.middleware('http')
    async def json_body_limit(request,call_next):
        # JSON request limits as well as streaming JPEG limits; never buffer arbitrary bodies.
        if request.method in {'POST','PATCH'}:
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body)>16384:
                    return JSONResponse(status_code=413,content=dict(error=dict(code='request_too_large',message='请求内容过大。')))
            request._body = bytes(body)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        return response

    def snapshot(task):
        row = runtime.store.get(task)
        result = row.pop('result')
        row.update(answer=result['answer'] if result else '',
                   evidence_text=result['evidence_text'] if result else '',result=result,
                   frame_id=row['frames'][0]['frame_id'] if len(row['frames'])==1 else None)
        return row

    @app.get('/health')
    def health():
        data = runtime.health()
        return JSONResponse(status_code=503 if data['status']=='degraded' else 200,content=data)

    @app.post('/api/v1/vision/tasks',dependencies=[Depends(authorize)])
    def create_task(body: NewTask):
        task = check_id(body.task_id or uuid.uuid4().hex)
        created = runtime.store.create(task,body.question,body.expected_frames)
        return JSONResponse(status_code=201 if created else 200,content=snapshot(task))

    @app.patch('/api/v1/vision/tasks/{task_id}',dependencies=[Depends(authorize)])
    def set_question(task_id: str,body: Question):
        check_id(task_id)
        runtime.store.question(task_id,body.question)
        return snapshot(task_id)

    @app.put('/api/v1/vision/tasks/{task_id}/frames/{frame_id}',dependencies=[Depends(authorize)],
             openapi_extra={'requestBody': {'required': True, 'content': {
                 'image/jpeg': {'schema': {'type': 'string', 'format': 'binary'}}}}})
    async def upload(task_id: str,frame_id: str,request: Request):
        check_id(task_id)
        check_id(frame_id)
        if any(request.headers.get(header,value)!=value for header,value in [('x-task-id',task_id),('x-frame-id',frame_id)]):
            raise Problem('id_mismatch','请求头与路径的 task_id/frame_id 不一致。')
        record = await run_in_threadpool(runtime.store.get,task_id)
        content_type = request.headers.get('content-type','').split(';')[0].lower()
        if content_type != 'image/jpeg':
            raise Problem('unsupported_media_type','Content-Type 必须为 image/jpeg。',415)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body)>settings.max_upload_bytes:
                raise Problem('upload_too_large','JPEG 超过8 MiB限制。',413)
        data = bytes(body)
        try:
            quality = await run_in_threadpool(inspect_jpeg,data,settings.max_image_pixels)
        except Problem as exc:
            if record['status'] in WAITING:
                await run_in_threadpool(runtime.store.fail,task_id,exc.code,exc.message,allowed=WAITING)
            raise
        digest = hashlib.sha256(data).hexdigest()
        # A unique storage name is independent of untrusted task/frame identifiers.
        directory = settings.data_dir / 'frames'
        directory.mkdir(parents=True,exist_ok=True)
        path = directory / (uuid.uuid4().hex+'.jpg')
        await run_in_threadpool(path.write_bytes,data)
        try:
            inserted = await run_in_threadpool(runtime.store.frame,task_id,frame_id,digest,path,quality)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        if not inserted:
            path.unlink(missing_ok=True)
        result = await run_in_threadpool(snapshot,task_id)
        result['duplicate_upload'] = not inserted
        return JSONResponse(status_code=202 if inserted else 200,content=result)

    @app.get('/api/v1/vision/tasks/{task_id}',dependencies=[Depends(authorize)])
    def get_task(task_id: str):
        return snapshot(check_id(task_id))

    return app
