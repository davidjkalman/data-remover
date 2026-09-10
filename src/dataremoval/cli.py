"""Command line entry point."""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional

from . import catalog, db, laws, registry, templates, verify
from .profile import Profile, summary, write_template

HOME = Path(os.environ.get("DR_HOME", Path.home() / ".dataremoval"))
DB_PATH = HOME / "removal.db"
PROFILE_PATH = HOME / "profile.yaml"
# Catalog location is the packaging layer's business, not the CLI's.
CATALOG_PATH = None

TODAY = date.today


# ---------------------------------------------------------------- formatting

def table(rows: List[List[str]], headers: List[str]) -> str:
    if not rows:
        return "(nothing)"
    widths = [len(h) for h in headers]
    for r in rows:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(str(cell)))
    line = lambda cells: "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(cells)).rstrip()
    out = [line(headers), "  ".join("-" * w for w in widths)]
    out += [line(r) for r in rows]
    return "\n".join(out)


def parse_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    return datetime.fromisoformat(s).date()


def days_until(s: Optional[str]) -> Optional[int]:
    d = parse_date(s)
    return None if d is None else (d - TODAY()).days


# ---------------------------------------------------------------- helpers

def open_db() -> sqlite3.Connection:
    if not DB_PATH.exists():
        sys.exit(f"No database at {DB_PATH}. Run `dr init` first.")
    return db.connect(DB_PATH)


def load_profile() -> Profile:
    p = Profile.load(PROFILE_PATH)
    if p.is_placeholder():
        sys.exit(
            f"{PROFILE_PATH} still has template values.\n"
            "Fill it in before generating requests - a request with placeholder "
            "identity data is worse than none, it just teaches the broker nothing."
        )
    return p


