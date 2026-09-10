"""Listings are the real unit of removal.

A broker that clears one of your four listings will tell you the job is done.
If the tool believes that, its coverage number is a lie - which is the number
the dashboard leads with.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataremoval import catalog, cli, dashboard, db
from dataremoval.profile import Address, Profile


def make_profile():
    return Profile(
        full_name="Real Person", birth_year=1980, emails=["me@mail.test"],
        addresses=[Address(line1="1 A St", city="X", state="CA", postal="90001",
                           from_year=2020)],
        residency_state="CA", contact_email="priv@mail.test",
    )


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(cli, "HOME", home)
    monkeypatch.setattr(cli, "DB_PATH", home / "removal.db")
    monkeypatch.setattr(cli, "load_profile", make_profile)
    monkeypatch.setattr(cli, "PROFILE_PATH", home / "profile.yaml")
    c = db.connect(home / "removal.db")
    db.init(c)
    catalog.sync(c, catalog.load_yaml(catalog.bundled_path()))
    from datetime import date, timedelta
    due = (date.today() + timedelta(days=30)).isoformat()
    c.execute("INSERT INTO request(broker_key, status, law, channel, opened_at, "
              "sent_at, due_at) VALUES('spokeo','sent','ccpa','form',"
              "?,?,?)", (date.today().isoformat(), date.today().isoformat(), due))
    c.commit()
    return c


URLS = ["https://s.test/a", "https://s.test/b", "https://s.test/c"]


def test_listings_are_recorded_and_deduped(conn):
    assert cli.add_listings(conn, 1, URLS) == 3
    assert cli.add_listings(conn, 1, URLS) == 0, "re-adding the same URL is not an error"
    assert cli.listing_tally(conn, 1)["total"] == 3


def test_blank_urls_are_ignored(conn):
    assert cli.add_listings(conn, 1, ["", "  ", None, URLS[0]]) == 1


def test_one_removal_of_three_is_partial_not_complete(conn):
    """The whole point. A broker clearing one listing has not finished."""
    cli.add_listings(conn, 1, URLS)
    conn.commit()
    args = cli.build_parser().parse_args(["listing", "1", "1", "--status", "removed"])
    cli.cmd_listing(args)
    assert conn.execute("SELECT status FROM request WHERE id=1").fetchone()["status"] \
        == "partial"


def test_all_removals_complete_the_request_and_schedule_a_recheck(conn):
    cli.add_listings(conn, 1, URLS)
    conn.commit()
    args = cli.build_parser().parse_args(["listing", "1", "--all", "--status", "removed"])
    cli.cmd_listing(args)
    r = conn.execute("SELECT * FROM request WHERE id=1").fetchone()
    assert r["status"] == "completed"
    assert r["closed_at"] and r["recheck_at"], "a completed removal must be re-checked"


def test_no_listings_means_no_derived_status(conn):
    """Most brokers never give per-listing URLs. Silence is not evidence, so a
    request without listings must keep whatever status you set by hand."""
    assert cli.derive_status(conn, 1, "sent") is None
    assert cli.sync_status_from_listings(conn, 1) is None


def test_listings_all_present_does_not_downgrade_a_status(conn):
    cli.add_listings(conn, 1, URLS)
    conn.commit()
    assert cli.derive_status(conn, 1, "sent") is None
    assert conn.execute("SELECT status FROM request WHERE id=1").fetchone()["status"] \
        == "sent"


def test_completing_with_listings_still_up_is_refused(conn, capsys):
    cli.add_listings(conn, 1, URLS)
    conn.commit()
    args = cli.build_parser().parse_args(["log", "1", "--status", "completed"])
    with pytest.raises(SystemExit) as e:
        cli.cmd_log(args)
    assert "still has 3 of 3" in str(e.value)
    assert conn.execute("SELECT status FROM request WHERE id=1").fetchone()["status"] \
        == "sent", "the refused transition must not have been applied"


def test_force_overrides_the_guard(conn):
    cli.add_listings(conn, 1, URLS)
    conn.commit()
    args = cli.build_parser().parse_args(
        ["log", "1", "--status", "completed", "--force"])
    cli.cmd_log(args)
    assert conn.execute("SELECT status FROM request WHERE id=1").fetchone()["status"] \
        == "completed"


def test_guard_does_not_fire_without_listings(conn):
    args = cli.build_parser().parse_args(["log", "1", "--status", "completed"])
    cli.cmd_log(args)
    assert conn.execute("SELECT status FROM request WHERE id=1").fetchone()["status"] \
        == "completed"


def test_unknown_status_is_not_counted_as_removed(conn):
    cli.add_listings(conn, 1, URLS)
    conn.commit()
    args = cli.build_parser().parse_args(["listing", "1", "1", "--status", "unknown"])
    cli.cmd_listing(args)
    t = cli.listing_tally(conn, 1)
    assert t["removed"] == 0 and t["unknown"] == 1
    assert conn.execute("SELECT status FROM request WHERE id=1").fetchone()["status"] \
        == "sent"


def test_out_of_range_listing_number_is_refused(conn):
    cli.add_listings(conn, 1, URLS)
    conn.commit()
    args = cli.build_parser().parse_args(["listing", "1", "9", "--status", "removed"])
    with pytest.raises(SystemExit, match="No listing 9"):
        cli.cmd_listing(args)


def test_dashboard_separates_listings_from_brokers(conn):
    """A partial removal must not read as a completed one on the dashboard."""
    cli.add_listings(conn, 1, URLS)
    conn.commit()
    args = cli.build_parser().parse_args(["listing", "1", "1", "--status", "removed"])
    cli.cmd_listing(args)

    d = dashboard.collect(conn, make_profile())
    assert d["totals"]["removed"] == 0, "a partial broker is not a removed broker"
    assert d["totals"]["partial"] == 1
    assert d["totals"]["listings"] == 3
    assert d["totals"]["listings_removed"] == 1
    html = dashboard.render(d)
    assert "Partly removed" in html
    assert "2 of 3 still up" in html, "the shortfall should be named, not implied"


def test_overdue_outranks_partial_in_the_attention_list(conn):
    """A request that is both partial and past its deadline is an overdue
    request first - the deadline is the actionable fact."""
    from datetime import date, timedelta

    past = (date.today() - timedelta(days=20)).isoformat()
    conn.execute("UPDATE request SET due_at=? WHERE id=1", (past,))
    cli.add_listings(conn, 1, URLS)
    conn.commit()
    args = cli.build_parser().parse_args(["listing", "1", "1", "--status", "removed"])
    cli.cmd_listing(args)

    d = dashboard.collect(conn, make_profile())
    row = d["attention"][0]
    assert row["overdue"] and row["status"] == "partial"
    html = dashboard.render(d)
    assert "20d over" in html
    assert "1/3" in html, "the table should still show the partial listing count"
