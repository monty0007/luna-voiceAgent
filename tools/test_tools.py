#!/usr/bin/env python3
"""Smoke-test the agent's tools in text mode (no audio needed).

Creates a short-lived prompt agent with the same tool wiring as
`src/create_agent.py`, asks it to list its tools, then optionally runs a real
Work IQ question. Useful to confirm the toolbox/Work IQ/MCP connections work
BEFORE you put a voice in front of them.

Auth: DefaultAzureCredential (`az login`).

Env: PROJECT_ENDPOINT, MODEL_DEPLOYMENT_NAME (plus the same TOOLBOX_* /
     WORK_IQ_PROJECT_CONNECTION_ID values used by create_agent.py).

Usage:
  python tools/test_tools.py                      # just list discovered tools
  python tools/test_tools.py --ask "What meetings do I have today?"
"""

from __future__ import annotations

import argparse
import os
import sys

from dotenv import load_dotenv

_HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_HERE, os.pardir, ".env"), override=False)

# Reuse the exact tool-wiring logic from create_agent.py.
sys.path.insert(0, os.path.join(_HERE, os.pardir, "src"))
from create_agent import build_tools  # noqa: E402

from azure.identity import DefaultAzureCredential  # noqa: E402
from azure.ai.projects import AIProjectClient  # noqa: E402
from azure.ai.projects.models import PromptAgentDefinition  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke-test the agent's tools (text mode).")
    parser.add_argument("--ask", help="Optional question to send after the tool-listing check.")
    args = parser.parse_args()

    tools = build_tools()
    if not tools:
        sys.exit("No tools configured. Set TOOLBOX_SERVER_URL + "
                 "TOOLBOX_PROJECT_CONNECTION_ID or WORK_IQ_PROJECT_CONNECTION_ID in .env.")

    project_endpoint = os.environ["PROJECT_ENDPOINT"]
    model = os.environ["MODEL_DEPLOYMENT_NAME"]

    with (
        DefaultAzureCredential() as credential,
        AIProjectClient(endpoint=project_endpoint, credential=credential) as project,
        project.get_openai_client() as openai,
    ):
        agent = project.agents.create_version(
            agent_name="voice-agent-toolcheck",
            definition=PromptAgentDefinition(
                model=model,
                instructions=(
                    "You can use the connected tools. When asked what you can do, "
                    "list your available tools and describe each briefly."
                ),
                tools=tools,
            ),
        )
        print(f"Temp agent created: {agent.name} (version {agent.version})")

        try:
            # 1. Discovery check: what tools did the runtime actually get?
            listing = openai.responses.create(
                input="List the tools you can use and briefly describe each one.",
                extra_body={"agent_reference": {"name": agent.name, "type": "agent_reference"}},
            )
            print("\n--- Tool discovery ---")
            print(listing.output_text)

            # 2. Optional real question (e.g. exercises Work IQ).
            if args.ask:
                answer = openai.responses.create(
                    input=args.ask,
                    extra_body={"agent_reference": {"name": agent.name, "type": "agent_reference"}},
                )
                print("\n--- Answer ---")
                print(answer.output_text)
        finally:
            project.agents.delete_version(agent_name=agent.name, agent_version=agent.version)
            print("\nTemp agent deleted.")


if __name__ == "__main__":
    main()
