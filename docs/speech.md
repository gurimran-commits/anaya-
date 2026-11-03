# Anaya Speech & Audio Engine Documentation

## 1. Overview

The `app.speech` package provides an offline-first, modular, and secure audio and speech subsystem for Anaya. It connects audio input (microphone) to speech-to-text (STT), routes transcripts through Anaya's natural language brain (`TextAssistant.process`), and optionally synthesizes responses back into audio via text-to-speech (TTS) and speaker playback.

```
+---------------------------------------------------------------------------------------+
|                                    VoicePipeline                                      |
|                                                                                       |
|  [AudioRecorder]  -->  [SpeechToText]  -->  [TextAssistant]  -->  [TextToSpeech]      |
|  (e.g. SoundDevice     (e.g. Whisper.cpp)    .process()            (e.g. Kokoro)      |
|   or MockRecorder)                                                      |             |
|                                                                         v             |
|                                                                   [AudioPlayer]       |
|                                                                   (e.g. SoundDevice   |
|                                                                    or MockPlayer)     |
+---------------------------------------------------------------------------------------+
```

---

## 2. Package Architecture

```text
app/speech/
    __init__.py           # Public exports and factory helpers
    models.py             # AudioFormat, AudioData, SpeechConfig, PipelineResult, and exceptions
    protocols.py          # AudioRecorder, SpeechToText, TextToSpeech, AudioPlayer protocols
    pipeline.py           # VoicePipeline orchestrator wrapping TextAssistant

    adapters/
        __init__.py       # Adapter exports
        mock.py           # Deterministic in-memory mock adapters
        sounddevice_io.py # Optional SoundDeviceRecorder & SoundDevicePlayer (lazy import)
        whisper_cpp.py    # Optional Whisper.cpp CLI STT subprocess adapter
        kokoro_tts.py     # Optional Kokoro neural TTS in-process adapter

tests/
    test_speech/
        __init__.py
        test_models.py
        test_protocols.py
        test_pipeline.py
        test_mock_adapters.py
        test_sounddevice_adapters.py
        test_whisper_cpp.py
        test_kokoro_tts.py

docs/
    speech.md             # This document
```

---

## 3. Protocol Contracts

All components adhere to typing `Protocol` definitions runtime-checked with `@runtime_checkable`:

### `AudioRecorder`
```python
class AudioRecorder(Protocol):
    def record(self, duration: float | None = None) -> AudioData: ...
```
- Captures microphone audio up to the specified duration or configured limit.
- Enforces push-to-talk semantics with bounded recording. Never runs continuous listening loops.
- Raises `SpeechDeviceError`, `SpeechTimeoutError`, or `AudioValidationError`.

### `SpeechToText`
```python
class SpeechToText(Protocol):
    def transcribe(self, audio: AudioData) -> str: ...
```
- Transcribes an `AudioData` payload into a normalized text string.
- Raises `AudioValidationError`, `SpeechBackendError`, or `SpeechTimeoutError`.

### `TextToSpeech`
```python
class TextToSpeech(Protocol):
    def synthesize(self, text: str) -> AudioData: ...
```
- Converts plain text into synthesized audio with accurate format metadata.
- Rejects empty or whitespace-only text.
- Raises `AudioValidationError` or `SpeechBackendError`.

### `AudioPlayer`
```python
class AudioPlayer(Protocol):
    def play(self, audio: AudioData) -> None: ...
```
- Plays `AudioData` to speakers.
- Releases audio stream resources upon completion, error, or interruption.
- Raises `AudioValidationError` or `SpeechDeviceError`.

---

## 4. Audio Data Models

### `AudioFormat`
An immutable `@dataclass(frozen=True)` describing audio representation:
- `sample_rate`: Sampling frequency in Hz (`[4000, 192000]`).
- `channels`: `1` (mono) or `2` (stereo).
- `sample_width`: Number of bytes per sample (`1`, `2`, or `4`).
- `encoding`: String identifier (e.g. `'pcm_s16le'`, `'pcm_f32le'`, `'wav'`).
- Helper properties: `frame_size` (`channels * sample_width`), `is_pcm` (`encoding.startswith('pcm_')`).

