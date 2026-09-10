"""Render the removal campaign as a single self-contained HTML page.

Everything is inlined: no network, no CDN, no telemetry. The file can sit on a
laptop that never goes online, which is the point - it describes what a person
is trying to get deleted, and that is not data to hand to a third party.

Deliberately omits the profile's addresses, phones and emails. The dashboard
answers "how is this going", and it does not need your address history to do
that.
"""

from __future__ import annotations

import html
import json
import sqlite3
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from . import laws
from .profile import Profile

# Status palette. Validated for the OKLCH lightness band, chroma floor, CVD
# separation and surface contrast in both modes - see dataviz validator.
# Status colours are reserved: they never double as a categorical series, and
# every use ships with a text label, never colour alone.
STATUS_GROUP = {
    "completed": "good",
    "not_found": "good",
    "sent": "inflight",
    "pending": "inflight",
    "acknowledged": "inflight",
    "verification_required": "warn",
    "reappeared": "warn",
    "partial": "warn",
    "rejected": "crit",
    "blocked": "wall",
}

STATUS_LABEL = {
    "completed": "Removed",
    "not_found": "No record held",
    "sent": "Awaiting response",
    "pending": "Not yet sent",
    "acknowledged": "Acknowledged",
    "verification_required": "Needs you to verify",
    "partial": "Partly removed",
    "reappeared": "Re-listed",
    "rejected": "Refused",
    "blocked": "Blocked by site",
}

VERIFY_LABEL = {
    "ok": "Link good",
    "moved": "Link moved",
    "soft404": "Link dead (soft)",
    "notfound": "Link dead (404)",
    "botwall": "Bot-walled",
    "error": "Unreachable",
    "skipped": "No link on file",
}


def _days(iso: Optional[str], today: date) -> Optional[int]:
    if not iso:
        return None
    try:
        return (datetime.fromisoformat(iso).date() - today).days
    except ValueError:
        return None


