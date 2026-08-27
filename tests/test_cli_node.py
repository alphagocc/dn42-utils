from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from dn42ctl.cli import app

NODE_A = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def base_args(db_path: Path, tmp_path: Path) -> list[str]:
    cfg_path = tmp_path / "config.toml"
    # Empty config file is enough for `node` subcommands (they don't require AppConfig).
    cfg_path.write_text("")
    return ["--db-path", str(db_path), "--config-path", str(cfg_path)]


class TestNodeAdd:
    def test_basic(self, runner: CliRunner, base_args: list[str]) -> None:
        result = runner.invoke(app, [*base_args, "node", "add", NODE_A, "--name", "alpha"])
        assert result.exit_code == 0, result.output
        assert "alpha" in result.output

    def test_invalid_uuid(self, runner: CliRunner, base_args: list[str]) -> None:
        result = runner.invoke(app, [*base_args, "node", "add", "bad-id", "--name", "x"])
        assert result.exit_code != 0


class TestNodeList:
    def test_empty(self, runner: CliRunner, base_args: list[str]) -> None:
        result = runner.invoke(app, [*base_args, "node", "list"])
        assert result.exit_code == 0
        assert "(没有" in result.output

    def test_after_add(self, runner: CliRunner, base_args: list[str]) -> None:
        runner.invoke(app, [*base_args, "node", "add", NODE_A, "--name", "alpha"])
        result = runner.invoke(app, [*base_args, "node", "list"])
        assert result.exit_code == 0
        assert NODE_A in result.output


class TestNodeShow:
    def test_existing(self, runner: CliRunner, base_args: list[str]) -> None:
        runner.invoke(app, [*base_args, "node", "add", NODE_A, "--name", "alpha"])
        result = runner.invoke(app, [*base_args, "node", "show", NODE_A])
        assert result.exit_code == 0
        assert "alpha" in result.output
        assert "write_policy" in result.output


class TestNodeRemove:
    def test_remove(self, runner: CliRunner, base_args: list[str]) -> None:
        runner.invoke(app, [*base_args, "node", "add", NODE_A, "--name", "alpha"])
        result = runner.invoke(app, [*base_args, "node", "remove", NODE_A])
        assert result.exit_code == 0
        assert "已删除" in result.output


class TestNodeTokenRotate:
    def test_prints_plaintext_once(self, runner: CliRunner, base_args: list[str]) -> None:
        runner.invoke(app, [*base_args, "node", "add", NODE_A, "--name", "alpha"])
        result = runner.invoke(app, [*base_args, "node", "token", "rotate", NODE_A])
        assert result.exit_code == 0, result.output
        assert "token (明文" in result.output


class TestNodePolicySet:
    def test_update(self, runner: CliRunner, base_args: list[str]) -> None:
        runner.invoke(app, [*base_args, "node", "add", NODE_A, "--name", "alpha"])
        result = runner.invoke(
            app,
            [*base_args, "node", "policy", "set", NODE_A, "--peer-add", "auto_accept"],
        )
        assert result.exit_code == 0, result.output
        assert "auto_accept" in result.output

    def test_no_options(self, runner: CliRunner, base_args: list[str]) -> None:
        runner.invoke(app, [*base_args, "node", "add", NODE_A, "--name", "alpha"])
        result = runner.invoke(app, [*base_args, "node", "policy", "set", NODE_A])
        assert result.exit_code != 0

    def test_invalid_peer_modify(self, runner: CliRunner, base_args: list[str]) -> None:
        runner.invoke(app, [*base_args, "node", "add", NODE_A, "--name", "alpha"])
        result = runner.invoke(
            app,
            [*base_args, "node", "policy", "set", NODE_A, "--peer-modify", "auto_accept"],
        )
        assert result.exit_code != 0