def get_broker(conn: sqlite3.Connection, key: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM broker WHERE key = ?", (key,)).fetchone()
    if row is None:
        near = conn.execute(
            "SELECT key FROM broker WHERE key LIKE ? OR name LIKE ? LIMIT 5",
            (f"%{key}%", f"%{key}%"),
        ).fetchall()
        hint = ("  Did you mean: " + ", ".join(r["key"] for r in near)) if near else ""
        sys.exit(f"Unknown broker '{key}'.{hint}")
    return row


def get_request(conn: sqlite3.Connection, rid: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT r.*, b.name AS broker_name, b.email AS broker_email, "
        "b.optout_url, b.method, b.requires, b.recheck_days "
        "FROM request r JOIN broker b ON b.key = r.broker_key WHERE r.id = ?",
        (rid,),
    ).fetchone()
    if row is None:
        sys.exit(f"No request #{rid}.")
    return row


def now_stamp() -> str:
    """Second-resolution ISO timestamp. Dates alone made same-day events
    unorderable by eye, which is most of them."""
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def log_event(
    conn: sqlite3.Connection,
    rid: Optional[int],
    kind: str,
    detail: str = "",
    broker_key: Optional[str] = None,
) -> None:
    conn.execute(
        "INSERT INTO event(request_id, broker_key, at, kind, detail) VALUES(?,?,?,?,?)",
        (rid, broker_key, now_stamp(), kind, detail),
    )


def law_for(p: Profile) -> laws.Law:
    return laws.choose(p.residency_state, p.is_eu_resident)


# ---------------------------------------------------------------- commands

def cmd_init(args) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    conn = db.connect(DB_PATH)
    db.init(conn)
    stats = catalog.sync(conn, catalog.load_yaml(args.catalog or catalog.bundled_path()))
    print(f"Database  {DB_PATH}")
    print(f"Catalog   +{stats['added']} new, {stats['updated']} refreshed")

    if not PROFILE_PATH.exists():
        write_template(PROFILE_PATH)
        try:
            os.chmod(PROFILE_PATH, 0o600)
        except OSError:
            pass
        print(f"Profile   {PROFILE_PATH}  (template written - fill this in next)")
    else:
        print(f"Profile   {PROFILE_PATH}")

    print(
        "\nNext:\n"
        f"  1. Edit {PROFILE_PATH} - address history is the part that matters.\n"
        "  2. `dr profile` to check it.\n"
        "  3. `dr queue` for the work list, highest leverage first."
    )


def cmd_profile(args) -> None:
    if args.edit:
        editor = os.environ.get("EDITOR", "vi")
        os.execvp(editor, [editor, str(PROFILE_PATH)])
    p = Profile.load(PROFILE_PATH)
    print(summary(p))
    law = law_for(p)
    print(f"\nStatute     {law.name}")
    print(f"            {law.citation}")
    print(f"Deadline    {law.deadline_text()}")
    print(f"Regulator   {law.regulator}")
    warn = p.warnings()
    if warn:
        print("\nGaps:")
        for w in warn:
            print(f"  ! {w}")


def cmd_brokers(args) -> None:
    conn = open_db()
    sql = (
        "SELECT b.*, ("
        "  SELECT r.status FROM request r WHERE r.broker_key = b.key "
        "  ORDER BY r.id DESC LIMIT 1) AS status "
        "FROM broker b WHERE b.active = 1"
    )
    params: List = []
    if args.tier:
        sql += " AND b.tier = ?"
        params.append(args.tier)
    if args.method:
        sql += " AND b.method = ?"
        params.append(args.method)
    if args.source:
        sql += " AND b.source = ?"
        params.append(args.source)
    sql += " ORDER BY b.tier, b.name"
    rows = conn.execute(sql, params).fetchall()
    if args.todo:
        rows = [r for r in rows if r["status"] is None]
    print(
        table(
            [
                [
                    r["key"],
                    r["name"][:34],
                    r["tier"],
                    r["method"],
                    r["requires"] or "-",
                    (r["verify_status"] or "?"),
                    r["status"] or "-",
                ]
                for r in rows
            ],
            ["KEY", "NAME", "T", "METHOD", "HURDLES", "URL", "STATUS"],
        )
    )
    print(f"\n{len(rows)} broker(s)")


def cmd_queue(args) -> None:
    """What to do next, ordered by leverage: tier, then fewest hurdles."""
    conn = open_db()
    rows = conn.execute(
        "SELECT b.*, ("
        "  SELECT r.status FROM request r WHERE r.broker_key = b.key "
        "  ORDER BY r.id DESC LIMIT 1) AS status "
        "FROM broker b WHERE b.active = 1 ORDER BY b.tier, b.name"
    ).fetchall()

    todo = [r for r in rows if r["status"] is None]
    todo.sort(key=lambda r: (r["tier"], len([x for x in (r["requires"] or "").split(",") if x])))

    limit = args.limit or 10
    print(f"Next {min(limit, len(todo))} of {len(todo)} not yet started:\n")
    print(
        table(
            [
                [r["key"], r["name"][:32], r["tier"], r["method"], r["requires"] or "none"]
                for r in todo[:limit]
            ],
            ["KEY", "NAME", "T", "METHOD", "HURDLES"],
        )
    )
    if todo:
        print(f"\nStart one:  dr start {todo[0]['key']}")


def cmd_start(args) -> None:
    conn = open_db()
    p = load_profile()
    b = get_broker(conn, args.broker)
    law = law_for(p)

    existing = conn.execute(
        "SELECT * FROM request WHERE broker_key = ? ORDER BY id DESC LIMIT 1", (b["key"],)
    ).fetchone()
    if existing and existing["status"] not in db.TERMINAL and not args.again:
        sys.exit(
            f"Request #{existing['id']} for {b['name']} is already open "
            f"(status: {existing['status']}). Use --again to open another."
        )

    urls = "\n".join(args.url or [])
    cur = conn.execute(
        "INSERT INTO request(broker_key, status, law, channel, opened_at, profile_urls) "
        "VALUES(?,?,?,?,?,?)",
        (b["key"], "pending", law.key, args.channel or b["method"], TODAY().isoformat(), urls),
    )
    rid = cur.lastrowid
    log_event(conn, rid, "status", "pending")
    conn.commit()

    print(f"Request #{rid} - {b['name']}  [{law.name}]")
    print("=" * 60)
    if b["notes"]:
        print(f"\nNote: {b['notes'].strip()}")
    if b["requires"]:
        print(f"Hurdles: {b['requires']}")
    if b["feeds"]:
        print(f"Also removes from (verify separately): {b['feeds']}")

    channel = args.channel or b["method"]
    if channel == "email" and b["email"]:
        print(f"\nSend to: {b['email']}\n")
        print(templates.deletion_request(p, law, b["name"], args.url))
    else:
        if b["optout_url"]:
            print(f"\nForm: {b['optout_url']}")
            if not b["url_verified"]:
                print("      (URL unverified - if it 404s, fix it with `dr broker-set`)")
        print()
        print(templates.form_crib(p, b["name"], law))
        if b["email"]:
            print(f"Fallback if the form fails - email {b['email']} with:\n")
            print(templates.deletion_request(p, law, b["name"], args.url))

    print("=" * 60)
    print(f"When submitted:  dr sent {rid}")
    print(f"If it walls you: dr log {rid} --status blocked --note 'wants government ID'")


def cmd_sent(args) -> None:
    conn = open_db()
    r = get_request(conn, args.id)
    law = laws.ALL.get(r["law"]) or laws.for_state(load_profile().residency_state)
    sent = parse_date(args.on) or TODAY()
    due = sent + timedelta(days=law.deadline_days)
    conn.execute(
        "UPDATE request SET status='sent', sent_at=?, due_at=?, confirmation=COALESCE(?, confirmation) "
        "WHERE id=?",
        (sent.isoformat(), due.isoformat(), args.ref, args.id),
    )
    log_event(conn, args.id, "status", f"sent via {r['channel']}" + (f" ref={args.ref}" if args.ref else ""))
    conn.commit()
    print(f"#{args.id} {r['broker_name']}: sent {sent.isoformat()}, due {due.isoformat()} ({law.deadline_days}d).")
    if "email_verify" in (r["requires"] or ""):
        print("Watch your contact inbox - this one needs you to click a verification link.")


def cmd_log(args) -> None:
    conn = open_db()
    r = get_request(conn, args.id)
    if args.status:
        if args.status not in db.STATUSES:
            sys.exit(f"Status must be one of: {', '.join(db.STATUSES)}")
        fields = ["status=?"]
        params: List = [args.status]
        if args.status == "completed":
            fields.append("closed_at=?")
            params.append(TODAY().isoformat())
            recheck = TODAY() + timedelta(days=r["recheck_days"] or 180)
            fields.append("recheck_at=?")
            params.append(recheck.isoformat())
        if args.status in db.TERMINAL and args.status != "completed":
            fields.append("closed_at=?")
            params.append(TODAY().isoformat())
        params.append(args.id)
        conn.execute(f"UPDATE request SET {', '.join(fields)} WHERE id=?", params)
        log_event(conn, args.id, "status", args.status + (f": {args.note}" if args.note else ""))
        print(f"#{args.id} {r['broker_name']} -> {args.status}")
        if args.status == "completed":
            print(f"Recheck scheduled for {(TODAY() + timedelta(days=r['recheck_days'] or 180)).isoformat()}.")
        if args.status == "rejected":
            print(f"Refused. Generate a complaint: dr escalate {args.id}")
    elif args.note:
        log_event(conn, args.id, "note", args.note)
        print(f"#{args.id} note recorded.")
    else:
        sys.exit("Give --status and/or --note.")
    if args.ref:
        conn.execute("UPDATE request SET confirmation=? WHERE id=?", (args.ref, args.id))
    conn.commit()


def cmd_status(args) -> None:
    conn = open_db()
    if args.id:
        r = get_request(conn, args.id)
        print(f"#{r['id']}  {r['broker_name']}  [{r['status']}]")
        print(f"  law {r['law']}   channel {r['channel']}")
        print(f"  opened {r['opened_at'] or '-'}   sent {r['sent_at'] or '-'}   due {r['due_at'] or '-'}")
        if r["confirmation"]:
            print(f"  ref {r['confirmation']}")
        if r["profile_urls"]:
            print("  listings:")
            for u in r["profile_urls"].splitlines():
                print(f"    {u}")
        evs = conn.execute(
            "SELECT * FROM event WHERE request_id=? ORDER BY id", (args.id,)
        ).fetchall()
        print("\n  history:")
        for e in evs:
            print(f"    {e['at']}  {e['kind']:<10} {e['detail']}")
        return

    rows = conn.execute(
        "SELECT r.*, b.name AS broker_name FROM request r JOIN broker b ON b.key=r.broker_key "
        "ORDER BY r.status, r.id"
    ).fetchall()
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    total = conn.execute("SELECT COUNT(*) c FROM broker WHERE active=1").fetchone()["c"]

    print(
        table(
            [
                [
                    r["id"],
                    r["broker_name"][:28],
                    r["status"],
                    r["sent_at"] or "-",
                    r["due_at"] or "-",
                    ("overdue" if (days_until(r["due_at"]) or 0) < 0 and r["status"] not in db.TERMINAL
                     else (f"{days_until(r['due_at'])}d" if r["due_at"] and r["status"] not in db.TERMINAL else "-")),
                ]
                for r in rows
            ],
            ["#", "BROKER", "STATUS", "SENT", "DUE", "LEFT"],
        )
    )
    done = counts.get("completed", 0)
    print(f"\n{len(rows)} request(s) across {total} brokers   |   " +
          "  ".join(f"{k}:{v}" for k, v in sorted(counts.items())))
    print(f"Coverage: {done}/{total} brokers confirmed removed ({100 * done // max(total, 1)}%)")



# Icons keep a long timeline scannable; the kind is still spelled out.
EVENT_MARK = {
    "status": "*", "note": "-", "escalation": "!", "recheck": "~", "verify": "?",
}


def cmd_history(args) -> None:
    """Everything that has happened, newest last.

    Per-request history is in `dr status <id>`; this is the cross-cutting view -
    what did I actually do, and when.
    """
    conn = open_db()
    db.migrate(conn)

    sql = (
        "SELECT e.*, b.name AS broker_name, r.broker_key AS req_broker "
        "FROM event e "
        "LEFT JOIN request r ON r.id = e.request_id "
        "LEFT JOIN broker b ON b.key = COALESCE(e.broker_key, r.broker_key) "
        "WHERE 1=1"
    )
    params: List = []
    if args.broker:
        get_broker(conn, args.broker)
        sql += " AND COALESCE(e.broker_key, r.broker_key) = ?"
        params.append(args.broker)
    if args.request:
        sql += " AND e.request_id = ?"
        params.append(args.request)
    if args.kind:
        sql += " AND e.kind = ?"
        params.append(args.kind)
    if args.since:
        sql += " AND e.at >= ?"
        params.append(args.since)
    if args.no_verify:
        sql += " AND e.kind != 'verify'"

    filtered = bool(params)

    # Newest first out of the DB so --limit keeps the RECENT rows, then flipped
    # for display so the timeline reads forwards.
    limit = args.limit or 50
    sql += " ORDER BY e.at DESC, e.id DESC LIMIT ?"
    params.append(limit)
    rows = list(reversed(conn.execute(sql, params).fetchall()))

    if not rows:
        print("No matching history." if filtered else "No history yet.")
        return

    total = conn.execute("SELECT COUNT(*) c FROM event").fetchone()["c"]
    last_day = None
    for e in rows:
        day = (e["at"] or "")[:10]
        if day != last_day:
            print(f"\n{day}")
            last_day = day
        clock = (e["at"] or "")[11:16] or "  -  "
        who = e["broker_name"] or e["broker_key"] or "-"
        ref = f"#{e['request_id']}" if e["request_id"] else "  "
        mark = EVENT_MARK.get(e["kind"], " ")
        print(f"  {clock}  {mark} {ref:<4} {who[:26]:<26} {e['kind']:<10} {e['detail'][:70]}")

    # Only blame --limit when --limit is actually what truncated the list; a
    # filter matching one event is not a truncated view.
    shown = len(rows)
    truncated = shown == limit
    if filtered:
        line = f"\n{shown} event(s) matched, of {total} total."
    else:
        line = f"\n{shown} of {total} event(s)."
    if truncated:
        line += f"  Showing the most recent {limit}; raise --limit for more."
    print(line)


def cmd_due(args) -> None:
    """Overdue requests, with the follow-up letter ready to send."""
    conn = open_db()
    p = load_profile()
    rows = conn.execute(
        "SELECT r.*, b.name AS broker_name, b.email AS broker_email "
        "FROM request r JOIN broker b ON b.key=r.broker_key "
        "WHERE r.due_at IS NOT NULL AND r.status NOT IN ('completed','rejected','not_found') "
        "ORDER BY r.due_at"
    ).fetchall()
    overdue = [r for r in rows if (days_until(r["due_at"]) or 0) < 0]
    soon = [r for r in rows if 0 <= (days_until(r["due_at"]) or 99) <= (args.within or 7)]

    if soon:
        print("Due soon:")
        print(table([[r["id"], r["broker_name"][:30], r["due_at"], f"{days_until(r['due_at'])}d"]
                     for r in soon], ["#", "BROKER", "DUE", "LEFT"]))
        print()
    if not overdue:
        print("Nothing overdue.")
        return

    print(f"{len(overdue)} overdue:\n")
    print(table([[r["id"], r["broker_name"][:30], r["sent_at"], f"{-days_until(r['due_at'])}d over"]
                 for r in overdue], ["#", "BROKER", "SENT", "OVERDUE"]))

    if args.draft:
        law = law_for(p)
        for r in overdue:
            print("\n" + "=" * 60)
            print(f"To: {r['broker_email'] or '(no email on file - use their form)'}")
            print()
            print(templates.followup(
                p, law, r["broker_name"], parse_date(r["sent_at"]) or TODAY(),
                -(days_until(r["due_at"]) or 0), r["confirmation"],
            ))
    else:
        print("\nDraft the follow-ups:  dr due --draft")


def cmd_escalate(args) -> None:
    conn = open_db()
    p = load_profile()
    r = get_request(conn, args.id)
    law = laws.ALL.get(r["law"]) or law_for(p)
    if not r["sent_at"]:
        sys.exit(f"#{args.id} was never marked sent - nothing to escalate yet.")
    evs = conn.execute("SELECT * FROM event WHERE request_id=? ORDER BY id", (args.id,)).fetchall()
    history = [f"{e['at']}  {e['kind']}: {e['detail']}" for e in evs]
    overdue = -(days_until(r["due_at"]) or 0)
    print(templates.complaint(p, law, r["broker_name"], parse_date(r["sent_at"]), overdue, history))
    log_event(conn, args.id, "escalation", f"complaint drafted for {law.regulator}")
    conn.commit()


def cmd_recheck(args) -> None:
    """Brokers re-list. This is the loop that makes removal stick."""
    conn = open_db()
    rows = conn.execute(
        "SELECT r.*, b.name AS broker_name, b.optout_url FROM request r "
        "JOIN broker b ON b.key=r.broker_key "
        "WHERE r.status='completed' AND r.recheck_at IS NOT NULL ORDER BY r.recheck_at"
    ).fetchall()
    duerows = [r for r in rows if (days_until(r["recheck_at"]) or 0) <= 0]
    if not duerows:
        nxt = rows[0]["recheck_at"] if rows else None
        print("No rechecks due." + (f" Next: {nxt}." if nxt else ""))
        return
    print(f"{len(duerows)} broker(s) due for a recheck - search yourself on each:\n")
    print(table([[r["id"], r["broker_name"][:30], r["closed_at"], r["optout_url"] or "-"]
                 for r in duerows], ["#", "BROKER", "REMOVED", "URL"]))
    print("\nStill gone:   dr log <id> --note 'recheck clear' && dr postpone <id>")
    print("Back again:   dr log <id> --status reappeared --note 'relisted' ; dr start <key> --again")
    if args.open:
        for r in duerows:
            if r["optout_url"]:
                webbrowser.open(r["optout_url"])


def cmd_postpone(args) -> None:
    conn = open_db()
    r = get_request(conn, args.id)
    days = args.days or r["recheck_days"] or 180
    nxt = TODAY() + timedelta(days=days)
    conn.execute("UPDATE request SET recheck_at=? WHERE id=?", (nxt.isoformat(), args.id))
    log_event(conn, args.id, "recheck", f"clear; next {nxt.isoformat()}")
    conn.commit()
    print(f"#{args.id} {r['broker_name']}: next recheck {nxt.isoformat()}")



def copy_to_clipboard(text: str) -> Optional[str]:
    """Best-effort clipboard. Returns the tool used, or None."""
    for cmd in (["pbcopy"], ["xclip", "-selection", "clipboard"], ["wl-copy"]):
        if not shutil.which(cmd[0]):
            continue
        try:
            subprocess.run(cmd, input=text.encode(), check=True)
            return cmd[0]
        except (OSError, subprocess.CalledProcessError):
            continue
    return None


BATCH_KEYS = """
  [enter] submitted it        s  skip for now
  b       blocked (ID/captcha wall)   e  use the email channel instead
  c       re-copy the crib sheet      o  re-open the page
  q       stop here
"""


def cmd_batch(args) -> None:
    """Work through opt-out forms one at a time, in a real browser.

    No automation: the browser is yours, the captcha is yours, the submit
    button is yours. What this removes is the part that actually makes people
    quit - looking up the URL, retyping the same twelve fields, and remembering
    which of forty sites you already did.
    """
    conn = open_db()
    db.migrate(conn)
    p = load_profile()
    law = law_for(p)

    sql = (
        "SELECT b.* FROM broker b WHERE b.active = 1 "
        "AND b.optout_url IS NOT NULL AND b.method IN ('form','account') "
        # Anything with a live request is already in flight.
        "AND NOT EXISTS (SELECT 1 FROM request r WHERE r.broker_key = b.key "
        "                AND r.status NOT IN ('completed','rejected','not_found'))"
    )
    params: List = []
    if not args.include_dead:
        sql += " AND COALESCE(b.verify_status,'') NOT IN ('notfound','soft404')"
    if args.tier:
        sql += " AND b.tier = ?"
        params.append(args.tier)
    if args.source:
        sql += " AND b.source = ?"
        params.append(args.source)
    sql += " ORDER BY b.tier, b.name"
    rows = conn.execute(sql, params).fetchall()

    # Same leverage ordering as `dr queue`: tier, then fewest hurdles.
    rows = sorted(rows, key=lambda r: (r["tier"],
                                       len([x for x in (r["requires"] or "").split(",") if x])))
    todo = rows[: args.limit or 10]

    if not todo:
        print("Nothing queued for a browser session.")
        print("Everything openable is either in flight, done, or a known dead link.")
        print("  dr queue                  # what is left overall")
        print("  dr batch --include-dead   # include links verify flagged as gone")
        return

    print(f"{len(todo)} site(s) this session (of {len(rows)} available)  [{law.name}]")
    print("Each one: crib sheet on your clipboard, page in your browser, you submit.")
    if not args.dry_run:
        print(BATCH_KEYS)

    if args.dry_run or not sys.stdin.isatty():
        if not args.dry_run:
            print("\n(stdin is not a terminal - showing the plan only)")
        print(table([[r["key"], r["name"][:32], r["tier"], r["requires"] or "none",
                      r["verify_status"] or "?"] for r in todo],
                    ["KEY", "NAME", "T", "HURDLES", "URL"]))
        return

    done = {"sent": 0, "blocked": 0, "skipped": 0, "email": 0}
    for i, b in enumerate(todo, 1):
        crib = templates.form_crib(p, b["name"], law)
        print("\n" + "=" * 68)
        print(f"[{i}/{len(todo)}]  {b['name']}   (tier {b['tier']})")
        if b["notes"]:
            print(f"  note:    {' '.join(b['notes'].split())[:220]}")
        if b["requires"]:
            print(f"  hurdles: {b['requires']}")
        if b["verify_status"] and b["verify_status"] != "ok":
            print(f"  warning: last link check said {b['verify_status']}")
        print(f"  url:     {b['optout_url']}")

        clip = copy_to_clipboard(crib)
        if clip:
            print(f"  crib sheet copied to clipboard ({clip})")
        else:
            print(crib)
        webbrowser.open(b["optout_url"])

        while True:
            try:
                choice = input("  > ").strip().lower()
            except EOFError:
                choice = "q"

            if choice in ("", "y", "yes"):
                rid = _open_request(conn, b, law, args.channel or b["method"])
                cmd_sent(argparse.Namespace(id=rid, on=None, ref=None))
                done["sent"] += 1
                break
            if choice == "s":
                done["skipped"] += 1
                break
            if choice == "b":
                rid = _open_request(conn, b, law, b["method"])
                note = input("  what wall? ").strip() or "blocked at the form"
                cmd_log(argparse.Namespace(id=rid, status="blocked", note=note, ref=None))
                done["blocked"] += 1
                break
            if choice == "e":
                if not b["email"]:
                    print("  no privacy email on file for this one.")
                    continue
                rid = _open_request(conn, b, law, "email")
                print(f"\n  Send to: {b['email']}\n")
                print(templates.deletion_request(p, law, b["name"]))
                if copy_to_clipboard(templates.deletion_request(p, law, b["name"])):
                    print("  (letter copied to clipboard)")
                print(f"  mark it sent with:  dr sent {rid}")
                done["email"] += 1
                break
            if choice == "c":
                clip = copy_to_clipboard(crib)
                print(f"  copied again ({clip})" if clip else crib)
                continue
            if choice == "o":
                webbrowser.open(b["optout_url"])
                continue
            if choice == "q":
                print("\nStopped.")
                _batch_summary(conn, done)
                return
            print("  ? " + " ".join(BATCH_KEYS.split()))

    _batch_summary(conn, done)


def _open_request(conn: sqlite3.Connection, broker, law, channel: str) -> int:
    cur = conn.execute(
        "INSERT INTO request(broker_key, status, law, channel, opened_at) "
        "VALUES(?,?,?,?,?)",
        (broker["key"], "pending", law.key, channel, TODAY().isoformat()),
    )
    rid = cur.lastrowid
    log_event(conn, rid, "status", f"pending (batch session, {channel})")
    conn.commit()
    return rid


def _batch_summary(conn: sqlite3.Connection, done: Dict[str, int]) -> None:
    print("\n" + "=" * 68)
    print("  ".join(f"{k}: {v}" for k, v in done.items() if v))
    total = conn.execute("SELECT COUNT(*) c FROM broker WHERE active=1").fetchone()["c"]
    sent = conn.execute(
        "SELECT COUNT(DISTINCT broker_key) c FROM request WHERE sent_at IS NOT NULL"
    ).fetchone()["c"]
    print(f"Reached {sent} of {total} brokers.")
    if done.get("sent"):
        print("Deadlines are running now:  dr due")
    print("Next session:  dr batch")


def cmd_open(args) -> None:
    conn = open_db()
    b = get_broker(conn, args.broker)
    if not b["optout_url"]:
        sys.exit(f"{b['name']} has no opt-out URL on file (method: {b['method']}).")
    print(b["optout_url"])
    if not args.print_only:
        webbrowser.open(b["optout_url"])



def cmd_import_registry(args) -> None:
    """Bulk-import the California data broker registry.

    Registration is mandatory for brokers doing business in California, so this
    is the authoritative list of who they are. It is *not* authoritative about
    opt-out URLs - see the confidence breakdown it prints.
    """
    conn = open_db()
    db.migrate(conn)

    src = Path(args.file) if args.file else None
    print(f"Reading {src if src else args.url}...")
    try:
        entries = registry.load(src, args.url)
    except (OSError, ValueError) as e:
        sys.exit(f"Could not read the registry: {e}")
    print(f"{len(entries)} registrants.\n")

    # Dedupe on domain, not name: 'Intelius' and 'PeopleConnect Inc.' are one
    # submission target, and registrants love a trading name.
    # One domain can map to several catalog entries - an alias like `anywho`
    # points at Spokeo's form, so both carry domain spokeo.com. Prefer the entry
    # that actually *is* the domain, and be deterministic about ties, so the
    # match we report is the one a human would name.
    existing_dom: Dict[str, str] = {}
    for r in conn.execute(
        "SELECT key, domain FROM broker WHERE domain IS NOT NULL ORDER BY key"
    ).fetchall():
        dom, key = r["domain"], r["key"]
        held = existing_dom.get(dom)
        if held is None or (key in dom and held not in dom):
            existing_dom[dom] = key
    existing_keys = {
        r["key"] for r in conn.execute("SELECT key FROM broker").fetchall()
    }

    new, dupes, keyless = [], [], []
    for e in entries:
        if e.domain and e.domain in existing_dom:
            dupes.append((e, existing_dom[e.domain]))
        elif e.key in existing_keys:
            dupes.append((e, e.key))
        elif not e.domain:
            keyless.append(e)
        else:
            new.append(e)

    if args.limit:
        new = new[: args.limit]

    with_url = sum(1 for e in new if e.optout_url)
    confident = sum(1 for e in new if e.optout_url and "keyword" in e.url_basis)
    unverified = sum(1 for e in new if e.optout_url and "unverified" in e.url_basis)

    print(f"  {len(new):>4}  new")
    print(f"  {len(dupes):>4}  already covered (matched on domain)")
    if keyless:
        print(f"  {len(keyless):>4}  skipped - no usable website domain")
    print()
    print("  Opt-out URL confidence for the new ones:")
    print(f"    {confident:>4}  URL matched an opt-out keyword")
    print(f"    {with_url - confident - unverified:>4}  single URL in the cell")
    print(f"    {unverified:>4}  first of several URLs - unverified guess")
    print(f"    {len(new) - with_url:>4}  no URL; email channel only")

    if args.show:
        print("\n  Sample:")
        for e in new[: args.show]:
            print(f"    {e.key:<28} {e.name[:38]:<40} {e.optout_url or '(email)'}")

    if dupes and args.show:
        print("\n  Already covered:")
        for e, key in dupes[: args.show]:
            print(f"    {e.name[:40]:<42} -> {key}")

    if args.dry_run:
        print("\nDry run - nothing written. Re-run without --dry-run to import.")
        return

    if not new:
        print("\nNothing to import.")
        return

    brokers = [e.as_broker(tier=args.tier, recheck_days=args.recheck_days) for e in new]
    inserted = 0
    for e, b in zip(new, brokers):
        conn.execute(
            "INSERT INTO broker(key,name,tier,method,optout_url,email,requires,"
            "recheck_days,feeds,notes,source,domain) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (b["key"], b["name"], b["tier"], b["method"], b["optout_url"], b["email"],
             b["requires"], b["recheck_days"], b["feeds"], b["notes"],
             "ca-registry", e.domain),
        )
        inserted += 1
    log_event(conn, None, "note",
              f"imported {inserted} brokers from the CA registry "
              f"({len(dupes)} already covered)")
    conn.commit()

    total = conn.execute("SELECT COUNT(*) c FROM broker WHERE active=1").fetchone()["c"]
    print(f"\nImported {inserted}. Catalog is now {total} brokers.")
    print("\nThese are unverified by definition - the registry's opt-out column is")
    print("free text. Check the links before working through them:")
    print(f"  dr verify --source ca-registry        # {inserted} URLs, paced")
    print("  dr brokers --source ca-registry --todo")


