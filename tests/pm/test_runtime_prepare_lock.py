"""The runtime prepare lock must not be waited on forever (t_8a57d263).

Measured failure this pins: on 2026-09-27 a bot-chat delivery child spent 1184s of an 1800s
wall with no output, because ``prepare_runtime()`` called ``lock_fd(..., wait=True)`` with no
timeout while another process was inside ``stage_runtime()``. A diverged ``selected.json``
identity skips the fast path, so EVERY `hermes` CLI call re-staged and re-held the same global
``.prepare.lock`` — one slow stage wedged the whole CLI, fleet-wide.

The contract these tests pin, in the order it matters:
  1. a held lock must NOT block for longer than the bound;
  2. on timeout it must RAISE (never stage unserialized — that would corrupt the generation
     other workers import);
  3. the error must NAME the lock path, so the operator knows what is wedged behind it;
  4. a free lock must still not add measurable latency to the fast path.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
import time

import pytest


@pytest.fixture(autouse=True)
def isolated_pm_home(tmp_path, monkeypatch):
    """Point PM's home AND its runtime store at a temp dir.

    ``runtime_environment()`` calls ``store_root()``, which falls back to
    ``<repo>/../manifest.json`` — the REAL hermes home — when ``HERMES_RUNTIME_DIR`` is unset,
    and the suite's home guard refuses that I/O. Isolating here keeps these tests exercising
    the production path instead of tripping the guard.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(home / "tools"))
    return home


def _hold_lock(path):
    """Take the OS lock the way ``prepare_runtime`` does, from THIS process."""
    from pm.filesystem import lock_fd

    lock_path = path / ".prepare.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    assert lock_fd(fd, wait=True, timeout=5.0), "could not take the lock for the fixture"
    # The fd is deliberately kept open by the caller for the duration of the test.
    return fd


def test_prepare_runtime_gives_up_on_a_held_lock_and_names_it(tmp_path, monkeypatch):
    from pm.package import InstallError
    from pm import runtime as runtime_mod

    monkeypatch.setattr(runtime_mod, "_PREPARE_LOCK_TIMEOUT_SECONDS", 1.0)
    root = tmp_path / "runtime"
    held = _hold_lock(root)
    try:
        started = time.monotonic()
        with pytest.raises(InstallError) as caught:
            runtime_mod.prepare_runtime(Path("uv"), Path(sys.executable), root)
        elapsed = time.monotonic() - started

        # 1. bounded: the bound plus a small, capped diagnostic probe — never the 5s the lock
        #    is actually held for, and never forever.
        assert elapsed < 3.5, "prepare_runtime blocked %.1fs on a held lock" % elapsed
        # 2. it RAISED instead of staging: no generation may be published without the lock.
        assert not (root / "selected.json").exists(), "staged without holding the lock"
        assert not (root / "generations").exists(), "published a generation without the lock"
        # 3. the message names what is wedged behind the lock.
        message = str(caught.value)
        assert str(root / ".prepare.lock") in message, message
        assert "held the runtime prepare lock" in message, message
    finally:
        os.close(held)


def test_prepare_runtime_fast_path_is_not_slowed_by_the_bound(tmp_path, monkeypatch):
    """A FREE lock must not pay the timeout: the fast path returns at once."""
    from pm import runtime as runtime_mod
    from pm.package import InstallError

    monkeypatch.setattr(runtime_mod, "_PREPARE_LOCK_TIMEOUT_SECONDS", 5.0)
    root = tmp_path / "runtime"

    # No selected.json and lazy installs disabled -> raises for THAT reason, not the lock.
    started = time.monotonic()
    with pytest.raises(InstallError) as caught:
        runtime_mod.prepare_runtime(Path("uv"), Path(sys.executable), root, bootstrap=False)
    elapsed = time.monotonic() - started

    assert elapsed < 2.0, "the free-lock fast path waited %.1fs" % elapsed
    assert "held the runtime prepare lock" not in str(caught.value), str(caught.value)
    assert "lazy installs are disabled" in str(caught.value), str(caught.value)
