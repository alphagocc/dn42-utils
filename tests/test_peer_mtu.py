from pathlib import Path
from unittest.mock import patch

import pytest
from conftest import VALID_ENDPOINT, VALID_PEER_LLA, VALID_PUBKEY

from dn42ctl.services.bgp import create_bgp_peer, modify_bgp_peer
from dn42ctl.services.core import Dn42CtlError
from dn42ctl.services.desired_state import build_desired_state
from dn42ctl.services.ibgp import create_ibgp_peer, modify_ibgp_peer
from dn42ctl.services.init_sys import genconf
from dn42ctl.services.scan import scan_local_configs
from dn42ctl.services.show import show_wg_tunnels


@pytest.mark.parametrize(
    ("create", "modify", "fields", "payload_key"),
    [
        (create_bgp_peer, modify_bgp_peer, {"peer_asn": 4242421234}, "bgp_peers"),
        (
            create_ibgp_peer,
            modify_ibgp_peer,
            {"name": "mesh", "peer_ip": "fd42::2", "babel_type": "tunnel", "babel_rxcost": 20},
            "ibgp_peers",
        ),
    ],
)
@pytest.mark.usefixtures("mock_wg_keypair")
def test_peer_mtu_lifecycle(sample_config, db_path: Path, create, modify, fields, payload_key) -> None:
    args = {
        "config": sample_config,
        "db_path": db_path,
        "peer_public_key": VALID_PUBKEY,
        "endpoint": VALID_ENDPOINT,
        "peer_lla": VALID_PEER_LLA,
        "net_backend": "networkd",
        **fields,
    }
    result = create(**args, mtu=1280)
    network = Path(sample_config.networkd_dir) / f"{result.ifname}.network"
    assert "[Link]\nMTUBytes=1280\n" in network.read_text()

    modify(**args, mtu=1400, render_files=False)
    modify(**args, render_files=False)
    with pytest.raises(Dn42CtlError, match="MTU"):
        modify(**args, mtu=1279)
    desired = build_desired_state(db_path=db_path, node_id=sample_config.node_id)
    assert desired.to_dict()[payload_key][0]["mtu"] == 1400
    assert show_wg_tunnels(config=sample_config, db_path=db_path, include_live=False)[0].mtu == 1400

    Path(sample_config.bird_roa_v6_conf_path).touch()
    with (
        patch("dn42ctl.services.init_sys.ensure_dummy_interface"),
        patch("dn42ctl.services.init_sys.subprocess.check_output"),
    ):
        genconf(
            config=sample_config,
            db_path=db_path,
            overwrite_bird_conf=True,
            overwrite_babel_conf=True,
            regenerate_peers=True,
        )
    assert "[Link]\nMTUBytes=1400\n" in network.read_text()
    scanned_db = db_path.with_name("scan.sqlite3")
    scan_glob = Path.glob

    def fixture_files(directory, pattern):
        return scan_glob(directory, pattern) if directory == network.parent else iter(())

    with (
        patch.object(Path, "glob", fixture_files),
        patch("shutil.which", return_value="wg"),
        patch("dn42ctl.services.scan.pubkey_from_private", return_value="pub"),
    ):
        scan_local_configs(config=sample_config, db_path=scanned_db)
    tunnels = show_wg_tunnels(config=sample_config, db_path=scanned_db, include_live=False)
    assert {t.ifname: t.mtu for t in tunnels}[result.ifname] == 1400

    assert modify(**args, mtu=None).mtu is None
    assert "MTUBytes" not in network.read_text()
    cleared = build_desired_state(db_path=db_path, node_id=sample_config.node_id)
    assert "mtu" not in cleared.to_dict()[payload_key][0]
    assert desired.revision != cleared.revision
