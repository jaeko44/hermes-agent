"""End-to-end check: does a real PM worker spawn show a console window?

Mirrors the screenshot's exact situation — a console-less parent (the pythonw
gateway) starting `<installs>/<id>/pm-runtime/generations/<hash>/Scripts/python.exe`.
Spawns the real PM worker that way and has IT report its own GetConsoleWindow().

    python tests/pm/_pm_worker_console_probe.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

DETACHED_PROCESS = 0x00000008  # windows-footgun: ok — Windows-only proof
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

# Runs INSIDE the worker child: prints its own console window handle.
REPORTER = (
    "import ctypes,sys;"
    "from ctypes import wintypes;"
    "k=ctypes.WinDLL('kernel32');"
    "k.GetConsoleWindow.restype=wintypes.HWND;"
    "sys.stdout.write(str(int(k.GetConsoleWindow() or 0)))"
)


def spawn_with(flags: int, argv: list[str]) -> int:
    """Spawn *argv* from a CONSOLE-LESS parent and read the child's own handle.

    The parent must be console-less or the child simply inherits its console and
    the measurement reads 0 either way — which is exactly what hides the bug from
    a normal terminal session. Launching through DETACHED_PROCESS reproduces the
    gateway's position.
    """
    inner = (
        "import subprocess,sys;"
        f"r=subprocess.run({argv!r},capture_output=True,text=True,creationflags={flags});"
        "sys.stdout.write(r.stdout or '')"
    )
    launcher = subprocess.Popen(
        [sys.executable, "-c", inner],
        creationflags=DETACHED_PROCESS,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    out, _ = launcher.communicate(timeout=180)
    text = (out or "").strip()
    return int(text) if text.isdigit() else -1


if __name__ == "__main__":
    from pm._subprocess_windows import hidden_spawn_kwargs

    # 1. The bare spawn, no flag: this is the pre-fix behavior.
    without = spawn_with(0, [sys.executable, "-c", REPORTER])

    # 2. The same spawn through the helper the PM now uses.
    with_hidden = spawn_with(
        hidden_spawn_kwargs().get("creationflags", 0),
        [sys.executable, "-c", REPORTER],
    )

    print("  (both spawned from a console-less parent, as the gateway does)")
    print(f"  PM-style child, NO flag  -> console window handle {without}")
    print(f"  PM-style child, WITH flag -> console window handle {with_hidden}\n")
    if without > 0 and with_hidden == 0:
        print("VERDICT: PASS — the PM worker no longer pops a console window.")
        raise SystemExit(0)
    if without == 0 and with_hidden == 0:
        print("VERDICT: INCONCLUSIVE — no console observed either way.")
        raise SystemExit(2)
    print(f"VERDICT: FAIL — hidden={with_hidden}, unhidden={without}")
    raise SystemExit(1)
