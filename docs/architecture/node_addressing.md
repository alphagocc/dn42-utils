# 节点地址集中管理

节点的"IP"在本项目里从来不是一个字段，而是三件不同的东西。这份文档定义它们各自存在哪里、谁是权威、改动如何传播，以及为什么某些看起来该自动化的事情被刻意留给人工。

相关：数据库 schema 见 [`database.md`](database.md)，同步机制见 [`sync_hub_spoke.md`](sync_hub_spoke.md)，路由见 [`rest_api.md`](rest_api.md)。

## 1. 三种"节点地址"

| 名称 | 含义 | 权威存储 | 消费者 |
|---|---|---|---|
| **公网 endpoint host** | 别的节点拨向本节点时用的主机名/IP | `managed_nodes.endpoint_host` | **其他节点**的 `ibgp_peers.endpoint` 的主机部分 |
| **DN42 地址** | 本节点的 ULA `/128`，绑在 `dn42-dummy` 上，同时是 `krt_prefsrc` | `managed_nodes.own_ipv6` | 本节点的 `bird.conf` + `dn42-dummy.network`；**其他节点**的 `ibgp_peers.peer_ip`（bird `neighbor`） |
| **router id** | bird `router id`，IPv4 字面量 | `managed_nodes.router_id` | 仅本节点的 `bird.conf` |

在 v10 之前，前者只存在于各节点 `ibgp_peers` 行的反范式化副本里，后两者只存在于每台机器本地的 `/etc/dn42ctl/config.toml`。**没有任何一处是权威的**，也没有任何东西把一条 `ibgp_peers` 行和它所指向的节点关联起来。

> **不要**把 `own_asn` / `ownnet_v6` / `ownnetset_v6` 加成 per-node 列。它们在 DN42 里是**全 AS 常量**，每个节点一份就是 N 处可以写错。如果将来需要中心渲染完整 `bird.conf`，正确的形状是单行 `site_settings` 表。

## 2. NULL 语义：未纳入中心管理

三个地址列**全部可空且没有 DEFAULT**。

> **`NULL` = "该字段不由中心管理"**：desired state 不下发它，节点 `config.toml` 里的现有值原样保留。

这是升级瞬间不砸掉一台正在正常工作的节点的唯一安全默认值。迁移之后所有节点的三列都是 NULL，行为与升级前完全一致；管理员显式填写之后，中心才开始接管该字段。

清除一个字段（PATCH 显式传 `null`）的语义是**交还本地管理**，不是"把节点的地址清空"。

## 3. `ibgp_peers.remote_node_id`

反向链接列，指向这条 peer 记录所代表的受管节点。没有它，"节点 A 的地址变了，改写所有指向 A 的行"是不可判定的。

- **不加 FOREIGN KEY。** 删除节点 A 不能级联删掉节点 B 指向 A 的 peer 行——那是 B 的配置，不是 A 的。悬空引用无害：传播时查不到地址就跳过并告警。
- 填写入口：`dn42ctl ibgp peer add/modify --remote-node-id`、REST 的同名字段、web 表单里的节点下拉。
- 也可以用 `dn42ctl node mesh backfill --dry-run` 一次性回填（按 `managed_nodes.own_ipv6 == ibgp_peers.peer_ip` 唯一匹配）。**这个动作不放进迁移**：迁移时所有 `own_ipv6` 都还是 NULL，匹配不到任何行；而且把数据启发式塞进 schema 迁移无法评审。

## 4. 传播规则

修改节点 A 的地址后，遍历所有 `remote_node_id = A` 的 `ibgp_peers` 行（它们分布在**其他**节点的分区里）：

| 字段 | 规则 |
|---|---|
| `endpoint` | **只替换主机部分，端口原样保留** |
| `peer_ip` | ← A 的 `own_ipv6` |

三条不变量：

1. **端口永不自动改写。** NAT 端口映射下，`endpoint` 的端口与对端 `listen_port` 本就合法地不一致。在一次无关的地址编辑里顺手"修正"它，会精确地弄坏最难排查的那类部署。
2. **传播只写非空值。** 把 `own_ipv6` 置空不传播、不改任何行——`render_bird_ibgp_peer_conf` 对空 `peer_ip` 直接抛异常，会让对端节点的整个 apply 失败。
3. **只碰 `ibgp_peers`。** `router_id` 不传播（只影响 A 自己的 `bird.conf`）；`local_lla` / `peer_lla` 是隧道链路本地地址，与公网地址无关；`bgp_peers` 是别的运营者的 ASN，始终保持原样。

