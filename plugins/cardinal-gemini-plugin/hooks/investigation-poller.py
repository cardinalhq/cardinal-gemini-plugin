#!/usr/bin/env python3
"""Detached Gemini CLI Investigation inbox/bootstrap worker (the Codex
adapter's investigation-poller.py over ~/.gemini); never reads transcripts.

Started by cardinal-gemini-telemetry.py (investigation_agent.spawn_poller)
at SessionStart, BeforeAgent and AfterTool; at most one per session; it
outlives the hook and stops with the Gemini CLI process (--anchor)."""
from __future__ import annotations
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cardinal_core import investigation_poller as poller
from cardinal_core.paths import AgentPaths
from cardinal_core.storyboard_agent import Wiring
from _plugin_version import plugin_version


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", required=True)
    parser.add_argument("--anchor", type=int)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    wiring = Wiring("gemini", AgentPaths(home=Path.home() / ".gemini"), plugin_version())
    anchor = poller.resolve_anchor(args.anchor) if args.anchor and args.anchor > 1 else None
    poller.run(Path.home(), args.session, connection=wiring.connection, client=wiring.client,
               anchor=anchor, connected=lambda: bool(wiring.connection()), once=args.once)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    os._exit(0)
