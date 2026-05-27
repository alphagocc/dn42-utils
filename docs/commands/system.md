# dn42ctl system

系统组件安装/卸载命令。

## 用法

```bash
dn42ctl system install <component>
dn42ctl system uninstall <component>
```

## 可用组件

### firewalld-conf

修改 `/etc/firewalld/firewalld.conf` 中的 `IPv6_rpfilter` 设置。

- **install**: 设置 `IPv6_rpfilter=no`，然后 `systemctl restart firewalld`。DN42 的非对称路由需要关闭 IPv6 反向路径过滤。
- **uninstall**: 恢复 `IPv6_rpfilter=yes`，然后 `systemctl restart firewalld`。

不需要 `dn42ctl init`。

### nftables-conf

安装 nftables 规则，禁用 DN42/WireGuard 接口上的连接跟踪（notrack）。

- **install**:
  1. 将规则写入 `/etc/nftables/dn42-no-conntrack.nft`
  2. 在 nftables.conf（自动检测 `/etc/sysconfig/nftables.conf` 或 `/etc/nftables.conf`）中添加 `include` 行
  3. `systemctl enable nftables`
  4. `nft -f` 立即加载规则
- **uninstall**:
  1. `nft delete table inet dn42_notrack` 移除运行时规则
  2. 删除 `/etc/nftables/dn42-no-conntrack.nft`
  3. 从 nftables.conf 中移除 include 行

不需要 `dn42ctl init`。

### roa-service

安装 ROA v6 定时更新的 systemd timer + service。

- **install**: 写入 `dn42-roa-v6.service` 和 `dn42-roa-v6.timer` 到 `/etc/systemd/system/`，`daemon-reload`，`enable --now` timer，立即触发一次下载。
- **uninstall**: 停止并禁用 timer/service，删除 unit 文件，`daemon-reload`。

需要 `dn42ctl init`（读取 `bird_roa_v6_conf_path`）。
