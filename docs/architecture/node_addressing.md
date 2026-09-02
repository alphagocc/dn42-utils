# 节点地址集中管理

节点的"IP"在本项目里分为三个值。本文定义它们的存储位置、权威来源和变更传播方式。

数据库 schema 见 [`database.md`](database.md)，同步机制见 [`sync_hub_spoke.md`](sync_hub_spoke.md)，路由见 [`rest_api.md`](rest_api.md)。

## 1. 三种"节点地址"

| 名称 | 含义 | 权威存储 | 消费者 |
|---|---|---|---|
| 公网 endpoint host | 其他节点连接本节点时使用的主机名或 IP | `managed_nodes.endpoint_host` | 其他节点的 `ibgp_peers.endpoint` 的主机部分 |
| DN42 地址 | 本节点的 ULA `/128`，绑定在 `dn42-dummy` 上，同时作为 `krt_prefsrc` | `managed_nodes.own_ipv6` | 本节点的 `bird.conf` + `dn42-dummy.network`；其他节点的 `ibgp_peers.peer_ip`（bird `neighbor`） |
| router id | bird `router id`，IPv4 字面量 | `managed_nodes.router_id` | 仅本节点的 `bird.conf` |

v10 之前，endpoint host 散落在各节点 `ibgp_peers` 行里，每行各存一份冗余副本；后两者只存在于每台机器本地的 `/etc/dn42ctl/config.toml`。没有任何一处是权威的，也没有任何机制把一条 `ibgp_peers` 行与它所指向的节点关联起来。

> `own_asn` / `ownnet_v6` / `ownnetset_v6` 是全 AS 常量，不应升级为 per-node 列。如果将来需要中心渲染完整 `bird.conf`，应当使用单行 `site_settings` 表。

## 2. NULL 语义：未纳入中心管理

三个地址列全部可空且没有 DEFAULT。`NULL` 表示该字段不由中心管理：desired state 不下发它，节点 `config.toml` 里的现有值原样保留。

迁移之后所有节点的三列都是 NULL，行为与升级前一致。管理员显式填写之后，中心才开始接管该字段。清除一个字段（PATCH 显式传 `null`）表示交还本地管理。

## 3. `ibgp_peers.remote_node_id`

反向链接列，指向这条 peer 记录所代表的受管节点。缺少它，传播无法判定改写目标。

不加 FOREIGN KEY，删除节点 A 不能级联删掉节点 B 指向 A 的 peer 行。悬空引用无害：传播时查不到地址则跳过并告警。

填写入口：`dn42ctl ibgp peer add/modify --remote-node-id`、REST 的同名字段、web 表单里的节点下拉。也可以使用 `dn42ctl node mesh backfill --dry-run` 按 `own_ipv6 == peer_ip` 唯一匹配一次性写入。这个动作不放进迁移，迁移时所有 `own_ipv6` 为 NULL，匹配不到任何行。

## 4. 传播规则

修改节点 A 的地址后，遍历所有 `remote_node_id = A` 的 `ibgp_peers` 行（分布在其他节点的分区里）。

### 传播改写的字段

| 字段 | 规则 |
|---|---|
| `endpoint` | 只替换主机部分，端口原样保留（NAT 映射下端口与对端 `listen_port` 合法地不一致） |
| `peer_ip` | ← A 的 `own_ipv6`；`own_ipv6` 为空时不传播（空 `peer_ip` 会让对端 apply 抛异常） |

### 保持原样的字段

| 字段 | 原因 |
|---|---|
| `endpoint` 的端口部分 | NAT 映射下端口与 `listen_port` 合法不一致 |
| `router_id` | 只影响 A 自己的 `bird.conf` |
| `local_lla` / `peer_lla` | 隧道链路本地地址，与公网地址无关 |
| `bgp_peers` 全部字段 | 属于其他运营者的 ASN |

### 需要人工处理的情况

必须出现在 API 响应的 `warnings[]` 里并由 UI 显示：

| 情况 | 处理 |
|---|---|
| `endpoint` 为空 | 保持为空（被动侧无端口可保留） |
| `endpoint` 无法解析 | 原样保留 |
| `remote_node_id` 为 NULL | 传播无法匹配到该行 |
| 目标节点 `own_ipv6` 仍为 NULL | 无操作，告知管理员 |

### 写入原子性与变更通知

`managed_nodes` 字段与所有被传播的 `ibgp_peers` 行在一个事务里写入。写入后为 A 和每个受影响的 B 各发一条 `desired` 事件（去重），后续推送复用现有机制。

### 延后：互惠端口修复

B→A 的端口可以从 A 侧反向行的 `listen_port` 推出，但该推导与 NAT 场景冲突，推导错误意味着静默断开隧道。将来做成只报告的 `dn42ctl node mesh check`。

## 5. 下发：desired state 的 `node` 块

`DesiredState` 增加 `node` 块，三列全部为 NULL 时整个键省略：

```json
{
  "node_id": "...", "revision": "...", "generated_at": "...",
  "bgp_peers": [], "ibgp_peers": [],
  "node": {"own_ipv6": "fd42:4242:1234::1", "router_id": "172.20.1.1"}
}
```

