from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from backend.providers.models.base import (
    GeneratedImage,
    ImageGenerationRequest,
    TextGenerationRequest,
    TextGenerationResult,
)
from backend.providers.models.errors import ProviderError, ProviderErrorCode


@dataclass(frozen=True)
class SiliconFlowConfig:
    api_key: str
    text_model: str
    image_model: str
    base_url: str = "https://api.siliconflow.cn/v1"
    image_size: str = "1024x1024"
    image_steps: int = 20
    image_guidance_scale: float = 7.5

    @classmethod
    def from_env(cls) -> SiliconFlowConfig:
        text_api_key = os.getenv("TEXT_MODEL_API_KEY", "").strip()
        image_api_key = os.getenv("IMAGE_MODEL_API_KEY", "").strip()
        shared_api_key = os.getenv("SILICONFLOW_API_KEY", "").strip()
        if text_api_key and image_api_key and text_api_key != image_api_key:
            raise ValueError("SiliconFlow text and image API keys must be identical")
        required = {
            "api_key": shared_api_key or text_api_key or image_api_key,
            "text_model": (
                os.getenv("SILICONFLOW_TEXT_MODEL", "").strip()
                or os.getenv("TEXT_MODEL_NAME", "").strip()
            ),
            "image_model": (
                os.getenv("SILICONFLOW_IMAGE_MODEL", "").strip()
                or os.getenv("IMAGE_MODEL_NAME", "").strip()
            ),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError("Missing SiliconFlow configuration: " + ", ".join(missing))
        return cls(
            **required,
            base_url=os.getenv("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1").rstrip("/"),
            image_size=os.getenv("SILICONFLOW_IMAGE_SIZE", "1024x1024"),
        )


class _SiliconFlowClient:
    provider_name = "siliconflow"

    def __init__(
        self,
        config: SiliconFlowConfig,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self.config = config
        self._client = client or httpx.Client(
            base_url=config.base_url,
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
            },
        )

    def _post(self, path: str, *, json_body: dict[str, Any], timeout: int) -> httpx.Response:
        try:
            response = self._client.post(path, json=json_body, timeout=timeout)
        except httpx.TimeoutException as error:
            raise ProviderError(
                ProviderErrorCode.TIMEOUT,
                "硅基流动请求超时，请稍后重试。",
                retryable=True,
            ) from error
        except httpx.RequestError as error:
            raise ProviderError(
                ProviderErrorCode.UNAVAILABLE,
                "暂时无法连接硅基流动，请稍后重试。",
                retryable=True,
            ) from error
        if response.is_success:
            return response
        self._raise_for_response(response)

    @staticmethod
    def _raise_for_response(response: httpx.Response) -> None:
        status = response.status_code
        if status in (401, 403):
            code, retryable = ProviderErrorCode.AUTH_FAILED, False
        elif status == 429:
            code, retryable = ProviderErrorCode.RATE_LIMITED, True
        elif status == 504:
            code, retryable = ProviderErrorCode.TIMEOUT, True
        elif status in (500, 502, 503):
            code, retryable = ProviderErrorCode.UNAVAILABLE, True
        else:
            code, retryable = ProviderErrorCode.CONTENT_REJECTED, False
        retry_after = response.headers.get("retry-after")
        raise ProviderError(
            code,
            f"硅基流动请求失败（HTTP {status}）。",
            retryable=retryable,
            retry_after_seconds=float(retry_after) if retry_after else None,
        )


class SiliconFlowTextModelProvider(_SiliconFlowClient):
    def __init__(
        self,
        config: SiliconFlowConfig,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        super().__init__(config, client=client)
        self.model_name = config.text_model

    def generate_structured(
        self,
        request: TextGenerationRequest,
    ) -> TextGenerationResult:
        started = time.monotonic()
        messages = [message.model_dump(mode="json") for message in request.messages]
        messages[-1]["content"] += "\n\noutput_schema:\n" + json.dumps(
            request.json_schema,
            ensure_ascii=False,
            sort_keys=True,
        )
        response = self._post(
            "/chat/completions",
            json_body={
                "model": self.config.text_model,
                "messages": messages,
                "stream": False,
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
            },
            timeout=request.timeout_seconds,
        )
        data = response.json()
        try:
            choice = data["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise ProviderError(
                ProviderErrorCode.CONTENT_REJECTED,
                "硅基流动返回了无法识别的文本响应。",
                retryable=False,
            ) from error
        try:
            parsed_content: Any = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            parsed_content = content
        usage = data.get("usage") or {}
        return TextGenerationResult(
            provider=self.provider_name,
            model=data.get("model") or self.config.text_model,
            content_json=parsed_content,
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            latency_ms=int((time.monotonic() - started) * 1_000),
            provider_request_id=(
                response.headers.get("x-siliconcloud-trace-id")
                or data.get("id")
                or request.request_id
            ),
            finish_reason=choice.get("finish_reason") or "unknown",
        )


class SiliconFlowImageModelProvider(_SiliconFlowClient):
    def __init__(
        self,
        config: SiliconFlowConfig,
        *,
        client: httpx.Client | None = None,
        reference_image_resolver: Callable[[str], str] | None = None,
    ) -> None:
        super().__init__(config, client=client)
        self.model_name = config.image_model
        self._reference_image_resolver = reference_image_resolver

    def generate(self, request: ImageGenerationRequest) -> list[GeneratedImage]:
        started = time.monotonic()
        json_body: dict[str, Any] = {
            "model": self.config.image_model,
            "prompt": request.prompt,
            "negative_prompt": request.negative_prompt,
            "image_size": self.config.image_size,
            "batch_size": request.count,
            "num_inference_steps": self.config.image_steps,
            "guidance_scale": self.config.image_guidance_scale,
        }
        if request.reference_artifact_ids:
            if self._reference_image_resolver is None:
                raise ProviderError(
                    ProviderErrorCode.CONTENT_REJECTED,
                    "图片生成需要引用资产，但尚未配置安全下载地址解析器。",
                    retryable=False,
                )
            references = [
                self._reference_image_resolver(artifact_id)
                for artifact_id in request.reference_artifact_ids[:3]
            ]
            for field_name, reference in zip(
                ("image", "image2", "image3"), references, strict=False
            ):
                json_body[field_name] = reference
        response = self._post(
            "/images/generations",
            json_body=json_body,
            timeout=request.timeout_seconds,
        )
        data = response.json()
        image_items = data.get("images") or []
        if len(image_items) != request.count:
            raise ProviderError(
                ProviderErrorCode.CONTENT_REJECTED,
                "硅基流动返回的图片数量与请求不一致。",
                retryable=False,
            )
        try:
            width, height = (int(part) for part in self.config.image_size.split("x", 1))
        except (TypeError, ValueError) as error:
            raise ValueError("SILICONFLOW_IMAGE_SIZE must use WIDTHxHEIGHT") from error
        trace_id = response.headers.get("x-siliconcloud-trace-id") or request.request_id
        results: list[GeneratedImage] = []
        for index, item in enumerate(image_items):
            image_url = item.get("url")
            if not image_url:
                raise ProviderError(
                    ProviderErrorCode.CONTENT_REJECTED,
                    "硅基流动图片响应缺少下载地址。",
                    retryable=False,
                )
            try:
                download = self._client.get(image_url, timeout=request.timeout_seconds)
                download.raise_for_status()
            except httpx.TimeoutException as error:
                raise ProviderError(
                    ProviderErrorCode.TIMEOUT,
                    "硅基流动图片下载超时。",
                    retryable=True,
                ) from error
            except httpx.HTTPError as error:
                raise ProviderError(
                    ProviderErrorCode.UNAVAILABLE,
                    "硅基流动图片下载失败。",
                    retryable=True,
                ) from error
            results.append(
                GeneratedImage(
                    provider=self.provider_name,
                    model=self.config.image_model,
                    content=download.content,
                    mime_type=download.headers.get("content-type", "image/png").split(";", 1)[0],
                    width=width,
                    height=height,
                    provider_request_id=f"{trace_id}:{index}",
                    latency_ms=int((time.monotonic() - started) * 1_000),
                )
            )
        return results
