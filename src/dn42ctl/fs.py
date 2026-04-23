from __future__ import annotations

import grp
import os
from pathlib import Path


def chmod_best_effort(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def chown_best_effort(path: Path, uid: int, group: str) -> None:
    try:
        gid = grp.getgrnam(group).gr_gid
    except (KeyError, OSError):
        return
    try:
        os.chown(path, uid, gid)
    except OSError:
        pass
