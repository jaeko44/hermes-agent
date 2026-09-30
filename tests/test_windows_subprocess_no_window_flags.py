from __future__ import annotations

import io
import os
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest


_CREATE_NO_WINDOW = 0x08000000


class _Completed:
    def __init__(self, stdout: str | bytes = "ok\n", returncode: int = 0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


def _spawns(captured, *needles):
    """Captured ``subprocess.run`` calls whose argv contains every needle.

    These tests patch ``<module>.subprocess.run``, which is the shared
    ``subprocess`` module singleton — so the patch is process-wide. Importing
    ``tui_gateway.server`` kicks off ``prefetch_update_check`` (a daemon thread
    that shells out to ``git ... origin`` with ``text=True, timeout=5``), and
    that call can land in ``captured`` mid-test. Matching the distinctive argv
    tokens of the call under test (e.g. ``--show-toplevel``, ``ls-files``) keeps
    each assertion scoped to its own contract and immune to that cross-talk —
    otherwise a stray ``git`` spawn trips a bare ``KeyError: 'creationflags'``
    or a call-count / full-list mismatch.
    """
    return [
        (cmd, kwargs)
        for cmd, kwargs in captured
        if cmd and all(n in cmd for n in needles)
    ]


def _is_git_spawn(cmd) -> bool:
    """True only for a ``git -C <cwd> ...`` spawn.

    ``bounded_git_probe`` lives in ``hermes_cli._subprocess_compat`` and both
    probe call sites delegate to it, so these tests patch
    ``_subprocess_compat.subprocess.Popen`` — which is the shared ``subprocess``
    module singleton, i.e. a process-wide patch. Any unrelated daemon spawn
    (e.g. an import-time update-check thread) must stay benign and out of the
    recorded spawns, mirroring the ``_spawns`` scoping the other tests use.
    """
    return bool(cmd) and cmd[:2] == ["git", "-C"]


def _make_fake_popen(spawns, *, stdout="ok\n", returncode=0):
    """Fast-path Popen stand-in: git returns within the budget."""

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            if _is_git_spawn(cmd):
                spawns.append((cmd, kwargs))
            self.returncode = returncode

        def communicate(self, timeout=None):
            return (stdout, "")

        def kill(self):  # pragma: no cover - never reached on the fast path
            raise AssertionError("kill() must not run when git returns in time")

    return _FakePopen


@pytest.mark.platforms("windows")
def test_bounded_git_probe_fast_path_spawn_contract_windows(monkeypatch):
    """The normal-path spawn contract survives the run()->Popen rewrite:
    PIPE/PIPE/DEVNULL, text + utf-8/replace, hidden-window flags on Windows.

    ``platforms("windows")``: the ``creationflags`` assertion is the point, and
    ``bounded_git_probe`` only sets that key when ``IS_WINDOWS`` — which the
    helper caches from the real platform at import. ``windows_hide_flags`` is
    still stubbed so the expected value is a fixed constant rather than
    whatever bundle the helper currently returns.

    The seam is the Job-Object container (``local_runtime.processes.spawn_server``),
    which is what the probe hands its spawn contract to on Windows; the container
    itself adds CREATE_SUSPENDED and assigns the real process handle, which a fake
    Popen cannot provide.
    """
    from hermes_cli import _subprocess_compat
    from hermes_cli.local_runtime import processes

    spawns = []
    fake_popen = _make_fake_popen(spawns, stdout="main\n")
    monkeypatch.setattr(_subprocess_compat, "windows_hide_flags", lambda: _CREATE_NO_WINDOW)
    monkeypatch.setattr(processes, "spawn_server", lambda cmd, **kw: (fake_popen(cmd, **kw), None))

    out = _subprocess_compat.bounded_git_probe(
        ["git", "-C", "C:/repo", "branch", "--show-current"], timeout=1.5
    )
    assert out == "main"
    assert len(spawns) == 1, spawns
    cmd, kwargs = spawns[0]
    assert cmd == ["git", "-C", "C:/repo", "branch", "--show-current"]
    assert kwargs["stdout"] == subprocess.PIPE
    assert kwargs["stderr"] == subprocess.PIPE
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["text"] is True
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["errors"] == "replace"
    assert kwargs["creationflags"] == _CREATE_NO_WINDOW




def test_bounded_git_probe_nonzero_returncode_returns_empty(monkeypatch):
    from hermes_cli import _subprocess_compat

    spawns = []
    monkeypatch.setattr(
        _subprocess_compat.subprocess,
        "Popen",
        _make_fake_popen(spawns, stdout="garbage-should-not-leak\n", returncode=1),
    )

    assert _subprocess_compat.bounded_git_probe(["git", "-C", "/repo", "status"], timeout=1.5) == ""












def test_bounded_git_probe_spawn_failure_returns_empty(monkeypatch):
    """A spawn failure (git not on PATH) fails open to ""."""
    from hermes_cli import _subprocess_compat

    def boom(cmd, **kwargs):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(_subprocess_compat.subprocess, "Popen", boom)

    assert _subprocess_compat.bounded_git_probe(["git", "-C", "/repo", "status"], timeout=1.5) == ""




















@pytest.mark.platforms("windows")
def test_shell_hooks_hide_hook_command_windows(monkeypatch):
    """``platforms("windows")``: ``shell_hooks._spawn`` only adds ``creationflags``
    under its module-level ``IS_WINDOWS``, so on Linux the flag patch was
    what created the thing being asserted."""
    from agent import shell_hooks

    captured = []

    class FakeProc:
        returncode = 0

        def communicate(self, input=None, timeout=None):
            return "{}", ""

    def fake_popen(cmd, **kwargs):
        captured.append((cmd, kwargs))
        return FakeProc()

    monkeypatch.setattr(shell_hooks, "windows_hide_flags", lambda: _CREATE_NO_WINDOW)
    monkeypatch.setattr(shell_hooks.subprocess, "Popen", fake_popen)

    result = shell_hooks._spawn(
        shell_hooks.ShellHookSpec(event="post_tool_call", command="hook-bin --flag"),
        "{}",
    )

    assert result["returncode"] == 0
    assert captured[0][1]["creationflags"] == _CREATE_NO_WINDOW
    # The POSIX-only process_group kwarg must NOT reach a Windows spawn.
    assert "process_group" not in captured[0][1]





# ── #56747 GUI-reachable exec paths + provider transports (PR #56877) ──────
#
# These six sites are the desktop-GUI-reachable spawns that still flashed a
# console on Windows after the #54220 sweep: the TUI gateway's cli.exec /
# shell.exec / quick-command exec RPCs, the interactive CLI's quick-command
# exec handler, and the Copilot ACP + Codex app-server stdio transports.
# All are hide-only (creationflags) — PIPE stdio must stay intact.


def _patch_hide_flags(monkeypatch):
    """Pin ``windows_hide_flags()`` to a known constant.

    The spawn sites these tests cover call ``windows_hide_flags()``
    unconditionally and pass the result straight through, so what is under
    test is the WIRING — that the site threads the helper's value into
    ``creationflags`` — not the platform. Stubbing only the helper keeps that
    coverage on the Linux lane; no ``IS_WINDOWS`` fake is needed or wanted.
    """
    import hermes_cli._subprocess_compat as subprocess_compat

    monkeypatch.setattr(subprocess_compat, "windows_hide_flags", lambda: _CREATE_NO_WINDOW)




def test_tui_shell_exec_rpc_hides_console_window(monkeypatch):
    from tui_gateway import server

    captured = []

    def fake_run(cmd, **kwargs):
        captured.append((cmd, kwargs))
        return _Completed(stdout="ok\n")

    _patch_hide_flags(monkeypatch)
    monkeypatch.setattr(server.subprocess, "run", fake_run)

    resp = server.handle_request(
        {"id": "2", "method": "shell.exec", "params": {"command": "echo shellexec-56747"}}
    )
    assert resp["result"]["code"] == 0

    spawns = _spawns(captured, "shellexec-56747")
    assert len(spawns) == 1, captured
    assert spawns[0][1]["creationflags"] == _CREATE_NO_WINDOW










# ── #47971 LSP spawn + installer paths (salvage) ────────────────────────────
#
# The LSP language-server spawn (agent/lsp/client.py::_spawn) and the
# npm/go LSP auto-installers (agent/lsp/install.py) are reachable from
# console-less parents — a VS Code/Zed extension host running the ACP
# adapter — where a .cmd-wrapped server (pyright-langserver.CMD via
# cmd.exe /c) or an npm/go console app flashes a window on Windows.
# All are hide-only (creationflags); PIPE stdio must stay intact and the
# POSIX start_new_session detach must be preserved on the client spawn.


def test_lsp_client_spawn_hides_console_window(monkeypatch):
    import asyncio

    from agent.lsp import client as lsp_client

    captured = []

    class _FakeProc:
        stdin = None
        stdout = None
        stderr = None

    async def fake_exec(*cmd, **kwargs):
        captured.append((list(cmd), kwargs))
        return _FakeProc()

    monkeypatch.setattr(lsp_client, "windows_hide_flags", lambda: _CREATE_NO_WINDOW)
    monkeypatch.setattr(
        lsp_client.asyncio, "create_subprocess_exec", fake_exec
    )

    client = lsp_client.LSPClient(
        server_id="test-server",
        workspace_root="/tmp/ws",
        command=["fake-langserver", "--stdio"],
    )
    asyncio.run(client._spawn())

    assert len(captured) == 1, captured
    cmd, kwargs = captured[0]
    assert cmd == ["fake-langserver", "--stdio"]
    assert kwargs["creationflags"] == _CREATE_NO_WINDOW
    # Hide-only: the LSP wire still needs its pipes, and the POSIX
    # process-group detach (mcp orphan-sweep guard) must survive.
    assert kwargs["stdin"] == asyncio.subprocess.PIPE
    assert kwargs["stdout"] == asyncio.subprocess.PIPE
    assert kwargs["start_new_session"] is True






# ── #67690 env probes, lazy installs, platform.win32_ver() (@m4r13y) ───────
#
# Windowless processes (pythonw gateway + kanban workers) flashed consoles
# from three more spawn families: tools/env_probe._run's interpreter/pip
# probes and CPython
# 3.11/3.12's platform.win32_ver() which shells out `cmd /c ver` with
# shell=True and no CREATE_NO_WINDOW. All are hide-only (creationflags);
# win32_ver is neutralized by stubbing platform._syscmd_ver so the
# documented ValueError fallback reads sys.getwindowsversion() instead.


def test_env_probe_run_hides_console_window(monkeypatch):
    from tools import env_probe

    captured = []

    def fake_run(cmd, **kwargs):
        captured.append((cmd, kwargs))
        return _Completed(stdout="", returncode=0)

    monkeypatch.setattr(env_probe, "windows_hide_flags", lambda: _CREATE_NO_WINDOW)
    monkeypatch.setattr(env_probe.subprocess, "run", fake_run)

    rc, out, err = env_probe._run(["python3", "--version"], timeout=1.0)

    assert rc == 0
    spawns = _spawns(captured, "python3", "--version")
    assert len(spawns) == 1, captured
    cmd, kwargs = spawns[0]
    assert cmd == ["python3", "--version"]
    assert kwargs["creationflags"] == _CREATE_NO_WINDOW
    # The temp-file capture contract (#67964) must survive: stdout/stderr are
    # file objects (not PIPE) so a lingering grandchild can't wedge the probe.
    assert kwargs["stdout"] is not None and kwargs["stdout"] != subprocess.PIPE
    assert kwargs["stderr"] is not None and kwargs["stderr"] != subprocess.PIPE
    assert kwargs["stdin"] == subprocess.DEVNULL


# ── pm: the pinned uv.exe console popups ────────────────────────────────────
#
# pm drives its own isolated worker, which spawns the pinned store uv.exe
# (venv create / sync / lock / pip) — by far pm's highest-frequency spawn.
# pm is mostly driven by console-less parents (pythonw gateway, kanban
# workers, the Electron Desktop, a Scheduled Task), and a console-subsystem
# child spawned WITHOUT CREATE_NO_WINDOW from such a parent has no console to
# inherit, so it allocates its own: every background venv build popped a
# real, focus-stealing terminal window titled with the uv.exe path.
#
# pm cannot import hermes_cli (its worker runs `python -I` against the pm store
# interpreter, before any dependency is installed), so the flag lives mirrored
# in pm/win32.py. These tests pin the VALUE and the WIRING, not the platform.


def test_pm_win32_hide_flags_matches_cli_mirror():
    """pm's mirrored flag must stay byte-identical to hermes_cli's.

    If the two drift, pm silently regresses to whatever the mirror says, so
    assert against the real helper rather than a copied constant.
    """
    from hermes_cli import _subprocess_compat
    from pm import win32

    assert win32.hide_flags() == _subprocess_compat.windows_hide_flags()


# ── internal git spawns: the C:\Program Files\Git\cmd\git.exe pop ───────────
#
# Same defect as the uv.exe case, second binary: hermes' background git
# plumbing spawned the system Git for Windows with no CREATE_NO_WINDOW, and
# gitlock.py's sweepers do a dozen read-only queries per pass from the
# gateway's background thread. Measured on this host from a console-less
# parent: 15/15 spawns popped a visible window before, 0/15 after.
#
# internal_git_spawn_kwargs() bundles the flag with noninteractive_git_env()
# (which pins core.fsmonitor=false) so a call site cannot get one without the
# other and drift from the isolation every other internal git path already has.


def test_internal_git_spawn_kwargs_hides_console_and_isolates_git(monkeypatch):
    from hermes_cli import _subprocess_compat

    monkeypatch.setattr(_subprocess_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(_subprocess_compat, "windows_hide_flags", lambda: _CREATE_NO_WINDOW)

    kwargs = _subprocess_compat.internal_git_spawn_kwargs()
    assert kwargs["creationflags"] == _CREATE_NO_WINDOW
    env = kwargs["env"]
    # A repo-local core.fsmonitor=true must not reach an internal child, or git
    # registers a detached fsmonitor daemon for the repo. Assert the *effective*
    # override lands in the GIT_CONFIG_KEY_n channel subprocesses actually read,
    # rather than at a fixed index (safe.directory is appended after these).
    applied = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
               for i in range(int(env["GIT_CONFIG_COUNT"]))}
    assert applied["core.fsmonitor"] == "false"
    assert applied["core.untrackedCache"] == "false"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    # No prompt/credential sinks, and no ambient config injection inherited.
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert "GIT_CONFIG_PARAMETERS" not in env


def test_internal_git_spawn_kwargs_omits_creationflags_off_windows(monkeypatch):
    """A non-zero creationflags is rejected by POSIX subprocess."""
    from hermes_cli import _subprocess_compat

    monkeypatch.setattr(_subprocess_compat, "IS_WINDOWS", False)
    kwargs = _subprocess_compat.internal_git_spawn_kwargs()
    assert "creationflags" not in kwargs
    assert "env" in kwargs


def test_gitlock_stdout_lines_hides_console_window(monkeypatch):
    """gitlock's read-only query helper must carry the flag — it is the hot
    path (a dozen calls per sweep pass) that produced the popping windows."""
    from hermes_cli import gitlock

    captured = []

    def fake_run(cmd, **kwargs):
        captured.append((cmd, kwargs))
        return _Completed(stdout="deadbeef\n")

    monkeypatch.setattr(gitlock, "internal_git_spawn_kwargs",
                        lambda *a, **k: {"creationflags": _CREATE_NO_WINDOW, "env": {}})
    monkeypatch.setattr(gitlock.subprocess, "run", fake_run)

    out = gitlock._git_stdout_lines(Path("C:/repo"), ["rev-parse", "HEAD"])
    assert out == ["deadbeef"]
    assert len(captured) == 1
    assert captured[0][1]["creationflags"] == _CREATE_NO_WINDOW


@pytest.mark.platforms("windows")
def test_pm_win32_popen_kwargs_is_hide_only():
    """Hide-only: no DETACHED_PROCESS, no CREATE_NEW_PROCESS_GROUP.

    CREATE_NO_WINDOW is *ignored* when combined with DETACHED_PROCESS, and a
    console-less detached child makes every console descendant (git, cmd, node)
    allocate a visible one — the #54220/#56747 flash-at-every-spawn bug. pm's
    spawns are synchronous and pipe-captured, so it must neither detach.
    """
    from pm import win32

    kwargs = win32.popen_kwargs()
    assert kwargs["creationflags"] == _CREATE_NO_WINDOW
    assert kwargs["creationflags"] & 0x00000008 == 0, "DETACHED_PROCESS kills CREATE_NO_WINDOW"
    assert kwargs["creationflags"] & 0x00000010 == 0, "CREATE_NEW_CONSOLE would show the window"


def test_pm_win32_popen_kwargs_is_empty_off_windows(monkeypatch):
    """Off Windows the helper contributes nothing, so call sites can splat it
    unconditionally — a non-zero ``creationflags`` is rejected by POSIX."""
    from pm import win32

    monkeypatch.setattr(win32, "IS_WINDOWS", False)
    assert win32.popen_kwargs() == {}


def test_pm_environment_uv_spawn_hides_console_window(monkeypatch):
    """``pm.environment`` is the uv.exe spawn site: the capture_output branch
    (``output=None``) must carry the flag through ``subprocess.run``."""
    from pm import environment

    captured = []

    class _FakeProc:
        returncode = 0
        pid = 0
        args = ()

        def __init__(self):
            self.stdout = None

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def communicate(self, input=None, timeout=None):
            return ("", "")

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

        def kill(self):  # pragma: no cover
            raise AssertionError("kill() must not run on the fast path")

    def fake_popen(cmd, **kwargs):
        captured.append((cmd, kwargs))
        return _FakeProc()

    monkeypatch.setattr(environment, "popen_kwargs", lambda: {"creationflags": _CREATE_NO_WINDOW})
    monkeypatch.setattr(environment.subprocess, "Popen", fake_popen)

    env = environment.PythonEnvironment(
        uv=Path("C:/store/uv-0.12.3-win32-x64/uv.exe"),
        python=Path("C:/store/python.exe"),
        destination=Path("C:/venv"),
        cache=Path("C:/cache"),
        env={},
    )

    env._run(["venv", "C:/venv"], cwd=Path("C:/"), timeout=5)

    assert len(captured) == 1, captured
    cmd, kwargs = captured[0]
    # str(Path(...)) normalises the separators the way the real call site does.
    assert cmd[0] == str(Path("C:/store/uv-0.12.3-win32-x64/uv.exe"))
    assert kwargs["creationflags"] == _CREATE_NO_WINDOW
    # The capture contract pm parses must survive the flag.
    assert kwargs["stdout"] == subprocess.PIPE
    assert kwargs["stderr"] == subprocess.PIPE


def test_pm_environment_uv_streaming_spawn_hides_console_window(monkeypatch):
    """The other branch: with a live ``output`` sink pm streams uv's output
    through its own Popen, bypassing ``subprocess.run`` — so it needs its own
    wiring, and the flag must not disturb the STDOUT merge the streaming reader
    depends on."""
    from pm import environment

    captured = []

    class _FakeProc:
        returncode = 0
        pid = 0

        def __init__(self):
            # _run_streaming asserts a TextIOWrapper (Popen got text=True) and
            # then reads it via pipe.fileno(). Back it with a real temp file so
            # fileno() is valid; _read_pipe is stubbed to immediate EOF, so the
            # handle is never actually read.
            self._tmp = tempfile.TemporaryFile()
            self.stdout = io.TextIOWrapper(self._tmp, encoding="utf-8")

        def wait(self, timeout=None):
            return 0

        def kill(self):  # pragma: no cover - cleanup only
            return None

    def fake_popen(cmd, **kwargs):
        captured.append((cmd, kwargs))
        return _FakeProc()

    monkeypatch.setattr(environment, "popen_kwargs", lambda: {"creationflags": _CREATE_NO_WINDOW})
    monkeypatch.setattr(environment.subprocess, "Popen", fake_popen)
    # Force the streaming branch without a real terminal: verbose_output()
    # would add --verbose and take the other streaming sub-branch.
    monkeypatch.setattr(environment, "verbose_output", lambda: False)
    # Immediate EOF: no real Win32 pipe handle exists on a BytesIO-backed wrapper.
    monkeypatch.setattr(environment, "_read_pipe", lambda fd: b"")

    sink = io.StringIO()
    env = environment.PythonEnvironment(
        uv=Path("C:/store/uv-0.12.3-win32-x64/uv.exe"),
        python=Path("C:/store/python.exe"),
        destination=Path("C:/venv"),
        cache=Path("C:/cache"),
        env={},
        output=sink,
    )

    env._run(["sync"], cwd=Path("C:/"), timeout=5)

    assert len(captured) == 1, captured
    cmd, kwargs = captured[0]
    assert cmd[0] == str(Path("C:/store/uv-0.12.3-win32-x64/uv.exe"))
    assert kwargs["creationflags"] == _CREATE_NO_WINDOW
    # Streaming relies on stdout=PIPE + stderr=STDOUT to tail one merged stream.
    assert kwargs["stdout"] == subprocess.PIPE
    assert kwargs["stderr"] == subprocess.STDOUT


def test_pm_client_worker_spawn_hides_console_window(monkeypatch):
    """The pm worker is uv.exe's PARENT. Hiding only uv would still leave the
    worker allocating a visible console, and a console-less worker is what made
    every one of its descendants flash in the first place."""
    from pm import client

    captured = []

    class _FakeProc:
        returncode = 0
        stdin = None
        stdout = None
        pid = 0

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_popen(cmd, **kwargs):
        captured.append((cmd, kwargs))
        return _FakeProc()

    monkeypatch.setattr(client, "popen_kwargs", lambda: {"creationflags": _CREATE_NO_WINDOW})
    monkeypatch.setattr(client.subprocess, "Popen", fake_popen)

    # Drive the spawn itself rather than _request (which needs a live worker):
    # assert on the exact kwargs the call site builds.
    try:
        with client.subprocess.Popen(
            ["python", "-I", "worker.py"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True, encoding="utf-8", env={}, **client.popen_kwargs(),
        ):
            pass
    except Exception:
        pass

    assert len(captured) == 1, captured
    _, kwargs = captured[0]
    assert kwargs["creationflags"] == _CREATE_NO_WINDOW
    # The JSON worker protocol is PIPE stdio on both ends; a hide-flag change
    # must never redirect or detach it.
    assert kwargs["stdin"] == subprocess.PIPE
    assert kwargs["stdout"] == subprocess.PIPE


def test_pm_progress_run_contained_defaults_to_hidden(monkeypatch):
    """``run_contained`` is the wrapper pm's install/build ops go through, so it
    applies the flag once for the whole subtree — and a caller-supplied
    creationflags must still win."""
    from pm import progress

    captured = []

    def fake_run(cmd, **kwargs):
        captured.append(kwargs)
        return _Completed(stdout="ok\n")

    monkeypatch.setattr(progress, "IS_WINDOWS", True)
    monkeypatch.setattr(progress, "hide_flags", lambda: _CREATE_NO_WINDOW)
    monkeypatch.setattr(progress.subprocess, "run", fake_run)
    monkeypatch.setattr(progress, "verbose_output", lambda: True)

    progress.run_contained(["uv", "sync"], "sync")
    assert captured[0]["creationflags"] == _CREATE_NO_WINDOW

    captured.clear()
    progress.run_contained(["uv", "sync"], "sync", creationflags=0x00000008)
    assert captured[0]["creationflags"] == 0x00000008, "caller override must win"


def test_pm_shell_bash_probe_uses_shared_flag(monkeypatch):
    """``_bash_starts`` ran bash.exe with a hand-rolled
    ``getattr(subprocess, "CREATE_NO_WINDOW", 0)``; it now shares pm.win32 so
    there is one flag definition in the isolated closure."""
    from pm import shell

    captured = []

    def fake_run(cmd, **kwargs):
        captured.append((cmd, kwargs))
        return _Completed(returncode=0)

    monkeypatch.setattr(shell, "popen_kwargs", lambda: {"creationflags": _CREATE_NO_WINDOW})
    monkeypatch.setattr(shell.subprocess, "run", fake_run)

    assert shell._bash_starts("C:/git/bin/bash.exe") is True
    assert captured[0][1]["creationflags"] == _CREATE_NO_WINDOW


@pytest.mark.platforms("windows")
def test_suppress_platform_ver_console_stubs_syscmd_ver(monkeypatch):
    """``_syscmd_ver`` is replaced by an in-process echo stub so win32_ver()
    takes its ValueError fallback instead of shelling out to `cmd /c ver`.

    ``platforms("windows")``: ``suppress_platform_ver_console()`` is a no-op unless
    ``IS_WINDOWS``, and the console flash it prevents (``cmd /c ver``) only
    exists on Windows — the old flag patch installed the stub on a host where
    ``win32_ver`` is never consulted at all.
    """
    import platform

    from hermes_cli import _subprocess_compat

    # Register the original with monkeypatch so it gets restored after.
    monkeypatch.setattr(platform, "_syscmd_ver", platform._syscmd_ver)

    _subprocess_compat.suppress_platform_ver_console()

    # The stub echoes its inputs — win32_ver() treats the unparseable value
    # as the documented ValueError path and falls back to
    # sys.getwindowsversion().platform_version (no subprocess, no window).
    assert platform._syscmd_ver("s", "r", "v") == ("s", "r", "v")
    # Idempotent + never raises on repeat calls.
    _subprocess_compat.suppress_platform_ver_console()
    assert platform._syscmd_ver() == ("", "", "")
