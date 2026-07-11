from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, PackageLoader, StrictUndefined

_JINJA_ENV = Environment(
    loader=PackageLoader("dn42ctl", "templates"),
    autoescape=False,
    undefined=StrictUndefined,
    keep_trailing_newline=True,
)


def _render_template(template_name: str, **context: object) -> str:
    return _JINJA_ENV.get_template(template_name).render(**context)


def render_bird_main_conf(
    *,
    own_asn: int,
    router_id: str,
    own_ipv6: str,
    ownnet_v6: str,
    ownnetset_v6: str,
    bird_babel_conf_path: Path,
    bird_peers_dir: Path,
    bird_roa_v6_conf_path: Path,
) -> str:
    return _render_template(
        "bird.conf.j2",
        own_asn=own_asn,
        router_id=router_id,
        own_ipv6=own_ipv6,
        ownnet_v6=ownnet_v6,
        ownnetset_v6=ownnetset_v6,
        bird_babel_conf_path=bird_babel_conf_path,
        bird_roa_v6_conf_path=bird_roa_v6_conf_path,
        bird_peers_include=(bird_peers_dir / "*"),
    )


def render_bird_bgp_peer_conf(*, ifname: str, peer_lla: str, peer_asn: int) -> str:
    if not peer_lla:
        raise ValueError(f"peer_lla must not be empty for BGP peer {ifname}")
    return _render_template(
        "bird_bgp_peer.conf.j2",
        ifname=ifname,
        peer_lla=peer_lla,
        peer_asn=peer_asn,
    )


def render_bird_ibgp_peer_conf(*, name: str, ifname: str, peer_ip: str) -> str:
    if not peer_ip:
        raise ValueError(f"peer_ip must not be empty for iBGP peer {ifname}")
    return _render_template(
        "bird_ibgp_peer.conf.j2",
        name=name,
        ifname=ifname,
        peer_ip=peer_ip,
    )


def render_babel_conf(*, interfaces: list[tuple[str, int, str]]) -> str:
    """Render babel.conf.

    `interfaces` is a deterministic list of (ifname, rxcost, babel_type) tuples.
    """
    return _render_template("babel.conf.j2", interfaces=interfaces)


def render_networkd_netdev(
    *,
    ifname: str,
    private_key: str,
    listen_port: int,
    peer_public_key: str,
    endpoint: str,
    allowed_ips: list[str],
) -> str:
    return _render_template(
        "networkd_netdev.j2",
        ifname=ifname,
        private_key=private_key,
        listen_port=listen_port,
        peer_public_key=peer_public_key,
        endpoint=endpoint,
        allowed_ips=allowed_ips,
    )


def render_networkd_network(*, ifname: str, local_lla: str, peer_lla: str) -> str:
    return _render_template(
        "networkd_network.j2",
        ifname=ifname,
        local_lla=local_lla,
        peer_lla=peer_lla,
    )


def render_dummy_netdev() -> str:
    return _render_template("dummy_netdev.j2")


def render_dummy_network(*, own_ipv6: str) -> str:
    return _render_template("dummy_network.j2", own_ipv6=own_ipv6)


def render_systemd_roa_service(*, roa_parent: Path, roa_target: Path, roa_url: str) -> str:
    return _render_template(
        "systemd_roa_service.j2",
        roa_parent=roa_parent,
        roa_target=roa_target,
        roa_url=roa_url,
    )


def render_systemd_roa_timer() -> str:
    return _render_template("systemd_roa_timer.j2")
