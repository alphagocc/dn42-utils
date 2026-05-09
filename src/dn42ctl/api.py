from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, field_validator

from dn42ctl.config import AppConfig
from dn42ctl.services import (
    Dn42CtlError,
    create_bgp_peer,
    create_ibgp_peer,
    delete_bgp_peer,
    delete_ibgp_peer,
    genconf,
    modify_bgp_peer,
    modify_ibgp_peer,
    show_bgp_peers,
    show_ibgp_peers,
    show_wg_tunnels,
)
from dn42ctl.validators import (
    validate_asn,
    validate_babel_type,
    validate_endpoint,
    validate_ipv6_address,
    validate_listen_port,
    validate_net_backend,
    validate_pubkey,
    validate_rxcost,
)

_bearer = HTTPBearer()

_config: AppConfig | None = None
_db_path: Path | None = None
_token: str = ""


def configure(*, config: AppConfig, db_path: Path, token: str) -> None:
    global _config, _db_path, _token
    _config = config
    _db_path = db_path
    _token = token


def _require_auth(
    cred: Annotated[HTTPAuthorizationCredentials, Depends(_bearer)],
) -> None:
    if cred.credentials != _token:
        raise HTTPException(status_code=401, detail="Invalid token")


def _get_config() -> AppConfig:
    if _config is None:
        raise HTTPException(status_code=500, detail="Server not configured")
    return _config


def _get_db_path() -> Path:
    if _db_path is None:
        raise HTTPException(status_code=500, detail="Server not configured")
    return _db_path


app = FastAPI(title="dn42ctl API", dependencies=[Depends(_require_auth)])
router_prefix = "/api"


# --- Pydantic models ---


class BgpPeerCreateRequest(BaseModel):
    peer_asn: int
    peer_public_key: str
    endpoint: str = ""
    peer_lla: str
    net_backend: str = "networkd"
    listen_port: int | None = None

    @field_validator("peer_asn")
    @classmethod
    def _check_asn(cls, v: int) -> int:
        return validate_asn(v)

    @field_validator("peer_public_key")
    @classmethod
    def _check_pubkey(cls, v: str) -> str:
        return validate_pubkey(v)

    @field_validator("endpoint")
    @classmethod
    def _check_endpoint(cls, v: str) -> str:
        return validate_endpoint(v, allow_empty=True)

    @field_validator("peer_lla")
    @classmethod
    def _check_peer_lla(cls, v: str) -> str:
        return validate_ipv6_address(v, field_name="Peer LLA")

    @field_validator("net_backend")
    @classmethod
    def _check_net_backend(cls, v: str) -> str:
        return validate_net_backend(v)

    @field_validator("listen_port")
    @classmethod
    def _check_listen_port(cls, v: int | None) -> int | None:
        if v is not None:
            return validate_listen_port(v, allow_zero=True)
        return v


class BgpPeerModifyRequest(BaseModel):
    peer_public_key: str
    endpoint: str = ""
    peer_lla: str
    net_backend: str = "networkd"
    listen_port: int | None = None

    @field_validator("peer_public_key")
    @classmethod
    def _check_pubkey(cls, v: str) -> str:
        return validate_pubkey(v)

    @field_validator("endpoint")
    @classmethod
    def _check_endpoint(cls, v: str) -> str:
        return validate_endpoint(v, allow_empty=True)

    @field_validator("peer_lla")
    @classmethod
    def _check_peer_lla(cls, v: str) -> str:
        return validate_ipv6_address(v, field_name="Peer LLA")

    @field_validator("net_backend")
    @classmethod
    def _check_net_backend(cls, v: str) -> str:
        return validate_net_backend(v)

    @field_validator("listen_port")
    @classmethod
    def _check_listen_port(cls, v: int | None) -> int | None:
        if v is not None:
            return validate_listen_port(v, allow_zero=True)
        return v


