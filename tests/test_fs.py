from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from dn42ctl.fs import chmod_best_effort, chown_best_effort


class TestChmodBestEffort:
    def test_success(self, tmp_path: Path) -> None:
        f = tmp_path / "test.txt"
        f.write_text("hello")
        chmod_best_effort(f, 0o600)

    def test_oserror_silenced(self) -> None:
        with patch("os.chmod", side_effect=OSError("perm denied")):
            chmod_best_effort(Path("/nonexistent"), 0o600)


class TestChownBestEffort:
    def test_success(self) -> None:
        mock_grp = MagicMock()
        mock_grp.gr_gid = 1000
        with (
            patch("grp.getgrnam", return_value=mock_grp) as grnam,
            patch("os.chown") as chown,
        ):
            chown_best_effort(Path("/test"), 0, "testgroup")
            grnam.assert_called_once_with("testgroup")
            chown.assert_called_once_with(Path("/test"), 0, 1000)

    def test_group_not_found(self) -> None:
        with patch("grp.getgrnam", side_effect=KeyError("no group")):
            chown_best_effort(Path("/test"), 0, "badgroup")

    def test_chown_oserror_silenced(self) -> None:
        mock_grp = MagicMock()
        mock_grp.gr_gid = 1000
        with (
            patch("grp.getgrnam", return_value=mock_grp),
            patch("os.chown", side_effect=OSError("perm denied")),
        ):
            chown_best_effort(Path("/test"), 0, "testgroup")
