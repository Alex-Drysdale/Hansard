# Hansard pipeline

Ingests UK Parliament debates, divisions and members into a local Postgres you
can query.

Two sources, joined on the identifier Parliament itself issues:

```
  ┌───────────────────────────┐
  │ Hansard API               │  debates, transcripts, divisions
  │ hansard-api.parliament.uk │──────────────┐
  └───────────────────────────┘              │      member_id
                                             ├──────────────────▶  Postgres
  ┌───────────────────────────┐              │   (canonical key)
  │ Members API               │  who a       │
  │ members-api.parliament.uk │──member is ──┘
  └───────────────────────────┘
```

Scope is deliberately narrow — **House of Commons, January to April 2026** —
because a dataset you can rebuild in twenty minutes is one you can afford to
keep changing your mind about.

---

## Quick start

```bash
docker compose up -d db          # Postgres 17
python -m venv .venv && .venv/Scripts/activate
pip install -e ".[dev]"

hansard migrate                  # create the schema
hansard ingest                   # ~19 min, 1,962 debates
hansard divisions                # who voted how
hansard sync-members             # who those people are
hansard reattribute              # recover speech Hansard leaves unattributed
hansard check                    # prove the store is consistent
hansard stats
```

Then, to ask it questions:

```bash
hansard index                    # embed the speeches locally (~45 min, one-off)
hansard index --resume           # continue if it was interrupted
hansard index-status             # is the index behind the database?
hansard ask "What did Wes Streeting say about cancer?"
hansard serve                    # the same thing with a web page
```

Both Parliament APIs are public, and embeddings run locally, so none of the
above needs a key. Only the *written answer* calls a model — retrieval works
without one via `hansard ask --passages-only`. Every command is safe to
interrupt and safe to repeat.

---

## What Phase 2 added, and why

### A second source, and the gap it closes

Hansard states a member's party only on their **first** turn in a debate.
Ministers speaking by office never carry one at all. After Phase 1 that left a
real hole:

Measured across the full four months, over the 36,089 speeches Hansard
attributes to a member:

| party known for an attributed speech | |
|---|---|
| stated on the transcript row itself | 35% |
| Phase 1's best effort | 77% |
| **with the Members API** | **100%** |

(A further 10,686 speeches — procedural text, motions, "Several hon. Members
rose" — carry no member id at all. Hansard attributes them to nobody, so no
source could give them a party; they are reported separately rather than
counted against coverage.)

43 members could only be named by source two at all.

The join needs no name matching, because both APIs use the same integer:
Hansard's `MemberId` *is* the Members API's `id`. That identifier is the
canonical key throughout this project.

It also resolves identity. Phase 1 could only record member 467 as
`"Mr Speaker"`. Source two knows that is **Sir Lindsay Hoyle, Speaker, Chorley**.

**Why the Members API rather than TheyWorkForYou.** TWFY's own README says its
member data is scraped from `data.parliament.uk` — the same platform our
canonical id comes from. For member facts they are *downstream* of the source we
already use, so going direct removes a hop rather than adding information. What
TWFY genuinely owns is a curated identity layer — a stable `person_id` surviving
name and seat changes, hand-fixed edge cases, links out to Wikidata — and that
remains worth having later. It is not what Phase 2 needed.

### Provenance is a schema decision, not a merge

Two sources means two chances to disagree. Rather than resolving that at write
time and losing the evidence, each source writes its own columns:

```sql
member.party_hansard   -- parsed from a transcript attribution string
member.party           -- stated outright by the Members API
```

A debate ingest can never overwrite what the member sync established, or the
reverse. The `member_resolved` view picks a winner for the common case; the
table keeps both answers.

### Divisions

Hansard's transcript records only that a division *happened*. The votes live
behind separate endpoints, and they carry `MemberId` — so a vote joins to a
speaker with no name matching at all.

```bash
hansard votes 4514        # how one member voted
```

### Postgres instead of SQLite

The port is not a transliteration. Columns SQLite could only hold as `TEXT` or
`0`/`1` are now `DATE`, `TIMESTAMPTZ`, `BOOLEAN` and enums, so the database
rejects nonsense the application previously had to be trusted about.

The biggest single win is full-text search. Phase 1 used an external-content
FTS5 table maintained by application code — and that design had two live traps:
a plain `DELETE` against such a table is not valid, and `COUNT(*)` on one
silently reads the *content* table, so an "is the index in sync?" check agreed
with itself no matter how far the index had drifted. In Postgres:

```sql
body_tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', ...)) STORED
```

The index cannot drift, because there is no code path that could forget to
update it. Search also gains a syntax people already know:

```bash
hansard search '"cost of living" -zebra'      # websearch_to_tsquery
```

### Migrations instead of a hand-edited schema

Phase 1 re-ran a `schema.sql` full of `CREATE TABLE IF NOT EXISTS` on startup.
That works right up until a column needs to change: `IF NOT EXISTS` silently
does nothing to an existing table, so the schema in your editor and the schema
in the database drift apart with no error.

Now each file in `migrations/` runs once, in order, inside a transaction, and
the database records which have been applied.

```bash
hansard migrate            # apply what is pending; safe to repeat
hansard migration-status   # what is applied, what is not
```

**When to edit a migration versus add one.** A migration becomes immutable the
moment it has been applied anywhere but your own machine — committed, pushed or
deployed. Before that, correcting it in place and rebuilding is right, and it is
what happened during this phase (see *Identifiers are not UUIDs* below). After
that, always add a new one; editing history that other databases have already
applied is how environments diverge.

### Structured logging

Every event is a name plus fields, not a sentence:

```python
log.warning("debate.skipped", ext_id=..., sitting_date=..., error=...)
```

Console-formatted for a terminal, JSON for a container (`--json-logs`). The run
id is bound to the logging context, so a line from deep inside the HTTP client
is traceable to the run that caused it without threading an identifier through
every function signature.

### A scheduler

```bash
hansard schedule            # run on cron expressions until interrupted
hansard schedule --now      # fire every job once, to prove the wiring
```

| job | cron | |
|---|---|---|
| `hansard.debates` | `0 6 * * *` | Hansard publishes overnight |
| `hansard.divisions` | `30 6 * * *` | driven off the Division markers debates stored |
| `members.sync` | `0 7 * * *` | incremental; only members never seen before |
| `checks` | `30 7 * * *` | over the finished result |

**The order is load-bearing.** Both debates and divisions introduce member ids,
and divisions introduce *more* of them — a backbencher can go a month without
speaking and still vote thirty times. A member sync scheduled before divisions
leaves every new voter without a party until the next run. There is a test that
asserts this ordering, because it is not visible from the code alone.

---

## Asking questions of it

```bash
hansard ask "What did Wes Streeting say about cancer?"
hansard ask "What has been said about the cost of living recently?"
hansard ask "..." --passages-only    # retrieval only, no model
hansard ask "..." --naive            # textbook RAG, for comparison
hansard serve                        # web UI on localhost:8000
```

Embeddings are local (ONNX, no torch, no GPU), the vector store is a directory
on disk (LanceDB), and the answer comes from any OpenAI-compatible endpoint —
Groq by default, but point `LLM_BASE_URL` at OpenAI or a local llama.cpp server
and nothing else changes.

### Where naive RAG breaks, and what is done about it

Textbook RAG is: embed the question, take the nearest five chunks, hand them to
a model. It fails on the questions people actually ask of this data. Run any of
these with `--naive` to watch it happen.

**"What did Wes Streeting say about cancer?"**
The textbook claim is that vector search returns passages *about cancer* by
everybody, because similarity has no notion of "by this person". **Measured on
this system, that is false** — naive vector search returned 15 out of 15
passages by Streeting, and 12 out of 12 even for "housing", which is not his
brief.

The reason is a design choice two modules away: the embedded text is prefixed
with the speaker and party (`build_embed_text`), so the name is *in* the
vector and similarity binds to it hard. Worth knowing, because it means the
usual advice would have sent you looking for a fix you did not need.

The member filter still earns its place, for a different reason: it makes the
constraint a **guarantee** rather than an empirical tendency. Vector affinity to
a name is a property of this embedding and this prefix; a `WHERE member_id = ?`
holds regardless of model, question phrasing, or how many other people happen to
be discussing the same subject.

**"…in the last month"**
A date range is not a direction in vector space. Nothing in an embedding
encodes recency, so a naive search happily returns the best-matching passage
from four months ago. Relative periods are anchored to the **latest data**, not
to today — the store ends in April 2026, so measuring "last month" against the
real clock would return nothing and look like an empty database.

