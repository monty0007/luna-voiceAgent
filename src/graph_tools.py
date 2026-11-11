"""Microsoft Graph tools for the voice agent (read-only calendar).

Auth: delegated sign-in (browser by default, device code optional) with the Entra
app in GRAPH_CLIENT_ID.
Sign in once with `python scripts/graph_login.py`; the token is cached on disk
(OS keychain) and refreshed silently, so the voice turn never blocks on a login.

Env: GRAPH_CLIENT_ID (required), GRAPH_TENANT_ID (default: organizations),
     GRAPH_AUTH_FLOW (browser|device, default browser),
     GRAPH_AUTH_RECORD (default ~/.cache/voiceagent/graph_auth.json)
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCOPES = ["https://graph.microsoft.com/Calendars.Read"]
GRAPH = "https://graph.microsoft.com/v1.0"
CACHE_NAME = "voiceagent_graph"
MAX_PAGES = 5
MAX_LISTED = 15


class GraphNotReady(RuntimeError):
    """Not configured or not signed in; the message tells the user what to run."""


def configured() -> bool:
    return bool(os.environ.get("GRAPH_CLIENT_ID", "").strip())


def _record_path() -> Path:
    return Path(os.environ.get("GRAPH_AUTH_RECORD", "~/.cache/voiceagent/graph_auth.json")).expanduser()


def _credential(*, allow_login: bool):
    from azure.identity import (AuthenticationRecord, DeviceCodeCredential,
                                InteractiveBrowserCredential, TokenCachePersistenceOptions)
    client_id = os.environ.get("GRAPH_CLIENT_ID", "").strip()
    if not client_id:
        raise GraphNotReady("GRAPH_CLIENT_ID is not set in .env")
    record = None
    path = _record_path()
    if path.exists():
        record = AuthenticationRecord.deserialize(path.read_text())
    elif not allow_login:
        raise GraphNotReady("not signed in to Microsoft Graph: run `python scripts/graph_login.py`")
    # "browser" (default) signs in through your normal browser on this Mac, so
    # conditional-access rules that need a managed/compliant device can see the
    # device. "device" (device code) has no device context and is often blocked
    # (AADSTS530033). Set GRAPH_AUTH_FLOW=device to use it.
    cls = (DeviceCodeCredential
           if os.environ.get("GRAPH_AUTH_FLOW", "browser").strip().lower() == "device"
           else InteractiveBrowserCredential)
    return cls(
        client_id=client_id,
        tenant_id=os.environ.get("GRAPH_TENANT_ID", "").strip() or "organizations",
        cache_persistence_options=TokenCachePersistenceOptions(name=CACHE_NAME),
        authentication_record=record,
        disable_automatic_authentication=not allow_login,
    )


def login() -> str:
    """Interactive one-time sign-in; returns the signed-in username."""
    cred = _credential(allow_login=True)
    record = cred.authenticate(scopes=SCOPES)
    path = _record_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(record.serialize())
    path.chmod(0o600)
    return record.username


def _token() -> str:
    try:
        return _credential(allow_login=False).get_token(*SCOPES).token
    except GraphNotReady:
        raise
    except Exception as exc:  # noqa: BLE001 - expired/revoked refresh token etc.
        raise GraphNotReady(f"Graph sign-in expired ({type(exc).__name__}): "
                            "run `python scripts/graph_login.py` again") from exc


def _graph_get(url: str, token: str) -> dict:
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Prefer": 'outlook.timezone="UTC"',
    })
    with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 - fixed https host
        return json.loads(resp.read().decode())


def period_bounds(period: str, now: datetime | None = None) -> tuple[datetime, datetime]:
    """Local-time [start, end) for today / this_week / next_week (weeks start Monday)."""
    now = (now or datetime.now()).astimezone()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "today":
        return midnight, midnight + timedelta(days=1)
    monday = midnight - timedelta(days=midnight.weekday())
    if period == "this_week":
        return monday, monday + timedelta(days=7)
    if period == "next_week":
        return monday + timedelta(days=7), monday + timedelta(days=14)
    raise ValueError("period must be one of: today, this_week, next_week")


def _parse_utc(value: str) -> datetime:
    # Graph returns e.g. "2026-11-12T09:00:00.0000000" (UTC because of the Prefer header).
    return datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def summarize(events: list[dict], start: datetime, end: datetime, period: str) -> dict[str, Any]:
    """Count real meetings (not cancelled, not all-day) and group them by local day."""
    meetings, all_day = [], 0
    for e in events:
        if e.get("isCancelled"):
            continue
        if e.get("isAllDay"):
            all_day += 1
            continue
        s = _parse_utc(e["start"]["dateTime"]).astimezone()
        f = _parse_utc(e["end"]["dateTime"]).astimezone()
        meetings.append((s, f, e.get("subject") or "(no subject)"))
    meetings.sort()
    by_day: dict[str, int] = {}
    for s, _f, _subj in meetings:
        key = s.strftime("%A %d %b")
        by_day[key] = by_day.get(key, 0) + 1
    return {
        "period": period,
        "from": start.strftime("%A %d %b"),
        "to": (end - timedelta(seconds=1)).strftime("%A %d %b"),
        "meeting_count": len(meetings),
        "all_day_events": all_day,
        "meetings_per_day": by_day,
        "meetings": [{"subject": subj, "day": s.strftime("%A %d %b"),
                      "start": s.strftime("%H:%M"), "end": f.strftime("%H:%M")}
                     for s, f, subj in meetings[:MAX_LISTED]],
        "truncated": len(meetings) > MAX_LISTED,
    }


def get_calendar(period: str = "this_week") -> dict[str, Any]:
    """Meetings in the signed-in user's calendar for today / this_week / next_week."""
    start, end = period_bounds(period)
    token = _token()
    params = urllib.parse.urlencode({
        "startDateTime": start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endDateTime": end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "$select": "subject,start,end,isAllDay,isCancelled",
        "$top": "100",
    })
    url: str | None = f"{GRAPH}/me/calendarView?{params}"
    events: list[dict] = []
    pages = 0
    while url and pages < MAX_PAGES:
        page = _graph_get(url, token)
        events += page.get("value", [])
        url = page.get("@odata.nextLink")
        pages += 1
    return summarize(events, start, end, period)
