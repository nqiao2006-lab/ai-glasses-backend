"""Configurable multimodal intent analysis with a safe mock provider."""

from __future__ import annotations

import base64
import json
import logging
import math
import os
from pathlib import Path
import re
from typing import Literal, Protocol

import httpx
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
logger = logging.getLogger("ai_glasses_backend.ai")

DEFAULT_TIMEOUT_SECONDS = 25.0
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4.1-mini"


class AgentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action: Literal["open_app", "unknown"]
    app_name: str | None
    target: str | None
    confirmation_required: bool
    message: str

    @model_validator(mode="after")
    def validate_action_fields(self) -> "AgentResponse":
        if self.action == "open_app" and not self.app_name:
            raise ValueError("open_app requires app_name")
        if self.action == "unknown" and self.app_name is not None:
            raise ValueError("unknown cannot include app_name")
        if not self.message.strip() or len(self.message) > 120:
            raise ValueError("message must contain 1 to 120 characters")
        return self


def safe_unknown() -> AgentResponse:
    return AgentResponse(
        action="unknown",
        app_name=None,
        target=None,
        confirmation_required=False,
        message="抱歉，我暂时无法确定您想执行的操作。",
    )


class AnalysisError(Exception):
    """A safe API-level analysis error."""


class Provider(Protocol):
    async def analyze(self, image: bytes, content_type: str, intent: str) -> AgentResponse: ...


class MockProvider:
    _apps = {
        "微信": ("微信", ("微信",)),
        "支付宝": ("支付宝", ("支付宝",)),
        "高德地图": ("高德地图", ("高德地图", "高德")),
        "电话": ("电话", ("电话", "拨号")),
        "相机": ("相机", ("相机", "拍照")),
        "设置": ("设置", ("设置",)),
    }

    async def analyze(self, image: bytes, content_type: str, intent: str) -> AgentResponse:
        del image, content_type
        normalized_intent = intent.strip()
        match = re.fullmatch(r"(?:请)?(?:帮我|帮忙|给我)?(?:打开|启动|进入)(.+?)[。！!]?", normalized_intent)
        if match:
            for app_name, aliases in self._apps.values():
                if match.group(1) in aliases:
                    return AgentResponse(
                        action="open_app",
                        app_name=app_name,
                        target=None,
                        confirmation_required=False,
                        message=f"我来帮您打开{app_name}。",
                    )
        return safe_unknown()


class OpenAICompatibleProvider:
    """Adapter for providers exposing OpenAI-compatible Chat Completions."""

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    @property
    def endpoint(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"

    async def analyze(self, image: bytes, content_type: str, intent: str) -> AgentResponse:
        encoded_image = base64.b64encode(image).decode("ascii")
        image_data_url = f"data:{content_type};base64,{encoded_image}"
        system_prompt = (
            "你是老人辅助眼镜的安全意图解析器。用户输入可能是语音转写。"
            "只处理明确的打开手机应用请求，action 只能是 open_app 或 unknown。"
            "支持的应用包括微信、支付宝、高德地图、电话、相机、设置；不得推断或执行其他操作。"
            "同时观察用户上传的原始图片，为识别应用相关上下文提供辅助；不要忽略图片。"
            "必须只输出 JSON 对象，字段严格为 action、app_name、target、confirmation_required、message。"
            "无法确定或不是打开应用请求时使用 unknown，app_name 和 target 为 null，"
            "confirmation_required 为 false，并以简短中文说明。不要输出 Markdown。"
        )
        request_body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"用户意图：{intent}"},
                        {"type": "image_url", "image_url": {"url": image_data_url}},
                    ],
                },
            ],
            "temperature": 0,
        }
        output_format = os.getenv("AI_OUTPUT_FORMAT", "json_schema")
        if output_format == "json_schema":
            request_body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "glasses_action", "strict": True,
                    "schema": AgentResponse.model_json_schema(),
                },
            }
        elif output_format == "json_object":
            request_body["response_format"] = {"type": "json_object"}
        elif output_format != "text":
            raise AnalysisError("AI_OUTPUT_FORMAT 必须是 json_schema、json_object 或 text。")

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    self.endpoint,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=request_body,
                )
        except httpx.TimeoutException as exc:
            logger.warning("Model API request timed out")
            raise AnalysisError("模型调用超时，请稍后重试。") from exc
        except httpx.RequestError as exc:
            logger.warning("Model API connection failed")
            raise AnalysisError("暂时无法连接模型服务，请检查网络或 API Base URL。") from exc

        if response.status_code in {401, 403}:
            logger.warning("Model API authentication failed: status=%s", response.status_code)
            raise AnalysisError("模型服务鉴权失败，请检查 API Key 和服务商配置。")
        if response.is_error:
            logger.error("Model API returned status %s", response.status_code)
            raise AnalysisError(f"模型服务返回错误（HTTP {response.status_code}），请稍后重试。")

        try:
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise TypeError("assistant content must be a string")
            parsed = json.loads(content)
            return AgentResponse.model_validate(parsed)
        except (ValueError, KeyError, IndexError, TypeError, ValidationError) as exc:
            logger.warning("Model response failed JSON/schema validation")
            return safe_unknown()


def _get_timeout() -> float:
    try:
        timeout = float(os.getenv("AI_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS)))
        if not math.isfinite(timeout) or timeout <= 0 or timeout > 120:
            raise ValueError
        return timeout
    except ValueError as exc:
        raise AnalysisError("AI_TIMEOUT_SECONDS 必须是大于 0 且不超过 120 的秒数。") from exc


async def analyze_image(image: bytes, content_type: str, intent: str) -> AgentResponse:
    """Analyze a user intent together with the original uploaded image."""
    mode = os.getenv("AI_MODE", "mock").strip().lower()
    if mode == "mock":
        provider: Provider = MockProvider()
    elif mode in {"openai_compatible", "openai-compatible"}:
        if os.getenv("AI_PROVIDER", "openai_compatible") != "openai_compatible":
            raise AnalysisError("当前适配器支持 AI_PROVIDER=openai_compatible。")
        api_key = os.getenv("AI_API_KEY", "").strip()
        if not api_key:
            raise AnalysisError("AI_MODE 已启用真实模型，但未配置 AI_API_KEY。")
        provider = OpenAICompatibleProvider(
            base_url=os.getenv("AI_BASE_URL", DEFAULT_BASE_URL).strip(),
            api_key=api_key,
            model=os.getenv("AI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL,
            timeout=_get_timeout(),
        )
    else:
        raise AnalysisError("AI_MODE 仅支持 mock 或 openai_compatible。")

    try:
        return await provider.analyze(image, content_type, intent)
    except AnalysisError:
        raise
    except Exception as exc:
        logger.warning("Unexpected model adapter failure")
        raise AnalysisError("AI 分析发生异常，请稍后重试。") from exc
