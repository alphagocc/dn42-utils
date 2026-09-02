# Auto-peer 公共 API

持有 DN42 ASN 的用户通过 web 表单提交 peering 请求。ASN 归属由 [Kioubit 认证服务](https://dn42.g-load.eu/about/authentication-services/) 按 registry 中 mntner 的 `auth:` 信息验证，签名后重定向回本站；服务端校验签名，将请求写入用户选定节点的 `config_proposals` 队列等待审批。

## 启用条件

环境变量 `DN42CTL_AUTOPEER_DOMAIN` 设为 auto-peer 页面的公开域名，例如 `peer.dn42.example.com`。`dn42ctl serve` 也接受同名的 `--autopeer-domain` 参数。该值必须与浏览器访问 peer 页面时使用的主机名一致，认证响应里的 `domain` 字段按它比对。至少一个受管节点需要开放 auto-peer 入口（见下方「可选节点」）。

未配置域名时所有 `/api/public/auto-peer/*` 返回 `503 {"detail": "auto-peer disabled (DN42CTL_AUTOPEER_DOMAIN not set)"}`。

签名校验只用内置公钥做本地运算，服务端不发起外部请求，`dn42ctl-server.service` 的 `IPAddressDeny=any` 保持原样。

## 可选节点

`managed_nodes.auto_peer` 决定节点是否出现在公共页面上，默认 0。新增节点与升级产生的存量节点都是关闭状态，运维显式开放后才对外可见：

```bash
dn42ctl node auto-peer <node_id> --enable
dn42ctl node auto-peer <node_id> --disable
```

admin 后台的节点编辑表单里有同一个开关，REST 是 `PATCH /api/admin/nodes/{node_id}` 的 `auto_peer` 字段。

列表与提交共用 `ManagedNodeStore.list_auto_peer()`，条件是 `auto_peer=1 AND enabled=1`。禁用节点会同时关闭它的 auto-peer 入口；关闭后停留在旧页面上的提交会被同一处判定拒绝（`400`）。

公开列表只返回 `node_id`、`name` 与 `endpoint_host`。`own_ipv6`、`router_id`、`write_policy` 不出现在公共响应里。

节点名称是请求方在下拉列表里看到的文字，`dn42ctl node rename <node_id> <name>` 或 admin 后台都能修改。self 节点的名称同样由运维管理，`dn42ctl serve` 的自注册只在首次创建时写入 `self`，见 [`sync_hub_spoke.md`](sync_hub_spoke.md)。

## 端到端流程

```
┌──────────────┐ 1.认证跳转  ┌─────────────────────────┐
│   browser    │────────────▶│ dn42.g-load.eu/auth/    │
│   /peer      │  ?return=   │   按 registry 的 auth:  │
│              │             │   信息验证用户          │
│              │◀────────────│ 302 重定向              │
│              │  ?params=&signature=                  │
│              │             └─────────────────────────┘
│              │ 2.换取      ┌─────────────────────────┐
│              │  session    │                         │
│              │────────────▶│ POST /session           │
│              │             │   ECDSA P-521 验签      │
│              │             │   domain + 时间窗校验   │
│              │◀────────────│ {peer_session_token}    │
│              │             └─────────────────────────┘
│              │ 3.选节点    ┌─────────────────────────┐
│              │────────────▶│ GET /nodes              │
│              │◀────────────│ [{node_id, name, host}] │
│              │             └─────────────────────────┘
│              │ 4.提交      ┌─────────────────────────┐
│              │────────────▶│ POST /submit (Bearer)   │
│              │  {node_id}  │   submit_proposal(...)  │
│              │◀────────────│ {proposal_id, status}   │
└──────────────┘             └─────────────────────────┘
```

跳转地址是一个普通的 GET 表单，页面不加载第三方脚本：

```html
<form action="https://dn42.g-load.eu/auth/" method="get">
  <input type="hidden" name="return" value="https://peer.dn42.example.com/">
  <button type="submit">Authenticate with Kioubit.dn42</button>
</form>
```

重定向回来后 `params` 与 `signature` 出现在 query 中。页面换取 session 成功后立即用 `history.replaceState` 清除 query，因为签名是一次性的，刷新页面会重复提交同一份响应。

## 路由

| 方法 | 端点 | Bearer | 入参 | 出参 |
|------|------|--------|------|------|
| POST | /api/public/auto-peer/session | 无 | `{params, signature}` | `{peer_session_token, verified_asn, verified_mntner, expires_in_seconds}` |
| GET | /api/public/auto-peer/nodes | 无 | 无 | `{nodes:[{node_id, name, endpoint_host}]}` |
| POST | /api/public/auto-peer/submit | peer-session | `{node_id, wg_public_key, endpoint?, peer_lla, listen_port?}` | `{proposal_id, status, node_id, node_name, received_at, message}` |

### 错误码

| 状态码 | 含义 |
|--------|------|
| `400` | 签名校验失败、载荷不是合法 JSON、`domain` 属于其他站点、ASN 不合法、`node_id` 不在开放列表里 |
| `401` | submit 缺少 peer-session bearer |
| `403` | peer-session 与请求不匹配 |
| `410` | 认证响应超出时间窗或已被使用；peer-session 过期或已被使用 |
| `422` | pydantic 校验失败（字段缺失或为空） |
| `503` | `DN42CTL_AUTOPEER_DOMAIN` 未配置 |

## 认证响应校验

`services/kioubit_auth.py` 的 `verify_auth_response()` 按顺序完成四步：

1. `signature` base64 解码后，用内置公钥对 `params` 原文做 ECDSA(P-521) + SHA-512 验签。签名覆盖的是回传的 base64 字符串本身，先解码再验签会验错对象。
2. `params` base64 解码得 JSON 对象。
3. `domain` 必须等于配置的域名。签名覆盖了这个字段，防止为其他站点签发的响应在本站换取 session。传入值允许带 `https://` 前缀与结尾斜杠。
4. `time` 与当前时间相差不超过 60 秒，与该服务其余各语言参考实现取同一个窗口。

公钥内置在模块常量 `KIOUBIT_PUBLIC_KEY_PEM` 中，来源 `https://dn42.g-load.eu/auth/assets/public_key.pem`。公钥写在代码里而不在运行时获取，因为 server 没有出网权限，且运行时获取所依赖的 TLS 链正是攻击者需要攻破的那条。

载荷中使用的字段：

| 字段 | 说明 |
|------|------|
| `asn` | 字符串形式的 AS 号，转 int 后经过 `validate_asn` |
| `effective_mnt` | 实际完成认证的 maintainer，记入 session |
| `mnt` | 该 ASN 的全部 maintainer。`effective_mnt` 缺失时取第一个；旧版本响应在这里放的是单个字符串，两种形态都接受 |
| `authtype` | 认证方式，例如 `logincode` |
| `time` | 签发时刻的 unix 时间戳 |
| `domain` | 用户意图认证的域名 |

`tests/test_services_kioubit_auth.py` 使用该服务公开的测试向量，本地实现与服务端产生分歧时测试会失败。

## 会话管理

`services/auto_peer.py` 模块级 `_sessions: dict[str, _Session]` 与 `_consumed: dict[str, float]`，由 `threading.Lock` 保护，每次写操作前调用 `_purge_expired_locked()`。

| 字段 | 类型 | 说明 |
|------|------|------|
| `Session.token` | `str` (`secrets.token_urlsafe(32)`) | |
| `Session.asn` | `int` | 来自签名载荷 |
| `Session.mntner` | `str` | 同上 |
| `Session.expires_at` | `float` (`time.monotonic()`) | 当前时刻 + `_SESSION_TTL_SECONDS=900` |

认证响应是一次性的：签名摘要在 `open_session` 时写入 `_consumed`，存活 120 秒（覆盖 60 秒时间窗的两个方向）。同一份响应无法换取第二个 session。

Session 也是一次性的：`submit_peer` 成功后 `pop`；失败时保留，允许用户修改字段后重新提交。进程重启会清空全部会话，等同于强制重新认证。

### 并发下的一次性保证

提案写库发生在锁外，耗时远长于锁的持有时间。如果不做额外处理，N 个并发请求可以同时读到同一个 session、各自创建一条提案（典型的 check-then-act 问题）。

处理方式是在锁内设置 in-flight 标记：第一个请求认领 session，后续请求立即收到「正在处理中」响应。写库失败或异常时在锁内清除标记，允许重试；成功则 `pop`。

并发提交在安全层面没有影响：所有 session 携带同一个 `(asn, mntner)`，竞态的前提是已经持有合法认证响应，持有者本来就能多次完成公开流程。实际后果只是重复的 pending 提案，而 `submit_peer` 传 `config=None` 使 auto-accept 在 `services/proposals.py` 中提前返回，提案到不了主表。设置 in-flight 标记是为了让"一次性"的语义严格成立。

## proposal payload

校验通过后 `submit_peer` 调用：

```python
payload = build_peer_add_payload(peer_kind="bgp", peer={
    "peer_asn": session.asn,
    "peer_public_key": form.wg_public_key,
    "endpoint": form.endpoint or "",
    "peer_lla": form.peer_lla,
    "net_backend": "networkd",
    "listen_port": form.listen_port,  # 可空
})
submit_proposal(
    db_path=db_path,
    node_id=target_id,      # 请求里选定的节点,经 list_auto_peer() 复核
    source="push",
    kind="peer_add",
    payload=payload,
    config=None,
)
```

`source="push"` 复用既有取值。`peer_kind="ibgp"` 不支持 auto-peer（iBGP 仅在内部节点间使用）。

## 已知限制

| 限制 | 说明 |
|------|------|
| 无应用层限流 | 依赖 nginx `limit_req_zone $binary_remote_addr zone=ap:10m rate=10r/m;` 限制 `/api/public/` |
| 对认证服务的信任 | ASN 归属完全由 Kioubit 认证服务判定。该服务被攻破或私钥泄漏时，任何人都能声称持有任意 ASN。提案仍需运维 accept，攻击者无法直接写入主表 |
| 认证响应在浏览器侧可见 | `params` 与 `signature` 经 query 回传，会进入浏览器历史与 Referer。60 秒时间窗加上一次性摘要将暴露窗口压到最小 |
| 没有 captcha | 任何能通过认证的 ASN 都能提交提案，但提案需要运维 accept |
| 公开的节点清单 | 开放 auto-peer 意味着公布该节点的名称与 `endpoint_host`。不希望暴露的节点保持开关关闭即可 |
