# web deploy

构建 Web UI 并复制到指定目录。

## 用法

```bash
dn42ctl web deploy <dest>
dn42ctl web deploy --skip-build <dest>
```

## 参数

| 参数 | 说明 |
|------|------|
| `dest` | 部署目标目录 (如 `/var/www/dn42ctl`) |
| `--skip-build` | 跳过构建，直接复制已有 `dist/` |

## 行为

1. 定位 `web/` 目录（相对于包安装位置）
2. 运行 `pnpm install --frozen-lockfile` + `pnpm build`（除非 `--skip-build`）
3. 将 `dist/admin/`、`dist/peer/`、`dist/assets/` 复制到 `<dest>/`

## 前置条件

- 需要安装 `pnpm`
- `web/` 目录下需要有 `package.json` 和 `pnpm-lock.yaml`

## 示例

```bash
# 构建并部署到 nginx 目录
sudo dn42ctl web deploy /var/www/dn42ctl

# 仅复制已构建的文件（跳过构建）
sudo dn42ctl web deploy --skip-build /var/www/dn42ctl
```
