# 输入校验

dn42ctl 对所有用户输入（CLI 参数、API 请求体、配置文件字段、节点提交的 payload）进行统一校验。校验逻辑集中在 `src/dn42ctl/validators.py`，各入口共用同一套校验函数。

## 架构

```
用户输入
  ├─ CLI (Typer)       → _cli_validate()  → validators.validate_xxx() → typer.BadParameter
  ├─ API (Pydantic)    → field_validator   → validators.validate_xxx() → HTTP 422
  ├─ config (TOML)     → load_config()     → validators.validate_xxx() → ConfigError
  └─ 节点 payload      → peer_payload.py   → validators.validate_xxx() → Dn42CtlError → HTTP 400
```

- **validators.py** 中的每个函数接收原始值，返回清理/规范化后的值，或抛出 `ValidationError`。
- 各入口层负责将 `ValidationError` 转换为自己的错误类型。

### 节点 payload 为什么需要单独一层

`config_proposals.payload_json` 与 `node_reports.payload_json` 是**任意 JSON**，由持有 node token 的一方写入，中途没有任何 schema。接受提案与导入上报时，这些字段被强制转换后直接交给 service 层，而 service 层只校验 `listen_port` / `net_backend` / `allowed_ips`，其余字段原样写入数据库。

后果不局限于一条 peer：`bird.conf` 用 `include "<peers_dir>/*";` 加载全部 peer 文件，所以一条语法非法的 peer 会让该节点**整份 BIRD 配置**加载失败。

`services/peer_payload.py` 把 payload 到 service 层参数的解析集中在一处，逐字段过 validators，并把 `KeyError` / `ValueError` / `OverflowError` 统一转成 `Dn42CtlError`——这些异常此前会裸奔成 HTTP 500，在 WS 路径上还会直接拆掉 agent 连接。

两条容易写错的规则：

**`has_wg` 只在 create 时可信。** `modify_ibgp_peer` 根本不接受 `has_wg` 参数——它按**数据库现有行**判断有没有隧道，并且直接拒绝 `has_wg=0` 的行。所以 modify payload 里的 `has_wg` 完全不影响写入结果，只会影响"要不要校验 WireGuard 字段"。让它自称 `false` 就能跳过公钥与 LLA 校验，随后被写成空串，落到一条数据库里 `has_wg=1` 的行上——渲染出的 netdev 带一个空 `PublicKey=`，systemd-networkd 直接拒绝拉起该接口。因此 **modify 始终按"有隧道"校验**，payload 的 `has_wg` 只用于 create。

**类型检查必须排在 `or` 默认值之前。** `peer.get("net_backend") or "networkd"` 这种写法会把 `false` / `0` / `[]` 一并当成"没填"，静默落到默认值上。先判类型、再取默认，才能把"填错了"和"没填"区分开。

## 校验器列表

| 函数 | 输入类型 | 校验规则 | 错误示例 |
|------|---------|---------|---------|
| `validate_asn` | `int` | 1 ~ 4294967295（RFC 6793 的 32 位 AS 号） | `ASN 必须是正整数` / `ASN 超出 32 位范围` |
| `validate_pubkey` | `str` | 非空，标准 base64，解码后恰好 **32 字节**（WireGuard X25519 公钥长度） | `公钥格式不合法` |
| `validate_endpoint` | `str` | `host:port` 或 `[IPv6]:port`，端口 1-65535（位数超长也按格式错误处理，不放 `ValueError` 出去）；支持 `allow_empty` | `Endpoint 格式错误` |
| `validate_ipv6_address` | `str` | 非空，合法 IPv6 地址；允许带 `/prefix`，**但前缀长度也要合法**（0-128） | `不是合法的 IPv6 地址` |
| `validate_ipv4_address` | `str` | 非空，合法 IPv4 地址 | `不是合法的 IPv4 地址` |
| `validate_ipv6_network` | `str` | 非空，合法 IPv6 CIDR 前缀 | `不是合法的 IPv6 CIDR 前缀` |
| `validate_babel_type` | `str` | `wired` / `wireless` / `tunnel`（大小写不敏感） | `type 必须是 wired, wireless, tunnel 之一` |
| `validate_net_backend` | `str` | `networkd` / `nm` / `networkmanager`，返回 `networkd` 或 `nm` | `net_backend 必须是 networkd 或 nm` |
| `validate_listen_port` | `int` | 0（可选允许）或 1-65535 | `ListenPort 超出范围` |
| `validate_rxcost` | `int` | 0-65535 | `rxcost 超出范围` |
| `validate_ownnetset_v6` | `str` | 非空，`[...+...]` 格式 | `OWNNETSETv6 格式不合法` |
| `validate_router_id` | `str` | 合法 IPv4 地址 | `Router ID 不是合法的 IPv4 地址` |

## HTTP 错误码语义

| 状态码 | 触发条件 | 响应格式 |
|-------|---------|---------|
| 400 | 服务层业务逻辑错误（`Dn42CtlError`：peer 已存在、端口冲突等） | `{"detail": "..."}` |
| 401 | Bearer Token 鉴权失败 | `{"detail": "Invalid token"}` |
| 422 | 输入格式/类型错误（Pydantic `field_validator` 或类型校验失败） | `{"detail": [{"loc": [...], "msg": "...", "type": "..."}]}` |

## 错误消息语言

所有校验错误消息使用中文，与项目现有风格一致。

## 两处刻意加严的校验

**WireGuard 公钥按长度校验，不按字符数。** 正则曾经写成 `[A-Za-z0-9+/]{42,44}={0,2}`，
把 42 到 46 个字符全部放行，于是 31 字节和 33 字节的 key 都能通过，而真实的 `wg`
都会报 `Key is not the correct length or format`。字符数与字节数不是一回事，改成
解码后判长度才是这个字段真正的约束。（错误文案里写的"40~44 字符"与那条正则的下限
42 也对不上，一并去掉。）

**IPv6 地址允许带 `/prefix` 是有意的，但斜杠后面必须也校验。** 早先的实现在 `/`
处截断、只验前半段，于是 `fd00::1/not-a-prefix` 与 `fd00::1/999` 都算合法，并被
原样写进 `bird.conf` 的 `neighbor` 行和 networkd 的 `Peer=`。宽松的是"接受哪种
形状"，不是"接受什么内容"。

## config.toml 的类型校验

`load_config` 用 `isinstance` 判类型，而 **Python 的 `bool` 是 `int` 的子类**，所以
`own_asn = true` 会通过 `isinstance(value, int)` 并原样存成 `True`。它不会变成 ASN 1：
模板渲染出来是 `define OWNAS = True;`，BIRD 直接无法解析；而 `dumps_config` 又会把它
按 `true` 写回，`node apply` 之后继续保留。整数字段因此显式排除 `bool`。
