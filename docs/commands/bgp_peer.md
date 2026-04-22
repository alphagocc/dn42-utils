# 命令：bgp peer

## `dn42ctl bgp peer`（等价于 `dn42ctl bgp peer add`）

用途：创建一个对外 BGP peer（wireguard 接口 + bird peers + 网络后端配置）。

### 输入

- 必填：`--asn`、`--pubkey`、`--endpoint`、`--peer-lla`、`--net`（`networkd` 或 `nm`）。
- 可选：`--listen-port`
  - `0` 表示不设置（让系统自行选择端口，适用于仅出站/防火墙后场景）
  - 留空则按规则推导
- 缺失时会提示用户输入。

> 交互模式下：如果 `--pubkey/--endpoint/--peer-lla` 缺失，CLI 会先生成并输出本端 WG 公钥与本端 LLA，便于先发给对端；随后再提示输入对端信息。

### 派生规则

- `ifname`：`dn42_<ASN后4位>`
- `ListenPort`：默认 `ASN后5位`（超出范围则报错）；也允许通过 `--listen-port` 覆盖

### WireGuard

- 本端密钥：必须调用系统命令生成：`wg genkey` 与 `wg pubkey`。
- 本端 LLA：随机生成 `fe80::xxxx:xxxx/64`。
- AllowedIPs：默认写入 `fe80::/64` 与 `fd00::/8`，同时必须满足“禁止修改路由表”约束（见网络后端文档）。

### 输出

- Bird peer conf：写入到 `bird_peers_dir/<ifname>.conf`。
- networkd：写入 `<ifname>.netdev` 与 `<ifname>.network`。
- NetworkManager：写入 `<ifname>.nmconnection`（文件权限目标为 0600）。
- CLI 会展示必要信息（本端公钥、本端 LLA、ListenPort、写入的文件路径）。

## `dn42ctl bgp peer modify`

用途：当 peer 信息无法一次性填写完整时，读取数据库中已有记录并根据新输入重新生成配置文件。

行为：

- 读取数据库中该 peer 的现有记录，提示用户输入缺失或需要更新的字段。
- 更新数据库记录。
- 重新渲染并覆盖生成 Bird peer conf 与对应网络后端配置文件。

---

## `dn42ctl bgp peer del`

用途：删除指定的外部 BGP peer。

### 输入

- 必填：`<ASN>`

### 行为

- 删除前必须二次确认（交互 prompt）。
- 删除数据库记录。
- 删除生成文件（Bird peer conf + networkd/NM 文件）。
