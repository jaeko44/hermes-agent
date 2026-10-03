"""A wall cut is TWO conditions and must BOOK two different things (fleet-engineer t_e0203ba6).

The card's DONE WHEN (a): "a busy-session delivery timeout and a dead delivery leg no longer
produce the same ``last_delivery_error`` string", with the falsification shown — a fixture that
is busy-session-only must NOT be flagged as a broken leg, and a genuinely stalled child MUST be.

Why this file is the prevention, not just the proof: before it, ``except
subprocess.TimeoutExpired`` bound no name, consulted nothing, and returned one fixed sentence
for every wall cut, so both conditions landed in ``last_delivery_error`` as the same bytes.
A fix that only changed that string would still let the CLASS return the moment the shared
template came back; these tests bind the two conditions to two different bookings, so a revert
of either half fails here.

The discriminator is the CHILD'S OWN resume banner, not a guess about the fleet's schedules:
``-Q`` prints ``↻ Resumed session <id> "Bot Chat"`` to stderr the moment it has acquired the
named session, before the turn's first inference (cli_agent_setup_mixin.py:614-617). Absent
banner + no output at all = the child never got into the session (a dead leg). Present banner
= the session was acquired and the cap cut a turn that had already started (a busy session).
"""
import subprocess

import pytest

from cron import scheduler_delivery as delivery


def _wall(*, startup_seen=True, stdout="", stderr="", returncode=-1):
    """The exception ``quiet_single_query.run_reported_turn`` raises at the cap, as stamped."""
    exc = subprocess.TimeoutExpired(["hermes", "chat"], 1800)
    exc.startup_seen = startup_seen
    exc.stdout = stdout
    exc.stderr = stderr
    exc.returncode = returncode
    exc.report_file = "/tmp/report.json"
    exc.report_nonce = "abc123"
    return exc


_BANNER = '↻ Resumed session 20260929_013922_8591de "Bot Chat" (3 user messages, 14 total)'


# --------------------------------------------------------------- the discriminator itself

def test_busy_session_is_NOT_a_dead_leg():
    """The card's own offense (web-frontend b7a51473e237): the child WAS in the session."""
    kind, why = delivery._bot_chat_wall_diagnosis(
        _wall(stderr=_BANNER + "\nsession_id: 20260929_013922_8591de\n"))
    assert kind == "session_busy", why
    assert "ALIVE" in why


def test_stalled_child_MUST_be_flagged_as_a_dead_leg():
    """A child killed before it ever reached the session is the OTHER condition."""
    kind, why = delivery._bot_chat_wall_diagnosis(_wall())
    assert kind == "leg_dead", why
    assert "cap is not the problem" in why or "raising the cap cannot change" in why


def test_the_two_conditions_can_never_collide():
    """The class itself: the two bookings must differ, not merely the two verdicts."""
    busy = delivery._bot_chat_wall_diagnosis(_wall(stderr=_BANNER + "\n"))
    dead = delivery._bot_chat_wall_diagnosis(_wall())
    assert busy[0] != dead[0]
    assert busy[1] != dead[1]


def test_unreadable_output_is_UNVERIFIED_not_silence():
    """A grandchild holding the pipe means we could not read the child — NOT that it was silent.

    Filing "unreadable" as ``leg_dead`` is the failure this case exists to prevent: it would
    action a cap that is not the problem. Fail closed to ``unverified`` instead.
    """
    kind, why = delivery._bot_chat_wall_diagnosis(_wall(startup_seen=False))
    assert kind == "unverified", why
    assert "UNKNOWN" in why


def test_a_stale_report_does_not_fake_a_resume_banner():
    """Output that never reported a resume is NOT session_busy — it is unverified."""
    kind, _why = delivery._bot_chat_wall_diagnosis(
        _wall(stderr="loading config\nsome unrelated traceback text\n"))
    assert kind == "unverified"