### 无法推导、留给人工的情况

这些必须出现在 API 响应的 `warnings[]` 里并由 UI 显示，**不能静默跳过**：

- `endpoint` 为空（被动侧从不主动拨号，没有端口可保留）→ 保持为空
- `endpoint` 无法解析 → 原样保留
- `remote_node_id` 为 NULL（未关联）→ 传播看不见这行
- 目标节点 `own_ipv6` 仍为 NULL → 无操作，但要让管理员知道这次编辑什么也没做

### 事务与事件

写入在**一个事务**里完成：`managed_nodes` 的字段 + 每一条被传播的 `ibgp_peers` 行。然后为 A（自身地址块变了）和每个受影响的 B（peer 行变了）各发一条 `desired` 事件（去重）。剩下的推送流程完全复用现有机制。

### 明确延后：互惠端口修复

B→A 的端口其实可以从 A 侧反向行的 `listen_port` 推出。**不做**：它与 NAT 场景正面冲突，猜错就是静默断隧道。将来做成只报告不改动的 `dn42ctl node mesh check`。

## 5. 下发：desired state 的 `node` 块

`DesiredState` 增加一个 `node` 块，**列为 NULL 时整个键省略**：

```json
{
  "node_id": "...", "revision": "...", "generated_at": "...",
  "bgp_peers": [], "ibgp_peers": [], "paths": {},
  "node": {"own_ipv6": "fd42:4242:1234::1", "router_id": "172.20.1.1"}
}
```

"不下发"直接表达在 payload 的形状里，不用魔法值。

**`endpoint_host` 不下发**：节点不会拨自己，apply 对它无事可做。desired state 里每个字段都必须对 spoke 有定义明确的作用；纯 hub 侧的簿记留在 hub 侧。

### 零抖动规则

`compute_content_digest` / `_compute_revision` **只在 block 非空时**才把 `node` 键放进 canonical JSON。

这一条不是优化，是正确性：否则升级的瞬间，全网每一个节点的内容哈希都会变，于是每个节点各收一次无意义的推送、各写一行 `config_revisions`。加了这个条件，没有启用该特性的机群 digest 逐字节不变。

旧的 `config_revisions` 快照里没有这个键，pin 回放路径一律 `payload.get("node", {})`。

## 6. apply 侧行为

> **当且仅当** payload 带**非空** `node` 块时，`node apply` 才会渲染 `config.toml` / `bird.conf` / `dn42-dummy.*`。

没有该块的节点，写入的文件集与 diff 与本特性引入之前**逐字节一致**。这个兼容铰链有专门的回归测试守着。

三个动作，各自按键存在与否单独门控：

1. **重写 `config.toml`** —— 读本地 `AppConfig`，合并下发的字段，**先比较，有差异才写**。常规路径根本不碰文件。
   > `save_config` 用 `tomli_w` 整体重写，**注释和未知键会丢**。这正是"比较优先"的理由。
2. **重渲 `bird.conf`** —— 使用与 peer 文件相同的原子写 + diff 管线，所以 `--dry-run` 看得到。
   > `include` 的 peers 目录与 babel 路径取自**解析后的路径**（desired-state `paths` + `node.toml [apply]` 覆盖），不是 `config.toml` 里的值——peer 文件正是按前者写出去的，用后者会让 bird 去 include 一个空目录。
   > `extra.conf` 的位置同样取自解析后的值。该文件**仅在缺失时**作为占位文件进入写入列表：它的内容属于用户，让它进入常规 diff 管线会让每 900 秒一次的 reconcile 反复覆盖用户配置。见 [`bird_extra_conf.md`](bird_extra_conf.md)。
3. **重写 `dn42-dummy.netdev` / `.network`** —— 同样作为普通文件条目进列表。**不调用 `ensure_dummy_interface`**：它自己 shell out 到 `networkctl`/`nmcli`，会绕过 diff/dry-run 机制。生效交给 reload 步骤。
   > **仅当 `dummy_backend = "networkd"`。** 该接口由 NetworkManager 管理时，写 networkd 的 `.netdev`/`.network` 会造出一份与 NM 冲突的配置；而 apply 刻意不 shell out（`nmcli` 是 `ensure_dummy_interface` 的事）。此时跳过并告警，该节点需要本机跑一次 `dn42ctl genconf` 让新 `own_ipv6` 生效。`config.toml` 与 `bird.conf` 仍照常更新。

