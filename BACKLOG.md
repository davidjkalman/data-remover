# Backlog

Current state: 45 brokers, request generation, deadline tracking, link
verification, audit log. 53 tests. Works end to end for a single subject.

**Done:** 12 (packaging), 13 (rate limiting + resumable sweeps),
`dr history` (audit trail, not originally on this list).
**Next up:** 1 — `dr import-registry`, now unblocked.

Ordered by leverage, not by effort. The rationale for each is the part worth
arguing with — the estimates are guesses.

---

## The one number that matters

**Coverage.** Everything here is downstream of "what fraction of the sites
holding your data have you actually reached?" We are at 45. DeleteMe claims
~750. The California registry lists **549 legally registered brokers**, and
that gap is the product.

Second-order: **stick rate** — of those reached, how many stay gone after 12
months. We can't measure it yet (see P1-3).

---

## P0 — Catalog breadth

### 1. `dr import-registry` — bulk-import the CA CPPA registry
**12x the catalog in one command.** `complete-reg-data-brokers.csv` at
cppa.ca.gov: 549 rows, columns for name, email, website, physical address,
opt-out instructions, and date registered. Verified fetchable and parseable
2026-09-09.

Real work is in the messy parts, not the download:
- **Emails are obfuscated** — `privacy [at] VDX.tv`. Mechanical to fix.
- **The opt-out column is free text** and often garbage: some rows are a URL,
  some are a paragraph, some are a whole privacy policy pasted into the cell.
  Needs URL extraction with a human-review fallback rather than a guess.
- **Dedupe against the 45 we have** — match on domain, not name; "Intelius"
  and "PeopleConnect Inc." are the same submission target.
- **Auto-tier.** The registry is mostly ad-tech (VDX.tv, Alesco), not
  people-search. Those matter for different reasons and shouldn't crowd the
  queue ahead of the sites publishing your home address. Heuristic: registry
  brokers default to tier 3; promote on evidence.

Ship it behind `--dry-run` first; a bad import poisons the catalog.
*Effort: M. Unlocks 2, 3, and most of the value of everything else.*

### 2. Registry refresh as a diff
The state updates the registry; brokers register and deregister. Re-import
should show **what changed** — new registrants, and deregistrations (which
often mean an acquisition, not a shutdown, so the data moved rather than went
away). *Effort: S, after 1.*

### 3. Other state registries — probably skip
Vermont, Oregon and Texas all run registries. Probed them: VT's inquiry portal
is JS-rendered, OR wouldn't resolve, TX 403s. Low marginal value anyway —
anyone doing business in California has to register there, so CA is close to a
superset. **Revisit only if CA import shows obvious gaps.**
*Effort: M for little. Deprioritized on evidence.*

---

## P0 — Close the loop

### 4. Inbox integration
This is what separates a tracker from a service. Right now every status change
is manual: you send, you watch your inbox, you remember to come back and type
`dr log`. Watching the contact mailbox would let us:
- detect confirmation emails and auto-advance `sent` → `acknowledged`
- surface verification links as an action queue instead of you finding them
- capture the broker's reply as evidence, attached to the request
- notice rejections and route them straight to `dr escalate`

Parsing broker mail is heuristic and will misfire; it must **propose**
transitions for confirmation, never apply them silently. A tracker that lies
about state is worse than one that makes you type.
*Effort: M–L. Highest quality-of-life item on the list.*

### 5. Evidence capture
Partly addressed: `dr history` now gives a dated, queryable trail of every
action, and `dr escalate` already folds a request's event history into the
complaint. What is still missing is the *artifacts* - the confirmation
screenshot, the broker's reply email.
`--ref` stores a ticket number, which is thin. A regulator complaint wants the
confirmation screen, the reply email, and the dates. Store artifacts per
request; teach `dr escalate` to assemble a complaint pack.
*Effort: S. Cheap, and it's what makes escalation credible.*

---

## P1 — The bot-wall problem

18 of 45 tier-1 sites answer `botwall`. At 549 brokers that fraction is the
single largest blocker to actually finishing.

### 6. `dr open --batch` — the 80/20
Open the next N opt-out forms in real browser tabs, crib sheet on the
clipboard, `dr sent` prompts as you go. No automation, no fragility, turns a
40-site slog into a session. **Do this before 7.**
*Effort: S.*

### 7. Playwright-assisted submission
Drive the form in a real browser, prefill from the profile, **stop at the
captcha and hand control back**, then capture the confirmation. Not
full automation — a co-pilot for the sites that fight scripts.
High maintenance: every site is bespoke and they change. Only worth it for
tier 1, and only after 6 proves which sites are actually the bottleneck.
*Effort: L. Highest effort on the list; sequence it last in this section.*

---

## P1 — Know where you actually are

### 8. Exposure scan
We currently send requests blindly. Searching yourself first would let us skip
brokers with no record of you, auto-collect listing URLs into the request
(which is what makes removals actually complete), and give a real before/after.
Partially blocked by the same bot walls as 7 — but a partial scan still beats
guessing, and it's the only way to measure **stick rate**.
*Effort: M.*

---

## P2 — Operational

### 9. Scheduled runs + notifications
Cron `dr due` and `dr recheck`, notify on overdue or re-listing. The tool
already models both; nothing surfaces them unless you type. *Effort: S.*

### 10. HTML/PDF report
`dr report` is markdown. A dated PDF is what you attach to a complaint or hand
a lawyer. *Effort: S.*

### 11. Multi-subject
Household members, or relatives whose listings make you findable. Schema is
single-subject today; this is a real refactor, not a flag. *Effort: M.*

---

## Tech debt

### ~~12. `CATALOG_PATH` breaks on a non-editable install~~ — **done**
Was worse than described: a built wheel contained **no catalog at all**, so the
path bug was academic. Catalog moved to `src/dataremoval/data/`, declared as
package data, resolved via `importlib.resources`. Verified by installing a
wheel into a clean venv and running `dr init` from `/tmp`. Three regression
tests, including one asserting `pyproject.toml` still declares the package
data — the file being in the right place does nothing without it.

### ~~13. Politeness and rate limiting at 549 brokers~~ — **done**
Global rate cap (default 2/s), per-host serialization with a minimum gap
(default 1s), `Retry-After` honoured against the whole host. Results persist as
they land and `--resume` continues an interrupted sweep; Ctrl+C now stops
queued work through a cooperative flag rather than draining the queue, and
exits 130. 13 new tests.

Two things this shook out that were not in the original write-up: progress
output was block-buffered, so an interrupted sweep printed **nothing** despite
saving its work; and the resume command was rendered 25 lines above the end of
the summary, where it scrolled away. Both fixed.

### 14. No git repo, no CI
32 tests that nothing runs automatically. *Effort: S.*

### 15. Secrets at rest
`profile.yaml` is chmod 600 at creation only; the SQLite DB is plaintext and
holds your full address history. Consider encryption at rest, or at minimum
document the exposure honestly. *Effort: S to document, M to encrypt.*

### 16. No partial-removal state
Status is per-broker, but removal is per-listing — brokers routinely remove one
of your four listings and call it done. The model can't currently express
"3 of 4 gone", which means `completed` overstates our own coverage number.
*Effort: M. Matters more as the catalog grows.*

---

## Explicitly not doing

- **Captcha solving.** Won't build it, won't integrate a service for it.
- **Submitting on your behalf unattended.** The last click stays yours.
- **Reselling this as a service.** Scoped as a personal tool; multi-tenant
  billing, support and liability are a different product.
