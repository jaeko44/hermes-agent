"""Pre-venv entry point for PM's dependency transaction.

Stdlib-only at import: installers call this before dependencies exist.
All checkout roots use PM's selected generation and facts; sealed payloads
remain build-owned. ``--check`` is passive and never provisions tools.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

from hermes_cli.steward import UPDATE_MECHANISMS


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _is_sealed(project_root: Path) -> bool:
    """A sealed tree ships its interpreter; only checkouts own a venv.

    The stamp file is the authority (shared with hermes_cli.steward).
    A tree with BOTH a stamp and .git is a dev tree — treat as checkout.

    A stamp without a valid ``updateMechanism`` is a build-lane bug and
    must not be silently read as "not sealed" (that is exactly the
    misclassification that made sealed trees look updatable) — same
    guard as hermes_cli.version_info._stamp_version_info.
    """
    if (project_root / ".git").exists():
        return False
    from hermes_cli.steward import read_install_stamp
    from pm.paths import install_stamp_path
    stamp_path = install_stamp_path(project_root)
    data = read_install_stamp(project_root)
    if not data:
        return False
    if data.get("updateMechanism") not in UPDATE_MECHANISMS:
        raise RuntimeError(
            f"install-stamp.json at {stamp_path.parent} is missing a valid "
            f"'updateMechanism' (one of {', '.join(UPDATE_MECHANISMS)}). The "
            "build lane that wrote this stamp must pass --update-mechanism to "
            "scripts/write_install_stamp.py."
        )
    return True


def check_runtime(project_root: Path) -> str | None:
    """One passive startup verdict; callers only choose stderr or logging."""
    import pm
    from hermes_cli.steward import read_install_stamp, sealed_steward

    if (Path(project_root) / ".git").exists() and read_install_stamp(project_root).get("updateMechanism") != "self":
        return None  # A developer's checkout does not owe managed products.

    problems = pm.activate()
    if not problems:
        return None
    steward = sealed_steward(Path(project_root))
    remedy = (f"this {steward}-managed install must rebuild the artifact to fix"
              if steward else "run `hermes pm install`")
    return f"install out of sync ({'; '.join(problems)}) — {remedy}"


def publish_launchers(project_root: Path, *, create: bool = True) -> None:
    """Refresh durable commands; bootstrap repairs only existing PATH exposure."""
    import logging

    from hermes_cli._launchers import ENTRY_POINTS, ensure_install_launchers, expose_cli, resolve_store_python
    from hermes_cli.steward import read_install_stamp

    root = Path(project_root)
    log = logging.getLogger(__name__)
    if _is_sealed(root):
        log.info("launchers: sealed tree at %s keeps its own", root)
        return  # Sealed and external/Nix interpreters retain their own launchers.
    if read_install_stamp(root).get("updateMechanism") == "external":
        log.info("launchers: external runtime at %s keeps its own", root)
        return
    if resolve_store_python(root) is None:
        # A PM tree promises its launchers (tests/install/e2e-assets/
        # source-driver.sh refuses to let --version paper over the gap), so
        # this skip is a half-finished update, never a quiet no-op.
        log.warning("launchers: no managed interpreter under %s; %s not published",
                    root, root / ".hermes" / "bin")
        return
    written = ensure_install_launchers(root, root / ".hermes" / "bin")
    if len(written) != len(ENTRY_POINTS):
        from pm.package import InstallError

        raise InstallError("launchers", "source launcher publication failed", "retry the source update")
    result = expose_cli(root, create=create)
    if not result["ok"]:
        import logging

        logging.getLogger(__name__).warning("CLI exposure failed: %s", result["error"])


def sync(project_root: Path | None = None, *, check: bool = False) -> dict:
    """Report or sync dependencies. A malformed install stamp is a build error."""
    from hermes_cli.update_stage import publish_stage

    root = Path(project_root) if project_root is not None else _project_root()
    if _is_sealed(root):
        return {"state": "sealed", "ok": True}
    if not (root / "pyproject.toml").is_file():
        return {"state": "failed", "ok": False, "detail": f"no pyproject.toml under {root}"}
    try:
        import pm

        if pm.venv_is_current(project_root=root):
            if not check:
                publish_launchers(root)
            return {"state": "current", "ok": True}
        if check:
            return {"state": "would-sync", "ok": True}
        publish_stage("Updating Python dependencies")
        refuse_foreign_owned_venv(root)
        pm.sync_venv(explicit=True, project_root=root, evict_incompatible_plugins=True)
        collect_superseded_generations(root)
        publish_launchers(root)
        return {"state": "synced", "ok": True}
    except Exception as exc:
        return {"state": "failed", "ok": False, "detail": str(exc)}


def collect_superseded_generations(project_root: Path) -> None:
    """Collect what a publish just superseded, as the Docker boot already does.

    Without this only a manual `hermes pm gc` reclaimed old environments. Safe
    right after a sync: the collectors skip leased, selected and day-young
    generations and yield to any in-flight install instead of waiting.
    """
    import logging

    from hermes_cli.runtime_state import collect_generations
    from pm.environments import install_state_dir
    from pm.runtime import collect_runtime_generations

    try:
        removed = collect_generations(project_root) + collect_runtime_generations(
            install_state_dir(project_root) / "pm-runtime")
    except (OSError, ValueError) as exc:
        # Reclaiming space must never turn a committed update into a failure.
        logging.getLogger(__name__).warning("dependency generation cleanup skipped: %s", exc)
        return
    if removed:
        logging.getLogger(__name__).info("collected %d unused dependency generations", len(removed))


#: Answered from the tree alone; a metadata query must never wait on (or fail with)
#: a network-bound source-update completion.
_METADATA_FLAGS = frozenset({"-h", "--help", "-V", "--version"})


def completion_pending_path(project_root: Path) -> Path:
    """Marker for a source update whose dependency sync committed but whose tail
    (launchers, products, maintenance) has not finished.

    Lives beside PM's facts, not in the checkout: it is per-install state, and a
    root-level file would trip the ZIP updater's dirty-tree check.
    """
    from pm.environments import install_state_dir

    return install_state_dir(project_root) / "source-completion-pending"


#: Second line of the obligation marker: the identity the tail attempt is keyed on. It is
#: minted ONCE, by the arm that creates the obligation, so a launch that re-arms the SAME
#: obligation can never re-open a tail that already ran. The identity used to be the marker's
#: mtime, which the boot path itself rewrote on every launch (t_f6924d31 D2).
_OBLIGATION_LINE = "generation: "


def _new_generation() -> str:
    import uuid

    return uuid.uuid4().hex


def _pending_generation(pending: Path) -> str:
    """The generation stamped at arm time; "" for a marker this version did not write."""
    try:
        text = pending.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    for line in text.splitlines():
        if line.strip().startswith(_OBLIGATION_LINE):
            return line.split(_OBLIGATION_LINE, 1)[1].strip()
    return ""


def arm_completion(project_root: Path, *, fresh: bool = False, origin: str | None = None) -> Path:
    """Persist the tail obligation before selecting a new dependency generation.

    IDEMPOTENT unless *fresh*: an obligation that is already outstanding keeps its marker,
    its identity and its attempt record. ``_sync_source_dependencies`` arms on every launch
    that finds the dependencies stale, so an unconditional re-arm minted a NEW obligation each
    time and deleted the record that said this install had already run the tail — every launch
    then paid the whole tail again (t_f6924d31). Update preparation is also idempotent: its own
    completion tail supersedes any earlier owed tail, so a retry must not mint a new generation
    and reset the one-attempt recovery budget. ``fresh=True`` is reserved for a genuinely new
    obligation when the caller explicitly needs to supersede an existing one.

    ``origin`` is a stable code-path label, never user input; it persists the arm site so a
    future stale marker names its generator without exposing a process command line.
    """
    pending = completion_pending_path(project_root)
    if pending.is_file() and not fresh:
        return pending
    pending.parent.mkdir(parents=True, exist_ok=True)
    provenance = f"origin: {origin}\n" if origin else ""
    pending.write_text(f"source update tail not finished\n{_OBLIGATION_LINE}{_new_generation()}\n{provenance}",
                       encoding="utf-8")
    # A new obligation supersedes any earlier attempt: it is owed a tail of its own.
    _drop_completion_attempt(project_root)
    return pending


def clear_completion(project_root: Path) -> None:
    completion_pending_path(project_root).unlink(missing_ok=True)
    _drop_completion_attempt(project_root)


def completion_attempt_path(project_root: Path) -> Path:
    """Record that the tail has ALREADY been attempted for the current marker.

    The marker alone cannot tell "a crash before the tail ran" from "the tail ran and did
    not finish": a failing or externally killed tail leaves the marker behind, so every later
    ``hermes`` invocation re-ran the whole tail. Measured on this host: ~3 minutes per call,
    unbounded, for every command in every profile (t_f6924d31).

    The record carries the marker mtime it belongs to, so a *fresh* obligation
    (``arm_completion`` writes a new marker) is still attempted once, and a tail that already
    ran is left to ``hermes update`` instead of taxing every command forever.
    """
    from pm.environments import install_state_dir

    return install_state_dir(project_root) / "source-completion-attempted.json"


def _drop_completion_attempt(project_root: Path) -> None:
    completion_attempt_path(project_root).unlink(missing_ok=True)


def _marker_identity(pending: Path) -> str:
    """The obligation's identity: the generation it was armed with, else its own mtime.

    The generation is what makes this stable: it is written once, by the arm that created the
    obligation, and nothing on the launch path rewrites it. The mtime fallback covers a marker
    armed by an older Hermes (or written by hand or a test), which has no generation to key on.
    """
    generation = _pending_generation(pending)
    if generation:
        return f"generation:{generation}"
    try:
        return "mtime:%.6f" % pending.stat().st_mtime
    except OSError:
        return "missing"


def tail_already_attempted(project_root: Path, pending: Path) -> bool:
    """True when the tail ran (or was killed) for exactly this pending marker."""
    try:
        data = json.loads(completion_attempt_path(project_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return data.get("marker") == _marker_identity(pending)


def record_completion_attempt(project_root: Path, pending: Path, *, code: int | None) -> None:
    """Persist the attempt BEFORE the tail runs: a killed tail still gets no second try.

    Not raising here is deliberate — failing to record must never itself break a launch.
    """
    import time

    path = completion_attempt_path(project_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "marker": _marker_identity(pending),
            "at": time.time(),
            "exit_code": code,
        }, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def refuse_foreign_owned_venv(project_root: Path) -> None:
    """Refuse cross-user mutation before PM changes the selected environment (#83529)."""
    if not hasattr(os, "geteuid"):
        return
    uid = os.geteuid()  # windows-footgun: ok — guarded POSIX ownership check
    root = Path(project_root)
    # A root-run update on a user's checkout is not safe even if a fresh
    # generation would be allocated: it publishes root-owned state for them.
    from pm.environments import selected_venv
    candidates = [root, root / "venv", root / ".venv", root / ".hermes", selected_venv(root)]
    for venv in (root / "venv", root / ".venv", candidates[-1]):
        for directory in (venv / ("Scripts" if os.name == "nt" else "bin"),
                          *venv.glob("lib/python*/site-packages")):
            if directory.is_dir():
                candidates.append(directory)
                for entry in list(directory.iterdir())[:2000]:
                    candidates.append(entry)
                    if entry.name.endswith(".dist-info") and entry.is_dir():
                        candidates.extend(list(entry.iterdir())[:100])
    for path in candidates:
        try:
            owner = path.lstat().st_uid
        except FileNotFoundError:
            continue
        if owner != uid:
            raise RuntimeError(
                f"refusing to update {root}: {path} is owned by uid {owner}, "
                f"not the current uid {uid}; repair ownership before retrying"
            )


def prepare_launch(project_root: Path, argv: list[str]) -> Path | None:
    """Finish a self-managed source update before importing app dependencies.

    PM's successful input stamp signals a finished dependency sync; the
    ``source-completion-pending`` marker signals the tail still owed after it.
    Each obligation is attempted AT MOST ONCE: a repair that re-runs itself on
    every command is worse than the state it repairs (measured here: 73s-192s per
    ``hermes`` invocation, every command, every profile — t_f6924d31). The
    obligation survives in the marker for ``hermes update``, which owns it; a
    genuinely new obligation still gets a tail of its own.
    Old updaters need not write a marker (and cannot accidentally clear this obligation).
    Return the store interpreter when this process must restart cleanly.
    """
    import os
    import sys
    from hermes_cli._parser import command_argv
    from hermes_cli.steward import read_install_stamp

    root = Path(project_root).resolve()
    if (command_argv(argv)[:1] == ["pm"]
            or _METADATA_FLAGS & set(argv)
            or os.environ.get("HERMES_DISABLE_LAZY_INSTALLS", "").lower() in ("1", "true", "yes")
            or not (root / ".git").exists()
            or not (root / "pyproject.toml").is_file()):
        return None
    stamp = read_install_stamp(root)
    if not stamp:
        from hermes_cli.post_update import step_adopt_blessed_checkout

        step_adopt_blessed_checkout(root)
        stamp = read_install_stamp(root)
    if stamp.get("updateMechanism") != "self":
        return None  # Developer checkouts and packaged runtimes retain their owner.

    import pm
    from hermes_cli._launchers import resolve_store_python
    from hermes_cli.update_lock import UpdateLock, read_live_update

    current = pm.venv_is_current(project_root=root)
    pending = completion_pending_path(root)
    if not current or pending.is_file():
        lock = UpdateLock()
        if not lock.acquire():
            raise RuntimeError("an update is still running; wait for it to exit, then relaunch Hermes")
        try:
            # The tail imports the application, whose entry point runs this very function:
            # under the launching process's own claim (its pid is our ancestor) we ARE that
            # tail and owe nothing — without this, a pending marker recurses forever.
            if not lock.acquired and read_live_update() is not None:
                if current:
                    return None
                # A process the update spawns before its dependencies are current (a restarted
                # gateway) would boot on a tree built for another interpreter. Sync — never the
                # tail, which is the updater's — then relaunch below into a current install.
                _sync_source_dependencies(root, arm=False)
                if not pm.venv_is_current(project_root=root):
                    # Relaunching would land back here and sync again, forever.
                    raise RuntimeError("dependency sync left this install out of date")
            else:
                _finish_source_update(root, current=current, pending=pending)
        finally:
            lock.release()
    python = resolve_store_python(root)
    if python is None:
        raise RuntimeError("source update has no managed Python; run `hermes pm install`")
    # Lexical identity, never resolve(): PM spells the store path through
    # HERMES_HOME (which may carry '..') while sys.executable arrives
    # normalized, so a raw compare re-execs every child forever (#122513). A
    # venv interpreter symlinked to the same binary is still a different
    # interpreter (its own sys.prefix) and must re-exec once.
    same = os.path.normcase(os.path.abspath(python)) == os.path.normcase(os.path.abspath(sys.executable))
    if not current or not same:
        publish_launchers(root)
        return python
    return None


def _finish_source_update(root: Path, *, current: bool, pending: Path) -> None:
    """Sync dependencies when they are stale, then run the tail the marker still owes."""
    import sys
    from hermes_cli._early_recovery import _marker_owner_is_live
    from pm.environments import activation_environment

    if not current:
        # Existing markers guard liveness, never create the completion obligation.
        # Current post-sync verification children can boot under a live updater.
        legacy_markers = (root / ".update-incomplete", root / ".lazy-refresh-incomplete")
        if any(_marker_owner_is_live(marker) for marker in legacy_markers):
            raise RuntimeError("an update is still running; wait for it to exit, then relaunch Hermes")
        # Normalize the obligation BEFORE anything is spent or recorded: arming is
        # idempotent, so the identity the attempt record is keyed on is the one this
        # obligation already has. A fresh one is minted only for a genuinely new obligation
        # (no marker at all) — that is what makes the crash-recovery path still work.
        pending = arm_completion(root, origin="venv_sync._finish_source_update (current=False)")
    if tail_already_attempted(root, pending):
        # The tail is not a thing to re-run on every launch until it works: it costs minutes
        # (Node deps + TUI + web + desktop) and, when the dependencies are stale, a sync that
        # blocks behind a live stager's prepare lock. A repair path retried forever on every
        # command is worse than the state it repairs. The obligation survives in the marker
        # for `hermes update`; a NEW obligation re-arms an attempt of its own.
        if not current:
            # The one attempt is spent AND the sync it owed never committed, so this install
            # is still not current. Returning here does not end the launch quietly: the
            # caller reaches `if not current or not same: return python` (below), hands the
            # launcher a store interpreter, and hermes_bootstrap re-execs
            # (hermes_bootstrap.py:533-543) into a process that recomputes exactly this
            # state — so the CLI spawns interpreters until the host gives up. Measured on a
            # clone whose sync raises "network unavailable": launch 2 and launch 3 each
            # RETURNED .../Scripts/python.exe while venv_is_current() stayed False
            # (t_5f5e6615). Raising keeps the documented terminal state instead:
            # hermes_bootstrap.py:544-551 warns and runs on the previous dependency
            # generation, which is intact because a failed sync commits nothing.
            raise RuntimeError(
                "the dependencies this update still owes are stale and the launch path has "
                "already spent its one attempt at them; run `hermes update` to finish it"
            )
        print("hermes: the source-update tail already ran for this install state and did not "
              "finish; not retrying it on every launch — run `hermes update` to finish it",
              file=sys.stderr, flush=True)
        return
    if not current:
        print("hermes: completing source-update dependencies...", file=sys.stderr, flush=True)
        # Recorded BEFORE the sync: the sync is the expensive half (a prepare-lock stage on
        # this host costs 30s+ per attempt and the products cost minutes), and a launch that
        # never got past it is still an attempt this install must not repeat forever.
        record_completion_attempt(root, pending, code=None)
        _sync_source_dependencies(root, arm=True)
    else:
        print("hermes: finishing an interrupted source update...", file=sys.stderr, flush=True)
        # Recorded BEFORE the child runs: a tail killed by the caller's own timeout is still
        # an attempt, and must not be re-run on the next launch.
        record_completion_attempt(root, pending, code=None)
    # Sync commits the dependency generation, but a source update also owes
    # the product builds and the post-build maintenance -- the tail every
    # install and finished update shares (hermes_cli/source_completion.py).
    # Those builds need PM's selected interpreter with its dependencies
    # activated, so hand that file THIS interpreter and let it re-exec
    # itself, exactly as the installers do.
    desktop_app = root / "apps/desktop"
    desktop = ((desktop_app / "dist/index.html").is_file()
               or any((desktop_app / "release").glob("*")))
    # The tail's progress lines go to stderr: this is an automatic repair in
    # front of whatever command the user ran, and that command may be
    # emitting machine-readable stdout (a JSON probe, a piped query).
    code = subprocess.call(
        [sys.executable, "-I", "-B", "-u",
         str(root / "hermes_cli/source_completion.py"),
         "--source", str(root), "--finish-update",
         *((("--desktop", "--desktop-optional") if desktop else ()))],
        cwd=root, env=activation_environment(root), stdout=sys.__stderr__,
    )
    record_completion_attempt(root, pending, code=code)
    if code != 0:
        raise RuntimeError(
            "source update completion failed; run `hermes update` to finish it"
        )
    clear_completion(root)


def _sync_source_dependencies(root: Path, *, arm: bool) -> None:
    """Commit the tree's dependency generation; *arm* also owes the tail afterwards."""
    import sys
    import pm
    from pm.client import ensure_tools_for_sync
    from pm.environments import runtime_facts_path
    from pm.extras import legacy_selection

    if not arm:
        print("hermes: preparing dependencies for this update...", file=sys.stderr, flush=True)
    refuse_foreign_owned_venv(root)
    if arm:
        # Owed from before the sync commits: a crash between the commit and the
        # tail must leave the tail, not a "current" install with nothing built.
        arm_completion(root, origin="venv_sync._sync_source_dependencies (arm=True)")
    # Main-era installs have no PM ledger; carry what their venv held.
    # Established PM installs retain their recorded extras and plugin union instead.
    extras = legacy_selection(root) if not runtime_facts_path(root).is_file() else None
    # Same order as `hermes update`: an interrupted update or a hand-run
    # `git pull` leaves this tree's lockfile ahead of the installed tools.
    ensure_tools_for_sync()
    pm.sync_venv(extras, explicit=True, project_root=root, evict_incompatible_plugins=True)
    collect_superseded_generations(root)
    # These can predate the swap. Once PM commits the replacement they
    # must not make early recovery immediately rebuild it a second time.
    for name in (".update-incomplete", ".lazy-refresh-incomplete"):
        (root / name).unlink(missing_ok=True)


def relaunch_command(
    python: Path, root: Path, argv: list[str], original: list[str], module: str | None,
) -> list[str]:
    """Re-enter the same script/module/launcher with the managed interpreter.

    An old venv may use a different Python ABI. Do not add the new generation
    to that interpreter, and do not depend on its obsolete editable finder.
    """
    # Preserve interpreter options, not application flags with the same names.
    options: list[str] = []
    index = 1
    while index < len(original):
        option = original[index]
        if option in ("-c", "-m", "--", "-") or not option.startswith("-"):
            break
        options.append(option)
        index += 1
        if option in ("-W", "-X") and index < len(original):
            options.append(original[index])
            index += 1
    prefix = f"import sys, runpy; sys.path.insert(0, {str(root)!r}); sys.argv = {argv!r}; "
    if argv[0] == "-c":
        body = f"exec({original[index + 1]!r})"
    elif module and module != "__main__":
        body = f"runpy.run_module({module!r}, run_name='__main__', alter_sys=True)"
    else:
        # distlib .exe launchers are executable zip files with __main__, not
        # importable modules named '__main__'. run_path handles both shapes.
        body = f"runpy.run_path({str(Path(argv[0]).absolute())!r}, run_name='__main__')"
    return [str(python), *options, "-I", "-c", prefix + body]


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hermes_cli.venv_sync")
    parser.add_argument("--project-root", default=None)
    parser.add_argument(
        "--check", action="store_true", help="report; change nothing"
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    result = sync(
        Path(args.project_root) if args.project_root else None, check=args.check
    )

    if args.json:
        print(json.dumps(result))
    else:
        detail = f" ({result['detail']})" if result.get("detail") else ""
        print(f"venv sync: {result['state']}{detail}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
