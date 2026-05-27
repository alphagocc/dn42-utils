from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from dn42ctl.config import AppConfig
from dn42ctl.render import render_systemd_roa_service, render_systemd_roa_timer
from dn42ctl.services.core import Dn42CtlError, write_text

_FIREWALLD_CONF = Path("/etc/firewalld/firewalld.conf")
_NFT_RULE_PATH = Path("/etc/nftables/dn42-no-conntrack.nft")
_NFT_CONF_CANDIDATES = [
    Path("/etc/sysconfig/nftables.conf"),
    Path("/etc/nftables.conf"),
]
_NFT_INCLUDE_LINE = f'include "{_NFT_RULE_PATH}"'
_NFT_TABLE_NAME = "inet dn42_notrack"

_SYSTEMD_UNIT_DIR = Path("/etc/systemd/system")
_ROA_SERVICE_NAME = "dn42-roa-v6.service"
_ROA_TIMER_NAME = "dn42-roa-v6.timer"
_ROA_URL = "https://dn42.burble.com/roa/dn42_roa_bird2_6.conf"

_NFT_RULES = """\
table inet dn42_notrack {
    chain prerouting {
        type filter hook prerouting priority raw; policy accept;
        iifname { "dn42*", "wg*" } notrack
    }

    chain output {
        type filter hook output priority raw; policy accept;
        oifname { "dn42*", "wg*" } notrack
    }
}
"""


@dataclass(frozen=True)
class SystemInstallResult:
    component: str
    action: str
    changed_files: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _run(cmd: list[str]) -> None:
    try:
        subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT)  # noqa: S603
    except FileNotFoundError as exc:
        raise Dn42CtlError(f"命令不存在: {cmd[0]}") from exc
    except subprocess.CalledProcessError as exc:
        out = (exc.output or "").strip()
        raise Dn42CtlError(f"命令失败: {' '.join(cmd)} (exit={exc.returncode}) {out}") from exc


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise Dn42CtlError(f"文件不存在: {path}") from exc
    except PermissionError as exc:
        raise Dn42CtlError(f"权限不足: 无法读取 {path}") from exc
    except OSError as exc:
        raise Dn42CtlError(f"读取失败: {path} ({exc})") from exc


def _write_text_raw(path: Path, content: str) -> None:
    try:
        path.write_text(content, encoding="utf-8", newline="\n")
    except PermissionError as exc:
        raise Dn42CtlError(f"权限不足: 无法写入 {path}") from exc
    except OSError as exc:
        raise Dn42CtlError(f"写入失败: {path} ({exc})") from exc


def _set_firewalld_rpfilter(value: str) -> SystemInstallResult:
    action = "install" if value == "no" else "uninstall"
    content = _read_text(_FIREWALLD_CONF)

    pattern = re.compile(r"^(IPv6_rpfilter\s*=\s*).*$", re.MULTILINE)
    match = pattern.search(content)
    if match is None:
        raise Dn42CtlError(f"未在 {_FIREWALLD_CONF} 中找到 IPv6_rpfilter 设置")

    current = match.group(0).split("=", 1)[1].strip()
    warnings: list[str] = []
    changed: list[str] = []

    if current == value:
        warnings.append(f"IPv6_rpfilter 已经是 {value}，跳过修改")
    else:
        new_content = pattern.sub(f"IPv6_rpfilter={value}", content)
        _write_text_raw(_FIREWALLD_CONF, new_content)
        changed.append(str(_FIREWALLD_CONF))

    _run(["systemctl", "restart", "firewalld"])

    return SystemInstallResult(
        component="firewalld-conf",
        action=action,
        changed_files=changed,
        warnings=warnings,
    )


def install_firewalld_conf() -> SystemInstallResult:
    return _set_firewalld_rpfilter("no")


def uninstall_firewalld_conf() -> SystemInstallResult:
    return _set_firewalld_rpfilter("yes")


def _find_nftables_conf() -> Path:
    for candidate in _NFT_CONF_CANDIDATES:
        if candidate.exists():
            return candidate
    paths = ", ".join(str(p) for p in _NFT_CONF_CANDIDATES)
    raise Dn42CtlError(f"未找到 nftables.conf（已检查: {paths}）")


def install_nftables_conf() -> SystemInstallResult:
    changed: list[str] = []
    warnings: list[str] = []

    write_text(_NFT_RULE_PATH, _NFT_RULES)
    changed.append(str(_NFT_RULE_PATH))

    nft_conf_path = _find_nftables_conf()
    content = _read_text(nft_conf_path)

    if _NFT_INCLUDE_LINE in content:
        warnings.append(f"include 行已存在于 {nft_conf_path}，跳过添加")
    else:
        suffix = "\n" if not content.endswith("\n") else ""
        new_content = content + suffix + _NFT_INCLUDE_LINE + "\n"
        _write_text_raw(nft_conf_path, new_content)
        changed.append(str(nft_conf_path))

    _run(["systemctl", "enable", "nftables"])
    _run(["nft", "-f", str(_NFT_RULE_PATH)])

    return SystemInstallResult(
        component="nftables-conf",
        action="install",
        changed_files=changed,
        warnings=warnings,
    )