### `AudioData`
An encapsulated, immutable audio buffer:
- `data`: Immutable `bytes` buffer.
- `format`: `AudioFormat` metadata.
- `duration`: Duration in seconds (automatically computed for PCM).
- Methods:
  - `to_wav_bytes() -> bytes`: Generates canonical RIFF/WAV header for PCM data.
  - `AudioData.from_wav_bytes(wav_bytes) -> AudioData`: Parses WAV headers into PCM payload and format metadata.

### `SpeechConfig`
Configuration dataclass storing speech defaults:
- `sample_rate`: `16000` (Mitra default 16 kHz mono).
- `channels`: `1`.
- `sample_width`: `2` (16-bit).
- `max_record_seconds`: `15.0` (Mitra default 15s push-to-talk limit; capped at 60s max).
- `push_to_talk`: `True`.
- `stt_backend`: `'mock'` (or `'whisper_cpp'`).
- `tts_backend`: `'mock'` (or `'kokoro'`).
- `tts_sample_rate`: `24000` (native sample rate of Kokoro).

### `PipelineResult`
Structured result returned by `VoicePipeline.process_audio()` and `VoicePipeline.listen_and_process()`:
- `transcript: str | None`: Normalized transcription text.
- `action_result: ActionResult | None`: `ActionResult` returned by `TextAssistant.process()`.
- `spoken_response: bool`: `True` if TTS and speaker playback succeeded.
- `success: bool`: Overall success status.
- `error: str | None`: Descriptive failure message if any stage failed.

---

## 5. Security & Privacy Guarantees

1. **TextAssistant Invariant:**
   - All inbound voice commands enter through `TextAssistant.process(cleaned_transcript)`.
   - Voice commands cannot bypass intent parsing, registry validation, permissions, or `ApprovalService`.
2. **Rejection of In-Band Verbal Approval:**
   - Destructive actions (`filesystem.delete`, `memory.clear`) return confirmation tokens requiring out-of-band approval.
   - Spoken confirmations ("yes", "confirm", "approve") are passed to `TextAssistant`, which strictly rejects them with `"Text confirmation is disabled. Approve or reject the pending request through the application's explicit approval interface."`.
3. **Push-to-Talk Only:**
   - Microphones are only opened upon explicit request. Continuous background ambient listening is forbidden.
4. **Offline Operation:**
   - No cloud APIs, telemetry, or remote network calls.
   - Automatic downloads of weights, models, or voices from Hugging Face or other repositories are strictly prohibited.
5. **Temporary File Handling:**
   - Audio is retained strictly in volatile RAM (`io.BytesIO` / memory buffers).
   - If an external CLI engine (such as Whisper.cpp) requires a WAV file, it is written exclusively to the OS temporary directory (`tempfile.NamedTemporaryFile`) and guaranteed to be deleted in a `try...finally` block upon success, failure, timeout, or interruption. Temporary recordings are never stored in the project directory.

---

## 6. Adapter Reference & Setup

### A. Mock Adapters (`app.speech.adapters.mock`)
Used for deterministic unit and CI testing with zero hardware or neural model dependencies:
- `MockAudioRecorder(next_audio=..., fail_with=...)`
- `MockSpeechToText(next_transcript=..., transcript_sequence=..., fail_with=...)`
- `MockTextToSpeech(next_audio=..., fail_with=...)`
- `MockAudioPlayer(fail_with=...)`

### B. SoundDevice Adapters (`app.speech.adapters.sounddevice_io`)
Provides real microphone input and speaker output using the `sounddevice` package:
- **Dependency:** Optional `sounddevice` (and `numpy`).
- **Installation:**
  ```powershell
  pip install sounddevice numpy
  ```
- **Behavior:**
  - Imported lazily; importing `app.speech` does not require `sounddevice` to be installed.
  - Enforces `max_duration` (default 15.0s) to avoid unbounded buffering.
  - Explicitly invokes `sounddevice.stop()` in `finally` blocks to release device streams.

