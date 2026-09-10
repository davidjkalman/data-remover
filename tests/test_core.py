import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataremoval import catalog, db, laws, templates
from dataremoval.profile import Address, Profile


def flat(text):
    """Compare against wrapped output without asserting on where it wrapped."""
    return " ".join(text.split())


def sample_profile():
    return Profile(
        full_name="Jane Q. Public",
        name_variants=["Jane Public"],
        birth_year=1980,
        emails=["jane@example.com"],
        phones=["+1 555 555 0100"],
        addresses=[
            Address(line1="123 Main St", city="Springfield", state="CA",
                    postal="90210", from_year=2019),
            Address(line1="9 Old Rd", city="Oakland", state="CA",
                    postal="94601", from_year=2012, to_year=2019),
        ],
        residency_state="CA",
        contact_email="privacy@example.net",
    )


def test_catalog_loads_and_syncs(tmp_path):
    brokers = catalog.load_yaml(catalog.bundled_path())
    assert len(brokers) > 20
    conn = db.connect(tmp_path / "t.db")
    db.init(conn)
    stats = catalog.sync(conn, brokers)
    assert stats["added"] == len(brokers)
    # Re-sync updates rather than duplicating.
    stats = catalog.sync(conn, brokers)
    assert stats["added"] == 0 and stats["updated"] == len(brokers)


