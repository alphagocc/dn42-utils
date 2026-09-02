from __future__ import annotations

from pathlib import Path

import pytest

from dn42ctl.db import Database, IbgpPeerRecord
from dn42ctl.db_managed import ManagedNodeStore
from dn42ctl.services.core import Dn42CtlError
from dn42ctl.services.node_address import (
    backfill_remote_node_ids,
    plan_propagation,
    set_node_addresses,
)

NODE_A = "11111111-1111-4111-8111-111111111111"
NODE_B = "22222222-2222-4222-8222-222222222222"
NODE_C = "33333333-3333-4333-8333-333333333333"

IPV6_A = "fd42:4242:1111::1"
IPV6_A_NEW = "fd42:4242:1111::9"


class _Row(dict):
    """够用的 sqlite3.Row 替身：plan_propagation 只做 row["key"] 下标访问。"""


def _row(**overrides: object) -> _Row:
    base = {
        "node_id": NODE_B,
        "name": "to-a",
        "peer_ip": IPV6_A,
        "endpoint": "old.example.com:51821",
    }
    base.update(overrides)
    return _Row(base)


class TestPlanPropagationPeerIp:
    def test_sets_peer_ip_from_own_ipv6(self) -> None:
        changes, warnings = plan_propagation(rows=[_row()], own_ipv6=IPV6_A_NEW, endpoint_host=None)
        assert warnings == []
        assert [(c.field, c.old, c.new) for c in changes] == [("peer_ip", IPV6_A, IPV6_A_NEW)]

    def test_unchanged_peer_ip_produces_no_change(self) -> None:
        changes, _ = plan_propagation(rows=[_row()], own_ipv6=IPV6_A, endpoint_host=None)
        assert changes == []

    def test_none_own_ipv6_never_blanks_peer_ip(self) -> None:
        """清空 own_ipv6 不能把 peer_ip 抹掉 —— 空 peer_ip 会让对端整个 apply 失败。"""
        changes, _ = plan_propagation(rows=[_row()], own_ipv6=None, endpoint_host=None)
        assert changes == []

    def test_empty_own_ipv6_never_blanks_peer_ip(self) -> None:
        changes, _ = plan_propagation(rows=[_row()], own_ipv6="", endpoint_host=None)
        assert changes == []


class TestPlanPropagationEndpoint:
    def test_swaps_host_and_keeps_port(self) -> None:
        changes, warnings = plan_propagation(
            rows=[_row(endpoint="old.example.com:51821")],
            own_ipv6=None,
            endpoint_host="new.example.com",
        )
        assert warnings == []
        assert [(c.field, c.new) for c in changes] == [("endpoint", "new.example.com:51821")]

    def test_keeps_nonstandard_port(self) -> None:
        """NAT 端口映射下端口与对端 listen_port 本就合法地不一致,传播地址时必须保留原端口。"""
        changes, _ = plan_propagation(
            rows=[_row(endpoint="old.example.com:12345")],
            own_ipv6=None,
            endpoint_host="new.example.com",
        )
        assert changes[0].new == "new.example.com:12345"

    def test_ipv6_literal_gets_brackets(self) -> None:
        changes, _ = plan_propagation(
            rows=[_row(endpoint="[2001:db8::1]:51821")],
            own_ipv6=None,
            endpoint_host="2001:db8::99",
        )
        assert changes[0].new == "[2001:db8::99]:51821"

    def test_ipv4_literal_has_no_brackets(self) -> None:
        changes, _ = plan_propagation(
            rows=[_row(endpoint="1.2.3.4:51821")],
            own_ipv6=None,
            endpoint_host="5.6.7.8",
        )
        assert changes[0].new == "5.6.7.8:51821"

    @pytest.mark.parametrize("empty", ["", "   ", None])
    def test_empty_endpoint_stays_empty_and_warns(self, empty: str | None) -> None:
        """被动侧没有端口可保留,编不出 endpoint。"""
        changes, warnings = plan_propagation(
            rows=[_row(endpoint=empty)],
            own_ipv6=None,
            endpoint_host="new.example.com",
        )
        assert changes == []
        assert len(warnings) == 1
        assert "endpoint 为空" in warnings[0]

    def test_unparsable_endpoint_left_alone_and_warns(self) -> None:
        changes, warnings = plan_propagation(
            rows=[_row(endpoint="garbage-without-port")],
            own_ipv6=None,
            endpoint_host="new.example.com",
        )
        assert changes == []
        assert "无法解析" in warnings[0]

    def test_unchanged_endpoint_produces_no_change(self) -> None:
        changes, _ = plan_propagation(
            rows=[_row(endpoint="same.example.com:51821")],
            own_ipv6=None,
            endpoint_host="same.example.com",
        )
        assert changes == []

    def test_none_endpoint_host_skips_endpoint_entirely(self) -> None:
        changes, warnings = plan_propagation(
            rows=[_row(endpoint="")],
            own_ipv6=IPV6_A_NEW,
            endpoint_host=None,
        )
        # 只有 peer_ip 的改动,不因为空 endpoint 而告警
        assert [c.field for c in changes] == ["peer_ip"]
        assert warnings == []


