#!/usr/bin/env python3
"""Multi-agent progress monitor for the KT_python port.

Reads agent status files from KT_python/.agents/*.json and renders a live
status table. Each agent writes its own status file while it works:

    {
      "agent": "api-layer",            # unique id, matches filename
      "title": "FastAPI HTTP layer",   # human label
      "status": "running",             # running | done | failed | stopped
      "progress": 40,                  # 0-100
      "current_task": "writing events router",
      "files": ["knowledge_tracer/api/app.py"],
      "tests": {"passed": 12, "failed": 0},
      "started_at": "2026-09-30T23:05:00+05:30",
      "updated_at": "2026-09-30T23:11:00+05:30",
      "error": null
    }

Usage:
    python3 scripts/monitor_agents.py              # one-shot table
    python3 scripts/monitor_agents.py --watch 5    # refresh every 5s
    python3 scripts/monitor_agents.py --json       # machine-readable summary
    python3 scripts/monitor_agents.py --stale-minutes 8

Exit code is 1 if any agent has status "failed", else 0.
A file whose status is "running" but whose updated_at is older than
--stale-minutes is shown as "stopped?" — the agent likely died mid-task.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

AGENTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".agents")

COLORS = {
    "running": "\033[94m",   # blue
    "done": "\033[92m",      # green
    "failed": "\033[91m",    # red
    "stopped": "\033[93m",   # yellow
    "stopped?": "\033[93m",
    "reset": "\033[0m",
    "dim": "\033[2m",
}


def load_statuses() -> list[dict]:
    path = os.path.abspath(AGENTS_DIR)
    if not os.path.isdir(path):
        return []
    out = []
    for name in sorted(os.listdir(path)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(path, name)) as fh:
                data = json.load(fh)
            data.setdefault("agent", name[:-5])
            out.append(data)
        except (json.JSONDecodeError, OSError):
            out.append({"agent": name[:-5], "status": "failed",
                        "error": "corrupt status file", "progress": 0})
    return out


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def age_str(ts: datetime | None) -> str:
    if ts is None:
        return "-"
    delta = datetime.now(timezone.utc) - ts.astimezone(timezone.utc)
    secs = max(0, int(delta.total_seconds()))
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    return f"{secs // 3600}h{(secs % 3600) // 60:02d}m ago"


def progress_bar(pct: int, width: int = 24) -> str:
    pct = max(0, min(100, int(pct)))
    filled = round(width * pct / 100)
    return "█" * filled + "░" * (width - filled)


def effective_status(agent: dict, stale_after_s: float) -> str:
    status = str(agent.get("status", "stopped")).lower()
    if status == "running":
        updated = parse_ts(agent.get("updated_at"))
        if updated is not None:
            age = (datetime.now(timezone.utc) - updated.astimezone(timezone.utc)).total_seconds()
            if age > stale_after_s:
                return "stopped?"
    return status


def render(agents: list[dict], stale_after_s: float) -> str:
    lines = []
    lines.append("KT_python — multi-agent monitor")
    lines.append(f"watching {os.path.abspath(AGENTS_DIR)}  ·  {datetime.now().strftime('%H:%M:%S')}")
    lines.append("")
    if not agents:
        lines.append("  (no agent status files yet)")
        return "\n".join(lines)

    header = f"  {'AGENT':<18} {'STATUS':<9} {'PROGRESS':<28} {'UPDATED':<9} CURRENT TASK"
    lines.append(header)
    lines.append("  " + "-" * (len(header) - 2))
    done = failed = running = 0
    for a in agents:
        status = effective_status(a, stale_after_s)
        base = str(a.get("status", "")).lower()
        done += base == "done"
        failed += base == "failed"
        running += status == "running"
        color = COLORS.get(status, COLORS["stopped"])
        bar = progress_bar(a.get("progress", 0))
        pct = f"{int(a.get('progress', 0)):3d}%"
        task = str(a.get("current_task") or a.get("error") or "-")
        if len(task) > 48:
            task = task[:45] + "..."
        updated = age_str(parse_ts(a.get("updated_at")))
        lines.append(
            f"  {a.get('agent', '?'):<18} {color}{status:<9}{COLORS['reset']}"
            f" {bar} {pct} {updated:<9} {task}"
        )
        t = a.get("tests") or {}
        if t.get("passed") or t.get("failed"):
            lines.append(
                f"  {'':<18} {COLORS['dim']}tests: {t.get('passed', 0)} passed,"
                f" {t.get('failed', 0)} failed{COLORS['reset']}"
            )
    lines.append("")
    lines.append(f"  total: {len(agents)}  ·  running: {running}  ·  done: {done}  ·  failed: {failed}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Monitor KT_python sub-agent progress")
    parser.add_argument("--watch", type=float, default=0, help="refresh interval in seconds")
    parser.add_argument("--json", action="store_true", help="print JSON summary and exit")
    parser.add_argument("--stale-minutes", type=float, default=5,
                        help="a 'running' agent silent for this long is shown as 'stopped?'")
    args = parser.parse_args()
    stale_s = args.stale_minutes * 60

    def snapshot() -> tuple[list[dict], bool]:
        agents = load_statuses()
        any_failed = any(str(a.get("status", "")).lower() == "failed" for a in agents)
        return agents, any_failed

    if args.json:
        agents, any_failed = snapshot()
        print(json.dumps({
            "agents": [
                {**a, "effective_status": effective_status(a, stale_s)} for a in agents
            ],
            "summary": {
                "total": len(agents),
                "done": sum(str(a.get("status")) == "done" for a in agents),
                "failed": sum(str(a.get("status")) == "failed" for a in agents),
                "running": sum(effective_status(a, stale_s) == "running" for a in agents),
            },
        }, indent=2))
        return 1 if any_failed else 0

    if args.watch and args.watch > 0:
        try:
            while True:
                agents, any_failed = snapshot()
                sys.stdout.write("\033[2J\033[H")  # clear screen
                sys.stdout.write(render(agents, stale_s) + "\n")
                sys.stdout.flush()
                time.sleep(args.watch)
        except KeyboardInterrupt:
            return 0

    agents, any_failed = snapshot()
    print(render(agents, stale_s))
    return 1 if any_failed else 0


if __name__ == "__main__":
    sys.exit(main())