class IbgpPeerCreateRequest(BaseModel):
    name: str
    peer_ip: str
    has_wg: bool = True
    peer_public_key: str | None = None
    endpoint: str | None = None
    peer_lla: str | None = None
    net_backend: str | None = None
    babel_rxcost: int = 0
    babel_type: str = "tunnel"
    listen_port: int | None = None

    @field_validator("peer_ip")
    @classmethod
    def _check_peer_ip(cls, v: str) -> str:
        return validate_ipv6_address(v, field_name="对端网内 IPv6")

    @field_validator("peer_public_key")
    @classmethod
    def _check_pubkey(cls, v: str | None) -> str | None:
        if v is not None:
            return validate_pubkey(v)
        return v

    @field_validator("endpoint")
    @classmethod
    def _check_endpoint(cls, v: str | None) -> str | None:
        if v is not None:
            return validate_endpoint(v, allow_empty=True)
        return v

    @field_validator("peer_lla")
    @classmethod
    def _check_peer_lla(cls, v: str | None) -> str | None:
        if v is not None:
            return validate_ipv6_address(v, field_name="Peer LLA")
        return v

    @field_validator("net_backend")
    @classmethod
    def _check_net_backend(cls, v: str | None) -> str | None:
        if v is not None:
            return validate_net_backend(v)
        return v

    @field_validator("babel_rxcost")
    @classmethod
    def _check_rxcost(cls, v: int) -> int:
        return validate_rxcost(v)

    @field_validator("babel_type")
    @classmethod
    def _check_babel_type(cls, v: str) -> str:
        return validate_babel_type(v)

    @field_validator("listen_port")
    @classmethod
    def _check_listen_port(cls, v: int | None) -> int | None:
        if v is not None:
            return validate_listen_port(v, allow_zero=True)
        return v


class IbgpPeerModifyRequest(BaseModel):
    peer_public_key: str
    endpoint: str = ""
    peer_lla: str = ""
    peer_ip: str
    net_backend: str = "networkd"
    babel_rxcost: int = 120
    babel_type: str = "tunnel"
    listen_port: int | None = None

    @field_validator("peer_public_key")
    @classmethod
    def _check_pubkey(cls, v: str) -> str:
        return validate_pubkey(v)

    @field_validator("endpoint")
    @classmethod
    def _check_endpoint(cls, v: str) -> str:
        return validate_endpoint(v, allow_empty=True)

    @field_validator("peer_lla")
    @classmethod
    def _check_peer_lla(cls, v: str) -> str:
        if v:
            return validate_ipv6_address(v, field_name="Peer LLA")
        return v

    @field_validator("peer_ip")
    @classmethod
    def _check_peer_ip(cls, v: str) -> str:
        return validate_ipv6_address(v, field_name="对端网内 IPv6")

    @field_validator("net_backend")
    @classmethod
    def _check_net_backend(cls, v: str) -> str:
        return validate_net_backend(v)

    @field_validator("babel_rxcost")
    @classmethod
    def _check_rxcost(cls, v: int) -> int:
        return validate_rxcost(v)

    @field_validator("babel_type")
    @classmethod
    def _check_babel_type(cls, v: str) -> str:
        return validate_babel_type(v)

    @field_validator("listen_port")
    @classmethod
    def _check_listen_port(cls, v: int | None) -> int | None:
        if v is not None:
            return validate_listen_port(v, allow_zero=True)
        return v


class GenconfRequest(BaseModel):
    overwrite_bird_conf: bool = True
    overwrite_babel_conf: bool = True


# --- BGP peer routes ---


@app.get(f"{router_prefix}/bgp/peers")
def api_show_bgp(live: Annotated[bool, Query()] = True) -> list[dict]:
    try:
        peers = show_bgp_peers(config=_get_config(), db_path=_get_db_path(), include_live=live)
    except Dn42CtlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return [asdict(p) for p in peers]


@app.post(f"{router_prefix}/bgp/peers", status_code=201)
def api_create_bgp(body: BgpPeerCreateRequest) -> dict:
    try:
        res = create_bgp_peer(
            config=_get_config(),
            db_path=_get_db_path(),
            peer_asn=body.peer_asn,
            peer_public_key=body.peer_public_key,
            endpoint=body.endpoint,
            peer_lla=body.peer_lla,
            net_backend=body.net_backend,
            listen_port=body.listen_port,
        )
    except Dn42CtlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "ifname": res.ifname,
        "listen_port": res.listen_port,
        "wg_public_key": res.wg_public_key,
        "local_lla": res.local_lla,
        "generated_files": [str(p) for p in res.generated_files],
    }


@app.put(f"{router_prefix}/bgp/peers/{{asn}}")
def api_modify_bgp(asn: int, body: BgpPeerModifyRequest) -> dict:
    try:
        res = modify_bgp_peer(
            config=_get_config(),
            db_path=_get_db_path(),
            peer_asn=asn,
            peer_public_key=body.peer_public_key,
            endpoint=body.endpoint,
            peer_lla=body.peer_lla,
            net_backend=body.net_backend,
            listen_port=body.listen_port,
        )
    except Dn42CtlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "ifname": res.ifname,
        "listen_port": res.listen_port,
        "wg_public_key": res.wg_public_key,
        "local_lla": res.local_lla,
        "generated_files": [str(p) for p in res.generated_files],
    }


