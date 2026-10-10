# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

from dotenv import load_dotenv

from sweedit.core.api_pool import get_api_config
from sweedit.core.context_manager import ContextManager
from sweedit.core.llm import LLM
from sweedit.tools import Tool
from sweedit.utils import generate_session_id

load_dotenv()

# API-specific configuration
_API_CONFIG = {
    "ANTHROPIC": {
        "output_attr": "content",
        "tool_type": "tool_use",
        "tool_args_attr": "input",
        "tool_id_attr": "id",
    },
    "OPENAI": {
        "output_attr": "output",
        "tool_type": "function_call",
        "tool_args_attr": "arguments",
        "tool_id_attr": "call_id",
    },
}


class ToolCallProtocolError(RuntimeError):
    """Raised when a tool-driven agent repeatedly returns no native calls."""


class AgentBase:
    """Base agent class that coordinates LLM, Context, and tool execution."""

    def __init__(
        self,
        system: str | None = None,
        tools: list[Tool] | None = None,
        reasoning_effort: Literal["low", "medium", "high"] = "medium",
        max_iterations: int = 50,
        session_id: str | None = None,
        save_dir: str = ".traj/",
        enable_live_save: bool = True,
        rate_limit_delay: float = 0.0,
        enable_context_summarization: bool = False,
        summarization_threshold_tokens: int = 50000,
        summarization_keep_recent_messages: int = 50,
        tool_call_retry_attempts: int = 5,
    ):
        # Get random API config from pool
        api_config = get_api_config()
        self.api_type = api_config["API_TYPE"]
        self.use_responses_api = str(api_config.get("USE_RESPONSES_API", "true")).strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }

        # Store the appropriate API configuration
        api_key = "ANTHROPIC" if self.api_type == "ANTHROPIC" else "OPENAI"
        self._api_config = _API_CONFIG[api_key]

        tools = tools or []
        self._tool_handlers: dict[str, Tool] = {tool.name: tool for tool in tools}

        self.max_iterations = max_iterations
        self.rate_limit_delay = rate_limit_delay
        self.enable_context_summarization = enable_context_summarization
        self.summarization_threshold_tokens = summarization_threshold_tokens
        self.summarization_keep_recent_messages = summarization_keep_recent_messages
        if tool_call_retry_attempts < 1:
            raise ValueError("tool_call_retry_attempts must be at least 1")
        self.tool_call_retry_attempts = tool_call_retry_attempts
        self.session_id = session_id or generate_session_id()
        self.save_dir = Path(save_dir) / self.session_id

        self.llm = LLM(
            api_type=self.api_type,
            api_config=api_config,
            reasoning_effort=reasoning_effort,
            use_responses_api=self.use_responses_api,
        )
        self.context = ContextManager(
            system=system,
            tools=tools,
            api_type=self.api_type,
            use_responses_api=self.use_responses_api,
            enable_live_save=enable_live_save,
            save_dir=self.save_dir,
        )

    async def run(self, user_message: str) -> None:
        await self.context.add_user_message(user_message)

        for iteration in range(self.max_iterations):
            finish_called = False

            # Add optional delay between iterations to avoid rate limits
            if iteration > 0 and self.rate_limit_delay > 0:
                await asyncio.sleep(self.rate_limit_delay)

            llm_response, tool_calls = await self._call_with_tool_drift_recovery()
            await self.context.add_llm_response(llm_response, tool_calls=tool_calls)

            if tool_calls:
                # Execute all tools in parallel
                results = [await self._execute_tool(tc) for tc in tool_calls]

                # Normalize results in original order. Anthropic requires every
                # tool_result block for one assistant tool-use turn to appear in
                # one immediately-following user message; adding them one at a
                # time creates invalid message history when the model calls
                # multiple tools in one response.
                normalized_results = []
                for tool_call, result in zip(tool_calls, results, strict=False):
                    tool_id = getattr(tool_call, self._api_config["tool_id_attr"])

                    # Check if result is a dict with LLM editor metadata
                    if isinstance(result, dict):
                        if "llm_editor_user" in result:
                            await self.context.add_llm_editor_call(
                                system=result["llm_editor_system"],
                                user=result["llm_editor_user"],
                                assistant=result["llm_editor_assistant"],
                                original_content=result["original_content"],
                                token_stats=result["llm_editor_token_stats"],
                            )
                            tool_result = result["output"]
                        elif "llm_viewer_user" in result:
                            await self.context.add_llm_viewer_call(
                                system=result["llm_viewer_system"],
                                user=result["llm_viewer_user"],
                                assistant=result["llm_viewer_assistant"],
                                token_stats=result["llm_viewer_token_stats"],
                            )
                            tool_result = result["output"]
                        elif "output" in result:
                            # Handle error cases that return dict with only output
                            tool_result = result["output"]
                    else:
                        # Regular string result
                        tool_result = result

                    normalized_results.append((tool_id, tool_result))

                    # Check if finish tool was called
                    if tool_call.name == "finish":
                        finish_called = True

                if self.api_type == "ANTHROPIC":
                    await self.context.add_tool_results(normalized_results)
                else:
                    for tool_id, tool_result in normalized_results:
                        await self.context.add_tool_result(tool_id, tool_result)
            else:
                # No tool calls made, inject fake user message to continue
                await self.context.add_user_message(
                    "If you have completed the task, please use the finish tool. Otherwise, continue with your work."
                )

            if finish_called:
                break

            # Compact the context once it grows past the configured threshold so
            # the next iteration stays within the model's context window.
            if self.enable_context_summarization:
                await self.context.maybe_summarize(
                    self.llm,
                    threshold_tokens=self.summarization_threshold_tokens,
                    keep_recent_messages=self.summarization_keep_recent_messages,
                )

    async def _call_with_tool_drift_recovery(self):
        """Retry protocol-drifted Chat Completions responses off-history.

        Some OpenAI-compatible gateways occasionally return a textual rendering
        of a tool call in ``message.content`` while leaving ``tool_calls`` empty.
        Adding that response to the conversation makes the model imitate the bad
        protocol on later turns. Retry the identical clean context first, and
        retain rejected responses only in the trajectory/usage accounting.
        """
        prepared_input = self.context.get_messages()
        recovery_enabled = self._tool_drift_recovery_enabled()
        attempts = self.tool_call_retry_attempts if recovery_enabled else 1

        for attempt in range(1, attempts + 1):
            response = await self.llm.call(prepared_input=prepared_input)
            tool_calls = self._extract_tool_calls(response)
            if tool_calls or not recovery_enabled:
                return response, tool_calls

            await self.context.add_discarded_llm_response(
                response,
                reason="missing_native_tool_call",
                retry_attempt=attempt,
            )

        raise ToolCallProtocolError(
            f"No native tool call after {attempts} attempts; "
            "all protocol-drifted responses were kept out of conversation history"
        )

    def _tool_drift_recovery_enabled(self) -> bool:
        """Limit this workaround to the affected OpenAI Chat protocol."""
        return bool(
            self._tool_handlers
            and self.api_type == "OPENAI"
            and not self.use_responses_api
            and self.tool_call_retry_attempts > 1
        )

    def _extract_tool_calls(self, response) -> list:
        """Return a common tool-call view for Responses and Chat Completions."""
        if self.api_type == "OPENAI" and not self.use_responses_api:
            message = response.choices[0].message
            return [
                SimpleNamespace(
                    type="function_call",
                    name=call.function.name,
                    arguments=call.function.arguments,
                    call_id=call.id,
                )
                for call in (message.tool_calls or [])
            ]

        outputs = getattr(response, self._api_config["output_attr"])
        return [output for output in outputs if output.type in ("function_call", "tool_use")]

    async def _execute_tool(self, tool_call: str) -> str:
        """Execute tool and return result (for parallel execution)."""
        tool_name = tool_call.name
        tool_args = getattr(tool_call, self._api_config["tool_args_attr"])

        if tool_name not in self._tool_handlers:
            return f"Error: Tool '{tool_name}' not found"
        else:
            return await self._tool_handlers[tool_name](tool_args)

    def reset(self) -> None:
        """Reset the agent's context and statistics."""
        self.context.clear()

    def get_model(self) -> str:
        """Get the model used by the agent."""
        return self.llm.model

    def set_save_dir(self, save_dir: str) -> None:
        """Set the save directory for the agent."""
        self.save_dir = Path(save_dir)
        self.context.set_save_dir(self.save_dir)

    def set_workdir(self, workdir: str) -> None:
        """Set the work directory for the agent."""
        for tool in self._tool_handlers.values():
            if hasattr(tool, "set_workdir"):
                tool.set_workdir(workdir)

    def save_traj(self) -> None:
        """Save agent trajectory to disk."""
        self.context.save(self.save_dir / self.session_id)

    def get_tool_call_stats(self) -> dict[str, int]:
        """Get tool call statistics."""
        return self.context.get_tool_call_stats()

    def get_token_stats(self) -> dict:
        """Get token consumption statistics."""
        return self.context.get_token_stats()
