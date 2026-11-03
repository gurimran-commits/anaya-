"""Kokoro neural text-to-speech adapter generating in-memory audio from local model assets."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.speech.models import (
    AudioData,
    AudioFormat,
    AudioValidationError,
    SpeechBackendError,
)

KOKORO_SAMPLE_RATE = 24000


def _get_kokoro_and_torch() -> tuple[Any, Any]:
    """Lazily import kokoro and torch modules, raising SpeechBackendError if missing."""
    try:
        import kokoro  # type: ignore
        import torch  # type: ignore
        return kokoro, torch
    except ImportError as exc:
        raise SpeechBackendError(
            "Kokoro or PyTorch dependencies are not installed. "
            "Install optional speech dependencies to enable neural text-to-speech."
        ) from exc


def _get_numpy() -> Any:
    """Lazily import numpy, raising SpeechBackendError if missing."""
    try:
        import numpy as np
        return np
    except ImportError as exc:
        raise SpeechBackendError("NumPy is required for Kokoro audio conversion.") from exc


class KokoroTTS:
    """Offline neural TTS adapter using Kokoro.

    All model weights, configuration, and voice files must be available locally.
    Automatic model downloading is strictly prohibited.
    """

    def __init__(
        self,
        model_path: str | Path | None = None,
        config_path: str | Path | None = None,
        voices_dir: str | Path | None = None,
        voice: str = "af_heart",
        language: str = "en",
        device: str = "cpu",
        _pipeline: Any | None = None,
    ) -> None:
        self.model_path = Path(model_path) if model_path is not None else None
        self.config_path = Path(config_path) if config_path is not None else None
        self.voices_dir = Path(voices_dir) if voices_dir is not None else None
        self.voice = voice
        self.language = language
        self.device = device
        self._pipeline = _pipeline
        self._resolved_voice: str | None = None

    def _ensure_pipeline(self) -> Any:
        """Lazily initialize and return the Kokoro pipeline using local assets."""
        if self._pipeline is not None:
            return self._pipeline

        kokoro, torch = _get_kokoro_and_torch()

        # Strict offline verification: require explicit, existing local assets
        if self.model_path is None or not self.model_path.is_file():
            raise SpeechBackendError(
                f"Kokoro model file not found at '{self.model_path}'. "
                "Explicit local model path is required; automatic downloads are disabled in offline mode."
            )
        if self.config_path is None or not self.config_path.is_file():
            raise SpeechBackendError(
                f"Kokoro config file not found at '{self.config_path}'. "
                "Explicit local config path is required; automatic downloads are disabled in offline mode."
            )

        # Resolve voice path
        voice_target: str
        if self.voice.endswith(".pt") and Path(self.voice).is_file():
            voice_target = str(Path(self.voice).resolve())
        elif self.voices_dir is not None:
            candidate = self.voices_dir / f"{self.voice}.pt"
            if not candidate.is_file():
                raise SpeechBackendError(
                    f"Kokoro voice asset '{self.voice}.pt' not found in voices directory '{self.voices_dir}'."
                )
            voice_target = str(candidate.resolve())
        else:
            raise SpeechBackendError(
                f"Kokoro voice asset path for '{self.voice}' cannot be resolved. "
                "Provide an explicit voices_dir or direct .pt voice path."
            )

        try:
            kmodel = kokoro.KModel(
                config=str(self.config_path),
                model=str(self.model_path),
            ).to(self.device).eval()

            # Map language to Kokoro language code
            lang_code_map = {
                "en": "a",
                "en-us": "a",
                "en-gb": "b",
                "es": "e",
                "fr": "f",
                "hi": "h",
                "it": "i",
                "pt": "p",
                "ja": "j",
                "zh": "z",
            }
            lang_code = lang_code_map.get(self.language.lower(), "a")

            pipeline = kokoro.KPipeline(
                lang_code=lang_code,
                model=kmodel,
                device=self.device,
            )
            self._pipeline = pipeline
            self._resolved_voice = voice_target
            return self._pipeline
        except Exception as exc:
            raise SpeechBackendError(f"Failed to initialize local Kokoro pipeline: {exc}") from exc

    def synthesize(self, text: str) -> AudioData:
        """Synthesize text to 24000 Hz 16-bit mono PCM in-memory audio data."""
        if not isinstance(text, str) or not text.strip():
            raise AudioValidationError("Cannot synthesize empty or whitespace text")

        pipeline = self._ensure_pipeline()
        np = _get_numpy()

        voice_to_use = self._resolved_voice or self.voice

        try:
            # Generate audio chunks from pipeline
            generator = pipeline(text.strip(), voice=voice_to_use)
            audio_segments: list[Any] = []

            for result in generator:
                # KPipeline yields Result objects with output.audio
                if hasattr(result, "output") and result.output is not None:
                    audio_piece = result.output.audio
                elif hasattr(result, "audio"):
                    audio_piece = result.audio
                elif isinstance(result, tuple) and len(result) >= 3:
                    audio_piece = result[2]
                else:
                    audio_piece = getattr(result, "audio", None)

                if audio_piece is not None:
                    if hasattr(audio_piece, "cpu"):
                        audio_piece = audio_piece.cpu()
                    if hasattr(audio_piece, "numpy"):
                        audio_np = audio_piece.numpy()
                    else:
                        audio_np = np.asarray(audio_piece)
                    audio_segments.append(audio_np)

            if not audio_segments:
                raise SpeechBackendError("Kokoro pipeline yielded no audio output for given text")

            combined = np.concatenate(audio_segments, axis=0) if len(audio_segments) > 1 else audio_segments[0]
            # Normalize and clip to [-1.0, 1.0], then convert to signed 16-bit PCM
            clipped = np.clip(combined, -1.0, 1.0)
            int16_audio = (clipped * 32767).astype(np.int16)
            raw_pcm = bytes(int16_audio.tobytes())

            fmt = AudioFormat(
                sample_rate=KOKORO_SAMPLE_RATE,
                channels=1,
                sample_width=2,
                encoding="pcm_s16le",
            )
            return AudioData(data=raw_pcm, format=fmt)

        except Exception as exc:
            if isinstance(exc, (AudioValidationError, SpeechBackendError)):
                raise
            raise SpeechBackendError(f"Kokoro speech synthesis failed: {exc}") from exc
