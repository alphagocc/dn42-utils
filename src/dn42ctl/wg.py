from __future__ import annotations

import random
import subprocess


class WireGuardError(RuntimeError):
    pass


def generate_wg_keypair() -> tuple[str, str]:
    try:
        privkey = subprocess.check_output(
            ["wg", "genkey"], text=True, stderr=subprocess.STDOUT
        ).strip()
        pubkey = subprocess.check_output(
            ["wg", "pubkey"], input=privkey, text=True, stderr=subprocess.STDOUT
        ).strip()
    except FileNotFoundError as exc:
        raise WireGuardError("未找到 'wg' 命令，请先安装 wireguard-tools") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.output.strip() if isinstance(exc.output, str) else ""
        raise WireGuardError(f"wg 命令执行失败: {detail}" if detail else "wg 命令执行失败") from exc
    if not privkey or not pubkey:
        raise WireGuardError("wg 返回空密钥")
    return privkey, pubkey


def generate_random_lla_cidr() -> str:
    return f"fe80::{random.randint(0, 0xFFFF):04x}:{random.randint(0, 0xFFFF):04x}/64"
