# DN42CTL 重构与优化实施计划 (Refactoring & Optimization Plan)

这份计划旨在将 `dn42ctl` 项目现有的紧耦合、大文件结构转化为模块化、高可维护的架构。它将作为 AI 或开发者一步步执行的指导规范。

## 目标概述
1. **文档与规范重组**: 拆分臃肿的 `docs/spec.md`，建立清晰的层级文档结构。
2. **模板引擎升级**: 引入 `Jinja2` 彻底替换 `dn42ctl.render` 里的 `re.sub` 和硬拼接逻辑。
3. **模块化拆分**: 将超过 1700 行的 `services.py` 按业务领域（BGP、iBGP、Scan、Show 等）拆分为多个子模块。
4. **Scan 解析强化**: 提升 `scan_local_configs` 解析现有系统配置（如 networkd）的容错性与鲁棒性，同时彻底移除对过时的 wg-quick 探测支持。

---

## 阶段一：文档与规范重组 (Documentation Reorganization)
**背景**: `docs/spec.md` 包含了系统约定、文件路径、所有的命令逻辑和数据库 Schema，内容过于庞大，不利于后续开发查阅。

**执行步骤**:
1. **建立目录结构**:
   在 `docs/` 下创建子目录 `docs/commands/` 和 `docs/architecture/`。
2. **抽离架构与设计**:
   创建 `docs/architecture/database.md` (存放 SQLite Schema 定义)、`docs/architecture/network_backends.md` (存放 networkd 与 NM 的设计约束，例如强制关闭路由的约定)。
3. **抽离命令规范**:
   将具体命令的行为定义拆分到 `docs/commands/init.md`, `docs/commands/genconf.md`, `docs/commands/bgp_peer.md`, `docs/commands/ibgp_peer.md`, `docs/commands/scan.md`, `docs/commands/show_and_del.md` 中。
4. **精简主入口**:
   修改 `docs/spec.md` 作为总索引文件（Index），仅保留项目核心目标、运行环境依赖，并添加指向上述拆分后文档的引用。

---

## 阶段二：引入 Jinja2 与重构 Render 层 (Template Engine Upgrade)
**背景**: `render.py` 使用了 `re.sub` 来匹配配置模板中的 `define OWNAS = ...`，而系统网络配置采用硬拼接实现。这对于复杂逻辑维护成本极高。

**执行步骤**:
1. **依赖更新**:
   在 `pyproject.toml` 中的 `dependencies` 加入 `jinja2>=3.0.0`。
2. **改写模板文件**:
   - 将 `src/dn42ctl/templates/` 下的模板后缀改为 `.j2`（如 `bird.conf.j2`, `babel.conf.j2`, `ibgp_peer.conf.j2`）。
   - 将原本 Python 中的网络配置字符串硬编码，抽取为独立的模板文件：`networkd_netdev.j2`, `networkd_network.j2`, `nmconnection.j2`。
   - 使用 Jinja2 的 `{{ variable }}` 语法替代先前的字符串格式化，使用 `{% if %}` 处理可选参数（例如仅当 `listen_port > 0` 时才渲染对应行）。
3. **重构 `src/dn42ctl/render.py`**:
   - 移除所有的正则表达式 (`re` 模块) 和硬拼接逻辑。
   - 初始化 `jinja2.Environment(loader=jinja2.PackageLoader("dn42ctl", "templates"))`。
   - 将 `render_bird_main_conf`, `render_networkd_netdev` 等函数改造为组装变量后调用 `template.render(**kwargs)` 的形式。
   - **严格要求**: 必须保证原有架构中对 `RouteTable=off` 和 `peer-routes=false` 的强制约束不变，避免路由表被自动修改。

---

## 阶段三：拆分 `services.py` 模块 (Services Layer Decomposition)
**背景**: 核心逻辑全在单一文件，可读性和可维护性在未来继续添加功能（例如接入 REST API）时会面临严峻挑战。