def cmd_verify(args) -> None:
    """Read-only sweep of opt-out URLs. Submits nothing."""
    conn = open_db()
    db.migrate(conn)

    sql = ("SELECT key, name, optout_url, verify_at, verify_status FROM broker "
           "WHERE active = 1")
    params: List = []
    if args.broker:
        marks = ",".join("?" * len(args.broker))
        sql += f" AND key IN ({marks})"
        params += args.broker
        for k in args.broker:
            get_broker(conn, k)          # fail loudly on a typo
    if args.tier:
        sql += " AND tier = ?"
        params.append(args.tier)
    if args.source:
        sql += " AND source = ?"
        params.append(args.source)
    sql += " ORDER BY tier, name"
    rows = conn.execute(sql, params).fetchall()

    # Default: only what has never been checked, or was checked long enough ago
    # to have rotted. --all re-checks everything.
    if not args.all and not args.broker:
        stale_cutoff = (TODAY() - timedelta(days=args.stale or 90)).isoformat()
        rows = [r for r in rows
                if not r["verify_at"] or r["verify_at"] < stale_cutoff
                or r["verify_status"] in ("error", "botwall")]

    # --resume picks up an interrupted sweep: anything already checked today is
    # done. Results are written as they land, so this is exact, not a guess.
    if args.resume:
        today = TODAY().isoformat()
        before = len(rows)
        rows = [r for r in rows if r["verify_at"] != today]
        if before != len(rows):
            print(f"Resuming: {before - len(rows)} already checked today, "
                  f"{len(rows)} to go.\n")

    if not rows:
        print("Nothing to verify. `dr verify --all` re-checks everything.")
        return

    todo = [(r["key"], r["name"], r["optout_url"]) for r in rows]
    eta = len(todo) / args.rate if args.rate else 0
    print(f"Checking {len(todo)} opt-out URL(s): {args.workers} workers, "
          f"{args.rate:g}/s cap, {args.host_gap:g}s per host"
          + (f", ~{eta / 60:.0f} min" if eta > 90 else "") + ".")
    print("Read-only - nothing is submitted. Ctrl+C is safe; "
          "progress is saved as it goes.\n", flush=True)

    now = TODAY().isoformat()
    prior = {r["key"]: r["verify_status"] for r in rows}
    applied = [0]
    done = [0]
    width = len(str(len(todo)))

    def record(res: verify.Result) -> None:
        """Persist immediately. A sweep of hundreds will be interrupted, and
        work already paid for should survive it."""
        done[0] += 1
        conn.execute(
            "UPDATE broker SET verify_status=?, verify_at=?, verify_url=?, verify_note=? "
            "WHERE key=?",
            (res.status, now, res.final_url, res.note, res.key),
        )
        # Only a clean hit earns the verified flag; anything else clears it so a
        # stale 'verified' never outlives the URL it described.
        conn.execute("UPDATE broker SET url_verified=? WHERE key=?",
                     (1 if res.verified else 0, res.key))
        if res.moved and args.apply and res.final_url:
            conn.execute("UPDATE broker SET optout_url=?, url_verified=1 WHERE key=?",
                         (res.final_url, res.key))
            applied[0] += 1
        conn.commit()

        # Log only what changed. A sweep that finds 45 URLs exactly as they were
        # is one line of history, not 45.
        was = prior.get(res.key)
        if was != res.status:
            log_event(
                conn, None, "verify",
                f"{res.name}: {was or 'unchecked'} -> {res.status} ({res.note})"[:300],
                broker_key=res.key,
            )
            conn.commit()

        mark = {"ok": "ok  ", "moved": "MOVE", "notfound": "GONE", "soft404": "SOFT",
                "botwall": "WALL", "error": "ERR ", "skipped": "--  "}[res.status]
        changed = "" if was == res.status else f"  (was {was or 'unchecked'})"
        print(f"  [{done[0]:>{width}}/{len(todo)}] {mark}  {res.name[:34]:<34} "
              f"{res.note}{changed}", flush=True)

    interrupted = False
    try:
        results = verify.check_many(
            todo, workers=args.workers, timeout=args.timeout,
            rate=args.rate, host_gap=args.host_gap, on_result=record,
        )
    except KeyboardInterrupt:
        results = []
        interrupted = True

    if len(results) < len(todo):
        interrupted = True

    resume_hint = None
    if interrupted:
        resume_hint = "dr verify --resume" + (" --all" if args.all else "")
        print(f"\n  Interrupted after {done[0]} of {len(todo)}. Progress saved.",
              flush=True)
    if not results:
        if resume_hint:
            print(f"  Resume with:  {resume_hint}", flush=True)
            raise SystemExit(130)
        return

    # Summary, worst first.
    by_status = {}
    for r in results:
        by_status.setdefault(r.status, []).append(r)

    print("\n" + "=" * 68)
    counts = "  ".join(f"{s}:{len(by_status[s])}"
                       for s in verify.STATUS_ORDER if s in by_status)
    print(f"{len(results)} checked   {counts}")

    log_event(conn, None, "verify",
              f"swept {len(results)}/{len(todo)} URLs - {counts}"
              + (" (interrupted)" if interrupted else ""))
    conn.commit()

    for status in ["notfound", "soft404", "moved", "error", "botwall"]:
        group = by_status.get(status)
        if not group:
            continue
        print(f"\n{status.upper()} - {verify.EXPLAIN[status]}")
        for r in group:
            print(f"  {r.key:<24} {r.url}")
            if r.final_url and not verify._same_page(r.url or '', r.final_url):
                print(f"  {'':<24}   -> {r.final_url}")
            print(f"  {'':<24}   {r.note}")

    if applied[0]:
        print(f"\nApplied {applied[0]} redirect(s) to the catalog.")
    elif by_status.get("moved"):
        print("\nAdopt the redirect targets:  dr verify --apply " +
              " ".join(r.key for r in by_status["moved"]))

    broken = by_status.get("notfound", []) + by_status.get("soft404", [])
    if broken:
        print("\nDead links need a replacement URL found by hand:")
        for r in broken[:5]:
            print(f"  dr broker-set {r.key} optout_url <new-url>")
        print("  ...then re-run `dr verify " +
              " ".join(r.key for r in broken[:5]) + "`")
    if by_status.get("botwall"):
        print("\nBot-walled sites are not necessarily broken - open one to judge:")
        print(f"  dr open {by_status['botwall'][0].key}")

    if resume_hint:
        print(f"\n{done[0]} of {len(todo)} checked before you stopped. "
              f"Resume with:\n  {resume_hint}", flush=True)
        raise SystemExit(130)


