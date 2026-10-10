# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import argparse
import asyncio

from sweedit.core.agenthub import MinAgent


def main():
    """Main entry point for the CLI."""
    parser = argparse.ArgumentParser(
        description="SWE-Edit Minimal Agent",
    )

    parser.add_argument("task", type=str, help="Task prompt for the agent to execute")
    parser.add_argument(
        "--enable-context-summarization",
        action="store_true",
        help="Summarize older conversation turns once the context grows past the "
        "threshold, to keep the agent within the model's context window.",
    )
    parser.add_argument(
        "--summarization-threshold-tokens",
        type=int,
        default=50000,
        help="Context size (input tokens) above which summarization triggers.",
    )
    parser.add_argument(
        "--summarization-keep-recent-messages",
        type=int,
        default=50,
        help="Number of most recent messages kept verbatim when summarizing.",
    )

    args = parser.parse_args()

    agent = MinAgent(
        enable_context_summarization=args.enable_context_summarization,
        summarization_threshold_tokens=args.summarization_threshold_tokens,
        summarization_keep_recent_messages=args.summarization_keep_recent_messages,
    )
    asyncio.run(agent.run(user_message=args.task))


if __name__ == "__main__":
    main()