**本地 `config.toml` 缺失或损坏 → 记 warning 并跳过全部三步**，不伪造 `AppConfig`。纯 spoke 可能只跑过 `node init` 而从没有过 `config.toml`；而 `bird.conf` 还需要 `own_asn` / `ownnet_v6` / `ownnetset_v6` 这些不在下发范围内的 AS 级字段，缺了就渲染不出来（模板跑在 `StrictUndefined` 下）。

## 7. seeding

| 目标 | 方式 |
|---|---|
| self 节点 | `serve_bootstrap` 在每次 `serve` 启动时，**仅当 DB 列为 NULL** 时用本机 `config.toml` 回填，永不覆盖 |
| 远端节点 | web Nodes 页表单，或 `dn42ctl node set-address <id> [...] [--dry-run]` |

NULL 门控保证不会震荡：首次升级后 hub 采纳本机正在工作的配置，此后 DB 是权威；agent 按 DB 写 `config.toml`，而 bootstrap 只在 DB 为空时读 `config.toml`。

> 让 agent **上报**本地地址供 hub 预填是**延后项**。上报绝不能写权威表（见 `sync_hub_spoke.md` 数据所有权）。将来只能做成表单里的"建议值"，不能自动采纳。

## 8. reload

在此之前 `node apply` 只写入文件、从不 reload——改了地址会写进文件然后静静躺着不生效。

触发条件是本次 apply **实际写入或删除的路径**：

| 条件 | 命令 |
|---|---|
| 有文件落在 `networkd_dir` | `networkctl reload` |
| 有文件落在 `bird_peers_dir`，或等于 `babel.conf` / `bird.conf` | `birdc configure` |

- **顺序固定**：先 `networkctl` 起接口，再 `birdc` 读到引用这些接口的 protocol。
- 用 `birdc configure` 而非 `configure soft`——soft 不能正确加载新增的 protocol。
- **什么都没变就一条都不跑。** 否则 agent 的 900 秒 reconcile 会让每个节点每天无谓地 `birdc configure` 96 次。
- **best-effort，绝不抛异常。** 失败进 `ApplyResult.warnings` 并随 `apply_result` 上报。文件已经正确写入的节点必须报 success-with-warnings 并正常结束，避免对着 `/etc` 崩溃重试。

退出开关（reload 是**节点本地决策**，hub 侧不设开关）：

- `node.toml` 的 `[apply] reload = "auto" | "never"`
- `dn42ctl node apply --no-reload`

> **不违反"禁止自动改路由表"约束**（见 [`../spec.md`](../spec.md)）。两条命令都只是让守护进程重读配置文件，不添加、不删除任何路由；`RouteTable=off` 仍然由 netdev 模板保证。

sandbox 提示：`dn42ctl-node-agent.service` 有 `ProtectSystem=strict`，`/run` 仍可写，`networkctl`（AF_UNIX varlink）与 `birdc`（`/run/bird/bird.ctl`）应该都能用。个别发行版可能需要给 `ReadWritePaths` 加 `/run/bird`。best-effort 的设计正是为了让这种意外退化成一行日志，把影响限制在单个节点。

## 9. 两个 node_id 的分叉隐患

hub 上存在**两个互不校验的 UUID**：

- `/etc/dn42ctl/config.toml` 的 `node_id`，由 `dn42ctl init` 生成，用于给 `bgp_peers` / `ibgp_peers` 分区
- `/var/lib/dn42ctl/self_node_id`，由 `serve_bootstrap` 独立生成，对应 `managed_nodes.is_self`，驱动 desired state

在引入节点作用域之前，admin API 用前者写 peer，而 desired state 用后者读 peer。**两者一旦分叉，管理员在 UI 里加的 peer 永远不会下发，而且没有任何报错。**

修法（不重新指向 `is_self`，那样反而可能孤立既有 peer 行）：

1. **admin 路由的默认作用域解析到 `is_self` 行**，与 desired state 的读取来源统一。这直接消除了静默失败路径。
2. `run_self_registration` 返回 `config_node_id_mismatch`，`serve` 启动时打醒目 warning。
3. `/api/show/all` 暴露该标志，web Overview 页渲染警告横幅。
4. 修复工具 `dn42ctl node adopt-self [--from <uuid>] [--dry-run]`：在一个事务里把 peer 行从失效分区重新挂到 self 节点，并对目标节点发一条 `desired`。目标分区非空时**拒绝执行**——那意味着已经有人在新分区下写过配置，合并策略只能由人来定，硬搬还会撞 `UNIQUE(node_id, ifname)`。
