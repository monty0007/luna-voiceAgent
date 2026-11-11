"""Create (or update) the Foundry prompt agent for the realtime voice agent.

What this does:
  1. Builds a PromptAgentDefinition (model + instructions + optional tools).
  2. Optionally attaches a Foundry Toolbox (Work IQ / MCP servers) as an MCP
     tool, when TOOLBOX_SERVER_URL and TOOLBOX_PROJECT_CONNECTION_ID are set.
     Leave them blank to create a plain agent first and add tools later.
  3. Stores the Voice Live session settings (voice, VAD, noise reduction, echo
     cancellation) in the agent metadata (chunked to respect the 512-char
     metadata value limit) so the Voice Live service applies them automatically.

Auth: uses DefaultAzureCredential (run `az login` first).

Required env: PROJECT_ENDPOINT, AGENT_NAME, MODEL_DEPLOYMENT_NAME
Optional env: TOOLBOX_SERVER_URL, TOOLBOX_PROJECT_CONNECTION_ID,
              VOICE_NAME
"""

import json
import os
import sys

from azure.identity import DefaultAzureCredential
from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import PromptAgentDefinition, MCPTool, WorkIQPreviewTool
from dotenv import load_dotenv

# Load config from the project root .env (one level up from this file).
_HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_HERE, ".env"), override=True)
load_dotenv(os.path.join(_HERE, os.pardir, ".env"), override=False)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# Voice Live config chunking (512-char metadata value limit)
# ---------------------------------------------------------------------------
def chunk_config(config_json: str, limit: int = 512) -> dict:
    """Split a config JSON string into chunked metadata entries."""
    metadata = {"microsoft.voice-live.configuration": config_json[:limit]}
    remaining = config_json[limit:]
    chunk_num = 1
    while remaining:
        metadata[f"microsoft.voice-live.configuration.{chunk_num}"] = remaining[:limit]
        remaining = remaining[limit:]
        chunk_num += 1
    return metadata


def reassemble_config(metadata: dict) -> str:
    """Reassemble a chunked Voice Live configuration from agent metadata."""
    config = metadata.get("microsoft.voice-live.configuration", "")
    chunk_num = 1
    while f"microsoft.voice-live.configuration.{chunk_num}" in metadata:
        config += metadata[f"microsoft.voice-live.configuration.{chunk_num}"]
        chunk_num += 1
    return config


# ---------------------------------------------------------------------------
# Build the optional tools list
# ---------------------------------------------------------------------------
def build_tools() -> list:
    """Return the list of tools to attach to the agent.

    Two mutually exclusive wiring options (toolbox takes precedence):

    1. Toolbox (preferred): set TOOLBOX_SERVER_URL + TOOLBOX_PROJECT_CONNECTION_ID.
       The toolbox centrally manages Work IQ and any MCP servers, so adding
       tools later does not require changing this code.
    2. Direct Work IQ: set WORK_IQ_PROJECT_CONNECTION_ID to attach the Work IQ
       tool straight to the agent. Simpler, but MCP servers then each need to
       be added individually.

    Leave both unset to create a plain agent first and add tools later.
    """
    tools = []
    toolbox_url = os.environ.get("TOOLBOX_SERVER_URL", "").strip()
    toolbox_conn = os.environ.get("TOOLBOX_PROJECT_CONNECTION_ID", "").strip()
    workiq_conn = os.environ.get("WORK_IQ_PROJECT_CONNECTION_ID", "").strip()

    if toolbox_url and toolbox_conn:
        tools.append(
            MCPTool(
                server_label="toolbox",
                server_url=toolbox_url,
                # "never" keeps the voice conversation flowing without a typed
                # approval step. Tighten to "always" for write-capable tools.
                require_approval="never",
                project_connection_id=toolbox_conn,
            )
        )
        print("Toolbox attached as MCP tool (require_approval=never).")
    elif workiq_conn:
        tools.append(WorkIQPreviewTool(project_connection_id=workiq_conn))
        print("Work IQ tool attached directly.")
    else:
        print("No tools configured — creating a plain agent. "
              "Set TOOLBOX_SERVER_URL + TOOLBOX_PROJECT_CONNECTION_ID (toolbox) "
              "or WORK_IQ_PROJECT_CONNECTION_ID (direct Work IQ) to add tools.")
    return tools


DEFAULT_INSTRUCTIONS = (
    "You are a voice assistant. "
    "Reply in ONE short sentence (max 12 words) unless the user "
    "explicitly asks for detail. "
    "Never use greetings, sign-offs, or filler like 'Great question'. "
    "No markdown, no bullet points — plain spoken words only. "
    "Use the connected tools when they help answer the user's request."
)


