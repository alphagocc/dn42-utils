"""生成文件的权限与属组:谁读这个文件,决定了它怎么写。

dn42ctl 有三处写同一批文件:CLI 的 genconf(`services/core.py`)、常驻 agent 的
apply(`services/node_apply.py`)、dn42-dummy 接口(`services/dummy.py`)。三处各自
声明权限的时候分叉过一次:agent 把 `.network` 写成 0600、`.netdev` 停在 root 组,
systemd-networkd 以 systemd-network 用户运行,重启后一个文件都打不开,全部 wg 接口
失去地址。因此权限只在这里声明一次,三处都从这里取。

新增一类文件时在这里加一条,并写清读它的是哪个进程、以什么身份运行。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dn42ctl.constants import FILE_MODE_PRIVATE
from dn42ctl.fs import chmod_best_effort, chown_best_effort


@dataclass(frozen=True)
class FilePolicy:
    mode: int
    #: 属组名。None 表示保持写入者的默认属组。
    group: str | None = None


#: `.netdev` 含 WireGuard 私钥,可读范围止于属组。
NETDEV = FilePolicy(0o640, "systemd-network")

#: `.network` 只有链路本地地址,没有秘密;networkd 必须读得到。
NETWORK = FilePolicy(0o644)

#: bird 以 `-u bird -g bird` 降权运行,`birdc configure` 的重读发生在降权之后。
BIRD = FilePolicy(0o644)

#: dn42ctl 自己的 config.toml / node.toml,含 token,只给 root。
PRIVATE = FilePolicy(FILE_MODE_PRIVATE)


def apply_policy(path: Path, policy: FilePolicy) -> None:
    """尽力而为地套用权限:非 root 运行时 chown 必然失败,不应当因此中断写入。"""
    chmod_best_effort(path, policy.mode)
    if policy.group is not None:
        chown_best_effort(path, 0, policy.group)


def ensure_policy(path: Path, policy: FilePolicy) -> None:
    """给内容不归工具管的文件校正权限,内容一个字节都不碰。

    `extra.conf` 属于这一类:内容归运维,能否被读取却由 bird 决定,`bird.conf` 里的
    include 打不开它就整份配置加载失败。只在创建时套一次权限不够,早先版本创建的
    0600 文件不会因为后来改对了默认值而自行变好。
    """
    if path.exists():
        apply_policy(path, policy)


__all__ = ["BIRD", "NETDEV", "NETWORK", "PRIVATE", "FilePolicy", "apply_policy", "ensure_policy"]
