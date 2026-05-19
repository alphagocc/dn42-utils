# Auto-peer 公共 API

允许任何持有合法 dn42 ASN 的用户通过 web 表单提交 peering 请求；服务端用 dn42 registry 中的 `mntner.auth:` 信息挑战用户证明 mntner 所有权，校验通过后把请求写入 self 节点的 `config_proposals` 队列等运维人员审批。

## 启用条件

- `config.toml` 中设置 `dn42_registry_path = "/path/to/registry"`。
- 该目录必须包含 dn42 registry 的标准布局：`data/aut-num/`, `data/mntner/`, `data/key-cert/`。
- 服务端可执行 `ssh-keygen` 与 `gpg`（SSH-only 部署可以省略 gpg，pgp-fingerprint 类型会被标为不可用）。

未配置 `dn42_registry_path` 时所有 `/api/public/auto-peer/*` 返回 `503 {"detail": "auto-peer disabled (dn42_registry_path not set)"}`。

## 端到端流程

```
┌──────────────┐ 1.lookup    ┌─────────────────────────┐
│   browser    │────────────▶│ POST /lookup            │
│   /peer      │   {asn}     │   读 data/aut-num/ASxx  │
│              │             │   读 data/mntner/...    │
│              │◀────────────│   返回 mntner+auth 列表 │
│              │             └─────────────────────────┘
│              │ 2.challenge ┌─────────────────────────┐
│              │────────────▶│ POST /challenge         │
│              │             │   随机 32B nonce        │
│              │             │   存 challenge store    │
│              │◀────────────│   {challenge_id, nonce} │
│              │             └─────────────────────────┘
│              │ 3.verify    ┌─────────────────────────┐
│              │────────────▶│ POST /verify            │
│              │  {sig}      │   ssh-keygen -Y verify  │
│              │             │   或 gpg --verify       │
│              │             │   成功:burn challenge   │
│              │◀────────────│   {peer_session_token}  │
│              │             └─────────────────────────┘
│              │ 4.submit    ┌─────────────────────────┐
│              │────────────▶│ POST /submit (Bearer)   │
│              │             │   submit_proposal(...)  │
│              │◀────────────│   {proposal_id,...}     │
└──────────────┘             └─────────────────────────┘
```

## 路由

| 方法 | 路径 | Bearer | 入参 | 出参 |
|------|------|--------|------|------|
| POST | /api/public/auto-peer/lookup | – | `{asn:int}` | `{asn, mntners:[{name, auth_options:[{index, scheme, fingerprint?}]}]}` |
| POST | /api/public/auto-peer/challenge | – | `{asn, mntner, auth_index}` | `{challenge_id, nonce, namespace, expires_at, scheme}` |
| POST | /api/public/auto-peer/verify | – | `{challenge_id, signature}` | `{peer_session_token, expires_at, verified_asn, verified_mntner}` |
| POST | /api/public/auto-peer/submit | peer-session | `{peer_kind:"bgp", wg_public_key, endpoint?, peer_lla, net_backend?, listen_port?}` | `{proposal_id, status, our_wg_public_key, our_endpoint?, our_listen_port}` |

### 错误码

- `400`: 参数格式不合法 / payload 不完整。
- `403`: peer-session bearer 不存在或 `peer_asn` 不匹配。
- `404`: `aut-num/AS<n>` 或 `mntner/<MNT>` 不存在。
- `410 Gone`: 挑战过期或已被使用。
- `422`: pydantic 校验失败。
- `503`: `dn42_registry_path` 未配置。

## Registry 解析

只接受形如 `aut-num: AS<digits>` 的文件。多个 `mnt-by:` 都被收集；空白行与 `#` 开头行忽略。

`mntner.auth:` 支持的方案：

| Scheme prefix | 实例 | 备注 |
|---------------|------|------|
| `ssh-ed25519` | `ssh-ed25519 AAAA...` | 走 `ssh-keygen -Y verify` |
| `ssh-rsa` | `ssh-rsa AAAA...` | 同上 |
| `ecdsa-sha2-nistp256` | `ecdsa-sha2-nistp256 AAAA...` | 同上 |
| `ecdsa-sha2-nistp521` | `ecdsa-sha2-nistp521 AAAA...` | 同上 |
| `sk-ssh-ed25519@openssh.com` | `sk-ssh-... AAAA...` | 同上（FIDO key） |
| `sk-ecdsa-sha2-nistp256@openssh.com` | `sk-ecdsa-... AAAA...` | 同上 |
| `pgp-fingerprint` | `pgp-fingerprint <40-hex>` | 走 gpg；读取 `data/key-cert/PGPKEY-<last8>` |
| `ed25519-pw` | `ed25519-pw <base64-hash>` | **当前不支持**；在 lookup 返回中标为 `unsupported`，无法选择 |

`auth_index` 按出现顺序在 mntner 文件里递增（仅含**支持**的方案），并与 lookup 响应一致。

### 路径安全

