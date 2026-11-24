# WireGuard 隧道 MTU

每个 eBGP 或启用 WireGuard 的 iBGP peer 可以单独设置本端接口 MTU。取值为 1280 至 65535 字节的整数。新建时省略 MTU 即采用系统默认值，旧数据库升级后也按此处理。

## 单机配置

创建 peer 时可加 `--mtu 1400`。已有 peer 可以单独修改 MTU，命令沿用该 peer 的公钥、地址与其他配置，无需交互输入。

```bash
sudo dn42ctl bgp peer modify 4242421234 --mtu 1380
sudo dn42ctl ibgp peer modify leaf --mtu 1400
sudo networkctl reload
ip link show dev dn42_1234
```

`modify` 将 MTU 保存到 SQLite 并重新生成文件。networkd 从对应 `.network` 的 `[Link]` 段读取 `MTUBytes`。`dn42ctl show wg` 显示保存的设置，`ip link show` 显示接口当前的值。

要移除覆盖值，执行 `sudo dn42ctl bgp peer modify 4242421234 --clear-mtu`，iBGP 的用法相同。这会从配置中删除 `MTUBytes`。已运行的接口可能保留先前的 MTU，直到接口重建。

## 从中心修改

中心与节点同步升级后，在管理界面选择节点，在 BGP 或 iBGP 页编辑 peer 的 **WireGuard MTU**。清空输入框并保存即可移除设置。

管理 API 的 BGP、iBGP peer 创建和修改请求均接受整数 `mtu`。在已有的 `PUT` 请求体中，省略 `mtu` 保留原值，发送 `"mtu": null` 清除设置。中心将设置随期望状态下发。节点应用时，若仅 MTU 改变且启用了自动重载，就会执行 `networkctl reload`。