**Questions naming a bill, a drug or a constituency**
Embeddings blur rare tokens; full-text search finds the literal one. Both are
run and fused with reciprocal rank fusion — by *position*, because a cosine
distance and a `ts_rank` are not on the same scale and cannot be added.

Re-measured on the finished index, hybrid beats naive by considerably less than
this README first claimed. Against a partly-built index, naive returned generic
health passages — *Topical Questions*, *A&E Waiting Times*, *Community Hospital
Services* — while only the keyword half found the debate titled **Cancer
Diagnosis**. This file previously said that result was a property of the method
and not an artefact of missing coverage. **That was wrong.** With every speech
indexed, naive returns *Cancer Diagnosis* at rank 1.

What survives is smaller, and checkable. Over the same eight results for
*"What did Wes Streeting say about cancer?"*:

| | naive | hybrid |
| --- | --- | --- |
| by Streeting | 8/8 | 8/8 |
| from *Cancer Diagnosis* | 2/8 | 3/8 |
| corroborated by both halves | — | 5/8 |
| found by keyword only | — | 2/8 |

Two passages — one of them a third *Cancer Diagnosis* extract — are reached
only by the keyword half. That is the honest argument for hybrid on this
corpus: not that vector search fails, but that the two halves miss different
things, and where they agree you have two independent reasons to trust the
passage. The dramatic version of this demo was an artefact of an unfinished
index, which is exactly the kind of result that survives in a README unless
somebody re-runs it.

**Stale context**
The index is derived from Postgres and goes out of date the moment you ingest
anything new. `hansard index-status` reports how many speeches the database has
that the index does not, and exits non-zero. The failure it prevents is the
quietest one in RAG: the query works, the answer is fluent, and it is simply
missing everything added since the index was built.

### Chunking

The unit is a **speech turn**, not a database row — which is only possible
because attribution was resolved first. Embedding raw rows would produce
passages that begin mid-argument with no speaker attached: *"Similarly, there
are cases in which young people…"* is useless to a retriever.

Chunks break on paragraph boundaries, never mid-sentence, and overlap by whole
paragraphs so a passage beginning "he said that" still carries the sentence it
refers to. The embedded text is prefixed with the speaker, debate and date;
without it, every passage about waiting times is equidistant from every
question about waiting times.

### Building the index is where the operational failures live

Three things went wrong here, none of which are about retrieval quality:

**The build ran the machine out of memory and was killed.** The first version
loaded all 36,000 speech turns, then built all 40,000 chunks, then started
embedding — holding the entire 6.7-million-word corpus in Python strings at
once, with every chunk storing its text twice (once raw, once with the header
prefixed). It now streams: turns arrive a page at a time from a server-side
cursor, chunks are embedded and written in batches of 512 and then dropped, and
`embed_text` is computed on demand rather than stored. Measured with
`tracemalloc`, the heap stays flat at about 2 MB across 20,000 chunks.

**Parallel workers cost about 1 GB each.** Not the corpus — `onnxruntime`'s
arena, once per process. Four workers took 4 GB and got the build killed a
second time. Two workers is roughly twice as fast as serial and leaves the
machine usable, which is why `HANSARD_EMBED_PARALLEL` defaults to 2 rather than
to the core count.

**A flag that did nothing, and destroyed data doing it.** `hansard index
--resume` was accepted by the parser, listed in `--help`, and never passed
through to `build_index`. The default path calls `create_table(overwrite=True)`,
so running it discarded 34,816 already-embedded passages in silence. Nothing
errored; `--help` said the flag was supported. It is now wired up, covered by a
test that asserts the flag actually reaches the function it names, and an
overwrite of a non-empty index logs a warning first.

Long jobs on a laptop that is also running an IDE and a browser get
interrupted, so the build is resumable by design — an interruption costs
minutes, not the hour.

**A faster model was the wrong answer.** `all-MiniLM-L6-v2` embeds ten times
faster — because fastembed truncates it at **128 tokens, roughly 90 words**.
Our chunks average 162 and reach 350, so most of every passage would have been
silently discarded. Nothing errors; retrieval just quietly gets worse.
`embeddings.check_model_fits_chunks` now refuses that configuration before any
work starts, using a tokens-per-word ratio measured over 800 real chunks
(mean 1.24, p95 1.35) rather than guessed.

### Reading the answer

