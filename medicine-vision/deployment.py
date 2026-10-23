"""Deployment adapter; the delivered OCR implementation remains byte-for-byte intact."""
from contextlib import asynccontextmanager
import math
from urllib.parse import urlsplit
import os

from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse

from vision_service.api import create_app as create_task_app
from vision_service.config import Settings


def frontend_origins(value):
    origins = []
    for origin in value.split(','):
        origin = origin.strip()
        if not origin:
            continue
        parsed = urlsplit(origin)
        # Origins, not URLs to individual pages. Never enable wildcard credentials.
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname
                or parsed.username or parsed.password or parsed.path
                or parsed.query or parsed.fragment or '*' in origin
                or any(char.isspace() for char in origin)):
            raise ValueError('VISION_CORS_ALLOW_ORIGINS requires explicit http(s) origins without paths')
        parsed.port  # Validate malformed or out-of-range ports as well.
        origins.append(origin)
    return origins


def create_app(settings=None, backend=None):
    settings = settings or Settings.from_env()
    token = settings.api_token
    if (not token or not token.isascii() or any(char.isspace() for char in token)):
        raise ValueError('Set a nonempty ASCII VISION_API_TOKEN without whitespace before starting OCR')
    for name in ('inference_timeout', 'startup_timeout', 'waiting_timeout'):
        value = getattr(settings, name)
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f'{name} must be finite and positive')
    origins = frontend_origins(os.environ.get('VISION_CORS_ALLOW_ORIGINS', ''))
    app = create_task_app(settings=settings, backend=backend)
    runtime = app.state.runtime

    @asynccontextmanager
    async def lifespan(application):
        runtime.start()
        try:
            yield
        finally:
            runtime.stop()

    app.router.lifespan_context = lifespan
    if origins:
        app.add_middleware(
            CORSMiddleware, allow_origins=origins, allow_credentials=False,
            allow_methods=['GET', 'POST', 'PUT', 'PATCH'],
            allow_headers=['Authorization', 'Content-Type', 'X-Task-Id', 'X-Frame-Id'],
        )

    @app.get('/ready', summary='200 only when the OCR model and SQLite are ready')
    def ready():
        data = runtime.health()
        return JSONResponse(status_code=200 if data['ready'] else 503, content=data)

    # The original dependency enforces auth; also describe it for Swagger and client generators.
    def openapi():
        if app.openapi_schema is None:
            schema = get_openapi(title=app.title, version=app.version,
                                 description=app.description, routes=app.routes)
            schema.setdefault('components', {}).setdefault('securitySchemes', {})['VisionBearer'] = {
                'type': 'http', 'scheme': 'bearer',
                'description': 'OCR service VISION_API_TOKEN (not a cloud-model API key)',
            }
            for path, operations in schema['paths'].items():
                if path.startswith('/api/v1/vision/tasks'):
                    for method in ('get', 'post', 'put', 'patch'):
                        if method in operations:
                            operations[method]['security'] = [{'VisionBearer': []}]
            app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = openapi
    return app
