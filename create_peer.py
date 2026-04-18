#!/usr/bin/env python3
import os
import subprocess
import argparse
import random
from pathlib import Path

def generate_wg_keys():
    """调用系统 wg 命令生成 WireGuard 公私钥对"""
    try:
        privkey = subprocess.check_output(['wg', 'genkey']).decode('utf-8').strip()
        pubkey = subprocess.check_output(['wg', 'pubkey'], input=privkey.encode('utf-8')).decode('utf-8').strip()
        return privkey, pubkey
    except FileNotFoundError:
        print("错误：未找到 'wg' 命令。请先安装 wireguard-tools。")
        exit(1)

def generate_lla():
    """生成一个随机的 fe80 LLA 地址，例如 fe80::8391:b5bb/64"""
    return f"fe80::{random.randint(0, 0xffff):04x}:{random.randint(0, 0xffff):04x}/64"

def main():
    parser = argparse.ArgumentParser(description="自动生成 DN42 Peer 配置文件 (systemd-networkd & BIRD)")
    parser.add_argument("--asn", type=str, help="Peer AS 号 (例如 4242421234)")
    parser.add_argument("--pubkey", type=str, default="{用户提供，未提供则保留原样}", help="Peer WireGuard 公钥")
    parser.add_argument("--endpoint", type=str, default="{用户提供，未提供则保留原样}", help="Peer Endpoint (IP:Port)")
    parser.add_argument("--peer-lla", type=str, default="{LLA 地址，用户提供，未提供则保留原样}", help="Peer Link-Local 地址")
    parser.add_argument("--outdir", type=str, default="/etc", help="基础输出目录 (默认为 /etc，可指定其他目录用于测试)")
    args = parser.parse_args()

    # 处理 AS 号相关字段
    asn = args.asn if args.asn else "{ASNUMBER 用户提供，未提供则保留原样}"
    if args.asn and len(args.asn) >= 5:
        as_last4 = args.asn[-4:]
        as_last5 = args.asn[-5:]
    elif args.asn:
        as_last4 = args.asn
        as_last5 = args.asn
    else:
        as_last4 = "{ASNUMBER后四位，用户提供，未提供则保留原样}"
        as_last5 = "{ASNUMBER后五位，用户提供，未提供则保留原样}"

    ifname = f"dn42_{as_last4}"

    # 生成密钥和本地 LLA
    privkey, pubkey = generate_wg_keys()
    local_lla = generate_lla()

    # --- 配置文件内容拼接 ---
    netdev_content = f"""[NetDev]
Name={ifname}
Kind=wireguard

[WireGuard]
PrivateKey={privkey}
ListenPort={as_last5}

[WireGuardPeer]
PublicKey={args.pubkey}
Endpoint={args.endpoint}
AllowedIPs=fe80::/64
AllowedIPs=fd00::/8
"""

    network_content = f"""[Match]
Name={ifname}

[Network]
DHCP=no
IPv6AcceptRA=false
IPForward=yes
IPv4ReversePathFilter=no

KeepConfiguration=yes

[Address]
Address={local_lla}
Peer={args.peer_lla}
"""

    bird_content = f"""protocol bgp {ifname} from dnpeers {{
    bfd graceful;
    bfd {{
        interval 10s;
    }};
    neighbor {args.peer_lla}%{ifname} as {asn};
}}
"""

    # --- 写入文件 ---
    base_dir = Path(args.outdir)
    netdev_path = base_dir / "systemd" / "network" / f"{ifname}.netdev"
    network_path = base_dir / "systemd" / "network" / f"{ifname}.network"
    bird_path = base_dir / "bird" / "peers" / f"{ifname}.conf"

    # 确保目标文件夹存在
    netdev_path.parent.mkdir(parents=True, exist_ok=True)
    bird_path.parent.mkdir(parents=True, exist_ok=True)

    # 写入配置
    with open(netdev_path, 'w', encoding='utf-8') as f:
        f.write(netdev_content)
    with open(network_path, 'w', encoding='utf-8') as f:
        f.write(network_content)
    with open(bird_path, 'w', encoding='utf-8') as f:
        f.write(bird_content)

    # 保存公钥到 ~/peers/
    home_dir = Path.home()
    peers_dir = home_dir / "peers"
    peers_dir.mkdir(parents=True, exist_ok=True)
    pubkey_filename = f"{asn}_pubkey" if args.asn else "unknown_asn_pubkey"
    pubkey_path = peers_dir / pubkey_filename
    with open(pubkey_path, 'w', encoding='utf-8') as f:
        f.write(pubkey)

    # --- 打印结果 ---
    print("✅ 配置文件生成成功！")
    print(f"  - NetDev:  {netdev_path}")
    print(f"  - Network: {network_path}")
    print(f"  - BIRD:    {bird_path}")
    print(f"  - 自动保存的 PubKey 文件: {pubkey_path}")
    print(f"\n🔑 你的 WireGuard 公钥: {pubkey}")
    print(f"📡 自动生成的本地 LLA : {local_lla}")

if __name__ == "__main__":
    main()