class TestPlanPropagationMultipleRows:
    def test_both_fields_across_rows(self) -> None:
        rows = [
            _row(node_id=NODE_B, name="to-a"),
            _row(node_id=NODE_C, name="also-to-a", endpoint="other.example.com:40000"),
        ]
        changes, _ = plan_propagation(rows=rows, own_ipv6=IPV6_A_NEW, endpoint_host="new.example.com")
        assert {(c.node_id, c.name, c.field, c.new) for c in changes} == {
            (NODE_B, "to-a", "peer_ip", IPV6_A_NEW),
            (NODE_B, "to-a", "endpoint", "new.example.com:51821"),
            (NODE_C, "also-to-a", "peer_ip", IPV6_A_NEW),
            (NODE_C, "also-to-a", "endpoint", "new.example.com:40000"),
        }

    def test_no_rows_is_noop(self) -> None:
        assert plan_propagation(rows=[], own_ipv6=IPV6_A_NEW, endpoint_host="h.example.com") == ([], [])


def _ibgp(node_id: str, name: str, **overrides: object) -> IbgpPeerRecord:
    base: dict[str, object] = {
        "node_id": node_id,
        "name": name,
        "ifname": f"wg_{name}",
        "wg_private_key": "priv",
        "wg_public_key": "pub",
        "peer_public_key": "peerpub",
        "endpoint": "old.example.com:51821",
        "local_lla": "fe80::1",
        "peer_lla": "fe80::2",
        "listen_port": 51821,
        "allowed_ips": ["::/0"],
        "net_backend": "networkd",
        "babel_rxcost": 20,
        "peer_ip": IPV6_A,
        "has_wg": True,
        "babel_type": "tunnel",
        "remote_node_id": None,
    }
    base.update(overrides)
    return IbgpPeerRecord(**base)  # type: ignore[arg-type]


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "dn42.sqlite3"
    db = Database.open(path)
    try:
        store = ManagedNodeStore(db.connection)
        store.add(NODE_A, "alpha")
        store.add(NODE_B, "beta")
        db.insert_ibgp_peer(_ibgp(NODE_B, "to-a", remote_node_id=NODE_A))
    finally:
        db.close()
    return path


