"""Integration tests for TextAssistant and ModelManager routing order and boundaries."""

import pytest

from app.ai.history import ConversationHistory
from app.ai.manager import ModelManager
from app.ai.models import ChatMessage, GenerationOptions, GenerationResult, ModelDetails, ModelInfo
from app.brain.text_assistant import TextAssistant
from app.commands.executor import CommandExecutor
from app.commands.registry import CommandRegistry, CommandSpec
from app.security.approval_service import ApprovalService


class MockAIProvider:
    """Mock AI provider for TextAssistant integration tests."""

    def __init__(self, responsive: bool = True):
        self.responsive = responsive
        self.installed = [
            ModelInfo(name="qwen2.5:1.5b", size_bytes=1000000, details=ModelDetails())
        ]
        self.queries: list[str] = []

    def ping(self) -> bool:
        return self.responsive

    def list_models(self) -> list[ModelInfo]:
        return self.installed

    def get_model_details(self, model_name: str) -> ModelDetails | None:
        return ModelDetails()

    def chat(
        self,
        messages: list[ChatMessage],
        model: str,
        options: GenerationOptions | None = None,
    ) -> GenerationResult:
        if not self.responsive:
            raise ConnectionError("Ollama offline")
        last_query = messages[-1].content
        self.queries.append(last_query)
        return GenerationResult(
            success=True,
            content=f"AI answer to: {last_query}",
            model=model,
        )

    def unload_model(self, model_name: str) -> bool:
        return True


@pytest.fixture
def integrated_assistant():
    registry = CommandRegistry()
    registry.register(
        CommandSpec(
            name="demo.echo",
            description="Return supplied text",
            handler=lambda value: value,
            validate_arguments=lambda args: len(args) == 1,
        )
    )
    registry.register(
        CommandSpec(
            name="files.read",
            description="Read file contents",
            handler=lambda dir_idx, filename: f"Content of {filename}",
            validate_arguments=lambda args: len(args) == 2 and not args[1].startswith(".."),
        )
    )
    approval = ApprovalService()
    executor = CommandExecutor(registry, approval_service=approval)

    provider = MockAIProvider(responsive=True)
    manager = ModelManager(provider=provider)
    manager.select_model("qwen2.5:1.5b")

    assistant = TextAssistant(
        registry=registry,
        executor=executor,
        model_manager=manager,
    )
    return assistant, manager, provider