def collect(conn: sqlite3.Connection, profile: Profile, today: Optional[date] = None) -> Dict[str, Any]:
    today = today or date.today()
    law = laws.choose(profile.residency_state, profile.is_eu_resident)

    brokers = conn.execute(
        "SELECT key, name, tier, source, method, verify_status FROM broker WHERE active = 1"
    ).fetchall()
    reqs = conn.execute(
        "SELECT r.*, b.name AS broker_name, b.tier, b.source, b.verify_status "
        "FROM request r JOIN broker b ON b.key = r.broker_key "
        "ORDER BY b.tier, b.name"
    ).fetchall()

    terminal = {"completed", "rejected", "not_found"}
    rows: List[Dict[str, Any]] = []
    for r in reqs:
        left = _days(r["due_at"], today)
        overdue = (
            left is not None and left < 0 and r["status"] not in terminal
        )
        rows.append({
            "id": r["id"],
            "broker": r["broker_name"],
            "key": r["broker_key"],
            "tier": r["tier"],
            "source": r["source"] or "catalog",
            "status": r["status"],
            "group": STATUS_GROUP.get(r["status"], "inflight"),
            "label": STATUS_LABEL.get(r["status"], r["status"]),
            "channel": r["channel"] or "",
            "sent_at": r["sent_at"] or "",
            "due_at": r["due_at"] or "",
            "days_left": left,
            "overdue": overdue,
            "ref": r["confirmation"] or "",
        })

    listings = conn.execute(
        "SELECT request_id, status FROM listing"
    ).fetchall()
    by_req: Dict[int, Dict[str, int]] = {}
    for l in listings:
        d = by_req.setdefault(l["request_id"], {})
        d[l["status"]] = d.get(l["status"], 0) + 1
    for r in rows:
        tal = by_req.get(r["id"], {})
        r["listings_total"] = sum(tal.values())
        r["listings_removed"] = tal.get("removed", 0)

    status_counts: Dict[str, int] = {}
    for r in rows:
        status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1

    verify_counts: Dict[str, int] = {}
    for b in brokers:
        verify_counts[b["verify_status"] or "unchecked"] = (
            verify_counts.get(b["verify_status"] or "unchecked", 0) + 1
        )

    tier_totals: Dict[int, Dict[str, int]] = {}
    for b in brokers:
        t = tier_totals.setdefault(b["tier"], {"total": 0, "reached": 0, "removed": 0})
        t["total"] += 1
    reached_keys = {r["key"] for r in rows if r["sent_at"]}
    removed_keys = {r["key"] for r in rows if r["status"] == "completed"}
    for b in brokers:
        t = tier_totals[b["tier"]]
        if b["key"] in reached_keys:
            t["reached"] += 1
        if b["key"] in removed_keys:
            t["removed"] += 1

    events = conn.execute(
        "SELECT e.*, COALESCE(b.name, e.broker_key, '') AS who FROM event e "
        "LEFT JOIN request r ON r.id = e.request_id "
        "LEFT JOIN broker b ON b.key = COALESCE(e.broker_key, r.broker_key) "
        "ORDER BY e.at DESC, e.id DESC LIMIT 25"
    ).fetchall()

    attention = [r for r in rows if r["overdue"]]
    attention += [r for r in rows if r["status"] == "verification_required"
                  and not r["overdue"]]
    attention += [r for r in rows if r["status"] in ("rejected", "reappeared", "partial")
                  and not r["overdue"]]

    due_soon = sorted(
        [r for r in rows if r["days_left"] is not None
         and 0 <= r["days_left"] <= 14 and r["status"] not in terminal],
        key=lambda r: r["days_left"],
    )

    return {
        "generated": today.isoformat(),
        "is_demo": profile.is_placeholder(),
        "subject": profile.full_name,
        "residency": profile.residency_state or ("EU" if profile.is_eu_resident else "-"),
        "law": {"name": law.name, "citation": law.citation,
                "days": law.deadline_days, "regulator": law.regulator},
        "totals": {
            "brokers": len(brokers),
            "requests": len(rows),
            "reached": len(reached_keys),
            "removed": len(removed_keys),
            "overdue": len([r for r in rows if r["overdue"]]),
            "blocked": len([r for r in rows if r["status"] == "blocked"]),
            "curated": len([b for b in brokers if (b["source"] or "catalog") == "catalog"]),
            "registry": len([b for b in brokers if b["source"] == "ca-registry"]),
            "partial": len([r for r in rows if r["status"] == "partial"]),
            "listings": len(listings),
            "listings_removed": len([l for l in listings if l["status"] == "removed"]),
        },
        "status_counts": status_counts,
        "verify_counts": verify_counts,
        "tiers": {str(k): v for k, v in sorted(tier_totals.items())},
        "rows": rows,
        "attention": attention,
        "due_soon": due_soon,
        "events": [{"at": e["at"], "kind": e["kind"], "who": e["who"],
                    "detail": e["detail"], "rid": e["request_id"]} for e in events],
    }


# --------------------------------------------------------------- rendering

