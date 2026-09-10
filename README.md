# data_removal

A personal replacement for DeleteMe / Cloaked. It keeps the broker catalog,
writes the requests with the right statute attached, holds brokers to their
statutory deadlines, and re-checks the ones that quietly re-list you.

It does not click the last button for you. See [Why it isn't fully automatic](#why-it-isnt-fully-automatic).

## Install

```bash
pip install -e .
dr init
$EDITOR ~/.dataremoval/profile.yaml
```

Everything personal lives in `~/.dataremoval/` (profile + SQLite DB), not in the
repo. `profile.yaml` and `*.db` are gitignored — keep it that way.

## The loop

```bash
dr queue                 # what to do next, highest leverage first
dr start spokeo          # opens a request, prints the letter or form crib
dr sent 1 --ref ABC123   # you submitted it; the statutory clock starts
dr due --draft           # who blew their deadline, with the follow-up ready
dr escalate 1            # regulator complaint, pre-filled
dr recheck               # who is due for a re-search
dr verify                # do the opt-out URLs still resolve?
dr report --out out.md   # a paper trail
```

`dr status` at any time for the board; `dr status 3` for one request's history.

## Seeing what happened

Every status change, note, escalation and link-check result is recorded in an
event log. `dr history` reads it across everything:

```bash
dr history                    # last 50 events, oldest first
dr history --no-verify        # only what you did, link checks hidden
dr history --broker radaris   # one broker's whole story
dr history --kind escalation  # just the complaints
dr history --since 2026-09-01
dr history --limit 200
```

Two deliberate choices:

- **A sweep logs what changed, not what it checked.** 45 URLs coming back
  exactly as they were is one summary line. A broker that moved from `ok` to
  `notfound` gets its own entry, and `dr verify` prints `(was ok)` inline so a
  regression is visible while it happens. At 549 brokers, logging every check
  would bury the signal.
- **`--limit` trims the oldest, then displays oldest-first.** A limit should
  show you what just happened, not ancient history.

Events carry a timestamp. Rows written before this existed show a date with no
clock, which is why some lines read `-` in the time column.

The log lives in the same SQLite file as everything else
(`~/.dataremoval/removal.db`, table `event`), so you can query it directly if
you want something the CLI does not offer.

## Why the ordering matters

`dr queue` sorts by tier then by hurdle count, which is not the same as
alphabetical and not the same as "worst offender first":

1. **Tier 1 aggregators** (Acxiom, LexisNexis, Epsilon, CoreLogic) are wholesale
   suppliers. People-search sites buy from them. Removing yourself from a
   retail site while the wholesaler still sells your record means you will be
   back in six months. Do these first even though they feel less urgent.
2. **California DROP**, if you are a CA resident. One submission that every
   registered broker is obliged to check on a recurring basis. Highest
   return on effort of anything in the catalog.
3. **OptOutPrescreen**, free, and it shuts off prescreened credit offers — a
   large and underrated source of address data leaking back out.
4. Then the retail people-search sites, easy ones first.

`feeds:` in the catalog records which sites source from which. Kill the parent
before the child.

## Why it isn't fully automatic

Most brokers gate opt-outs behind a CAPTCHA, an email or SMS verification, or a
government ID upload. This tool will not defeat those — so the honest design is
that it does everything up to the last click and keeps the state.

That is most of the work. The reason people give up on DIY removal is not that
any single form is hard; it is that there are forty of them, each with a
deadline, each re-listing you on a different schedule. That is bookkeeping, and
bookkeeping is what this handles.

When a site demands ID you would rather not hand over, mark it `blocked` and use
the email channel instead — a statutory request by email is legally equivalent
and does not require you to upload a passport to a company you are trying to
get away from.

## Operational notes

- **Use a dedicated contact email.** Everything you hand a broker becomes part
  of your record with them. `dr profile` warns you if you reused a real one.
- **Address history is the whole game.** Brokers key listings to addresses. A
  request naming only your current address leaves every prior listing standing.
  Two-plus addresses, going back ~15 years.
- **Collect listing URLs first.** Search yourself on a site before opting out
  and copy every result. Most forms remove *one listing*, not one person.
  `dr start <broker> --url ... --url ...` records them in the request.
- **Screenshot confirmations.** `--ref` stores the ticket number; the screenshot
  is what you attach to a complaint.
- **Deadlines are real.** CCPA is 45 days; GDPR is 30. `dr due` tracks them and
  `dr escalate` writes the complaint. Brokers respond very differently to a
  request that cites a statute and names the regulator.

## Catalog accuracy: `dr verify`

Opt-out URLs rot constantly, and a 404 you discover three months into a campaign
has cost you three months. `dr verify` sweeps them with read-only GETs — it
submits nothing and sends none of your data.

```bash
dr verify                    # unchecked or >90 days stale
dr verify --all              # everything
dr verify --tier 1           # just the aggregators
dr verify spokeo radaris     # named brokers
dr verify --apply            # adopt redirect targets into the catalog
dr verify --resume --all     # continue an interrupted sweep
```

**Pacing.** Requests are capped globally (default 2/s) and never run two at
once against the same host (default 1s gap), so parallelism spreads across
companies instead of stacking on any one of them. A `Retry-After` header backs
off that whole host. Tune with `--rate` and `--host-gap`; `--rate 0` removes
the cap.

**Interrupting is safe.** Results are written as each one lands, so Ctrl+C
loses nothing and prints the command to pick up where you stopped. That matters
at 45 brokers and is essential at 549.

It sorts each URL into one of six verdicts:

| Verdict | Meaning | What to do |
|---|---|---|
| `ok` | reachable, reads like an opt-out page | nothing; `url_verified` is set |
| `moved` | redirects, destination still an opt-out page | `dr verify --apply <key>` |
| `soft404` | 200, but the page is missing or unrelated | find the new URL by hand |
| `notfound` | 404/410 | find the new URL by hand |
| `botwall` | bot protection answered instead of the site | **not** proof it's broken — open it |
| `error` | network/TLS/DNS failure | retried once; re-run before believing it |

Three distinctions it makes deliberately, because getting them wrong is what
makes link checkers useless:

- **A captcha is a hurdle, not a wall.** Spokeo, Whitepages and OptOutPrescreen
  all serve a perfectly good opt-out page with a captcha on it. Those are `ok`,
  annotated `(has captcha)` — not `botwall`.
- **HTTP status beats body sniffing.** A 404 whose body says "enable JavaScript"
  is a 404, not a bot wall.
- **Transient failures are not verdicts.** DNS falls over under concurrency, so
  network errors are retried once serially and then labelled as unproven rather
  than reported as a dead broker. Lower `--workers` if you see a lot of them.

Only `ok` sets `url_verified`; anything else clears it, so a stale "verified"
flag can never outlive the URL it described. Your local corrections survive
`dr init` re-runs — a catalog refresh updates shipped fields but never resets
verification work.

```bash
dr broker-set corelogic optout_url https://...   # fix one by hand
dr verify corelogic                              # then confirm it
```

Expect roughly a third of tier-1 sites to answer `botwall`. That is the
industry working as intended and is not a bug in the checker.

## Layout

```
src/dataremoval/data/brokers.yaml        the catalog (edit freely, it is just data)
src/dataremoval/
  cli.py                 commands
  db.py                  schema
  profile.py             your identity, and what brokers match on
  laws.py                which statute applies, deadlines, regulators
  templates.py           request / follow-up / complaint text
  catalog.py             YAML -> DB sync
tests/
```

## Not legal advice

The templates cite real statutes and are written to be accurate, but this is a
tool, not a lawyer.
