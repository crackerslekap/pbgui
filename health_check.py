"""CLI health checks for PBGUI deployment components.

This script validates core dependencies without starting services.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import psutil

from Database import Database
from pbgui_func import PBGDIR


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str


SERVICE_DEFINITIONS = (
    ("PBRun", "pbrun.pid", "pbrun.py"),
    ("PBStat", "pbstat.pid", "pbstat.py"),
    ("PBMon", "pbmon.pid", "pbmon.py"),
    ("PBData", "pbdata.pid", "pbdata.py"),
    ("PBRemote", "pbremote.pid", "pbremote.py"),
    ("PBCoinData", "pbcoindata.pid", "pbcoindata.py"),
)


def check_database() -> CheckResult:
    """Verify the SQLite database file is reachable and readable."""
    try:
        db = Database()
        conn = db._connect()  # pylint: disable=protected-access
        conn.execute("SELECT 1;").fetchone()
        return CheckResult("database", True, "Connected and responded to SELECT 1")
    except Exception as exc:  # pragma: no cover - defensive catch for CLI visibility
        return CheckResult("database", False, f"Database connection failed: {exc}")


def _read_pid(pid_path: Path) -> tuple[bool, str | None]:
    if not pid_path.exists():
        return False, "pid file missing"
    pid_raw = pid_path.read_text().strip()
    if not pid_raw.isdigit():
        return False, "pid file is not numeric"
    return True, pid_raw


def check_service(name: str, pid_filename: str, process_suffix: str) -> CheckResult:
    pid_path = Path(PBGDIR) / "data" / "pid" / pid_filename
    ok, pid_raw = _read_pid(pid_path)
    if not ok:
        return CheckResult(name, False, str(pid_raw))

    pid = int(pid_raw)  # type: ignore[arg-type]
    if not psutil.pid_exists(pid):
        return CheckResult(name, False, f"pid {pid} not running")

    try:
        cmdline = [segment.lower() for segment in psutil.Process(pid).cmdline()]
        if any(segment.endswith(process_suffix) for segment in cmdline):
            return CheckResult(name, True, f"running (pid {pid})")
        return CheckResult(name, False, f"pid {pid} running but command mismatch: {cmdline}")
    except psutil.Error as exc:
        return CheckResult(name, False, f"psutil error for pid {pid}: {exc}")


def run_health_checks(skip_services: bool = False) -> list[CheckResult]:
    results = [check_database()]
    if not skip_services:
        results.extend(check_service(*svc) for svc in SERVICE_DEFINITIONS)
    return results


def format_results(results: list[CheckResult]) -> str:
    lines = []
    for result in results:
        status = "OK" if result.ok else "FAIL"
        lines.append(f"{result.name}: {status} - {result.detail}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PBGUI health check CLI")
    parser.add_argument(
        "--skip-services",
        action="store_true",
        help="Skip daemon liveness checks (useful on development machines).",
    )
    args = parser.parse_args(argv)

    results = run_health_checks(skip_services=args.skip_services)
    print(format_results(results))
    return 0 if all(result.ok for result in results) else 1


if __name__ == "__main__":
    sys.exit(main())
