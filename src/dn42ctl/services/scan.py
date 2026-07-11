from __future__ import annotations

import ipaddress
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from dn42ctl.config import AppConfig
from dn42ctl.constants import BABEL_DEFAULT_RXCOST, BABEL_DEFAULT_TYPE, IFNAME_PREFIX_BGP, IFNAME_PREFIX_IBGP
from dn42ctl.db import BgpPeerRecord, DatabaseError, IbgpPeerRecord
from dn42ctl.services.core import (
    DEFAULT_ALLOWED_IPS,
    BirdPathsDiscovery,
    Dn42CtlError,
    ScanImported,
    ScanResult,
    open_db_and_ensure_node,
    sanitize_name,
)
from dn42ctl.wg import WireGuardError, pubkey_from_private

_BIRD_INCLUDE_RE = re.compile(
    r"^\s*include\s+([\"'])([^\"']+)\1\s*;\s*(?:#.*)?$",
    flags=re.MULTILINE,
)


def discover_bird_paths(
    *,
    candidate_bird_conf_paths: list[Path],
) -> BirdPathsDiscovery:
    """Best-effort parse bird.conf to infer include paths.

    The primary use is `scan`: detect non-standard peers/babel/roa locations.
    """

    warnings: list[str] = []

    def _try_read(path: Path) -> str | None:
        try:
            if not path.exists():
                return None
        except OSError as exc:
            warnings.append(f"无法访问 bird.conf: {path} ({exc})")
            return None
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except PermissionError:
            warnings.append(f"权限不足: 无法读取 bird.conf: {path}")
            return None
        except OSError as exc:
            warnings.append(f"读取 bird.conf 失败: {path} ({exc})")
            return None

    seen: set[str] = set()
    best: BirdPathsDiscovery | None = None

    for p in candidate_bird_conf_paths:
        key = str(p)
        if key in seen:
            continue
        seen.add(key)

        text = _try_read(p)
        if text is None:
            continue

        peers_dir: Path | None = None
        babel_path: Path | None = None
        roa_v6_path: Path | None = None

        for m in _BIRD_INCLUDE_RE.finditer(text):
            inc = m.group(2).strip()
            if not inc:
                continue

            inc_path = Path(inc)
            name = inc_path.name
            normalized = inc.replace("\\", "/")

            if babel_path is None and name == "babel.conf":
                babel_path = inc_path
            if roa_v6_path is None and name == "roa_dn42_v6.conf":
                roa_v6_path = inc_path

            if peers_dir is None and "*" in inc:
                # Heuristic: prefer an include that clearly targets a peers dir.
                if "/peers/" in normalized or "/peers" in normalized:
                    peers_dir = inc_path.parent

        discovery = BirdPathsDiscovery(
            bird_conf_path=p,
            bird_peers_dir=peers_dir,
            bird_babel_conf_path=babel_path,
            bird_roa_v6_conf_path=roa_v6_path,
            warnings=[],
        )

        if peers_dir or babel_path or roa_v6_path:
            # Found useful paths; return immediately.
            return BirdPathsDiscovery(
                bird_conf_path=p,
                bird_peers_dir=peers_dir,
                bird_babel_conf_path=babel_path,
                bird_roa_v6_conf_path=roa_v6_path,
                warnings=warnings,
            )

        # Keep as fallback if we at least managed to read a candidate.
        if best is None:
            best = discovery

    if best is not None:
        return BirdPathsDiscovery(
            bird_conf_path=best.bird_conf_path,
            bird_peers_dir=best.bird_peers_dir,
            bird_babel_conf_path=best.bird_babel_conf_path,
            bird_roa_v6_conf_path=best.bird_roa_v6_conf_path,
            warnings=warnings,
        )

    return BirdPathsDiscovery(
        bird_conf_path=None,
        bird_peers_dir=None,
        bird_babel_conf_path=None,
        bird_roa_v6_conf_path=None,
        warnings=warnings,
    )


