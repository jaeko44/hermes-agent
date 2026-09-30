"""Windows console-suppression for pm's subprocess spawns.

pm runs inside a deliberately isolated import closure: the worker is launched
with ``python -I -B`` against the pm store interpreter, so ``hermes_cli`` is not
importable here (and importing it would drag ruamel/SDK deps into a bootstrap
that must work before any dependency is installed). The Windows console-flash
helpers therefore live here as a mirror of
``hermes_cli._subprocess_compat.windows_hide_flags`` — same value, same
contract, no cross-package import. Double application is harmless.

Why this module exists
----------------------
Hermes has a long tail of console-flash fixes, all the same bug: a
console-subsystem child spawned WITHOUT ``CREATE_NO_WINDOW`` from a
*console-less* parent (``pythonw.exe`` gateway, kanban workers, the Electron
Desktop, the Tauri bootstrap installer, a Scheduled Task) is a Win32 console
process with no console to inherit, so it allocates its OWN — and the default
terminal application turns that into a real, visible window that steals focus
and pops in front of the operator. The same spawn from a parent that *has* a
console is silent, which is why this only shows up in the background/service
paths and never in interactive `hermes ...` runs.

That is precisely the pm shape: ``pm`` is mostly driven by the windowless
gateway/worker, and its highest-frequency spawn is the pinned ``uv.exe``
(venv create/sync/lock/pip) at ``pm/environment.py``. Each of those allocated a
fresh visible console per invocation, so every background venv build flashed a
terminal window titled with the uv.exe path.

Why ``CREATE_NO_WINDOW`` and not ``DETACHED_PROCESS``
----------------------------------------------------
``DETACHED_PROCESS`` (0x8) is deliberately NOT used, and must not be re-added
(the recurring bug #54220 / #56747):

1. MSDN: ``CREATE_NO_WINDOW`` "is ignored if used with either
   ``CREATE_NEW_CONSOLE`` or ``DETACHED_PROCESS``" — so combining them makes
   the no-window bit dead and the window comes back.
2. A ``DETACHED_PROCESS`` child has NO console at all, so every
   console-subsystem DESCENDANT (git, cmd, node, powershell, …) then allocates
   its own — one visible flash per spawn, including inside third-party code no
   per-site sweep can reach. A ``CREATE_NO_WINDOW`` child instead owns a HIDDEN
   console that all descendants inherit, so the whole subtree stays invisible.

The tradeoff is deliberate and worth stating: the hidden console is a real
console, so a child that writes to fd 1/2 with no pipe redirect still has
somewhere to go, and the child is NOT detached from the parent's lifetime
(Ctrl+C and job teardown still propagate). That is the right trade for pm:
these are synchronous, pipe-captured, caller-waits calls, not fire-and-forget
daemons. Callers that genuinely need a detached background process must use
``hermes_cli._subprocess_compat.windows_detach_flags()`` instead.
"""

from __future__ import annotations

import subprocess
import sys

__all__ = ["IS_WINDOWS", "hide_flags", "popen_kwargs"]

IS_WINDOWS = sys.platform == "win32"

# Win32 CreationFlags, defined as a literal because CREATE_NO_WINDOW is not
# guaranteed to be exposed by the stdlib subprocess module on older Pythons
# (mirroring hermes_cli._subprocess_compat._CREATE_NO_WINDOW).
_CREATE_NO_WINDOW = 0x08000000


def hide_flags() -> int:
    """Win32 creationflags that hide the child's console without detaching it.

    0 on non-Windows. ``subprocess.run``/``Popen`` reject a non-zero
    ``creationflags`` on POSIX, so this must only be threaded through
    :func:`popen_kwargs` (or under an ``IS_WINDOWS`` guard), never passed raw.
    """
    return _CREATE_NO_WINDOW if IS_WINDOWS else 0


def popen_kwargs() -> dict:
    """Spawn kwargs for a console-suppressed child; empty dict off Windows.

    The portable form of the flag, so call sites can splat it unconditionally
    into ``subprocess.run``/``Popen`` instead of branching on platform::

        subprocess.run(cmd, **popen_kwargs(), capture_output=True, ...)

    Hide-only, never detach: see the module docstring. Pair it with
    ``stdin=subprocess.DEVNULL`` for probes so nothing can block on an
    inherited handle, and keep ``capture_output``/PIPE stdio intact — pm
    parses this output to build venvs.
    """
    return {"creationflags": hide_flags()} if IS_WINDOWS else {}
