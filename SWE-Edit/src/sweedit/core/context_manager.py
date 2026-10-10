# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import contextlib
import json
from pathlib import Path
from typing import Any, Literal

import aiofiles

from sweedit.core.types import AgentInput, AgentResponse
from sweedit.tools import Tool

# Tools that have sub-commands (command parameter)
TOOLS_WITH_COMMANDS = {
    "file_editor",
    "str_replace_editor",
    "str_replace_editor_v2",
    "llm_file_editor",
    "llm_editor",
}

# ---------------------------------------------------------------------------
# Context summarization prompts
# ---------------------------------------------------------------------------

SUMMARIZATION_SYSTEM_PROMPT = (
    "You are a meticulous context-summarization assistant for an autonomous "
    "software-engineering agent. The agent is solving a coding task and its "
    "conversation has grown too long to keep in full. Your job is to compress the "
    "earlier part of the conversation into a dense, factual summary that lets the "
    "agent continue working without re-reading the original messages.\n\n"
    "Preserve everything that is operationally important and discard chatter. "
    "Produce a structured summary with these sections (omit a section only if it "
    "is genuinely empty):\n"
    "1. TASK: the problem statement / goal the agent is working toward.\n"
    "2. REPO STATE: relevant files, directories, and code locations discovered "
    "(with paths and key symbols/line references when known).\n"
    "3. CHANGES MADE: edits already applied to the codebase, described precisely "
    "enough to reconstruct what was done.\n"
    "4. COMMANDS & RESULTS: important shell/test commands run and their outcomes "
    "(failures, tracebacks, test pass/fail).\n"
    "5. CURRENT STATE & NEXT STEPS: where things stand and what remains to be done.\n"
    "6. KEY FACTS: any other constraints, hypotheses, or observations worth keeping.\n\n"
    "Be specific and concrete — keep exact file paths, function names, error "
    "messages, and command strings. Do not invent information. Do not ask "
    "questions or address the user; output only the summary."
)

SUMMARIZATION_USER_TEMPLATE = (
    "Summarize the following earlier portion of the agent's conversation so the "
    "agent can continue the task. This transcript is the prefix that will be "
    "replaced by your summary; the most recent messages are kept verbatim and are "
    "NOT shown here.\n\n"
    "<transcript>\n{transcript}\n</transcript>"
)

SUMMARY_WRAPPER = (
    "[CONVERSATION SUMMARY]\n"
    "To stay within the context limit, the earlier part of this conversation was "
    "condensed into the summary below. Treat it as an accurate record of "
    "everything that happened before the recent messages that follow.\n\n"
    "{summary}\n\n"
    "[END OF SUMMARY — continue the task using this summary plus the recent "
    "messages below.]"
)