def test_sync_preserves_local_verification(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init(conn)
    brokers = catalog.load_yaml(catalog.bundled_path())
    catalog.sync(conn, brokers)
    conn.execute("UPDATE broker SET url_verified=1 WHERE key='spokeo'")
    conn.commit()
    catalog.sync(conn, brokers)
    row = conn.execute("SELECT url_verified FROM broker WHERE key='spokeo'").fetchone()
    assert row["url_verified"] == 1, "a catalog refresh must not discard verification work"


def test_law_selection():
    assert laws.choose("CA", False).key == "ccpa"
    assert laws.choose("CA", True).key == "gdpr", "EU residency should win on strength"
    assert laws.choose("TX", False).deadline_days == 45
    assert laws.choose("WY", False).key == "none"


def test_deletion_request_cites_statute_and_lists_addresses():
    p = sample_profile()
    body = templates.deletion_request(p, laws.CCPA, "Spokeo", ["https://spokeo.com/x"])
    assert "1798.105" in flat(body)
    assert "123 Main St" in body and "9 Old Rd" in body, "old addresses must be included"
    assert "https://spokeo.com/x" in body
    assert "45 days" in flat(body)


def test_gdpr_request_differs():
    body = templates.deletion_request(sample_profile(), laws.GDPR, "Acxiom")
    assert "Article 17" in flat(body) and "1798.105" not in flat(body)


def test_followup_reports_overdue_days():
    sent = date.today() - timedelta(days=60)
    body = templates.followup(sample_profile(), laws.CCPA, "Radaris", sent, 15)
    assert "15 day(s) ago" in flat(body)
    assert "California Privacy Protection Agency" in flat(body)


def test_profile_warns_about_reusing_contact_email():
    p = sample_profile()
    p.contact_email = p.emails[0]
    assert any("dedicated" in w for w in p.warnings())


def test_profile_warns_on_thin_address_history():
    p = sample_profile()
    p.addresses = p.addresses[:1]
    assert any("prior addresses" in w for w in p.warnings())


def test_paragraphs_are_wrapped():
    body = templates.deletion_request(sample_profile(), laws.CCPA, "Spokeo")
    prose = [l for l in body.splitlines() if l.startswith("Under ")]
    assert prose, "expected the statutory paragraph"
    assert all(len(l) <= 80 for l in body.splitlines()), "plain-text email must be pre-wrapped"


def test_migration_upgrades_a_v1_database(tmp_path):
    """A database created before verify existed must gain the columns, keep its
    rows, and not be recreated from scratch."""
    from dataremoval import db as dbm

    V1_BROKER = """
    CREATE TABLE broker (
        key TEXT PRIMARY KEY, name TEXT NOT NULL, tier INTEGER NOT NULL DEFAULT 2,
        method TEXT NOT NULL DEFAULT 'form', optout_url TEXT, email TEXT,
        requires TEXT NOT NULL DEFAULT '', recheck_days INTEGER NOT NULL DEFAULT 180,
        feeds TEXT NOT NULL DEFAULT '', notes TEXT,
        url_verified INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1);
    CREATE TABLE request (
        id INTEGER PRIMARY KEY AUTOINCREMENT, broker_key TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending');
    CREATE TABLE event (
        id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER,
        at TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '');
    CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
    """

    conn = dbm.connect(tmp_path / "old.db")
    conn.executescript(V1_BROKER)
    conn.execute("INSERT INTO meta(k,v) VALUES('schema_version','1')")
    conn.execute("INSERT INTO broker(key,name,url_verified) VALUES('keepme','Keep Me',1)")
    conn.commit()
    assert "verify_status" not in [r[1] for r in conn.execute("PRAGMA table_info(broker)")]

    assert dbm.migrate(conn) == dbm.SCHEMA_VERSION - 1

    cols = [r[1] for r in conn.execute("PRAGMA table_info(broker)")]
    assert "verify_status" in cols and "verify_url" in cols
    assert "broker_key" in [r[1] for r in conn.execute("PRAGMA table_info(event)")]
    row = conn.execute("SELECT name, url_verified FROM broker WHERE key='keepme'").fetchone()
    assert row["name"] == "Keep Me" and row["url_verified"] == 1
    assert dbm.migrate(conn) == 0, "migrate must be idempotent"


def test_migrate_is_safe_on_a_fresh_database(tmp_path):
    """init() already applied SCHEMA; migrate must not choke on existing columns."""
    from dataremoval import db as dbm

    conn = dbm.connect(tmp_path / "new.db")
    dbm.init(conn)
    assert dbm.migrate(conn) == 0
    assert dbm.get_meta(conn, "schema_version") == str(dbm.SCHEMA_VERSION)


def test_bundled_catalog_resolves_and_parses():
    """The shipped catalog must be findable through the package, not by
    guessing at a repo layout from __file__."""
    path = catalog.bundled_path()
    assert path.is_file()
    assert len(catalog.load_yaml(path)) > 20


def test_catalog_lives_inside_the_package():
    """It has to sit under the package directory or setuptools package-data
    cannot ship it - a wheel built with it at the repo root contained no
    catalog at all."""
    import dataremoval

    pkg_dir = Path(dataremoval.__file__).resolve().parent
    assert catalog.bundled_path().resolve().is_relative_to(pkg_dir) if hasattr(
        Path, "is_relative_to"
    ) else str(catalog.bundled_path().resolve()).startswith(str(pkg_dir))


def test_package_data_is_declared():
    """Guards the other half of the packaging bug: the file being in the right
    place does nothing if pyproject does not declare it."""
    import re

    toml = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    assert re.search(r"\[tool\.setuptools\.package-data\]", toml)
    assert re.search(r"dataremoval\s*=\s*\[[^\]]*data/\*\.yaml", toml)


def test_init_upgrades_an_older_database_in_place(tmp_path):
    """The path that actually broke: init() on an existing older database.
    Calling migrate() directly did not catch it, because the failure was an
    index in SCHEMA referencing a column migrate had not added yet."""
    from dataremoval import db as dbm

    path = tmp_path / "v2.db"
    conn = dbm.connect(path)
    # A v2 database: everything except event.broker_key.
    tables = dbm.SCHEMA_TABLES.replace(
        "    broker_key    TEXT,                        -- set when the event has no request\n", ""
    )
    conn.executescript(tables)
    conn.execute("INSERT INTO meta(k,v) VALUES('schema_version','2')")
    conn.execute("INSERT INTO broker(key,name) VALUES('b','B')")
    conn.execute("INSERT INTO request(broker_key) VALUES('b')")
    conn.execute("INSERT INTO event(request_id,at,kind,detail) "
                 "VALUES(1,'2026-01-01','status','pending')")
    conn.commit()
    conn.close()

    conn = dbm.connect(path)
    dbm.init(conn)          # must not raise
    assert dbm.get_meta(conn, "schema_version") == str(dbm.SCHEMA_VERSION)
    assert "broker_key" in [r[1] for r in conn.execute("PRAGMA table_info(event)")]
    assert conn.execute("SELECT COUNT(*) c FROM event").fetchone()["c"] == 1
    idx = [r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")]
    assert "idx_event_broker" in idx, "index on the migrated column was never created"
