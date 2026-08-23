"""HTTP client for DataGate Monitor API (https://api.datagateapp.com)."""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

SERVER_TYPE_OPENVPN = 0
SERVER_TYPE_XRAY = 1


class DataGateApiError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class DataGateServer:
    id: int
    server_type: int
    server_name: str
    api_url: str | None = None
    is_online: bool = False
    is_disabled: bool = False
    tags: list[str] = field(default_factory=list)
    host: str | None = None
    port: int | None = None
    proto: str | None = None

    @property
    def check_type(self) -> str:
        return "xray" if self.server_type == SERVER_TYPE_XRAY else "openvpn"


@dataclass
class _TokenState:
    token: str
    expires_at: datetime


class DataGateClient:
    def __init__(
        self,
        base_url: str,
        client_id: str,
        client_secret: str,
        *,
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self._timeout = timeout
        self._transport = transport
        self._token: _TokenState | None = None

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            timeout=self._timeout,
            transport=self._transport,
            follow_redirects=False,
        )

    def _unwrap(self, payload: Any) -> Any:
        if not isinstance(payload, dict):
            raise DataGateApiError("Unexpected DataGate response shape")
        if payload.get("success") is False:
            raise DataGateApiError(str(payload.get("message") or "DataGate request failed"))
        if "data" in payload:
            return payload["data"]
        return payload

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        auth: bool = True,
    ) -> Any:
        headers: dict[str, str] = {}
        if auth:
            headers["Authorization"] = f"Bearer {self.get_token()}"
        with self._client() as client:
            response = client.request(method, path, json=json, headers=headers)
        try:
            body = response.json()
        except Exception as exc:  # noqa: BLE001
            raise DataGateApiError(
                f"DataGate returned non-JSON ({response.status_code})",
                status_code=response.status_code,
            ) from exc
        if response.status_code >= 400:
            message = body.get("message") if isinstance(body, dict) else None
            raise DataGateApiError(
                str(message or f"DataGate HTTP {response.status_code}"),
                status_code=response.status_code,
            )
        return self._unwrap(body)

    def get_token(self) -> str:
        now = datetime.now(timezone.utc)
        if self._token and self._token.expires_at > now:
            return self._token.token
        data = self._request(
            "POST",
            "/api/auth/token",
            json={"clientId": self.client_id, "clientSecret": self.client_secret},
            auth=False,
        )
        if not isinstance(data, dict) or not data.get("token"):
            raise DataGateApiError("DataGate token response missing token")
        expires_raw = data.get("expiration")
        if expires_raw:
            try:
                expires_at = datetime.fromisoformat(str(expires_raw).replace("Z", "+00:00"))
            except ValueError:
                expires_at = now
        else:
            expires_at = now
        # Refresh 2 minutes before expiry.
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        self._token = _TokenState(token=str(data["token"]), expires_at=expires_at - timedelta(minutes=2))
        return self._token.token

    def list_servers(self) -> list[DataGateServer]:
        data = self._request("GET", "/api/v3/open-vpn-servers/get-all")
        raw_servers = []
        if isinstance(data, dict):
            raw_servers = data.get("vpnServers") or data.get("vpn_servers") or []
        elif isinstance(data, list):
            raw_servers = data
        servers: list[DataGateServer] = []
        for item in raw_servers:
            if not isinstance(item, dict):
                continue
            servers.append(
                DataGateServer(
                    id=int(item["id"]),
                    server_type=int(item.get("serverType", SERVER_TYPE_OPENVPN)),
                    server_name=str(item.get("serverName") or f"server-{item['id']}"),
                    api_url=item.get("apiUrl"),
                    is_online=bool(item.get("isOnline", False)),
                    is_disabled=bool(item.get("isDisabled", False)),
                    tags=[str(t) for t in (item.get("tags") or [])],
                    host=_host_from_api_url(item.get("apiUrl")),
                )
            )
        return servers

    def get_export_config(self, vpn_server_id: int) -> dict[str, Any] | None:
        try:
            data = self._request("GET", f"/api/open-vpn-configs/get/{vpn_server_id}")
        except DataGateApiError as exc:
            if exc.status_code in (404, 400):
                return None
            raise
        if not isinstance(data, dict):
            return None
        return data

    def enrich_server(self, server: DataGateServer) -> DataGateServer:
        """Fill host/port/proto from export config template and tags."""
        cfg = self.get_export_config(server.id)
        if cfg:
            host = cfg.get("vpnServerIp") or cfg.get("vpn_server_ip")
            port = cfg.get("vpnServerPort") or cfg.get("vpn_server_port")
            template = cfg.get("configTemplate") or cfg.get("config_template") or ""
            if host:
                server.host = str(host)
            if port is not None:
                try:
                    server.port = int(port)
                except (TypeError, ValueError):
                    pass
            parsed = parse_ovpn_endpoint(str(template))
            if parsed["host"] and not server.host:
                server.host = parsed["host"]
            if parsed["port"] is not None and server.port is None:
                server.port = parsed["port"]
            if parsed["proto"]:
                server.proto = parsed["proto"]
        if not server.proto:
            for tag in server.tags:
                low = tag.lower()
                if low in ("tcp", "udp"):
                    server.proto = low
                    break
        if not server.host:
            server.host = _host_from_api_url(server.api_url)
        return server

    def list_issued_files(self, vpn_server_id: int, *, xray: bool) -> list[dict[str, Any]]:
        base = "/api/xray-client-links" if xray else "/api/open-vpn-files"
        data = self._request("GET", f"{base}/get-all/{vpn_server_id}")
        if isinstance(data, dict):
            files = data.get("issuedOvpnFiles") or data.get("issued_ovpn_files") or data.get("files") or []
            return [f for f in files if isinstance(f, dict)]
        if isinstance(data, list):
            return [f for f in data if isinstance(f, dict)]
        return []

    def issue_file(
        self,
        *,
        vpn_server_id: int,
        common_name: str,
        external_id: str,
        xray: bool,
        expire_days: int = 3650,
    ) -> dict[str, Any]:
        base = "/api/xray-client-links" if xray else "/api/open-vpn-files"
        data = self._request(
            "POST",
            f"{base}/add",
            json={
                "externalId": external_id,
                "commonName": common_name,
                "vpnServerId": vpn_server_id,
                "issuedTo": "statusgate",
                "ovpnFileExpireDays": expire_days,
            },
        )
        if isinstance(data, dict):
            issued = data.get("issuedOvpnFile") or data.get("issued_ovpn_file") or data
            if isinstance(issued, dict):
                return issued
        raise DataGateApiError("Unexpected issue-file response")

    def download_by_cn(self, *, vpn_server_id: int, common_name: str, xray: bool) -> str:
        base = "/api/xray-client-links" if xray else "/api/open-vpn-files"
        data = self._request(
            "POST",
            f"{base}/download-file-by-cn",
            json={"vpnServerId": vpn_server_id, "commonName": common_name},
        )
        return _decode_file_content(data)

    def download_by_id(self, *, vpn_server_id: int, issued_file_id: int, xray: bool) -> str:
        base = "/api/xray-client-links" if xray else "/api/open-vpn-files"
        data = self._request(
            "POST",
            f"{base}/download-file",
            json={"vpnServerId": vpn_server_id, "issuedOvpnFileId": issued_file_id},
        )
        return _decode_file_content(data)

    def ensure_config_text(
        self,
        *,
        vpn_server_id: int,
        common_name: str,
        external_id: str,
        xray: bool,
    ) -> str:
        """Download existing CN config or issue a new one then download."""
        try:
            return self.download_by_cn(vpn_server_id=vpn_server_id, common_name=common_name, xray=xray)
        except DataGateApiError as exc:
            # Only treat not-found style failures as "issue a new CN".
            if exc.status_code not in (404, 400):
                raise
            logger.info("CN %s missing on server %s — issuing", common_name, vpn_server_id)

        files = self.list_issued_files(vpn_server_id, xray=xray)
        for item in files:
            cn = str(item.get("commonName") or item.get("common_name") or "")
            if cn == common_name and not item.get("isRevoked", False):
                file_id = item.get("id")
                if file_id is not None:
                    return self.download_by_id(
                        vpn_server_id=vpn_server_id,
                        issued_file_id=int(file_id),
                        xray=xray,
                    )

        issued = self.issue_file(
            vpn_server_id=vpn_server_id,
            common_name=common_name,
            external_id=external_id,
            xray=xray,
        )
        file_id = issued.get("id")
        if file_id is not None:
            try:
                return self.download_by_id(
                    vpn_server_id=vpn_server_id,
                    issued_file_id=int(file_id),
                    xray=xray,
                )
            except DataGateApiError:
                pass
        return self.download_by_cn(vpn_server_id=vpn_server_id, common_name=common_name, xray=xray)