The UI shows the retrieved passages next to the answer, and the CLI prints the
citations. That is deliberate. The characteristic RAG failure is a fluent answer
assembled from the model's own knowledge of British politics, which reads
exactly like a grounded one. The system prompt forbids it and demands a citation
per claim, but the real defence is that you can read the sources.

---

## The interesting problems

### Identifiers are not UUIDs

The first Postgres schema typed `ext_id` as `UUID`. The first real ingest
disproved it immediately:

```
ValueError: Debate has no usable external id: 'DeferredDivisions2026-01-14'
```

Hansard uses at least three formats — a UUID, a numeric string
(`26011562000145`), and a synthetic one — and 10% of contribution ids are not
UUIDs. So the columns are `TEXT`.

That reintroduces a subtler problem the `UUID` type had been quietly solving:
**the same section arrives with different casing from different endpoints.** A
debate's own `ExtId` is uppercase; the `Navigator` trail names its parent in
lowercase. Compared as raw text they do not match, and the parent/child tree
silently breaks. Every identifier is therefore canonicalised on the way in —
parsed and lowercased when it is a UUID, kept verbatim when it is not.

### Hansard's own vote counts do not add up

A division states `AyesCount` and lists its aye members. Those disagree, and not
consistently. Measured across real divisions:

```
stated  listed  tellers  listed-minus-tellers  delta
   319     317        2                   315     +4
   174     177        2                   175     -1
   307     300        2                   298     +9
   175     177        2                   175      0
```

Tellers explain most of it, but not all. Across the 395 divisions in this
window: **280 match exactly**, another **14** reconcile once tellers are
excluded, and **101 reconcile under neither rule** — deltas run from −3 to +9.

So there is no single arithmetic that reproduces the stated count from the
listed members.

So the pipeline stores both figures and asserts neither. That distinction runs
through the whole checks module:

- **`hansard check`** asserts things *we* control — did we store what the API
  handed us, do the foreign keys resolve, is every completed day full. It exits
  non-zero, and it is scheduled.
- **`hansard report`** describes what is wrong with the *upstream* data. It
  exits zero, because failing nightly on something nobody can fix teaches you
  only to ignore the alert.

### An all-stopword speech is not a broken index

A check asserting "every speech produces search terms" failed on real data. The
speech was `"I will."` — both words are English stopwords, so an empty
`tsvector` is correct Postgres behaviour, not a fault. The check now asserts
what would actually indicate a problem: a speech of ten or more words indexing
to nothing.

### APScheduler does not speak standard cron

Two defects, found by testing the trigger's arithmetic rather than waiting on a
clock:

1. **`from_crontab` builds triggers in the machine's local timezone**, and the
   trigger's zone beats the scheduler's. A scheduler configured for UTC would
   have fired on British Summer Time for half the year while reporting UTC.
2. **APScheduler numbers Monday 0; standard cron numbers Sunday 0.** Passing
   `0 5 * * 1` straight through schedules it for **Tuesday**. It also rejects
   `7`, which cron accepts for Sunday.

Both are fixed rather than documented around: the timezone is explicit, and the
day-of-week field is translated so a cron expression means what a cron user
expects. A numeric range that wraps the week is rejected rather than guessed at.

---

## Schema

```
ingest_run ────── every run of every job: counters, status, errors
sitting_day ───── which (house, day) pairs are enumerated and complete
member ────────── canonical member_id; columns split by source
  └─ member_resolved (view)   API value, falling back to the transcript
debate ────────── one titled section; depth records the tree
  └─ contribution ── transcript rows; body_tsv generated, cannot drift
division ──────── stated counts, kept apart from the votes
  └─ division_vote ── per-member aye/no, tellers flagged
speech (view) ─── speeches only, speaker and party resolved
```

Two decisions worth knowing before changing it:

**`parent_ext_id` is deliberately not a foreign key.** The topmost ancestor of
any day is a container with no fetchable payload, so a real FK would reject
legitimate rows. `debate.depth` — from the payload's Navigator trail, where 2
means top-level and 3+ means nested — is what lets the orphan check tell an
expected unresolved link from a real gap.

**`division.debate_ext_id` *is* a foreign key, but a broken link is survivable.**
If a division names a section outside the ingested window, the link is dropped
and the votes are kept. Losing a whole division over one unresolvable reference
would be the wrong trade.

---

## Querying it

```bash
docker exec -it hansard-db psql -U hansard -d hansard
```

