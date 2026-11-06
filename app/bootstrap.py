"""Application bootstrap and dependency injection factory for Anaya.

Constructs exactly one instance of each stateful subsystem, enforces a shared
ApprovalService across commands and skills, and returns a fully wired AssistantController.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import shutil
import sys
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


def resolve_whisper_paths(
    executable_path: str | Path | None = None,
    model_path: str | Path | None = None,
) -> tuple[Path | None, Path | None]:
    """Resolve Whisper.cpp executable and model paths from params, env vars, or standard locations."""
    resolved_exe: Path | None = None
    resolved_model: Path | None = None
    repo_root = Path(__file__).resolve().parents[1]

    # 1. Resolve Executable
    if executable_path is not None:
        p = Path(executable_path).expanduser().resolve()
        if p.is_file():
            resolved_exe = p
    elif env_exe := (os.environ.get("ANAYA_WHISPER_BIN") or os.environ.get("WHISPER_CPP_BIN") or os.environ.get("WHISPER_BIN")):
        p = Path(env_exe).expanduser().resolve()
        if p.is_file():
            resolved_exe = p
    else:
        is_windows = sys.platform == "win32"
        exe_name = "whisper-cli.exe" if is_windows else "whisper-cli"
        legacy_exe_name = "main.exe" if is_windows else "main"

        # Check candidate paths relative to repo root
        candidates = [
            repo_root.parent / "whisper.cpp" / "build" / "bin" / "Release" / exe_name,
            repo_root.parent / "whisper.cpp" / "build" / "bin" / exe_name,
            repo_root / "bin" / exe_name,
            Path.home() / ".anaya" / "bin" / exe_name,
            repo_root.parent / "whisper.cpp" / "build" / "bin" / "Release" / legacy_exe_name,
            repo_root.parent / "whisper.cpp" / "build" / "bin" / legacy_exe_name,
            repo_root.parent / "whisper.cpp" / legacy_exe_name,
        ]
        for c in candidates:
            if c.is_file():
                resolved_exe = c.resolve()
                break

        # Fall back to PATH search for whisper-cli or whisper
        if resolved_exe is None:
            for bin_name in ("whisper-cli", "whisper"):
                if which_exe := shutil.which(bin_name):
                    p = Path(which_exe).resolve()
                    if p.is_file() and (not is_windows or p.suffix.lower() == ".exe"):
                        resolved_exe = p
                        break

    # 2. Resolve Model
    if model_path is not None:
        p = Path(model_path).expanduser().resolve()
        if p.is_file():
            resolved_model = p
    elif env_model := (os.environ.get("ANAYA_WHISPER_MODEL") or os.environ.get("WHISPER_CPP_MODEL") or os.environ.get("WHISPER_MODEL")):
        p = Path(env_model).expanduser().resolve()
        if p.is_file():
            resolved_model = p
    else:
        candidates = [
            repo_root / "models" / "whisper" / "ggml-base.en.bin",
            repo_root / "models" / "whisper" / "ggml-base.bin",
            repo_root / "models" / "whisper" / "ggml-tiny.en.bin",
            repo_root / "models" / "whisper" / "ggml-tiny.bin",
            Path.home() / ".anaya" / "models" / "whisper" / "ggml-base.en.bin",
            repo_root.parent / "whisper.cpp" / "models" / "ggml-base.en.bin",
        ]
        for c in candidates:
            if c.is_file():
                resolved_model = c.resolve()
                break

        if resolved_model is None:
            whisper_dir = repo_root / "models" / "whisper"
            if whisper_dir.is_dir():
                for ext in ("*.bin", "*.gguf"):
                    found = list(whisper_dir.glob(ext))
                    if found:
                        resolved_model = found[0].resolve()
                        break

    return resolved_exe, resolved_model


def resolve_kokoro_paths(
    model_path: str | Path | None = None,
    config_path: str | Path | None = None,
    voices_dir: str | Path | None = None,
) -> tuple[Path | None, Path | None, Path | None]:
    """Resolve Kokoro TTS model, config, and voices directory."""
    resolved_model: Path | None = None
    resolved_config: Path | None = None
    resolved_voices: Path | None = None
    repo_root = Path(__file__).resolve().parents[1]

    # 1. Model
    if model_path is not None:
        p = Path(model_path).expanduser().resolve()
        if p.is_file():
            resolved_model = p
    elif env_model := os.environ.get("ANAYA_KOKORO_MODEL"):
        p = Path(env_model).expanduser().resolve()
        if p.is_file():
            resolved_model = p

    # 2. Config
    if config_path is not None:
        p = Path(config_path).expanduser().resolve()
        if p.is_file():
            resolved_config = p
    elif env_config := os.environ.get("ANAYA_KOKORO_CONFIG"):
        p = Path(env_config).expanduser().resolve()
        if p.is_file():
            resolved_config = p

    # 3. Voices
    if voices_dir is not None:
        p = Path(voices_dir).expanduser().resolve()
        if p.is_dir():
            resolved_voices = p
    elif env_voices := os.environ.get("ANAYA_KOKORO_VOICES"):
        p = Path(env_voices).expanduser().resolve()
        if p.is_dir():
            resolved_voices = p

    # Auto-discovery in HuggingFace cache if any part is missing
    if not (resolved_model and resolved_config and resolved_voices):
        hf_snapshots = (
            Path.home()
            / ".cache"
            / "huggingface"
            / "hub"
            / "models--hexgrad--Kokoro-82M"
            / "snapshots"
        )
        if hf_snapshots.is_dir():
            for snapshot_dir in hf_snapshots.iterdir():
                if snapshot_dir.is_dir():
                    m = snapshot_dir / "kokoro-v1_0.pth"
                    c = snapshot_dir / "config.json"
                    v = snapshot_dir / "voices"
                    if m.is_file() and c.is_file() and v.is_dir():
                        resolved_model = resolved_model or m.resolve()
                        resolved_config = resolved_config or c.resolve()
                        resolved_voices = resolved_voices or v.resolve()
                        break

    # Auto-discovery in repo_root / models / kokoro
    if not (resolved_model and resolved_config and resolved_voices):
        local_kokoro = repo_root / "models" / "kokoro"
        if local_kokoro.is_dir():
            m = local_kokoro / "kokoro-v1_0.pth"
            c = local_kokoro / "config.json"
            v = local_kokoro / "voices"
            if m.is_file() and c.is_file() and v.is_dir():
                resolved_model = resolved_model or m.resolve()
                resolved_config = resolved_config or c.resolve()
                resolved_voices = resolved_voices or v.resolve()

    return resolved_model, resolved_config, resolved_voices


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
    whisper_executable_path: Path | str | None = None,
    whisper_model_path: Path | str | None = None,
    kokoro_model_path: Path | str | None = None,
    kokoro_config_path: Path | str | None = None,
    kokoro_voices_dir: Path | str | None = None,
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
                sample_rate=16000,
                push_to_talk=effective_config.push_to_talk,
                tts_voice=effective_config.tts_voice,
                tts_language=effective_config.tts_language,
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
                recorder: AudioRecorder | None = None
                stt: SpeechToText | None = None
                tts: TextToSpeech | None = None
                player: AudioPlayer | None = None

                # A. Audio Recorder (Microphone Input)
                try:
                    from app.speech.adapters.sounddevice_io import SoundDeviceRecorder
                    recorder = SoundDeviceRecorder(
                        sample_rate=speech_config.sample_rate,
                        channels=speech_config.channels,
                    )
                except Exception as exc:
                    logger.warning("Microphone audio recorder unavailable: %s", exc)
                    recorder = None

                # B. Speech-to-Text (Whisper.cpp)
                whisper_exe, whisper_model = resolve_whisper_paths(
                    executable_path=whisper_executable_path,
                    model_path=whisper_model_path,
                )
                if whisper_exe is None:
                    logger.warning(
                        "Whisper.cpp executable not found. Configure ANAYA_WHISPER_BIN or place whisper-cli on PATH."
                    )
                elif whisper_model is None:
                    logger.warning(
                        "Whisper.cpp model not found. Configure ANAYA_WHISPER_MODEL or place ggml model in models/whisper."
                    )
                else:
                    try:
                        from app.speech.adapters.whisper_cpp import WhisperCppSTT
                        stt = WhisperCppSTT(
                            executable_path=whisper_exe,
                            model_path=whisper_model,
                            language="en",
                        )
                    except Exception as exc:
                        logger.warning("Whisper.cpp STT adapter initialization failed: %s", exc)
                        stt = None

                # C. Audio Player (Playback Output)
                try:
                    from app.speech.adapters.sounddevice_io import SoundDevicePlayer
                    player = SoundDevicePlayer()
                except Exception as exc:
                    logger.info("Audio playback output unavailable (%s); using null player.", exc)
                    player = None

                if player is None:
                    from app.speech.adapters.null import NullAudioPlayer
                    player = NullAudioPlayer(reason="Physical audio output device or library unavailable.")

                # D. Text-to-Speech (Kokoro TTS)
                k_model, k_config, k_voices = resolve_kokoro_paths(
                    model_path=kokoro_model_path,
                    config_path=kokoro_config_path,
                    voices_dir=kokoro_voices_dir,
                )
                if k_model and k_config and k_voices:
                    try:
                        from app.speech.adapters.kokoro_tts import KokoroTTS, _get_kokoro_and_torch
                        _get_kokoro_and_torch()
                        tts = KokoroTTS(
                            model_path=k_model,
                            config_path=k_config,
                            voices_dir=k_voices,
                            voice=effective_config.tts_voice,
                            language=effective_config.tts_language,
                        )
                    except Exception as exc:
                        logger.info("Kokoro TTS unavailable (%s); using null TTS.", exc)
                        tts = None
                else:
                    logger.info("Kokoro TTS assets not configured or not found; using null TTS.")
                    tts = None

                if tts is None:
                    from app.speech.adapters.null import NullTextToSpeech
                    tts = NullTextToSpeech(reason="Neural TTS dependencies or model assets unavailable.")

                # E. Assemble VoicePipeline if Input & STT are Available
                if recorder is not None and stt is not None:
                    voice_pipeline = VoicePipeline(
                        recorder=recorder,
                        stt=stt,
                        assistant=initialized_runtime.text_assistant,
                        tts=tts,
                        player=player,
                        config=speech_config,
                    )
                    logger.info(
                        "VoicePipeline initialized successfully (STT: Whisper.cpp, TTS: %s).",
                        type(tts).__name__,
                    )
                else:
                    reasons: list[str] = []
                    if recorder is None:
                        reasons.append("recorder unavailable")
                    if stt is None:
                        reasons.append("STT unavailable")
                    logger.info(
                        "VoicePipeline not initialized (%s); operating in text-only mode.",
                        ", ".join(reasons),
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
