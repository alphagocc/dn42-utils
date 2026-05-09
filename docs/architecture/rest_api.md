# REST API

dn42ctl 提供可选的 REST API 模式，通过 `dn42ctl serve` 启动 HTTP 服务器。

## 运行方式

```bash
dn42ctl serve --token <secret> [--host 127.0.0.1] [--port 4242]
```

- 默认绑定 `127.0.0.1`（仅本地访问），端口 `4242`。
- `--token` 必须提供（也可通过环境变量 `DN42CTL_API_TOKEN`）。

## 鉴权

所有 API 请求需携带 `Authorization: Bearer <token>` 头。Token 与 `--token` 参数一致。未通过鉴权返回 `401 Unauthorized`。

## 路由表

所有路由均在 `/api/` 下：

| 方法 | 路径 | 对应 CLI | 说明 |
|------|------|---------|------|
| `GET` | `/api/bgp/peers` | `show bgp` | 列出所有 BGP peer（支持 `?live=false` 跳过探测） |
| `POST` | `/api/bgp/peers` | `bgp peer` | 创建 BGP peer |
| `PUT` | `/api/bgp/peers/{asn}` | `bgp peer modify` | 修改 BGP peer |
| `DELETE` | `/api/bgp/peers/{asn}` | `bgp peer del` | 删除 BGP peer |
| `GET` | `/api/ibgp/peers` | `show ibgp` | 列出所有 iBGP peer（支持 `?live=false`） |
| `POST` | `/api/ibgp/peers` | `ibgp peer` | 创建 iBGP peer |
| `PUT` | `/api/ibgp/peers/{name}` | `ibgp peer modify` | 修改 iBGP peer |
| `DELETE` | `/api/ibgp/peers/{name}` | `ibgp peer del` | 删除 iBGP peer |
| `GET` | `/api/wg/tunnels` | `show wg` | 列出所有 WireGuard 隧道（支持 `?live=false`） |
| `GET` | `/api/show/all` | `show all` | 聚合视图：node_id + wg/bgp/ibgp 列表 |
| `POST` | `/api/genconf` | `genconf` | 重新生成 Bird/Babel/ROA 配置 |

## 排除的命令

`init` 和 `scan` 仅在 CLI 中可用，不暴露为 API。原因：
- `init` 涉及交互式配置创建和系统接口初始化。
- `scan` 涉及扫描本地文件系统并修改 config.toml。

## 错误处理

- 服务层 `Dn42CtlError` → HTTP `400` + `{"detail": "..."}`
- 鉴权失败 → HTTP `401`
- 输入格式/类型校验失败（Pydantic `field_validator`） → HTTP `422` + `{"detail": [{"loc": [...], "msg": "...", "type": "..."}]}`
- 路径参数无效 → HTTP `422`（FastAPI 自动校验）

详见 `docs/architecture/validation.md`。

## 技术栈

- 框架：FastAPI
- ASGI 服务器：Uvicorn
- 请求/响应模型：Pydantic v2