def uninstall_nftables_conf() -> SystemInstallResult:
    changed: list[str] = []
    warnings: list[str] = []

    try:
        _run(["nft", "delete", "table", *_NFT_TABLE_NAME.split()])
    except Dn42CtlError:
        warnings.append("nft delete table 失败（可能表不存在）")

    if _NFT_RULE_PATH.exists():
        try:
            _NFT_RULE_PATH.unlink()
            changed.append(str(_NFT_RULE_PATH))
        except PermissionError as exc:
            raise Dn42CtlError(f"权限不足: 无法删除 {_NFT_RULE_PATH}") from exc
        except OSError as exc:
            raise Dn42CtlError(f"删除失败: {_NFT_RULE_PATH} ({exc})") from exc
    else:
        warnings.append(f"{_NFT_RULE_PATH} 不存在，跳过删除")

    try:
        nft_conf_path = _find_nftables_conf()
        content = _read_text(nft_conf_path)
        if _NFT_INCLUDE_LINE in content:
            new_content = content.replace(_NFT_INCLUDE_LINE + "\n", "")
            new_content = new_content.replace(_NFT_INCLUDE_LINE, "")
            _write_text_raw(nft_conf_path, new_content)
            changed.append(str(nft_conf_path))
        else:
            warnings.append(f"未在 {nft_conf_path} 中找到 include 行，跳过")
    except Dn42CtlError as exc:
        warnings.append(str(exc))

    return SystemInstallResult(
        component="nftables-conf",
        action="uninstall",
        changed_files=changed,
        warnings=warnings,
    )


def install_roa_service(*, config: AppConfig) -> SystemInstallResult:
    changed: list[str] = []
    warnings: list[str] = []

    if not shutil.which("systemctl"):
        raise Dn42CtlError("systemctl 不可用，无法安装 ROA systemd timer")

    if shutil.which("curl") is None:
        warnings.append("未找到 curl：ROA 定时更新可能失败")

    roa_target = Path(config.bird_roa_v6_conf_path)
    roa_parent = roa_target.parent

    service_path = _SYSTEMD_UNIT_DIR / _ROA_SERVICE_NAME
    timer_path = _SYSTEMD_UNIT_DIR / _ROA_TIMER_NAME

    service_text = render_systemd_roa_service(
        roa_parent=roa_parent,
        roa_target=roa_target,
        roa_url=_ROA_URL,
    )
    timer_text = render_systemd_roa_timer()

    write_text(service_path, service_text)
    changed.append(str(service_path))
    write_text(timer_path, timer_text)
    changed.append(str(timer_path))

    _run(["systemctl", "daemon-reload"])
    _run(["systemctl", "enable", "--now", _ROA_TIMER_NAME])
    _run(["systemctl", "start", _ROA_SERVICE_NAME])

    return SystemInstallResult(
        component="roa-service",
        action="install",
        changed_files=changed,
        warnings=warnings,
    )


def uninstall_roa_service() -> SystemInstallResult:
    changed: list[str] = []
    warnings: list[str] = []

    if not shutil.which("systemctl"):
        raise Dn42CtlError("systemctl 不可用")

    try:
        _run(["systemctl", "disable", "--now", _ROA_TIMER_NAME])
    except Dn42CtlError:
        warnings.append(f"{_ROA_TIMER_NAME} disable 失败（可能未安装）")

    try:
        _run(["systemctl", "stop", _ROA_SERVICE_NAME])
    except Dn42CtlError:
        warnings.append(f"{_ROA_SERVICE_NAME} stop 失败（可能未运行）")

    service_path = _SYSTEMD_UNIT_DIR / _ROA_SERVICE_NAME
    timer_path = _SYSTEMD_UNIT_DIR / _ROA_TIMER_NAME

    for path in (service_path, timer_path):
        if path.exists():
            try:
                path.unlink()
                changed.append(str(path))
            except PermissionError as exc:
                raise Dn42CtlError(f"权限不足: 无法删除 {path}") from exc
            except OSError as exc:
                raise Dn42CtlError(f"删除失败: {path} ({exc})") from exc
        else:
            warnings.append(f"{path} 不存在，跳过删除")

    _run(["systemctl", "daemon-reload"])

    return SystemInstallResult(
        component="roa-service",
        action="uninstall",
        changed_files=changed,
        warnings=warnings,
    )
