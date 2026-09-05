from __future__ import annotations

import io
import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import Result
from conftest import FAKE_WG_PRIVKEY, FAKE_WG_PUBKEY, VALID_ENDPOINT, VALID_PEER_IP, VALID_PEER_LLA, VALID_PUBKEY
from typer.testing import CliRunner

from dn42ctl.cli import app
from dn42ctl.config import AppConfig, load_config, save_config
from dn42ctl.db import Database

LocalCli = Callable[..., Result]
ASN = 4242421234
NAME = "leaf"


@pytest.fixture
def local_cli(sample_config: AppConfig, db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LocalCli:
    config_path = tmp_path / "config.toml"
    save_config(config_path, sample_config)
    db = Database.open(db_path)
    try:
        db.ensure_node(sample_config.node_id)
    finally:
        db.close()

    def system_output(cmd: list[str], **_kwargs: object) -> str:
        if cmd == ["wg", "genkey"]:
            return FAKE_WG_PRIVKEY + "\n"
        if cmd == ["wg", "pubkey"]:
            return FAKE_WG_PUBKEY + "\n"
        if cmd[:2] == ["wg", "show"] or cmd[:3] == ["birdc", "show", "protocols"]:
            return f"live {cmd[-1]}\n"
        if cmd == ["networkctl", "reload"]:
            return ""
        raise AssertionError(f"Unexpected system command: {cmd}")

    monkeypatch.setattr(subprocess, "check_output", system_output)
    monkeypatch.setattr(
        "dn42ctl.services.init_sys.urllib.request.urlopen", lambda *_a, **_kw: io.BytesIO(b"# test ROA\n")
    )
    runner = CliRunner()

    def invoke(args: list[str], *, input: str | None = None) -> Result:
        return runner.invoke(app, ["--config-path", str(config_path), "--db-path", str(db_path), *args], input=input)

    return invoke


def _peer_args(kind: str) -> list[str]:
    identity = (
        ["--asn", str(ASN)]
        if kind == "bgp"
        else ["--name", NAME, "--peer-ip", VALID_PEER_IP, "--rxcost", "120", "--type", "tunnel"]
    )
    return [
        kind,
        "peer",
        *identity,
        "--pubkey",
        VALID_PUBKEY,
        "--endpoint",
        VALID_ENDPOINT,
        "--peer-lla",
        VALID_PEER_LLA,
        "--listen-port",
        "31001" if kind == "bgp" else "31002",
        "--allowed-ips",
        "fe80::1/64,fd42::/16",
    ]


@pytest.mark.parametrize(("kind", "key", "ifname"), [("bgp", str(ASN), "dn42_1234"), ("ibgp", NAME, "wg_leaf")])
def test_peer_lifecycle_updates_persistent_state_and_files(
    local_cli: LocalCli, sample_config: AppConfig, kind: str, key: str, ifname: str
) -> None:
    created = local_cli(_peer_args(kind))
    assert created.exit_code == 0, created.output
    netdev = Path(sample_config.networkd_dir) / f"{ifname}.netdev"
    network = netdev.with_suffix(".network")
    assert "RouteTable=off" in netdev.read_text()
    assert "AllowedIPs=fe80::/64" in netdev.read_text()
    assert "AllowedIPs=fd42::/16" in netdev.read_text()
    assert f"Peer={VALID_PEER_LLA}" in network.read_text()

    extra = ["--peer-ip", "2001:db8:2::99", "--rxcost", "256", "--type", "wired"] if kind == "ibgp" else []
    modified = local_cli(
        [
            kind,
            "peer",
            "modify",
            key,
            "--endpoint",
            "new.example:51900",
            "--peer-lla",
            "fe80::99",
            "--listen-port",
            "31003",
            "--allowed-ips",
            "2001:db8::/32",
            *extra,
        ],
        input="\n",
    )
    assert modified.exit_code == 0, modified.output
    shown = local_cli(["show", kind, "--json"])
    assert shown.exit_code == 0, shown.output
    peer = json.loads(shown.output)[0]
    assert peer["ifname"] == ifname
    assert peer["peer_public_key"] == VALID_PUBKEY
    assert peer["endpoint"] == "new.example:51900"
    assert peer["peer_lla"] == "fe80::99"
    assert peer["listen_port"] == 31003
    assert peer["allowed_ips"] == ["2001:db8::/32"]
    assert peer["live_wg"]["output"] == f"live {ifname}"
    assert "Endpoint=new.example:51900" in netdev.read_text()
    assert "ListenPort=31003" in netdev.read_text()
    if kind == "ibgp":
        assert peer["peer_ip"] == "2001:db8:2::99"
        assert peer["babel_rxcost"] == 256
        assert peer["babel_type"] == "wired"
        assert "rxcost 256" in Path(sample_config.bird_babel_conf_path).read_text()

    cancelled = local_cli([kind, "peer", "del", key], input="n\n")
    assert cancelled.exit_code == 0, cancelled.output
    assert netdev.exists() and network.exists()
    assert len(json.loads(local_cli(["show", kind, "--json"]).output)) == 1

    deleted = local_cli([kind, "peer", "del", key], input="y\n")
    assert deleted.exit_code == 0, deleted.output
    assert not netdev.exists() and not network.exists()
    assert json.loads(local_cli(["show", kind, "--json"]).output) == []


def test_interactive_bgp_creation_uses_the_advertised_keypair(local_cli: LocalCli, sample_config: AppConfig) -> None:
    result = local_cli(["bgp", "peer"], input=f"{ASN}\n{VALID_PUBKEY}\n\n{VALID_PEER_LLA}\n")
    assert result.exit_code == 0, result.output
    assert result.output.index(FAKE_WG_PUBKEY) < result.output.index("Peer 公钥")
    netdev = (Path(sample_config.networkd_dir) / "dn42_1234.netdev").read_text()
    assert f"PrivateKey={FAKE_WG_PRIVKEY}" in netdev
    assert "Endpoint=" not in netdev
    peer = json.loads(local_cli(["show", "bgp", "--json"]).output)[0]
    assert peer["wg_public_key"] == FAKE_WG_PUBKEY
    assert peer["listen_port"] == 21234


def test_ibgp_without_wireguard_rejects_tunnel_modification(local_cli: LocalCli, sample_config: AppConfig) -> None:
    result = local_cli(["ibgp", "peer", "--name", NAME, "--peer-ip", VALID_PEER_IP, "--no-wg"])
    assert result.exit_code == 0, result.output
    assert (Path(sample_config.bird_peers_dir) / "ibgp_leaf.conf").exists()
    assert not (Path(sample_config.networkd_dir) / "wg_leaf.netdev").exists()
    shown = local_cli(["show", "ibgp"])
    assert shown.exit_code == 0, shown.output
    assert "no-wg" in shown.output and VALID_PEER_IP in shown.output
    assert json.loads(local_cli(["show", "wg", "--json"]).output) == []
    modified = local_cli(["ibgp", "peer", "modify", NAME])
    assert modified.exit_code == 2
    assert "未启用 WireGuard" in modified.output


@pytest.mark.parametrize(
    ("kind", "flag", "value", "message"),
    [
        ("bgp", "--allowed-ips", "10.0.0.0/8", "IPv6 CIDR"),
        ("bgp", "--pubkey", "invalid!", "base64"),
        ("ibgp", "--peer-ip", "192.0.2.1", "IPv6 地址"),
        ("ibgp", "--rxcost", "-1", "rxcost"),
        ("ibgp", "--type", "invalid", "type"),
    ],
    ids=["bgp-allowed-ips", "bgp-key", "ibgp-address", "ibgp-cost", "ibgp-type"],
)
def test_invalid_peer_input_leaves_database_and_files_untouched(
    local_cli: LocalCli, sample_config: AppConfig, db_path: Path, kind: str, flag: str, value: str, message: str
) -> None:
    result = local_cli([*_peer_args(kind), flag, value])
    assert result.exit_code == 2, result.output
    assert message in result.output
    db = Database.open(db_path)
    try:
        assert db.list_bgp_peers(sample_config.node_id) == []
        assert db.list_ibgp_peers(sample_config.node_id) == []
    finally:
        db.close()
    assert list(Path(sample_config.networkd_dir).iterdir()) == []


@pytest.mark.parametrize("view", [[], ["all"], ["bgp"], ["ibgp"], ["wg"]], ids=["default", "all", "bgp", "ibgp", "wg"])
def test_show_text_and_json_report_local_state(local_cli: LocalCli, view: list[str]) -> None:
    for kind in ("bgp", "ibgp"):
        result = local_cli(_peer_args(kind))
        assert result.exit_code == 0, result.output
    text = local_cli(["show", *view])
    assert text.exit_code == 0, text.output
    assert "live(wg): OK" in text.output
    assert "OK:" in text.output
    result = local_cli(["show", *view, "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    if not view or view == ["all"]:
        assert payload["node_id"] == "test-node"
        assert len(payload["wg"]) == 2
        assert payload["bgp"][0]["peer_asn"] == ASN
        assert payload["ibgp"][0]["name"] == NAME
    else:
        assert len(payload) == (2 if view == ["wg"] else 1)
        assert all(peer["live_wg"]["ok"] for peer in payload)


def test_show_keeps_other_probe_results_when_one_command_is_denied(
    local_cli: LocalCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    for kind in ("bgp", "ibgp"):
        created = local_cli(_peer_args(kind))
        assert created.exit_code == 0, created.output
    system_output = subprocess.check_output

    def deny_one_probe(cmd: list[str], **kwargs: object) -> str:
        if cmd == ["wg", "show", "dn42_1234"]:
            raise PermissionError("probe denied")
        return system_output(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "check_output", deny_one_probe)
    result = local_cli(["show", "all", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["bgp"][0]["live_wg"]["ok"] is False
    assert payload["bgp"][0]["live_wg"]["error"] == "probe denied"
    assert payload["bgp"][0]["live_bird"]["output"] == "live dn42_1234"
    assert payload["ibgp"][0]["live_wg"]["output"] == "live wg_leaf"
    shown = local_cli(["show", "bgp"])
    assert shown.exit_code == 0, shown.output
    assert "live(wg): UNAVAILABLE" in shown.output


def test_genconf_requires_overwrite_consent_and_preserves_custom_config(
    local_cli: LocalCli, sample_config: AppConfig
) -> None:
    for kind in ("bgp", "ibgp"):
        result = local_cli(_peer_args(kind))
        assert result.exit_code == 0, result.output
    bird = Path(sample_config.bird_conf_path)
    bird.write_text("operator config\n")
    extra = Path(sample_config.bird_extra_conf_path)
    extra.write_text("# operator custom config\n")
    refused = local_cli(["genconf", "--all"], input="n\ny\n")
    assert refused.exit_code == 1, refused.output
    assert bird.read_text() == "operator config\n"
    result = local_cli(["genconf", "--all"], input="y\ny\n")
    assert result.exit_code == 0, result.output
    assert str(sample_config.bird_peers_dir) in bird.read_text()
    assert f'include "{extra}";' in bird.read_text()
    assert extra.read_text() == "# operator custom config\n"
    assert Path(sample_config.bird_roa_v6_conf_path).read_text() == "# test ROA\n"
    assert (Path(sample_config.networkd_dir) / "dn42-dummy.network").exists()


def test_scan_imports_bgp_and_ibgp_files_and_reports_repeat_conflicts(
    local_cli: LocalCli, sample_config: AppConfig, tmp_path: Path
) -> None:
    for kind in ("bgp", "ibgp"):
        created = local_cli(_peer_args(kind))
        assert created.exit_code == 0, created.output
    generated = local_cli(["genconf"], input="y\n")
    assert generated.exit_code == 0, generated.output
    scan_db = tmp_path / "imported.sqlite3"
    scanned = local_cli(["--db-path", str(scan_db), "scan"])
    assert scanned.exit_code == 0, scanned.output
    assert "inserted: 2" in scanned.output
    db = Database.open(scan_db)
    try:
        bgp = db.get_bgp_peer(sample_config.node_id, ASN)
        ibgp = db.get_ibgp_peer(sample_config.node_id, NAME)
        assert bgp is not None and ibgp is not None
        assert bgp["wg_public_key"] == FAKE_WG_PUBKEY
        assert bgp["listen_port"] == 31001
        assert ibgp["peer_ip"] == VALID_PEER_IP
        assert ibgp["babel_rxcost"] == 120
        assert ibgp["babel_type"] == "tunnel"
    finally:
        db.close()
    repeated = local_cli(["--db-path", str(scan_db), "scan"])
    assert repeated.exit_code == 0, repeated.output
    assert "inserted: 0  conflicts: 2" in repeated.output


def test_init_reuses_identity_and_locations(local_cli: LocalCli, sample_config: AppConfig, tmp_path: Path) -> None:
    result = local_cli(["init", "--own-asn", "4242429999"])
    assert result.exit_code == 0, result.output
    stored = load_config(tmp_path / "config.toml")
    assert stored.node_id == sample_config.node_id
    assert stored.own_asn == 4242429999
    assert stored.router_id == sample_config.router_id
    assert stored.bird_conf_path == sample_config.bird_conf_path
    assert stored.networkd_dir == sample_config.networkd_dir


def test_first_init_uses_supplied_ipv6_and_generates_requested_files(
    local_cli: LocalCli, sample_config: AppConfig, tmp_path: Path
) -> None:
    (tmp_path / "config.toml").unlink()
    result = local_cli(
        [
            "init",
            "--own-asn",
            str(sample_config.own_asn),
            "--own-ipv6",
            "2001:db8:100::abcd",
            "--ownnet-v6",
            "2001:db8:100::/48",
            "--ownnetset-v6",
            "[2001:db8:100::/48+]",
            "--bird-conf",
            sample_config.bird_conf_path,
            "--bird-peers-dir",
            sample_config.bird_peers_dir,
            "--bird-babel-conf",
            sample_config.bird_babel_conf_path,
            "--bird-roa-v6-conf",
            sample_config.bird_roa_v6_conf_path,
            "--bird-extra-conf",
            sample_config.bird_extra_conf_path,
            "--networkd-dir",
            sample_config.networkd_dir,
            "--nm-system-connections-dir",
            sample_config.nm_system_connections_dir,
            "--dummy-backend",
            "networkd",
            "--genconf",
        ],
        input=sample_config.router_id + "\n",
    )
    assert result.exit_code == 0, result.output
    stored = load_config(tmp_path / "config.toml")
    assert stored.node_id != sample_config.node_id
    assert stored.own_ipv6 == "2001:db8:100::abcd"
    assert stored.router_id == sample_config.router_id
    assert stored.own_ipv6 in Path(stored.bird_conf_path).read_text()
    assert f"Address={stored.own_ipv6}/128" in (Path(stored.networkd_dir) / "dn42-dummy.network").read_text()
    assert Path(stored.bird_roa_v6_conf_path).read_text() == "# test ROA\n"


@pytest.mark.parametrize(
    ("content", "message"), [(None, "未初始化"), ("invalid [toml", "配置文件读取失败")], ids=["missing", "malformed"]
)
def test_bad_config_stops_commands_with_exit_2(
    local_cli: LocalCli, tmp_path: Path, content: str | None, message: str
) -> None:
    config_path = tmp_path / "config.toml"
    if content is None:
        config_path.unlink()
    else:
        config_path.write_text(content)
    result = local_cli(["show", "all"])
    assert result.exit_code == 2, result.output
    assert message in result.output
