from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from dn42ctl.constants import FILE_MODE_NETDEV
from dn42ctl.fs import chmod_best_effort
from dn42ctl.render import render_dummy_netdev, render_dummy_network


DUMMY_IFNAME = "dn42-dummy"


@dataclass(frozen=True)
class DummyResult:
    created: bool
    skipped: bool
    backend: str
    warnings: list[str]


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


def _ensure_networkd(own_ipv6: str, networkd_dir: str) -> DummyResult:
    netdev_path = Path(networkd_dir) / f"{DUMMY_IFNAME}.netdev"
    network_path = Path(networkd_dir) / f"{DUMMY_IFNAME}.network"

    netdev_content = render_dummy_netdev()
    network_content = render_dummy_network(own_ipv6=own_ipv6)

    existing_netdev = netdev_path.read_text() if netdev_path.exists() else None
    existing_network = network_path.read_text() if network_path.exists() else None

    if existing_netdev == netdev_content and existing_network == network_content:
        return DummyResult(created=False, skipped=True, backend="networkd", warnings=[])

    warnings: list[str] = []
    try:
        netdev_path.write_text(netdev_content)
        chmod_best_effort(netdev_path, FILE_MODE_NETDEV)
        network_path.write_text(network_content)
        chmod_best_effort(network_path, FILE_MODE_NETDEV)
    except OSError as exc:
        warnings.append(f"dn42-dummy networkd 配置写入失败: {exc}")
        return DummyResult(created=False, skipped=False, backend="networkd", warnings=warnings)

    try:
        subprocess.check_output(
            ["networkctl", "reload"],
            text=True,
            stderr=subprocess.STDOUT,
            timeout=10,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        warnings.append(f"networkctl reload 失败: {exc}")

    return DummyResult(created=True, skipped=False, backend="networkd", warnings=warnings)


def _ensure_nm(own_ipv6: str) -> DummyResult:
    addr_cidr = f"{own_ipv6}/128"
    warnings: list[str] = []

    if _interface_exists(DUMMY_IFNAME):
        if _address_bound(DUMMY_IFNAME, addr_cidr):
            return DummyResult(created=False, skipped=True, backend="nm", warnings=[])
        try:
            subprocess.check_output(
                ["nmcli", "connection", "modify", DUMMY_IFNAME, "ipv6.addresses", addr_cidr],
                text=True,
                stderr=subprocess.STDOUT,
                timeout=10,
            )
            subprocess.check_output(
                ["nmcli", "connection", "up", DUMMY_IFNAME],
                text=True,
                stderr=subprocess.STDOUT,
                timeout=10,
            )
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            warnings.append(f"dn42-dummy 地址绑定失败: {exc}")
            return DummyResult(created=False, skipped=False, backend="nm", warnings=warnings)
        return DummyResult(created=True, skipped=False, backend="nm", warnings=warnings)

    try:
        subprocess.check_output(
            [
                "nmcli",
                "connection",
                "add",
                "type",
                "dummy",
                "ifname",
                DUMMY_IFNAME,
                "ipv6.method",
                "manual",
                "ipv6.addresses",
                addr_cidr,
            ],
            text=True,
            stderr=subprocess.STDOUT,
            timeout=10,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        warnings.append(f"dn42-dummy 创建失败: {exc}")
        return DummyResult(created=False, skipped=False, backend="nm", warnings=warnings)

    return DummyResult(created=True, skipped=False, backend="nm", warnings=warnings)


def ensure_dummy_interface(own_ipv6: str, *, backend: str, networkd_dir: str) -> DummyResult:
    if backend == "networkd":
        return _ensure_networkd(own_ipv6, networkd_dir)
    return _ensure_nm(own_ipv6)
