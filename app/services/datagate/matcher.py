"""Match DataGate VPN servers to StatusGate monitored components."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from uuid import UUID

from app.services.datagate.client import DataGateServer, parse_ovpn_endpoint

_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001F9FF"
    "\U00002600-\U000027BF"
    "\U0001F1E0-\U0001F1FF"
    "]+",
    flags=re.UNICODE,
)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def normalize_name(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = _EMOJI_RE.sub(" ", text)
    text = text.lower()
    for token in ("openvpn", "xray", "vpn", "tcp", "udp", "api"):
        text = re.sub(rf"\b{token}\b", " ", text)
    text = _NON_ALNUM_RE.sub(" ", text)
    return " ".join(text.split())


def monitor_common_name(prefix: str, project_slug: str, server_id: int) -> str:
    safe_prefix = re.sub(r"[^a-zA-Z0-9_-]+", "-", prefix.strip()) or "statusgate"
    safe_slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", project_slug.strip()) or "project"
    return f"{safe_prefix}-{safe_slug}-{server_id}"


@dataclass
class LocalVpnComponent:
    id: UUID
    name: str
    slug: str
    check_type: str
    config_text: str | None = None
    datagate_server_id: int | None = None
    datagate_common_name: str | None = None

    @property
    def endpoint(self) -> dict:
        return parse_ovpn_endpoint(self.config_text or "")


@dataclass
class MatchResult:
    server: DataGateServer
    component: LocalVpnComponent
    score: float
    name_differs: bool
    endpoint_match: bool
    already_linked: bool


@dataclass
class PreviewBuckets:
    matched: list[MatchResult]
    new_servers: list[DataGateServer]
    unmatched_local: list[LocalVpnComponent]


def score_pair(server: DataGateServer, component: LocalVpnComponent) -> tuple[float, bool]:
    """Return (score, endpoint_match). Score 0 means incompatible."""
    if server.check_type != component.check_type:
        return 0.0, False

    score = 0.0
    endpoint_match = False

    sn = normalize_name(server.server_name)
    cn = normalize_name(component.name)
    if sn and cn:
        if sn == cn:
            score += 50
        elif sn in cn or cn in sn:
            score += 35
        else:
            s_tokens = set(sn.split())
            c_tokens = set(cn.split())
            if s_tokens and c_tokens:
                overlap = len(s_tokens & c_tokens) / max(len(s_tokens | c_tokens), 1)
                score += 25 * overlap

    local_ep = component.endpoint
    if server.host and local_ep.get("host"):
        if server.host.lower() == str(local_ep["host"]).lower():
            score += 30
            endpoint_match = True
            if server.port is not None and local_ep.get("port") is not None:
                if int(server.port) == int(local_ep["port"]):
                    score += 10
        elif server.host.lower() in str(local_ep["host"]).lower() or str(local_ep["host"]).lower() in server.host.lower():
            score += 15
            endpoint_match = True

    if server.check_type == "openvpn":
        server_proto = (server.proto or "").lower() or None
        local_proto = (local_ep.get("proto") or "").lower() or None
        if not server_proto:
            for tag in server.tags:
                if tag.lower() in ("tcp", "udp"):
                    server_proto = tag.lower()
                    break
        if server_proto and local_proto:
            if server_proto == local_proto:
                score += 15
            else:
                score -= 20
        # Soft hint from component name containing tcp/udp
        name_lower = component.name.lower()
        if server_proto == "tcp" and "tcp" in name_lower:
            score += 5
        if server_proto == "udp" and "udp" in name_lower:
            score += 5

    return score, endpoint_match


MATCH_THRESHOLD = 40.0


def match_servers(
    servers: list[DataGateServer],
    components: list[LocalVpnComponent],
) -> PreviewBuckets:
    matched: list[MatchResult] = []
    used_component_ids: set[UUID] = set()
    used_server_ids: set[int] = set()

    # 1) Already linked
    by_dg_id = {c.datagate_server_id: c for c in components if c.datagate_server_id is not None}
    for server in servers:
        component = by_dg_id.get(server.id)
        if component is None:
            continue
        _score, endpoint_match = score_pair(server, component)
        matched.append(
            MatchResult(
                server=server,
                component=component,
                score=100.0,
                name_differs=server.server_name.strip() != component.name.strip(),
                endpoint_match=endpoint_match,
                already_linked=True,
            )
        )
        used_component_ids.add(component.id)
        used_server_ids.add(server.id)

    # 2) Best unique fuzzy matches
    candidates: list[tuple[float, bool, DataGateServer, LocalVpnComponent]] = []
    for server in servers:
        if server.id in used_server_ids:
            continue
        for component in components:
            if component.id in used_component_ids:
                continue
            score, endpoint_match = score_pair(server, component)
            if score >= MATCH_THRESHOLD:
                candidates.append((score, endpoint_match, server, component))

    candidates.sort(key=lambda row: row[0], reverse=True)
    for score, endpoint_match, server, component in candidates:
        if server.id in used_server_ids or component.id in used_component_ids:
            continue
        matched.append(
            MatchResult(
                server=server,
                component=component,
                score=score,
                name_differs=server.server_name.strip() != component.name.strip(),
                endpoint_match=endpoint_match,
                already_linked=False,
            )
        )
        used_component_ids.add(component.id)
        used_server_ids.add(server.id)

    new_servers = [s for s in servers if s.id not in used_server_ids]
    unmatched_local = [c for c in components if c.id not in used_component_ids]
    return PreviewBuckets(matched=matched, new_servers=new_servers, unmatched_local=unmatched_local)