**执行步骤**:
1. **建立 Package**:
   在 `src/dn42ctl/` 下新建 `services/` 目录，并添加 `__init__.py` 暴露所有的子服务函数（从而不破坏现有的 `cli.py` 导入结构）。
2. **抽象基础实体与公共方法 (`src/dn42ctl/services/core.py`)**:
   - 迁移所有的 Dataclass (`PeerResult`, `GenConfResult`, `BgpPeerView` 等)。
   - 迁移公共辅助函数 (`_write_text`, `_ensure_dir`, `normalize_net_backend`, `sanitize_name`, `_pick_unused_port` 等)。
3. **拆分 Init 与 Genconf (`src/dn42ctl/services/init_sys.py`)**:
   - 迁移 `init_node`、`genconf` 以及 ROA v6 的 systemd timer 安装逻辑。
4. **拆分 BGP/iBGP 逻辑**:
   - 创建 `src/dn42ctl/services/bgp.py`：包含 `create_bgp_peer`, `modify_bgp_peer`, `delete_bgp_peer`。
   - 创建 `src/dn42ctl/services/ibgp.py`：包含 `create_ibgp_peer`, `delete_ibgp_peer`。
5. **拆分 Show 命令与并发探测 (`src/dn42ctl/services/show.py`)**:
   - 迁移 `show_bgp_peers`, `show_ibgp_peers`, `show_wg_tunnels`。
   - **优化点**: 引入 `concurrent.futures.ThreadPoolExecutor`。现存逻辑中对每个节点执行 `wg show` 和 `birdc show protocols` 为线性同步调用，若有几十个 peers 会非常耗时。请将其改造为并发执行。
6. **拆分 Scan 逻辑 (`src/dn42ctl/services/scan.py`)**:
   - 迁移 `discover_bird_paths` 和 `scan_local_configs`（将在下一阶段强化解析能力）。

---

## 阶段四：强化 Scan 配置解析的鲁棒性 (Scan Robustness Enhancement)
**背景**: 当前 `scan` 功能通过简单的 `.split("=", 1)` 解析旧配置，这在遇到复杂的空白符缩进、内联注释或乱序配置时非常容易抛出异常，阻断扫描流程。

**执行步骤**:
1. **强化 `networkd` 解析**:
   - `_parse_networkd_netdev` 与 `_parse_networkd_network`: 改写为基于更智能的正则表达式或者轻量级类 INI 解析器。
   - 必须能忽略行内注释（如 `ListenPort=51820 # This is a port`）、正确剔除空白符。
   - 针对缺失或无效的端口，提供稳健的 Fallback（即 `0`，表示由系统分配）。
2. **彻底移除 `wg-quick` 支持**:
   - 删除 `_parse_wgquick_conf` 相关逻辑，移除针对 `/etc/wireguard` 目录的扫描代码。
   - 明确 `dn42ctl` 仅聚焦支持 `systemd-networkd` 与 `NetworkManager` 这两大现代主流网络后端。
3. **细粒度的错误恢复机制 (Error Recovery)**:
   - 确保当扫描数百个文件时，如果某单个文件（如 `.netdev`）格式错误导致异常，仅将该错误记录到 `skipped` 列表和 `warnings` 列表中，而**不应当抛出 `Dn42CtlError` 中断整体流程**。
   - 在 CLI 层清晰输出被跳过的文件及其具体的解析错误堆栈的摘要信息。

---

## 验收标准 (Acceptance Criteria)
1. 运行 `uv run dn42ctl --help` 正常。
2. 在任意现存 SQLite 环境下运行 `dn42ctl genconf` 和 `dn42ctl show all` 不出现报错，且配置的输出路径和内容应当与重构前无差别。
3. `services.py` 不复存在，相关逻辑均在 `src/dn42ctl/services/` 各个子模块下。
4. `pyproject.toml` 包含 `jinja2`。
5. 在包含复杂缩进和注释的旧网络配置文件目录下运行 `dn42ctl scan` 能成功收集信息并打印跳过理由，不发生崩溃。
