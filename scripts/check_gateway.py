#!/usr/bin/env python3
"""Live check of the AI gateway: chat models + the realtime WebSocket handshake.

Needs AI_GATEWAY_BASE_URL and AI_GATEWAY_KEY in .env. Makes real (tiny) calls.
Run:  python scripts/check_gateway.py
"""

from __future__ import annotations

import asyncio
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, os.pardir, "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(_HERE, os.pardir, ".env"), override=False)

BASE = os.environ.get("AI_GATEWAY_BASE_URL", "").strip()
KEY = os.environ.get("AI_GATEWAY_KEY", "").strip()
if not BASE or not KEY:
    sys.exit("Set AI_GATEWAY_BASE_URL and AI_GATEWAY_KEY in .env first.")

from openai import OpenAI  # noqa: E402

import voice_client  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def chat(model: str) -> None:
    root = BASE.rstrip("/")
    root = root if root.endswith("/v1") else root + "/v1"
    try:
        r = OpenAI(base_url=root, api_key=KEY, timeout=30).chat.completions.create(
            model=model, messages=[{"role": "user", "content": "Reply with the word: pong"}],
            max_completion_tokens=200)
        RESULTS.append((f"chat {model}", True, (r.choices[0].message.content or "").strip()[:60]))
    except Exception as exc:  # noqa: BLE001
        RESULTS.append((f"chat {model}", False, f"{type(exc).__name__}: {str(exc)[:160]}"))


async def realtime(model: str) -> None:
    mgr = voice_client.GatewayConnectionManager(base_url=BASE, api_key=KEY, model=model)
    try:
        async with mgr as conn:
            ev = await asyncio.wait_for(conn.recv(), 20)
            RESULTS.append((f"realtime {model}", True, f"first event: {ev.type}"))
    except Exception as exc:  # noqa: BLE001
        RESULTS.append((f"realtime {model}", False, f"{type(exc).__name__}: {str(exc)[:160]}"))


def main() -> None:
    chat_model = os.environ.get("AI_GATEWAY_CHAT_MODEL", "gpt-5.6-terra")
    # Only the chat model(s) your key may use; extra ones via AI_GATEWAY_EXTRA_MODELS=a,b
    extra = [m.strip() for m in os.environ.get("AI_GATEWAY_EXTRA_MODELS", "").split(",") if m.strip()]
    for m in dict.fromkeys([chat_model, *extra]):
        chat(m)
    asyncio.run(realtime(os.environ.get("AI_GATEWAY_REALTIME_MODEL", "gpt-realtime-2.1")))
    for name, good, detail in RESULTS:
        print(f"  [{'ok' if good else 'FAIL'}] {name}: {detail}")
    sys.exit(0 if all(g for _, g, _ in RESULTS) else 1)


if __name__ == "__main__":
    main()
