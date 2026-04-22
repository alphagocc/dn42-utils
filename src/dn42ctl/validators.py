from __future__ import annotations

from dn42ctl.constants import MAX_PORT


class ValidationError(ValueError):
    pass


def validate_listen_port(value: int, *, allow_zero: bool = False) -> int:
    lo = 0 if allow_zero else 1
    if value < 0 or value > MAX_PORT:
        raise ValidationError(f"ListenPort 超出范围 ({lo}-{MAX_PORT}): {value}")
    return value


def validate_rxcost(value: int) -> int:
    if value < 0 or value > MAX_PORT:
        raise ValidationError(f"rxcost 超出范围 (0-{MAX_PORT}): {value}")
    return value
