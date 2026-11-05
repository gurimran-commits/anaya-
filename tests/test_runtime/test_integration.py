"""Integration tests verifying TextAssistant, CommandExecutor, AI fallback, and Runtime harmony."""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.ai.manager import ModelManager
from app.ai.models import ChatMessage, GenerationOptions, GenerationResult, ModelDetails, ModelInfo
from app.runtime import AnayaRuntime


class MockAIProvider:
    """Mock provider for testing AI integration in runtime."""

    def __init__(self, responsive: bool = True):
        self.responsive = responsive
        self.installed = [
            ModelInfo(name="qwen2.5:1.5b", size_bytes=1000000, details=ModelDetails())
        ]
        self.queries: list[str] = []

    def ping(self) -> bool:
        return self.responsive

    def list_models(self) -> list[ModelInfo]:
        return self.installed

    def get_model_details(self, model_name: str) -> ModelDetails | None:
        return ModelDetails()

    def chat(
        self,
        messages: list[ChatMessage],
        model: str,
        options: GenerationOptions | None = None,
    ) -> GenerationResult:
        if not self.responsive:
            raise ConnectionError("Ollama offline")
        last = messages[-1].content
        self.queries.append(last)
        return GenerationResult(
            success=True,
            content=f"AI answer to: {last}",
            model=model,
        )

    def unload_model(self, model_name: str) -> bool:
        return True


@pytest.fixture
def temp_db(tmp_path):
    return tmp_path / "test_integration.db"


def test_deterministic_commands_have_precedence_over_ai(temp_db):
    """Task operations and standard commands execute deterministically without invoking AI."""
    provider = MockAIProvider(responsive=True)
    model_mgr = ModelManager(provider=provider)
    model_mgr.select_model("qwen2.5:1.5b")

    runtime = AnayaRuntime(db_path=temp_db, model_manager=model_mgr)
    try:
        # Help command
        res_help = runtime.process("help")
        assert res_help.success is True
        assert len(provider.queries) == 0

        # Task command
        res_task = runtime.process("Remind me to call Mom at 6 PM")
        assert res_task.success is True
        assert "Created task: 'call Mom'" in res_task.message
        assert len(provider.queries) == 0

        # List command
        res_list = runtime.process("Show pending tasks")
        assert res_list.success is True
        assert len(provider.queries) == 0
    finally:
        runtime.stop()


def test_conversational_questions_route_to_ai(temp_db):
    """General conversational queries are routed to AI if enabled and available."""
    provider = MockAIProvider(responsive=True)
    model_mgr = ModelManager(provider=provider)
    model_mgr.select_model("qwen2.5:1.5b")

    runtime = AnayaRuntime(db_path=temp_db, model_manager=model_mgr)
    try:
        res = runtime.process("What is the capital of Japan?")
        assert res.success is True
        assert "AI answer to: What is the capital of Japan?" in res.message
        assert len(provider.queries) == 1
    finally:
        runtime.stop()


def test_runtime_without_ai_manager(temp_db):
    """When model_manager is None, task commands work and unrecognized input returns deterministic error."""
    runtime = AnayaRuntime(db_path=temp_db, model_manager=None)
    try:
        res_task = runtime.process("Remind me to call Dad at 5 PM")
        assert res_task.success is True
        assert "Created task: 'call Dad'" in res_task.message

        res_unknown = runtime.process("What is the speed of sound?")
        assert res_unknown.success is False
        assert "Command not recognized" in res_unknown.message
    finally:
        runtime.stop()


def test_runtime_without_notification_callback(temp_db):
    """Default LoggingNotificationHandler is used when on_reminder is omitted."""
    now = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
    runtime = AnayaRuntime(db_path=temp_db, clock=lambda: now)
    try:
        t = runtime.task_manager.create_task("Test task", reminder_at=now)
        # run_pending should succeed and dispatch to LoggingNotificationHandler without error
        dispatched = runtime.run_scheduler_pending(now=now)
        assert dispatched == 1
    finally:
        runtime.stop()
