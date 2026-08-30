# Bird 自定义配置（extra.conf）

`bird.conf` 由模板整体渲染，每次 `genconf` 都会重写，因此用户手工加进去的内容会在下一次生成时消失。DN42 的实际运维中总有一些配置无法用数据库中的 peer 记录表达：静态路由、额外的 `protocol`、面向特定邻居的 filter、临时的调试用 `protocol pipe`。`extra.conf` 是为这类内容保留的插入位置。

职责划分只有一句：dn42ctl 负责保证 `bird.conf` 里的 `include` 存在、文件本身存在；文件内容完全属于用户，工具永不读取、永不解析、永不改写。

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

旧版本写出的 `config.toml` 中没有这个键。读取配置时缺键按 `bird_peers_dir` 的上一级目录推导为其中的 `extra.conf`，因此升级 dn42ctl 之后无需重跑 `init`。

锚点选择 peers 目录而非 `bird_conf`，是因为发行版编译内置的 bird 主配置位置在 `/etc/bird.conf`，按它的同级目录会推出 `/etc/extra.conf`——既不是用户期望的位置，也不在 agent 沙盒的可写范围内。锚点也没有固定成 `/etc/bird`，那样把配置指向可写目录的开发与测试环境就不自洽了。peers 目录同时满足这两点。主配置位于 `/etc/bird.conf` 的部署见 [`paths.md`](paths.md)。

## 生成规则

`genconf` 在渲染 `bird.conf` 之后检查目标文件：缺失时写入一份仅含说明注释的占位文件，已存在时原样保留。

占位文件的意义在于让 `include` 始终指向一个真实存在的文件。BIRD 对无匹配 `include` 的容忍程度取决于版本与实现细节，让 `bird.conf` 的可用性依赖这一行为并不可取；先写出空文件则任何版本都能启动。

该文件不参与 `bird.conf` 与 `babel.conf` 的覆盖确认提问。那两个文件由工具完整拥有，重跑 `genconf` 时提示用户确认覆盖是必要的；`extra.conf` 的内容属于用户，永不覆盖是它的固有语义，多问一次没有意义。

## 与多节点同步的关系

写入位置属于节点本地信息，中心不下发。`extra.conf` 的位置由本机 `config.toml` 的 `[paths].bird_extra_conf` 决定，与 `genconf` 读的是同一处，解析规则见 [`paths.md`](paths.md)。文件内容同样始终是节点本地的，既不上报也不推送。

`node apply` 在收到非空 `node` 块时会重新渲染 `bird.conf`（语义见 [`node_addressing.md`](node_addressing.md)），其中的 `include` 使用解析后的位置，与 `peers_dir`、`babel_conf_path` 的处理方式一致。占位文件同样由 apply 负责创建，但**仅在文件缺失时才进入写入列表**：占位内容一旦进入常规的 diff 与原子写管线，agent 每 900 秒一次的 reconcile 就会把用户写的配置抹掉。

## 未纳入范围

`scan` 不探测 `extra.conf`。该命令的用途是从既有的、非 dn42ctl 生成的 `bird.conf` 中识别 peers 目录与 babel/ROA 位置，而 `extra.conf` 是本工具引入的概念，外来配置中不会出现。

工具不校验 `extra.conf` 的语法。写错的内容会在 `birdc configure` 时由 BIRD 自己报错，此时整份配置加载失败，包括 dn42ctl 生成的部分。这是用户接管该文件所换取的代价。

## 相关文档

- 默认位置与提权：[`paths.md`](paths.md)
- 生成流程：[`../commands/genconf.md`](../commands/genconf.md)
- 节点同步：[`sync_hub_spoke.md`](sync_hub_spoke.md)
