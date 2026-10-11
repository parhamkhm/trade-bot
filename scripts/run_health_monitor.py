"""Entry point for ``tbot-health-monitor.service`` (stdlib only; see tbot.monitoring.health_monitor).

Usage on the server (the systemd unit runs the first form):
    python3 scripts/run_health_monitor.py              # long-running loop
    python3 scripts/run_health_monitor.py --check      # one pass, print problems, send nothing
    python3 scripts/run_health_monitor.py --send-test  # send one test message to Telegram
"""

from __future__ import annotations

import sys
from pathlib import Path


def _run() -> int:
    try:
        from tbot.monitoring.health_monitor import main
    except ImportError:  # started straight from a checkout, without uv / PYTHONPATH
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
        from tbot.monitoring.health_monitor import main
    return main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(_run())
