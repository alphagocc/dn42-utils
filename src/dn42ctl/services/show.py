from __future__ import annotations

import json
import subprocess
from concurrent.futures import Future, as_completed
from concurrent.futures.thread import ThreadPoolExecutor
from pathlib import Path
from typing import cast

from dn42ctl.config import AppConfig
from dn42ctl.db import DatabaseError

from dn42ctl.services.core import (
    DEFAULT_ALLOWED_IPS,
    BgpPeerView,
    CommandOutput,
    Dn42CtlError,
    FileStatus,
    IbgpPeerView,
    WgTunnelView,
    open_db,
    peer_files_for_backend,
)


_LIVE_CMD_TIMEOUT = 2  # seconds


def _run_live_probes(
    *,
    wg_ifnames: list[str],
    bird_protocols: list[str],
) -> tuple[dict[str, CommandOutput], dict[str, CommandOutput]]:
    if not wg_ifnames and not bird_protocols:
        return {}, {}

    task_count = len(wg_ifnames) + len(bird_protocols)
    max_workers = min(32, max(4, task_count))

    wg_results: dict[str, CommandOutput] = {}
    bird_results: dict[str, CommandOutput] = {}

    def _run_noexcept(cmd: list[str]) -> CommandOutput:
        try:
            return _run_cmd_best_effort(cmd)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            return CommandOutput(cmd=cmd, ok=False, output=None, error=str(exc))

    futures: dict[Future[CommandOutput], tuple[str, str]] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for ifname in wg_ifnames:
            cmd = ["wg", "show", ifname]
            futures[executor.submit(_run_noexcept, cmd)] = ("wg", ifname)
        for proto in bird_protocols:
            cmd = ["birdc", "show", "protocols", proto]
            futures[executor.submit(_run_noexcept, cmd)] = ("bird", proto)

        for fut in as_completed(futures):
            kind, key = futures[fut]
            res = fut.result()
            if kind == "wg":
                wg_results[key] = res
            else:
                bird_results[key] = res

    return wg_results, bird_results


def _run_cmd_best_effort(cmd: list[str]) -> CommandOutput:
    try:
        out = subprocess.check_output(
            cmd, text=True, stderr=subprocess.STDOUT, timeout=_LIVE_CMD_TIMEOUT
        ).strip()
        return CommandOutput(cmd=cmd, ok=True, output=out, error=None)
    except FileNotFoundError as exc:
        return CommandOutput(cmd=cmd, ok=False, output=None, error=str(exc))
    except subprocess.TimeoutExpired:
        return CommandOutput(cmd=cmd, ok=False, output=None, error="timeout")
    except subprocess.CalledProcessError as exc:
        output = exc.output.strip() if isinstance(exc.output, str) else None
        return CommandOutput(
            cmd=cmd, ok=False, output=output, error=f"exit={exc.returncode}"
        )


def _parse_allowed_ips_json(raw: str | None) -> list[str]:
    if not raw:
        return DEFAULT_ALLOWED_IPS
    try:
        loaded: object = json.loads(raw)
    except json.JSONDecodeError:
        return DEFAULT_ALLOWED_IPS
    if isinstance(loaded, list):
        ips: list[str] = []
        for item in cast(list[object], loaded):
            if not isinstance(item, str):
                return DEFAULT_ALLOWED_IPS
            ips.append(item)
        return ips
    return DEFAULT_ALLOWED_IPS


def _file_status(paths: list[Path]) -> list[FileStatus]:
    out: list[FileStatus] = []
    for p in paths:
        try:
            exists = p.exists()
        except OSError:
            exists = False
        out.append(FileStatus(path=str(p), exists=exists))
    return out