class TestAssistantAIIntegration:
    """Test suite verifying routing priority and security boundaries between Brain and AI."""

    def test_construction_without_model_manager(self):
        registry = CommandRegistry()
        executor = CommandExecutor(registry)
        assistant = TextAssistant(registry=registry, executor=executor)

        assert assistant.model_manager is None
        # Unrecognized command returns standard deterministic error
        res = assistant.process("Tell me a story")
        assert res.success is False
        assert "Command not recognized" in res.message

    def test_non_command_routes_to_ai(self, integrated_assistant):
        assistant, manager, provider = integrated_assistant

        res = assistant.process("What is the capital of France?")
        assert res.success is True
        assert "AI answer to: What is the capital of France?" in res.message
        assert len(provider.queries) == 1

    def test_deterministic_command_has_priority_over_ai(self, integrated_assistant):
        assistant, manager, provider = integrated_assistant

        # "help" is a deterministic command
        res = assistant.process("help")
        assert res.success is True
        assert "Available commands" in res.message
        # AI provider was never invoked
        assert len(provider.queries) == 0

    def test_spoken_confirmation_rejection_boundary_preserved(self, integrated_assistant):
        assistant, manager, provider = integrated_assistant

        res_yes = assistant.process("yes")
        assert res_yes.success is False
        assert "confirmation is disabled" in res_yes.message
        assert len(provider.queries) == 0

        res_confirm = assistant.process("confirm 1234")
        assert res_confirm.success is False
        assert "confirmation is disabled" in res_confirm.message
        assert len(provider.queries) == 0

    def test_reset_context_clears_ai_history(self, integrated_assistant):
        assistant, manager, provider = integrated_assistant

        assistant.process("Who wrote Hamlet?")
        assert manager.history.turn_count == 2

        res_reset = assistant.process("reset context")
        assert res_reset.success is True
        assert "Context has been reset" in res_reset.message

        # Both context and AI history are reset
        assert assistant.context.has_target is False
        assert manager.history.turn_count == 0

    def test_ai_fallback_when_disabled(self, integrated_assistant):
        assistant, manager, provider = integrated_assistant
        manager.disable()

        res = assistant.process("Tell me a story")
        assert res.success is False
        assert "Command not recognized" in res.message
        assert len(provider.queries) == 0

    def test_ai_fallback_when_ollama_offline(self, integrated_assistant):
        assistant, manager, provider = integrated_assistant
        provider.responsive = False

        res = assistant.process("Tell me a story")
        assert res.success is False
        assert "Command not recognized" in res.message

    def test_recognized_command_with_invalid_arguments_never_sent_to_ai(self, integrated_assistant):
        assistant, manager, provider = integrated_assistant

        # Recognized command with missing required arguments
        res = assistant.process("demo.echo")
        assert res.success is False
        assert "arguments are invalid" in res.message
        assert len(provider.queries) == 0

        # Recognized command with excessive arguments
        res2 = assistant.process("demo.echo arg1 arg2")
        assert res2.success is False
        assert "arguments are invalid" in res2.message
        assert len(provider.queries) == 0

    def test_explicit_unsupported_operations_never_sent_to_ai(self, integrated_assistant):
        assistant, manager, provider = integrated_assistant

        # Folder operations
        res_folder = assistant.process("create folder projects")
        assert res_folder.success is False
        assert "Folder operations are not currently supported" in res_folder.message
        assert len(provider.queries) == 0

        # System power operations
        res_power = assistant.process("shutdown computer")
        assert res_power.success is False
        assert "System power and process operations are not supported" in res_power.message
        assert len(provider.queries) == 0

        res_reboot = assistant.process("reboot system")
        assert res_reboot.success is False
        assert len(provider.queries) == 0

        # Messaging operations
        res_msg = assistant.process("send email to alice")
        assert res_msg.success is False
        assert "Messaging and email operations are not supported" in res_msg.message
        assert len(provider.queries) == 0

        # Web browsing operations
        res_web = assistant.process("search google for recipes")
        assert res_web.success is False
        assert "Web browsing and search operations are not supported" in res_web.message
        assert len(provider.queries) == 0

        # Media playback operations
        res_media = assistant.process("play music rock")
        assert res_media.success is False
        assert "Media playback operations are not supported" in res_media.message
        assert len(provider.queries) == 0

    def test_unknown_command_names_never_reach_ai(self, integrated_assistant):
        assistant, manager, provider = integrated_assistant

        for cmd in ["files.unknown_op foo bar", "system.reboot", "custom.action 1 2", "calc.add 1 2"]:
            res = assistant.process(cmd)
            assert res.success is False
            assert "Command not recognized" in res.message
            assert len(provider.queries) == 0, f"Query '{cmd}' unexpectedly reached the LLM"

    def test_malformed_command_syntax_never_reaches_ai(self, integrated_assistant):
        assistant, manager, provider = integrated_assistant

        malformed_inputs = [
            'files.read "unterminated quote',
            'demo.echo "unclosed string',
            "files.read 'unclosed single quote",
            'echo hello | grep h',
        ]
        for cmd in malformed_inputs:
            res = assistant.process(cmd)
            assert res.success is False
            assert len(provider.queries) == 0, f"Malformed input '{cmd}' unexpectedly reached the LLM"

    def test_incomplete_command_arguments_never_reach_ai(self, integrated_assistant):
        assistant, manager, provider = integrated_assistant

        # Exact command with missing required arguments
        res1 = assistant.process("files.read")
        assert res1.success is False
        assert "arguments are invalid" in res1.message
        assert len(provider.queries) == 0

        # Natural language command with missing required argument
        res2 = assistant.process("read file")
        assert res2.success is False
        assert "Which file would you like to read?" in res2.message
        assert len(provider.queries) == 0
        assert assistant.context.has_pending is True

    def test_clarification_responses_never_reach_ai(self, integrated_assistant):
        assistant, manager, provider = integrated_assistant

        # 1. Trigger pending clarification
        res_init = assistant.process("read file")
        assert res_init.success is False
        assert assistant.context.has_pending is True
        assert len(provider.queries) == 0

        # 2. Response with invalid argument (e.g. directory traversal attempt)
        res_invalid = assistant.process("../secret.txt")
        assert res_invalid.success is False
        assert len(provider.queries) == 0

        # Cancel the pending clarification
        res_cancel = assistant.process("cancel")
        assert res_cancel.success is False
        assert "Action cancelled" in res_cancel.message
        assert assistant.context.has_pending is False
        assert len(provider.queries) == 0

        # 3. Trigger clarification again and verify input during pending clarification does not leak to AI
        assistant.process("read file")
        assert assistant.context.has_pending is True
        res_other = assistant.process("What is the capital of Spain?")
        # Because a clarification is pending, input does not reach conversational AI
        assert len(provider.queries) == 0

    def test_conversational_questions_remain_eligible_for_ai(self, integrated_assistant):
        assistant, manager, provider = integrated_assistant

        res1 = assistant.process("What is the capital of France?")
        assert res1.success is True
        assert "AI answer to: What is the capital of France?" in res1.message

        res2 = assistant.process("Can you explain how photosynthesis works?")
        assert res2.success is True
        assert "AI answer to: Can you explain how photosynthesis works?" in res2.message

        assert len(provider.queries) == 2

    def test_empty_ai_response_falls_back_safely_to_deterministic_error(self, integrated_assistant):
        assistant, manager, provider = integrated_assistant

        # Override provider chat to return controlled empty response
        provider.chat = lambda messages, model, options=None: GenerationResult(
            success=False,
            content="",
            model=model,
            error_message="Model returned empty response content.",
        )

        res = assistant.process("How are you today?")
        assert res.success is False
        assert "Command not recognized" in res.message
        # History must not keep the unresponded user turn
        user_messages = [m for m in manager.history.get_messages() if m.role == "user"]
        assert len(user_messages) == 0
