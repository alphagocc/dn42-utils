from __future__ import annotations

MAX_PORT = 65535
# RFC 6793:AS 号是 32 位无符号数。没有上界的话超大值要到 sqlite 才炸成 OverflowError。
MAX_ASN = 4294967295
FILE_MODE_PRIVATE = 0o600
FILE_MODE_NETDEV = 0o640
BABEL_DEFAULT_RXCOST = 20
BABEL_DEFAULT_TYPE = "tunnel"
BABEL_VALID_TYPES = ("wired", "wireless", "tunnel")
WG_PORT_RANGE = (30000, 49999)
NET_BACKEND_NETWORKD = "networkd"
NET_BACKEND_NM = "nm"
IFNAME_PREFIX_BGP = "dn42_"
IFNAME_PREFIX_IBGP = "wg_"
LIVE_CMD_TIMEOUT = 2

# sync_events(变更通知队列)
SYNC_EVENT_DESIRED = "desired"
SYNC_EVENT_ACCESS_REVOKED = "access_revoked"
SYNC_EVENTS_KEEP = 1000
SYNC_EVENTS_TRIM_EVERY = 256

# SQLite busy timeout(毫秒)。hub 上 server 进程与 CLI 进程会并发访问同一个库文件。
SQLITE_BUSY_TIMEOUT_MS = 5000

# node apply 之后是否 reload networkd/bird。放在 constants 而不是 services.reload,
# 是因为 node_config 要用它,而 services 包的 __init__ 会反向 import node_config。
RELOAD_POLICY_AUTO = "auto"
RELOAD_POLICY_NEVER = "never"
VALID_RELOAD_POLICIES = (RELOAD_POLICY_AUTO, RELOAD_POLICY_NEVER)


class _Unset:
    """区分"字段没出现在 PATCH body 里"(保持不变)与"显式传了 null"(清除/取消管理)。

    `None` 在这两处都是合法值,所以不能用它当哨兵。放在 constants 里是为了让 db 层与
    service 层共用同一个单例而不引入循环依赖。
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNSET"

    def __bool__(self) -> bool:
        return False


UNSET = _Unset()