`endpoint_host` 不在下发范围内，它仅供其他节点建立隧道时使用，本节点的 apply 用不到。

`compute_content_digest` / `_compute_revision` 只在 `node` 块非空时才将其放进 canonical JSON。否则升级瞬间全网每个节点的哈希都会变，触发一轮无意义推送。旧 `config_revisions` 快照里没有这个键，pin 回放路径统一使用 `payload.get("node", {})`。

## 6. apply 侧行为

当且仅当 payload 带非空 `node` 块时，`node apply` 才渲染 `config.toml` / `bird.conf` / `dn42-dummy.*`。没有该块的节点，写入文件集与本特性引入前逐字节一致（有回归测试）。

三个动作按键是否存在独立触发：

1. **重写 `config.toml`**：合并下发字段，先比较、有差异才写入。`save_config` 使用 `tomli_w` 整体重写，注释和未知键会丢失，这正是比较优先的理由。

2. **重渲 `bird.conf`**：使用与 peer 文件相同的原子写 + diff 管线。`include` 路径与 peer 文件写入位置同出一源，都取自本机 `config.toml` 的 `[paths]`（见 [`paths.md`](paths.md)）。`extra.conf` 仅在缺失时作为占位文件进入写入列表，它的内容属于用户（见 [`bird_extra_conf.md`](bird_extra_conf.md)）。

3. **重写 `dn42-dummy.netdev` / `.network`**：仅当 `dummy_backend = "networkd"` 时写入。不调用 `ensure_dummy_interface`，因为它 shell out 到 `networkctl`/`nmcli`，会绕过 diff/dry-run 机制。NetworkManager 管理该接口时跳过并告警，需要在本机执行一次 `dn42ctl genconf`。

本地 `config.toml` 缺失或损坏时记 warning 并跳过全部三步。纯 spoke 可能只执行过 `node init`，本机从未生成过 `config.toml`；`bird.conf` 需要 `own_asn` / `ownnet_v6` / `ownnetset_v6` 等不在下发范围内的字段，缺失时模板在 `StrictUndefined` 模式下会抛异常。

## 7. seeding

| 目标 | 方式 |
|---|---|
| self 节点 | `serve_bootstrap` 启动时，仅当 DB 列为 NULL 时用本机 `config.toml` 写入 |
| 远端节点 | web Nodes 页表单，或 `dn42ctl node set-address <id> [...] [--dry-run]` |

首次升级后 hub 采纳本机配置，此后 DB 是权威；agent 按 DB 写 `config.toml`，bootstrap 只在 DB 为空时读 `config.toml`。

> 让 agent 上报本地地址供 hub 预填是延后项。上报不能写主表（见 `sync_hub_spoke.md`），将来只能做成表单里的"建议值"。

## 8. reload

`node apply` 根据本次实际写入或删除的路径决定 reload：

| 条件 | 命令 |
|---|---|
| 有文件属于 `networkd_dir` | `networkctl reload` |
| 有文件属于 `bird_peers_dir`，或等于 `babel.conf` / `bird.conf` | `birdc configure` |

先 `networkctl` 启动接口，再 `birdc` 加载引用这些接口的 protocol。使用 `birdc configure` 而非 `configure soft`，因为 soft 不能正确加载新增的 protocol。什么都没变则跳过。

Reload 采用尽力而为策略，失败进 `ApplyResult.warnings` 并随 `apply_result` 上报，不中断 apply。

退出开关（reload 是节点本地决策，hub 侧不设开关）：`node.toml` 的 `[apply] reload = "never"` 或 `dn42ctl node apply --no-reload`。

> 两条 reload 命令只让守护进程重读配置文件，不修改路由表；`RouteTable=off` 由 netdev 模板保证。

### Sandbox 约束

`dn42ctl-node-agent.service` 的 `ProtectSystem=strict` 允许写 `/run`，`networkctl` 和 `birdc` 应该能正常使用。个别发行版可能需要给 `ReadWritePaths` 加 `/run/bird`。尽力而为的设计让这种意外退化成一行日志。

## 9. 两个 node_id 的分叉隐患

hub 上存在两个互不校验的 UUID：

- `/etc/dn42ctl/config.toml` 的 `node_id`，由 `dn42ctl init` 生成，用于 peer 表分区
- `/var/lib/dn42ctl/self_node_id`，由 `serve_bootstrap` 生成，对应 `managed_nodes.is_self`，驱动 desired state

在引入节点作用域之前，admin API 用前者写 peer，desired state 用后者读 peer。两者分叉后，管理员在 UI 中添加的 peer 永远不会下发，且没有任何报错。

修复方案（以 `is_self` 行为基准，保全既有 peer 行所在的分区）：

1. admin 路由的默认作用域解析到 `is_self` 行，与 desired state 的读取来源统一，消除静默失败的路径。
2. `run_self_registration` 返回 `config_node_id_mismatch`，`serve` 启动时打醒目 warning。
3. `/api/show/all` 暴露该标志，web Overview 页渲染警告横幅。
4. 修复工具 `dn42ctl node adopt-self [--from <uuid>] [--dry-run]`：在一个事务里把 peer 行从失效分区迁移到 self 节点，并对目标节点发一条 `desired`。目标分区非空时拒绝执行，合并策略只能由人来定。