def _host_from_api_url(api_url: str | None) -> str | None:
    if not api_url:
        return None
    parsed = urlparse(api_url if "://" in api_url else f"https://{api_url}")
    return parsed.hostname


def parse_ovpn_endpoint(config_text: str) -> dict[str, Any]:
    """Extract proto / remote host / port from OpenVPN config or Xray share text."""
    host: str | None = None
    port: int | None = None
    proto: str | None = None
    for raw_line in config_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        parts = line.split()
        if not parts:
            continue
        key = parts[0].lower()
        if key == "proto" and len(parts) >= 2:
            proto = parts[1].lower()
        elif key == "remote" and len(parts) >= 2:
            host = parts[1]
            if len(parts) >= 3:
                try:
                    port = int(parts[2])
                except ValueError:
                    pass
        elif key in ("address", "server") and len(parts) >= 2 and host is None:
            host = parts[1].split(":")[0]
    # vless://uuid@host:port?...
    if host is None and "://" in config_text:
        try:
            parsed = urlparse(config_text.strip().split()[0])
            if parsed.hostname:
                host = parsed.hostname
            if parsed.port:
                port = parsed.port
        except Exception:  # noqa: BLE001
            pass
    return {"host": host, "port": port, "proto": proto}


def _decode_file_content(data: Any) -> str:
    if not isinstance(data, dict):
        raise DataGateApiError("Unexpected download response")
    content = data.get("content")
    if content is None:
        raise DataGateApiError("Download response missing content")
    if isinstance(content, str):
        # ASP.NET typically base64-encodes byte[].
        try:
            raw = base64.b64decode(content)
            return raw.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return content
    if isinstance(content, list):
        return bytes(int(b) & 0xFF for b in content).decode("utf-8", errors="replace")
    raise DataGateApiError("Unsupported download content type")
