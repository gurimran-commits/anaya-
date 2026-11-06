"""Local Ollama HTTP model provider using Python standard library urllib."""

from __future__ import annotations

import ipaddress
import json
import logging
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from app.ai.models import (
    ChatMessage,
    GenerationOptions,
    GenerationResult,
    ModelConnectionError,
    ModelDetails,
    ModelInfo,
    ModelNotFoundError,
    ModelProviderError,
    ModelTimeoutError,
    ModelValidationError,
)

logger = logging.getLogger(__name__)

# Response payload limits (10 MB)
MAX_RESPONSE_BYTES = 10 * 1024 * 1024


def _is_loopback_host(hostname: str) -> bool:
    """Return True if hostname is a recognized local loopback address or alias."""
    clean = hostname.strip().lower()
    if clean == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(clean.strip("[]"))
        return ip.is_loopback
    except ValueError:
        return False


class _RestrictedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Prevent HTTP redirects from escaping the local loopback boundary."""

    def __init__(self, allow_non_loopback: bool) -> None:
        super().__init__()
        self.allow_non_loopback = allow_non_loopback

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlparse(newurl)
        hostname = (parsed.hostname or "").lower()
        if not self.allow_non_loopback and not _is_loopback_host(hostname):
            raise ModelProviderError(
                f"Security violation: HTTP redirect to non-loopback host '{hostname}' was blocked."
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class OllamaProvider:
    """Provider communicating with a locally running Ollama daemon."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        timeout: float = 30.0,
        ping_timeout: float = 2.0,
        allow_non_loopback: bool = False,
        opener: urllib.request.OpenerDirector | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.ping_timeout = ping_timeout
        self.allow_non_loopback = allow_non_loopback

        self._validate_endpoint(self.base_url, allow_non_loopback)

        if opener is not None:
            self._opener = opener
        else:
            redirect_handler = _RestrictedRedirectHandler(allow_non_loopback)
            self._opener = urllib.request.build_opener(redirect_handler)

    @staticmethod
    def _validate_endpoint(url: str, allow_non_loopback: bool) -> None:
        """Validate URL syntax, scheme, and loopback safety."""
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise ModelValidationError(
                f"Invalid URL scheme '{parsed.scheme}'. Only http and https are permitted."
            )

        hostname = (parsed.hostname or "").lower()
        if not hostname:
            raise ModelValidationError(f"Invalid URL '{url}': hostname is missing.")

        if not allow_non_loopback and not _is_loopback_host(hostname):
            raise ModelValidationError(
                f"Security violation: Host '{hostname}' is not a local loopback address. "
                "Ollama connections are restricted to 127.0.0.1 / localhost by default."
            )

    def _http_request(
        self,
        path: str,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Perform an HTTP request to the Ollama daemon and parse the JSON response."""
        url = f"{self.base_url}{path}"
        data: bytes | None = None
        headers = {"Accept": "application/json"}

        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        effective_timeout = timeout or self.timeout

        try:
            with self._opener.open(req, timeout=effective_timeout) as resp:
                status = resp.status
                content_type = resp.headers.get("Content-Type", "")
                raw_bytes = resp.read(MAX_RESPONSE_BYTES + 1)
                if len(raw_bytes) > MAX_RESPONSE_BYTES:
                    raise ModelProviderError(
                        f"Response from '{path}' exceeded maximum size limit of {MAX_RESPONSE_BYTES} bytes."
                    )

                text = raw_bytes.decode("utf-8")
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise ModelProviderError(
                        f"Failed to decode JSON response from '{path}': {exc}"
                    ) from exc

                if not isinstance(parsed, dict):
                    raise ModelProviderError(
                        f"Expected JSON object from '{path}', got {type(parsed).__name__}."
                    )
                return parsed

        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read(4096).decode("utf-8", errors="replace")
            except Exception:
                pass

            if exc.code == 404:
                raise ModelNotFoundError(
                    f"Requested resource or model at '{path}' was not found: {body}"
                ) from exc
            raise ModelProviderError(
                f"Ollama HTTP error {exc.code} for '{path}': {body}"
            ) from exc

        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (socket.timeout, TimeoutError)) or "timed out" in str(exc.reason).lower():
                raise ModelTimeoutError(
                    f"Request to Ollama at '{path}' timed out after {effective_timeout}s."
                ) from exc
            raise ModelConnectionError(
                f"Failed to connect to Ollama daemon at '{self.base_url}': {exc.reason}"
            ) from exc

        except (socket.timeout, TimeoutError) as exc:
            raise ModelTimeoutError(
                f"Request to Ollama at '{path}' timed out after {effective_timeout}s."
            ) from exc

        except Exception as exc:
            if isinstance(exc, (ModelProviderError, ModelNotFoundError, ModelTimeoutError, ModelConnectionError)):
                raise
            raise ModelProviderError(f"Unexpected error communicating with Ollama: {exc}") from exc

    def ping(self) -> bool:
        """Check if Ollama is running and responsive."""
        try:
            self._http_request("/api/tags", method="GET", timeout=self.ping_timeout)
            return True
        except Exception:
            return False

    def list_models(self) -> list[ModelInfo]:
        """List all locally installed models in Ollama."""
        res = self._http_request("/api/tags", method="GET")
        models_data = res.get("models")
        if not isinstance(models_data, list):
            return []

        result: list[ModelInfo] = []
        for item in models_data:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if not name or not isinstance(name, str):
                continue

            size = item.get("size", 0)
            if not isinstance(size, int):
                size = 0

            mod_at = item.get("modified_at")
            if mod_at is not None and not isinstance(mod_at, str):
                mod_at = None

            details_dict = item.get("details", {})
            details = ModelDetails()
            if isinstance(details_dict, dict):
                details = ModelDetails(
                    family=str(details_dict.get("family", "")),
                    parameter_size=str(details_dict.get("parameter_size", "")),
                    quantization_level=str(details_dict.get("quantization_level", "")),
                    context_length=details_dict.get("context_length"),
                )

            result.append(
                ModelInfo(
                    name=name,
                    size_bytes=size,
                    modified_at=mod_at,
                    details=details,
                )
            )
        return result

    def get_model_details(self, model_name: str) -> ModelDetails | None:
        """Fetch model parameters and architecture details from Ollama."""
        if not model_name or not isinstance(model_name, str):
            return None
        try:
            res = self._http_request("/api/show", method="POST", payload={"name": model_name})
            details_dict = res.get("details", {})
            if isinstance(details_dict, dict):
                return ModelDetails(
                    family=str(details_dict.get("family", "")),
                    parameter_size=str(details_dict.get("parameter_size", "")),
                    quantization_level=str(details_dict.get("quantization_level", "")),
                    context_length=details_dict.get("context_length"),
                )
            return ModelDetails()
        except ModelNotFoundError:
            return None
        except Exception as exc:
            logger.debug("Failed to retrieve model details for '%s': %s", model_name, exc)
            return None

    def chat(
        self,
        messages: list[ChatMessage],
        model: str,
        options: GenerationOptions | None = None,
    ) -> GenerationResult:
        """Execute a conversational chat completion via /api/chat."""
        if not model or not isinstance(model, str):
            raise ModelValidationError("Model name must be a non-empty string.")
        if not messages:
            raise ModelValidationError("At least one ChatMessage must be provided.")

        opts = options or GenerationOptions()
        payload = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": False,
            "think": False,
            "options": {
                "temperature": opts.temperature,
                "num_predict": opts.num_predict,
                "num_ctx": opts.num_ctx,
                "top_k": opts.top_k,
                "top_p": opts.top_p,
            },
            "keep_alive": opts.keep_alive,
        }

        res = self._http_request("/api/chat", method="POST", payload=payload)
        msg_obj = res.get("message")
        if not isinstance(msg_obj, dict):
            raise ModelProviderError("Malformed response from Ollama /api/chat: 'message' object missing.")

        content = msg_obj.get("content")
        if not isinstance(content, str):
            raise ModelProviderError("Malformed response from Ollama /api/chat: 'content' must be a string.")

        if not content.strip():
            logger.warning("Ollama /api/chat returned empty content for model '%s'.", model)
            return GenerationResult(
                success=False,
                content="",
                model=str(res.get("model", model)),
                total_duration_ns=res.get("total_duration"),
                eval_count=res.get("eval_count"),
                error_message="Model returned empty response content.",
            )

        return GenerationResult(
            success=True,
            content=content,
            model=str(res.get("model", model)),
            total_duration_ns=res.get("total_duration"),
            eval_count=res.get("eval_count"),
        )

    def unload_model(self, model_name: str) -> bool:
        """Evict model from memory using Ollama's keep_alive: 0 mechanism."""
        if not model_name or not isinstance(model_name, str):
            return False
        try:
            self._http_request(
                "/api/generate",
                method="POST",
                payload={"model": model_name, "keep_alive": 0},
                timeout=5.0,
            )
            return True
        except Exception as exc:
            logger.debug("Failed to unload model '%s': %s", model_name, exc)
            return False
