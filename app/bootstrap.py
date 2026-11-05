"""Application bootstrap and dependency injection factory for Anaya.

Constructs exactly one instance of each stateful subsystem, enforces a shared
ApprovalService across commands and skills, and returns a fully wired AssistantController.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Mapping

from app.ai.manager import ModelManager
from app.ai.models import HardwareProfile, ModelSettings
from app.ai.providers.ollama import OllamaProvider
from app.commands.filesystem import FilesystemService
from app.commands.filesystem_commands import register_filesystem_commands
from app.controller import AssistantController
from app.core.config import DEFAULT_CONFIG, AppConfig
from app.memory.memory_manager import MemoryManager
from app.runtime import AnayaRuntime
from app.security.approval_service import ApprovalService
from app.skills.skill_manager import SkillManager
from app.speech.models import SpeechConfig
from app.speech.pipeline import VoicePipeline

if TYPE_CHECKING:
    from datetime import datetime
    from app.ai.protocols import ModelProvider
    from app.notifications.protocols import NotificationHandler
    from app.speech.protocols import (
        AudioPlayer,
        AudioRecorder,
        SpeechToText,
        TextToSpeech,
    )

logger = logging.getLogger(__name__)


def create_controller(
    config: AppConfig | None = None,
    memory_db_path: Path | str | None = None,
    tasks_db_path: Path | str | None = None,
    allowed_directories: tuple[Path, ...] | None = None,
    skill_permission_grants: Mapping[str, tuple[str, ...]] | None = None,
    enable_speech: bool = True,
    enable_ai: bool = True,
    custom_recorder: AudioRecorder | None = None,
    custom_stt: SpeechToText | None = None,
    custom_tts: TextToSpeech | None = None,
    custom_player: AudioPlayer | None = None,
    custom_ai_provider: ModelProvider | None = None,
    custom_notification_handler: NotificationHandler | None = None,
    clock: Callable[[], datetime] | None = None,
) -> AssistantController:
    """Assemble and wire the complete Anaya assistant subsystem graph.

    Guarantees:
        - Exactly one shared ApprovalService across skills, commands, and executor.
        - Single authoritative TaskManager, CommandRegistry, and TextAssistant in AnayaRuntime.
        - MemoryManager, SkillManager, and ModelManager injected without duplication.
        - Filesystem commands registered safely without overwriting.
        - Zero microphone activation or standing audio threads during initialization.
        - Zero LLM model loading or heavy network checks during initialization.
        - Safe rollback and resource closure if initialization fails partway through.
    """
    effective_config = config or DEFAULT_CONFIG

    # Validate custom speech adapters: must provide all four or none
    custom_adapters = {
        "custom_recorder": custom_recorder,
        "custom_stt": custom_stt,
        "custom_tts": custom_tts,
        "custom_player": custom_player,
    }
    provided_adapters = [k for k, v in custom_adapters.items() if v is not None]
    missing_adapters = [k for k, v in custom_adapters.items() if v is None]

    if provided_adapters and missing_adapters:
        raise ValueError(
            f"Partial custom speech adapter injection is not supported. "
            f"Provided: {', '.join(provided_adapters)}; Missing: {', '.join(missing_adapters)}. "
            "Provide all four adapters (recorder, stt, tts, player) or none."
        )

    initialized_memory_manager: MemoryManager | None = None
    model_manager: ModelManager | None = None
    initialized_runtime: AnayaRuntime | None = None
    initialized_filesystem_service: FilesystemService | None = None

    try:
        # 1. Authoritative Shared Approval Service
        # Enforce single instance across CommandExecutor and SkillExecutor.
        shared_approval_service = ApprovalService()

        # 2. Memory Subsystem
        initialized_memory_manager = MemoryManager(db_path=memory_db_path)

        # 3. Skills Subsystem
        # Explicitly inject the shared ApprovalService and permission grants into SkillManager.
        effective_grants = (
            effective_config.skill_permission_grants
            if skill_permission_grants is None
            else skill_permission_grants
        )
        skill_manager = SkillManager(
            approval_service=shared_approval_service,
            skill_permission_grants=effective_grants,
        )

        # 4. Local AI Subsystem (Lazy)
        # Does not connect or load models during bootstrap.
        if effective_config.ai_enabled and enable_ai:
            provider = custom_ai_provider or OllamaProvider()
            settings = ModelSettings(
                enabled=effective_config.ai_enabled,
                profile=effective_config.ai_profile,
            )
            model_manager = ModelManager(provider=provider, settings=settings)

        # 5. Core Application Runtime
        # Injects the shared ApprovalService, MemoryManager, SkillManager, and ModelManager.
        initialized_runtime = AnayaRuntime(
            config=effective_config,
            approval_service=shared_approval_service,
            memory_manager=initialized_memory_manager,
            skill_manager=skill_manager,
            model_manager=model_manager,
            notification_handler=custom_notification_handler,
            clock=clock,
            db_path=tasks_db_path,
        )

        # 6. Filesystem Commands Registration (if allowed directories specified)
        if allowed_directories:
            initialized_filesystem_service = FilesystemService(
                allowed_directories=allowed_directories
            )
            if initialized_runtime.registry.get("files.list") is None:
                register_filesystem_commands(
                    initialized_runtime.registry,
                    initialized_filesystem_service,
                )

        # 7. Voice Pipeline (Optional & Push-to-Talk)
        # Avoids hard failure when optional audio packages/devices are unavailable.
        voice_pipeline: VoicePipeline | None = None
        if enable_speech:
            speech_config = SpeechConfig(
                sample_rate=effective_config.speech_sample_rate,
                push_to_talk=effective_config.push_to_talk,
            )
            if len(provided_adapters) == 4:
                voice_pipeline = VoicePipeline(
                    recorder=custom_recorder,  # type: ignore[arg-type]
                    stt=custom_stt,  # type: ignore[arg-type]
                    assistant=initialized_runtime.text_assistant,
                    tts=custom_tts,  # type: ignore[arg-type]
                    player=custom_player,  # type: ignore[arg-type]
                    config=speech_config,
                )
            else:
                try:
                    from app.speech.adapters.kokoro_tts import KokoroTTSAdapter
                    from app.speech.adapters.sounddevice_io import (
                        SoundDeviceAudioPlayer,
                        SoundDeviceAudioRecorder,
                    )
                    from app.speech.adapters.whisper_cpp import WhisperCppSTTAdapter

                    recorder = SoundDeviceAudioRecorder()
                    stt = WhisperCppSTTAdapter()
                    tts = KokoroTTSAdapter()
                    player = SoundDeviceAudioPlayer()

                    voice_pipeline = VoicePipeline(
                        recorder=recorder,
                        stt=stt,
                        assistant=initialized_runtime.text_assistant,
                        tts=tts,
                        player=player,
                        config=speech_config,
                    )
                except Exception as exc:
                    logger.info(
                        "Audio adapters unavailable (%s); AssistantController will operate in text-only mode.",
                        exc,
                    )
                    voice_pipeline = None

        return AssistantController(
            runtime=initialized_runtime,
            voice_pipeline=voice_pipeline,
            memory_manager=initialized_memory_manager,
            skill_manager=skill_manager,
            model_manager=model_manager,
            filesystem_service=initialized_filesystem_service,
            config=effective_config,
        )

    except Exception:
        # Safe cleanup of partially initialized resources in reverse construction order
        if initialized_filesystem_service is not None:
            try:
                initialized_filesystem_service.close()
            except Exception:
                pass
            initialized_filesystem_service = None

        if initialized_runtime is not None:
            try:
                initialized_runtime.stop()
            except Exception:
                pass
            initialized_runtime = None

        if model_manager is not None:
            try:
                model_manager.unload_active_model()
            except Exception:
                pass
            model_manager = None

        if initialized_memory_manager is not None:
            try:
                initialized_memory_manager.close()
            except Exception:
                pass
            initialized_memory_manager = None

        raise
