# Hansard pipeline

Fetches UK Parliament debates from the [Hansard API](https://hansard-api.parliament.uk/swagger/ui/index)
into a local SQLite database you can query.

The scope is deliberately narrow — **House of Commons, January to April 2026** —
because a dataset small enough to rebuild in twenty minutes is a dataset you can
afford to keep changing your mind about.

```
     Hansard API                    this pipeline                   your queries
  ┌───────────────┐            ┌──────────────────────┐          ┌──────────────┐
  │ calendar      │──days────▶ │ client   fetch+retry │          │ sqlite3      │
  │ search/debates│──sections▶ │ normalise  parse     │──rows──▶ │ hansard      │
  │ debates/debate│──speech──▶ │ store    upsert      │          │   search/stats│
  └───────────────┘            └──────────────────────┘          └──────────────┘
```

---

## Quick start

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows;  source .venv/bin/activate elsewhere
pip install -e ".[dev]"

hansard ingest                  # ~20 min, ~2,000 debates, no API key needed
hansard check                   # prove the store is internally consistent
hansard stats                   # see what you got
hansard search "NEAR(climate target, 5)"
```

`ingest` is safe to interrupt and safe to repeat. Run it twice and the second
run writes nothing.

---

## Commands

| Command | What it does |
|---|---|
| `hansard init-db` | Create the database and schema. Idempotent. |
| `hansard ingest` | Fetch a date range into the store. |
| `hansard ingest --resume` | Skip sitting days already ingested completely. |
| `hansard ingest --limit-days 2` | Stop after two days — a smoke test against the real API. |
| `hansard check` | Run the integrity and duplicate checks. Exits non-zero on failure. |
| `hansard stats` | Summarise the store: counts, parties, most frequent speakers. |
| `hansard search "<query>"` | Full-text search the speeches (SQLite FTS5 syntax). |

FTS5 query syntax is its own thing — note `NEAR` in particular, which is
`NEAR(a b, 5)` here, not the `a NEAR/5 b` form other search engines use:

```bash
hansard search "refinery"                 # single term
hansard search '"cost of living"'         # exact phrase
hansard search "NEAR(nhs waiting, 5)"     # within 5 tokens of each other
hansard search "housing AND (rent OR mortgage)"
```

A malformed query is reported, not raised: `hansard search "AND OR"` prints
`Invalid FTS query` and exits 2.

Every command takes `--database`; `ingest` also takes `--house`, `--start` and
`--end`. Defaults come from the environment — see [`.env.example`](.env.example).

---

## How it works

### The three endpoints

Hansard has no bulk export, so the shape of the ingest is dictated by what the
API offers:

| Endpoint | Gives us | Calls |
|---|---|---|
| `/overview/calendar.json?year&month&house` | which days the House sat | 4 |
| `/search/debates.json?queryParameters.*` | the debate sections on a day | ~55 |
| `/debates/debate/{extId}.json` | one section's full transcript | ~1,960 |

Note the `queryParameters.` prefix on the search endpoint. It is not a typo —
that is genuinely how the API expects those arguments.

Asking the calendar which days sat, rather than probing all 120 dates in the
range, removes about half the requests before we start.

### The layers

Each module knows one thing, so each can be tested without the others:

```
config.py       what to fetch, where to put it            (no I/O)
api/models.py   the wire format, exactly as sent          (no I/O)
api/client.py   HTTP: rate limit, retry, pagination       (no SQL)
pipeline/
  normalise.py  wire format -> our rows                   (pure functions)
  ingest.py     orchestration                             (no HTTP, no SQL)
  checks.py     integrity checks                          (read-only SQL)
db/
  schema.sql    the tables, and why they are shaped so
  store.py      idempotent writes                         (no HTTP)
cli.py          argument parsing and presentation         (thin)
```

The one rule worth stating: **`normalise.py` is the only module that knows both
the API's shape and ours.** Every upstream quirk is absorbed there, so a field
rename at Parliament breaks in one obvious place.

---

## The interesting problems

Most of this project is ordinary. These four parts are not, and they are where
the design decisions live.

### 1. Child debates arrive twice

Hansard sections nest — a `Petition` section contains a specific petition. The
API returns a child section **inline inside its parent's payload** *and* **as
its own entry in the day's search results**.

The obvious implementation recurses into `ChildDebates` and writes every nested
section twice. This one deliberately does not: ingest walks the flat day index
only, and reconstructs the hierarchy from each payload's `Navigator` breadcrumb
trail instead. `check_duplicate_debates` is what would catch a regression.

### 2. A single bracket means two different things

Hansard packs the speaker, their seat and their party into one display string —
and the format is genuinely ambiguous:

| String | Reading |
|---|---|
| `Martin Vickers (Brigg and Immingham) (Con)` | name, constituency, party |
| `The Minister for Policing and Crime (Sarah Jones)` | **role**, then name |
| `Lord Callanan (Con)` | name, party |
| `Mr Speaker` | name |

With one trailing bracket you cannot tell a party from a person by position.
Sampling four sitting days settled it: in the Commons a single bracket is
**always** a person behind a ministerial title, never a party. So the parser
checks the bracket against a set of known party abbreviations, and treats
anything else as a name — which means Ed Miliband groups with himself whether he
spoke from the back benches or the Dispatch Box.

### 3. Party is mostly missing, and that is upstream's doing

Hansard states a member's party only on their **first** turn in a debate. Every
later turn is a bare name, and ministers speaking by office never carry one at
all — so `contribution.party` is null for roughly 60% of speeches.

The tables keep what Hansard actually said. The `speech` view fills the gaps
from the `member` table, which accumulates the best-known value across every
debate in the store:

```sql
-- what Hansard said (sparse)
SELECT party, COUNT(*) FROM contribution WHERE is_speech = 1 GROUP BY party;

-- best available answer (use this)
SELECT party, COUNT(*) FROM speech GROUP BY party;
```

Raw in the tables, resolved in the views. Getting real party data means the
[Members API](https://members-api.parliament.uk/index.html) — a second source,
and therefore Phase 2.

### 4. Not every "Contribution" is a contribution

`ItemType` is `"Contribution"` for column markers and page furniture as well as
speech. Left alone they inflate every word count and pollute every search
result. `is_speech` is the derived flag that encodes the rule once; downstream
queries filter on it rather than re-deriving it, and the `speech` view applies
it for you.

---

## Idempotency

Re-running an ingest must produce the same database, not a second copy of it.
Four things make that true:

1. **Every table is keyed on an identifier Hansard owns** — `debate.ext_id`,
   `contribution.item_id` — never on an autoincrement of our own. That alone
   turns a re-fetch into an UPSERT rather than an INSERT.
2. **A content hash short-circuits unchanged work.** A debate's hash covers all
   of its contributions, so "has this changed?" is one column comparison. On a
   second run every debate reports `unchanged` and nothing is written.
3. **Transcripts are replaced, not merged.** A revised debate can *remove* a
   contribution; an upsert alone would leave the withdrawn speech in the
   database for ever, quietly corrupting every count derived from it.
4. **Derived counts are recomputed, never incremented.** `member.contribution_count`
   is recalculated at the end of a run, so it cannot drift.

Try it:

```bash
hansard ingest --limit-days 2     # inserted 67, contributions 1,429
hansard ingest --limit-days 2     # unchanged 67, contributions       0
```

**Failure is expected, so it is designed for.** One transaction per debate, not
one per run: a network failure ninety minutes in leaves everything already
fetched committed. A day is marked complete only if *nothing* on it failed, so
`--resume` can never skip past a day it knows is short.

---

## Schema

```
ingest_run ──── provenance: what was fetched, when, and how it went
sitting_day ─── which (house, day) pairs are enumerated and complete
debate ──────── one titled section; parent_ext_id records the tree
  └─ contribution ── one transcript row; is_speech marks the real speech
       └─ member ─── speakers, assembled from attribution strings
contribution_fts ─ FTS5 index, kept in step by triggers
speech ──────── view: speeches only, with party and name resolved
```

Two decisions worth knowing before you change it:

**`parent_ext_id` is deliberately not a foreign key.** The topmost ancestor of
any day is a container (`Commons Chamber`) with no fetchable payload of its own,
so a real FK would reject legitimate rows. `debate.depth` — taken from the
payload's Navigator trail, where 2 means top-level and 3+ means nested — is what
lets `check_orphan_parents` tell an expected unresolved link from a real gap,
without the FK.

**The FTS index is external-content and trigger-maintained.** It stores no
second copy of the text, so the index and the table cannot disagree about
wording. The price is that application code must never write to it directly — a
plain `DELETE` against an external-content table is not valid, and `COUNT(*)`
on one silently reads the *content* table, so it agrees with itself no matter
how far the index has drifted. Triggers in `schema.sql` own the invariant, and
`check_fts_in_sync` uses FTS5's `integrity-check` **with `rank = 1`**, which is
the argument that makes it compare index against content rather than merely
checking the index is self-consistent.

---

## Querying it

```bash
sqlite3 data/hansard.db
```

```sql
-- Who spoke most about a topic?
-- member_id IS NOT NULL matters: without it every unattributed procedural
-- line collapses into one meaningless "NULL speaker" group at the top.
SELECT s.speaker_name, s.party, COUNT(*) AS mentions
  FROM contribution_fts f
  JOIN speech s ON s.item_id = f.rowid
 WHERE f.body_text MATCH 'housing'
   AND s.member_id IS NOT NULL
 GROUP BY s.member_id
 ORDER BY mentions DESC
 LIMIT 10;

-- Busiest sitting days
SELECT sitting_date, COUNT(*) AS debates, SUM(word_count) AS words
  FROM debate GROUP BY sitting_date ORDER BY words DESC LIMIT 10;

-- A debate, in order
SELECT order_in_section, speaker_name, body_text
  FROM speech WHERE debate_ext_id = '69FFB3CB-33EF-41DD-94B0-E8685BEB39EF'
 ORDER BY order_in_section;
```

---

## Development

```bash
pytest                      # 160 tests, no network, ~3s
pytest --cov=hansard        # 96% coverage
ruff check . && ruff format --check .
mypy                        # strict
```

Tests run against **real API payloads** captured under `tests/fixtures/`, not
invented ones — a test against a hand-written fixture only proves the code
agrees with your imagination. HTTP is intercepted at the transport layer with
`respx`, so nothing touches the network.

`hansard ingest --limit-days 1` is the smoke test that does hit the real API.

---

## What this is not

Scope kept out on purpose, so that what is here stays correct:

- **One source.** Hansard only. Members, Bills and Divisions are separate APIs.
- **One house, four months.** Lords works today (`--house Lords`) but is not
  what the defaults or the numbers above describe.
- **Spoken debates only.** `search/debates` does not return written statements
  or petitions-as-documents.
- **No frontend.** This repo produces a database; reading it is a separate job.

Natural next steps: enrich members from the Members API (fixing the party gap),
add divisions, extend to the Lords, widen the date range.

---

Public UK Parliament data, used under the
[Open Parliament Licence](https://www.parliament.uk/site-information/copyright-parliament/open-parliament-licence/).
No authentication required — please keep the rate limit polite.
