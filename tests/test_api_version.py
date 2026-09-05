from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from dn42ctl import __version__
from dn42ctl.api import app


@pytest.mark.parametrize(
    ("content", "commit"),
    [(None, None), (" \n", None), (" ce53440\n", "ce53440")],
    ids=["missing-build-metadata", "empty-build-metadata", "build-commit"],
)
def test_public_version_reads_optional_packaged_commit(tmp_path: Path, content: str | None, commit: str | None) -> None:
    if content is not None:
        (tmp_path / "_build_commit.txt").write_text(content)
    with patch("dn42ctl._version_info.importlib.resources.files", return_value=tmp_path):
        response = TestClient(app).get("/api/version")
    assert response.status_code == 200
    assert response.json() == {"version": __version__, "commit": commit}
