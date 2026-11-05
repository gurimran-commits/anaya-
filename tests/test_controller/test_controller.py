"""Tests for AssistantController public interface, lifecycle, voice, and security."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.ai.models import ChatMessage, GenerationOptions, GenerationResult, ModelDetails, ModelInfo
from app.bootstrap import create_controller
from app.core.action_result import ActionResult
from app.core.config import AppConfig
from app.speech.adapters.mock import (
    MockAudioPlayer,
    MockAudioRecorder,
    MockSpeechToText,
    MockTextToSpeech,
)
from app.speech.models import AudioData, AudioFormat


@pytest.fixture
def temp_paths(tmp_path: Path):
    return {
        "mem_db": tmp_path / "test_ctrl_mem.db",
        "tasks_db": tmp_path / "test_ctrl_tasks.db",
        "files_dir": tmp_path / "test_ctrl_files",
    }


@pytest.fixture
def mock_speech_adapters():
    return {
        "recorder": MockAudioRecorder(),
        "stt": MockSpeechToText(),
        "tts": MockTextToSpeech(),
        "player": MockAudioPlayer(),
    }


def test_controller_typed_commands_execution(temp_paths):
    """Verify typed command routing through AssistantController.handle_text()."""
    controller = create_controller(
        memory_db_path=temp_paths["mem_db"],
        tasks_db_path=temp_paths["tasks_db"],
        enable_speech=False,
    )
    try:
        controller.start()

        # 1. Create a task
        res_task = controller.handle_text("Add a task to submit report tomorrow")
        assert res_task.success is True
        assert "Created task: 'submit report'" in res_task.message

        # 2. Save a memory
        res_mem = controller.handle_text("Remember that my coffee preference is dark roast")
        assert res_mem.success is True
        assert "Saved memory:" in res_mem.message
        assert "dark roast" in res_mem.message

        # 3. List skills
        from app.skills.builtin.example_skill import GreetingSkill
        controller.skill_manager.register_skill(GreetingSkill())
        res_skills = controller.handle_text("What skills do you have?")
        assert res_skills.success is True
        assert "example.greeting" in res_skills.message
    finally:
        controller.stop()


def test_controller_ai_fallback_and_disabled_state(temp_paths):
    """Conversational text routes to AI when enabled, and returns unsupported when disabled."""
    # 1. Controller with mock AI provider
    class MockAIProvider:
        def __init__(self):
            self.installed = [
                ModelInfo(name="qwen2.5:1.5b", size_bytes=1000000, details=ModelDetails())
            ]
        def ping(self) -> bool:
            return True
        def list_models(self) -> list[ModelInfo]:
            return self.installed
        def get_model_details(self, model_name: str) -> ModelDetails | None:
            return ModelDetails()
        def chat(self, messages: list[ChatMessage], model: str, options: GenerationOptions | None = None) -> GenerationResult:
            return GenerationResult(success=True, content="I am a local AI assistant.", model=model)
        def unload_model(self, model_name: str) -> bool:
            return True

    controller = create_controller(
        memory_db_path=temp_paths["mem_db"],
        tasks_db_path=temp_paths["tasks_db"],
        enable_speech=False,
        enable_ai=True,
        custom_ai_provider=MockAIProvider(),
    )
    try:
        controller.start()
        # Non-command input routes to AI
        res_ai = controller.handle_text("What is the capital of France?")
        assert res_ai.success is True
        assert "local AI assistant" in res_ai.message
    finally:
        controller.stop()

    # 2. Controller with AI disabled
    config_no_ai = AppConfig(ai_enabled=False)
    controller_no_ai = create_controller(
        config=config_no_ai,
        memory_db_path=temp_paths["mem_db"],
        tasks_db_path=temp_paths["tasks_db"],
        enable_speech=False,
    )
    try:
        controller_no_ai.start()
        res_no_ai = controller_no_ai.handle_text("What is the capital of France?")
        assert res_no_ai.success is False
        assert "Command not recognized" in res_no_ai.message
    finally:
        controller_no_ai.stop()


def test_controller_voice_delegation_with_mock_adapters(temp_paths, mock_speech_adapters):
    """Verify handle_voice delegates to VoicePipeline push-to-talk recording and transcription."""
    mock_speech_adapters["stt"].next_transcript = "What tasks do I have?"

    controller = create_controller(
        memory_db_path=temp_paths["mem_db"],
        tasks_db_path=temp_paths["tasks_db"],
        enable_speech=True,
        custom_recorder=mock_speech_adapters["recorder"],
        custom_stt=mock_speech_adapters["stt"],
        custom_tts=mock_speech_adapters["tts"],
        custom_player=mock_speech_adapters["player"],
    )
    try:
        controller.start()
        result = controller.handle_voice(duration=2.0, speak_response=True)
        assert result.success is True
        assert result.transcript == "What tasks do I have?"
        assert result.action_result is not None
        assert result.spoken_response is True
        # Verify TTS and Player were called
        assert len(mock_speech_adapters["tts"].synthesize_calls) > 0
        assert len(mock_speech_adapters["player"].play_calls) > 0
    finally:
        controller.stop()


def test_controller_handle_audio_payload(temp_paths, mock_speech_adapters):
    """Verify handle_audio processes pre-recorded AudioData payloads."""
    mock_speech_adapters["stt"].next_transcript = "help"
    fmt = AudioFormat(sample_rate=24000, channels=1, sample_width=2, encoding="pcm_s16le")
    audio = AudioData(data=b"\x00" * 4800, format=fmt)

    controller = create_controller(
        memory_db_path=temp_paths["mem_db"],
        tasks_db_path=temp_paths["tasks_db"],
        enable_speech=True,
        custom_recorder=mock_speech_adapters["recorder"],
        custom_stt=mock_speech_adapters["stt"],
        custom_tts=mock_speech_adapters["tts"],
        custom_player=mock_speech_adapters["player"],
    )
    try:
        controller.start()
        result = controller.handle_audio(audio_data=audio, speak_response=False)
        assert result.success is True
        assert result.transcript == "help"
        assert result.action_result is not None
        assert "Available commands" in result.action_result.message
    finally:
        controller.stop()


def test_controller_speak_delegation(temp_paths, mock_speech_adapters):
    """Verify speak() delegates directly to VoicePipeline TTS and playback."""
    controller = create_controller(
        memory_db_path=temp_paths["mem_db"],
        tasks_db_path=temp_paths["tasks_db"],
        enable_speech=True,
        custom_recorder=mock_speech_adapters["recorder"],
        custom_stt=mock_speech_adapters["stt"],
        custom_tts=mock_speech_adapters["tts"],
        custom_player=mock_speech_adapters["player"],
    )
    try:
        controller.start()
        success = controller.speak("System initialized successfully.")
        assert success is True
        assert "System initialized successfully." in mock_speech_adapters["tts"].synthesize_calls
        assert len(mock_speech_adapters["player"].play_calls) == 1
    finally:
        controller.stop()


def test_controller_missing_optional_speech_subsystem(temp_paths):
    """When speech is disabled or unavailable, voice and speak fail cleanly without crashing."""
    controller = create_controller(
        memory_db_path=temp_paths["mem_db"],
        tasks_db_path=temp_paths["tasks_db"],
        enable_speech=False,
    )
    try:
        controller.start()
        res_voice = controller.handle_voice()
        assert res_voice.success is False
        assert "Speech subsystem is unavailable" in res_voice.error

        fmt = AudioFormat(sample_rate=24000, channels=1, sample_width=2, encoding="pcm_s16le")
        dummy_audio = AudioData(data=b"\x00" * 100, format=fmt)
        res_audio = controller.handle_audio(dummy_audio)
        assert res_audio.success is False
        assert "Speech subsystem is unavailable" in res_audio.error

        assert controller.speak("Hello") is False
    finally:
        controller.stop()


def test_controller_lifecycle_and_context_manager(temp_paths):
    """Verify start(), stop(), single-use terminal state, and context manager protocol."""
    controller = create_controller(
        memory_db_path=temp_paths["mem_db"],
        tasks_db_path=temp_paths["tasks_db"],
        enable_speech=False,
    )

    # Initial state: not running
    assert controller.is_running is False

    # Start
    controller.start()
    assert controller.is_running is True

    # Repeated start while running is safe/idempotent
    controller.start()
    assert controller.is_running is True

    # Stop
    controller.stop()
    assert controller.is_running is False

    # Repeated stop is safe and idempotent
    controller.stop()
    assert controller.is_running is False

    # Start after stop is strictly rejected (single-use lifecycle)
    with pytest.raises(RuntimeError, match="cannot be restarted"):
        controller.start()

    # Context manager protocol with a fresh controller
    controller2 = create_controller(
        memory_db_path=temp_paths["mem_db"],
        tasks_db_path=temp_paths["tasks_db"],
        enable_speech=False,
    )
    with controller2 as c:
        assert c.is_running is True
    assert controller2.is_running is False

    # Once exited, controller2 also cannot be restarted
    with pytest.raises(RuntimeError, match="cannot be restarted"):
        controller2.start()


def test_controller_stop_failure_propagated_after_auxiliary_cleanup(temp_paths, monkeypatch):
    """If runtime.stop() raises, auxiliary resources are still cleaned up, and error is raised."""
    temp_paths["files_dir"].mkdir()
    controller = create_controller(
        memory_db_path=temp_paths["mem_db"],
        tasks_db_path=temp_paths["tasks_db"],
        allowed_directories=(temp_paths["files_dir"],),
        enable_speech=False,
    )
    controller.start()

    orig_stop = controller.runtime.stop

    # Force runtime.stop() to raise an exception once
    failing = True
    def failing_stop(*args, **kwargs):
        nonlocal failing
        orig_stop(*args, **kwargs)
        if failing:
            failing = False
            raise RuntimeError("Simulated runtime.stop() failure")

    monkeypatch.setattr(controller.runtime, "stop", failing_stop)

    # controller.stop() must propagate the runtime error
    with pytest.raises(RuntimeError, match="Simulated runtime.stop\\(\\) failure"):
        controller.stop()

    # Auxiliary resources must still have been closed
    assert getattr(controller.memory_manager.store, "_closed", False) is True
    assert getattr(controller.filesystem_service, "_closed", False) is True

    # Controller is in terminal stopped state
    with pytest.raises(RuntimeError, match="cannot be restarted"):
        controller.start()

    # Repeated stop succeeds once runtime does not raise
    controller.stop()
    assert controller._cleanly_stopped is True


def test_controller_stop_repeated_failures_not_suppressed(temp_paths, monkeypatch):
    """When runtime.stop() fails repeatedly, controller.stop() propagates failure on every call."""
    controller = create_controller(
        memory_db_path=temp_paths["mem_db"],
        tasks_db_path=temp_paths["tasks_db"],
        enable_speech=False,
    )
    controller.start()

    call_count = 0
    orig_stop = controller.runtime.stop

    def failing_stop(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        orig_stop(*args, **kwargs)
        raise RuntimeError(f"Runtime stop error #{call_count}")

    monkeypatch.setattr(controller.runtime, "stop", failing_stop)

    # First call must raise error #1
    with pytest.raises(RuntimeError, match="Runtime stop error #1"):
        controller.stop()
    assert call_count == 1
    assert controller._stopped is True
    assert controller._cleanly_stopped is False

    # Second call must NOT suppress error #2 or report clean stop
    with pytest.raises(RuntimeError, match="Runtime stop error #2"):
        controller.stop()
    assert call_count == 2
    assert controller._cleanly_stopped is False

    # Subsequent start attempts remain rejected
    with pytest.raises(RuntimeError, match="cannot be restarted"):
        controller.start()

    # If runtime stop subsequently succeeds, cleanly_stopped is marked True
    monkeypatch.setattr(controller.runtime, "stop", lambda *args, **kwargs: None)
    controller.stop()
    assert controller._cleanly_stopped is True

    # After clean stop, subsequent calls are safe no-ops
    controller.stop()


def test_controller_stop_auxiliary_cleanup_failure_not_marked_clean(temp_paths, monkeypatch):
    """When auxiliary resource cleanup fails, _cleanly_stopped is not marked True and error is raised."""
    temp_paths["files_dir"].mkdir()
    controller = create_controller(
        memory_db_path=temp_paths["mem_db"],
        tasks_db_path=temp_paths["tasks_db"],
        allowed_directories=(temp_paths["files_dir"],),
        enable_speech=False,
    )
    controller.start()

    # Make MemoryManager.close() raise
    def failing_mem_close():
        raise RuntimeError("Simulated MemoryStore close failure")

    monkeypatch.setattr(controller.memory_manager, "close", failing_mem_close)

    with pytest.raises(RuntimeError, match="Simulated MemoryStore close failure"):
        controller.stop()

    assert controller._stopped is True
    assert controller._cleanly_stopped is False

    # Repeated calls propagate the cleanup failure
    with pytest.raises(RuntimeError, match="Simulated MemoryStore close failure"):
        controller.stop()
    assert controller._cleanly_stopped is False

    # If runtime also raises, the runtime exception is prioritized and auxiliary error is reported
    monkeypatch.setattr(controller.runtime, "stop", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("Runtime stop error")))
    with pytest.raises(RuntimeError, match="Runtime stop error"):
        controller.stop()
    assert controller._cleanly_stopped is False


def test_controller_stop_model_unload_failure_not_marked_clean(temp_paths, monkeypatch):
    """When ModelManager.unload_active_model() fails, _cleanly_stopped is False and error is raised."""
    class MockAIProvider:
        def ping(self) -> bool:
            return True
        def list_models(self):
            return [ModelInfo(name="test:model", size_bytes=1000, details=ModelDetails())]
        def get_model_details(self, model_name: str):
            return ModelDetails()
        def chat(self, messages, model, options=None):
            return GenerationResult(success=True, content="ok", model=model)
        def unload_model(self, model_name: str) -> bool:
            return True

    controller = create_controller(
        memory_db_path=temp_paths["mem_db"],
        tasks_db_path=temp_paths["tasks_db"],
        enable_speech=False,
        enable_ai=True,
        custom_ai_provider=MockAIProvider(),
    )
    controller.start()

    assert controller.model_manager is not None

    def failing_unload():
        raise RuntimeError("Simulated model unload failure")

    monkeypatch.setattr(controller.model_manager, "unload_active_model", failing_unload)

    with pytest.raises(RuntimeError, match="Simulated model unload failure"):
        controller.stop()

    assert controller._stopped is True
    assert controller._cleanly_stopped is False

    # Repeated stop propagates the failure
    with pytest.raises(RuntimeError, match="Simulated model unload failure"):
        controller.stop()
    assert controller._cleanly_stopped is False

    # Once unload succeeds, clean shutdown completes
    monkeypatch.setattr(controller.model_manager, "unload_active_model", lambda: True)
    controller.stop()
    assert controller._cleanly_stopped is True


def test_controller_security_and_approval_barrier(temp_paths):
    """Destructive operations enforce ApprovalService; spoken approvals are strictly blocked."""
    controller = create_controller(
        memory_db_path=temp_paths["mem_db"],
        tasks_db_path=temp_paths["tasks_db"],
        enable_speech=False,
    )
    try:
        controller.start()
        # Create task
        t = controller.task_manager.create_task("Protected Task")

        # 1. Request deletion through text: requires confirmation
        res_del = controller.handle_text(f"Delete task {t.id}")
        assert res_del.success is False
        assert "Confirmation required. Confirmation ID:" in res_del.message

        # Task still exists
        assert controller.task_manager.get_task(t.id) is not None

        # 2. Attempt spoken / text approval: must be REJECTED by Brain security barrier
        res_spoken_yes = controller.handle_text("yes")
        assert res_spoken_yes.success is False
        assert "Text confirmation is disabled" in res_spoken_yes.message

        res_spoken_confirm = controller.handle_text("confirm")
        assert res_spoken_confirm.success is False
        assert "Text confirmation is disabled" in res_spoken_confirm.message

        # 3. Authorized execution via out-of-band ApprovalService
        import re
        m = re.search(r"Confirmation ID:\s*(\S+)", res_del.message)
        assert m is not None
        conf_id = m.group(1)

        req = controller.approval_service.get_request(conf_id)
        assert req is not None
        approved = controller.approval_service.approve(req)
        assert approved is True

        # 4. Authorized execution succeeds
        res_exec = controller.runtime.executor.execute(
            "tasks.delete",
            (t.id,),
            confirmation_id=conf_id,
        )
        assert res_exec.success is True
        assert f"Deleted task {t.id}" in res_exec.message
    finally:
        controller.stop()


def test_controller_scheduler_timeout_safety_preserved(temp_paths):
    """Preserve Phase 7B guarantee: timeout leaves TaskStore open and rejects premature restart."""
    entered_cb = threading.Event()
    blocker = threading.Event()

    def slow_cb(notif):
        entered_cb.set()
        blocker.wait(timeout=2.0)

    config = AppConfig(
        task_recover_missed_reminders=False,
        task_scheduler_poll_interval=0.05,
    )
    controller = create_controller(
        config=config,
        memory_db_path=temp_paths["mem_db"],
        tasks_db_path=temp_paths["tasks_db"],
        enable_speech=False,
    )
    # Wire the slow reminder callback
    controller.runtime._notification_handler.notify = slow_cb

    try:
        controller.task_manager.create_task("Slow reminder", reminder_at=datetime.now(timezone.utc))
        controller.start()

        # Wait until worker enters callback
        assert entered_cb.wait(timeout=3.0) is True

        # Stop with tiny timeout: worker remains active
        controller.stop(timeout=0.01)
        assert controller.runtime.scheduler.is_running is True

        # Controller is stopped and cannot be restarted
        with pytest.raises(RuntimeError, match="cannot be restarted"):
            controller.start()

        # Direct runtime restart while worker is active MUST raise the Phase 7B RuntimeError
        with pytest.raises(RuntimeError, match="previous scheduler worker thread is still active"):
            controller.runtime.start()

        # TaskStore remains unclosed and safe
        assert getattr(controller.task_manager.store, "_closed", False) is False

        # Unblock worker and wait for exit
        blocker.set()
        for _ in range(50):
            if not controller.runtime.scheduler.is_running:
                break
            time.sleep(0.05)
        assert controller.runtime.scheduler.is_running is False

        # Clean restart on runtime now succeeds
        controller.runtime.start()
        assert controller.runtime.is_running is True
        controller.runtime.stop(timeout=2.0)
        assert controller.runtime.is_running is False
    finally:
        blocker.set()
        controller.runtime.stop(timeout=2.0)
