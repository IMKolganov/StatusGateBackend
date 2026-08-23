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
_PROTO_IN_NAME_RE = re.compile(r"\b(tcp|udp)\b", re.IGNORECASE)


def normalize_name(value: str) -> str:
    """Normalize display names for comparison (strips emoji and vpn/proto noise)."""
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


def _proto_from_name(value: str) -> str | None:
    match = _PROTO_IN_NAME_RE.search(value or "")
    return match.group(1).lower() if match else None


def server_proto(server: DataGateServer) -> str | None:
    proto = (server.proto or "").lower() or None
    if proto in {"tcp", "udp"}:
        return proto
    for tag in server.tags:
        if tag.lower() in ("tcp", "udp"):
            return tag.lower()
    return _proto_from_name(server.server_name)


def local_proto(component: LocalVpnComponent) -> str | None:
    endpoint = component.endpoint
    proto = (endpoint.get("proto") or "").lower() or None
    if proto in {"tcp", "udp"}:
        return proto
    return _proto_from_name(component.name) or _proto_from_name(component.slug)


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

    # OpenVPN tcp/udp are distinct services — never cross-link them.
    if server.check_type == "openvpn":
        sp = server_proto(server)
        lp = local_proto(component)
        if sp and lp and sp != lp:
            return 0.0, False

    score = 0.0
    endpoint_match = False

    sn = normalize_name(server.server_name)
    cn = normalize_name(component.name)
    slug_n = normalize_name(component.slug.replace("-", " "))
    if sn and cn:
        if sn == cn or sn == slug_n:
            score += 50
        elif sn in cn or cn in sn or sn in slug_n or slug_n in sn:
            score += 35
        else:
            s_tokens = set(sn.split())
            c_tokens = set(cn.split()) | set(slug_n.split())
            if s_tokens and c_tokens:
                overlap = len(s_tokens & c_tokens) / max(len(s_tokens | c_tokens), 1)
                score += 25 * overlap

    local_ep = component.endpoint
    if server.host and local_ep.get("host"):
        if server.host.lower() == str(local_ep["host"]).lower():
            score += 40
            endpoint_match = True
            if server.port is not None and local_ep.get("port") is not None:
                if int(server.port) == int(local_ep["port"]):
                    score += 15
        elif server.host.lower() in str(local_ep["host"]).lower() or str(local_ep["host"]).lower() in server.host.lower():
            score += 15
            endpoint_match = True

    if server.check_type == "openvpn":
        sp = server_proto(server)
        lp = local_proto(component)
        if sp and lp and sp == lp:
            score += 20

    return score, endpoint_match


MATCH_THRESHOLD = 40.0


def _name_differs(server: DataGateServer, component: LocalVpnComponent) -> bool:
    return server.server_name.strip() != component.name.strip()


def match_servers(
    servers: list[DataGateServer],
    components: list[LocalVpnComponent],
) -> PreviewBuckets:
    matched: list[MatchResult] = []
    used_component_ids: set[UUID] = set()
    used_server_ids: set[int] = set()
    batch_server_ids = {s.id for s in servers}

    by_dg_id = {c.datagate_server_id: c for c in components if c.datagate_server_id is not None}

    # 1) Keep existing links only when they are still compatible (same type/proto).
    for server in servers:
        component = by_dg_id.get(server.id)
        if component is None:
            continue
        score, endpoint_match = score_pair(server, component)
        if score < MATCH_THRESHOLD:
            # Stale wrong link (e.g. udp server pinned to tcp service) — free for rematch.
            continue
        matched.append(
            MatchResult(
                server=server,
                component=component,
                score=max(score, 100.0),
                name_differs=_name_differs(server, component),
                endpoint_match=endpoint_match,
                already_linked=True,
            )
        )
        used_component_ids.add(component.id)
        used_server_ids.add(server.id)

    def _component_available(component: LocalVpnComponent) -> bool:
        if component.id in used_component_ids:
            return False
        linked = component.datagate_server_id
        if linked is None:
            return True
        # Partial import: do not steal a component linked to a server outside this batch.
        if linked not in batch_server_ids:
            return False
        # Linked server is in-batch but did not claim this component (incompatible) → rematch ok.
        return linked not in used_server_ids

    # 2) Exact endpoint wins (host + port + proto) before fuzzy names.
    endpoint_candidates: list[tuple[float, bool, DataGateServer, LocalVpnComponent]] = []
    fuzzy_candidates: list[tuple[float, bool, DataGateServer, LocalVpnComponent]] = []
    for server in servers:
        if server.id in used_server_ids:
            continue
        for component in components:
            if not _component_available(component):
                continue
            score, endpoint_match = score_pair(server, component)
            if score < MATCH_THRESHOLD:
                continue
            row = (score, endpoint_match, server, component)
            if endpoint_match and score >= 70:
                endpoint_candidates.append(row)
            else:
                fuzzy_candidates.append(row)

    def _consume(candidates: list[tuple[float, bool, DataGateServer, LocalVpnComponent]]) -> None:
        candidates.sort(key=lambda row: row[0], reverse=True)
        for score, endpoint_match, server, component in candidates:
            if server.id in used_server_ids or component.id in used_component_ids:
                continue
            if not _component_available(component):
                continue
            matched.append(
                MatchResult(
                    server=server,
                    component=component,
                    score=score,
                    name_differs=_name_differs(server, component),
                    endpoint_match=endpoint_match,
                    already_linked=False,
                )
            )
            used_component_ids.add(component.id)
            used_server_ids.add(server.id)

    _consume(endpoint_candidates)
    _consume(fuzzy_candidates)

    new_servers = [s for s in servers if s.id not in used_server_ids]
    unmatched_local = [c for c in components if c.id not in used_component_ids]
    return PreviewBuckets(matched=matched, new_servers=new_servers, unmatched_local=unmatched_local)