@app.delete(f"{router_prefix}/bgp/peers/{{asn}}")
def api_delete_bgp(asn: int) -> dict:
    try:
        res = delete_bgp_peer(config=_get_config(), db_path=_get_db_path(), peer_asn=asn)
    except Dn42CtlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return asdict(res)


# --- iBGP peer routes ---


@app.get(f"{router_prefix}/ibgp/peers")
def api_show_ibgp(live: Annotated[bool, Query()] = True) -> list[dict]:
    try:
        peers = show_ibgp_peers(config=_get_config(), db_path=_get_db_path(), include_live=live)
    except Dn42CtlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return [asdict(p) for p in peers]


@app.post(f"{router_prefix}/ibgp/peers", status_code=201)
def api_create_ibgp(body: IbgpPeerCreateRequest) -> dict:
    try:
        res = create_ibgp_peer(
            config=_get_config(),
            db_path=_get_db_path(),
            name=body.name,
            peer_ip=body.peer_ip,
            has_wg=body.has_wg,
            peer_public_key=body.peer_public_key,
            endpoint=body.endpoint,
            peer_lla=body.peer_lla,
            net_backend=body.net_backend,
            babel_rxcost=body.babel_rxcost,
            babel_type=body.babel_type,
            listen_port=body.listen_port,
        )
    except Dn42CtlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "ifname": res.ifname,
        "listen_port": res.listen_port,
        "wg_public_key": res.wg_public_key,
        "local_lla": res.local_lla,
        "generated_files": [str(p) for p in res.generated_files],
    }


@app.put(f"{router_prefix}/ibgp/peers/{{name}}")
def api_modify_ibgp(name: str, body: IbgpPeerModifyRequest) -> dict:
    try:
        res = modify_ibgp_peer(
            config=_get_config(),
            db_path=_get_db_path(),
            name=name,
            peer_public_key=body.peer_public_key,
            endpoint=body.endpoint,
            peer_lla=body.peer_lla,
            peer_ip=body.peer_ip,
            net_backend=body.net_backend,
            babel_rxcost=body.babel_rxcost,
            babel_type=body.babel_type,
            listen_port=body.listen_port,
        )
    except Dn42CtlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "ifname": res.ifname,
        "listen_port": res.listen_port,
        "wg_public_key": res.wg_public_key,
        "local_lla": res.local_lla,
        "generated_files": [str(p) for p in res.generated_files],
    }


@app.delete(f"{router_prefix}/ibgp/peers/{{name}}")
def api_delete_ibgp(name: str) -> dict:
    try:
        res = delete_ibgp_peer(config=_get_config(), db_path=_get_db_path(), name=name)
    except Dn42CtlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return asdict(res)


# --- WireGuard tunnels ---


@app.get(f"{router_prefix}/wg/tunnels")
def api_show_wg(live: Annotated[bool, Query()] = True) -> list[dict]:
    try:
        tunnels = show_wg_tunnels(config=_get_config(), db_path=_get_db_path(), include_live=live)
    except Dn42CtlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return [asdict(t) for t in tunnels]


# --- Show all ---


@app.get(f"{router_prefix}/show/all")
def api_show_all(live: Annotated[bool, Query()] = True) -> dict:
    config = _get_config()
    db_path = _get_db_path()
    try:
        tunnels = show_wg_tunnels(config=config, db_path=db_path, include_live=live)
        bgp = show_bgp_peers(config=config, db_path=db_path, include_live=live)
        ibgp = show_ibgp_peers(config=config, db_path=db_path, include_live=live)
    except Dn42CtlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "node_id": config.node_id,
        "wg": [asdict(t) for t in tunnels],
        "bgp": [asdict(p) for p in bgp],
        "ibgp": [asdict(p) for p in ibgp],
    }


# --- Genconf ---


@app.post(f"{router_prefix}/genconf")
def api_genconf(body: GenconfRequest) -> dict:
    try:
        res = genconf(
            config=_get_config(),
            db_path=_get_db_path(),
            overwrite_bird_conf=body.overwrite_bird_conf,
            overwrite_babel_conf=body.overwrite_babel_conf,
        )
    except Dn42CtlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "bird_conf_path": str(res.bird_conf_path),
        "bird_babel_conf_path": str(res.bird_babel_conf_path),
        "bird_roa_v6_conf_path": str(res.bird_roa_v6_conf_path),
        "systemd_roa_timer_enabled": res.systemd_roa_timer_enabled,
        "dummy": asdict(res.dummy) if res.dummy else None,
        "warnings": res.warnings,
    }
