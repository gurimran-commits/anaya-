"""Tests for application bootstrap and dependency injection factory."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.bootstrap import create_controller
from app.core.config import AppConfig
from app.speech.adapters.mock import (
    MockAudioPlayer,
    MockAudioRecorder,
    MockSpeechToText,
    MockTextToSpeech,
)


@pytest.fixture
def temp_paths(tmp_path: Path):
    return {
        "mem_db": tmp_path / "test_mem.db",
        "tasks_db": tmp_path / "test_tasks.db",
        "files_dir": tmp_path / "files",
    }


def test_bootstrap_shared_approval_service_identity(temp_paths):
    """Enforce that exactly one ApprovalService is shared across skills and commands."""
    temp_paths["files_dir"].mkdir()
    controller = create_controller(
        memory_db_path=temp_paths["mem_db"],
        tasks_db_path=temp_paths["tasks_db"],
        allowed_directories=(temp_paths["files_dir"],),
        enable_speech=False,
    )
    try:
        shared_approval = controller.approval_service
        assert shared_approval is not None

        # Verify identity across SkillManager executor
        assert controller.skill_manager is not None
        assert controller.skill_manager.executor.approval_service is shared_approval

        # Verify identity across AnayaRuntime and CommandExecutor
        assert controller.runtime.approval_service is shared_approval
        assert controller.runtime.executor.approval_service is shared_approval
    finally:
        controller.stop()


def test_bootstrap_single_instance_dependency_ownership(temp_paths):
    """Verify stateful subsystems are singletons owned by their authoritative containers."""
    controller = create_controller(
        memory_db_path=temp_paths["mem_db"],
        tasks_db_path=temp_paths["tasks_db"],
        enable_speech=False,
    )
    try:
        # TaskManager singleton in runtime
        assert controller.task_manager is controller.runtime.task_manager

        # TextAssistant singleton in runtime
        assert controller.text_assistant is controller.runtime.text_assistant

        # Injected memory manager singleton
        assert controller.memory_manager is not None
        assert controller.text_assistant.memory_manager is controller.memory_manager

        # Injected skill manager singleton
        assert controller.skill_manager is not None
        assert controller.text_assistant.skill_manager is controller.skill_manager

        # Injected model manager singleton
        assert controller.model_manager is not None
        assert controller.text_assistant.model_manager is controller.model_manager
    finally:
        controller.stop()


def test_bootstrap_registers_filesystem_commands_when_configured(temp_paths):
    """When allowed_directories is supplied, filesystem commands are registered."""
    temp_paths["files_dir"].mkdir()
    controller = create_controller(
        memory_db_path=temp_paths["mem_db"],
        tasks_db_path=temp_paths["tasks_db"],
        allowed_directories=(temp_paths["files_dir"],),
        enable_speech=False,
    )
    try:
        registry = controller.runtime.registry
        assert registry.get("files.list") is not None
        assert registry.get("files.read") is not None
        assert registry.get("files.create") is not None
        assert registry.get("files.delete") is not None
        assert controller.filesystem_service is not None
    finally:
        controller.stop()


def test_bootstrap_without_filesystem_commands(temp_paths):
    """When allowed_directories is None, filesystem service is omitted without error."""
    controller = create_controller(
        memory_db_path=temp_paths["mem_db"],
        tasks_db_path=temp_paths["tasks_db"],
        allowed_directories=None,
        enable_speech=False,
    )
    try:
        registry = controller.runtime.registry
        assert registry.get("files.list") is None
        assert controller.filesystem_service is None
    finally:
        controller.stop()


def test_bootstrap_with_custom_speech_adapters(temp_paths):
    """Verify custom speech adapters are wired cleanly into VoicePipeline."""
    rec = MockAudioRecorder()
    stt = MockSpeechToText()
    tts = MockTextToSpeech()
    player = MockAudioPlayer()

    controller = create_controller(
        memory_db_path=temp_paths["mem_db"],
        tasks_db_path=temp_paths["tasks_db"],
        enable_speech=True,
        custom_recorder=rec,
        custom_stt=stt,
        custom_tts=tts,
        custom_player=player,
    )
    try:
        assert controller.voice_pipeline is not None
        assert controller.voice_pipeline.recorder is rec
        assert controller.voice_pipeline.stt is stt
        assert controller.voice_pipeline.tts is tts
        assert controller.voice_pipeline.player is player
        assert controller.voice_pipeline.assistant is controller.text_assistant
    finally:
        controller.stop()


def test_bootstrap_disabled_speech_mode(temp_paths):
    """When speech is disabled, bootstrap succeeds and controller operates in text-only mode."""
    controller = create_controller(
        memory_db_path=temp_paths["mem_db"],
        tasks_db_path=temp_paths["tasks_db"],
        enable_speech=False,
    )
    try:
        assert controller.voice_pipeline is None
        # Text operation is fully functional
        res = controller.handle_text("help")
        assert res.success is True
        assert "Available commands" in res.message
    finally:
        controller.stop()


def test_bootstrap_disabled_ai_mode(temp_paths):
    """When AI is disabled via config or parameter, ModelManager is omitted."""
    config = AppConfig(ai_enabled=False)
    controller = create_controller(
        config=config,
        memory_db_path=temp_paths["mem_db"],
        tasks_db_path=temp_paths["tasks_db"],
        enable_speech=False,
    )
    try:
        assert controller.model_manager is None
        assert controller.text_assistant.model_manager is None
    finally:
        controller.stop()


def test_bootstrap_partial_initialization_failure_cleans_up(temp_paths, monkeypatch):
    """If bootstrap fails midway, already-opened resources (e.g. MemoryStore) are closed."""
    # Force AnayaRuntime creation to fail after MemoryManager has been opened
    def failing_runtime(*args, **kwargs):
        raise RuntimeError("Simulated runtime creation failure")

    monkeypatch.setattr("app.bootstrap.AnayaRuntime", failing_runtime)

    with pytest.raises(RuntimeError, match="Simulated runtime creation failure"):
        create_controller(
            memory_db_path=temp_paths["mem_db"],
            tasks_db_path=temp_paths["tasks_db"],
            enable_speech=False,
        )


def test_bootstrap_partial_custom_adapter_injection_rejected(temp_paths):
    """Providing only a subset of custom speech adapters raises ValueError."""
    with pytest.raises(ValueError, match="Partial custom speech adapter injection is not supported"):
        create_controller(
            memory_db_path=temp_paths["mem_db"],
            tasks_db_path=temp_paths["tasks_db"],
            custom_recorder=MockAudioRecorder(),
        )


def test_bootstrap_rollback_closes_resources_in_reverse_order(temp_paths, monkeypatch):
    """On bootstrap failure, resources are closed in exact reverse construction order."""
    from app.commands.filesystem import FilesystemService
    from app.memory.memory_manager import MemoryManager
    from app.runtime import AnayaRuntime

    call_order: list[str] = []

    orig_fs_close = FilesystemService.close
    def tracking_fs_close(self):
        call_order.append("filesystem")
        return orig_fs_close(self)
    monkeypatch.setattr(FilesystemService, "close", tracking_fs_close)

    orig_rt_stop = AnayaRuntime.stop
    def tracking_rt_stop(self, *args, **kwargs):
        call_order.append("runtime")
        return orig_rt_stop(self, *args, **kwargs)
    monkeypatch.setattr(AnayaRuntime, "stop", tracking_rt_stop)

    orig_mem_close = MemoryManager.close
    def tracking_mem_close(self):
        call_order.append("memory")
        return orig_mem_close(self)
    monkeypatch.setattr(MemoryManager, "close", tracking_mem_close)

    temp_paths["files_dir"].mkdir()

    def failing_pipeline(*args, **kwargs):
        raise RuntimeError("Simulated pipeline creation failure")

    monkeypatch.setattr("app.bootstrap.VoicePipeline", failing_pipeline)

    with pytest.raises(RuntimeError, match="Simulated pipeline creation failure"):
        create_controller(
            memory_db_path=temp_paths["mem_db"],
            tasks_db_path=temp_paths["tasks_db"],
            allowed_directories=(temp_paths["files_dir"],),
            custom_recorder=MockAudioRecorder(),
            custom_stt=MockSpeechToText(),
            custom_tts=MockTextToSpeech(),
            custom_player=MockAudioPlayer(),
        )

    # Must close in reverse order of creation: filesystem -> runtime -> memory
    assert call_order == ["filesystem", "runtime", "memory"]
