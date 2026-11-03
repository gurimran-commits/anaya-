# Anaya Local AI Model Switcher — Phase 7A Documentation

## 1. Overview & Architectural Principles

The Local AI Model Switcher introduces an optional local conversational intelligence layer to Anaya. Built on top of [Ollama](https://ollama.com/), it allows users to switch between different locally installed Large Language Models (LLMs) to match their personal computer's hardware specifications (e.g., lightweight models for low-RAM machines versus advanced models for dedicated GPU systems).

### Core Principles
- **Local-First & Offline**: Zero network calls to external cloud APIs. All interactions bind to `http://127.0.0.1:11434` on the loopback interface.
- **Zero Additional Mandatory Dependencies**: Built entirely with Python's standard library (`urllib.request`, `json`, `dataclasses`).
- **No Automatic Downloads**: Anaya **never** silently pulls or downloads models in the background. Users explicitly manage model downloads via the standard Ollama CLI.
- **Preserved Deterministic Invariants**: Anaya's rule-based command engine, task manager, persistent memory engine, filesystem service, and spoken-approval security barriers operate with strict precedence over conversational AI.

```
+-------------------------------------------------------------------+
|                     User Input (Text / Speech)                    |
+-------------------------------------------------------------------+
                                  |
                                  v
+-------------------------------------------------------------------+
|                        TextAssistant                              |
|   1. Spoken Confirmation Rejection (e.g. "yes", "confirm")       |
|   2. Context Reset Keywords ("reset", "clear history")            |
|   3. IntentHandler & NaturalLanguageParser (Deterministic Matching)|
+-------------------------------------------------------------------+
              /                                         \
    Intent Match?                                    No Match?
            /                                             \
           v                                               v
+------------------------+                    +------------------------+
|    CommandExecutor     |                    |      ModelManager      |
| (Files, Memory, Tasks, |                    | (Active Model, History,|
|  Skills with Approvals)|                    |  Profile Tuning)       |
+------------------------+                    +------------------------+
                                                           |
                                                           v
                                              +------------------------+
                                              |     OllamaProvider     |
                                              | (Loopback REST Client) |
                                              +------------------------+
                                                           |
                                                           v
                                              +------------------------+
                                              |  Local Ollama Daemon   |
                                              | (http://127.0.0.1:11434|
                                              +------------------------+
```

---

## 2. Optional External Prerequisite: Ollama

> [!IMPORTANT]
> **Ollama and its language models are optional external prerequisites and are not bundled with Anaya.**
> Anaya continues to function completely without Ollama installed. If Ollama is not installed or the daemon is stopped, all deterministic commands, task management, memory operations, and skills execute normally, falling back cleanly to deterministic response handling.

### Installing Ollama
- **Windows**: Download and run the Windows installer from [ollama.com/download](https://ollama.com/download).
- **Linux**: `curl -fsSL https://ollama.com/install.sh | sh`
- **macOS**: Download the `.zip` archive or install via Homebrew (`brew install ollama`).

Verify Ollama is running in your terminal:
```bash
ollama --version
```

---

## 3. Hardware Profiles & Model Catalog

Anaya categorizes supported models into three hardware tiers to prevent Out-Of-Memory (OOM) crashes, system freezing, and high inference latency on resource-constrained PCs.

| Profile Tier | Target Hardware | Parameter Range | Recommended Models | Recommended Ollama Command | Typical RAM / VRAM | Context (`num_ctx`) | Max Tokens (`num_predict`) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Lightweight** | Intel Core i3 / 4 GB RAM / Integrated graphics | **0.5B – 1.0B** | `qwen2.5:0.5b`<br>`llama3.2:1b` | `ollama pull qwen2.5:0.5b`<br>`ollama pull llama3.2:1b` | ~0.8 GB – 1.6 GB | 2,048 tokens | 256 tokens |
| **Balanced** | Standard PC / 8–16 GB RAM / Standard GPU | **1.5B – 3.0B** | `qwen2.5:1.5b`<br>`llama3.2:3b`<br>`gemma2:2b` | `ollama pull qwen2.5:1.5b`<br>`ollama pull llama3.2:3b` | ~2.0 GB – 4.2 GB | 4,096 tokens | 512 tokens |
| **Advanced** | Capable PC / 16+ GB RAM / Dedicated 6GB+ GPU | **3.8B – 7.0B** | `qwen2.5:7b`<br>`mistral:7b`<br>`phi-3-mini:3.8b` | `ollama pull qwen2.5:7b`<br>`ollama pull mistral:7b` | ~5.0 GB – 8.5 GB | 8,192 tokens | 1,024 tokens |

> [!WARNING]
> Hardware profiles are guidance recommendations, not absolute guarantees. Total system RAM alone does not dictate model compatibility. Factors such as available unreserved RAM, discrete GPU VRAM, quantization scheme (`Q4_K_M`), and KV-cache sizing linearly affect memory consumption.

---

## 4. Component Architecture & Responsibilities

The package `app/ai/` contains the following modules:

### 1. `models.py`
Defines core data classes, enums, settings, and exceptions:
- **`ChatMessage`**: Frozen representation of a single conversation turn with validation for roles (`"system"`, `"user"`, `"assistant"`).
- **`ModelInfo` & `ModelDetails`**: Structured metadata parsed from Ollama (`name`, `size_bytes`, `family`, `parameter_size`, `quantization_level`).
- **`GenerationOptions`**: Strict options passed to the model (`temperature`, `num_predict`, `num_ctx`, `top_k`, `top_p`, `keep_alive`).
- **`GenerationResult`**: Output wrapper containing generated content, token counts, model identifier, and duration.
- **`ModelSettings`**: Persistent settings manager supporting save/load to `~/.anaya/ai_settings.json`.
- **`HardwareProfile`**: Enum representing target tiers (`LIGHTWEIGHT`, `BALANCED`, `ADVANCED`, `CUSTOM`).

### 2. `protocols.py`
Defines `ModelProvider` as a runtime-checkable `Protocol`:
- `ping() -> bool`
- `list_models() -> list[ModelInfo]`
- `get_model_details(model_name: str) -> ModelDetails | None`
- `chat(messages: list[ChatMessage], model: str, options: GenerationOptions | None) -> GenerationResult`
- `unload_model(model_name: str) -> bool`

### 3. `providers/ollama.py`
Standard-library HTTP client for Ollama's local REST API:
- **Loopback Enforcement**: Validates URL hostname against `{127.0.0.1, localhost, ::1, [::1]}`. Rejects external network addresses by default.
- **Size Bounds**: Implements a maximum response payload buffer (10 MB) to prevent denial-of-service from malformed streams.
- **Timeouts**: Applies explicit connection and read timeouts.
- **Keep-Alive Eviction**: Uses `POST /api/generate` with `keep_alive: 0` to immediately evict models from RAM/VRAM.

### 4. `registry.py`
- Maintains curated recommendations for each hardware profile.
- Maps model tags to their corresponding profile tier.
- Discovers installed models via `provider.list_models()`.
- Does not download or modify local models.

### 5. `history.py`
- Implements `ConversationHistory`, an in-memory chronological buffer.
- Enforces turn bounds (`max_turns: int = 10`, maintaining at most 20 conversational messages).
- Trims oldest messages when bounds are reached while **always preserving the system prompt**.
- **Ephemeral**: Never persists conversation history to disk and never conflates dialogue with persistent Memory Engine facts.

### 6. `manager.py`
- `ModelManager` orchestrates the active model, profile presets, history, and provider communication.
- **Safe Switching Trade-Off (Unload-First Pattern)**:
  1. Validates that the requested target model is installed before modifying internal state. If validation fails, active model state remains completely untouched (`ModelNotFoundError`).
  2. Once target validation passes, explicitly unloads the outgoing model (`keep_alive: 0`) and immediately sets `active_model = None` before activating the target model to prevent dual-model memory/VRAM spikes.
  3. If target activation fails (e.g. system error), `active_model` remains `None`. The manager never falsely claims the previous model is still loaded.
  4. Subsequent selection of an installed model recovers cleanly without lingering failed state.
- **Fallback**: If Ollama crashes or is unavailable, `generate_response()` catches exceptions, logs a warning, and returns `None`, allowing `TextAssistant` to fall back seamlessly to deterministic command messaging.

---

## 5. Security & Isolation Boundaries

1. **Deterministic Precedence**:
   - Spoken approval rejection (`"yes"`, `"approve"`, `"confirm"`) is evaluated first.
   - Exact and natural language command intents (`files.*`, `memory.*`, `skills.*`, `tasks.*`) are matched deterministically.
   - Conversational AI is invoked **only** if the input does not match any registered command.
2. **Read-Only / No Execution Privilege**:
   - The LLM generates text only. It possesses zero execution tools, cannot invoke system commands, cannot approve actions, and cannot modify memory or filesystem state.
   - Model outputs are never fed back into the command executor as shell commands.
3. **Endpoint Sandboxing**:
   - Ollama URLs are restricted to localhost/loopback. Remote endpoints are rejected unless `allow_non_loopback=True` is explicitly passed in code.
4. **Privacy**:
   - Zero telemetry. Zero external cloud API calls. Dialogue remains strictly in local memory and is deleted when the session ends or `"reset context"` is issued.

---

## 6. Configuration & Persistence

Configuration is persisted to `~/.anaya/ai_settings.json` (or passed via dependency injection in tests):

```json
{
  "enabled": true,
  "provider": "ollama",
  "selected_model": "qwen2.5:1.5b",
  "profile": "balanced",
  "base_url": "http://127.0.0.1:11434",
  "context_window": 4096,
  "max_tokens": 512,
  "temperature": 0.7,
  "keep_alive": "5m",
  "timeout_seconds": 30.0,
  "max_history_turns": 10
}
```

- Safe defaults are used if the file is absent or corrupted.
- Importing the `app.ai` package **never** writes files to disk automatically.

---

## 7. Known Limitations & Future Work

- **Streaming Generation**: Version 1 uses synchronous request/response (`stream: false`). Streaming token generation will be considered in future phases.
- **Tool Calling / Function Calling**: Model outputs are strictly conversational prose. Structured tool-calling with schema enforcement and explicit user approval will be designed in a separate phase.
- **Multi-Modal / Vision**: Version 1 focuses exclusively on text-to-text chat models.
