"""管理员显式编辑的节点本地 BIRD 配置。"""

from __future__ import annotations

import fcntl
import hashlib
import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from dn42ctl.file_policy import BIRD, apply_policy, ensure_policy
from dn42ctl.render import render_bird_extra_conf_placeholder
from dn42ctl.services.core import Dn42CtlError
from dn42ctl.ws_protocol import MAX_FRAME_BYTES

# JSON 转义可将一个控制字符扩成六个字节，帧还要容纳请求元数据。
MAX_BIRD_EXTRA_BYTES = MAX_FRAME_BYTES // 8


class BirdExtraError(Dn42CtlError):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class BirdExtraDocument:
    content: str
    revision: str


def encode_bird_extra(content: str) -> bytes:
    try:
        data = content.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise BirdExtraError(422, "extra.conf 必须是有效的 UTF-8 文本") from exc
    if len(data) > MAX_BIRD_EXTRA_BYTES:
        raise BirdExtraError(413, f"extra.conf 超过 {MAX_BIRD_EXTRA_BYTES} 字节")
    return data


def read_bird_extra(path: Path) -> BirdExtraDocument:
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK), "rb") as src:
            if not stat.S_ISREG(os.fstat(src.fileno()).st_mode):
                raise BirdExtraError(422, "extra.conf 必须是普通文件")
            data = src.read(MAX_BIRD_EXTRA_BYTES + 1)
    except FileNotFoundError:
        return BirdExtraDocument("", "missing")
    except OSError as exc:
        raise BirdExtraError(500, f"读取 extra.conf 失败: {exc}") from exc
    if len(data) > MAX_BIRD_EXTRA_BYTES:
        raise BirdExtraError(413, f"extra.conf 超过 {MAX_BIRD_EXTRA_BYTES} 字节")
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BirdExtraError(422, "已有 extra.conf 不是有效的 UTF-8 文本") from exc
    return BirdExtraDocument(content, hashlib.sha256(data).hexdigest())


@contextmanager
def _stage(path: Path, data: bytes) -> Iterator[Path]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
        apply_policy(tmp, BIRD)
        yield tmp
    finally:
        tmp.unlink(missing_ok=True)


def _resolve_target(path: Path) -> Path:
    try:
        return path.resolve()
    except RuntimeError as exc:
        # Python 3.11 的 Path.resolve 在符号链接成环时抛出 RuntimeError。
        raise BirdExtraError(500, f"无法解析 extra.conf 路径: {path}") from exc


def ensure_bird_extra(path: Path) -> bool:
    """返回是否新建了占位文件。genconf 和 apply 可能与 Web 保存并发。"""
    try:
        target = _resolve_target(path)
        if not target.exists():
            with _stage(target, render_bird_extra_conf_placeholder().encode("utf-8")) as tmp:
                try:
                    os.link(tmp, target)
                    return True
                except FileExistsError:
                    pass
        ensure_policy(target, BIRD)
        return False
    except OSError as exc:
        raise BirdExtraError(500, f"创建 extra.conf 占位文件失败: {exc}") from exc


def write_bird_extra(path: Path, *, content: str, expected_revision: str) -> BirdExtraDocument:
    data = encode_bird_extra(content)
    document = BirdExtraDocument(content, hashlib.sha256(data).hexdigest())
    try:
        target = _resolve_target(path)
        with _stage(target, data) as tmp:
            # rename 会更换文件 inode，采用父目录锁让不同 agent 进程的保存操作也依次比较版本。
            directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                fcntl.flock(directory, fcntl.LOCK_EX)
                current = read_bird_extra(target)
                if current.revision != expected_revision:
                    raise BirdExtraError(409, "extra.conf 已发生变化，请重新读取后保存")
                if current.revision == "missing":
                    try:
                        os.link(tmp, target)
                    except FileExistsError as exc:
                        raise BirdExtraError(409, "extra.conf 已创建，请重新读取后保存") from exc
                elif current.revision != document.revision:
                    # 使用新 inode 保存时保留文件属主，否则原来可以手工编辑的用户可能失去写权限。
                    previous, staged = target.stat(), tmp.stat()
                    if (staged.st_uid, staged.st_gid) != (previous.st_uid, previous.st_gid):
                        os.chown(tmp, previous.st_uid, previous.st_gid)
                    os.replace(tmp, target)
            finally:
                os.close(directory)
    except OSError as exc:
        raise BirdExtraError(500, f"保存 extra.conf 失败: {exc}") from exc
    return document