class TestSetNodeAddresses:
    def test_updates_node_and_propagates(self, db_path: Path) -> None:
        result = set_node_addresses(
            db_path=db_path,
            node_id=NODE_A,
            own_ipv6=IPV6_A_NEW,
            endpoint_host="new.example.com",
        )
        assert result.node.own_ipv6 == IPV6_A_NEW
        assert result.node.endpoint_host == "new.example.com"

        db = Database.open(db_path)
        try:
            row = db.get_ibgp_peer(NODE_B, "to-a")
        finally:
            db.close()
        assert row is not None
        assert row["peer_ip"] == IPV6_A_NEW
        assert row["endpoint"] == "new.example.com:51821"

    def test_dry_run_writes_nothing(self, db_path: Path) -> None:
        result = set_node_addresses(
            db_path=db_path,
            node_id=NODE_A,
            own_ipv6=IPV6_A_NEW,
            dry_run=True,
        )
        assert result.dry_run is True
        assert [c.new for c in result.changes] == [IPV6_A_NEW]

        db = Database.open(db_path)
        try:
            assert ManagedNodeStore(db.connection).get(NODE_A).own_ipv6 is None
            assert db.get_ibgp_peer(NODE_B, "to-a")["peer_ip"] == IPV6_A
        finally:
            db.close()

    def test_no_propagate_leaves_peer_rows_alone(self, db_path: Path) -> None:
        result = set_node_addresses(
            db_path=db_path,
            node_id=NODE_A,
            own_ipv6=IPV6_A_NEW,
            propagate=False,
        )
        assert result.changes == []
        db = Database.open(db_path)
        try:
            assert db.get_ibgp_peer(NODE_B, "to-a")["peer_ip"] == IPV6_A
        finally:
            db.close()

    def test_unlinked_rows_are_invisible_to_propagation(self, tmp_path: Path) -> None:
        path = tmp_path / "db.sqlite3"
        db = Database.open(path)
        try:
            ManagedNodeStore(db.connection).add(NODE_A, "alpha")
            ManagedNodeStore(db.connection).add(NODE_B, "beta")
            db.insert_ibgp_peer(_ibgp(NODE_B, "unlinked"))  # remote_node_id 为 None
        finally:
            db.close()

        result = set_node_addresses(db_path=path, node_id=NODE_A, own_ipv6=IPV6_A_NEW)
        assert result.changes == []
        assert any("没有任何 iBGP peer 行" in w for w in result.warnings)

        db = Database.open(path)
        try:
            assert db.get_ibgp_peer(NODE_B, "unlinked")["peer_ip"] == IPV6_A
        finally:
            db.close()

    def test_uses_existing_value_for_unset_field(self, db_path: Path) -> None:
        """只改 endpoint_host 时,peer_ip 仍按库里已有的 own_ipv6 传播。"""
        set_node_addresses(db_path=db_path, node_id=NODE_A, own_ipv6=IPV6_A_NEW, propagate=False)
        result = set_node_addresses(db_path=db_path, node_id=NODE_A, endpoint_host="new.example.com")
        fields = {c.field for c in result.changes}
        assert fields == {"peer_ip", "endpoint"}

    def test_clearing_own_ipv6_does_not_touch_peer_ip(self, db_path: Path) -> None:
        set_node_addresses(db_path=db_path, node_id=NODE_A, own_ipv6=IPV6_A_NEW)
        result = set_node_addresses(db_path=db_path, node_id=NODE_A, own_ipv6=None)
        assert result.node.own_ipv6 is None
        assert result.changes == []
        db = Database.open(db_path)
        try:
            assert db.get_ibgp_peer(NODE_B, "to-a")["peer_ip"] == IPV6_A_NEW
        finally:
            db.close()

    def test_rename_and_disable(self, db_path: Path) -> None:
        result = set_node_addresses(db_path=db_path, node_id=NODE_A, name="renamed", enabled=False)
        assert result.node.name == "renamed"
        assert result.node.enabled is False

    def test_unknown_node_raises(self, db_path: Path) -> None:
        with pytest.raises(Dn42CtlError, match="managed node 不存在"):
            set_node_addresses(db_path=db_path, node_id=NODE_C, own_ipv6=IPV6_A_NEW)

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"own_ipv6": "not-an-ip"}, "IPv6"),
            ({"router_id": "999.1.1.1"}, "IPv4"),
            ({"endpoint_host": "host.example.com:51821"}, "端口"),
            ({"endpoint_host": "[2001:db8::1]"}, "方括号"),
        ],
    )
    def test_invalid_values_rejected(self, db_path: Path, kwargs: dict, message: str) -> None:
        with pytest.raises(Dn42CtlError, match=message):
            set_node_addresses(db_path=db_path, node_id=NODE_A, **kwargs)


