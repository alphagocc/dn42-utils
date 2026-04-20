from __future__ import annotations

import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from dn42ctl.config import AppConfig, save_config
from dn42ctl.db import DatabaseError
from dn42ctl.render import render_babel_conf, render_bird_main_conf

from dn42ctl.services.core import (
    Dn42CtlError,
    GenConfResult,
    InitConfigResult,
    ensure_dir,
    open_db,
    write_text,
)


DN42_ROA_V6_URL = "https://dn42.burble.com/roa/dn42_roa_bird2_6.conf"


def init_node(
    *,
    config_path: Path,
    db_path: Path,
    node_id: str,
    own_asn: int,
    router_id: str,
    own_ipv6: str,
    ownnet_v6: str,
    ownnetset_v6: str,
    bird_conf_path: Path,
    bird_peers_dir: Path,
    bird_babel_conf_path: Path,
    bird_roa_v6_conf_path: Path,
    networkd_dir: Path,
    nm_system_connections_dir: Path,
) -> InitConfigResult:
    config = AppConfig(
        node_id=node_id,
        own_asn=own_asn,
        router_id=router_id,
        own_ipv6=own_ipv6,
        ownnet_v6=ownnet_v6,
        ownnetset_v6=ownnetset_v6,
        bird_conf_path=str(bird_conf_path),
        bird_peers_dir=str(bird_peers_dir),
        bird_babel_conf_path=str(bird_babel_conf_path),
        bird_roa_v6_conf_path=str(bird_roa_v6_conf_path),
        networkd_dir=str(networkd_dir),
        nm_system_connections_dir=str(nm_system_connections_dir),
    )
    try:
        save_config(config_path, config)
    except PermissionError as exc:
        raise Dn42CtlError(f"权限不足: 无法写入配置 {config_path}。") from exc
    except OSError as exc:
        raise Dn42CtlError(f"写入配置失败: {config_path} ({exc})") from exc

    db = open_db(db_path)
    try:
        db.ensure_node(node_id)
    except DatabaseError as exc:
        raise Dn42CtlError(str(exc)) from exc

    return InitConfigResult(config=config, config_path=config_path, db_path=db_path)


