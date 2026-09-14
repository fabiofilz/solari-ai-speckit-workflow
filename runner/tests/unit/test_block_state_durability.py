"""Fifth remediation B1: BlockState persistence fails closed on every
durability step (temp-file fsync, atomic replace, directory fsync); only
the documented UNSUPPORTED directory-fsync cases may continue."""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

import pytest

from solari_workflow.state import block_state as bs


def _state(name: str = "Test") -> bs.BlockState:
    return bs.new_block_state(branch_name="T091-Test", first_task="T091", last_task="T091", block_name=name)


def _is_dir_fd(fd: int) -> bool:
    return stat.S_ISDIR(os.fstat(fd).st_mode)


def _no_temp_files(ai_runs: Path) -> bool:
    return not list(ai_runs.glob(".block-state.*.tmp"))


def _fail_fsync(monkeypatch: pytest.MonkeyPatch, *, on_directory: bool, code: int) -> list[str]:
    real_fsync = os.fsync
    calls: list[str] = []

    def _fsync(fd: int) -> None:
        kind = "dir" if _is_dir_fd(fd) else "file"
        calls.append(kind)
        if (kind == "dir") == on_directory:
            raise OSError(code, os.strerror(code))
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", _fsync)
    return calls


def test_temp_file_fsync_failure_fails_the_write_and_keeps_previous_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bs.write_block_state(tmp_path, _state("Before"))
    before = (tmp_path / ".block-state.json").read_bytes()
    _fail_fsync(monkeypatch, on_directory=False, code=errno.EIO)
    with pytest.raises(OSError) as excinfo:
        bs.write_block_state(tmp_path, _state("After"))
    assert excinfo.value.errno == errno.EIO
    assert (tmp_path / ".block-state.json").read_bytes() == before
    assert _no_temp_files(tmp_path)


def test_replace_failure_fails_the_write_and_keeps_previous_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bs.write_block_state(tmp_path, _state("Before"))
    before = (tmp_path / ".block-state.json").read_bytes()

    def _replace(src, dst):
        raise OSError(errno.EXDEV, "simulated replace failure")

    monkeypatch.setattr(os, "replace", _replace)
    with pytest.raises(OSError):
        bs.write_block_state(tmp_path, _state("After"))
    assert (tmp_path / ".block-state.json").read_bytes() == before
    assert _no_temp_files(tmp_path)


@pytest.mark.parametrize("code", [errno.EIO, errno.ENOSPC, errno.EBADF, errno.EROFS])
def test_directory_fsync_failure_fails_the_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    if not bs._directory_fsync_platform_supported():
        pytest.skip("directory fsync is documented as unsupported on this platform")
    calls = _fail_fsync(monkeypatch, on_directory=True, code=code)
    with pytest.raises(OSError) as excinfo:
        bs.write_block_state(tmp_path, _state())
    assert excinfo.value.errno == code
    assert calls == ["file", "dir"]


def test_directory_open_failure_fails_the_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if not bs._directory_fsync_platform_supported():
        pytest.skip("directory fsync is documented as unsupported on this platform")
    real_open = os.open

    def _open(path, flags, *args, **kwargs):
        if Path(path) == tmp_path:
            raise OSError(errno.EACCES, "simulated directory open failure")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", _open)
    with pytest.raises(OSError) as excinfo:
        bs.write_block_state(tmp_path, _state())
    assert excinfo.value.errno == errno.EACCES


def test_supported_directory_fsync_is_performed_and_write_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not bs._directory_fsync_platform_supported():
        pytest.skip("directory fsync is documented as unsupported on this platform")
    real_fsync = os.fsync
    calls: list[str] = []

    def _spy(fd: int) -> None:
        calls.append("dir" if _is_dir_fd(fd) else "file")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", _spy)
    bs.write_block_state(tmp_path, _state("Synced"))
    assert calls == ["file", "dir"]
    assert bs.load_block_state(tmp_path).block_name == "Synced"
    assert bs._fsync_directory(tmp_path) is True


@pytest.mark.parametrize("code", sorted(bs._DIRECTORY_FSYNC_UNSUPPORTED_ERRNOS))
def test_documented_unsupported_directory_fsync_errno_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    if not bs._directory_fsync_platform_supported():
        pytest.skip("directory fsync is documented as unsupported on this platform")
    _fail_fsync(monkeypatch, on_directory=True, code=code)
    bs.write_block_state(tmp_path, _state("Unsupported"))
    assert bs.load_block_state(tmp_path).block_name == "Unsupported"
    assert bs._fsync_directory(tmp_path) is False


def test_unsupported_errno_set_is_exactly_the_documented_one() -> None:
    expected = {errno.EINVAL}
    for name in ("ENOTSUP", "EOPNOTSUPP"):
        if hasattr(errno, name):
            expected.add(getattr(errno, name))
    assert bs._DIRECTORY_FSYNC_UNSUPPORTED_ERRNOS == frozenset(expected)
    assert errno.EIO not in bs._DIRECTORY_FSYNC_UNSUPPORTED_ERRNOS


def test_non_posix_platform_skips_directory_fsync_by_documented_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bs, "_directory_fsync_platform_supported", lambda: False)
    real_fsync = os.fsync
    calls: list[str] = []

    def _spy(fd: int) -> None:
        calls.append("dir" if _is_dir_fd(fd) else "file")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", _spy)
    bs.write_block_state(tmp_path, _state("Portable"))
    assert calls == ["file"]
    assert bs.load_block_state(tmp_path).block_name == "Portable"
