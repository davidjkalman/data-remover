"""The browser session. Its whole job is bookkeeping you would otherwise skip,
so what it records has to be exactly right."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataremoval import catalog, cli, db
from dataremoval.profile import Address, Profile


def make_profile():
    return Profile(
        full_name="Real Person", birth_year=1980, emails=["me@mail.test"],
        addresses=[Address(line1="1 A St", city="X", state="CA", postal="90001",
                           from_year=2020),
                   Address(line1="2 B St", city="Y", state="CA", postal="90002",
                           from_year=2010, to_year=2020)],
        residency_state="CA", contact_email="priv@mail.test",
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(cli, "HOME", home)
    monkeypatch.setattr(cli, "DB_PATH", home / "removal.db")
    conn = db.connect(home / "removal.db")
    db.init(conn)
    catalog.sync(conn, catalog.load_yaml(catalog.bundled_path()))
    conn.commit()
    conn.close()

    monkeypatch.setattr(cli, "load_profile", make_profile)
    monkeypatch.setattr(cli.webbrowser, "open", lambda u: True)
    monkeypatch.setattr(cli, "copy_to_clipboard", lambda t: "fake")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    return home


def run(answers, **kw):
    it = iter(answers)
    import builtins
    orig = builtins.input
    builtins.input = lambda *a: next(it, "q")
    try:
        args = cli.build_parser().parse_args(["batch"] + [str(x) for x in kw.get("argv", [])])
        cli.cmd_batch(args)
    finally:
        builtins.input = orig


def db_open(home):
    c = db.connect(home / "removal.db")
    return c


def test_enter_marks_sent_and_starts_the_clock(env, capsys):
    run([""], argv=["--limit", "1"])
    c = db_open(env)
    r = c.execute("SELECT * FROM request").fetchall()
    assert len(r) == 1
    assert r[0]["status"] == "sent"
    assert r[0]["sent_at"] and r[0]["due_at"], "a sent request must carry a deadline"


def test_skip_records_nothing(env):
    """A skipped site must not leave a phantom pending request - that would
    make it invisible to the next session."""
    run(["s"], argv=["--limit", "1"])
    c = db_open(env)
    assert c.execute("SELECT COUNT(*) c FROM request").fetchone()["c"] == 0


def test_blocked_is_recorded_with_the_reason(env):
    run(["b", "wants a passport scan"], argv=["--limit", "1"])
    c = db_open(env)
    r = c.execute("SELECT * FROM request").fetchone()
    assert r["status"] == "blocked"
    ev = c.execute("SELECT detail FROM event WHERE kind='status' "
                   "ORDER BY id DESC LIMIT 1").fetchone()
    assert "passport" in ev["detail"]


def test_quit_stops_immediately(env):
    run(["", "q"], argv=["--limit", "5"])
    c = db_open(env)
    assert c.execute("SELECT COUNT(*) c FROM request").fetchone()["c"] == 1


def test_email_channel_switch_records_the_channel(env, capsys):
    # Constrain to a broker that actually publishes a privacy address.
    c = db_open(env)
    c.execute("UPDATE broker SET active=0")
    c.execute("UPDATE broker SET active=1 WHERE key='spokeo'")
    c.commit(); c.close()
    run(["e"], argv=["--limit", "1"])
    c = db_open(env)
    r = c.execute("SELECT * FROM request").fetchone()
    assert r["channel"] == "email"
    assert r["status"] == "pending", "an emailed letter is not sent until you send it"
    assert "1798.105" in capsys.readouterr().out


def test_in_flight_brokers_are_not_offered_again(env):
    """The point of the session is not doing the same site twice."""
    run([""], argv=["--limit", "1"])
    c = db_open(env)
    first = c.execute("SELECT broker_key FROM request").fetchone()["broker_key"]
    c.close()
    run([""], argv=["--limit", "1"])
    c = db_open(env)
    keys = [r["broker_key"] for r in c.execute("SELECT broker_key FROM request")]
    assert len(set(keys)) == 2, f"offered {first} twice"


def test_known_dead_links_are_excluded_unless_asked_for(env, capsys):
    c = db_open(env)
    c.execute("UPDATE broker SET active=0")
    c.execute("UPDATE broker SET active=1, verify_status='notfound' WHERE key='spokeo'")
    c.commit(); c.close()

    args = cli.build_parser().parse_args(["batch", "--dry-run"])
    cli.cmd_batch(args)
    assert "Nothing queued" in capsys.readouterr().out

    args = cli.build_parser().parse_args(["batch", "--dry-run", "--include-dead"])
    cli.cmd_batch(args)
    assert "Spokeo" in capsys.readouterr().out


def test_email_only_brokers_are_not_offered(env, capsys):
    """A browser session is for forms; email-channel brokers belong elsewhere."""
    c = db_open(env)
    c.execute("UPDATE broker SET active=0")
    c.execute("UPDATE broker SET active=1 WHERE method='email'")
    c.commit(); c.close()
    args = cli.build_parser().parse_args(["batch", "--dry-run"])
    cli.cmd_batch(args)
    assert "Nothing queued" in capsys.readouterr().out


def test_non_interactive_shows_the_plan_and_writes_nothing(env, monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    args = cli.build_parser().parse_args(["batch", "--limit", "3"])
    cli.cmd_batch(args)
    out = capsys.readouterr().out
    assert "not a terminal" in out
    c = db_open(env)
    assert c.execute("SELECT COUNT(*) c FROM request").fetchone()["c"] == 0


def test_ordering_is_tier_then_fewest_hurdles(env, capsys):
    args = cli.build_parser().parse_args(["batch", "--dry-run", "--tier", "1"])
    cli.cmd_batch(args)
    lines = [l for l in capsys.readouterr().out.splitlines() if l.startswith(("optout", "acxiom", "ca-drop"))]
    assert lines and lines[0].startswith("optoutprescreen"), \
        "the zero-hurdle tier-1 item should lead"


def test_clipboard_failure_falls_back_to_printing(env, monkeypatch, capsys):
    monkeypatch.setattr(cli, "copy_to_clipboard", lambda t: None)
    run(["s"], argv=["--limit", "1"])
    assert "paste into" in capsys.readouterr().out, \
        "with no clipboard the crib must be printed or the session is useless"


def test_email_switch_is_refused_when_no_address_is_on_file(env, capsys):
    """OptOutPrescreen has no privacy address. Offering the email channel
    anyway would produce a letter with nowhere to go."""
    c = db_open(env)
    c.execute("UPDATE broker SET active=0")
    c.execute("UPDATE broker SET active=1 WHERE key='optoutprescreen'")
    c.commit(); c.close()
    run(["e", "s"], argv=["--limit", "1"])
    assert "no privacy email on file" in capsys.readouterr().out
    c = db_open(env)
    assert c.execute("SELECT COUNT(*) c FROM request").fetchone()["c"] == 0
