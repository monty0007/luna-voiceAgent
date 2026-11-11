"""Local function tools for realtime mode (opt-in with REALTIME_TOOLS=1).

Realtime mode talks straight to gpt-realtime and has no Foundry toolbox, so tools
that should work there are plain Python functions registered here. To add one:
write a function that takes keyword arguments and returns something
JSON-serialisable, then add it to TOOLS with a JSON-schema `parameters` block.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable


def get_current_datetime() -> dict[str, str]:
    now = datetime.now().astimezone()
    return {
        "date": now.strftime("%A, %d %B %Y"),
        "time": now.strftime("%H:%M"),
        "timezone": str(now.tzinfo),
    }


# name -> (callable, description, JSON-schema parameters)
TOOLS: dict[str, tuple[Callable[..., Any], str, dict]] = {
    "get_current_datetime": (
        get_current_datetime,
        "Get the user's current local date, time and timezone.",
        {"type": "object", "properties": {}, "required": []},
    ),
}


def _get_calendar(period: str = "this_week") -> dict[str, Any]:
    import graph_tools
    try:
        return graph_tools.get_calendar(period)
    except graph_tools.GraphNotReady as exc:
        return {"error": str(exc)}


GRAPH_TOOLS: dict[str, tuple[Callable[..., Any], str, dict]] = {
    "get_calendar": (
        _get_calendar,
        "Count and list the user's meetings in their Microsoft 365 calendar for a "
        "period. Use for questions like 'how many meetings do I have this week?'. "
        "Answer with the meeting_count; mention details only if asked.",
        {"type": "object",
         "properties": {"period": {"type": "string",
                                   "enum": ["today", "this_week", "next_week"],
                                   "description": "Which period to look at."}},
         "required": []},
    ),
}


def active_tools() -> dict[str, tuple[Callable[..., Any], str, dict]]:
    """Base tools plus Graph tools when GRAPH_CLIENT_ID is configured (read at call time)."""
    import graph_tools
    return {**TOOLS, **(GRAPH_TOOLS if graph_tools.configured() else {})}


def run_tool(name: str, arguments_json: str) -> str:
    """Execute a tool by name; always returns a JSON string (errors included)."""
    entry = active_tools().get(name)
    if entry is None:
        return json.dumps({"error": f"unknown tool: {name}"})
    try:
        args = json.loads(arguments_json or "{}")
        return json.dumps(entry[0](**args))
    except Exception as exc:  # noqa: BLE001 - report any failure back to the model
        return json.dumps({"error": str(exc)})
