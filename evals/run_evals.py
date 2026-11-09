"""Run the scripted scenarios through the real agent, model and live API.

    python -m evals.run_evals [--scenario NAME] [--runs N]

Prints `scenario | run | status | detail` and writes the rows to
`logs/evals/<YYYYmmddTHHMMSS>.json`. Exit code 0 iff every row is `pass` or
`skipped (precondition)`.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from agent import build_agent, build_deps, run_turn
from config import Settings, load_settings
from evals.scenarios import FAIL, PASS, SCENARIOS, SKIPPED, Scenario, new_return_local
from payment import ScriptedPaymentCollector

OK_STATUSES = {PASS, SKIPPED}


def run_scenario(settings: Settings, scenario: Scenario, run_index: int) -> dict:
    """Play one scenario in a fresh session and grade its trace."""
    deps = build_deps(settings, ScriptedPaymentCollector(scenario.payments))
    row = {
        "scenario": scenario.name,
        "run": run_index,
        "status": FAIL,
        "detail": "",
        "session_id": deps.state.session_id,
    }
    try:
        new_local = ""
        if scenario.extend_by is not None:
            reservation = deps.client.get_reservation(scenario.reservation_id)
            new_local = new_return_local(
                reservation, datetime.now(timezone.utc), scenario.extend_by
            )
        values = {
            "rid": scenario.reservation_id or "",
            "email": scenario.email or "",
            "new_local": new_local,
        }
        agent = build_agent(settings)
        history: list = []
        replies: list[str] = []
        for template in scenario.messages:
            reply, history = run_turn(agent, deps, history, template.format(**values))
            replies.append(reply)
        row["status"], row["detail"] = scenario.check(
            deps.state.trace, "\n".join(replies)
        )
    except Exception as exc:  # noqa: BLE001 - one broken run must not stop the rest
        row["detail"] = f"error: {type(exc).__name__}: {exc}"
    return row


def _print_table(rows: list[dict]) -> None:
    header = ("scenario", "run", "status", "detail")
    lines = [header] + [
        (r["scenario"], str(r["run"]), r["status"], r["detail"]) for r in rows
    ]
    widths = [max(len(line[i]) for line in lines) for i in range(3)]
    for line in lines:
        cells = [line[i].ljust(widths[i]) for i in range(3)] + [line[3]]
        print(" | ".join(cells))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the scripted scenario evals.")
    parser.add_argument("--scenario", help="run only this scenario")
    parser.add_argument("--runs", type=int, default=1, help="runs per scenario")
    args = parser.parse_args(argv)

    scenarios = [s for s in SCENARIOS if args.scenario in (None, s.name)]
    if not scenarios:
        print(f"Unknown scenario: {args.scenario}", file=sys.stderr)
        return 2

    settings = load_settings()
    # run_scenario is a module global looked up at call time, so tests can patch it.
    rows = [
        run_scenario(settings, scenario, run)
        for run in range(1, args.runs + 1)
        for scenario in scenarios
    ]

    _print_table(rows)
    out_dir = Path(settings.log_dir) / "evals"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    out = out_dir / f"{stamp}.json"
    out.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(f"Results: {out}")
    return 0 if all(r["status"] in OK_STATUSES for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
