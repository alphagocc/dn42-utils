# Web UI（admin + peer — React + Vite）

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
├── pnpm-workspace.yaml           # minimumReleaseAge 配置
├── pnpm-lock.yaml
├── vite.config.ts                 # 多页入口 (admin + peer)
├── tsconfig.json
├── admin/
│   └── index.html                 # Vite 入口 → src/admin/main.tsx
├── peer/
│   └── index.html                 # Vite 入口 → src/peer/main.tsx
└── src/
    ├── shared/
    │   ├── api.ts                 # fetch 封装 (Bearer token, 401 处理)
    │   ├── theme.ts               # 主题切换逻辑
    │   ├── index.css              # Tailwind 指令 + 字体
    │   └── components/
    │       ├── Table.tsx           # 通用数据表格
    │       ├── Modal.tsx           # 弹窗 (表单 + 确认)
    │       ├── Toast.tsx           # 通知 (React Context)
    │       └── ThemeToggle.tsx     # 主题切换按钮
    ├── admin/
    │   ├── main.tsx               # React 根
    │   ├── App.tsx                # 登录/仪表盘条件渲染
    │   ├── Login.tsx              # token 登录表单
    │   ├── Dashboard.tsx          # Tab 容器 + 标题栏
    │   └── tabs/
    │       ├── Overview.tsx
    │       ├── Bgp.tsx            # CRUD
    │       ├── Ibgp.tsx           # CRUD
    │       ├── Wg.tsx             # 只读
    │       ├── Nodes.tsx          # CRUD + rotate token
    │       ├── Proposals.tsx      # accept/reject + 节点选择器
    │       ├── Reports.tsx        # import + 节点选择器
    │       ├── Revisions.tsx      # pin/unpin + 节点选择器
    │       └── Genconf.tsx        # 触发按钮
    └── peer/
        ├── main.tsx
        ├── App.tsx                # 步骤状态机 + 步骤指示器
        └── steps/
            ├── Step1Lookup.tsx
            ├── Step2Auth.tsx
            ├── Step3Sign.tsx
            ├── Step4Submit.tsx
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

## admin: 鉴权与状态

- 无路由库：`App.tsx` 根据 `sessionStorage.dn42ctl_admin_token` 条件渲染 `<Login />` 或 `<Dashboard />`。
- 所有 fetch 通过 `shared/api.ts` 的 `api()` 封装，自动加 `Authorization: Bearer ${token}`；401 → 清空 token 并触发重新渲染到登录页。
- "Sign out"按钮：`sessionStorage.removeItem` + 状态更新。
- **不存到 `localStorage`**：保持 token 仅活在当前 tab。

## admin: 顶部 Tab 视图

| Tab | 数据来源 (admin API) | 操作 |
|-----|---------------------|------|
| Overview | `GET /api/show/all?live=false` | 只读卡片：node_id + 三类 peer 数量 |
| BGP peers | `GET /api/bgp/peers?live=false` | + Add / row Edit / row Delete |
| iBGP peers | `GET /api/ibgp/peers?live=false` | + Add / row Edit / row Delete |
| WG tunnels | `GET /api/wg/tunnels?live=false` | 只读 |
| Nodes | `GET /api/admin/nodes` | + Add / Rotate token (一次性明文) / Delete |
| Proposals | `GET /api/admin/nodes/{id}/proposals` (按 node 切换) | Accept / Reject (带 reason) |
| Reports | `GET /api/admin/nodes/{id}/reports` | Import |
| Revisions | `GET /api/admin/nodes/{id}/revisions` | Pin (rollback) / Unpin |
| Genconf | `POST /api/genconf` | 触发按钮 + 显示返回的 warnings/paths |

Tab 切换使用 React `useState`，刷新按钮递增 `refreshKey` 强制组件重新挂载重新请求。

设计原则：

- **始终 `?live=false`**：服务端 sandbox 不能 shell out，强求会拖慢页面或报错。
- **没有 WebSocket**：tab 切换 / 手动 "Refresh" 按钮触发轮询，避免引入额外协议。
- **错误展示**：所有非 2xx 响应弹一个顶部 toast (3.5 秒消失)，正文显示 `detail` 字段。

## peer: 4 步向导

| 步骤 | 操作 | 关键 API |
|------|------|---------|
| 1. Identify | 输入 ASN | `POST /api/public/auto-peer/lookup {asn}` |
| 2. Choose mntner | 后端返回 mnt-by × auth_lines 矩阵，用户点选一行 | `POST /api/public/auto-peer/challenge {asn, mntner, auth_index}` |
| 3. Sign challenge | 页面展示 nonce + 复制粘贴命令，用户回填签名 | `POST /api/public/auto-peer/verify {challenge_id, signature}` → `peer_session_token` |
| 4. Submit peer | 表单：WG pubkey, endpoint (可空), peer LLA, net_backend, listen_port | `POST /api/public/auto-peer/submit` (带 Bearer peer-session) |

- `peer_session_token` 只放在 React 组件状态中，不写 storage——刷新即作废。
- 成功后展示：`Proposal #N is pending operator approval`。

## 构建与开发

```bash
# 安装依赖
cd web && pnpm install

# 开发模式 (自动代理 /api/* 到 [::1]:4242)
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
sudo dn42ctl web deploy /var/www/dn42ctl
```

详见 `docs/architecture/deployment.md`。

## 已知限制

- 无国际化：英文 UI。
- 无 WebSocket / 实时刷新；操作页面后手动 Refresh。
- 没有客户端表单复杂校验，依赖服务端 422 错误反馈。
- 浏览器最低支持：ES2020 (Chrome 90+ / Firefox 88+ / Safari 14+)。
