from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trading.ai import SignalReviewer
from trading.runner import RunnerConfig, build_broker, run_once, write_report


def main() -> int:
    mode = os.environ.get("TRADING_MODE", "paper").strip().lower()
    config_path = Path(os.environ.get("STRATEGY_CONFIG", "strategies.json"))
    ledger_path = Path(os.environ.get("PAPER_LEDGER", "paper-ledger.json"))
    fixture = os.environ.get("STRATEGY_FIXTURE_FILE", "").strip()
    fixture_path = Path(fixture) if fixture else None

    if mode == "live" and os.environ.get("TRADING_LIVE_CONFIRM", "") != "I_UNDERSTAND_REAL_MONEY":
        print("Refusing live mode without TRADING_LIVE_CONFIRM=I_UNDERSTAND_REAL_MONEY", file=sys.stderr)
        return 2

    config = RunnerConfig.from_file(config_path)
    broker = build_broker(mode, config, ledger_path, fixture_path)
    reviewer = SignalReviewer(os.environ.get("OPENAI_API_KEY"), os.environ.get("OPENAI_MODEL", "gpt-4o-mini"))

    outcomes = run_once(config, broker, reviewer, mode=mode)
    write_report(outcomes, mode, Path("strategy-run.json"), Path("strategy-run.md"))
    print(Path("strategy-run.md").read_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
