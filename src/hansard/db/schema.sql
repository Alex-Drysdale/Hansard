-- Hansard local store, schema v1.
--
-- Design notes worth knowing before you change anything here:
--
--  * Every table keyed on an identifier the API itself owns (debate.ext_id,
--    contribution.item_id). That is what makes writes idempotent: re-running an
--    ingest UPSERTs onto the same row instead of appending a near-duplicate.
--
--  * Timestamps we generate are UTC ISO-8601 strings; dates from Hansard are
--    stored as bare YYYY-MM-DD so that string comparison equals date ordering.
--
--  * content_hash lets a re-run distinguish "unchanged" from "revised" without
--    diffing every field, which is what makes the run summary meaningful.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- Provenance: which slice of Hansard has been pulled, and how it went.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS ingest_run (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at           TEXT    NOT NULL,
    finished_at          TEXT,
    status               TEXT    NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
    house                TEXT    NOT NULL CHECK (house IN ('Commons', 'Lords')),
    start_date           TEXT    NOT NULL,
    end_date             TEXT    NOT NULL,
    sitting_days         INTEGER NOT NULL DEFAULT 0,
    debates_seen         INTEGER NOT NULL DEFAULT 0,
    debates_inserted     INTEGER NOT NULL DEFAULT 0,
    debates_updated      INTEGER NOT NULL DEFAULT 0,
    debates_unchanged    INTEGER NOT NULL DEFAULT 0,
    contributions_seen   INTEGER NOT NULL DEFAULT 0,
    contributions_written INTEGER NOT NULL DEFAULT 0,
    errors               INTEGER NOT NULL DEFAULT 0,
    error_message        TEXT
);

-- One row per (house, day) we have confirmed sat and enumerated. Its presence
-- is the resume marker: `ingest --resume` skips days already complete.
CREATE TABLE IF NOT EXISTS sitting_day (
    house           TEXT    NOT NULL CHECK (house IN ('Commons', 'Lords')),
    sitting_date    TEXT    NOT NULL,
    debate_count    INTEGER NOT NULL DEFAULT 0,
    first_seen_at   TEXT    NOT NULL,
    last_fetched_at TEXT    NOT NULL,
    completed_at    TEXT,
    PRIMARY KEY (house, sitting_date)
);

-- ---------------------------------------------------------------------------
-- Core content
-- ---------------------------------------------------------------------------

