# Web UI（admin + peer，React + Vite）

dn42ctl 不内置任何 HTML 渲染；UI 是一个 Vite 多页应用，构建后由 **nginx** 托管，**不**由 FastAPI 提供。FastAPI 始终只回 JSON。

## 设计目标

- **最少依赖**：仅 React + ReactDOM（运行时），Vite + Tailwind CSS + TypeScript（构建时）。不使用 UI 库、状态管理库、路由库、fetch 封装库。
- **供应链安全**：pnpm `minimumReleaseAge: 1440`（24 小时隔离期）；`onlyBuiltDependencies` 白名单限制 postinstall 脚本。
- **现代化 + 黑白配色**：仅使用 `zinc/neutral` 灰阶 + 纯黑/纯白，强调留白与排版。
- **亮色 / 暗色**：基于 Tailwind `dark:` 变体，由 `<html class="dark">` 切换，状态写 `localStorage.theme`，默认跟随 `prefers-color-scheme`。
- **可独立分发**：`web/` 不依赖 dn42ctl 的任何 Python 模块，可单独打包。

## 目录布局

```
web/
├── package.json
├── pnpm-workspace.yaml          # minimumReleaseAge 配置
├── pnpm-lock.yaml
├── vite.config.ts               # 多页入口 (admin + peer)
├── tsconfig.json
├── admin/
│   └── index.html               # Vite 入口 → src/admin/main.tsx
├── peer/
│   └── index.html               # Vite 入口 → src/peer/main.tsx
└── src/
    ├── shared/
    │   ├── api.ts               # fetch 封装 (Bearer token, 401 处理)
    │   ├── theme.ts             # 主题切换逻辑
    │   ├── index.css            # Tailwind 指令 + 字体
    │   └── components/
    │       ├── Table.tsx        # 通用数据表格
    │       ├── Modal.tsx        # 弹窗 (表单 + 确认)
    │       ├── Toast.tsx        # 通知 (React Context)
    │       ├── NodeSelector.tsx # 节点下拉 (共享)
    │       └── ThemeToggle.tsx  # 主题切换按钮
    ├── admin/
    │   ├── main.tsx             # React 根
    │   ├── App.tsx              # 登录/仪表盘条件渲染
    │   ├── Login.tsx            # token 登录表单
    │   ├── NodeContext.tsx      # 当前选中节点 (React Context)
    │   ├── Dashboard.tsx        # 顶栏 + 侧栏 Tab 容器
    │   └── tabs/
    │       ├── Overview.tsx
    │       ├── Bgp.tsx          # CRUD
    │       ├── Ibgp.tsx         # CRUD
    │       ├── Wg.tsx           # 只读
    │       ├── Nodes.tsx        # CRUD + rotate token + 地址与 auto-peer 开关编辑
    │       ├── Proposals.tsx    # accept/reject
    │       ├── Reports.tsx      # import
    │       ├── Revisions.tsx    # pin/unpin
    │       ├── Database.tsx     # 只读表浏览
    │       └── Genconf.tsx      # 触发按钮
    └── peer/
        ├── main.tsx
        ├── App.tsx              # 步骤状态机 + 步骤指示器
        └── steps/
            ├── StepAuth.tsx     # 跳转到 Kioubit 认证服务
            ├── StepSubmit.tsx   # 选节点 + WireGuard 表单
            └── Success.tsx
```

## 主题策略

HTML `<head>` 内尽早执行的内联脚本（防 FOUC）：

```html
<script>
  const saved = localStorage.getItem("theme");
  const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  if (saved === "dark" || (!saved && prefersDark)) document.documentElement.classList.add("dark");
</script>
```

每个页面顶部放一个 `<ThemeToggle />` 组件，写入 `localStorage.theme = "dark" | "light"` 并 toggle `.dark`。

调色板（来自 Tailwind 默认）：

| 用途 | light | dark |
|------|-------|------|
| 页面背景 | `bg-white` | `dark:bg-black` |
| 主要文字 | `text-zinc-900` | `dark:text-zinc-100` |
| 次要文字 | `text-zinc-500` | `dark:text-zinc-400` |
| 边框 | `border-zinc-200` | `dark:border-zinc-800` |
| 卡片背景 | `bg-zinc-50` | `dark:bg-zinc-900` |
| 强调按钮 | `bg-black text-white` | `dark:bg-white dark:text-black` |
| 危险操作 | `text-red-600 dark:text-red-400` |

