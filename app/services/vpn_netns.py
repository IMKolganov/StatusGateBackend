from __future__ import annotations

import subprocess
from typing import Any
from uuid import UUID


class NetnsPermissionError(RuntimeError):
    """Raised when the container cannot create network namespaces."""


def netns_name_for_component(component_id: UUID) -> str:
    return f"sg-{str(component_id).split('-')[0]}"


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


def delete_netns(name: str) -> None:
    subprocess.run(["ip", "netns", "delete", name], check=False, capture_output=True)


def netns_exec(name: str, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["ip", "netns", "exec", name, *cmd], **kwargs)


def netns_popen(name: str, cmd: list[str], **kwargs: Any) -> subprocess.Popen[Any]:
    return subprocess.Popen(["ip", "netns", "exec", name, *cmd], **kwargs)


def run_ip_command(args: list[str], *, netns: str | None = None, timeout: float = 5) -> str:
    cmd = ["ip", *args]
    if netns:
        cmd = ["ip", "netns", "exec", netns, "ip", *args]
    return subprocess.check_output(cmd, text=True, timeout=timeout)