class ContextManager:
    """Manages conversation context, token usage, and tool call statistics."""

    def __init__(
        self,
        system: str | None = None,
        tools: list[Tool] | None = None,
        api_type: Literal["OPENAI", "AZURE_OPENAI", "ANTHROPIC"] = "OPENAI",
        use_responses_api: bool = True,
        enable_live_save: bool = True,
        save_dir: Path | None = None,
    ):
        if enable_live_save and save_dir is None:
            raise ValueError("save_dir must be provided if enable_live_save is True")

        self._system = system
        self._tools = tools or []
        self._api_type = api_type
        self._use_responses_api = use_responses_api
        self._messages: list[dict[str, Any]] = []

        # Initialize tool call counts - nested dict for tools with commands, int for others
        self._tool_call_counts: dict[str, int | dict[str, int]] = {}
        for tool in self._tools:
            if tool.name in TOOLS_WITH_COMMANDS:
                self._tool_call_counts[tool.name] = {}
            else:
                self._tool_call_counts[tool.name] = 0

        # Token tracking
        self._token_stats = {
            "total_noncached_input_tokens": 0,
            "total_cached_input_tokens": 0,
            "total_output_tokens": 0,  # Total decode work (sum all rounds)
            "total_reasoning_tokens": 0,  # Sum of all reasoning tokens (subset of output)
            "peak_context_size": 0,  # Maximum context size seen (capacity limit / memory bound)
            "num_rounds": 0,  # Number of LLM API calls (iteration complexity)
            "num_tool_call_retries": 0,  # Off-history retries after protocol drift
            "num_summarizations": 0,  # Number of times the context was summarized
        }

        # Most recent context size (input tokens sent on the last LLM call). Used
        # to decide when to trigger context summarization.
        self._last_context_size = 0

        # Context summarizer token tracking (separate from main agent)
        self._summarizer_token_stats = {
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "num_calls": 0,
        }

        # LLM editor token tracking (separate from main agent)
        self._llm_editor_token_stats = {
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "num_calls": 0,
        }

        # LLM viewer token tracking (separate from main agent)
        self._llm_viewer_token_stats = {
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "num_calls": 0,
        }

        # Live save configuration
        self._enable_live_save = enable_live_save
        self.set_save_dir(save_dir)

    async def add_user_message(self, user_message: str) -> None:
        if self._api_type == "ANTHROPIC" or (
            self._api_type == "OPENAI" and not self._use_responses_api
        ):
            message = {"role": "user", "content": user_message}
        else:
            message = {"role": "user", "content": user_message, "type": "message"}

        self._messages.append(message)
        await self._update_trajectory_async(message)

    async def add_llm_response(self, response: AgentResponse, tool_calls: list | None = None) -> None:
        if self._api_type == "ANTHROPIC":
            # Always save full response to trajectory
            full_content = [content.model_dump() for content in response.content]
            self._track_tool_calls(response.content if tool_calls is None else tool_calls)
            await self._update_trajectory_async({"role": "assistant", "content": full_content})

            self._messages.append({"role": "assistant", "content": full_content})
        elif self._api_type == "OPENAI" and not self._use_responses_api:
            message = response.choices[0].message.model_dump(exclude_none=True)
            self._track_tool_calls(tool_calls or [])
            self._messages.append(message)
            await self._update_trajectory_async(message)
        else:
            for output in response.output:
                message = output.model_dump(exclude={"status"})
                await self._update_trajectory_async(message)
                self._messages.append(message)

            self._track_tool_calls(response.output if tool_calls is None else tool_calls)

        # Track token usage from response
        self._track_token_usage(response)

        await self._update_tool_stats_async()
        await self._update_token_stats_async()

    async def add_discarded_llm_response(
        self,
        response: AgentResponse,
        *,
        reason: str,
        retry_attempt: int,
    ) -> None:
        """Audit an off-history response rejected by protocol recovery.

        The response is intentionally not appended to ``_messages``. This keeps
        malformed textual tool syntax from teaching the model to repeat the same
        non-native protocol on the next attempt.
        """
        if self._api_type == "OPENAI" and not self._use_responses_api:
            message = response.choices[0].message.model_dump(exclude_none=True)
        elif self._api_type == "ANTHROPIC":
            message = {"content": [content.model_dump() for content in response.content]}
        else:
            message = {"output": [output.model_dump(exclude={"status"}) for output in response.output]}

        await self._update_tool_call_drift_async(
            {
                **message,
                "role": "assistant_tool_call_drift",
                "discarded": True,
                "reason": reason,
                "retry_attempt": retry_attempt,
            }
        )
        self._token_stats["num_tool_call_retries"] += 1
        self._track_token_usage(response)
        await self._update_token_stats_async()

    async def add_tool_result(self, tool_call_id: str, tool_call_result: str) -> None:
        await self.add_tool_results([(tool_call_id, tool_call_result)])

    async def add_tool_results(self, results: list[tuple[str, str]]) -> None:
        """Add one assistant turn's tool results in provider-valid form."""
        if not results:
            return
        if self._api_type == "ANTHROPIC":
            message = {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_call_id,
                        "content": tool_call_result,
                    }
                    for tool_call_id, tool_call_result in results
                ],
            }
        elif self._api_type == "OPENAI" and not self._use_responses_api:
            for tool_call_id, tool_call_result in results:
                message = {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": tool_call_result,
                }
                self._messages.append(message)
                await self._update_trajectory_async(message)
            return
        else:
            for tool_call_id, tool_call_result in results:
                message = {
                    "call_id": tool_call_id,
                    "output": tool_call_result,
                    "type": "function_call_output",
                }
                self._messages.append(message)
                await self._update_trajectory_async(message)
            return

        self._messages.append(message)
        await self._update_trajectory_async(message)

    async def add_llm_editor_call(
        self, system: str, user: str, assistant: str, original_content: str, token_stats: dict[str, int]
    ) -> None:
        """Track LLM editor call input, response, and token usage."""
        # Update LLM editor token stats
        self._llm_editor_token_stats["total_input_tokens"] += token_stats.get("input_tokens") or 0
        self._llm_editor_token_stats["total_output_tokens"] += token_stats.get("output_tokens") or 0
        self._llm_editor_token_stats["num_calls"] += 1

        # Save trajectory and token stats
        await self._update_llm_editor_trajectory_async(system, user, assistant, original_content=original_content)
        await self._update_llm_editor_token_stats_async()

    async def add_llm_viewer_call(self, system: str, user: str, assistant: str, token_stats: dict[str, int]) -> None:
        """Track LLM viewer call input, response, and token usage."""
        # Update LLM viewer token stats
        self._llm_viewer_token_stats["total_input_tokens"] += token_stats.get("input_tokens") or 0
        self._llm_viewer_token_stats["total_output_tokens"] += token_stats.get("output_tokens") or 0
        self._llm_viewer_token_stats["num_calls"] += 1

        # Save trajectory and token stats
        await self._update_llm_viewer_trajectory_async(system, user, assistant)
        await self._update_llm_viewer_token_stats_async()

    def get_messages(self) -> AgentInput:
        tool_schemas = None
        if self._tools and self._api_type:
            tool_schemas = [
                tool.schema(api_type=self._api_type, use_responses_api=self._use_responses_api)
                for tool in self._tools
            ]

        return AgentInput(messages=self._messages, system=self._system, tools=tool_schemas)

    def get_tool_call_stats(self) -> dict[str, int | dict[str, int]]:
        return self._tool_call_counts

    def get_token_stats(self) -> dict[str, Any]:
        """Get token consumption statistics."""
        return {
            "agent": self._token_stats,
            "llm_editor": self._llm_editor_token_stats,
            "llm_viewer": self._llm_viewer_token_stats,
            "summarizer": self._summarizer_token_stats,
        }

    @property
    def last_context_size(self) -> int:
        """Input tokens sent on the most recent LLM call (0 if unknown)."""
        return self._last_context_size

    def _track_tool_calls(self, output: list[AgentResponse]) -> None:
        for item in output:
            if item.type in ("function_call", "tool_use"):
                tool_name = item.name

                # Check if this tool has sub-commands
                if tool_name in TOOLS_WITH_COMMANDS:
                    # Extract command from arguments
                    command = self._extract_command(item)
                    if command:
                        # Initialize command count if not present
                        if tool_name not in self._tool_call_counts:
                            self._tool_call_counts[tool_name] = {}
                        if command not in self._tool_call_counts[tool_name]:
                            self._tool_call_counts[tool_name][command] = 0
                        self._tool_call_counts[tool_name][command] += 1
                else:
                    # Simple count for tools without commands
                    if tool_name not in self._tool_call_counts:
                        self._tool_call_counts[tool_name] = 0
                    self._tool_call_counts[tool_name] += 1

    def _extract_command(self, item) -> str | None:
        """Extract the command parameter from tool arguments."""
        try:
            # Handle different API formats
            if hasattr(item, "arguments"):
                # OpenAI format - arguments is a JSON string
                args = json.loads(item.arguments) if isinstance(item.arguments, str) else item.arguments
            elif hasattr(item, "input"):
                # Anthropic format - input is already a dict
                args = item.input
            else:
                return None

            return args.get("command")
        except (json.JSONDecodeError, AttributeError, TypeError):
            return None

    def _track_token_usage(self, response: AgentResponse) -> None:
        """Track token usage from LLM response."""
        if not hasattr(response, "usage") or response.usage is None:
            return

        usage = response.usage

        # Responses/Anthropic use input_tokens/output_tokens; Chat Completions
        # uses prompt_tokens/completion_tokens.
        input_tokens = self._usage_value(usage, "input_tokens", "prompt_tokens")
        output_tokens = self._usage_value(usage, "output_tokens", "completion_tokens")

        # Reasoning tokens are part of output tokens
        reasoning_tokens = 0
        if hasattr(usage, "output_tokens_details") and usage.output_tokens_details:
            reasoning_tokens = self._usage_value(usage.output_tokens_details, "reasoning_tokens")
        elif hasattr(usage, "completion_tokens_details") and usage.completion_tokens_details:
            reasoning_tokens = self._usage_value(usage.completion_tokens_details, "reasoning_tokens")

        # Cached input tokens are part of input tokens
        cached_input_tokens = 0
        if hasattr(usage, "input_tokens_details") and usage.input_tokens_details:
            cached_input_tokens = self._usage_value(usage.input_tokens_details, "cached_tokens")
        elif hasattr(usage, "prompt_tokens_details") and usage.prompt_tokens_details:
            cached_input_tokens = self._usage_value(usage.prompt_tokens_details, "cached_tokens")

        self._token_stats["total_noncached_input_tokens"] += input_tokens - cached_input_tokens
        self._token_stats["total_cached_input_tokens"] += cached_input_tokens
        self._token_stats["total_output_tokens"] += output_tokens
        self._token_stats["total_reasoning_tokens"] += reasoning_tokens
        self._token_stats["peak_context_size"] = max(self._token_stats["peak_context_size"], input_tokens)
        self._token_stats["num_rounds"] += 1
        self._last_context_size = input_tokens

    @staticmethod
    def _usage_value(usage: Any, *names: str) -> int:
        for name in names:
            value = getattr(usage, name, None)
            if value is not None:
                return int(value or 0)
        return 0

    def clear(self) -> None:
        self._messages = []
        # Reset tool call counts with proper structure
        self._tool_call_counts = {}
        for tool in self._tools:
            if tool.name in TOOLS_WITH_COMMANDS:
                self._tool_call_counts[tool.name] = {}
            else:
                self._tool_call_counts[tool.name] = 0

        self._token_stats = {
            "total_noncached_input_tokens": 0,
            "total_cached_input_tokens": 0,
            "total_output_tokens": 0,
            "total_reasoning_tokens": 0,
            "num_rounds": 0,
            "peak_context_size": 0,
            "num_tool_call_retries": 0,
            "num_summarizations": 0,
        }
        self._last_context_size = 0
        self._llm_editor_token_stats = {
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "num_calls": 0,
        }
        self._llm_viewer_token_stats = {
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "num_calls": 0,
        }
        self._summarizer_token_stats = {
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "num_calls": 0,
        }

    def set_save_dir(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        self._save_dir = path
        self._traj_path = self._save_dir / "traj.jsonl"
        self._tools_path = self._save_dir / "tools.jsonl"
        self._tool_call_stats_path = self._save_dir / "tool_call_stats.json"
        self._token_stats_path = self._save_dir / "token_stats.json"
        self._tool_call_drift_path = self._save_dir / "tool_call_drift.jsonl"
        self._llm_editor_traj_path = self._save_dir / "llm_editor_traj.jsonl"
        self._llm_editor_token_stats_path = self._save_dir / "llm_editor_token_stats.json"
        self._llm_viewer_traj_path = self._save_dir / "llm_viewer_traj.jsonl"
        self._llm_viewer_token_stats_path = self._save_dir / "llm_viewer_token_stats.json"
        self._summarizer_traj_path = self._save_dir / "summarizer_traj.jsonl"
        self._summarizer_token_stats_path = self._save_dir / "summarizer_token_stats.json"

        if self._enable_live_save:
            self._initialize_live_save()

    def save(self) -> None:
        with open(self._traj_path, "w", encoding="utf-8") as f:
            if self._system:
                system_msg = {"role": "system", "content": self._system}
                f.write(json.dumps(system_msg, ensure_ascii=False) + "\n")

            for message in self._messages:
                msg = self._serialize_message(message)

                for m in msg:
                    f.write(json.dumps(m, ensure_ascii=False) + "\n")

        with open(self._tools_path, "w", encoding="utf-8") as f:
            for tool in self._tools:
                tool_schema = tool.schema(api_type=self._api_type, use_responses_api=self._use_responses_api)
                f.write(json.dumps(tool_schema, ensure_ascii=False, indent=2) + "\n")

        with open(self._tool_call_stats_path, "w", encoding="utf-8") as f:
            json.dump(self._tool_call_counts, f, ensure_ascii=False, indent=2)

        with open(self._token_stats_path, "w", encoding="utf-8") as f:
            json.dump(self._token_stats, f, ensure_ascii=False, indent=2)

        with open(self._llm_editor_token_stats_path, "w", encoding="utf-8") as f:
            json.dump(self._llm_editor_token_stats, f, ensure_ascii=False, indent=2)

        with open(self._llm_viewer_token_stats_path, "w", encoding="utf-8") as f:
            json.dump(self._llm_viewer_token_stats, f, ensure_ascii=False, indent=2)

        with open(self._summarizer_token_stats_path, "w", encoding="utf-8") as f:
            json.dump(self._summarizer_token_stats, f, ensure_ascii=False, indent=2)

    def _initialize_live_save(self) -> None:
        """Initialize live save by creating directory and writing system/tools."""
        # Write system message if present
        if self._system:
            system_msg = {"role": "system", "content": self._system}
            with open(self._traj_path, "w", encoding="utf-8") as f:
                f.write(json.dumps(system_msg, ensure_ascii=False) + "\n")

        # Write tool schemas
        with open(self._tools_path, "w", encoding="utf-8") as f:
            for tool in self._tools:
                tool_schema = tool.schema(api_type=self._api_type, use_responses_api=self._use_responses_api)
                f.write(json.dumps(tool_schema, ensure_ascii=False, indent=2) + "\n")

    async def _update_trajectory_async(self, message: dict[str, Any]) -> None:
        """Update the trajectory file with a new message."""
        if not self._enable_live_save or not self._save_dir:
            return

        serialized_messages = self._serialize_message(message)

        async with aiofiles.open(self._traj_path, "a", encoding="utf-8") as f:
            for msg in serialized_messages:
                await f.write(json.dumps(msg, ensure_ascii=False) + "\n")

    async def _update_tool_call_drift_async(self, entry: dict[str, Any]) -> None:
        """Append a rejected response without mixing it into replayable history."""
        if not self._enable_live_save or not self._save_dir:
            return

        async with aiofiles.open(self._tool_call_drift_path, "a", encoding="utf-8") as f:
            await f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    async def _update_tool_stats_async(self) -> None:
        """Update the tool call statistics file."""
        if not self._enable_live_save or not self._save_dir:
            return

        async with aiofiles.open(self._tool_call_stats_path, "w", encoding="utf-8") as f:
            await f.write(json.dumps(self._tool_call_counts, ensure_ascii=False, indent=2))

    async def _update_token_stats_async(self) -> None:
        """Update the token statistics file."""
        if not self._enable_live_save or not self._save_dir:
            return

        async with aiofiles.open(self._token_stats_path, "w", encoding="utf-8") as f:
            await f.write(json.dumps(self._token_stats, ensure_ascii=False, indent=2))

    async def _update_llm_editor_trajectory_async(
        self, system: str, user: str, assistant: str, original_content: str
    ) -> None:
        """Update the LLM editor trajectory file with input and response."""
        if not self._enable_live_save or not self._save_dir:
            return

        # Serialize the input and response in simplified format
        trajectory_entry = {
            "system": system,
            "user": user,
            "assistant": assistant,
            "original_content": original_content,
        }

        async with aiofiles.open(self._llm_editor_traj_path, "a", encoding="utf-8") as f:
            await f.write(json.dumps(trajectory_entry, ensure_ascii=False) + "\n")

    async def _update_llm_editor_token_stats_async(self) -> None:
        """Update the LLM editor token statistics file."""
        if not self._enable_live_save or not self._save_dir:
            return

        async with aiofiles.open(self._llm_editor_token_stats_path, "w", encoding="utf-8") as f:
            await f.write(json.dumps(self._llm_editor_token_stats, ensure_ascii=False, indent=2))

    async def _update_llm_viewer_trajectory_async(self, system: str, user: str, assistant: str) -> None:
        """Update the LLM viewer trajectory file with input and response."""
        if not self._enable_live_save or not self._save_dir:
            return

        # Serialize the input and response in simplified format
        trajectory_entry = {"system": system, "user": user, "assistant": assistant}

        async with aiofiles.open(self._llm_viewer_traj_path, "a", encoding="utf-8") as f:
            await f.write(json.dumps(trajectory_entry, ensure_ascii=False) + "\n")

    async def _update_llm_viewer_token_stats_async(self) -> None:
        """Update the LLM viewer token statistics file."""
        if not self._enable_live_save or not self._save_dir:
            return

        async with aiofiles.open(self._llm_viewer_token_stats_path, "w", encoding="utf-8") as f:
            await f.write(json.dumps(self._llm_viewer_token_stats, ensure_ascii=False, indent=2))

    # ------------------------------------------------------------------
    # Context summarization
    # ------------------------------------------------------------------

    async def maybe_summarize(self, llm, threshold_tokens: int, keep_recent_messages: int) -> bool:
        """Compress old messages into a summary when the context grows too large.

        Triggers when the most recent context size exceeds ``threshold_tokens``.
        The oldest messages are condensed via ``llm`` into a single summary
        message, while the ``keep_recent_messages`` most recent messages are kept
        verbatim. The on-disk trajectory is left intact — only the in-memory
        context sent to the model is compacted. Returns True if it summarized.
        """
        if threshold_tokens <= 0 or self._last_context_size <= threshold_tokens:
            return False

        split = self._find_clean_split(len(self._messages) - keep_recent_messages)
        # Need a non-trivial prefix to condense, and at least one kept message,
        # otherwise summarizing buys nothing.
        if split < 2 or split >= len(self._messages):
            return False

        prefix = self._messages[:split]
        transcript = self._render_transcript(prefix)

        agent_input = AgentInput(
            messages=[self._build_summary_request_message(transcript)],
            system=SUMMARIZATION_SYSTEM_PROMPT,
            tools=None,
        )

        try:
            response = await llm.call(prepared_input=agent_input)
        except Exception:  # noqa: BLE001 — summarization is best-effort; never crash the run
            return False

        summary_text = self._extract_response_text(response)
        if not summary_text:
            return False

        self._record_summarizer_usage(response)

        # Replace the summarized prefix with a single summary message.
        self._messages = [self._build_summary_context_message(summary_text)] + self._messages[split:]
        self._token_stats["num_summarizations"] += 1
        # Force a fresh measurement; the next round will record the new size.
        self._last_context_size = 0

        await self._update_summarizer_trajectory_async(transcript, summary_text, split)
        await self._update_summarizer_token_stats_async()
        await self._update_token_stats_async()
        return True

    def _is_clean_boundary(self, i: int) -> bool:
        """Whether index ``i`` is a safe place to begin the kept message window.

        A clean boundary is the start of a fresh turn, so summarizing
        ``messages[:i]`` never (a) leaves a tool result whose tool call was
        condensed away — an orphaned ``tool_result`` / ``function_call_output``
        that the API rejects — nor (b) splits an assistant turn between its
        reasoning and the call it produced.
        """
        msgs = self._messages
        cur = msgs[i]
        if self._api_type == "ANTHROPIC":
            # The injected summary is a user message; the kept head must start an
            # assistant turn (both avoids a dangling tool_result and avoids two
            # consecutive user messages).
            return cur.get("role") == "assistant"

        # OpenAI/Azure Responses API: the kept head must begin a user turn or a
        # model turn that follows a completed tool round.
        if cur.get("type") == "function_call_output":
            return False
        if cur.get("type") == "message" and cur.get("role") == "user":
            return True
        prev = msgs[i - 1] if i > 0 else None
        return prev is not None and prev.get("type") == "function_call_output"

    def _find_clean_split(self, candidate: int) -> int:
        """Advance ``candidate`` forward to a clean turn boundary."""
        n = len(self._messages)
        split = max(0, min(candidate, n))
        while split < n and not self._is_clean_boundary(split):
            split += 1
        return split

    def _render_transcript(self, messages: list[dict[str, Any]]) -> str:
        """Flatten messages into a readable transcript for summarization."""
        lines: list[str] = []
        for message in messages:
            for m in self._serialize_message(message):
                role = m.get("role", "unknown")
                if role == "function_call":
                    lines.append(f"[TOOL CALL: {m.get('name')}]\n{m.get('arguments')}")
                elif role == "function_call_output":
                    lines.append(f"[TOOL RESULT]\n{m.get('output')}")
                elif role == "reasoning":
                    lines.append(f"[REASONING]\n{m.get('content')}")
                else:
                    lines.append(f"[{role.upper()}]\n{m.get('content')}")
        return "\n\n".join(lines)

    def _build_summary_request_message(self, transcript: str) -> dict[str, Any]:
        content = SUMMARIZATION_USER_TEMPLATE.format(transcript=transcript)
        if self._api_type == "ANTHROPIC":
            return {"role": "user", "content": content}
        return {"role": "user", "content": content, "type": "message"}

    def _build_summary_context_message(self, summary_text: str) -> dict[str, Any]:
        content = SUMMARY_WRAPPER.format(summary=summary_text)
        if self._api_type == "ANTHROPIC":
            return {"role": "user", "content": content}
        return {"role": "user", "content": content, "type": "message"}

    def _extract_response_text(self, response: AgentResponse) -> str:
        """Extract the assistant's plain text from a summarization response."""
        if self._api_type == "ANTHROPIC":
            parts = [
                getattr(block, "text", "")
                for block in (getattr(response, "content", None) or [])
                if getattr(block, "type", None) == "text"
            ]
            return "\n".join(p for p in parts if p).strip()

        # OpenAI/Azure Responses API exposes an aggregated convenience field.
        text = getattr(response, "output_text", None)
        if text:
            return text.strip()
        parts = []
        for item in getattr(response, "output", None) or []:
            if getattr(item, "type", None) == "message":
                for block in getattr(item, "content", None) or []:
                    if getattr(block, "type", None) in ("output_text", "text"):
                        parts.append(getattr(block, "text", ""))
        return "\n".join(p for p in parts if p).strip()

    def _record_summarizer_usage(self, response: AgentResponse) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        input_tokens = getattr(usage, "input_tokens", None)
        if input_tokens is None:
            input_tokens = getattr(usage, "prompt_tokens", 0)
        output_tokens = getattr(usage, "output_tokens", None)
        if output_tokens is None:
            output_tokens = getattr(usage, "completion_tokens", 0)
        self._summarizer_token_stats["total_input_tokens"] += input_tokens or 0
        self._summarizer_token_stats["total_output_tokens"] += output_tokens or 0
        self._summarizer_token_stats["num_calls"] += 1

    async def _update_summarizer_trajectory_async(
        self, transcript: str, summary: str, num_messages_summarized: int
    ) -> None:
        """Record each summarization event for later inspection."""
        if not self._enable_live_save or not self._save_dir:
            return

        entry = {
            "num_messages_summarized": num_messages_summarized,
            "summary": summary,
            "transcript": transcript,
        }
        async with aiofiles.open(self._summarizer_traj_path, "a", encoding="utf-8") as f:
            await f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    async def _update_summarizer_token_stats_async(self) -> None:
        if not self._enable_live_save or not self._save_dir:
            return

        async with aiofiles.open(self._summarizer_token_stats_path, "w", encoding="utf-8") as f:
            await f.write(json.dumps(self._summarizer_token_stats, ensure_ascii=False, indent=2))

    def _serialize_message(self, message: dict[str, Any]) -> list[dict]:
        if self._api_type == "ANTHROPIC":
            # Anthropic Messages API
            if message["role"] == "user":
                if isinstance(message["content"], list):
                    return [
                        {
                            "role": "function_call_output",
                            "output": block["content"],
                            "tool_use_id": block["tool_use_id"],
                        }
                        for block in message["content"]
                    ]
                else:
                    return [{"role": "user", "content": message["content"]}]
            else:
                serialized_messages = []
                content_list = message["content"]

                for block in content_list:
                    block_type = block.get("type")

                    if block_type == "text":
                        serialized_messages.append({"role": "assistant", "content": block["text"]})
                    elif block_type == "thinking":
                        serialized_messages.append({"role": "reasoning", "content": block["thinking"]})
                    elif block_type == "tool_use":
                        serialized_messages.append(
                            {
                                "role": "function_call",
                                "name": block["name"],
                                "arguments": block["input"],
                            }
                        )

                return serialized_messages
        elif self._api_type == "OPENAI" and not self._use_responses_api:
            if message.get("role") == "user":
                return [{"role": "user", "content": message.get("content", "")}]
            if message.get("role") == "tool":
                return [
                    {
                        "role": "function_call_output",
                        "output": message.get("content", ""),
                        "tool_use_id": message.get("tool_call_id"),
                    }
                ]
            if message.get("role") == "assistant":
                serialized_messages = []
                if message.get("content"):
                    serialized_messages.append({"role": "assistant", "content": message["content"]})
                for call in message.get("tool_calls") or []:
                    function = call.get("function", {})
                    arguments = function.get("arguments", "")
                    with contextlib.suppress(TypeError, json.JSONDecodeError):
                        arguments = json.loads(arguments)
                    serialized_messages.append(
                        {
                            "role": "function_call",
                            "name": function.get("name"),
                            "arguments": arguments,
                            "tool_use_id": call.get("id"),
                        }
                    )
                return serialized_messages
            return []
        else:
            # OpenAI/Azure OpenAI Responses API
            if message["type"] == "message":
                content = message["content"][0]["text"] if isinstance(message["content"], list) else message["content"]
                return [{"role": message["role"], "content": content}]
            elif message["type"] == "reasoning":
                return [{"role": "reasoning", "content": message["content"]}]
            elif message["type"] == "function_call":
                return [
                    {
                        "role": "function_call",
                        "name": message["name"],
                        "arguments": message["arguments"],
                    }
                ]
            elif message["type"] == "function_call_output":
                return [
                    {
                        "role": "function_call_output",
                        "output": message["output"],
                    }
                ]
            else:
                raise ValueError(f"Unknown message type: {message['type']}")
