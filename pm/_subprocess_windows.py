"""Console-free subprocess spawn kwargs for PM's own children.

Every PM child is an implementation detail of an install/resolve operation: the
caller reads a JSON reply on a pipe (or captured output), and nobody is ever
going to type into the console. On Windows, a console-subsystem child spawned
without ``CREATE_NO_WINDOW`` allocates and shows its own console window — the
user sees a terminal flash/pop per spawn. The PM worker is the worst offender:
it is ``<installs>/<id>/pm-runtime/generations/<hash>/Scripts/python.exe``, spawned
once per PM operation, so routine activity kept opening visible terminals.

``windows_hide_flags()`` (CREATE_NO_WINDOW, no DETACHED_PROCESS) is the right
bit here for two reasons, both documented at length in
``hermes_cli/_subprocess_compat.py``:

* MSDN: ``CREATE_NO_WINDOW`` is *ignored* when combined with
  ``CREATE_NEW_CONSOLE`` or ``DETACHED_PROCESS`` — so the bundle must stay a
  lone no-window flag, or it is dead.
* A no-window child OWNS a hidden console that its descendants (git, cmd, node)
  inherit, so they don't each flash their own. A truly console-less
  ``DETACHED_PROCESS`` child re-creates the per-descendant flash bug this
  codebase already fixed (#54220 / #56747).

These children are not daemons: they are short-lived and we hold their pipes,
so the detach/breakaway half of ``windows_detach_flags()`` would be wrong here
(it would outlive the request and escape the install lock's job object).
"""

from __future__ import annotations

import sys

__all__ = ["IS_WINDOWS", "hidden_spawn_kwargs"]

IS_WINDOWS = sys.platform == "win32"

# CREATE_NO_WINDOW. Defined locally (not imported from hermes_cli) so pm/ stays
# importable during boot before hermes_cli is on the path; the value is a stable
# Win32 constant, and hermes_cli._subprocess_compat defines the identical one.
_CREATE_NO_WINDOW = 0x08000000


def hidden_spawn_kwargs() -> dict:
    """``Popen``/``run`` kwargs that suppress a console window on Windows.

    Empty on non-Windows, so callers can splat it unconditionally. Yields only
    when the caller supplied no ``creationflags`` of its own: Windows creation
    flags are one integer, so a caller that chose deliberately keeps its choice
    rather than having ours silently OR'd in.
    """
    return {"creationflags": _CREATE_NO_WINDOW} if IS_WINDOWS else {}