-- A debate section: one titled block of a sitting day, e.g. "Oil Refining
-- Sector". Sections nest, and the API returns children both inline under their
-- parent AND as their own top-level search hits -- so this table is the single
-- place a section can exist, and parent_ext_id records the tree.
--
-- parent_ext_id is intentionally NOT a foreign key. The topmost ancestor is a
-- day-root container ("Commons Chamber") that has no fetchable detail payload,
-- so a real FK would reject legitimate rows. dedup-check verifies it instead.
CREATE TABLE IF NOT EXISTS debate (
    ext_id               TEXT    PRIMARY KEY,
    hansard_id           INTEGER NOT NULL,
    parent_ext_id        TEXT,
    parent_title         TEXT,
    -- Position in the day's section tree, from the payload's Navigator trail.
    -- 1 = the day-root container, 2 = a top-level section, 3+ = nested. This is
    -- what lets check_orphan_parents tell "parent is the unstored day root"
    -- (fine, and expected) from "parent is missing" (a real gap).
    depth                INTEGER NOT NULL DEFAULT 1,
    title                TEXT    NOT NULL,
    house                TEXT    NOT NULL CHECK (house IN ('Commons', 'Lords')),
    sitting_date         TEXT    NOT NULL,
    location             TEXT,
    hrs_tag              TEXT,
    debate_type_id       INTEGER,
    volume_no            INTEGER,
    content_last_updated TEXT,
    contribution_count   INTEGER NOT NULL DEFAULT 0,
    word_count           INTEGER NOT NULL DEFAULT 0,
    content_hash         TEXT    NOT NULL,
    first_seen_at        TEXT    NOT NULL,
    last_fetched_at      TEXT    NOT NULL,
    revision             INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_debate_date   ON debate (house, sitting_date);
CREATE INDEX IF NOT EXISTS idx_debate_parent ON debate (parent_ext_id);
CREATE INDEX IF NOT EXISTS idx_debate_hash   ON debate (content_hash);

-- A speaker as Hansard identifies them. Hansard gives us no member endpoint in
-- this phase, so party and constituency are parsed out of the AttributedTo
-- string; see hansard.pipeline.normalise.parse_attribution.
CREATE TABLE IF NOT EXISTS member (
    member_id       INTEGER PRIMARY KEY,
    display_name    TEXT    NOT NULL,
    party           TEXT,
    constituency    TEXT,
    contribution_count INTEGER NOT NULL DEFAULT 0,
    first_seen_at   TEXT    NOT NULL,
    last_seen_at    TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_member_party ON member (party);

-- One row of a transcript. item_id is globally unique across all of Hansard,
-- which is what lets a re-run upsert cleanly.
--
-- Not every row is a speech: ItemType/hrs_tag also cover timestamps, column
-- markers and division blocks. is_speech is the derived flag downstream
-- queries should filter on rather than re-deriving that rule each time.
CREATE TABLE IF NOT EXISTS contribution (
    item_id          INTEGER PRIMARY KEY,
    debate_ext_id    TEXT    NOT NULL REFERENCES debate (ext_id) ON DELETE CASCADE,
    external_id      TEXT,
    order_in_section INTEGER NOT NULL,
    item_type        TEXT    NOT NULL,
    hrs_tag          TEXT,
    is_speech        INTEGER NOT NULL DEFAULT 0 CHECK (is_speech IN (0, 1)),
    member_id        INTEGER REFERENCES member (member_id),
    attributed_to    TEXT,
    speaker_name     TEXT,
    speaker_key      TEXT,
    -- Ministerial or presiding role, when the speaker was addressed by office
    -- rather than by name ("The Minister for Policing and Crime").
    speaker_role     TEXT,
    party            TEXT,
    constituency     TEXT,
    body_html        TEXT,
    body_text        TEXT,
    word_count       INTEGER NOT NULL DEFAULT 0,
    timecode         TEXT,
    is_reiteration   INTEGER NOT NULL DEFAULT 0 CHECK (is_reiteration IN (0, 1)),
    content_hash     TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_contribution_debate  ON contribution (debate_ext_id, order_in_section);
CREATE INDEX IF NOT EXISTS idx_contribution_member  ON contribution (member_id);
CREATE INDEX IF NOT EXISTS idx_contribution_speaker ON contribution (speaker_key);
CREATE INDEX IF NOT EXISTS idx_contribution_hash    ON contribution (content_hash);

-- ---------------------------------------------------------------------------
-- Full-text search over speech text.
--
-- An external-content FTS5 table: the index stores no second copy of the text,
-- it points back at `contribution` by rowid. So there is exactly one source of
-- truth for the body text, and no way for the two to disagree about wording.
--
-- The price of external content is that the index does not update itself, and
-- a plain DELETE against it is not valid -- entries must be retired with FTS5's
-- 'delete' command, quoting the *old* text. The triggers below do exactly that,
-- which means application code never touches the index at all: write to
-- `contribution` and the index follows. One owner for one invariant.
--
-- The index mirrors every row of `contribution`, not just speeches, because
-- that 1:1 correspondence is what FTS5's own 'integrity-check' verifies.
-- Filtering to real speeches is the query's job (see the `speech` view).
-- ---------------------------------------------------------------------------

CREATE VIRTUAL TABLE IF NOT EXISTS contribution_fts USING fts5 (
    body_text,
    content = 'contribution',
    content_rowid = 'item_id',
    tokenize = 'porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS contribution_fts_insert
AFTER INSERT ON contribution BEGIN
    INSERT INTO contribution_fts (rowid, body_text) VALUES (new.item_id, new.body_text);
END;

CREATE TRIGGER IF NOT EXISTS contribution_fts_delete
AFTER DELETE ON contribution BEGIN
    INSERT INTO contribution_fts (contribution_fts, rowid, body_text)
    VALUES ('delete', old.item_id, old.body_text);
END;

CREATE TRIGGER IF NOT EXISTS contribution_fts_update
AFTER UPDATE ON contribution BEGIN
    INSERT INTO contribution_fts (contribution_fts, rowid, body_text)
    VALUES ('delete', old.item_id, old.body_text);
    INSERT INTO contribution_fts (rowid, body_text) VALUES (new.item_id, new.body_text);
END;

-- ---------------------------------------------------------------------------
-- Convenience views
--
-- The tables above hold what Hansard said, unaltered. This view is where
-- convenience lives, so that "raw" and "derived" never get confused.
--
-- The party fallback matters more than it looks. Hansard states a member's
-- party only on their *first* attribution in a debate ("Jim Shannon (Strangford)
-- (DUP)"); every later turn is just the bare name. Ministers speaking by office
-- never carry one at all. So contribution.party is populated for a minority of
-- rows, while member.party -- accumulated across every debate we hold -- is the
-- better answer. Reading the view gets you that; reading the table gets you the
-- literal source.
-- ---------------------------------------------------------------------------

CREATE VIEW IF NOT EXISTS speech AS
SELECT
    c.item_id,
    c.debate_ext_id,
    d.title        AS debate_title,
    d.sitting_date,
    d.house,
    d.location,
    c.order_in_section,
    c.member_id,
    COALESCE(c.speaker_name, m.display_name)  AS speaker_name,
    c.speaker_role,
    COALESCE(c.party, m.party)                AS party,
    COALESCE(c.constituency, m.constituency)  AS constituency,
    c.body_text,
    c.word_count
FROM contribution AS c
JOIN debate AS d ON d.ext_id = c.debate_ext_id
LEFT JOIN member AS m ON m.member_id = c.member_id
WHERE c.is_speech = 1;
