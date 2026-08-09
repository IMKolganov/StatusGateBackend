"""Process/log helpers shared by OpenVPN and Xray checks."""

from __future__ import annotations

import os
import re
import signal
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.models.check_result import CheckResult
from app.models.enums import CheckOutcome
from app.models.monitored_component import MonitoredComponent

def _terminate_process(proc: subprocess.Popen[Any], pid_path: Path | None = None) -> None:
    if pid_path and pid_path.exists():
        try:
            os.kill(int(pid_path.read_text(encoding="utf-8").strip()), signal.SIGTERM)
        except (OSError, ValueError):
            pass
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                proc.kill()


def _vpn_log_hint(log_tail: str | None) -> str | None:
    if not log_tail:
        return None

    for line in reversed(log_tail.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        upper = stripped.upper()
        if "AUTH_FAILED" in upper:
            return "Authentication failed (AUTH_FAILED)"
        if "TLS ERROR" in upper:
            return stripped
        if "CANNOT RESOLVE" in upper:
            return stripped
        if "CONNECTION REFUSED" in upper:
            return stripped
        if "INACTIVITY TIMEOUT" in upper:
            return stripped
        if "VERIFY ERROR" in upper or "CERTIFICATE VERIFY FAILED" in upper:
            return stripped
        if any(marker in upper for marker in (" ERROR", " FATAL", " EXITING")):
            return stripped
    return None


def _read_tail(path: Path, max_chars: int = 4000) -> str | None:
    if not path.exists():
        return None
    content = path.read_text(encoding="utf-8", errors="replace")
    if not content:
        return None
    return content[-max_chars:]


def _mask_proxy(proxy_url: str) -> str:
    return re.sub(r"://([^:@/]+):([^@/]+)@", "://***:***@", proxy_url)


def _error_result(
    component: MonitoredComponent,
    message: str,
    *,
    latency_ms: int | None = None,
    log_tail: str | None = None,
) -> CheckResult:
    details: dict[str, Any] = {"check_type": component.check_type}
    if log_tail:
        details["log_tail"] = log_tail
    return CheckResult(
        monitored_component_id=component.id,
        checked_at=datetime.now(UTC),
        outcome=CheckOutcome.ERROR.value,
        latency_ms=latency_ms,
        http_status_code=None,
        error_message=message,
        details=details,
    )


