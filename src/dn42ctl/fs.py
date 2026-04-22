from __future__ import annotations

import os
from pathlib import Path


def chmod_best_effort(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass
