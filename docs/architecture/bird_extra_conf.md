# Bird 自定义配置（extra.conf）

`bird.conf` 由模板整体渲染，每次 `genconf` 都会重写，因此用户手工加进去的内容会在下一次生成时消失。DN42 的实际运维中总有一些配置无法用数据库中的 peer 记录表达：静态路由、额外的 `protocol`、面向特定邻居的 filter、临时的调试用 `protocol pipe`。`extra.conf` 是为这类内容保留的插入位置。

文件内容由运维维护。配置生成负责写入 `include` 并创建缺失的文件，已有内容会保留。

## 从 Web 管理后台编辑

同步升级 Web、hub 和节点 agent 后，登录管理后台，选择目标节点，打开 **BIRD extra.conf**。页面读取节点上已有的 UTF-8 文本。点击 **Save file** 后，agent 将内容保存到节点本机配置指定的位置。清空编辑框并保存会留下空文件。文件大小上限为 1 MiB。

保存时会比对节点文件的当前版本。版本与读取时不同，页面会保留草稿并显示节点上的最新内容，便于核对后再保存。节点离线时可等它重连后重试。保存超时或断线时，请重新读取文件核实结果。

保存只更新文件。要使 BIRD 加载变更，在目标节点检查主配置，成功后重载，命令见下节。

## 追加并加载一段配置

首次运行 `dn42ctl genconf` 会创建 `extra.conf`。在目标节点执行 `sudoedit /etc/bird/extra.conf`，例如追加一个使用独立 IPv6 路由表的静态协议。这里的 `OWNNETv6` 由主配置定义，示例路由只进入 BIRD 的 `example_v6` 表。

```bird
ipv6 table example_v6;

protocol static example_static {
    ipv6 { table example_v6; };
    route OWNNETv6 reject;
}
```

保存后检查整份配置，再让运行中的 BIRD 加载变更。

```bash
sudo bird -p -c /etc/bird.conf && sudo birdc configure
sudo birdc show route table example_v6
```

以上命令使用默认路径。若已自定义路径，编辑本机 `config.toml` 的 `[paths].bird_extra_conf` 指定的文件，将 `-c` 后的路径替换为服务正在使用的主配置（`[paths].bird_conf`）。以后修改段落，只需保存、检查和重载。

## include 的位置

`include` 位于 `bird.conf` 末尾，在 `babel.conf` 与 `peers/*` 之后：

```
include "/etc/bird/babel.conf";
include "/etc/bird/peers/*";
include "/etc/bird/extra.conf";
```

这个位置决定了 `extra.conf` 中可以写什么。BIRD 要求符号先定义后引用，因此模板头部的 `define OWNAS` / `OWNIPv6` / `OWNNETv6` / `OWNNETSETv6`、工具函数 `is_self_net_v6()` / `is_valid_network_v6()`、以及 `dnpeers` 与 `ibgp_template` 两个 BGP template，在 `extra.conf` 中均可引用。反过来，`extra.conf` 中定义的 function 无法被内置模板使用，因为模板中的 filter 出现得更早。需要修改内置过滤逻辑的场合，应当修改 [`../../src/dn42ctl/templates/bird.conf.j2`](../../src/dn42ctl/templates/bird.conf.j2) 本身。

`protocol` 的定义在 BIRD 中没有先后要求，所以追加协议实例不受这个位置约束。

## 文件位置

| 项目 | 值 |
| --- | --- |
| 默认位置 | `/etc/bird/extra.conf` |
| 配置键 | `config.toml` 的 `[paths].bird_extra_conf` |
| init 参数 | `dn42ctl init --bird-extra-conf` |
| 解析来源 | 本机 `config.toml` |

旧版本写出的 `config.toml` 中没有这个键。读取配置时缺键按 `bird_peers_dir` 的上一级目录推导为其中的 `extra.conf`，因此升级 dn42ctl 之后无需重新执行 `init`。

锚点选择 peers 目录而非 `bird_conf`，是因为发行版编译内置的 bird 主配置位置在 `/etc/bird.conf`，按它的同级目录会推出 `/etc/extra.conf`，该位置既不是用户期望的位置，也不在 agent 沙盒的可写范围内。锚点也没有固定成 `/etc/bird`，那样把配置指向可写目录的开发与测试环境就不自洽了。peers 目录同时满足这两点。主配置位于 `/etc/bird.conf` 的部署见 [`paths.md`](paths.md)。

## 生成规则

`genconf` 在渲染 `bird.conf` 之后检查目标文件：缺失时写入一份仅含说明注释的占位文件，已存在时原样保留。

占位文件的意义在于让 `include` 始终指向一个真实存在的文件。BIRD 对无匹配 `include` 的容忍程度取决于版本与实现细节，让 `bird.conf` 的可用性依赖这一行为并不可取；先写出空文件则任何版本都能启动。

该文件不参与 `bird.conf` 与 `babel.conf` 的覆盖确认提问。那两个文件由工具完整拥有，重新执行 `genconf` 时提示用户确认覆盖是必要的；`extra.conf` 的内容属于用户，永不覆盖是它的固有语义，多问一次没有意义。

## 权限归工具

内容与权限的归属不同。内容属于用户，权限由 bird 的读取需求决定：bird 以 `-u bird -g bird` 降权运行，打不开 `include` 指向的文件时整份配置加载失败，`bird -p` 因此在开机校验阶段就拒绝启动。

`genconf` 与 `node apply` 每次都把该文件校正为 `0644`（`dn42ctl/file_policy.py` 的 `BIRD`），文件内容一个字节都不读也不写。仅在创建时套一次权限不够：`0.1` 早期版本按 `0600` 创建过它，那些文件不会因为后来改对了默认值而自行变好。`--dry-run` 不改权限。

需要在 bird 配置里放置秘密时，`0600 root` 并不是可行的做法，bird 读不到它。

## 与多节点同步的关系

文件位于节点本机 `config.toml` 的 `[paths].bird_extra_conf`。仅执行过 `node init` 且没有该配置文件的节点使用默认位置。解析规则见[本机路径](paths.md)。

Web 读写通过目标节点的在线 agent 完成，hub 自身节点也使用这个通道。中心转发管理员的单次请求，文件内容不进入 SQLite 的期望状态、报告和历史快照。回滚 peer 配置时会保留当前的 `extra.conf`。

`node apply` 重新渲染 `bird.conf` 时会确保被包含的文件存在。如果 Web 编辑器或命令行已经创建了文件，生成器会保留其内容。节点地址的处理见[节点地址配置](node_addressing.md)。

## 未纳入范围

`scan` 不探测 `extra.conf`。该命令的用途是从既有的、非 dn42ctl 生成的 `bird.conf` 中识别 peers 目录与 babel/ROA 位置，而 `extra.conf` 是本工具引入的概念，外来配置中不会出现。

工具不校验 `extra.conf` 的语法。写错的内容会在 `birdc configure` 时由 BIRD 自己报错，此时整份配置加载失败，包括 dn42ctl 生成的部分。这是用户接管该文件所换取的代价。

## 相关文档

- 默认位置与提权：[`paths.md`](paths.md)
- 生成流程：[`../commands/genconf.md`](../commands/genconf.md)
- 节点同步：[`sync_hub_spoke.md`](sync_hub_spoke.md)