def genconf(
    *,
    config: AppConfig,
    db_path: Path,
    overwrite_bird_conf: bool,
    overwrite_babel_conf: bool,
) -> GenConfResult:
    node_id = config.node_id
    bird_conf_path = Path(config.bird_conf_path)
    bird_peers_dir = Path(config.bird_peers_dir)
    bird_babel_conf_path = Path(config.bird_babel_conf_path)
    bird_roa_v6_conf_path = Path(config.bird_roa_v6_conf_path)

    db = open_db(db_path)
    try:
        db.ensure_node(node_id)
    except DatabaseError as exc:
        raise Dn42CtlError(str(exc)) from exc

    bird_conf_text = render_bird_main_conf(
        own_asn=config.own_asn,
        router_id=config.router_id,
        own_ipv6=config.own_ipv6,
        ownnet_v6=config.ownnet_v6,
        ownnetset_v6=config.ownnetset_v6,
        bird_babel_conf_path=bird_babel_conf_path,
        bird_peers_dir=bird_peers_dir,
        bird_roa_v6_conf_path=bird_roa_v6_conf_path,
    )

    if bird_conf_path.exists() and not overwrite_bird_conf:
        raise Dn42CtlError(f"Bird 主配置已存在且未允许覆盖: {bird_conf_path}")
    write_text(bird_conf_path, bird_conf_text)

    ensure_dir(bird_peers_dir)

    # Regenerate babel.conf deterministically from DB iBGP peers.
    try:
        interface_names = [str(r["ifname"]) for r in db.list_ibgp_peers(node_id)]
    except DatabaseError as exc:
        raise Dn42CtlError(str(exc)) from exc
    babel_text = render_babel_conf(interface_names=interface_names)
    if bird_babel_conf_path.exists() and not overwrite_babel_conf:
        raise Dn42CtlError(f"babel.conf 已存在且未允许覆盖: {bird_babel_conf_path}")
    write_text(bird_babel_conf_path, babel_text)

    warnings: list[str] = []

    # ROA v6: required by the Bird template filter; keep best-effort but explicit.
    if not bird_roa_v6_conf_path.exists():
        try:
            req = urllib.request.Request(
                DN42_ROA_V6_URL,
                headers={"User-Agent": "dn42ctl/0.1"},
            )
            with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310
                content = resp.read().decode("utf-8", errors="replace")
            if not content.strip():
                raise ValueError("empty ROA content")
            write_text(bird_roa_v6_conf_path, content)
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            warnings.append(f"ROA v6 下载失败: {exc}")
            warnings.append(
                "警告: ROA 文件为占位符（空表）。在 Bird 路由过滤中，"
                "未命中 ROA 的路由将被判定为 UNKNOWN 并拒绝导入，"
                f"请尽快手动获取: {DN42_ROA_V6_URL}"
            )
            placeholder = (
                "# dn42ctl: ROA v6 placeholder\n"
                f"# 下载失败，请稍后手动获取: {DN42_ROA_V6_URL}\n"
            )
            write_text(bird_roa_v6_conf_path, placeholder)

    systemd_enabled = False
    if sys.platform.startswith("linux") and shutil.which("systemctl"):
        unit_dir = Path("/etc/systemd/system")
        service_path = unit_dir / "dn42-roa-v6.service"
        timer_path = unit_dir / "dn42-roa-v6.timer"
        roa_target = bird_roa_v6_conf_path
        roa_parent = roa_target.parent

        if shutil.which("curl") is None:
            warnings.append("未找到 curl：systemd ROA 定时更新可能失败")

        service_text = (
            "[Unit]\n"
            "Description=Download DN42 ROA for BIRD2 (IPv6)\n"
            "After=network-online.target\n"
            "Wants=network-online.target\n\n"
            "[Service]\n"
            "Type=oneshot\n"
            # Quote path to handle spaces/special characters safely.
            f"ExecStartPre=/usr/bin/mkdir -p '{roa_parent}'\n"
            f"ExecStart=curl -fsSL -o '{roa_target}' {DN42_ROA_V6_URL}\n"
            "ExecStartPost=-birdc configure\n"
        )
        timer_text = (
            "[Unit]\n"
            "Description=Daily timer to download DN42 ROA (IPv6)\n\n"
            "[Timer]\n"
            "OnCalendar=*-*-* 00:05:00 UTC\n"
            "Persistent=true\n\n"
            "[Install]\n"
            "WantedBy=timers.target\n"
        )

        try:
            write_text(service_path, service_text)
            write_text(timer_path, timer_text)
            subprocess.check_output(["systemctl", "daemon-reload"], text=True)
            subprocess.check_output(
                ["systemctl", "enable", "--now", "dn42-roa-v6.timer"], text=True
            )
            # Trigger once so the ROA file is available immediately.
            subprocess.check_output(
                ["systemctl", "start", "dn42-roa-v6.service"], text=True
            )
            systemd_enabled = True
        except PermissionError as exc:
            raise Dn42CtlError("权限不足: 无法安装/启用 systemd 定时器。") from exc
        except FileNotFoundError as exc:
            # systemctl disappeared between which() and now; just warn.
            warnings.append(f"systemctl 不可用，跳过 ROA 定时器: {exc}")
        except subprocess.CalledProcessError as exc:
            out = exc.output.strip() if isinstance(exc.output, str) else ""
            warnings.append(f"systemd ROA 定时器启用失败: exit={exc.returncode} {out}")
    else:
        warnings.append("当前系统不支持 systemd：已跳过 ROA 定时器配置")

    return GenConfResult(
        config=config,
        db_path=db_path,
        bird_conf_path=bird_conf_path,
        bird_babel_conf_path=bird_babel_conf_path,
        bird_roa_v6_conf_path=bird_roa_v6_conf_path,
        systemd_roa_timer_enabled=systemd_enabled,
        warnings=warnings,
    )
