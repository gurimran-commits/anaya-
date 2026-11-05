"""Central assistant controller coordinating text, voice, and lifecycle.

Integrates AnayaRuntime, VoicePipeline, and subsystem managers into a unified
application facade without creating alternate command execution pathways.
"""

from __future__ import annotations

import logging
from threading import RLock
from typing import TYPE_CHECKING, Any

from app.core.action_result import ActionResult
from app.core.config import DEFAULT_CONFIG, AppConfig
from app.speech.models import AudioData, PipelineResult

if TYPE_CHECKING:
    from app.ai.manager import ModelManager
    from app.brain.text_assistant import TextAssistant
    from app.commands.filesystem import FilesystemService
    from app.memory.memory_manager import MemoryManager
    from app.runtime import AnayaRuntime
    from app.security.approval_service import ApprovalService
    from app.skills.skill_manager import SkillManager
    from app.speech.pipeline import VoicePipeline
    from app.tasks.task_manager import TaskManager

logger = logging.getLogger(__name__)


class AssistantController:
    """High-level application coordinator for the Anaya assistant.

    Guarantees:
        - Text and voice inputs enter strictly through TextAssistant.process().
        - CommandExecutor remains the single authoritative security boundary.
        - Shared ApprovalService instance across commands and skills.
        - No generic conversational approval method exposed.
        - Clean resource cleanup across crashes and shutdown timeouts.
    """

    def __init__(
        self,
        runtime: "AnayaRuntime",
        voice_pipeline: "VoicePipeline | None" = None,
        memory_manager: "MemoryManager | None" = None,
        skill_manager: "SkillManager | None" = None,
        model_manager: "ModelManager | None" = None,
        filesystem_service: "FilesystemService | None" = None,
        config: AppConfig | None = None,
    ) -> None:
        self._runtime = runtime
        self._voice_pipeline = voice_pipeline
        self._memory_manager = memory_manager
        self._skill_manager = skill_manager
        self._model_manager = model_manager
        self._filesystem_service = filesystem_service
        self._config = config or runtime.config or DEFAULT_CONFIG
        self._lock = RLock()
        self._stopped = False
        self._cleanly_stopped = False

    @property
    def is_running(self) -> bool:
        """Return True if background runtime and scheduler are active."""
        return self._runtime.is_running

    @property
    def config(self) -> AppConfig:
        """Return current application configuration."""
        return self._config

    @property
    def runtime(self) -> "AnayaRuntime":
        """Expose core runtime coordinator."""
        return self._runtime

    @property
    def voice_pipeline(self) -> "VoicePipeline | None":
        """Expose configured voice pipeline (if speech subsystem is available)."""
        return self._voice_pipeline

    @property
    def memory_manager(self) -> "MemoryManager | None":
        """Expose persistent memory manager."""
        return self._memory_manager

    @property
    def skill_manager(self) -> "SkillManager | None":
        """Expose modular skills manager."""
        return self._skill_manager

    @property
    def model_manager(self) -> "ModelManager | None":
        """Expose local AI model manager."""
        return self._model_manager

    @property
    def filesystem_service(self) -> "FilesystemService | None":
        """Expose filesystem service."""
        return self._filesystem_service

    @property
    def approval_service(self) -> "ApprovalService":
        """Expose the authoritative ApprovalService for out-of-band authorization."""
        return self._runtime.approval_service

    @property
    def task_manager(self) -> "TaskManager":
        """Expose task manager domain service."""
        return self._runtime.task_manager

    @property
    def text_assistant(self) -> "TextAssistant":
        """Expose brain text assistant."""
        return self._runtime.text_assistant

    def handle_text(self, text: str) -> ActionResult:
        """Process natural language or command text through TextAssistant.

        Execution is authoritative and deterministic; text confirmation ("yes",
        "confirm", "approve") is strictly blocked by the Brain security barrier.
        """
        return self._runtime.process(text)

    def handle_voice(
        self,
        duration: float | None = None,
        speak_response: bool = False,
    ) -> PipelineResult:
        """Capture microphone audio, transcribe, and route through TextAssistant.

        Enforces push-to-talk duration bounds; never initiates continuous background listening.
        If the speech subsystem is unavailable, returns a structured failure without crashing.
        """
        if self._voice_pipeline is None:
            return PipelineResult(
                transcript=None,
                action_result=None,
                spoken_response=False,
                success=False,
                error="Speech subsystem is unavailable or not configured.",
            )

        return self._voice_pipeline.listen_and_process(
            duration=duration,
            speak_response=speak_response,
        )

    def handle_audio(
        self,
        audio_data: AudioData,
        speak_response: bool = False,
    ) -> PipelineResult:
        """Process an explicit AudioData payload through the voice pipeline."""
        if self._voice_pipeline is None:
            return PipelineResult(
                transcript=None,
                action_result=None,
                spoken_response=False,
                success=False,
                error="Speech subsystem is unavailable or not configured.",
            )

        return self._voice_pipeline.process_audio(
            audio_data=audio_data,
            speak_response=speak_response,
        )

    def speak(self, text: str) -> bool:
        """Synthesize and play speech aloud for the given text message.

        Returns False if speech subsystem is unavailable or playback fails.
        """
        if self._voice_pipeline is None:
            logger.warning("speak() requested but voice pipeline is unavailable.")
            return False

        return self._voice_pipeline.speak(text)

    def start(self) -> None:
        """Start the application runtime and background reminder scheduler.

        Idempotent: safe to call repeatedly while active. If the controller has
        already been stopped, raises RuntimeError (single-use lifecycle). If a previous
        scheduler worker is still alive (e.g. hung after shutdown timeout), raises
        RuntimeError without creating duplicate threads.
        """
        with self._lock:
            if self._stopped:
                raise RuntimeError(
                    "AssistantController has been stopped and cannot be restarted. "
                    "Create a new controller instance instead."
                )
            self._runtime.start()
            logger.info("AssistantController started.")

    def stop(self, timeout: float = 5.0) -> None:
        """Cleanly stop background services, close SQLite stores, and unload active AI model.

        Preserves Phase 7B shutdown safety: if the scheduler worker thread does
        not exit within the timeout, TaskStore is left open to prevent crashing
        the active worker.

        Repeated calls to stop() are safe and idempotent once cleanly stopped.
        If runtime.stop() or any auxiliary resource cleanup fails, the controller
        is marked terminal (cannot be restarted), errors are reported and raised,
        and cleanly_stopped is not marked True.
        """
        with self._lock:
            if self._cleanly_stopped:
                return

            self._stopped = True
            runtime_exc: BaseException | None = None
            cleanup_errors: list[Exception] = []

            try:
                self._runtime.stop(timeout=timeout)
            except Exception as exc:
                runtime_exc = exc
                logger.error("Error stopping runtime: %s", exc)

            if self._memory_manager is not None:
                try:
                    self._memory_manager.close()
                except Exception as exc:
                    cleanup_errors.append(exc)
                    logger.error("Error closing MemoryManager store: %s", exc)

            if self._filesystem_service is not None:
                try:
                    self._filesystem_service.close()
                except Exception as exc:
                    cleanup_errors.append(exc)
                    logger.error("Error closing FilesystemService: %s", exc)

            if self._model_manager is not None:
                try:
                    self._model_manager.unload_active_model()
                except Exception as exc:
                    cleanup_errors.append(exc)
                    logger.error("Error unloading active AI model on stop: %s", exc)

            if runtime_exc is not None:
                if cleanup_errors:
                    logger.error(
                        "Additional errors occurred during auxiliary cleanup: %s",
                        cleanup_errors,
                    )
                raise runtime_exc

            if cleanup_errors:
                logger.error("Stop completed with auxiliary cleanup failures: %s", cleanup_errors)
                if len(cleanup_errors) == 1:
                    raise cleanup_errors[0]
                raise RuntimeError(f"Multiple cleanup failures during shutdown: {cleanup_errors}")

            self._cleanly_stopped = True
            logger.info("AssistantController stopped cleanly.")

    def __enter__(self) -> AssistantController:
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.stop()
