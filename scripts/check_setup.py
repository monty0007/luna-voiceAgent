#!/usr/bin/env python3
"""Pre-flight check: validate local setup before a live voice test.

Checks (no Azure calls are made):
  - Python >= 3.10
  - required packages import (and the local pyaudio shim resolves)
  - audio input/output devices are visible
  - required .env values are present (and warns about placeholder/empty values)
  - Azure CLI login state
  - azd availability (only needed for the Work IQ / toolbox phase)

Run:  python scripts/check_setup.py
Exit code is non-zero if any *blocking* check fails.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.join(_HERE, os.pardir)
sys.path.insert(0, os.path.join(_ROOT, "src"))  # so the pyaudio shim resolves

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(_ROOT, ".env"), override=False)

FAILURES: list[str] = []
WARNINGS: list[str] = []


def ok(msg: str) -> None:
    print(f"  [ok] {msg}")


def warn(msg: str) -> None:
    WARNINGS.append(msg)
    print(f"  [warn] {msg}")


def fail(msg: str) -> None:
    FAILURES.append(msg)
    print(f"  [FAIL] {msg}")


def check_python() -> None:
    print("\nPython")
    if sys.version_info >= (3, 10):
        ok(f"Python {sys.version_info.major}.{sys.version_info.minor}")
    else:
        fail(f"Python {sys.version_info.major}.{sys.version_info.minor} — need 3.10+")


def check_packages() -> None:
    print("\nPackages")
    for mod in ["azure.ai.projects", "azure.ai.voicelive", "openai", "azure.identity", "dotenv", "pyaudio"]:
        try:
            __import__(mod)
            note = " (sounddevice shim)" if mod == "pyaudio" else ""
            ok(f"import {mod}{note}")
        except Exception as exc:  # noqa: BLE001
            fail(f"import {mod} -> {exc}")


def check_audio() -> None:
    print("\nAudio devices")
    try:
        import pyaudio  # the shim
        p = pyaudio.PyAudio()
        count = p.get_device_count()
        inputs = sum(1 for i in range(count) if p.get_device_info_by_index(i)["maxInputChannels"] > 0)
        outputs = sum(1 for i in range(count) if p.get_device_info_by_index(i)["maxOutputChannels"] > 0)
        p.terminate()
        if inputs and outputs:
            ok(f"{inputs} input / {outputs} output device(s)")
        else:
            fail(f"inputs={inputs} outputs={outputs} — need at least one of each")
    except Exception as exc:  # noqa: BLE001
        fail(f"audio check -> {exc}")


def check_env() -> None:
    mode = os.environ.get("VOICE_MODE", "realtime").strip().lower()
    print(f"\n.env values (VOICE_MODE={mode})")
    if mode == "realtime":
        gw = [os.environ.get(n, "").strip() for n in ("AI_GATEWAY_BASE_URL", "AI_GATEWAY_KEY")]
        if any(gw):
            required = ["AI_GATEWAY_BASE_URL", "AI_GATEWAY_KEY"]
            print("  (AI gateway selected; run scripts/check_gateway.py for a live test)")
        else:
            required = ["VOICELIVE_ENDPOINT", "VOICELIVE_API_KEY"]
    elif mode == "agent":
        required = ["PROJECT_ENDPOINT", "PROJECT_NAME", "VOICELIVE_ENDPOINT",
                    "MODEL_DEPLOYMENT_NAME", "AGENT_NAME"]
    else:
        fail(f"VOICE_MODE must be 'realtime' or 'agent', got '{mode}'")
        return
    for name in required:
        value = os.environ.get(name, "").strip()
        if not value:
            fail(f"{name} is empty")
        else:
            ok(f"{name} set")
    if mode == "agent":
        # Tooling is optional at this stage.
        if os.environ.get("TOOLBOX_SERVER_URL", "").strip() and os.environ.get("TOOLBOX_PROJECT_CONNECTION_ID", "").strip():
            ok("Toolbox wiring present (TOOLBOX_*)")
        elif os.environ.get("WORK_IQ_PROJECT_CONNECTION_ID", "").strip():
            ok("Direct Work IQ wiring present")
        else:
            warn("No tools wired yet (TOOLBOX_* / WORK_IQ_PROJECT_CONNECTION_ID empty) — plain agent only")


def check_azure_login() -> None:
    print("\nAzure CLI login")
    if shutil.which("az") is None:
        fail("Azure CLI (az) not found")
        return
    proc = subprocess.run(["az", "account", "show", "--query", "user.name", "-o", "tsv"],
                          capture_output=True, text=True)
    if proc.returncode == 0 and proc.stdout.strip():
        ok(f"az logged in as {proc.stdout.strip()}")
    else:
        fail("not logged in — run `az login`")


def check_azd() -> None:
    print("\nazd (only needed for Work IQ / toolbox phase)")
    if shutil.which("azd") is None:
        warn("azd not installed — needed only to create the Work IQ connection/toolbox")
    else:
        proc = subprocess.run(["azd", "version"], capture_output=True, text=True)
        ok(f"azd {proc.stdout.strip() or 'installed'}")


def main() -> None:
    print("Voice agent pre-flight check")
    check_python()
    check_packages()
    check_audio()
    check_env()
    if os.environ.get("VOICE_MODE", "realtime").strip().lower() == "agent":
        check_azure_login()
        check_azd()
    else:
        print("\n(Skipping az/azd checks: realtime mode uses an API key.)")

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"BLOCKERS ({len(FAILURES)}):")
        for f in FAILURES:
            print(f"  - {f}")
    if WARNINGS:
        print(f"Notes ({len(WARNINGS)}):")
        for w in WARNINGS:
            print(f"  - {w}")
    if not FAILURES:
        print("All blocking checks passed. You're ready to run the voice client.")
    sys.exit(1 if FAILURES else 0)


if __name__ == "__main__":
    main()
