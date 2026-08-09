"""Continuous in-tunnel ping sampler.

The regular check cycle pings the gateway for ~4 seconds out of every 60, so
short stalls (the "YouTube freezes for 10 seconds" class of problem) slip
through unseen. This sampler runs alongside a persistent VPN session and pings
continuously — one packet per second — aggregating each minute into a
`tunnel_ping_samples` row per target:

- ``gateway``:  the first hop inside the tunnel (tunnel-path health);
- ``internet``: a host beyond the exit (catches degradation past the gateway,
  e.g. bad peering from the VPN datacenter toward Google).
"""

from __future__ import annotations

import logging
import re
import signal
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from app.core.probe_defaults import internet_ping_host
from app.database import SessionLocal
from app.models.tunnel_ping_sample import GATEWAY_TARGET, INTERNET_TARGET, TunnelPingSample

logger = logging.getLogger(__name__)
SAMPLE_WINDOW_SECONDS = 60
# 55 one-second pings leave headroom for parsing + persisting within the minute.
PINGS_PER_WINDOW = 55
RETENTION = timedelta(hours=48)

_COUNTS_RE = re.compile(r"(\d+) packets transmitted, (\d+) (?:packets )?received")
_RTT_RE = re.compile(r"rtt min/avg/max/mdev = ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)")


def parse_ping_aggregate(output: str) -> dict[str, Any] | None:
    counts = _COUNTS_RE.search(output)
    if not counts:
        return None
    sent = int(counts.group(1))
    received = int(counts.group(2))
    if sent <= 0:
        return None

    result: dict[str, Any] = {
        "samples_sent": sent,
        "samples_received": received,
        "loss_percent": round(100.0 * (sent - received) / sent, 2),
    }
    rtt = _RTT_RE.search(output)
    if rtt:
        result["min_ms"] = float(rtt.group(1))
        result["avg_ms"] = float(rtt.group(2))
        result["max_ms"] = float(rtt.group(3))
        result["jitter_ms"] = float(rtt.group(4))
    return result


class TunnelPingSampler(threading.Thread):
    def __init__(
        self,
        component_id: UUID,
        *,
        netns: str,
        gateway: str | None,
        stop_event: threading.Event | None = None,
    ) -> None:
        super().__init__(name=f"tunnel-ping-{component_id}", daemon=True)
        self._component_id = component_id
        self._netns = netns
        self._gateway = gateway
        self._stop = stop_event or threading.Event()

    def stop(self) -> None:
        self._stop.set()
        self.join(timeout=10)

    def run(self) -> None:
        logger.info(
            "Continuous tunnel ping started for component %s (gateway=%s, internet=%s)",
            self._component_id,
            self._gateway,
            internet_ping_host(),
        )
        while not self._stop.is_set():
            window_started = datetime.now(UTC)
            try:
                rows = self._sample_window(window_started)
                if rows:
                    self._persist(rows)
            except Exception:
                logger.exception(
                    "Continuous tunnel ping window failed for component %s", self._component_id
                )
            elapsed = (datetime.now(UTC) - window_started).total_seconds()
            remaining = SAMPLE_WINDOW_SECONDS - elapsed
            if remaining > 0 and self._stop.wait(remaining):
                break
        logger.info("Continuous tunnel ping stopped for component %s", self._component_id)

    def _targets(self) -> list[tuple[str, str]]:
        targets = [(INTERNET_TARGET, internet_ping_host())]
        if self._gateway:
            targets.insert(0, (GATEWAY_TARGET, self._gateway))
        return targets

    def _sample_window(self, bucket_start: datetime) -> list[TunnelPingSample]:
        procs: list[tuple[str, str, subprocess.Popen[str]]] = []
        for target, host in self._targets():
            cmd = [
                "ip",
                "netns",
                "exec",
                self._netns,
                "ping",
                "-i",
                "1",
                "-c",
                str(PINGS_PER_WINDOW),
                "-W",
                "1",
                host,
            ]
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
            except OSError:
                logger.exception("Failed to start ping to %s in netns %s", host, self._netns)
                continue
            procs.append((target, host, proc))

        deadline = bucket_start + timedelta(seconds=PINGS_PER_WINDOW + 5)
        while any(proc.poll() is None for _, _, proc in procs):
            if self._stop.is_set() or datetime.now(UTC) >= deadline:
                for _, _, proc in procs:
                    if proc.poll() is None:
                        # SIGINT makes ping print its statistics before exiting.
                        proc.send_signal(signal.SIGINT)
                break
            self._stop.wait(0.5)

        rows: list[TunnelPingSample] = []
        for target, host, proc in procs:
            try:
                output, _ = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                output, _ = proc.communicate()
            aggregate = parse_ping_aggregate(output or "")
            if aggregate is None:
                continue
            rows.append(
                TunnelPingSample(
                    monitored_component_id=self._component_id,
                    bucket_start=bucket_start.replace(microsecond=0),
                    target=target,
                    target_host=host,
                    **aggregate,
                )
            )
        return rows

    def _persist(self, rows: list[TunnelPingSample]) -> None:
        cutoff = datetime.now(UTC) - RETENTION
        with SessionLocal() as session:
            for row in rows:
                session.add(row)
            session.query(TunnelPingSample).filter(
                TunnelPingSample.monitored_component_id == self._component_id,
                TunnelPingSample.bucket_start < cutoff,
            ).delete(synchronize_session=False)
            session.commit()
