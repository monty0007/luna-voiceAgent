#!/usr/bin/env python3
"""Create the Work IQ connection and toolbox via the Azure Developer CLI (azd).

This is Phase 2 of the setup. It is a thin, well-logged wrapper around the
supported `azd ai` commands. Run it AFTER the one-time tenant setup (see
README.md "Work IQ setup"):
  1. Global Admin provisions the Work IQ service principal.
  2. A single-tenant Entra app is registered with delegated `WorkIQAgent.Ask`
     and admin consent granted, and a client secret created.
  3. Copilot Credits usage-based billing is enabled.

Prereqs on your machine:
  - azd >= 1.27.1 with the azure.ai.agents extension  (`brew install azd` /
    see README), and `azd auth login` completed.

Env required (see .env.example):
  PROJECT_ENDPOINT, WORKIQ_CLIENT_ID, WORKIQ_CLIENT_SECRET, WORKIQ_TENANT_ID
Optional:
  WORKIQ_CONNECTION_NAME (default workiq-conn)
  TOOLBOX_NAME         (default work-iq-toolbox)

After it succeeds, copy the printed values into your .env and re-run
`src/create_agent.py` to attach the toolbox to the agent.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

from dotenv import load_dotenv

_HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_HERE, os.pardir, ".env"), override=False)
load_dotenv(os.path.join(_HERE, ".env"), override=False)

WORKIQ_A2A_TARGET = "https://workiq.svc.cloud.microsoft/a2a/"
WORKIQ_SCOPE = "api://workiq.svc.cloud.microsoft/WorkIQAgent.Ask,offline_access"


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"Missing required env var: {name} (set it in .env)")
    return value


def _run(cmd: list[str]) -> str:
    """Run a command, echoing it (with secrets masked), return stdout."""
    shown = [("<secret>" if "secret" in c.lower() or (i > 0 and cmd[i - 1] == "--client-secret") else c)
             for i, c in enumerate(cmd)]
    print(f"\n$ {' '.join(shown)}")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.stdout:
        print(proc.stdout.strip())
    if proc.returncode != 0:
        print(proc.stderr.strip(), file=sys.stderr)
        sys.exit(f"Command failed with exit code {proc.returncode}: {cmd[0]} {cmd[1]} ...")
    return proc.stdout.strip()


def main() -> None:
    if shutil.which("azd") is None:
        sys.exit("azd is not installed. Install the Foundry Dev Pack / azd first "
                 "(see README.md), then run `azd auth login`.")

    project_endpoint = _require("PROJECT_ENDPOINT")
    tenant_id = _require("WORKIQ_TENANT_ID")
    client_id = _require("WORKIQ_CLIENT_ID")
    client_secret = _require("WORKIQ_CLIENT_SECRET")
    conn_name = os.environ.get("WORKIQ_CONNECTION_NAME", "workiq-conn").strip()
    toolbox_name = os.environ.get("TOOLBOX_NAME", "work-iq-toolbox").strip()

    token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    auth_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/authorize"

    # Point azd at the project.
    _run(["azd", "ai", "project", "set", project_endpoint])

    # 1. Create the Work IQ (remote A2A, OAuth2) connection.
    _run([
        "azd", "ai", "connection", "create", conn_name,
        "--kind", "remote-a2a",
        "--target", WORKIQ_A2A_TARGET,
        "--auth-type", "oauth2",
        "--authorization-url", auth_url,
        "--token-url", token_url,
        "--refresh-url", token_url,
        "--client-id", client_id,
        "--client-secret", client_secret,
        "--scopes", WORKIQ_SCOPE,
    ])
    print("\nNOTE: If this is the first Work IQ connection, open the consent/"
          "redirect URL azd prints, complete admin consent, and add the shown "
          "OAuth redirect URL to your Entra app registration (Web platform).")

    # 2. Create the toolbox containing the Work IQ tool.
    toolbox_yaml = os.path.join(_HERE, "_toolbox.yaml")
    with open(toolbox_yaml, "w", encoding="utf-8") as fh:
        fh.write(
            "description: Work IQ toolbox for the realtime voice agent\n"
            "tools:\n"
            "  - type: work_iq_preview\n"
            f"    project_connection_id: {conn_name}\n"
        )
    try:
        _run(["azd", "ai", "toolbox", "create", toolbox_name, "--from-file", toolbox_yaml])
    finally:
        os.remove(toolbox_yaml)

    # 3. Derive the values the agent needs.
    project_name = project_endpoint.rstrip("/").split("/")[-1]
    resource = project_endpoint.split("//", 1)[-1].split(".", 1)[0]
    toolbox_url = (
        f"https://{resource}.services.ai.azure.com/api/projects/{project_name}"
        f"/toolboxes/{toolbox_name}/mcp?api-version=v1"
    )

    print("\n" + "=" * 66)
    print("DONE. Copy these into your .env, then re-run src/create_agent.py:")
    print("=" * 66)
    print(f"TOOLBOX_PROJECT_CONNECTION_ID={conn_name}")
    print(f"TOOLBOX_SERVER_URL={toolbox_url}")
    print("\n(If azd printed a full connection resource ID, you can use that "
          "for TOOLBOX_PROJECT_CONNECTION_ID instead of the name.)")


if __name__ == "__main__":
    main()