class TestNodeSetAddress:
    def test_sets_all_three(self, runner: CliRunner, base_args: list[str]) -> None:
        runner.invoke(app, [*base_args, "node", "add", NODE_A, "--name", "alpha"])
        result = runner.invoke(
            app,
            [
                *base_args,
                "node",
                "set-address",
                NODE_A,
                "--endpoint-host",
                "a.example.com",
                "--own-ipv6",
                "fd42:4242:1::1",
                "--router-id",
                "172.20.1.1",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "a.example.com" in result.output
        assert "fd42:4242:1::1" in result.output
        assert "172.20.1.1" in result.output

    def test_requires_at_least_one_option(self, runner: CliRunner, base_args: list[str]) -> None:
        runner.invoke(app, [*base_args, "node", "add", NODE_A, "--name", "alpha"])
        result = runner.invoke(app, [*base_args, "node", "set-address", NODE_A])
        assert result.exit_code != 0

    def test_clear_flag(self, runner: CliRunner, base_args: list[str]) -> None:
        runner.invoke(app, [*base_args, "node", "add", NODE_A, "--name", "alpha"])
        runner.invoke(app, [*base_args, "node", "set-address", NODE_A, "--own-ipv6", "fd42:4242:1::1"])
        result = runner.invoke(app, [*base_args, "node", "set-address", NODE_A, "--clear-own-ipv6"])
        assert result.exit_code == 0, result.output
        assert "own_ipv6:      -" in result.output

    def test_value_and_clear_conflict(self, runner: CliRunner, base_args: list[str]) -> None:
        runner.invoke(app, [*base_args, "node", "add", NODE_A, "--name", "alpha"])
        result = runner.invoke(
            app,
            [*base_args, "node", "set-address", NODE_A, "--own-ipv6", "fd42::1", "--clear-own-ipv6"],
        )
        assert result.exit_code != 0

    def test_service_error_becomes_nonzero_exit(self, runner: CliRunner, base_args: list[str]) -> None:
        """校验规则本身在 service / API 层测;这里只钉住 Dn42CtlError -> 非零退出码这一段接线。"""
        runner.invoke(app, [*base_args, "node", "add", NODE_A, "--name", "alpha"])
        result = runner.invoke(app, [*base_args, "node", "set-address", NODE_A, "--own-ipv6", "not-an-ip"])
        assert result.exit_code != 0

    def test_dry_run_does_not_write(self, runner: CliRunner, base_args: list[str]) -> None:
        runner.invoke(app, [*base_args, "node", "add", NODE_A, "--name", "alpha"])
        runner.invoke(app, [*base_args, "node", "set-address", NODE_A, "--own-ipv6", "fd42:4242:1::1", "--dry-run"])
        shown = runner.invoke(app, [*base_args, "node", "show", NODE_A])
        assert "own_ipv6:      -" in shown.output

    def test_warnings_go_to_output(self, runner: CliRunner, base_args: list[str]) -> None:
        runner.invoke(app, [*base_args, "node", "add", NODE_A, "--name", "alpha"])
        result = runner.invoke(app, [*base_args, "node", "set-address", NODE_A, "--own-ipv6", "fd42:4242:1::1"])
        assert "没有任何 iBGP peer 行" in result.output


class TestNodeMeshBackfill:
    def test_reports_when_nothing_to_do(self, runner: CliRunner, base_args: list[str]) -> None:
        result = runner.invoke(app, [*base_args, "node", "mesh-backfill"])
        assert result.exit_code == 0, result.output
        assert "已关联" in result.output

    def test_dry_run_flag_accepted(self, runner: CliRunner, base_args: list[str]) -> None:
        result = runner.invoke(app, [*base_args, "node", "mesh-backfill", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert "dry-run" in result.output


class TestNodeAdoptSelf:
    def test_errors_without_self_node(self, runner: CliRunner, base_args: list[str]) -> None:
        result = runner.invoke(app, [*base_args, "node", "adopt-self"])
        assert result.exit_code != 0

    def test_dry_run_reports_move(self, runner: CliRunner, db_path: Path, tmp_path: Path) -> None:
        from dn42ctl.config import AppConfig, save_config
        from dn42ctl.db import Database, IbgpPeerRecord
        from dn42ctl.db_managed import ManagedNodeStore

        stale = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        cfg_path = tmp_path / "config.toml"
        save_config(
            cfg_path,
            AppConfig(
                node_id=stale,
                own_asn=4242421234,
                router_id="172.23.0.1",
                own_ipv6="fd42:4242:1234::1",
                ownnet_v6="fd42:4242:1234::/48",
                ownnetset_v6="[fd42:4242:1234::/48+]",
                bird_conf_path=str(tmp_path / "bird.conf"),
                bird_peers_dir=str(tmp_path / "peers"),
                bird_babel_conf_path=str(tmp_path / "babel.conf"),
                bird_roa_v6_conf_path=str(tmp_path / "roa.conf"),
                networkd_dir=str(tmp_path / "networkd"),
                nm_system_connections_dir=str(tmp_path / "nm"),
                dummy_backend="networkd",
            ),
        )
        db = Database.open(db_path)
        try:
            ManagedNodeStore(db.connection).upsert_self(NODE_A, name="self")
            db.ensure_node(stale)
            db.insert_ibgp_peer(
                IbgpPeerRecord(
                    node_id=stale,
                    name="orphan",
                    ifname="wg_orphan",
                    wg_private_key="priv",
                    wg_public_key="pub",
                    peer_public_key="peerpub",
                    endpoint="a.example.com:51821",
                    local_lla="fe80::1",
                    peer_lla="fe80::2",
                    listen_port=51821,
                    allowed_ips=["::/0"],
                    net_backend="networkd",
                    babel_rxcost=20,
                    peer_ip="fd42:4242:1::1",
                )
            )
        finally:
            db.close()

        args = ["--db-path", str(db_path), "--config-path", str(cfg_path)]
        result = runner.invoke(app, [*args, "node", "adopt-self", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert "dry-run" in result.output
        assert "ibgp_peers: 1" in result.output

        # dry-run 之后行仍在原分区
        db = Database.open(db_path)
        try:
            assert len(db.list_ibgp_peers(stale)) == 1
        finally:
            db.close()

        assert runner.invoke(app, [*args, "node", "adopt-self"]).exit_code == 0
        db = Database.open(db_path)
        try:
            assert len(db.list_ibgp_peers(NODE_A)) == 1
            assert db.list_ibgp_peers(stale) == []
        finally:
            db.close()
