"""Whisper.cpp speech-to-text adapter executing standalone local binary via subprocess."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Sequence

from app.speech.models import (
    AudioData,
    AudioFormat,
    AudioValidationError,
    SpeechBackendError,
    SpeechTimeoutError,
)


class WhisperCppSTT:
    """Offline STT adapter utilizing local whisper.cpp executable and GGML model files.

    All execution occurs locally with no internet connectivity or automatic downloads.
    """

    def __init__(
        self,
        executable_path: str | Path,
        model_path: str | Path,
        timeout_seconds: float = 30.0,
        language: str = "en",
        extra_args: Sequence[str] | None = None,
        _runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self.executable_path = Path(executable_path)
        self.model_path = Path(model_path)
        self.timeout_seconds = timeout_seconds
        self.language = language
        self.extra_args = list(extra_args) if extra_args else []
        self._runner = _runner or subprocess.run

    def _validate_paths(self) -> None:
        """Validate that the executable and model files exist locally on disk."""
        if not self.executable_path.is_file():
            raise SpeechBackendError(
                f"Whisper.cpp executable not found at '{self.executable_path}'. "
                "Explicit local binary path is required; automatic downloads are disabled."
            )
        if not self.model_path.is_file():
            raise SpeechBackendError(
                f"Whisper.cpp model not found at '{self.model_path}'. "
                "Explicit local model file is required; automatic downloads are disabled."
            )

    def _prepare_wav_payload(self, audio: AudioData) -> bytes:
        """Ensure audio payload is 16 kHz mono 16-bit WAV for whisper.cpp compatibility."""
        if audio.format.sample_rate != 16000 or audio.format.channels != 1:
            # If the format differs, whisper.cpp requires 16kHz mono.
            # Convert or enforce valid input format.
            raise AudioValidationError(
                f"Whisper.cpp requires 16000 Hz mono audio, got {audio.format.sample_rate} Hz, "
                f"{audio.format.channels} channel(s)."
            )
        return audio.to_wav_bytes()

    def transcribe(self, audio: AudioData) -> str:
        """Transcribe speech audio using local whisper.cpp subprocess.

        Temporary audio file is created in OS temp dir and guaranteed to be deleted in finally block.
        """
        if not isinstance(audio, AudioData):
            raise AudioValidationError(f"Expected AudioData instance, got {type(audio).__name__}")

        self._validate_paths()
        wav_bytes = self._prepare_wav_payload(audio)

        # Create temporary WAV file strictly in OS temp directory
        temp_wav: str | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
                temp_wav = tf.name
                tf.write(wav_bytes)

            cmd: list[str] = [
                str(self.executable_path),
                "-m",
                str(self.model_path),
                "-f",
                temp_wav,
                "-l",
                self.language,
                "--no-timestamps",
                "-nt",
            ]
            if self.extra_args:
                cmd.extend(self.extra_args)

            # Subprocess execution with explicit arguments, shell=False, and timeout
            result = self._runner(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                shell=False,
            )

            if result.returncode != 0:
                stderr_summary = (result.stderr or "").strip()
                raise SpeechBackendError(
                    f"Whisper.cpp process exited with return code {result.returncode}: {stderr_summary}"
                )

            # Extract and normalize stdout transcript
            raw_stdout = result.stdout or ""
            return self._normalize_transcript(raw_stdout)

        except subprocess.TimeoutExpired as exc:
            raise SpeechTimeoutError(
                f"Whisper.cpp transcription timed out after {self.timeout_seconds} seconds"
            ) from exc
        except (subprocess.SubprocessError, OSError) as exc:
            if isinstance(exc, (SpeechBackendError, SpeechTimeoutError)):
                raise
            raise SpeechBackendError(f"Failed to execute whisper.cpp subprocess: {exc}") from exc
        finally:
            # Guarantee prompt deletion of temporary audio file
            if temp_wav is not None:
                try:
                    if os.path.exists(temp_wav):
                        os.unlink(temp_wav)
                except OSError:
                    pass

    @staticmethod
    def _normalize_transcript(raw_text: str) -> str:
        """Clean and normalize transcript text, removing timestamp artifacts and bounding length."""
        lines: list[str] = []
        for line in raw_text.splitlines():
            line = line.strip()
            if not line:
                continue
            # Strip bracketed timestamp artifacts if present e.g. [00:00:00.000 --> 00:00:02.000]
            if line.startswith("[") and "]" in line:
                line = line.split("]", 1)[1].strip()
            if line:
                lines.append(line)

        transcript = " ".join(lines)
        # Limit to reasonable length (e.g. 4096 chars) to prevent runaway memory
        if len(transcript) > 4096:
            transcript = transcript[:4096]
        return transcript.strip()
