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

`config_proposals.payload_json` 与 `node_reports.payload_json` 是**任意 JSON**，由持有 node token 的一方写入，中途没有任何 schema。接受提案与导入上报时，这些字段被强制转换后直接交给 service 层，而 service 层只校验 `listen_port` / `net_backend` / `allowed_ips`，其余字段原样落库。

后果不局限于一条 peer：`bird.conf` 用 `include "<peers_dir>/*";` 加载全部 peer 文件，所以一条语法非法的 peer 会让该节点**整份 BIRD 配置**加载失败。

`services/peer_payload.py` 把 payload 到 service 层参数的解析集中在一处，逐字段过 validators，并把 `KeyError` / `ValueError` / `OverflowError` 统一转成 `Dn42CtlError`——这些异常此前会裸奔成 HTTP 500，在 WS 路径上还会直接拆掉 agent 连接。

## 校验器列表

| 函数 | 输入类型 | 校验规则 | 错误示例 |
|------|---------|---------|---------|
| `validate_asn` | `int` | 1 ~ 4294967295（RFC 6793 的 32 位 AS 号） | `ASN 必须是正整数` / `ASN 超出 32 位范围` |
| `validate_pubkey` | `str` | 非空，base64 格式，40~44 字符 | `公钥格式不合法` |
| `validate_endpoint` | `str` | `host:port` 或 `[IPv6]:port`，端口 1-65535；支持 `allow_empty` | `Endpoint 格式错误` |
| `validate_ipv6_address` | `str` | 非空，合法 IPv6 地址（允许带 `/prefix`） | `不是合法的 IPv6 地址` |
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