### C. Whisper.cpp STT Adapter (`app.speech.adapters.whisper_cpp`)
Offline STT adapter utilizing local `whisper.cpp` executable and GGML model files:
- **Prerequisites:**
  1. Standalone `whisper.exe` binary built from [whisper.cpp](https://github.com/ggerganov/whisper.cpp).
  2. Local GGML model file (e.g. `ggml-tiny.bin` or `ggml-base.en.bin`).
- **Configuration:**
  ```python
  from app.speech.adapters.whisper_cpp import WhisperCppSTT

  stt = WhisperCppSTT(
      executable_path="C:/tools/whisper.cpp/whisper.exe",
      model_path="C:/models/whisper/ggml-base.en.bin",
      timeout_seconds=30.0,
      language="en",
  )
  ```
- **Requirements & Safety:**
  - Verifies executable and model exist before execution.
  - Invokes subprocess with argument lists and `shell=False`.
  - Normalizes timestamps e.g. `[00:00:00.000 --> 00:00:02.000]` and limits output length to 4096 characters.
  - Cleans up temporary WAV files in `finally`.

### D. Kokoro Neural TTS Adapter (`app.speech.adapters.kokoro_tts`)
Offline neural TTS adapter using Kokoro:
- **Prerequisites:**
  1. `kokoro>=0.9.4`, `torch>=2.0.0`, and `soundfile>=0.14.0` (already included in `.venv312win`).
  2. Local Kokoro model file (`kokoro-v1_0.pth` or ONNX model).
  3. Local Kokoro `config.json`.
  4. Local voice embedding tensor file (e.g. `af_heart.pt` in a voices directory).
- **Configuration:**
  ```python
  from app.speech.adapters.kokoro_tts import KokoroTTS

  tts = KokoroTTS(
      model_path="C:/models/kokoro/kokoro-v1_0.pth",
      config_path="C:/models/kokoro/config.json",
      voices_dir="C:/models/kokoro/voices",
      voice="af_heart",
      device="cpu",
  )
  ```
- **Offline Assurance:**
  - Lazy imports; no model weights loaded at package import.
  - If local weights are missing, raises `SpeechBackendError` explaining that automatic downloads are forbidden in offline mode.
  - Generates 24000 Hz 16-bit mono PCM in memory.

---

## 7. Example Pipeline Construction

### Testing with Mock Adapters
```python
from app.brain.text_assistant import TextAssistant
from app.commands.executor import CommandExecutor
from app.commands.registry import CommandRegistry
from app.speech import create_mock_pipeline

# 1. Initialize Brain
registry = CommandRegistry()
executor = CommandExecutor(registry)
assistant = TextAssistant(registry, executor)

# 2. Build mock pipeline
pipeline = create_mock_pipeline(assistant, transcript="system.status")

# 3. Execute
result = pipeline.listen_and_process(speak_response=True)
print(f"Transcript: {result.transcript}")
print(f"Result: {result.action_result.message}")
print(f"Spoken: {result.spoken_response}")
```

### Physical Audio Assembly (when hardware backends are configured)
```python
from app.speech.adapters.kokoro_tts import KokoroTTS
from app.speech.adapters.sounddevice_io import SoundDevicePlayer, SoundDeviceRecorder
from app.speech.adapters.whisper_cpp import WhisperCppSTT
from app.speech.models import SpeechConfig
from app.speech.pipeline import VoicePipeline

config = SpeechConfig(
    max_record_seconds=15.0,
    push_to_talk=True,
)

recorder = SoundDeviceRecorder(max_duration=config.max_record_seconds)
player = SoundDevicePlayer()
stt = WhisperCppSTT(
    executable_path="C:/tools/whisper.exe",
    model_path="C:/models/ggml-base.en.bin",
)
tts = KokoroTTS(
    model_path="C:/models/kokoro-v1_0.pth",
    config_path="C:/models/config.json",
    voices_dir="C:/models/voices",
    voice="af_heart",
)

pipeline = VoicePipeline(
    recorder=recorder,
    stt=stt,
    assistant=assistant,
    tts=tts,
    player=player,
    config=config,
)
```

---

## 8. Failure Modes & Graceful Degradation

- **STT Failure / Empty Transcript:**
  - `TextAssistant` is never invoked.
  - Returns `PipelineResult(success=False, error="...")`.
- **Brain Failure:**
  - Does not corrupt context or return fake success.
  - Returns `PipelineResult(success=False, error=action_result.message)`.
- **TTS or Playback Failure:**
  - Does not undo or invalidate a successful Brain operation.
  - `result.success` remains `True` (as the action executed), but `result.spoken_response` is set to `False`.
  - The application logs a warning and does not crash.

---

## 9. Running Tests

To run the speech test suite:
```powershell
python -m pytest tests/test_speech -v
```

To run the complete repository test suite:
```powershell
python -m pytest tests -q
```
Expected output: 285 passed, 5 skipped (Windows unprivileged symlink skips), 0 failed.
