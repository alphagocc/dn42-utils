"""CLI 层的异常到退出码映射。

契约与保留在命令内部的例外见 docs/architecture/cli_errors.md。
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar, cast

import click
import typer

from dn42ctl.config import ConfigError
from dn42ctl.context import AppContext
from dn42ctl.db import DatabaseError
from dn42ctl.node_config import NodeConfigError
from dn42ctl.services import Dn42CtlError
from dn42ctl.wg import WireGuardError

EXIT_FAILURE = 1
EXIT_USAGE = 2

_F = TypeVar("_F", bound=Callable[..., Any])


def db_open_hint(db_path: Path) -> str:
    return (
        f"无法打开数据库: {db_path}。"
        "若使用默认系统路径，通常需要以 root 运行（sudo），"
        "或使用 --db-path 覆盖到可写位置。"
    )


def _current_db_hint() -> str | None:
    """从 click 的当前上下文取出 --db-path 用于生成提示。

    装饰器没有形参可以获得 AppContext。根 callback 把它放进 ctx.obj，click 的子命令继承
    父级的 obj，因此连没有声明 ctx 形参的 deploy 命令也能取到。
    """
    ctx = click.get_current_context(silent=True)
    obj = getattr(ctx, "obj", None) if ctx is not None else None
    if isinstance(obj, AppContext):
        return db_open_hint(obj.db_path)
    return None


def cli_errors(fn: _F) -> _F:
    """把异常映射为退出码与一行 stderr 消息。

    必须写在 @app.command() 的下方：注册发生在导入期，顺序颠倒会让 Typer 拿到未包裹的原
    函数，且不会有任何报错。functools.wraps 留下的 __wrapped__ 让 Typer 的
    inspect.signature 仍然读到原始形参，参数声明与 --help 因此不受影响。
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except typer.BadParameter as exc:
            typer.echo(f"输入错误: {exc}", err=True)
            raise typer.Exit(EXIT_USAGE) from exc
        except (ConfigError, NodeConfigError) as exc:
            typer.echo(f"错误: {exc}", err=True)
            raise typer.Exit(EXIT_USAGE) from exc
        except DatabaseError as exc:
            typer.echo(f"错误: {exc}", err=True)
            hint = _current_db_hint()
            if hint is not None:
                typer.echo(hint, err=True)
            raise typer.Exit(EXIT_FAILURE) from exc
        except (Dn42CtlError, WireGuardError) as exc:
            typer.echo(f"错误: {exc}", err=True)
            raise typer.Exit(EXIT_FAILURE) from exc

    return cast(_F, wrapper)
