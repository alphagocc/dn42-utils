from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class DummyResult:
    created: bool
    skipped: bool
    backend: str
    warnings: list[str]


def detect_dummy_backend() -> str:
    if shutil.which("nmcli") is None:
        return "iproute2"
    try:
        subprocess.check_output(
            ["nmcli", "general", "status"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        return "nm"
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return "iproute2"


def _interface_exists(ifname: str) -> bool:
    try:
        subprocess.check_output(
            ["ip", "link", "show", ifname],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def _address_bound(ifname: str, addr_cidr: str) -> bool:
    addr_part = addr_cidr.split("/", 1)[0]
    try:
        output = subprocess.check_output(
            ["ip", "-6", "addr", "show", "dev", ifname],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        return addr_part in output
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def ensure_dummy_interface(own_ipv6: str) -> DummyResult:
    addr_cidr = f"{own_ipv6}/128"
    ifname = "dn42-dummy"
    warnings: list[str] = []

    if _interface_exists(ifname):
        if _address_bound(ifname, addr_cidr):
            return DummyResult(created=False, skipped=True, backend="", warnings=[])
        backend = detect_dummy_backend()
        try:
            if backend == "nm":
                subprocess.check_output(
                    ["nmcli", "connection", "modify", ifname, "ipv6.addresses", addr_cidr],
                    text=True,
                    stderr=subprocess.STDOUT,
                    timeout=10,
                )
                subprocess.check_output(
                    ["nmcli", "connection", "up", ifname],
                    text=True,
                    stderr=subprocess.STDOUT,
                    timeout=10,
                )
            else:
                subprocess.check_output(
                    ["ip", "addr", "add", addr_cidr, "dev", ifname],
                    text=True,
                    stderr=subprocess.STDOUT,
                    timeout=10,
                )
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            warnings.append(f"dn42-dummy 地址绑定失败: {exc}")
            return DummyResult(created=False, skipped=False, backend=backend, warnings=warnings)
        return DummyResult(created=True, skipped=False, backend=backend, warnings=warnings)

    backend = detect_dummy_backend()
    try:
        if backend == "nm":
            subprocess.check_output(
                [
                    "nmcli",
                    "connection",
                    "add",
                    "type",
                    "dummy",
                    "ifname",
                    ifname,
                    "ipv6.method",
                    "manual",
                    "ipv6.addresses",
                    addr_cidr,
                ],
                text=True,
                stderr=subprocess.STDOUT,
                timeout=10,
            )
        else:
            subprocess.check_output(
                ["ip", "link", "add", ifname, "type", "dummy"],
                text=True,
                stderr=subprocess.STDOUT,
                timeout=10,
            )
            subprocess.check_output(
                ["ip", "addr", "add", addr_cidr, "dev", ifname],
                text=True,
                stderr=subprocess.STDOUT,
                timeout=10,
            )
            subprocess.check_output(
                ["ip", "link", "set", ifname, "up"],
                text=True,
                stderr=subprocess.STDOUT,
                timeout=10,
            )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        warnings.append(f"dn42-dummy 创建失败: {exc}")
        return DummyResult(created=False, skipped=False, backend=backend, warnings=warnings)

    return DummyResult(created=True, skipped=False, backend=backend, warnings=warnings)
