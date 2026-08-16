# Babel 配置生成（rxcost / interface type）

`babel.conf` 由 dn42ctl **整体重生成**（确定性、幂等），不做增量编辑。每个有 WireGuard 隧道的 iBGP peer 对应一个 `interface` 段，其参数按 **iBGP peer 粒度**存储在 SQLite。

## rxcost

- 存储位置：`ibgp_peers.babel_rxcost`。
- 创建带 WG 隧道的 iBGP peer 时**必须**提供 `rxcost`，可通过命令行参数给出，也可由交互提示输入。
- 修改 iBGP peer 的 `rxcost` 后应重生成 `babel.conf`（幂等）。

## interface type

- 存储位置：`ibgp_peers.babel_type`。
- 取值范围：`wired`、`wireless`、`tunnel`；默认值为 `tunnel`。
- 创建时通过 `--type` 指定，修改时同样通过 `--type`。
- 高丢包链路建议使用 `wireless`。

## scan 导入

`scan` 会从现有 `babel.conf` 中尽力解析各接口的 `rxcost` 与 `type` 并导入 SQLite：

- 解析失败会给出 warning 并回退到默认值（`type` 回退为 `tunnel`），保持原有行为。
- 解析结果与 peer 通过接口名关联。

## 无 WG 的 iBGP peer

`--no-wg` 创建的 iBGP peer 不写网络配置文件，也**不会**出现在 `babel.conf` 的 interface 列表中。

## 相关文档

- 命令参数与校验：[`../commands/ibgp_peer.md`](../commands/ibgp_peer.md)
- 重生成时机：[`../commands/genconf.md`](../commands/genconf.md)
- 导入行为：[`../commands/scan.md`](../commands/scan.md)
- 表结构：[`database.md`](database.md)