def _is_managed_ifname(ifname: str) -> bool:
    return ifname.startswith(IFNAME_PREFIX_BGP) or ifname.startswith(IFNAME_PREFIX_IBGP)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except PermissionError as exc:
        raise Dn42CtlError(f"权限不足: 无法读取 {path}") from exc
    except OSError as exc:
        raise Dn42CtlError(f"读取失败: {path} ({exc})") from exc


def _find_first(existing_paths: list[Path]) -> Path | None:
    for p in existing_paths:
        try:
            if p.exists():
                return p
        except OSError:
            continue
    return None


def _strip_inline_comment(line: str) -> str:
    for prefix in ("#", ";"):
        idx = line.find(prefix)
        if idx >= 0:
            return line[:idx].rstrip()
    return line


def _parse_networkd_netdev(text: str) -> dict[str, object]:
    section = ""
    allowed_ips: list[str] = []
    out: dict[str, object] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        line = _strip_inline_comment(line)
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()

        if section == "WireGuard":
            if key == "PrivateKey":
                out["private_key"] = val
            elif key == "ListenPort":
                try:
                    out["listen_port"] = int(val)
                except ValueError:
                    pass
        elif section == "WireGuardPeer":
            if key == "PublicKey":
                out["peer_public_key"] = val
            elif key == "Endpoint":
                out["endpoint"] = val
            elif key == "AllowedIPs":
                for item in re.split(r"[\s,]+", val):
                    item = item.strip()
                    if item:
                        try:
                            ipaddress.IPv6Network(item, strict=False)
                        except ValueError:
                            continue
                        allowed_ips.append(item)
    if allowed_ips:
        out["allowed_ips"] = allowed_ips
    return out


def _parse_networkd_network(text: str) -> dict[str, object]:
    section = ""
    out: dict[str, object] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        line = _strip_inline_comment(line)
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if section == "Address":
            if key == "Address":
                out["local_lla"] = val.split("/", 1)[0]
            elif key == "Peer":
                out["peer_lla"] = val
    return out


def _parse_bird_bgp_peer_conf(text: str, ifname: str) -> tuple[int | None, str | None]:
    # neighbor <peer_lla>%<ifname> as <asn>;
    m = re.search(
        rf"neighbor\s+([^%\s]+)%{re.escape(ifname)}\s+as\s+(\d+)\s*;",
        text,
    )
    if not m:
        return None, None
    try:
        asn = int(m.group(2))
    except ValueError:
        asn = None
    peer_lla = m.group(1)
    return asn, peer_lla


def _parse_bird_ibgp_peer_conf(text: str, ifname: str) -> tuple[str | None, str | None]:
    """Parse iBGP Bird peer conf. Returns (peer_ip, peer_lla).

    Matches two formats:
    - Generated: ``neighbor <peer_ip> as OWNAS;`` (bare IP, no interface binding)
    - Legacy:    ``neighbor <ip>%<ifname> as OWNAS;`` (link-local with interface)
    """
    m = re.search(
        rf"neighbor\s+([^%\s]+)%{re.escape(ifname)}\s+as\s+OWNAS\s*;",
        text,
    )
    if m:
        return None, m.group(1)
    m = re.search(
        r"neighbor\s+(\S+)\s+as\s+OWNAS\s*;",
        text,
    )
    if m:
        return m.group(1), None
    return None, None


_BABEL_INTERFACE_BLOCK_RE = re.compile(
    r"interface\s+\"([^\"]+)\"\s*\{([^}]*)\}\s*;",
    flags=re.MULTILINE,
)
_BABEL_RXCOST_RE = re.compile(r"\brxcost\s+(\d+)\s*;", flags=re.MULTILINE)
_BABEL_TYPE_RE = re.compile(r"\btype\s+(wired|wireless|tunnel)\s*;", flags=re.MULTILINE)


@dataclass(frozen=True)
class _BabelInterfaceParams:
    rxcost: int | None
    babel_type: str | None