CSS = """
:root {
  color-scheme: light dark;
  --bg:#f2f5f8; --surface:#ffffff; --surface-2:#f7f9fb;
  --ink:#131a24; --ink-2:#3d4a5a; --muted:#5f6b7a; --line:#dbe2ea;
  --good:#06805e; --inflight:#1f5fb0; --warn:#c2760a; --crit:#b3261e; --wall:#6d4d9c;
  --shadow:0 1px 2px rgba(19,26,36,.06), 0 8px 24px -12px rgba(19,26,36,.14);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg:#0e141c; --surface:#161e28; --surface-2:#1b2532;
    --ink:#e3e9f0; --ink-2:#b3bfcd; --muted:#8a95a4; --line:#26313f;
    --good:#17a37c; --inflight:#4a8ede; --warn:#9c9122; --crit:#d43f57; --wall:#8f6fd0;
    --shadow:0 1px 2px rgba(0,0,0,.4), 0 10px 28px -14px rgba(0,0,0,.6);
  }
}
:root[data-theme="dark"] {
  --bg:#0e141c; --surface:#161e28; --surface-2:#1b2532;
  --ink:#e3e9f0; --ink-2:#b3bfcd; --muted:#8a95a4; --line:#26313f;
  --good:#17a37c; --inflight:#4a8ede; --warn:#9c9122; --crit:#d43f57; --wall:#8f6fd0;
  --shadow:0 1px 2px rgba(0,0,0,.4), 0 10px 28px -14px rgba(0,0,0,.6);
}

* { box-sizing:border-box; }
body {
  margin:0; background:var(--bg); color:var(--ink);
  font-family:"IBM Plex Sans","Helvetica Neue",Arial,sans-serif;
  font-size:15px; line-height:1.55;
  -webkit-font-smoothing:antialiased;
}
.wrap { max-width:1120px; margin:0 auto; padding:32px 24px 72px; }

/* ---- header ---- */
header { display:flex; flex-wrap:wrap; gap:20px 32px; align-items:flex-end;
         justify-content:space-between; padding-bottom:20px;
         border-bottom:2px solid var(--ink); margin-bottom:28px; }
h1 { font-size:1.5rem; font-weight:600; letter-spacing:-.01em; margin:0 0 4px;
     text-wrap:balance; }
.subject { font-family:"IBM Plex Mono",ui-monospace,monospace; font-size:.82rem;
           color:var(--muted); }
.statute { font-family:"IBM Plex Serif",Georgia,serif; font-size:.86rem;
           color:var(--ink-2); max-width:44ch; text-align:right; line-height:1.4; }
.statute b { font-weight:600; font-style:normal; display:block; color:var(--ink); }

/* ---- kpi row ---- */
.kpis { display:grid; grid-template-columns:repeat(auto-fit,minmax(184px,1fr));
        gap:1px; background:var(--line); border:1px solid var(--line);
        border-radius:3px; overflow:hidden; margin-bottom:32px; }
.kpi { background:var(--surface); padding:16px 18px 18px; }
.kpi .k { font-size:.68rem; text-transform:uppercase; letter-spacing:.09em;
          color:var(--muted); font-weight:600; }
.kpi .v { font-family:"IBM Plex Mono",ui-monospace,monospace; font-size:1.9rem;
          font-weight:500; line-height:1.15; margin-top:6px;
          font-variant-numeric:tabular-nums; }
.kpi .n { font-size:.76rem; color:var(--muted); margin-top:2px; }
.kpi.is-crit .v { color:var(--crit); }
.kpi.is-good .v { color:var(--good); }
.kpi.is-wall .v { color:var(--wall); }

.meter { height:5px; background:var(--line); border-radius:99px; margin-top:10px;
         overflow:hidden; }
.meter > i { display:block; height:100%; background:var(--good); border-radius:99px; }

/* ---- sections ---- */
section { margin-bottom:34px; }
h2 { font-size:.72rem; text-transform:uppercase; letter-spacing:.11em;
     color:var(--muted); font-weight:600; margin:0 0 12px;
     display:flex; align-items:baseline; gap:10px; }
h2 .c { font-family:"IBM Plex Mono",ui-monospace,monospace; color:var(--ink-2);
        letter-spacing:0; }
.lede { color:var(--ink-2); margin:-4px 0 14px; max-width:66ch; font-size:.9rem; }

/* ---- attention list: severity stripe, not a card ---- */
.att { display:flex; flex-direction:column; gap:1px; background:var(--line);
       border:1px solid var(--line); border-radius:3px; overflow:hidden; }
.att-row { background:var(--surface); display:grid;
           grid-template-columns:4px 1fr auto auto; gap:0 14px; align-items:center;
           padding:0; }
.att-row > .stripe { align-self:stretch; background:var(--warn); }
.att-row.sev-crit > .stripe { background:var(--crit); }
.att-row.sev-wall > .stripe { background:var(--wall); }
.att-body { padding:11px 0 11px 4px; min-width:0; }
.att-name { font-weight:500; }
.att-why { font-size:.82rem; color:var(--muted); }
.att-num { font-family:"IBM Plex Mono",ui-monospace,monospace; font-size:.86rem;
           font-variant-numeric:tabular-nums; color:var(--crit); font-weight:500;
           white-space:nowrap; }
.att-row.sev-warn .att-num { color:var(--warn); }
.att-row.sev-wall .att-num { color:var(--wall); }
.att-do { padding-right:16px; font-family:"IBM Plex Mono",ui-monospace,monospace;
          font-size:.76rem; color:var(--muted); white-space:nowrap; }

/* ---- stacked distribution bar ---- */
.bar { display:flex; height:34px; border-radius:3px; overflow:hidden;
       background:var(--surface-2); gap:2px; }
.bar > span { display:block; position:relative; }
.bar > span.g-good{background:var(--good);} .bar > span.g-inflight{background:var(--inflight);}
.bar > span.g-warn{background:var(--warn);} .bar > span.g-crit{background:var(--crit);}
.bar > span.g-wall{background:var(--wall);}
.legend { display:flex; flex-wrap:wrap; gap:6px 20px; margin-top:12px;
          font-size:.83rem; }
.legend span { display:flex; align-items:center; gap:7px; color:var(--ink-2); }
.legend i { width:10px; height:10px; border-radius:2px; flex:none; }
.legend b { font-family:"IBM Plex Mono",ui-monospace,monospace; color:var(--ink);
            font-weight:500; font-variant-numeric:tabular-nums; }

/* ---- tables ---- */
.scroll { overflow-x:auto; border:1px solid var(--line); border-radius:3px;
          background:var(--surface); }
table { border-collapse:collapse; width:100%; font-size:.87rem; }
th { text-align:left; font-size:.68rem; text-transform:uppercase;
     letter-spacing:.08em; color:var(--muted); font-weight:600;
     padding:10px 14px; border-bottom:1px solid var(--line);
     background:var(--surface-2); white-space:nowrap; position:sticky; top:0; }
td { padding:9px 14px; border-bottom:1px solid var(--line); vertical-align:top; }
tr:last-child td { border-bottom:0; }
td.num, th.num { font-family:"IBM Plex Mono",ui-monospace,monospace;
                 font-variant-numeric:tabular-nums; white-space:nowrap; }
.key { font-family:"IBM Plex Mono",ui-monospace,monospace; font-size:.8rem;
       color:var(--muted); }

.pill { display:inline-flex; align-items:center; gap:6px; font-size:.76rem;
        font-weight:500; padding:2px 9px 2px 7px; border-radius:99px;
        border:1px solid; white-space:nowrap; }
.pill i { width:6px; height:6px; border-radius:99px; flex:none; }
.p-good{color:var(--good);border-color:color-mix(in oklab,var(--good) 40%,transparent);}
.p-good i{background:var(--good);}
.p-inflight{color:var(--inflight);border-color:color-mix(in oklab,var(--inflight) 40%,transparent);}
.p-inflight i{background:var(--inflight);}
.p-warn{color:var(--warn);border-color:color-mix(in oklab,var(--warn) 40%,transparent);}
.p-warn i{background:var(--warn);}
.p-crit{color:var(--crit);border-color:color-mix(in oklab,var(--crit) 40%,transparent);}
.p-crit i{background:var(--crit);}
.p-wall{color:var(--wall);border-color:color-mix(in oklab,var(--wall) 40%,transparent);}
.p-wall i{background:var(--wall);}

/* ---- catalog health ---- */
.health { display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
          gap:1px; background:var(--line); border:1px solid var(--line);
          border-radius:3px; overflow:hidden; }
.health div { background:var(--surface); padding:12px 14px; }
.health .hv { font-family:"IBM Plex Mono",ui-monospace,monospace; font-size:1.15rem;
              font-variant-numeric:tabular-nums; }
.health .hk { font-size:.74rem; color:var(--muted); }

/* ---- activity ---- */
.log { font-size:.85rem; }
.log li { display:grid; grid-template-columns:96px 1fr; gap:14px;
          padding:7px 0; border-bottom:1px solid var(--line); list-style:none; }
.log li:last-child { border-bottom:0; }
.log time { font-family:"IBM Plex Mono",ui-monospace,monospace; font-size:.78rem;
            color:var(--muted); }
.log ul, .log { margin:0; padding:0; }
.log b { font-weight:500; }
.log .d { color:var(--ink-2); }

.empty { background:var(--surface); border:1px dashed var(--line);
         border-radius:3px; padding:22px; color:var(--muted); }
.empty code, code { font-family:"IBM Plex Mono",ui-monospace,monospace;
                    font-size:.85em; background:var(--surface-2);
                    border:1px solid var(--line); border-radius:3px;
                    padding:1px 5px; color:var(--ink); }
footer { margin-top:44px; padding-top:16px; border-top:1px solid var(--line);
         font-size:.8rem; color:var(--muted); display:flex; flex-wrap:wrap;
         gap:8px 24px; justify-content:space-between; }
.demo { background:color-mix(in oklab,var(--warn) 12%,var(--surface));
        border:1px solid color-mix(in oklab,var(--warn) 45%,transparent);
        border-left:4px solid var(--warn); border-radius:3px;
        padding:11px 16px; margin-bottom:24px; font-size:.87rem; color:var(--ink-2); }
.demo b { color:var(--ink); }
.note { font-family:"IBM Plex Serif",Georgia,serif; font-size:.84rem;
        color:var(--muted); max-width:70ch; }
@media (max-width:640px) {
  .statute { text-align:left; }
  .att-row { grid-template-columns:4px 1fr auto; }
  .att-do { display:none; }
  .log li { grid-template-columns:1fr; gap:2px; }
}
@media (prefers-reduced-motion:reduce) { * { transition:none !important; } }
"""