def show_bgp_peers(
    *,
    config: AppConfig,
    db_path: Path,
    include_live: bool = True,
) -> list[BgpPeerView]:
    db = open_db(db_path)
    node_id = config.node_id
    try:
        rows = db.list_bgp_peers(node_id)
    except DatabaseError as exc:
        raise Dn42CtlError(str(exc)) from exc

    out: list[BgpPeerView] = []

    wg_map: dict[str, CommandOutput] = {}
    bird_map: dict[str, CommandOutput] = {}
    if include_live and rows:
        ifnames = [str(r["ifname"]) for r in rows]
        wg_map, bird_map = _run_live_probes(wg_ifnames=ifnames, bird_protocols=ifnames)

    for row in rows:
        ifname = str(row["ifname"])
        peer_asn = int(row["peer_asn"])
        net_backend = str(row["net_backend"])
        files = peer_files_for_backend(
            config=config, ifname=ifname, net_backend=net_backend, kind="bgp"
        )
        live_wg = wg_map.get(ifname) if include_live else None
        live_bird = bird_map.get(ifname) if include_live else None

        out.append(
            BgpPeerView(
                peer_asn=peer_asn,
                ifname=ifname,
                peer_public_key=row["peer_public_key"],
                endpoint=row["endpoint"],
                peer_lla=row["peer_lla"],
                local_lla=str(row["local_lla"]),
                listen_port=int(row["listen_port"]),
                allowed_ips=_parse_allowed_ips_json(
                    str(row["allowed_ips_json"]) if row["allowed_ips_json"] else None
                ),
                net_backend=net_backend,
                wg_public_key=str(row["wg_public_key"]),
                files=_file_status(files),
                live_wg=live_wg,
                live_bird=live_bird,
            )
        )
    return out


def show_ibgp_peers(
    *,
    config: AppConfig,
    db_path: Path,
    include_live: bool = True,
) -> list[IbgpPeerView]:
    db = open_db(db_path)
    node_id = config.node_id
    try:
        rows = db.list_ibgp_peers(node_id)
    except DatabaseError as exc:
        raise Dn42CtlError(str(exc)) from exc

    out: list[IbgpPeerView] = []

    wg_map: dict[str, CommandOutput] = {}
    bird_map: dict[str, CommandOutput] = {}
    if include_live and rows:
        ifnames = [str(r["ifname"]) for r in rows]
        protos = [f"ibgp_{str(r['name'])}" for r in rows]
        wg_map, bird_map = _run_live_probes(wg_ifnames=ifnames, bird_protocols=protos)

    for row in rows:
        name = str(row["name"])
        ifname = str(row["ifname"])
        net_backend = str(row["net_backend"])
        files = peer_files_for_backend(
            config=config,
            ifname=ifname,
            net_backend=net_backend,
            kind="ibgp",
            ibgp_name=name,
        )
        proto = f"ibgp_{name}"
        live_wg = wg_map.get(ifname) if include_live else None
        live_bird = bird_map.get(proto) if include_live else None
        out.append(
            IbgpPeerView(
                name=name,
                ifname=ifname,
                babel_rxcost=int(row["babel_rxcost"]),
                peer_public_key=row["peer_public_key"],
                endpoint=row["endpoint"],
                peer_lla=row["peer_lla"],
                local_lla=str(row["local_lla"]),
                listen_port=int(row["listen_port"]),
                allowed_ips=_parse_allowed_ips_json(
                    str(row["allowed_ips_json"]) if row["allowed_ips_json"] else None
                ),
                net_backend=net_backend,
                wg_public_key=str(row["wg_public_key"]),
                files=_file_status(files),
                live_wg=live_wg,
                live_bird=live_bird,
            )
        )
    return out


def show_wg_tunnels(
    *,
    config: AppConfig,
    db_path: Path,
    include_live: bool = True,
) -> list[WgTunnelView]:
    tunnels: list[WgTunnelView] = []

    def _show(kind: str, p: BgpPeerView | IbgpPeerView) -> None:
        tunnels.append(
            WgTunnelView(
                kind=kind,
                peer_asn=getattr(p, "peer_asn", None),
                name=getattr(p, "name", None),
                ifname=p.ifname,
                peer_public_key=p.peer_public_key,
                endpoint=p.endpoint,
                allowed_ips=p.allowed_ips,
                listen_port=p.listen_port,
                local_lla=p.local_lla,
                peer_lla=p.peer_lla,
                net_backend=p.net_backend,
                wg_public_key=p.wg_public_key,
                files=p.files,
                live_wg=p.live_wg,
            )
        )

    for bgp in show_bgp_peers(
        config=config, db_path=db_path, include_live=include_live
    ):
        _show("bgp", bgp)
    for ibgp in show_ibgp_peers(
        config=config, db_path=db_path, include_live=include_live
    ):
        _show("ibgp", ibgp)

    return tunnels