def cmd_broker_set(args) -> None:
    conn = open_db()
    get_broker(conn, args.broker)
    allowed = {"optout_url", "email", "method", "tier", "recheck_days", "notes",
               "url_verified", "active", "name"}
    if args.field not in allowed:
        sys.exit(f"Field must be one of: {', '.join(sorted(allowed))}")
    conn.execute(f"UPDATE broker SET {args.field}=? WHERE key=?", (args.value, args.broker))
    conn.commit()
    print(f"{args.broker}.{args.field} = {args.value}")
    if args.field == "optout_url":
        print("Tip: also mark it checked -> dr broker-set", args.broker, "url_verified 1")


def cmd_report(args) -> None:
    conn = open_db()
    p = Profile.load(PROFILE_PATH)
    rows = conn.execute(
        "SELECT r.*, b.name AS broker_name, b.tier FROM request r "
        "JOIN broker b ON b.key=r.broker_key ORDER BY b.tier, b.name"
    ).fetchall()
    total = conn.execute("SELECT COUNT(*) c FROM broker WHERE active=1").fetchone()["c"]
    done = sum(1 for r in rows if r["status"] == "completed")
    law = law_for(p)

    lines = [
        f"# Data removal report - {TODAY().isoformat()}",
        "",
        f"Subject: {p.full_name}",
        f"Statute relied on: {law.name} ({law.citation})",
        f"Brokers tracked: {total}   Requests opened: {len(rows)}   Confirmed removed: {done}",
        "",
        "| Broker | Tier | Status | Sent | Due | Ref |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        lines.append(
            f"| {r['broker_name']} | {r['tier']} | {r['status']} | {r['sent_at'] or ''} "
            f"| {r['due_at'] or ''} | {r['confirmation'] or ''} |"
        )
    overdue = [r for r in rows
               if r["due_at"] and (days_until(r["due_at"]) or 0) < 0 and r["status"] not in db.TERMINAL]
    if overdue:
        lines += ["", "## Non-compliant (past statutory deadline)", ""]
        for r in overdue:
            lines.append(f"- **{r['broker_name']}** - sent {r['sent_at']}, "
                         f"{-days_until(r['due_at'])} days overdue")
    text = "\n".join(lines) + "\n"
    if args.out:
        Path(args.out).write_text(text)
        print(f"Wrote {args.out}")
    else:
        print(text)