def _pill(group: str, label: str) -> str:
    return (f'<span class="pill p-{group}"><i></i>{html.escape(label)}</span>')


def _e(v: Any) -> str:
    return html.escape("" if v is None else str(v))


def render(d: Dict[str, Any]) -> str:
    t = d["totals"]
    pct = (100 * t["reached"] // t["brokers"]) if t["brokers"] else 0
    removed_pct = (100 * t["removed"] // t["brokers"]) if t["brokers"] else 0

    # ---- KPIs
    kpis = [
        ("Brokers tracked", f'{t["brokers"]}',
         f'{t["curated"]} curated &middot; {t["registry"]} from the CA registry', "", None),
        ("Reached", f'{t["reached"]}',
         f'{pct}% of the catalog has heard from you', "", pct),
        ("Confirmed removed", f'{t["removed"]}',
         f'{removed_pct}% of the catalog', "is-good", None),
    ] + ([
        ("Listings cleared", f'{t["listings_removed"]}/{t["listings"]}',
         f'{t["partial"]} broker(s) removed only some', "", None),
    ] if t["listings"] else []) + [
        ("Past deadline", f'{t["overdue"]}',
         'statutory response window missed', "is-crit" if t["overdue"] else "", None),
        ("Blocked by site", f'{t["blocked"]}',
         'wants ID or defeated by a captcha', "is-wall" if t["blocked"] else "", None),
    ]
    kpi_html = "".join(
        f'<div class="kpi {cls}"><div class="k">{k}</div><div class="v">{v}</div>'
        f'<div class="n">{n}</div>'
        + (f'<div class="meter"><i style="width:{meter}%"></i></div>' if meter is not None else "")
        + "</div>"
        for k, v, n, cls, meter in kpis
    )

    # ---- attention
    if d["attention"]:
        att_rows = []
        for r in d["attention"]:
            if r["overdue"]:
                sev, num = "crit", f'{-r["days_left"]}d over'
                why = f'Sent {r["sent_at"]} &middot; was due {r["due_at"]}'
                do = f'dr escalate {r["id"]}'
            elif r["status"] == "verification_required":
                sev, num, why = "warn", "waiting on you", "Check the contact inbox for their link"
                do = f'dr log {r["id"]} --status sent'
            elif r["status"] == "partial":
                left = r["listings_total"] - r["listings_removed"]
                sev = "warn"
                num = f'{left} of {r["listings_total"]} still up'
                why = "They removed some listings and stopped"
                do = f'dr listings {r["id"]}'
            elif r["status"] == "rejected":
                sev, num, why = "crit", "refused", "They declined the request"
                do = f'dr escalate {r["id"]}'
            else:
                sev, num, why = "warn", "re-listed", "Removed once, back again"
                do = f'dr start {r["key"]} --again'
            att_rows.append(
                f'<div class="att-row sev-{sev}"><div class="stripe"></div>'
                f'<div class="att-body"><div class="att-name">{_e(r["broker"])}</div>'
                f'<div class="att-why">{why}</div></div>'
                f'<div class="att-num">{num}</div>'
                f'<div class="att-do">{_e(do)}</div></div>'
            )
        attention = (
            f'<h2>Needs attention <span class="c">{len(d["attention"])}</span></h2>'
            f'<div class="att">{"".join(att_rows)}</div>'
        )
    else:
        attention = (
            '<h2>Needs attention</h2><div class="empty">Nothing overdue and nothing '
            'waiting on you. Deadlines are tracked automatically once a request is '
            'marked sent.</div>'
        )

    # ---- distribution bar (one chart, labelled, never colour alone)
    if d["rows"]:
        order = ["good", "inflight", "warn", "crit", "wall"]
        grouped: Dict[str, Dict[str, int]] = {g: {} for g in order}
        for r in d["rows"]:
            grouped[r["group"]][r["label"]] = grouped[r["group"]].get(r["label"], 0) + 1
        total = len(d["rows"])
        segs, legend = [], []
        for g in order:
            n = sum(grouped[g].values())
            if not n:
                continue
            names = ", ".join(f"{k} ({v})" for k, v in sorted(grouped[g].items()))
            segs.append(f'<span class="g-{g}" style="width:{100 * n / total:.2f}%" '
                        f'title="{_e(names)}"></span>')
            for k, v in sorted(grouped[g].items(), key=lambda kv: -kv[1]):
                legend.append(f'<span><i style="background:var(--{g})"></i>'
                              f'{_e(k)} <b>{v}</b></span>')
        dist = (f'<h2>Where the {total} request(s) stand</h2>'
                f'<div class="bar">{"".join(segs)}</div>'
                f'<div class="legend">{"".join(legend)}</div>')
    else:
        dist = ('<h2>Where the requests stand</h2><div class="empty">No requests yet. '
                'Start a browser session with <code>dr batch --tier 1</code>, or a single '
                'broker with <code>dr start acxiom</code>.</div>')

    # ---- requests table
    if d["rows"]:
        trs = []
        for r in d["rows"]:
            if r["overdue"]:
                left = f'<span style="color:var(--crit)">{-r["days_left"]}d over</span>'
            elif r["days_left"] is not None and r["status"] not in (
                    "completed", "rejected", "not_found"):
                left = f'{r["days_left"]}d'
            else:
                left = "&mdash;"
            trs.append(
                f'<tr><td class="num">{r["id"]}</td>'
                f'<td>{_e(r["broker"])}<div class="key">{_e(r["key"])}</div></td>'
                f'<td class="num">{r["tier"]}</td>'
                f'<td>{_pill(r["group"], r["label"])}</td>'
                f'<td class="key">{_e(r["channel"])}</td>'
                f'<td class="num">{_e(r["sent_at"]) or "&mdash;"}</td>'
                f'<td class="num">{_e(r["due_at"]) or "&mdash;"}</td>'
                f'<td class="num">{left}</td>'
                f'<td class="num">'
                + (f'{r["listings_removed"]}/{r["listings_total"]}'
                   if r["listings_total"] else "&mdash;")
                + '</td>'
                f'<td class="key">{_e(r["ref"]) or "&mdash;"}</td></tr>'
            )
        table = (
            '<h2>Every request</h2><div class="scroll"><table><thead><tr>'
            '<th class="num">#</th><th>Broker</th><th class="num">Tier</th>'
            '<th>Status</th><th>Channel</th><th class="num">Sent</th>'
            '<th class="num">Due</th><th class="num">Left</th>'
            '<th class="num">Listings</th><th>Ref</th>'
            f'</tr></thead><tbody>{"".join(trs)}</tbody></table></div>'
        )
    else:
        table = ""

    # ---- catalog health
    hv = d["verify_counts"]
    order_v = ["ok", "moved", "botwall", "notfound", "soft404", "error", "skipped",
               "unchecked"]
    health = "".join(
        f'<div><div class="hv">{hv[k]}</div>'
        f'<div class="hk">{VERIFY_LABEL.get(k, "Never checked")}</div></div>'
        for k in order_v if hv.get(k)
    )

    # ---- tier coverage
    tier_rows = "".join(
        f'<tr><td class="num">{k}</td><td class="num">{v["total"]}</td>'
        f'<td class="num">{v["reached"]}</td><td class="num">{v["removed"]}</td>'
        f'<td class="num">{100 * v["reached"] // v["total"] if v["total"] else 0}%</td></tr>'
        for k, v in d["tiers"].items()
    )

    # ---- activity
    if d["events"]:
        log = "".join(
            f'<li><time>{_e(e["at"][:16])}</time>'
            f'<div><b>{_e(e["who"]) or "&mdash;"}</b> '
            f'<span class="d">{_e(e["detail"])[:200]}</span></div></li>'
            for e in d["events"]
        )
        activity = f'<h2>Recent activity</h2><ul class="log">{log}</ul>'
    else:
        activity = ""

    demo = ""
    if d.get("is_demo"):
        demo = (
            '<div class="demo"><b>Example data.</b> The profile is still the '
            'template, so these are sample figures - not a real campaign. Fill in '
            '<code>~/.dataremoval/profile.yaml</code> and re-run '
            '<code>dr dashboard</code>.</div>'
        )

    return f"""<title>Broker Removal Progress</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Serif:ital,wght@0,400;0,600;1,400&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>{CSS}</style>
<div class="wrap">
{demo}
<header>
  <div>
    <h1>Data removal progress</h1>
    <div class="subject">{_e(d["subject"])} &middot; {_e(d["residency"])} &middot; generated {_e(d["generated"])}</div>
  </div>
  <div class="statute"><b>{_e(d["law"]["name"])}</b>{_e(d["law"]["citation"])}<br>
  {d["law"]["days"]}-day response window &middot; {_e(d["law"]["regulator"])}</div>
</header>

<div class="kpis">{kpi_html}</div>

<section>{attention}</section>
<section>{dist}</section>
<section>{table}</section>

<section>
  <h2>Coverage by tier</h2>
  <p class="lede">Tier 1 is the wholesale aggregators that supply everyone else.
  Clearing a retail people-search site while its supplier still sells your record
  buys about six months.</p>
  <div class="scroll"><table><thead><tr><th class="num">Tier</th>
  <th class="num">Brokers</th><th class="num">Reached</th>
  <th class="num">Removed</th><th class="num">Reached %</th></tr></thead>
  <tbody>{tier_rows}</tbody></table></div>
</section>

<section>
  <h2>Catalog link health</h2>
  <p class="lede">From the last <code>dr verify</code> sweep. Bot-walled is not the
  same as broken - it means a script could not confirm the page, not that the page
  is gone.</p>
  <div class="health">{health}</div>
</section>

<section>{activity}</section>

<footer>
  <span>Regenerate: <code>dr dashboard --open</code></span>
  <span class="note">Self-contained and local. Your address history, phone numbers
  and email addresses are deliberately not on this page.</span>
</footer>
</div>
"""
