# CLI 异常处理与退出码

命令函数不再各自编写 `try/except`。异常到退出码的映射集中在
[`src/dn42ctl/cli_errors.py`](../../src/dn42ctl/cli_errors.py) 的 `@cli_errors` 装饰器里。

## 退出码契约

退出码由异常类型决定，与调用点无关。

| 异常 | 语义 | 退出码 |
| --- | --- | ---: |
| `typer.BadParameter` | 参数不合法 | 2 |
| `ConfigError` | config.toml 缺失或读取失败 | 2 |
| `NodeConfigError` | node.toml 缺失或不合法 | 2 |
| `Dn42CtlError` | 服务层运行期失败 | 1 |
| `DatabaseError` | 数据库打开或查询失败 | 1 |
| `WireGuardError` | `wg` 调用失败 | 1 |

分界在于用户能否自行修正。退出码 2 表示命令尚未开始实际工作，修正参数或先执行
`dn42ctl init` 即可；退出码 1 表示命令已经开始执行并在中途失败。

错误消息一律写入 stderr。服务层与配置类错误的格式是 `错误: <消息>`，参数类错误的格式是
`输入错误: <消息>`。写入 stderr 让 `dn42ctl show ... | jq` 一类管道用法不会把错误文案
混进 JSON。

## 装饰器的位置

`@cli_errors` 必须写在 Typer 的注册装饰器**下方**：

```python
@app.command("genconf")
@cli_errors
def cmd_genconf(ctx: typer.Context, ...) -> None:
    ...
```

`@app.command()` 在导入期就把紧随其下的可调用对象注册进去，顺序颠倒会让 Typer 拿到未包裹
的原函数，装饰器就此失效且没有任何报错。

`cli_errors` 使用 `functools.wraps`，因此 `__wrapped__` 得以保留。Typer 通过
`inspect.signature` 读取形参时会跟随该属性，参数声明、默认值与 `--help` 均不受影响。

## 数据库提示的获取方式

数据库打不开时，除异常消息外还需要提示用户可能要以 root 运行。该提示需要 `--db-path`
的实际取值，而装饰器没有形参可以获得它。

`cli_errors` 通过 `click.get_current_context(silent=True)` 读取当前命令的 `ctx.obj`。根
callback 在那里放置了 `AppContext`，click 的子命令会继承父级的 `obj`，因此连没有声明
`ctx` 形参的 `deploy` 命令也能取到。取不到 `AppContext` 时不打印提示。

## 保留在命令内部的异常块

装饰器只表达「打印一行消息并以固定退出码结束」。except 体内有其他行为的块保留原样：

| 位置 | 保留原因 |
| --- | --- |
| `cmd_scan` 回写 config.toml | 失败后改写提示文案并继续执行 scan |
| `cmd_node_once` | 两层嵌套 try，失败时还要向中心上报 |
| `cmd_node_agent` | 捕获 `KeyboardInterrupt` 后以 0 退出，`systemctl stop` 不是故障 |
| `cmd_node_status` | 文案带两个空格的缩进，且 `NodeClientError` 用「不可达」而非「错误」 |
| `cmd_node_apply` | 文案带阶段前缀「错误 (pull):」与「错误 (apply):」 |
| `cmd_node_init` | 文案插值目标文件位置 |
| `cmd_node_push` 与 `cmd_node_report` | JSON 读取失败的文案是「无法读取 JSON」 |
| `cmd_serve` | 唯一使用 `else:` 子句，self 注册失败只告警不退出 |
| 各处 `(PermissionError, OSError)` | 每个调用点的消息带各自的目标文件 |

`PermissionError` 与 `OSError` 刻意不纳入装饰器：这两类过于宽泛，纳入后会把与本次操作无关
的 I/O 故障也压成同一条消息。

`NodeClientError` 同样不纳入。它只在 `cmd_node_status` 逃逸到 CLI，其余命令在服务层已经把
它翻译成 `Dn42CtlError`；纳入还会让 `httpx` 变成 CLI 的顶层导入。

## 相关文档

[`validation.md`](validation.md) 记录输入校验如何把 `ValidationError` 转换为
`typer.BadParameter`。

[`testing.md`](testing.md) 记录 CLI 测试如何断言退出码与输出。