# ---------------------------------------------------------------- wiring

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="dr", description="Personal data-removal tracker.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="create the database and profile template")
    p.add_argument("--catalog", type=Path, help="alternate brokers.yaml")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("profile", help="show or edit your identity profile")
    p.add_argument("--edit", action="store_true")
    p.set_defaults(func=cmd_profile)

    p = sub.add_parser("brokers", help="list the catalog")
    p.add_argument("--tier", type=int, choices=[1, 2, 3])
    p.add_argument("--method", choices=["form", "email", "account", "mail"])
    p.add_argument("--todo", action="store_true", help="only ones with no request yet")
    p.add_argument("--source", help="catalog | ca-registry")
    p.set_defaults(func=cmd_brokers)

    p = sub.add_parser("queue", help="what to do next, highest leverage first")
    p.add_argument("--limit", type=int)
    p.set_defaults(func=cmd_queue)

    p = sub.add_parser("start", help="open a request and print the letter/form crib")
    p.add_argument("broker")
    p.add_argument("--url", action="append", help="a listing URL (repeatable)")
    p.add_argument("--channel", choices=["form", "email", "account", "mail"])
    p.add_argument("--again", action="store_true", help="open another despite an existing one")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("sent", help="mark submitted and start the statutory clock")
    p.add_argument("id", type=int)
    p.add_argument("--on", help="ISO date, default today")
    p.add_argument("--ref", help="their confirmation/ticket number")
    p.set_defaults(func=cmd_sent)

    p = sub.add_parser("log", help="record a status change or note")
    p.add_argument("id", type=int)
    p.add_argument("--status", choices=db.STATUSES)
    p.add_argument("--note")
    p.add_argument("--ref")
    p.set_defaults(func=cmd_log)

    p = sub.add_parser("status", help="overview, or detail for one request")
    p.add_argument("id", type=int, nargs="?")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("history", help="everything that has happened, newest last")
    p.add_argument("--broker", help="only this broker")
    p.add_argument("--request", type=int, help="only this request id")
    p.add_argument("--kind", choices=["status", "note", "escalation", "recheck", "verify"])
    p.add_argument("--since", help="ISO date or datetime lower bound")
    p.add_argument("--no-verify", action="store_true", dest="no_verify",
                   help="hide link-check noise, show only what you did")
    p.add_argument("--limit", type=int, help="most recent N events (default 50)")
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("due", help="deadlines missed or approaching")
    p.add_argument("--draft", action="store_true", help="print follow-up letters")
    p.add_argument("--within", type=int, help="days ahead to warn (default 7)")
    p.set_defaults(func=cmd_due)

    p = sub.add_parser("escalate", help="draft a regulator complaint")
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_escalate)

    p = sub.add_parser("recheck", help="brokers due for re-verification")
    p.add_argument("--open", action="store_true", help="open each in a browser")
    p.set_defaults(func=cmd_recheck)

    p = sub.add_parser("postpone", help="recheck was clear; schedule the next one")
    p.add_argument("id", type=int)
    p.add_argument("--days", type=int)
    p.set_defaults(func=cmd_postpone)

    p = sub.add_parser("batch", help="work through opt-out forms in a browser session")
    p.add_argument("--limit", type=int, help="how many this session (default 10)")
    p.add_argument("--tier", type=int, choices=[1, 2, 3])
    p.add_argument("--source", help="catalog | ca-registry")
    p.add_argument("--channel", choices=["form", "account"])
    p.add_argument("--include-dead", action="store_true", dest="include_dead",
                   help="also offer links verify flagged notfound/soft404")
    p.add_argument("--dry-run", action="store_true", dest="dry_run",
                   help="list the session without opening anything")
    p.set_defaults(func=cmd_batch)

    p = sub.add_parser("open", help="open a broker's opt-out page")
    p.add_argument("broker")
    p.add_argument("--print-only", action="store_true")
    p.set_defaults(func=cmd_open)

    p = sub.add_parser("import-registry",
                       help="bulk-import the California data broker registry")
    p.add_argument("--file", help="read a local CSV instead of fetching")
    p.add_argument("--url", default=registry.REGISTRY_URL)
    p.add_argument("--dry-run", action="store_true", dest="dry_run",
                   help="report what would be imported and write nothing")
    p.add_argument("--tier", type=int, default=3, choices=[1, 2, 3],
                   help="tier for imported brokers (default 3)")
    p.add_argument("--recheck-days", type=int, default=365, dest="recheck_days")
    p.add_argument("--limit", type=int, help="import at most N new brokers")
    p.add_argument("--show", type=int, default=0, metavar="N",
                   help="list N examples of what was matched")
    p.set_defaults(func=cmd_import_registry)

    p = sub.add_parser("verify", help="check opt-out URLs still resolve (read-only)")
    p.add_argument("broker", nargs="*", help="specific brokers; default is unchecked/stale ones")
    p.add_argument("--all", action="store_true", help="re-check every broker")
    p.add_argument("--tier", type=int, choices=[1, 2, 3])
    p.add_argument("--source", help="only brokers from this source (catalog | ca-registry)")
    p.add_argument("--apply", action="store_true", help="adopt redirect targets into the catalog")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--timeout", type=float, default=15.0)
    p.add_argument("--rate", type=float, default=verify.DEFAULT_RATE,
                   help=f"global requests/second cap (default {verify.DEFAULT_RATE:g}; 0 = uncapped)")
    p.add_argument("--host-gap", type=float, default=verify.DEFAULT_HOST_GAP, dest="host_gap",
                   help=f"minimum seconds between requests to one host (default {verify.DEFAULT_HOST_GAP:g})")
    p.add_argument("--resume", action="store_true",
                   help="skip anything already checked today (continue an interrupted sweep)")
    p.add_argument("--stale", type=int, help="re-check anything older than N days (default 90)")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("broker-set", help="correct a catalog field locally")
    p.add_argument("broker")
    p.add_argument("field")
    p.add_argument("value")
    p.set_defaults(func=cmd_broker_set)

    p = sub.add_parser("report", help="markdown status report")
    p.add_argument("--out")
    p.set_defaults(func=cmd_report)

    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except FileNotFoundError as e:
        sys.exit(str(e))
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
