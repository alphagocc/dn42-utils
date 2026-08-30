# SELinux 策略模块

`selinux/` 下的策略模块把 `dn42ctl` 的两个常驻服务从 `unconfined_service_t` 移进各自的受限域。策略在 Fedora 的 `targeted` 策略之上以可加载模块的形式存在，不修改发行版自带的任何规则。

## 解决的问题

节点侧 agent 是一个全天候运行的 root 进程，通过 WebSocket 接受中心推送的期望状态。推送的内容是 peer 数据，写入位置由节点自行解析，中心无从指定（见 [`paths.md`](paths.md)）。即便如此，中心失守仍然意味着攻击者能让各节点的 agent 以 root 身份改写 `/etc/bird` 与 `/etc/systemd/network` 下的配置——peer 的接口名、密钥、endpoint 都来自推送。`docs/architecture/deployment.md` 记录了这一影响面。

unit 中的 `ReadWritePaths=` 已经把可写范围限制在五个目录加上可选的 `/etc/bird.conf`。SELinux 在此之上按类型再限制一层：agent 能写的是带 dn42ctl 类型标记的文件，`/etc` 下其余内容既读不到也改不了。两层限制的失效条件互不相同，systemd 的沙盒依赖 unit 文件完整，SELinux 依赖策略模块已加载。

## 文件清单

```
selinux/
├── dn42ctl.te      # 域、类型与规则
├── dn42ctl.fc      # 文件上下文
├── dn42ctl.if      # 对外接口,供其它模块引用
└── Makefile        # 构建、安装、宽容模式切换
```

## 两个域

| 域 | unit | 运行身份 | 可写类型 | 网络 |
| --- | --- | --- | --- | --- |
| `dn42ctl_server_t` | `dn42ctl-server.service` | `dn42ctl` 用户 | `dn42ctl_etc_t`、`dn42ctl_var_lib_t`、`dn42ctl_tmp_t` | 监听 TCP，回环 |
| `dn42ctl_agent_t` | `dn42ctl-node-agent.service` | root | 前者全部，外加 `dn42ctl_bird_conf_t`、`dn42ctl_networkd_conf_t` | 向外发起 TCP 与 DNS |

server 域没有 `/etc/bird` 与 `/etc/systemd/network` 的任何权限，与 `deployment.md` 里“server 不碰系统配置”的划分一致。agent 域没有监听端口的权限。

## 一个可执行文件承载两个域

SELinux 的自动域转换以入口文件类型为键，`/usr/local/bin/dn42ctl` 只能有一条 `type_transition` 规则，两个 unit 无法各自获得自动转换。

自动转换归 `dn42ctl_server_t`，agent 由 unit 里的 `SELinuxContext=` 显式指定。这个方向的选择有意为之：`SELinuxContext=` 一旦遗漏，进程会落进权限较小的域，产生成片的拒绝记录；反过来则是静默获得多余权限，无人察觉。

两个 unit 里的写法都带 `-` 前缀：

```ini
SELinuxContext=-system_u:system_r:dn42ctl_agent_t:s0
```

前缀让 systemd 在上下文无效时忽略该行，因此同一份 unit 文件在没有加载策略模块或没有启用 SELinux 的机器上照常启动。

## 类型与标注

| 类型 | 覆盖位置 | 说明 |
| --- | --- | --- |
| `dn42ctl_exec_t` | `/usr/local/bin/dn42ctl`、`/opt/dn42ctl/dn42ctl/bin/dn42ctl` | 两个域的入口文件 |
| `dn42ctl_etc_t` | `/etc/dn42ctl(/.*)?` | `config.toml`、`node.toml`、`server.env` |
| `dn42ctl_var_lib_t` | `/var/lib/dn42ctl(/.*)?` | 权威库、节点缓存、`self_node_id` |
| `dn42ctl_bird_conf_t` | `/etc/bird(/.*)?`、`/etc/bird.conf` | 渲染出的 Bird 配置与用户维护的 `extra.conf` |
| `dn42ctl_networkd_conf_t` | `/etc/systemd/network/` 下的 `dn42_*`、`wg_*`、`dn42-dummy.*` | netdev 与 network 文件 |
| `dn42ctl_registry_t` | 无默认标注 | DN42 registry 的本地克隆，位置由 `config.toml` 决定 |
| `dn42ctl_tmp_t` | 运行时生成 | `ssh-keygen -Y verify` 与 `gpg --verify` 的临时目录 |

`/etc/systemd/network` 目录本身保持 `etc_t`。管理员手写的 `.network` 文件通常与 dn42ctl 渲染的文件混放在同一目录，改变目录类型会波及 systemd-networkd 对前者的读取。代价是 agent 需要 `etc_t` 目录的写权限，因而能在 `/etc` 下任意位置新建 `dn42ctl_networkd_conf_t` 文件——它读不到也改不了已有的 `etc_t` 文件，能造成的后果止于产生一个别的域都读不出内容的孤立文件。

