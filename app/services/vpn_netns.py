from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from uuid import UUID


class NetnsPermissionError(RuntimeError):
    """Raised when the container cannot create network namespaces."""


def netns_name_for_component(component_id: UUID) -> str:
    return f"sg-{str(component_id).split('-')[0]}"


def tun_name_for_component(component_id: UUID) -> str:
    # IFNAMSIZ is 15; "tun-" + 8 hex chars from UUID prefix fits.
    return f"tun-{str(component_id).split('-')[0]}"


def list_netns_names() -> set[str]:
    try:
        output = subprocess.check_output(["ip", "-json", "netns", "list"], text=True, timeout=5)
    except (subprocess.SubprocessError, FileNotFoundError):
        try:
            output = subprocess.check_output(["ip", "netns", "list"], text=True, timeout=5)
        except (subprocess.SubprocessError, FileNotFoundError):
            return set()
        return {line.split()[0] for line in output.splitlines() if line.strip()}

    import json

    try:
        rows = json.loads(output)
    except json.JSONDecodeError:
        return set()
    return {row["name"] for row in rows if isinstance(row, dict) and isinstance(row.get("name"), str)}


def ensure_netns(name: str) -> None:
    if name in list_netns_names():
        return
    try:
        subprocess.check_call(["ip", "netns", "add", name])
    except subprocess.CalledProcessError as exc:
        raise NetnsPermissionError(
            f"Failed to create network namespace {name!r} (exit {exc.returncode}). "
            "Persistent OpenVPN needs SYS_ADMIN plus security_opt apparmor:unconfined "
            "(Docker's default AppArmor denies mount used by `ip netns add`). "
            "Ephemeral OpenVPN checks only need NET_ADMIN + /dev/net/tun."
        ) from exc
    subprocess.check_call(["ip", "netns", "exec", name, "ip", "link", "set", "lo", "up"])
    ensure_netns_resolv(name)


def ensure_netns_resolv(name: str) -> None:
    """Provide DNS inside the netns for probes (ip netns exec bind-mounts this file)."""
    try:
        resolv_dir = Path("/etc/netns") / name
        resolv_dir.mkdir(parents=True, exist_ok=True)
        resolv_path = resolv_dir / "resolv.conf"
        if not resolv_path.exists():
            resolv_path.write_text("nameserver 1.1.1.1\nnameserver 8.8.8.8\n", encoding="utf-8")
    except OSError:
        # Non-fatal in restricted environments / unit tests.
        return


def delete_netns(name: str) -> None:
    subprocess.run(["ip", "netns", "delete", name], check=False, capture_output=True)
    try:
        resolv_dir = Path("/etc/netns") / name
        if resolv_dir.exists():
            for child in resolv_dir.iterdir():
                child.unlink(missing_ok=True)
            resolv_dir.rmdir()
    except OSError:
        return


def move_iface_to_netns(
    iface: str,
    netns: str,
    *,
    addresses: list[tuple[str, int]] | None = None,
    gateway: str | None = None,
) -> None:
    """Move a TUN into an isolated netns and restore addressing/routes.

    Moving a device can drop addresses (especially with subnet topology). We
    re-apply captured IPv4 addresses and install a default route for probes.
    """
    subprocess.check_call(["ip", "link", "set", "dev", iface, "netns", netns])
    subprocess.check_call(["ip", "netns", "exec", netns, "ip", "link", "set", iface, "up"])
    for local, prefixlen in addresses or []:
        subprocess.check_call(
            [
                "ip",
                "netns",
                "exec",
                netns,
                "ip",
                "addr",
                "replace",
                f"{local}/{prefixlen}",
                "dev",
                iface,
            ]
        )
    if gateway:
        subprocess.check_call(
            ["ip", "netns", "exec", netns, "ip", "route", "replace", "default", "via", gateway, "dev", iface]
        )


def netns_exec(name: str, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["ip", "netns", "exec", name, *cmd], **kwargs)


def netns_popen(name: str, cmd: list[str], **kwargs: Any) -> subprocess.Popen[Any]:
    return subprocess.Popen(["ip", "netns", "exec", name, *cmd], **kwargs)


def run_ip_command(args: list[str], *, netns: str | None = None, timeout: float = 5) -> str:
    cmd = ["ip", *args]
    if netns:
        cmd = ["ip", "netns", "exec", netns, "ip", *args]
    return subprocess.check_output(cmd, text=True, timeout=timeout)