不使用其他色相 (蓝/绿/紫等)，保持黑白配色一致性。

## 按钮的按下反馈

按下反馈写在 `shared/index.css` 的 base layer 内，以元素选择器 `button` 生效，admin 与 peer 两个入口的全部按钮因此获得一致行为，新增按钮无需重复书写 class。

三条规则：指针悬停时 `cursor: pointer`；按下时 `transform: scale(0.97)` 叠加 `opacity: 0.85`，过渡时长 120ms；`disabled` 状态降为半透明并取消上述反馈。

`prefers-reduced-motion: reduce` 下取消缩放与过渡，仅保留透明度变化，反馈对偏好减少动效的用户依然可见。

## 数字输入框

`type="number"` 的 spinner 箭头会让方向键与滚轮意外改写数值，ASN、端口这类字段并没有逐一增减的语义。全部数字字段改用 `type="text"` 配合 `inputMode="numeric"` 与 `pattern="[0-9]*"`：移动端仍然弹出数字键盘，浏览器仍按 pattern 拦截含非数字字符的提交，而 spinner 箭头、方向键增减与滚轮改值一并消失。

覆盖 peer 页的 ASN 与 listen_port，以及 `FormModal` 中声明为 `type: "number"` 的字段（peer_asn、listen_port、babel_rxcost）。`FieldDef` 的类型名保持 `"number"`，转换发生在 `Modal.tsx` 内部，调用方无需改动。

上下界校验交给服务端，与本文件"已知限制"中记录的表单校验策略一致。

## admin: 鉴权与状态

- 无路由库：`App.tsx` 根据 `sessionStorage.dn42ctl_admin_token` 条件渲染 `<Login />` 或 `<Dashboard />`。
- 所有 fetch 通过 `shared/api.ts` 的 `api()` 封装，自动加 `Authorization: Bearer ${token}`；401 → 清空 token 并触发重新渲染到登录页。
- "Sign out"按钮：`sessionStorage.removeItem` + 状态更新。
- **不存到 `localStorage`**：保持 token 仅活在当前 tab。

## admin: 侧栏 Tab 视图

十个 tab 竖排在左侧导航栏内，顶栏只保留标题与全局操作控件（节点选择器、Refresh、Theme、Sign out）。改为侧栏之前，十个 tab 与四个控件同处一行，窗口稍窄时 tab 就会折行，并把 Sign out 的文字挤成两行。

布局要点：

顶栏内容区高度固定为 `h-14`，右侧控件带 `shrink-0` 与 `whitespace-nowrap`，窗口收窄时按钮文字保持单行。

侧栏在 `lg` 及以上宽度常显，宽度 `w-52`，内部 `<nav>` 使用 `sticky top-14` 跟随页面滚动。

`lg` 以下宽度侧栏隐藏，顶栏左侧出现 `Menu` 按钮，点击后以抽屉形式覆盖显示：半透明遮罩加左侧面板；点击遮罩、按 Escape、或选中任一 tab 都会关闭抽屉。

侧栏与抽屉共用同一份 `TabNav`，选中项沿用黑底白字（暗色下反转），未选中项使用 `hover:bg-zinc-100 dark:hover:bg-zinc-900`。

| Tab | 数据来源 (admin API) | 操作 |
|-----|---------------------|------|
| Overview | `GET /api/show/all?live=false` | 只读卡片：node_id + 三类 peer 数量；node_id 分叉时显示警告横幅 |
| BGP peers | `GET /api/admin/bgp/peers?live=false` | + Add / row Edit / row Delete |
| iBGP peers | `GET /api/admin/ibgp/peers?live=false` | + Add / row Edit / row Delete |
| WG tunnels | `GET /api/admin/wg/tunnels?live=false` | 只读 |
| Nodes | `GET /api/admin/nodes` | + Add / Edit (name/enabled/auto-peer/地址) / Policy / Status / Rotate token (一次性明文) / Delete |
| Proposals | `GET /api/admin/nodes/{id}/proposals` | Accept / Reject (带 reason) |
| Reports | `GET /api/admin/nodes/{id}/reports` | Import |
| Revisions | `GET /api/admin/nodes/{id}/revisions` | Pin (rollback) / Unpin |
| Database | `GET /api/admin/db/tables[/{table}]` | 只读分页浏览，私钥/token hash 显示为 `***` |
| Genconf | `POST /api/admin/genconf` | 触发按钮 + 显示返回的 warnings/paths |