class TestBackfillRemoteNodeIds:
    def test_links_unique_match(self, tmp_path: Path) -> None:
        path = tmp_path / "db.sqlite3"
        db = Database.open(path)
        try:
            store = ManagedNodeStore(db.connection)
            store.add(NODE_A, "alpha")
            store.add(NODE_B, "beta")
            store.set_addresses(NODE_A, own_ipv6=IPV6_A)
            db.insert_ibgp_peer(_ibgp(NODE_B, "to-a"))
        finally:
            db.close()

        linked, _ = backfill_remote_node_ids(db_path=path)
        assert linked == [f"{NODE_B}/to-a -> {NODE_A}"]

        db = Database.open(path)
        try:
            assert db.get_ibgp_peer(NODE_B, "to-a")["remote_node_id"] == NODE_A
        finally:
            db.close()

    def test_dry_run_writes_nothing(self, tmp_path: Path) -> None:
        path = tmp_path / "db.sqlite3"
        db = Database.open(path)
        try:
            store = ManagedNodeStore(db.connection)
            store.add(NODE_A, "alpha")
            store.add(NODE_B, "beta")
            store.set_addresses(NODE_A, own_ipv6=IPV6_A)
            db.insert_ibgp_peer(_ibgp(NODE_B, "to-a"))
        finally:
            db.close()

        linked, _ = backfill_remote_node_ids(db_path=path, dry_run=True)
        assert len(linked) == 1
        db = Database.open(path)
        try:
            assert db.get_ibgp_peer(NODE_B, "to-a")["remote_node_id"] is None
        finally:
            db.close()

    def test_ambiguous_match_is_skipped(self, tmp_path: Path) -> None:
        """两个节点共用同一个 own_ipv6 时不能猜 —— 猜错会让地址传播改错节点的配置。"""
        path = tmp_path / "db.sqlite3"
        db = Database.open(path)
        try:
            store = ManagedNodeStore(db.connection)
            store.add(NODE_A, "alpha")
            store.add(NODE_B, "beta")
            store.add(NODE_C, "gamma")
            store.set_addresses(NODE_A, own_ipv6=IPV6_A)
            store.set_addresses(NODE_C, own_ipv6=IPV6_A)
            db.insert_ibgp_peer(_ibgp(NODE_B, "to-a"))
        finally:
            db.close()

        linked, skipped = backfill_remote_node_ids(db_path=path)
        assert linked == []
        assert any("不唯一" in s for s in skipped)

    def test_existing_link_is_preserved(self, tmp_path: Path) -> None:
        path = tmp_path / "db.sqlite3"
        db = Database.open(path)
        try:
            store = ManagedNodeStore(db.connection)
            store.add(NODE_A, "alpha")
            store.add(NODE_B, "beta")
            store.add(NODE_C, "gamma")
            store.set_addresses(NODE_A, own_ipv6=IPV6_A)
            db.insert_ibgp_peer(_ibgp(NODE_B, "to-a", remote_node_id=NODE_C))
        finally:
            db.close()

        linked, skipped = backfill_remote_node_ids(db_path=path)
        assert linked == []
        assert any("已有 remote_node_id" in s for s in skipped)

    def test_no_match_is_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "db.sqlite3"
        db = Database.open(path)
        try:
            ManagedNodeStore(db.connection).add(NODE_B, "beta")
            db.insert_ibgp_peer(_ibgp(NODE_B, "to-a"))
        finally:
            db.close()

        linked, skipped = backfill_remote_node_ids(db_path=path)
        assert linked == []
        assert any("没有匹配的受管节点" in s for s in skipped)


