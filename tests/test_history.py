"""The audit trail. If this is wrong you cannot tell what you already did."""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataremoval import catalog, cli, db


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "h.db")
    db.init(c)
    catalog.sync(c, catalog.load_yaml(catalog.bundled_path()))
    return c


def events(c):
    return c.execute("SELECT * FROM event ORDER BY id").fetchall()


def test_log_event_stamps_a_time_not_just_a_date(conn):
    """Same-day events are the common case; a date alone cannot order them."""
    conn.execute("INSERT INTO request(broker_key) VALUES('spokeo')")
    cli.log_event(conn, 1, "status", "sent")
    conn.commit()
    at = events(conn)[0]["at"]
    assert len(at) >= 16 and ":" in at, f"expected a timestamp, got {at!r}"


def test_event_can_belong_to_a_broker_with_no_request(conn):
    """Verify sweeps are not tied to a request, but still belong in history."""
    cli.log_event(conn, None, "verify", "spokeo: ok -> notfound", broker_key="spokeo")
    conn.commit()
    e = events(conn)[0]
    assert e["request_id"] is None and e["broker_key"] == "spokeo"


def test_history_orders_forwards_but_keeps_the_recent_ones_under_a_limit(conn):
    """--limit must trim the OLDEST, then display oldest-first - otherwise a
    limit shows you ancient history and hides what just happened."""
    conn.execute("INSERT INTO request(broker_key) VALUES('spokeo')")
    for i in range(10):
        conn.execute(
            "INSERT INTO event(request_id, at, kind, detail) VALUES(1,?,?,?)",
            (f"2026-09-{i + 1:02d} 09:00:00", "note", f"n{i}"),
        )
    conn.commit()
    rows = conn.execute(
        "SELECT detail FROM (SELECT * FROM event ORDER BY at DESC, id DESC LIMIT 3) "
        "ORDER BY at ASC"
    ).fetchall()
    assert [r["detail"] for r in rows] == ["n7", "n8", "n9"]


def test_verify_logs_only_changes(tmp_path, monkeypatch, capsys):
    """A sweep finding everything unchanged should add one summary line, not
    one line per broker - otherwise history becomes unreadable at 549."""
    from dataremoval import verify

    home = tmp_path / "home"
    monkeypatch.setattr(cli, "HOME", home)
    monkeypatch.setattr(cli, "DB_PATH", home / "removal.db")
    c = db.connect(home / "removal.db")
    db.init(c)
    catalog.sync(c, catalog.load_yaml(catalog.bundled_path()))
    c.execute("UPDATE broker SET active=0")
    c.execute("UPDATE broker SET active=1, verify_status='ok' WHERE key='spokeo'")
    c.execute("UPDATE broker SET active=1, verify_status='ok' WHERE key='radaris'")
    c.commit()
    c.close()

    def fake(brokers, **kw):
        out = []
        for key, name, url in brokers:
            # spokeo unchanged; radaris regressed.
            status = "ok" if key == "spokeo" else "notfound"
            r = verify.Result(key, name, url, status, note="n")
            out.append(r)
            if kw.get("on_result"):
                kw["on_result"](r)
        return out

    monkeypatch.setattr(verify, "check_many", fake)
    args = cli.build_parser().parse_args(["verify", "--all"])
    cli.cmd_verify(args)

    c = db.connect(home / "removal.db")
    ev = [e["detail"] for e in events(c) if e["kind"] == "verify"]
    per_broker = [d for d in ev if "->" in d]
    assert len(per_broker) == 1, f"expected only the changed broker, got {per_broker}"
    assert "notfound" in per_broker[0]
    assert any("swept" in d for d in ev), "the sweep itself should be recorded"