def _parse_babel_conf_interface_params(text: str) -> dict[str, _BabelInterfaceParams]:
    """Best-effort parse of per-interface rxcost and type from babel.conf."""
    out: dict[str, _BabelInterfaceParams] = {}
    for m in _BABEL_INTERFACE_BLOCK_RE.finditer(text):
        ifname = m.group(1).strip()
        body = m.group(2)
        if not ifname:
            continue
        rxcost: int | None = None
        babel_type: str | None = None
        m2 = _BABEL_RXCOST_RE.search(body)
        if m2:
            try:
                rxcost = int(m2.group(1))
            except ValueError:
                pass
        m3 = _BABEL_TYPE_RE.search(body)
        if m3:
            babel_type = m3.group(1)
        out[ifname] = _BabelInterfaceParams(rxcost=rxcost, babel_type=babel_type)
    return out


def scan_local_configs(*, config: AppConfig, db_path: Path) -> ScanResult:
    warnings: list[str] = []
    inserted: list[ScanImported] = []
    conflicts: list[ScanImported] = []
    skipped: list[str] = []

    if shutil.which("wg") is None:
        raise Dn42CtlError("scan 需要 'wg' 命令以从私钥推导公钥，请先安装 wireguard-tools")

    # Directories to scan (dedup while preserving intent).
    bird_peers_dirs = [
        Path(config.bird_peers_dir),
        Path("/etc/bird/peers"),
        Path("/etc/bird6/peers"),
    ]
    networkd_dirs = [Path(config.networkd_dir), Path("/etc/systemd/network")]

    def _dedup(paths: list[Path]) -> list[Path]:
        seen: set[str] = set()
        out: list[Path] = []
        for p in paths:
            s = str(p)
            if s in seen:
                continue
            seen.add(s)
            out.append(p)
        return out

    bird_peers_dirs = _dedup(bird_peers_dirs)
    networkd_dirs = _dedup(networkd_dirs)

    # Optional: parse babel.conf to import per-interface rxcost for iBGP peers.
    babel_params_by_ifname: dict[str, _BabelInterfaceParams] = {}
    missing_rxcost_ifnames: list[str] = []
    babel_path = Path(config.bird_babel_conf_path)
    try:
        if babel_path.exists():
            try:
                babel_text = _read_text(babel_path)
            except Dn42CtlError as exc:
                warnings.append(f"读取 babel.conf 失败: {exc}")
            else:
                babel_params_by_ifname = _parse_babel_conf_interface_params(babel_text)
        else:
            warnings.append(f"未找到 babel.conf: {babel_path}（无法探测 rxcost/type，将使用默认值）")
    except OSError as exc:
        warnings.append(f"无法访问 babel.conf: {babel_path} ({exc})")

    # Candidate interfaces from known config file names.
    candidates: set[str] = set()

    def _collect_stems(dirs: list[Path], suffix: str) -> None:
        nonlocal candidates
        for d in dirs:
            try:
                if not d.exists():
                    continue
                for p in d.glob(f"*{suffix}"):
                    stem = p.name[: -len(suffix)]
                    if _is_managed_ifname(stem):
                        candidates.add(stem)
            except PermissionError:
                # Degrade gracefully: warn instead of aborting the whole scan.
                warnings.append(f"权限不足: 无法扫描目录 {d}，已跳过")
            except OSError as exc:
                warnings.append(f"扫描目录失败: {d} ({exc})")

    _collect_stems(networkd_dirs, ".netdev")
    _collect_stems(networkd_dirs, ".network")

    db = open_db_and_ensure_node(db_path, config.node_id)

    for ifname in sorted(candidates):
        kind = "bgp" if ifname.startswith(IFNAME_PREFIX_BGP) else "ibgp"
        peer_name = ifname[len(IFNAME_PREFIX_IBGP) :] if kind == "ibgp" else None

        # Locate config sources.
        netdev_path = _find_first([d / f"{ifname}.netdev" for d in networkd_dirs])
        network_path = _find_first([d / f"{ifname}.network" for d in networkd_dirs])

        backend: str = "networkd"
        data: dict[str, object] = {}

        try:
            if netdev_path and network_path:
                data.update(_parse_networkd_netdev(_read_text(netdev_path)))
                data.update(_parse_networkd_network(_read_text(network_path)))
            else:
                skipped.append(f"{ifname}: 未找到 networkd 配置")
                continue
        except Dn42CtlError as exc:
            skipped.append(f"{ifname}: 读取配置失败: {exc}")
            continue

        private_key = str(data.get("private_key") or "").strip()
        if not private_key:
            skipped.append(f"{ifname}: 缺少 PrivateKey")
            continue

        raw_port = data.get("listen_port")
        listen_port: int = 0
        if isinstance(raw_port, int):
            listen_port = raw_port
        elif isinstance(raw_port, str):
            try:
                listen_port = int(raw_port.strip())
            except ValueError:
                listen_port = 0
        if listen_port <= 0:
            # ListenPort is optional for some setups (e.g. behind NAT/firewall).
            # Store 0 as a sentinel meaning "unset".
            warnings.append(f"{ifname}: 未找到 ListenPort，将以 0(未设置) 导入")
            listen_port = 0

        local_lla = str(data.get("local_lla") or "").strip()
        if not local_lla:
            skipped.append(f"{ifname}: 缺少本端 LLA/Address")
            continue

        peer_public_key = str(data.get("peer_public_key") or "").strip() or None
        endpoint = str(data.get("endpoint") or "").strip() or None
        allowed_ips_list: list[str]
        raw_allowed = data.get("allowed_ips")
        if isinstance(raw_allowed, list):
            collected: list[str] = []
            for item in cast(list[object], raw_allowed):
                if isinstance(item, str) and item:
                    collected.append(item)
            allowed_ips_list = collected or DEFAULT_ALLOWED_IPS
        else:
            allowed_ips_list = DEFAULT_ALLOWED_IPS

        peer_lla = str(data.get("peer_lla") or "").strip() or None

        # Bird conf is required for BGP ASN; optional for iBGP peer_lla.
        if kind == "bgp":
            bird_path = _find_first([d / f"{ifname}.conf" for d in bird_peers_dirs])
            if bird_path is None:
                skipped.append(f"{ifname}: 缺少 Bird peer conf，无法解析 ASN")
                continue
            try:
                bird_text = _read_text(bird_path)
            except Dn42CtlError as exc:
                skipped.append(f"{ifname}: 读取 Bird peer conf 失败: {exc}")
                continue
            asn, bird_peer_lla = _parse_bird_bgp_peer_conf(bird_text, ifname)
            if asn is None:
                skipped.append(f"{ifname}: Bird peer conf 解析 ASN 失败")
                continue
            if peer_lla is None and bird_peer_lla:
                peer_lla = bird_peer_lla

            peer_key = f"AS{asn}"
            try:
                # Skip conflicts by default; user can delete then rescan.
                if db.get_bgp_peer(config.node_id, asn) is not None:
                    conflicts.append(
                        ScanImported(
                            kind="bgp",
                            key=peer_key,
                            ifname=ifname,
                            net_backend=backend,
                        )
                    )
                    continue
            except DatabaseError as exc:
                raise Dn42CtlError(str(exc)) from exc

            try:
                wg_public_key = pubkey_from_private(private_key)
            except WireGuardError as exc:
                skipped.append(f"{ifname}: wg pubkey 失败: {exc}")
                continue
            try:
                db.insert_bgp_peer(
                    BgpPeerRecord(
                        node_id=config.node_id,
                        peer_asn=asn,
                        ifname=ifname,
                        wg_private_key=private_key,
                        wg_public_key=wg_public_key,
                        peer_public_key=peer_public_key,
                        endpoint=endpoint,
                        local_lla=local_lla,
                        peer_lla=peer_lla,
                        listen_port=listen_port,
                        allowed_ips=allowed_ips_list,
                        net_backend=backend,
                    )
                )
                inserted.append(
                    ScanImported(
                        kind="bgp",
                        key=peer_key,
                        ifname=ifname,
                        net_backend=backend,
                    )
                )
            except DatabaseError as exc:
                # Keep it explicit; treat as conflict-like.
                conflicts.append(
                    ScanImported(
                        kind="bgp",
                        key=peer_key,
                        ifname=ifname,
                        net_backend=backend,
                    )
                )
                warnings.append(f"{ifname}: 写入 DB 失败: {exc}")
        else:
            assert peer_name is not None
            try:
                peer_name = sanitize_name(peer_name)
            except Dn42CtlError as exc:
                skipped.append(f"{ifname}: 接口名无效: {exc}")
                continue

            # Optional: try bird conf to extract peer_ip and/or peer_lla.
            peer_ip: str | None = None
            if True:
                bird_path = _find_first([d / f"ibgp_{peer_name}.conf" for d in bird_peers_dirs])
                if bird_path is not None:
                    try:
                        bird_text = _read_text(bird_path)
                    except Dn42CtlError as exc:
                        warnings.append(f"{ifname}: 读取 Bird iBGP peer conf 失败: {exc}")
                    else:
                        parsed_ip, parsed_lla = _parse_bird_ibgp_peer_conf(bird_text, ifname)
                        if parsed_ip:
                            peer_ip = parsed_ip
                        if parsed_lla and peer_lla is None:
                            peer_lla = parsed_lla

            try:
                if db.get_ibgp_peer(config.node_id, peer_name) is not None:
                    conflicts.append(
                        ScanImported(
                            kind="ibgp",
                            key=peer_name,
                            ifname=ifname,
                            net_backend=backend,
                        )
                    )
                    continue
            except DatabaseError as exc:
                raise Dn42CtlError(str(exc)) from exc

            try:
                wg_public_key = pubkey_from_private(private_key)
            except WireGuardError as exc:
                skipped.append(f"{ifname}: wg pubkey 失败: {exc}")
                continue
            try:
                params = babel_params_by_ifname.get(ifname)
                scan_rxcost = params.rxcost if (params and params.rxcost is not None) else BABEL_DEFAULT_RXCOST
                scan_babel_type = params.babel_type if (params and params.babel_type) else BABEL_DEFAULT_TYPE
                db.insert_ibgp_peer(
                    IbgpPeerRecord(
                        node_id=config.node_id,
                        name=peer_name,
                        ifname=ifname,
                        wg_private_key=private_key,
                        wg_public_key=wg_public_key,
                        peer_public_key=peer_public_key,
                        endpoint=endpoint,
                        local_lla=local_lla,
                        peer_lla=peer_lla,
                        listen_port=listen_port,
                        allowed_ips=allowed_ips_list,
                        net_backend=backend,
                        babel_rxcost=scan_rxcost,
                        babel_type=scan_babel_type,
                        peer_ip=peer_ip,
                    )
                )
                inserted.append(
                    ScanImported(
                        kind="ibgp",
                        key=peer_name,
                        ifname=ifname,
                        net_backend=backend,
                    )
                )
            except DatabaseError as exc:
                conflicts.append(
                    ScanImported(
                        kind="ibgp",
                        key=peer_name,
                        ifname=ifname,
                        net_backend=backend,
                    )
                )
                warnings.append(f"{ifname}: 写入 DB 失败: {exc}")

            if ifname not in babel_params_by_ifname:
                missing_rxcost_ifnames.append(ifname)

    if conflicts:
        warnings.append("存在冲突（DB 已有记录）：默认已跳过。可先使用 'dn42ctl del peer ...' 删除后再 scan。")

    if missing_rxcost_ifnames:
        preview = ", ".join(missing_rxcost_ifnames[:8])
        extra = "" if len(missing_rxcost_ifnames) <= 8 else f" ... (+{len(missing_rxcost_ifnames) - 8})"
        warnings.append(f"babel.conf 未提供部分接口的 rxcost：{preview}{extra}；已使用默认值 {BABEL_DEFAULT_RXCOST}")

    return ScanResult(
        inserted=inserted,
        conflicts=conflicts,
        skipped=skipped,
        warnings=warnings,
    )