Tab 切换使用 React `useState`，刷新按钮递增 `refreshKey` 强制组件重新挂载重新请求。

### 节点选择器

标题栏有一个全局节点下拉（`NodeContext` + `NodeSelector`），决定当前浏览/编辑哪个受管节点的数据，对应 API 的 `?node_id=`。Nodes / Genconf / Database 是 hub 全局视图，不显示选择器。

选中的 node_id 存在 `sessionStorage`（与 token 同生命周期）。

> **`<NodeProvider>` 必须包在被 `refreshKey` 当作 React `key` 的子树之外**，否则每次点 "Refresh" 都会把节点选择重置回第一个。

之前 Proposals / Reports / Revisions 各自复制了一份节点选择器与 nodes 拉取逻辑，现已统一到共享 context，只拉一次。

设计原则：

- **始终 `?live=false`**：服务端 sandbox 不能 shell out，强求会拖慢页面或报错。
- **没有 WebSocket**：tab 切换 / 手动 "Refresh" 按钮触发轮询，避免引入额外协议。
  > 注：`dn42ctl` 确实有一条 WebSocket 通道（`/api/v1/nodes/{id}/ws`），但它**只服务于节点常驻 agent**，浏览器不使用，详见 `docs/architecture/sync_ws_protocol.md`。本条决策未被推翻。
- **错误展示**：所有非 2xx 响应弹一个顶部 toast (3.5 秒消失)，正文显示 `detail` 字段。

### `FormModal` 的两个问题

`Modal.tsx` 用 `Object.fromEntries(new FormData(...))` 取值，因此：

- **未勾选的 checkbox 在 `FormData` 里是缺席而非 `false`**，取值要写 `!!d.enabled`。
- **空文本框产出 `""` 而非缺席。** 涉及"可清除"语义的字段（节点的三个地址字段）必须显式把 `"" → null` 再序列化。`""` 是 UI 表达"取消中心管理"的方式，而 `null` 才是 API 的表达方式。

## peer: 2 步向导

| 步骤 | 操作 | 关键 API |
|------|------|---------|
| 1. Authenticate | 跳转到 Kioubit 认证服务，回跳后页面把 query 中的 `params` 与 `signature` 交给后端 | `POST /api/public/auto-peer/session {params, signature}` → `peer_session_token` |
| 2. Submit peer | 表单：目标节点下拉、WG pubkey, endpoint (可空), peer LLA, listen_port | `GET /api/public/auto-peer/nodes`、`POST /api/public/auto-peer/submit` (带 Bearer peer-session) |

- 跳转用一个 `method="get"` 的表单指向 `https://dn42.g-load.eu/auth/`，隐藏字段 `return` 是页面自身地址；页面不加载第三方脚本。
- 兑换成功后立刻 `history.replaceState` 清掉 query：认证响应是一次性的，刷新会把它再交一次。
- 节点下拉的内容来自 `GET /nodes`，每个选项由节点名称与 endpoint host 组成。列表为空时该步只显示一句说明，不渲染表单。
- `peer_session_token` 只放在 React 组件状态中，不写 storage，因此刷新即作废。
- 成功后展示：`Proposal #N is pending operator approval`，并给出提案所属节点。

## 构建与开发

```bash
# 安装依赖
cd web && pnpm install

# 开发模式：自动代理 /api/* 到 [::1]:4242
pnpm dev

# 构建
pnpm build    # 输出到 web/dist/

# 预览构建结果
pnpm preview
```

开发模式使用 Vite 内置代理 (`vite.config.ts` 中 `server.proxy`)，无需手动配置 CORS。

## 部署

```bash
# 构建 (跨域部署时需指定 API 地址)
cd web && VITE_API_BASE=https://api.dn42.example.com pnpm build

# 复制到 nginx 目录
sudo dn42ctl deploy web /var/www/dn42ctl
```

详见 `docs/architecture/deployment.md`。

## 已知限制

- 无国际化：英文 UI。
- 无 WebSocket / 实时刷新；操作页面后手动 Refresh。
- 没有客户端表单复杂校验，依赖服务端 422 错误反馈。
- 浏览器最低支持：ES2020 (Chrome 90+ / Firefox 88+ / Safari 14+)。
