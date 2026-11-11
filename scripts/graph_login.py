#!/usr/bin/env python3
"""One-time Microsoft Graph sign-in (browser), then a live calendar check.

Needs GRAPH_CLIENT_ID (and GRAPH_TENANT_ID) in .env. Run:  python scripts/graph_login.py
"""

from __future__ import annotations

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, os.pardir, "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(_HERE, os.pardir, ".env"), override=False)

import graph_tools  # noqa: E402

if not graph_tools.configured():
    sys.exit("Set GRAPH_CLIENT_ID (and GRAPH_TENANT_ID) in .env first.")

print("Signing in: a browser window opens; pick your Synapx account...")
user = graph_tools.login()
print(f"Signed in as {user}. Token cached; no further prompts needed.\n")
print("Calendar check (this week):")
print(json.dumps(graph_tools.get_calendar("this_week"), indent=2))
