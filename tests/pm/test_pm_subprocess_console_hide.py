"""PM's own subprocesses must never open a console window on Windows.

Regression: every PM child spawned without ``CREATE_NO_WINDOW`` allocates and
shows its own console on Windows. The PM worker made this chronic -- it is
``<installs>/<id>/pm-runtime/generations/<hash>/Scripts/python.exe``, spawned once
per PM operation, so routine background activity kept popping visible terminals
at the user.

Asserts the *contract* (every PM spawn path carries the no-window bit), not a
snapshot of which files were patched: a new spawn site without the helper fails.
"""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

import pytest

from pm._subprocess_windows import IS_WINDOWS, hidden_spawn_kwargs

PM_DIR = Path(__file__).resolve().parents[2] / "pm"

CREATE_NO_WINDOW = 0x08000000

# ``run_cli`` re-execs the PM CLI for ``hermes pm`` and deliberately inherits the
# caller's stdio -- it is the interactive surface, not a background child.
_INTERACTIVE_EXEMPT = {"run_cli"}


def _spawn_sites() -> list[tuple[Path, int, ast.Call, str | None]]:
    """Every ``subprocess.run/Popen(...)`` call in pm/, with its enclosing func."""
    sites = []
    for path in sorted(PM_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        funcs = [
            node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (
                isinstance(func, ast.Attribute)
                and func.attr in {"run", "Popen"}
                and isinstance(func.value, ast.Name)
                and func.value.id == "subprocess"
            ):
                continue
            owner = next(
                (f for f in funcs if f.lineno <= node.lineno <= (f.end_lineno or f.lineno)),
                None,
            )
            sites.append((path, node.lineno, node, owner.name if owner else None))
    return sites


def test_helper_is_a_noop_off_windows():
    # Off Windows the helper must contribute nothing, so callers can splat it
    # unconditionally without a platform branch at every call site.
    if not IS_WINDOWS:
        assert hidden_spawn_kwargs() == {}


@pytest.mark.skipif(not IS_WINDOWS, reason="CREATE_NO_WINDOW is a Win32 creation flag")
def test_helper_sets_create_no_window():
    assert hidden_spawn_kwargs() == {"creationflags": CREATE_NO_WINDOW}


def test_every_pm_spawn_site_hides_its_console():
    """No PM child may spawn with a visible console on Windows."""
    offenders = []
    for path, lineno, _call, owner in _spawn_sites():
        if owner in _INTERACTIVE_EXEMPT:
            continue
        # Scan the whole enclosing function, not a line window: a shared wrapper
        # (Runner.run) applies the flag via setdefault well above its spawn line.
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        fn = next(
            (
                n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.lineno <= lineno <= (n.end_lineno or n.lineno)
            ),
            None,
        )
        window = ast.unparse(fn) if fn is not None else ""
        if "hidden_spawn_kwargs" not in window and "CREATE_NO_WINDOW" not in window:
            offenders.append(f"{path.name}:{lineno} ({owner})")
    assert not offenders, (
        "PM subprocess spawn sites missing console suppression: "
        + ", ".join(sorted(offenders))
        + " — add **hidden_spawn_kwargs() from pm._subprocess_windows"
    )


@pytest.mark.skipif(not IS_WINDOWS, reason="asserts a Win32 creation flag")
def test_helper_does_not_add_detach_flags():
    """CREATE_NO_WINDOW must stay a lone flag.

    MSDN ignores it when combined with CREATE_NEW_CONSOLE or DETACHED_PROCESS,
    and a console-less detached child re-creates the per-descendant console
    flash this codebase already fixed (#54220 / #56747).
    """
    flags = hidden_spawn_kwargs()["creationflags"]
    for forbidden in (0x00000008, 0x00000010):  # DETACHED_PROCESS, CREATE_NEW_CONSOLE
        assert not flags & forbidden
