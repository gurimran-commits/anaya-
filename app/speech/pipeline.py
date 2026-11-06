"""Voice pipeline orchestrator coordinating audio capture, STT, Brain, TTS, and playback."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.speech.models import (
    AudioData,
    AudioValidationError,
    PipelineResult,
    SpeechConfig,
    SpeechError,
)
from app.speech.protocols import (
    AudioPlayer,
    AudioRecorder,
    SpeechToText,
    TextToSpeech,
)

if TYPE_CHECKING:
    from app.brain.text_assistant import TextAssistant

logger = logging.getLogger(__name__)


class VoicePipeline:
    """Synchronous pipeline orchestrating speech I/O and Brain text processing.

    All inbound voice commands enter the system strictly through TextAssistant.process(),
    preserving all existing Brain security barriers, validations, and approval workflows.
    """

    def __init__(
        self,
        recorder: AudioRecorder,
        stt: SpeechToText,
        assistant: TextAssistant,
        tts: TextToSpeech,
        player: AudioPlayer,
        config: SpeechConfig | None = None,
    ) -> None:
        self.recorder = recorder
        self.stt = stt
        self.assistant = assistant
        self.tts = tts
        self.player = player
        self.config = config or SpeechConfig()

    def process_audio(
        self,
        audio_data: AudioData,
        speak_response: bool = False,
    ) -> PipelineResult:
        """Process an audio payload through transcription, brain processing, and optional TTS.

        Steps:
            1. Validate audio payload.
            2. Transcribe via STT.
            3. Reject empty, whitespace, or invalid transcripts without invoking the Brain.
            4. Route valid transcript strictly through TextAssistant.process().
            5. Obtain ActionResult.
            6. Optionally synthesize and play response message.
            7. Return structured PipelineResult.
        """
        if not isinstance(audio_data, AudioData):
            return PipelineResult(
                success=False,
                error=f"Invalid audio input: expected AudioData instance, got {type(audio_data).__name__}",
            )

        # 1. Transcribe audio
        try:
            transcript = self.stt.transcribe(audio_data)
        except SpeechError as exc:
            logger.warning("Transcription failed: %s", exc)
            return PipelineResult(
                transcript=None,
                action_result=None,
                spoken_response=False,
                success=False,
                error=f"Transcription failed: {exc}",
            )
        except Exception as exc:
            logger.error("Unexpected error during transcription: %s", exc)
            return PipelineResult(
                transcript=None,
                action_result=None,
                spoken_response=False,
                success=False,
                error=f"Unexpected transcription error: {exc}",
            )

        # 2. Validate transcription output
        cleaned_transcript = (transcript or "").strip()
        if not cleaned_transcript:
            return PipelineResult(
                transcript="",
                action_result=None,
                spoken_response=False,
                success=False,
                error="Transcription produced empty or whitespace text",
            )

        # 3. Process strictly through TextAssistant
        try:
            action_result = self.assistant.process(cleaned_transcript)
        except Exception as exc:
            logger.error("TextAssistant processing failed: %s", exc)
            return PipelineResult(
                transcript=cleaned_transcript,
                action_result=None,
                spoken_response=False,
                success=False,
                error=f"Brain processing failed: {exc}",
            )

        # 4. Optional spoken response (TTS + Playback)
        spoken_success = False
        if speak_response and action_result.message:
            spoken_success = self.speak(action_result.message)

        # TTS or playback failure does not undo or invalidate a successful Brain operation
        return PipelineResult(
            transcript=cleaned_transcript,
            action_result=action_result,
            spoken_response=spoken_success,
            success=action_result.success,
            error=None if action_result.success else action_result.message,
        )

    def listen_and_process(
        self,
        duration: float | None = None,
        speak_response: bool = False,
    ) -> PipelineResult:
        """Capture microphone audio explicitly and process through the pipeline.

        Enforces push-to-talk duration bounds; never initiates continuous background listening.
        """
        effective_duration = duration if duration is not None else self.config.max_record_seconds
        try:
            audio = self.recorder.record(duration=effective_duration)
        except SpeechError as exc:
            logger.warning("Audio recording failed: %s", exc)
            return PipelineResult(
                transcript=None,
                action_result=None,
                spoken_response=False,
                success=False,
                error=f"Recording failed: {exc}",
            )
        except Exception as exc:
            logger.error("Unexpected error during recording: %s", exc)
            return PipelineResult(
                transcript=None,
                action_result=None,
                spoken_response=False,
                success=False,
                error=f"Unexpected recording error: {exc}",
            )

        return self.process_audio(audio, speak_response=speak_response)

    def speak(self, text: str) -> bool:
        """Synthesize and play speech for the given text message.

        Handles synthesis and playback errors safely without crashing the caller.
        """
        if not isinstance(text, str) or not text.strip():
            return False

        from app.speech.adapters.null import NullAudioPlayer, NullTextToSpeech

        if isinstance(self.tts, NullTextToSpeech) or isinstance(self.player, NullAudioPlayer):
            logger.debug("Spoken response skipped: TTS or audio player is not configured.")
            return False

        try:
            audio = self.tts.synthesize(text.strip())
            if not isinstance(audio, AudioData):
                logger.error("TTS returned invalid audio object: %s", type(audio))
                return False

            self.player.play(audio)
            return True
        except SpeechError as exc:
            logger.warning("Speech synthesis or playback failed: %s", exc)
            return False
        except Exception as exc:
            logger.error("Unexpected error in speak(): %s", exc)
            return False
