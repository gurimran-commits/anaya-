"""Unit tests for OllamaProvider with mocked HTTP responses and security checks."""

import io
import json
import socket
import urllib.error
import urllib.response

import pytest

from app.ai.models import (
    ChatMessage,
    GenerationOptions,
    ModelConnectionError,
    ModelNotFoundError,
    ModelProviderError,
    ModelTimeoutError,
    ModelValidationError,
)
from app.ai.providers.ollama import OllamaProvider


class MockHTTPResponse(io.BytesIO):
    """Simulated urllib HTTP response object."""

    def __init__(self, data: bytes, status: int = 200, headers: dict | None = None):
        super().__init__(data)
        self.status = status
        self.code = status
        self.headers = headers or {"Content-Type": "application/json"}


class MockOpener:
    """Mock urllib OpenerDirector to simulate Ollama HTTP endpoints."""

    def __init__(self, handler_func=None):
        self.handler_func = handler_func

    def open(self, req, timeout=None):
        if self.handler_func:
            return self.handler_func(req, timeout)
        return MockHTTPResponse(b"{}", status=200)


class TestOllamaProviderSecurity:
    """Security and endpoint validation checks."""

    def test_default_loopback_endpoint_accepted(self):
        provider = OllamaProvider("http://127.0.0.1:11434")
        assert provider.base_url == "http://127.0.0.1:11434"

        provider2 = OllamaProvider("http://localhost:11434")
        assert provider2.base_url == "http://localhost:11434"

        provider3 = OllamaProvider("http://[::1]:11434")
        assert provider3.base_url == "http://[::1]:11434"

    def test_non_loopback_endpoint_rejected_by_default(self):
        with pytest.raises(ModelValidationError, match="Security violation: Host '192.168.1.5' is not a local loopback"):
            OllamaProvider("http://192.168.1.5:11434")

        with pytest.raises(ModelValidationError, match="Security violation: Host 'api.external.com' is not a local loopback"):
            OllamaProvider("https://api.external.com/api")

    def test_explicit_non_loopback_opt_in_allowed(self):
        provider = OllamaProvider("http://192.168.1.5:11434", allow_non_loopback=True)
        assert provider.base_url == "http://192.168.1.5:11434"

    def test_loopback_ip_variations_accepted(self):
        # 127.0.0.0/8 block
        p1 = OllamaProvider("http://127.0.1.1:11434")
        assert p1.base_url == "http://127.0.1.1:11434"

    def test_redirect_handler_blocks_external_redirects(self):
        from app.ai.providers.ollama import _RestrictedRedirectHandler

        handler = _RestrictedRedirectHandler(allow_non_loopback=False)
        req = urllib.request.Request("http://127.0.0.1:11434/api/tags")

        # Allowed redirect to another loopback endpoint
        req_loopback = handler.redirect_request(req, None, 302, "Found", {}, "http://127.0.0.1:11435/api/tags")
        assert req_loopback is not None

        # Blocked redirect to external host
        with pytest.raises(ModelProviderError, match="HTTP redirect to non-loopback host 'evil.com' was blocked"):
            handler.redirect_request(req, None, 302, "Found", {}, "http://evil.com/api/tags")

    def test_invalid_scheme_rejected(self):
        with pytest.raises(ModelValidationError, match="Invalid URL scheme 'ftp'"):
            OllamaProvider("ftp://127.0.0.1:11434")