- ASN 仅接受 `^[0-9]+$` 数字，转成 `AS<N>` 文件名。
- 维护者名仅接受 `^[A-Z0-9-]+$`，且必须等于 mnt-by 中真实出现的值。
- PGP 文件名按 `PGPKEY-<fingerprint[-8:].upper()>` 拼接，且只接受 40 位十六进制 fingerprint。
- 所有解析后的 `Path` 调用 `.resolve()` 并校验仍在 `dn42_registry_path` 下，防止 `..` 注入。

## 挑战 / 会话 store

`services/auto_peer.py` 模块级 `_challenges: dict[str, _Challenge]` 与 `_sessions: dict[str, _Session]`，由 `threading.Lock` 保护。每次写操作前调用 `_purge_expired()`。

| 字段 | 类型 | 说明 |
|------|------|------|
| `Challenge.id` | `str` (uuid4) | URL-safe |
| `Challenge.nonce` | `str` (hex 64 字符) | 32 字节随机熵 |
| `Challenge.namespace` | `str` | 常量 `"dn42ctl-autopeer"` |
| `Challenge.asn` | `int` | 用户声明的 ASN |
| `Challenge.mntner` | `str` | |
| `Challenge.auth_line` | `str` | 完整的 `auth:` 行（验证用） |
| `Challenge.scheme` | `str` | `ssh` 或 `pgp` |
| `Challenge.expires_at` | `float` (`time.monotonic()`) | 现在 + `_CHALLENGE_TTL_SECONDS=600` |
| `Session.token` | `str` (`secrets.token_urlsafe(32)`) | |
| `Session.asn` | `int` | |
| `Session.mntner` | `str` | |
| `Session.expires_at` | `float` | 现在 + `_SESSION_TTL_SECONDS=900` |

- **一次性 challenge**：`verify_challenge` 成功后 `pop`；失败保留以容许重试。
- **一次性 session**：`submit_peer` 成功后 `pop`；失败保留以让用户改字段重交。
- 进程重启即清空所有挑战/会话（设计如此，节点重启等价于强制重新认证）。

## 签名验证

`services/crypto_verify.py` 提供两个纯 stdlib subprocess 封装：

### SSH

```
verify_ssh(message: bytes, signature: str, allowed_pubkey: str, namespace: str) -> bool
```

实现：在 `tempfile.TemporaryDirectory` 里写 `allowed_signers`（格式 `dn42@<asn>-<mntner> <pubkey-line>`）+ `sig.asc`（用户回填）+ `msg.bin`，然后

```
ssh-keygen -Y verify -n <namespace> -I dn42@<asn>-<mntner> -s sig.asc -f allowed_signers < msg.bin
```

5 秒超时；返回码 0 → True，其他 / 异常 → False。

### PGP

```
verify_pgp(message: bytes, signature: str, ascii_key: str) -> bool
```

`tempfile.TemporaryDirectory()` 内做 fresh `--homedir`：

```
gpg --homedir <tmp> --batch --no-tty --no-keyserver --no-auto-key-locate --import <key.asc>
gpg --homedir <tmp> --batch --no-tty --no-keyserver --verify <signed-message.asc>
```

由于挑战 nonce 是用户拿来 clear-sign 的，签名输入是 `gpg --clearsign` 的 ASCII 装甲整体；服务端用 `--verify` 同时校验签名和提取明文，比较解出来的明文等于原始 nonce。

5 秒超时；任何 subprocess 失败 / 明文不匹配 → False。

## proposal payload

verify 成功后 `submit_peer` 调用：

```python
payload = build_peer_add_payload(peer_kind="bgp", peer={
    "peer_asn": session.asn,
    "peer_public_key": form.wg_public_key,
    "endpoint": form.endpoint or "",
    "peer_lla": form.peer_lla,
    "net_backend": form.net_backend or "networkd",
    "listen_port": form.listen_port,  # 可空
})
submit_proposal(
    db_path=db_path,
    node_id=self_node_id,   # 来自 ManagedNodeStore.get_self()
    source="push",
    kind="peer_add",
    payload=payload,
    config=config,
)
```

`source="push"` 复用既有取值；`peer_kind="ibgp"` 暂时不支持自动 peer（iBGP 仅在内部节点间使用）。

## 威胁模型 / 已知限制

- **无应用层限流**：靠 nginx `limit_req_zone $binary_remote_addr zone=ap:10m rate=10r/m;` 限制 `/api/public/`。
- **registry 来源信任**：服务端默认 registry 已通过 git fetch 同步过；如果 registry 仓库被污染，攻击者可以替换 `auth:` 行。运维需保证 `dn42_registry_path` 来自可信 git remote。
- **subprocess 风险**：所有调用都禁用 stdin 解析用户控制字符，签名通过文件传入；不构造 shell 字符串，使用 list 形式调用。
- **TOCTOU**：lookup -> challenge -> verify 期间 mntner 文件被替换的窗口存在但短（10 分钟挑战 TTL）；可以接受。
- **PGP 密钥导入副作用**：每次 verify 用全新 `--homedir`，进程结束后清空，不会污染系统 keyring。
- **没有 captcha**：任何 ASN 只要 mntner 私钥泄漏 / 持有就能提交 proposal，但 proposal 仍需运维 accept，没有自动落地写权限。
