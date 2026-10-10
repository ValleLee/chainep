from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

with patch("importlib.metadata.version", return_value="1.0.0"):
    from sweedit.core.agent import AgentBase


def response_with_calls(*calls):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=list(calls)))],
    )


class ToolDriftRecoveryTest(IsolatedAsyncioTestCase):
    def make_agent(self, responses, *, attempts=5, enabled=True):
        agent = AgentBase.__new__(AgentBase)
        agent.api_type = "OPENAI"
        agent.use_responses_api = not enabled
        agent.tool_call_retry_attempts = attempts
        agent._tool_handlers = {"run_shell": object()}
        agent._api_config = {
            "output_attr": "output",
            "tool_type": "function_call",
            "tool_args_attr": "arguments",
            "tool_id_attr": "call_id",
        }
        prepared_input = {"messages": [{"role": "user", "content": "task"}]}
        agent.context = SimpleNamespace(
            get_messages=lambda: prepared_input,
            add_discarded_llm_response=AsyncMock(),
        )
        agent.llm = SimpleNamespace(call=AsyncMock(side_effect=responses))
        return agent, prepared_input

    async def test_discards_drift_off_history_until_native_call(self):
        native_call = SimpleNamespace(
            id="call-1",
            function=SimpleNamespace(name="run_shell", arguments='{"command":"pwd"}'),
        )
        bad_1 = response_with_calls()
        bad_2 = response_with_calls()
        good = response_with_calls(native_call)
        agent, prepared_input = self.make_agent([bad_1, bad_2, good])

        response, calls = await agent._call_with_tool_drift_recovery()

        self.assertIs(response, good)
        self.assertEqual([call.name for call in calls], ["run_shell"])
        self.assertEqual(agent.llm.call.await_count, 3)
        self.assertTrue(
            all(call.kwargs["prepared_input"] is prepared_input for call in agent.llm.call.await_args_list)
        )
        self.assertEqual(agent.context.add_discarded_llm_response.await_count, 2)
        self.assertEqual(
            [call.kwargs["retry_attempt"] for call in agent.context.add_discarded_llm_response.await_args_list],
            [1, 2],
        )

    async def test_fails_cleanly_after_retry_budget_is_exhausted(self):
        responses = [response_with_calls() for _ in range(5)]
        agent, _ = self.make_agent(responses)

        with self.assertRaisesRegex(RuntimeError, "No native tool call after 5 attempts"):
            await agent._call_with_tool_drift_recovery()

        self.assertEqual(agent.llm.call.await_count, 5)
        self.assertEqual(agent.context.add_discarded_llm_response.await_count, 5)

    async def test_non_chat_protocol_is_not_retried(self):
        response = SimpleNamespace(output=[])
        agent, _ = self.make_agent([response], enabled=False)

        returned, calls = await agent._call_with_tool_drift_recovery()

        self.assertIs(returned, response)
        self.assertEqual(calls, [])
        self.assertEqual(agent.llm.call.await_count, 1)
        agent.context.add_discarded_llm_response.assert_not_awaited()