```sql
-- Who spoke most about a topic?
-- member_id IS NOT NULL matters: without it every unattributed procedural line
-- collapses into one meaningless NULL-speaker group at the top.
SELECT speaker_name, party, COUNT(*) AS mentions
  FROM speech
 WHERE body_tsv @@ websearch_to_tsquery('english', 'housing')
   AND member_id IS NOT NULL
 GROUP BY member_id, speaker_name, party
 ORDER BY mentions DESC LIMIT 10;

-- Rebels: members who voted with the losing side most often
SELECT m.name, m.party, COUNT(*) AS on_the_losing_side
  FROM division_vote v
  JOIN member_resolved m USING (member_id)
  JOIN division d ON d.ext_id = v.division_ext_id
 WHERE v.lobby::text = CASE WHEN d.ayes_count < d.noes_count THEN 'aye' ELSE 'no' END
 GROUP BY m.member_id, m.name, m.party
 ORDER BY on_the_losing_side DESC LIMIT 10;

-- Did the people who spoke in a debate also vote in its division?
SELECT DISTINCT s.speaker_name, v.lobby
  FROM speech s
  JOIN division d ON d.debate_ext_id = s.debate_ext_id
  LEFT JOIN division_vote v
         ON v.division_ext_id = d.ext_id AND v.member_id = s.member_id
 WHERE s.debate_ext_id = '9969e436-926f-463c-8323-059d1158df4a';
```

---

## Development

```bash
pytest                      # 357 tests
pytest --cov=hansard        # 87% coverage
ruff check . && ruff format --check .
mypy                        # strict
```

Tests run against **real captured API payloads** under `tests/fixtures/`, and
database tests against **a real Postgres**, not a mock or SQLite. Half of what
this phase added lives *in* the database — generated columns, enums, foreign
keys, UPSERT semantics — and a fake would let all of it pass while being wrong.
They skip cleanly when no container is running, so the pure-logic tests still
run anywhere.

HTTP is intercepted at the transport layer with `respx`; nothing touches the
network. `hansard ingest --limit-days 1` is the smoke test that does.

The test database is a separate one (`hansard_test`), recreated per session, so
running the suite can never destroy data you spent twenty minutes fetching.

---

## Idempotency

Re-running must produce the same database, not a second copy of it.

```bash
hansard ingest --start 2026-03-02 --end 2026-03-04   # inserted 105
hansard ingest --start 2026-03-02 --end 2026-03-04   # unchanged 105, written 0
```

Four things make that true:

1. **Every table is keyed on an identifier Parliament owns** — never on an
   autoincrement of ours. That alone turns a re-fetch into an UPSERT.
2. **A content hash short-circuits unchanged work**, so a repeat run writes
   nothing at all.
3. **Transcripts and vote lists are replaced, not merged.** A revision can
   *remove* a row; an upsert alone would leave the withdrawn one for ever.
4. **Derived counts are recomputed, never incremented**, so they cannot drift.

Failure is designed for, not hoped against. One transaction per debate means a
network failure ninety minutes in leaves everything already fetched committed. A
day is marked complete only if *nothing* on it failed, so `--resume` can never
skip past a day it knows is short. And a job that crashes still closes its
`ingest_run` row — including on Ctrl-C — so nothing is left saying `running` for
ever.

---

## What this is not

- **Two sources, not four.** Bills, committees and written answers are separate
  APIs.
- **One house, four months.** Lords works (`--house Lords`) but is not what the
  defaults or the numbers above describe.
- **Spoken debates only.** `search/debates` does not return written statements.
- **One machine.** The scheduler is a single process, not a distributed queue.
- **Retrieval, not analysis.** The RAG layer finds and quotes passages. It does
  not classify stance, and it should not be trusted to: knowing an MP spoke in a
  debate is easy, knowing whether they spoke *for* or *against* is a reading
  task, and a wrong stance label is invisible in aggregate.

Natural next steps: written statements and answers, the Lords, TWFY's curated
`person_id` as a cross-era identity layer, select committee evidence (which
needs scraping -- it is published as HTML and PDF, not through a transcript
API), and a longer date range.

---

Public UK Parliament data, used under the
[Open Parliament Licence](https://www.parliament.uk/site-information/copyright-parliament/open-parliament-licence/).
No authentication required — please keep the rate limit polite.