class TestAdoptSelfPartition:
    """修复 config.node_id 与 self 节点 id 分叉的存量部署。"""

    @staticmethod
    def _setup(tmp_path: Path, *, stale_peers: bool = True, target_peers: bool = False) -> Path:
        from dn42ctl.db import BgpPeerRecord

        path = tmp_path / "db.sqlite3"
        db = Database.open(path)
        try:
            store = ManagedNodeStore(db.connection)
            store.upsert_self(NODE_A, name="self")
            db.ensure_node(NODE_C)
            if stale_peers:
                db.insert_bgp_peer(
                    BgpPeerRecord(
                        node_id=NODE_C,
                        peer_asn=4242420001,
                        ifname="dn42_0001",
                        wg_private_key="priv",
                        wg_public_key="pub",
                        peer_public_key="peerpub",
                        endpoint="a.example.com:51820",
                        local_lla="fe80::1",
                        peer_lla="fe80::2",
                        listen_port=31000,
                        allowed_ips=["::/0"],
                        net_backend="networkd",
                    )
                )
                db.insert_ibgp_peer(_ibgp(NODE_C, "stale"))
            if target_peers:
                db.insert_ibgp_peer(_ibgp(NODE_A, "already-here"))
        finally:
            db.close()
        return path

    def test_moves_rows_to_self(self, tmp_path: Path) -> None:
        from dn42ctl.services.node_address import adopt_self_partition

        path = self._setup(tmp_path)
        result = adopt_self_partition(db_path=path, config_node_id=NODE_C)
        assert (result.from_node_id, result.to_node_id) == (NODE_C, NODE_A)
        assert (result.bgp_moved, result.ibgp_moved) == (1, 1)

        db = Database.open(path)
        try:
            assert len(db.list_bgp_peers(NODE_A)) == 1
            assert len(db.list_ibgp_peers(NODE_A)) == 1
            assert db.list_bgp_peers(NODE_C) == []
        finally:
            db.close()

    def test_emits_desired_for_target(self, tmp_path: Path) -> None:
        from dn42ctl.db_managed import SyncEventStore
        from dn42ctl.services.node_address import adopt_self_partition

        path = self._setup(tmp_path)
        db = Database.open(path)
        try:
            before = SyncEventStore(db.connection).latest_id()
        finally:
            db.close()

        adopt_self_partition(db_path=path, config_node_id=NODE_C)

        db = Database.open(path)
        try:
            kinds = [(e.node_id, e.kind) for e in SyncEventStore(db.connection).fetch_since(before)]
        finally:
            db.close()
        assert (NODE_A, "desired") in kinds

    def test_dry_run_writes_nothing(self, tmp_path: Path) -> None:
        from dn42ctl.services.node_address import adopt_self_partition

        path = self._setup(tmp_path)
        result = adopt_self_partition(db_path=path, config_node_id=NODE_C, dry_run=True)
        assert result.dry_run is True
        assert (result.bgp_moved, result.ibgp_moved) == (1, 1)

        db = Database.open(path)
        try:
            assert db.list_bgp_peers(NODE_A) == []
            assert len(db.list_bgp_peers(NODE_C)) == 1
        finally:
            db.close()

    def test_refuses_when_target_partition_non_empty(self, tmp_path: Path) -> None:
        """两边都有行意味着有人已经在新分区写过配置,合并策略只能由人来定;
        而且硬搬会撞 UNIQUE(node_id, ifname)。"""
        from dn42ctl.services.node_address import adopt_self_partition

        path = self._setup(tmp_path, target_peers=True)
        with pytest.raises(Dn42CtlError, match="非空"):
            adopt_self_partition(db_path=path, config_node_id=NODE_C)

    def test_refuses_when_nothing_to_move(self, tmp_path: Path) -> None:
        from dn42ctl.services.node_address import adopt_self_partition

        path = self._setup(tmp_path, stale_peers=False)
        with pytest.raises(Dn42CtlError, match="没有任何 peer 行"):
            adopt_self_partition(db_path=path, config_node_id=NODE_C)

    def test_refuses_when_already_aligned(self, tmp_path: Path) -> None:
        from dn42ctl.services.node_address import adopt_self_partition

        path = self._setup(tmp_path)
        with pytest.raises(Dn42CtlError, match="无需修复"):
            adopt_self_partition(db_path=path, config_node_id=NODE_A)

    def test_requires_a_self_node(self, tmp_path: Path) -> None:
        from dn42ctl.services.node_address import adopt_self_partition

        path = tmp_path / "empty.sqlite3"
        Database.open(path).close()
        with pytest.raises(Dn42CtlError, match="没有 self 节点"):
            adopt_self_partition(db_path=path, config_node_id=NODE_C)

    def test_explicit_from_overrides_config(self, tmp_path: Path) -> None:
        from dn42ctl.services.node_address import adopt_self_partition

        path = self._setup(tmp_path)
        result = adopt_self_partition(db_path=path, config_node_id=NODE_B, from_node_id=NODE_C)
        assert result.from_node_id == NODE_C
        assert (result.bgp_moved, result.ibgp_moved) == (1, 1)
