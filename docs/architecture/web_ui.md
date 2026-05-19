# Web UI（admin + peer 静态站点）

dn42ctl 不内置任何 HTML 渲染；UI 是两个独立的静态目录，由 **nginx** 托管，**不**由 FastAPI 提供。FastAPI 始终只回 JSON。

## 设计目标

- **零构建**：纯 HTML + Vanilla JS + Tailwind CDN，不引入 Node 工具链；放到 `/var/www/dn42ctl/...` 就能跑。
- **现代化 + 黑白配色**：仅使用 `zinc/neutral` 灰阶 + 纯黑/纯白，强调留白与排版。
- **亮色 / 暗色**：基于 Tailwind `dark:` 变体，由 `<html class="dark">` 切换，状态写 `localStorage.theme`，默认跟随 `prefers-color-scheme`。
- **同源部署**：由 nginx 把 `/`、`/admin/`、`/api/*` 都挂在同一个 vhost；CORS 不需要。
- **可独立分发**：`web/` 不依赖 dn42ctl 的任何 Python 模块，可单独打包。

## 目录布局

```
web/
├── admin/
│   ├── index.html             # 登录页 (token 输入 -> sessionStorage)
│   ├── dashboard.html         # 单页面: 顶部 tab 栏切 view
│   ├── assets/
│   │   ├── app.js             # API client + tab renderers
│   │   └── styles.css         # 主题切换 + 少量自定义
│   └── README.md
└── peer/
    ├── index.html             # 4 步向导
    ├── assets/
    │   ├── app.js
    │   └── styles.css
    └── README.md
```

## 主题策略

`<head>` 内尽早执行的内联脚本：

```html
<script>
  const saved = localStorage.getItem("theme");
  const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  if (saved === "dark" || (!saved && prefersDark)) document.documentElement.classList.add("dark");
</script>
<script src="https://cdn.tailwindcss.com"></script>
<script>tailwind.config = { darkMode: "class" };</script>
```

每个页面顶部放一个主题切换按钮，写入 `localStorage.theme = "dark" | "light"` 并 toggle `.dark`。

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

- `index.html`: 一个文本框 + “Sign in”按钮；提交时 `POST /api/show/all?live=false` 来验证 token。成功 → `sessionStorage.setItem("dn42ctl_admin_token", token)` 并 `location = "dashboard.html"`。
- `dashboard.html`: 加载时检查 `sessionStorage.dn42ctl_admin_token`；缺失则跳回登录页。
- 所有 fetch 通过 `app.js` 里的 `api(path, opts)` 包装，自动加 `Authorization: Bearer ${token}`；401 → 清空 token 并跳回登录。
- “Sign out”按钮：`sessionStorage.removeItem` + 跳转回 `index.html`。
- **不存到 `localStorage`**：保持 token 仅活在当前 tab。

## admin: 顶部 Tab 视图

| Tab | 数据来源 (admin API) | 操作 |
|-----|---------------------|------|
| Overview | `GET /api/show/all?live=false` | 只读卡片：node_id + 三类 peer 数量 |
| BGP peers | `GET /api/bgp/peers?live=false` | + Add / row Edit / row Delete |
| iBGP peers | `GET /api/ibgp/peers?live=false` | + Add / row Edit / row Delete |
| WG tunnels | `GET /api/wg/tunnels?live=false` | 只读 |
| Nodes | `GET /api/admin/nodes` | + Add / Rotate token (一次性明文) / Edit policy / Delete |
| Proposals | `GET /api/admin/nodes/{id}/proposals` (按 node 切换) | Accept / Reject (带 reason) |
| Reports | `GET /api/admin/nodes/{id}/reports` | View payload / Import |
| Revisions | `GET /api/admin/nodes/{id}/revisions` | Pin (rollback) / Unpin |
| Genconf | `POST /api/genconf` | 触发按钮 + 显示返回的 warnings/paths |

每个 add/edit 是同一个 form 组件参数化复用；删除需要二次确认 modal。

设计原则：

- **始终 `?live=false`**：服务端 sandbox 不能 shell out，强求会拖慢页面或报错。
- **没有 WebSocket**：tab 切换 / 手动 “Refresh” 按钮触发轮询，避免引入额外协议。
- **错误展示**：所有非 2xx 响应弹一个顶部 toast (3 秒消失)，正文显示 `detail` 字段。

## peer: 4 步向导

| 步骤 | 操作 | 关键 API |
|------|------|---------|
| 1. Identify | 输入 ASN | `POST /api/public/auto-peer/lookup {asn}` |
| 2. Choose mntner | 后端返回 mnt-by × auth_lines 矩阵，用户点选一行 | `POST /api/public/auto-peer/challenge {asn, mntner, auth_index}` |
| 3. Sign challenge | 页面展示 nonce + 复制粘贴命令，用户回填签名 | `POST /api/public/auto-peer/verify {challenge_id, signature}` → `peer_session_token` |
| 4. Submit peer | 表单：WG pubkey, endpoint (可空), peer LLA, peer_kind=bgp，peer_asn 锁死 | `POST /api/public/auto-peer/submit` (带 Bearer peer-session) |

- 用 `<details>` 块按 auth 类型分别展示命令模板：`ssh-keygen -Y sign -n dn42ctl-autopeer -f ~/.ssh/id_ed25519 < nonce.txt > sig.txt`，`echo "<nonce>" | gpg --clearsign`。
- `peer_session_token` 只放在 JS 内存（`let session`），不写 storage——刷新即作废。
- 成功后展示：`Proposal #N is pending operator approval`，附带服务端返回的本地 WG pubkey / endpoint / listen_port，方便对端预先配置。

## 部署

详见 `docs/architecture/deployment.md` 的 nginx 段：

```
/var/www/dn42ctl/peer/   <- web/peer/
/var/www/dn42ctl/admin/  <- web/admin/
```

## 开发模式

无构建，本地直接 `python -m http.server` 起一个静态服务即可：

```bash
cd web/peer && python -m http.server 8001
cd web/admin && python -m http.server 8002
# FastAPI 另起: uv run dn42ctl serve --host 127.0.0.1 --port 4242
```

由于跨源调用，开发时需要给 FastAPI 临时加 CORS（生产同源走 nginx，不开 CORS）。可设环境变量 `DN42CTL_DEV_CORS=1`，`app.add_middleware(CORSMiddleware, ...)` 仅在开发模式启用。

## 已知限制

- 无国际化：英文 UI（按 AGENTS.md 要求）。
- 无 WebSocket / 实时刷新；操作页面后手动 Refresh。
- 没有客户端表单复杂校验，依赖服务端 422 错误反馈。
- 浏览器最低支持：原生 ES2020 (Chrome 90+ / Firefox 88+ / Safari 14+)，无 polyfill。
