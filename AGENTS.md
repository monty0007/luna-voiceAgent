# AGENTS.md — guidance for coding agents working in this repo

## Project
Realtime voice agent: **Voice Live (agent mode)** front-end over a **Foundry
prompt agent** that can reach **Work IQ** and **MCP servers** through a Foundry
**toolbox**.

## Layout
- `src/create_agent.py` — creates/updates the Foundry prompt agent, attaches the
  toolbox (if configured), and stores Voice Live session config in metadata.
- `src/voice_client.py` — Voice Live session/event handling, reconnect, watchdog.
- `src/audio.py` — mic capture + jitter-buffered playback and barge-in.
- `src/local_tools.py` — opt-in function tools for realtime mode.
- `tests/` — `python -m unittest discover -s tests`. Fully offline: `test_voice_client.py`
  replays wire-format server events through a fake connection (no key/network/audio).
- `.env` — local config (NOT committed). `.env.example` documents every value.
- `_vendor/voicelive-samples/` — read-only copy of the official samples for
  reference. Do not edit.

## Conventions
- Python 3.12 venv at `.venv` (created with `uv`).
- Config is read from the project-root `.env` (both scripts fall back to it).
- Auth: agent mode is Entra ID only (`az login`); realtime mode (default) uses
  `VOICELIVE_API_KEY`. Never commit keys or tokens (`.vscode/mcp.json` uses a prompt input).
- Keep `require_approval="never"` for tools during voice demos so the
  conversation does not stall.

If you are in VS Code, read the vscode-microsoft-foundry skill first.
