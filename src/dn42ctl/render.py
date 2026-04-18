from __future__ import annotations

import re
import uuid
from importlib import resources
from pathlib import Path


def load_template(template_name: str) -> str:
    return (
        resources.files("dn42ctl")
        .joinpath("templates")
        .joinpath(template_name)
        .read_text(encoding="utf-8")
    )


def render_bird_main_conf(
    *,
    template_text: str,
    own_asn: int,
    router_id: str,
    own_ipv6: str,
    ownnet_v6: str,
    ownnetset_v6: str,
    bird_babel_conf_path: Path,
    bird_peers_dir: Path,
    bird_roa_v6_conf_path: Path,
) -> str:
    out = template_text
    out = re.sub(
        r"^define\s+OWNAS\s*=\s*.*?;\s*(?:#.*)?$",
        f"define OWNAS =  {own_asn};",
        out,
        flags=re.MULTILINE,
    )
    out = re.sub(
        r"^define\s+ROUTERID\s*=\s*.*?;\s*(?:#.*)?$",
        f"define ROUTERID = {router_id};",
        out,
        flags=re.MULTILINE,
    )
    out = re.sub(
        r"^define\s+OWNIPv6\s*=\s*.*?;\s*(?:#.*)?$",
        f"define OWNIPv6 = {own_ipv6};",
        out,
        flags=re.MULTILINE,
    )
    out = re.sub(
        r"^define\s+OWNNETv6\s*=\s*.*?;\s*(?:#.*)?$",
        f"define OWNNETv6 = {ownnet_v6};",
        out,
        flags=re.MULTILINE,
    )
    out = re.sub(
        r"^define\s+OWNNETSETv6\s*=\s*.*?;\s*(?:#.*)?$",
        f"define OWNNETSETv6 = {ownnetset_v6};",
        out,
        flags=re.MULTILINE,
    )

    out = out.replace(
        'include "/etc/bird/roa_dn42_v6.conf";',
        f'include "{bird_roa_v6_conf_path}";',
    )
    out = out.replace(
        'include "/etc/bird/babel.conf";',
        f'include "{bird_babel_conf_path}";',
    )
    out = out.replace(
        'include "/etc/bird/peers/*";',
        f'include "{bird_peers_dir / "*"}";',
    )
    return out


def render_bird_bgp_peer_conf(*, ifname: str, peer_lla: str, peer_asn: int) -> str:
    return (
        f"protocol bgp {ifname} from dnpeers {{\n"
        "    bfd graceful;\n"
        "    bfd {\n"
        "        interval 10s;\n"
        "    };\n"
        f"    neighbor {peer_lla}%{ifname} as {peer_asn};\n"
        "}\n"
    )


def render_bird_ibgp_peer_conf(*, name: str, ifname: str, peer_lla: str) -> str:
    proto = f"ibgp_{name}"
    return (
        f"protocol bgp {proto} from ibgp_template {{\n"
        f"    neighbor {peer_lla}%{ifname} as OWNAS;\n"
        "};\n"
    )


def render_babel_conf(*, interface_names: list[str]) -> str:
    # Generated file: keep it deterministic and idempotent.
    interfaces = "\n".join(
        [
            f'    interface "{name}" {{\n        rxcost 120;\n    }};'
            for name in interface_names
        ]
    )
    if interfaces:
        interfaces = interfaces + "\n"

    return (
        "protocol direct {\n"
        "    ipv4;\n"
        "    ipv6;\n"
        '    interface "dn42-dummy";\n'
        "};\n\n"
        "protocol babel intra_babel {\n"
        "    ipv6 {\n"
        "        import where source != RTS_BGP && is_self_net_v6();\n"
        "        export where source != RTS_BGP && is_self_net_v6();\n"
        "    };\n"
        f"{interfaces}"
        "};\n"
    )


def render_networkd_netdev(
    *,
    ifname: str,
    private_key: str,
    listen_port: int,
    peer_public_key: str,
    endpoint: str,
    allowed_ips: list[str],
) -> str:
    allowed = "\n".join([f"AllowedIPs={cidr}" for cidr in allowed_ips])
    return (
        "[NetDev]\n"
        f"Name={ifname}\n"
        "Kind=wireguard\n\n"
        "[WireGuard]\n"
        f"PrivateKey={private_key}\n"
        f"ListenPort={listen_port}\n"
        "RouteTable=off\n\n"
        "[WireGuardPeer]\n"
        f"PublicKey={peer_public_key}\n"
        f"Endpoint={endpoint}\n"
        f"{allowed}\n"
    )


def render_networkd_network(*, ifname: str, local_lla_cidr: str, peer_lla: str) -> str:
    return (
        "[Match]\n"
        f"Name={ifname}\n\n"
        "[Network]\n"
        "DHCP=no\n"
        "IPv6AcceptRA=false\n"
        "IPForward=yes\n"
        "IPv4ReversePathFilter=no\n\n"
        "KeepConfiguration=yes\n\n"
        "[Address]\n"
        f"Address={local_lla_cidr}\n"
        f"Peer={peer_lla}\n"
    )


def new_nm_uuid() -> str:
    # Backward-compatible helper (prefer nm_uuid_for).
    return str(uuid.uuid4())


NM_UUID_NAMESPACE = uuid.UUID("4b45d197-2d1f-4c65-9a2b-4efb5a2c602f")


def nm_uuid_for(*, node_id: str, ifname: str) -> str:
    # Deterministic UUID so that regeneration does not create a "new" profile.
    return str(uuid.uuid5(NM_UUID_NAMESPACE, f"dn42ctl:{node_id}:{ifname}"))


def render_nmconnection_wireguard(
    *,
    conn_id: str,
    ifname: str,
    conn_uuid: str,
    private_key: str,
    listen_port: int,
    peer_public_key: str,
    endpoint: str,
    allowed_ips: list[str],
    local_ipv6_cidr: str,
    persistent_keepalive: int | None = None,
) -> str:
    # NOTE: peer-routes=false is mandatory to satisfy “禁止修改路由表”.
    allowed = ";".join(allowed_ips)
    peer_parts = [
        peer_public_key,
        f"endpoint={endpoint}",
        f"allowed-ips={allowed}",
    ]
    if persistent_keepalive is not None:
        peer_parts.append(f"persistent-keepalive={persistent_keepalive}")
    peers = " ".join(peer_parts)

    return (
        "[connection]\n"
        f"id={conn_id}\n"
        f"uuid={conn_uuid}\n"
        "type=wireguard\n"
        f"interface-name={ifname}\n"
        "autoconnect=true\n\n"
        "[wireguard]\n"
        f"private-key={private_key}\n"
        f"listen-port={listen_port}\n"
        "peer-routes=false\n"
        f"peers={peers}\n\n"
        "[ipv4]\n"
        "method=disabled\n\n"
        "[ipv6]\n"
        "method=manual\n"
        f"address1={local_ipv6_cidr}\n"
    )