class TestOllamaProviderEndpoints:
    """API interaction tests with mock responses."""

    def test_ping_success(self):
        def handler(req, timeout):
            assert req.full_url == "http://127.0.0.1:11434/api/tags"
            return MockHTTPResponse(b'{"models": []}', status=200)

        provider = OllamaProvider(opener=MockOpener(handler))
        assert provider.ping() is True

    def test_ping_failure_on_connection_error(self):
        def handler(req, timeout):
            raise urllib.error.URLError("Connection refused")

        provider = OllamaProvider(opener=MockOpener(handler))
        assert provider.ping() is False

    def test_list_models_parsing(self):
        mock_data = {
            "models": [
                {
                    "name": "qwen2.5:1.5b",
                    "size": 986000000,
                    "modified_at": "2026-11-01T10:00:00Z",
                    "details": {
                        "family": "qwen2",
                        "parameter_size": "1.5B",
                        "quantization_level": "Q4_K_M",
                    },
                },
                {
                    "name": "llama3.2:1b",
                    "size": 1300000000,
                    "details": {
                        "family": "llama",
                        "parameter_size": "1.2B",
                    },
                },
            ]
        }

        def handler(req, timeout):
            assert "/api/tags" in req.full_url
            return MockHTTPResponse(json.dumps(mock_data).encode("utf-8"))

        provider = OllamaProvider(opener=MockOpener(handler))
        models = provider.list_models()
        assert len(models) == 2
        assert models[0].name == "qwen2.5:1.5b"
        assert models[0].size_bytes == 986000000
        assert models[0].details.family == "qwen2"
        assert models[0].details.parameter_size == "1.5B"
        assert models[1].name == "llama3.2:1b"

    def test_get_model_details(self):
        mock_show = {
            "details": {
                "family": "qwen2",
                "parameter_size": "1.5B",
                "quantization_level": "Q4_K_M",
            }
        }

        def handler(req, timeout):
            assert "/api/show" in req.full_url
            data = json.loads(req.data.decode("utf-8"))
            assert data["name"] == "qwen2.5:1.5b"
            return MockHTTPResponse(json.dumps(mock_show).encode("utf-8"))

        provider = OllamaProvider(opener=MockOpener(handler))
        details = provider.get_model_details("qwen2.5:1.5b")
        assert details is not None
        assert details.family == "qwen2"
        assert details.parameter_size == "1.5B"

    def test_chat_generation_success(self):
        mock_chat_res = {
            "model": "qwen2.5:1.5b",
            "message": {
                "role": "assistant",
                "content": "The weather today is clear.",
            },
            "total_duration": 1500000000,
            "eval_count": 12,
        }

        def handler(req, timeout):
            assert "/api/chat" in req.full_url
            data = json.loads(req.data.decode("utf-8"))
            assert data["model"] == "qwen2.5:1.5b"
            assert data["stream"] is False
            assert data.get("think") is False
            assert len(data["messages"]) == 1
            assert data["messages"][0]["content"] == "How is the weather?"
            return MockHTTPResponse(json.dumps(mock_chat_res).encode("utf-8"))

        provider = OllamaProvider(opener=MockOpener(handler))
        messages = [ChatMessage(role="user", content="How is the weather?")]
        result = provider.chat(messages, model="qwen2.5:1.5b")

        assert result.success is True
        assert result.content == "The weather today is clear."
        assert result.model == "qwen2.5:1.5b"
        assert result.eval_count == 12

    def test_chat_thinking_never_exposed_as_content(self):
        """Internal reasoning tokens in message.thinking are never returned as user-facing content."""
        mock_thinking_res = {
            "model": "qwen3.5:4b",
            "message": {
                "role": "assistant",
                "content": "",
                "thinking": "Step 1: Ponder user prompt. Step 2: Formulate internal thoughts.",
            },
            "done_reason": "length",
            "total_duration": 2000000000,
        }

        def handler(req, timeout):
            data = json.loads(req.data.decode("utf-8"))
            assert data["think"] is False
            return MockHTTPResponse(json.dumps(mock_thinking_res).encode("utf-8"))

        provider = OllamaProvider(opener=MockOpener(handler))
        messages = [ChatMessage(role="user", content="Hello Anaya")]
        result = provider.chat(messages, model="qwen3.5:4b")

        assert result.success is False
        assert result.content == ""
        assert "Step 1" not in result.content
        assert "thinking" not in result.content.lower()
        assert result.error_message == "Model returned empty response content."

    def test_chat_empty_or_whitespace_content_returns_controlled_failure(self):
        """Responses with empty or whitespace-only content return a controlled failure."""
        mock_empty_res = {
            "model": "qwen2.5:1.5b",
            "message": {
                "role": "assistant",
                "content": "   \n\t  ",
            },
        }

        def handler(req, timeout):
            return MockHTTPResponse(json.dumps(mock_empty_res).encode("utf-8"))

        provider = OllamaProvider(opener=MockOpener(handler))
        messages = [ChatMessage(role="user", content="Hello")]
        result = provider.chat(messages, model="qwen2.5:1.5b")

        assert result.success is False
        assert result.content == ""
        assert result.error_message == "Model returned empty response content."

    def test_chat_model_not_found_error(self):
        def handler(req, timeout):
            raise urllib.error.HTTPError(
                req.full_url, 404, "Not Found", {}, io.BytesIO(b'{"error": "model not found"}')
            )

        provider = OllamaProvider(opener=MockOpener(handler))
        messages = [ChatMessage(role="user", content="Hello")]
        with pytest.raises(ModelNotFoundError, match="not found"):
            provider.chat(messages, model="non_existent_model")

    def test_chat_timeout_error(self):
        def handler(req, timeout):
            raise urllib.error.URLError(socket.timeout("Operation timed out"))

        provider = OllamaProvider(opener=MockOpener(handler))
        messages = [ChatMessage(role="user", content="Hello")]
        with pytest.raises(ModelTimeoutError, match="timed out"):
            provider.chat(messages, model="qwen2.5:1.5b")

    def test_unload_model_success(self):
        called = False

        def handler(req, timeout):
            nonlocal called
            assert "/api/generate" in req.full_url
            data = json.loads(req.data.decode("utf-8"))
            assert data["model"] == "qwen2.5:1.5b"
            assert data["keep_alive"] == 0
            called = True
            return MockHTTPResponse(b'{"response": ""}')

        provider = OllamaProvider(opener=MockOpener(handler))
        success = provider.unload_model("qwen2.5:1.5b")
        assert success is True
        assert called is True
