# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Minimal agent"""

from sweedit.core.agent import AgentBase
from sweedit.core.prompts import MINIMAL
from sweedit.tools import ExecuteBash, Finish, StrReplaceEditor


class MinAgent(AgentBase):
    def __init__(
        self,
        session_id: str | None = None,
        save_dir: str = ".traj/",
        enable_context_summarization: bool = False,
        summarization_threshold_tokens: int = 50000,
        summarization_keep_recent_messages: int = 50,
    ):
        super().__init__(
            system=MINIMAL,
            tools=[ExecuteBash(), StrReplaceEditor(), Finish()],
            session_id=session_id,
            save_dir=save_dir,
            enable_context_summarization=enable_context_summarization,
            summarization_threshold_tokens=summarization_threshold_tokens,
            summarization_keep_recent_messages=summarization_keep_recent_messages,
        )
