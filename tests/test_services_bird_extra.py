from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from dn42ctl.file_policy import BIRD
from dn42ctl.services.bird_extra import (
    MAX_BIRD_EXTRA_BYTES,
    BirdExtraError,
    ensure_bird_extra,
    read_bird_extra,
    write_bird_extra,
)


def test_create_edit_clear_and_preserve_local_changes(tmp_path: Path) -> None:
    path = tmp_path / "bird" / "extra.conf"
    missing = read_bird_extra(path)
    assert not path.exists()
    first = write_bird_extra(path, content="# 中文\r\n", expected_revision=missing.revision)
    assert path.read_bytes() == first.content.encode()
    assert path.stat().st_mode & 0o777 == BIRD.mode
    assert not ensure_bird_extra(path)
    assert read_bird_extra(path) == first

    path.write_text("# operator edit\n")
    with pytest.raises(BirdExtraError) as conflict:
        write_bird_extra(path, content="", expected_revision=first.revision)
    assert conflict.value.status_code == 409
    current = read_bird_extra(path)
    assert current.content == "# operator edit\n"
    empty = write_bird_extra(path, content="", expected_revision=current.revision)
    assert empty.revision != missing.revision
    assert path.read_bytes() == b""


def test_concurrent_web_saves_only_replace_the_reviewed_revision(tmp_path: Path) -> None:
    path = tmp_path / "extra.conf"
    assert ensure_bird_extra(path)
    revision = read_bird_extra(path).revision
    ready = Barrier(2)

    def save(text: str) -> int:
        ready.wait(timeout=5)
        try:
            write_bird_extra(path, content=text, expected_revision=revision)
        except BirdExtraError as exc:
            return exc.status_code
        return 200

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(save, ("first", "second"))) == [200, 409]
    assert path.read_text() in {"first", "second"}
    assert list(tmp_path.iterdir()) == [path]


def test_invalid_text_never_replaces_the_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "extra.conf"
    path.write_bytes(b"\xff\xfe")
    with pytest.raises(BirdExtraError) as invalid:
        read_bird_extra(path)
    assert invalid.value.status_code == 422
    with pytest.raises(BirdExtraError) as too_large:
        write_bird_extra(path, content="x" * (MAX_BIRD_EXTRA_BYTES + 1), expected_revision="missing")
    assert too_large.value.status_code == 413
    assert path.read_bytes() == b"\xff\xfe"


def test_save_preserves_configured_symlink(tmp_path: Path) -> None:
    target = tmp_path / "operator.conf"
    target.write_text("# old\n")
    path = tmp_path / "extra.conf"
    path.symlink_to(target)
    write_bird_extra(path, content="# new\n", expected_revision=read_bird_extra(path).revision)
    assert path.is_symlink()
    assert target.read_text() == "# new\n"
