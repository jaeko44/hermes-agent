"""Regression tests for the DM rendezvous directory fallback.

The class: ``_dm_dir()`` trusted ``mkdir(exist_ok=True)``/``lstat`` as proof that
the shared-temp directory was usable, so a directory sealed by an administrator
made EVERY peer DM fail with ``[WinError 5] Access is denied`` while looking like a
tool bug. Two defects are pinned here:

1. no fallback - an unusable primary directory was fatal;
2. a broken writability probe - the first attempt probed with ``tempfile.mkstemp``,
   which HANGS on the locked directory it was meant to detect, because CPython's
   mkstemp trusts ``os.access(dir, W_OK)`` and retries TMP_MAX times on the
   resulting PermissionError.
"""
from __future__ import annotations

import errno
import os
import stat
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools import bot_mode_dm as m  # noqa: E402


@pytest.fixture
def unlocked_cache():
    """Every test starts with an unresolved (uncached) DM directory."""
    saved = m._DM_DIR_CACHED
    m._DM_DIR_CACHED = None
    yield
    m._DM_DIR_CACHED = saved


class _SealedDir:
    """A directory that accepts mkdir/lstat but denies every create inside.

    Stands in for the administrator-sealed ``%TEMP%\\hermes-dm`` on the estate host.
    """

    def __init__(self, path: Path):
        self.path = path

    def __enter__(self):
        real_open = os.open

        def fake_open(file, flags, mode=0o777, **kwargs):
            if (isinstance(file, (str, bytes, os.PathLike))
                    and str(file).startswith(str(self.path))
                    and flags & os.O_CREAT):
                raise PermissionError(
                    errno.EACCES, os.strerror(errno.EACCES), str(file))
            return real_open(file, flags, mode, **kwargs)

        real_access = os.access
        real_isdir = os.path.isdir

        def fake_access(p, mode, **kw):
            if str(p) == str(self.path):
                return True  # the token's granted rights lie about the ACL
            return real_access(p, mode, **kw)

        self._saved = (os.open, os.access, os.path.isdir)
        os.open = fake_open
        os.access = fake_access
        return self

    def __exit__(self, *exc):
        os.open, os.access, os.path.isdir = self._saved
        return False


def test_candidate_order_puts_shared_temp_first(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(os, "environ", dict(os.environ, LOCALAPPDATA=str(tmp_path / "lad")))
    candidates = m._dm_dir_candidates(None)
    assert candidates[0] == tmp_path / m._DM_DIR_NAME
    assert len(candidates) > 1, "there must be a fallback candidate"


def test_sealed_primary_falls_back_to_a_writable_dir(tmp_path, monkeypatch, unlocked_cache):
    primary = tmp_path / m._DM_DIR_NAME
    primary.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(os, "environ", dict(os.environ, LOCALAPPDATA=str(tmp_path / "lad")))

    with _SealedDir(primary):
        chosen = m._dm_dir()

    assert chosen != primary
    # The chosen directory must actually accept the write the caller performs.
    fd, path = tempfile.mkstemp(dir=chosen)
    os.close(fd)
    os.unlink(path)


def test_writability_probe_does_not_hang_on_a_sealed_dir(tmp_path):
    """The probe must raise immediately; mkstemp-based probing spun for 25s+."""
    sealed = tmp_path / m._DM_DIR_NAME
    sealed.mkdir()
    with _SealedDir(sealed):
        with pytest.raises(PermissionError):
            m._dm_dir_is_writable(sealed)


def test_probe_tolerates_a_leftover_probe_file(tmp_path):
    """A killed process leaves .dm-probe-<pid>; that is not a broken directory."""
    target = tmp_path / m._DM_DIR_NAME
    target.mkdir()
    stale = target / (".dm-probe-99999")
    stale.write_text("x")
    m._dm_dir_is_writable(target)  # must not raise
    assert stale.exists(), "a pre-existing probe file must be left alone"


def test_healthy_directory_is_used_without_fallback(tmp_path, monkeypatch, unlocked_cache):
    healthy = tmp_path / m._DM_DIR_NAME
    healthy.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(os, "environ", dict(os.environ, LOCALAPPDATA=str(tmp_path / "lad")))
    assert m._dm_dir() == healthy


def test_dm_file_write_roundtrips_through_the_resolved_dir(tmp_path, monkeypatch, unlocked_cache):
    primary = tmp_path / m._DM_DIR_NAME
    primary.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(os, "environ", dict(os.environ, LOCALAPPDATA=str(tmp_path / "lad")))

    with _SealedDir(primary):
        path = m._write_dm_file("regression probe payload")
        try:
            assert Path(path).read_text(encoding="utf-8") == "regression probe payload"
            assert Path(path).parent == m._dm_dir()
        finally:
            os.unlink(path)


def test_posix_mode_tightening_is_still_enforced(tmp_path, monkeypatch):
    """The 0700 guard must survive the Windows-only branch we added.

    Simulated, not host-dependent: ``_prepare_dm_dir`` skips the mode check on
    Windows because S_IMODE is always 0777 there, so the real POSIX behaviour can
    only be exercised by faking a POSIX uid and a POSIX-reported mode.
    """
    world = tmp_path / "world"
    world.mkdir()
    os.chmod(world, 0o755)
    real_lstat = Path.lstat

    def fake_lstat(self):
        # os.stat_result fields are readonly, so hand back a stand-in carrying a
        # POSIX-style owner and a world-readable mode so both branches fire.
        real = real_lstat(self)

        class _FakeStat:
            st_mode = stat.S_IFDIR | 0o755
            st_uid = 4242
            st_gid = 4242
            st_size = real.st_size
            st_mtime = real.st_mtime

        return _FakeStat()

    monkeypatch.setattr(Path, "lstat", fake_lstat)
    m._prepare_dm_dir(world, 4242)


def test_all_candidates_unusable_raises_the_first_error(tmp_path, monkeypatch, unlocked_cache):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "t"))
    monkeypatch.setattr(os, "environ", dict(os.environ, LOCALAPPDATA=str(tmp_path / "lad")))
    monkeypatch.setattr(Path, "mkdir", lambda self, *a, **k: (_ for _ in ()).throw(
        PermissionError(errno.EACCES, "sealed")))
    with pytest.raises(PermissionError):
        m._dm_dir()