def test_stdout_answer_alone_is_not_a_resume_proof():
    """The banner must come from stderr (where ``-Q`` prints it), not from an inner echo.

    A turn whose ANSWER mentions "Resumed session" (a resumed-session digest) must not be
    able to fake session acquisition. It read as ``unverified`` rather than ``leg_dead``
    because stdout was non-empty: "it said something but never proved it resumed the session"
    is genuinely unknown, and filing it as a dead leg would action a cap that is not the
    problem. The property under test is that stdout can NEVER produce ``session_busy``.
    """
    kind, _why = delivery._bot_chat_wall_diagnosis(
        _wall(stdout="I read the Resumed session banner for 20260929 and continued."))
    assert kind != "session_busy"
    assert kind == "unverified"


# ------------------------------------------------------- the BOOKING (the real regression)

@pytest.fixture()
def cli_lane(tmp_path, monkeypatch):
    """Route _deliver_to_bot_chat straight into the CLI fallback lane, queueing nothing."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


def _book(cli_lane, monkeypatch, wall):
    """Book one wall cut through the lane and return the string the blotter would carry."""
    queued = []
    from cron import bot_chat_delivery as queue
    monkeypatch.setattr(queue, "defer", lambda *a, **k: queued.append(1) or {"id": "x",
                                                                           "status": "queued"})
    monkeypatch.setattr(delivery, "_run_bot_chat_turn",
                        lambda *a, **k: (_ for _ in ()).throw(wall))
    job = {"id": "job-1", "name": "probe", "execution_id": "exec-1"}
    booked = delivery._deliver_to_bot_chat(job, "payload", "")
    return booked, queued


def test_the_two_conditions_book_DIFFERENT_strings(cli_lane, monkeypatch):
    """DONE WHEN (a), asserted on the shipped producer, not on a helper's return value."""
    busy, _ = _book(cli_lane, monkeypatch, _wall(stderr=_BANNER + "\n"))
    dead, _ = _book(cli_lane, monkeypatch, _wall())
    assert busy is not None and dead is not None
    assert busy != dead, "the two conditions still book one sentence"
    assert "[session_busy]" in busy and "[leg_dead]" in dead


def test_a_dead_leg_still_queues_the_marker(cli_lane, monkeypatch):
    """Every condition keeps the short marker: suppressing it for one would make that
    condition the one that loses the alert (the 2026-09-19 docgen-deadman regression),
    reached through a fix for a different class."""
    _booked_busy, busy_queued = _book(cli_lane, monkeypatch, _wall(stderr=_BANNER + "\n"))
    _booked_dead, dead_queued = _book(cli_lane, monkeypatch, _wall())
    _booked_unver, unver_queued = _book(cli_lane, monkeypatch,
                                        _wall(startup_seen=False))
    assert len(busy_queued) == 1 and len(dead_queued) == 1 and len(unver_queued) == 1


def test_the_cap_advice_is_scoped_to_the_condition(cli_lane, monkeypatch):
    """The old text told every wall cut to raise the cap — wrong for a dead leg."""
    dead, _ = _book(cli_lane, monkeypatch, _wall())
    assert "only for session_busy" in dead


def test_an_unattributable_wall_stays_loud(cli_lane, monkeypatch):
    """Fail-closed: a wall the spawner did not stamp is ``unverified``, never filed as either."""
    bare = subprocess.TimeoutExpired(["hermes", "chat"], 1800)  # nothing stamped on it
    booked, _queued = _book(cli_lane, monkeypatch, bare)
    assert "[unverified]" in booked


def test_the_booking_still_names_the_saved_output(cli_lane, monkeypatch):
    """The regression this class was opened over: a wall cut must never lose the alert."""
    booked, _queued = _book(cli_lane, monkeypatch, _wall())
    assert "hermes cron runs" in booked
    assert "job-1" in booked


# ------------------------------------------------------------- the spawner's half of it

def test_the_spawner_stamps_the_attribution_the_rule_demands():
    """The booking rule refuses a result with no returncode, no report path or no nonce, so
    the wall's exception must carry all of them (quiet_single_query.run_reported_turn)."""
    import inspect
    from hermes_cli import quiet_single_query as qsq
    src = inspect.getsource(qsq.run_reported_turn)
    for stamp in ("exc.returncode", "exc.report_file", "exc.report_nonce"):
        assert stamp in src, "the wall no longer stamps %s -> the booking rule refuses it" % stamp