def prune_old_versions(project_client, agent_name: str, keep: int) -> None:
    """Delete all but the newest `keep` versions so they don't pile up. keep<=0 disables."""
    if keep <= 0:
        return
    try:
        versions = list(project_client.agents.list_versions(agent_name=agent_name, order="desc"))
        for old in versions[keep:]:
            project_client.agents.delete_version(agent_name=agent_name, agent_version=old.version)
            print(f"Pruned old agent version {old.version}")
    except Exception as exc:  # noqa: BLE001 - pruning is best-effort
        print(f"(Skipped pruning old versions: {exc})")


def main() -> None:
    required = ("PROJECT_ENDPOINT", "AGENT_NAME", "MODEL_DEPLOYMENT_NAME")
    missing = [name for name in required if not os.environ.get(name, "").strip()]
    if missing:
        sys.exit(f"Set {', '.join(missing)} in .env (see .env.example).")
    project_endpoint = os.environ["PROJECT_ENDPOINT"].strip()
    agent_name = os.environ["AGENT_NAME"].strip()
    model = os.environ["MODEL_DEPLOYMENT_NAME"].strip()
    voice_name = os.environ.get("VOICE_NAME", "en-US-Ava:DragonHDLatestNeural")

    project_client = AIProjectClient(
        endpoint=project_endpoint,
        credential=DefaultAzureCredential(),
    )

    # Voice Live session settings stored on the agent.
    # Transcription model: agent mode uses a CASCADED pipeline, which only
    # supports: azure-speech, azure-mrs, mai-transcribe, mai-transcribe-1.5,
    # mai-transcribe-2, mai-transcribe-medical, mai-transcribe-2-streaming.
    # (whisper-1 / gpt-4o-transcribe are rejected by the service in this mode.)
    # Default: mai-transcribe-2. NOTE: the -streaming variant stalled turn
    # completion in cascaded mode (no response.created after speech stopped),
    # so we use the non-streaming model which follows the same per-turn flow
    # as azure-speech but with better accuracy/latency. Override with
    # TRANSCRIPTION_MODEL in .env (e.g. azure-speech to fall back).
    transcription_model = os.environ.get("TRANSCRIPTION_MODEL", "mai-transcribe-2")

    voice_live_config = {
        "session": {
            # temperature 0.5 (was 0.8): calmer, more direct delivery — less
            # prosodic flourish means measurably faster spoken replies.
            "voice": {"name": voice_name, "type": "azure-standard", "temperature": 0.5},
            "input_audio_transcription": {"model": transcription_model},
            # Dropped the multilingual end-of-utterance model: it adds real
            # latency waiting for semantic confirmation on every turn. Plain
            # azure_semantic_vad with tighter silence/threshold settings is
            # noticeably snappier for English-only conversations.
            # silence_duration_ms 400 (was 500): how long you must be quiet
            # before the system accepts your turn is over. 400ms still feels
            # natural but shaves 100ms off EVERY turn.
            "turn_detection": {
                "type": "azure_semantic_vad",
                "threshold": 0.5,
                "silence_duration_ms": _env_int("VAD_SILENCE_MS", 400),
                "prefix_padding_ms": 300,
                # Server cancels the reply as soon as the user speaks (barge-in);
                # the client only drops audio already in flight.
                "interrupt_response": True,
            },
            "input_audio_noise_reduction": {"type": "azure_deep_noise_suppression"},
            "input_audio_echo_cancellation": {"type": "server_echo_cancellation"},
        }
    }

    tools = build_tools()

    agent = project_client.agents.create_version(
        agent_name=agent_name,
        definition=PromptAgentDefinition(
            model=model,
            instructions=os.environ.get("AGENT_INSTRUCTIONS", "").strip() or DEFAULT_INSTRUCTIONS,
            tools=tools,
        ),
        metadata=chunk_config(json.dumps(voice_live_config)),
    )
    print(f"Agent created: {agent.name} (version {agent.version})")
    prune_old_versions(project_client, agent_name, keep=_env_int("AGENT_VERSIONS_KEEP", 5))

    # Verify the Voice Live configuration was stored correctly.
    retrieved = project_client.agents.get(agent_name=agent_name)
    stored_metadata = (retrieved.versions or {}).get("latest", {}).get("metadata", {})
    stored_config = reassemble_config(stored_metadata)
    if stored_config:
        print("\nVoice Live configuration stored:")
        print(json.dumps(json.loads(stored_config), indent=2))
    else:
        print("\nWARNING: Voice Live configuration not found in agent metadata.")


if __name__ == "__main__":
    main()