`/etc/bird` 整个目录换成 `dn42ctl_bird_conf_t`。bird 在 Fedora 尚无独立策略模块，以 `unconfined_service_t` 运行，读取任何类型都不受限。bird 将来若获得独立域，该域需要调用 `dn42ctl.if` 里的 `dn42ctl_read_bird_conf()`。

`/etc/bird.conf` 单列一条。上一条的正则整体锚定在完整位置上，匹配不到 `/etc` 下的这个文件，而发行版编译内置的 bird 主配置就在那里。它的父目录是 `etc_t`，因此策略中另有一条 `manage_files_pattern(dn42ctl_agent_t, etc_t, dn42ctl_bird_conf_t)`；没有配套的 file transition，该文件由 `genconf` 在无沙盒环境下创建，agent 只覆写已有文件。采用该布局的完整前置条件见 [`paths.md`](paths.md)。

registry 目录没有默认标注，部署时按实际位置补：

```bash
sudo semanage fcontext -a -t dn42ctl_registry_t '/srv/dn42-registry(/.*)?'
sudo restorecon -Rv /srv/dn42-registry
```

## 构建

```bash
sudo dnf install selinux-policy-devel
cd selinux && make
```

产物是 `dn42ctl.pp`。构建不需要 root。

## 安装

```bash
cd selinux
sudo make install
```

`install` 目标执行 `semodule -i dn42ctl.pp`，随后对以下位置执行 `restorecon -iRvF`：`/usr/local/bin/dn42ctl`、`/opt/dn42ctl`、`/etc/dn42ctl`、`/var/lib/dn42ctl`、`/etc/bird`、`/etc/bird.conf`、`/etc/systemd/network`。已经存在的文件需要这一步才会带上新类型。`-i` 让 `/etc/bird.conf` 这类只在部分布局下存在的位置在缺失时被跳过。

加载模块后重启两个服务，让它们在新域中启动：

```bash
sudo systemctl restart dn42ctl-server dn42ctl-node-agent
ps -eZ | grep dn42ctl        # 应显示 dn42ctl_server_t 与 dn42ctl_agent_t
```

`dn42ctl deploy daemon` 本身会对新装的可执行文件调用 `restorecon`，升级二进制无需额外操作。

## 首次上线

任何新策略模块都应当先在宽容模式下跑满一个完整的业务周期，再切回强制。dn42ctl 的完整周期包括一次 `node apply` 写入全部渲染目标、一次 `networkctl reload` 与 `birdc configure`、一次 auto-peer 的签名校验。

```bash
cd selinux
sudo make install
sudo make permissive            # 只对这两个域宽容,系统其余部分保持 Enforcing

# 跑完整业务周期,随后查看被记录下来的拒绝
sudo ausearch -m AVC -ts recent | grep -E 'dn42ctl_(server|agent)_t'
```

有拒绝记录时，先判断该操作是否本就应当发生，确认属于正当需求后再生成补充规则：

```bash
sudo ausearch -m AVC -ts recent | audit2allow -m dn42ctl_local > dn42ctl_local.te
```

把 `dn42ctl_local.te` 中的规则并入 `dn42ctl.te`，重新构建安装，而非长期挂着一个独立的 local 模块——规则分散在两个文件里，后续审阅会漏看。确认没有新的拒绝之后切回强制：

```bash
sudo make enforcing
```

## 卸载

```bash
cd selinux && sudo make uninstall
```

`uninstall` 移除模块并把上述位置重新标注回发行版默认类型。

## 尚未验证的部分

策略在 Fedora 44、`selinux-policy` 44.7 上编译通过，文件上下文的正则由 `sefcontext_compile` 校验通过。以下内容需要在实际节点上确认：

模块加载本身尚未执行。`.te` 中三个 `optional_policy` 块分别依赖 `systemd_networkd_t`、`unconfined_service_t`、`NetworkManager_etc_rw_t`，这三个类型在本机加载的策略中都存在，但 optional 块在加载时若依赖不满足会被静默丢弃，加载后需要确认相关操作没有被拒绝。

`birdc configure` 与 `networkctl reload` 连接的是两个守护进程的控制套接字。前者位于 `/run/bird/bird.ctl`，类型 `var_run_t`，对端域取决于 bird 以何种身份运行；后者位于 `/run/systemd/netif`，类型 `systemd_networkd_var_run_t`。这两条路只有在实际执行一次 reload 时才能确认规则完整。

CLI 仍以 `unconfined_t` 运行。策略只在 `initrc_domain` 上建立自动转换，管理员用 `sudo dn42ctl ...` 执行的写命令不受本模块限制。在受限管理员角色（`staff_t`、`sysadm_t`）下运行 CLI 未经测试。

TCP 端口沿用 `unreserved_port_t` 与 `http_port_t`，没有为 4242 定义专用端口类型。server 的监听面已由 unit 中的 `IPAddressAllow=localhost` 限制在回环。需要更严的限制时可以定义 `dn42ctl_port_t` 并用 `semanage port` 绑定，代价是多一个部署步骤，端口变更时容易遗漏。
