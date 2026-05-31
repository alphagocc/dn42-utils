from __future__ import annotations

import importlib.resources


def get_commit() -> str | None:
    try:
        ref = importlib.resources.files("dn42ctl").joinpath("_build_commit.txt")
        return ref.read_text(encoding="utf-8").strip() or None
    except (FileNotFoundError, TypeError, OSError):
        return None
