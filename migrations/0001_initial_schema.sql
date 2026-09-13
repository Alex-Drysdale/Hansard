-- Initial schema, ported from the Phase 1 SQLite store.
--
-- The port is not a transliteration. Postgres has real types, so the columns
-- that SQLite could only hold as TEXT or 0/1 integers are now DATE, TIMESTAMPTZ
-- and BOOLEAN, and the database rejects nonsense the application used to have
-- to be trusted about.
--
-- depends:

-- ---------------------------------------------------------------------------
-- Provenance
-- ---------------------------------------------------------------------------

CREATE TYPE run_status AS ENUM ('running', 'completed', 'failed');
CREATE TYPE house AS ENUM ('Commons', 'Lords');

-- One row per pipeline run, whatever the job. `job` distinguishes them, so the
-- scheduler's history and a hand-run ingest live in the same table.
CREATE TABLE ingest_run (
    id                    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job                   TEXT        NOT NULL,
    started_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at           TIMESTAMPTZ,
    status                run_status  NOT NULL,
    house                 house,
    start_date            DATE,
    end_date              DATE,
    sitting_days          INTEGER     NOT NULL DEFAULT 0,
    debates_seen          INTEGER     NOT NULL DEFAULT 0,
    debates_inserted      INTEGER     NOT NULL DEFAULT 0,
    debates_updated       INTEGER     NOT NULL DEFAULT 0,
    debates_unchanged     INTEGER     NOT NULL DEFAULT 0,
    contributions_seen    INTEGER     NOT NULL DEFAULT 0,
    contributions_written INTEGER     NOT NULL DEFAULT 0,
    errors                INTEGER     NOT NULL DEFAULT 0,
    error_message         TEXT
);

CREATE INDEX ingest_run_job_started_idx ON ingest_run (job, started_at DESC);

CREATE TABLE sitting_day (
    house           house       NOT NULL,
    sitting_date    DATE        NOT NULL,
    debate_count    INTEGER     NOT NULL DEFAULT 0,
    first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ,
    PRIMARY KEY (house, sitting_date)
);

-- ---------------------------------------------------------------------------
-- Members
--
-- member_id is the identifier Parliament itself issues, shared by the Hansard
-- API and the Members API. Making it the canonical key is what lets a second
-- source enrich these rows rather than sit beside them in a parallel universe.
--
-- Columns are split by provenance: `*_hansard` is what we parsed out of a
-- transcript attribution string, plain columns are what the Members API stated
-- outright. Keeping them apart means a disagreement between sources is
-- visible rather than silently resolved by whichever ran last.
-- ---------------------------------------------------------------------------

CREATE TABLE member (
    member_id           INTEGER PRIMARY KEY,

    -- Derived from Hansard transcripts (source 1).
    display_name_hansard TEXT,
    party_hansard        TEXT,
    constituency_hansard TEXT,

    -- Stated by the Members API (source 2). NULL until members sync has run.
    display_name        TEXT,
    full_title          TEXT,
    list_as             TEXT,
    party               TEXT,
    party_abbreviation  TEXT,
    constituency        TEXT,
    house               house,
    gender              TEXT,
    thumbnail_url       TEXT,
    membership_start    DATE,
    membership_end      DATE,
    is_current          BOOLEAN,
    synced_at           TIMESTAMPTZ,

    contribution_count  INTEGER     NOT NULL DEFAULT 0,
    first_seen_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX member_party_idx      ON member (party);
CREATE INDEX member_unsynced_idx   ON member (member_id) WHERE synced_at IS NULL;

-- The answer you almost always want: the API's value when we have it, the
-- transcript's when we do not.
CREATE VIEW member_resolved AS
SELECT
    member_id,
    COALESCE(display_name, display_name_hansard)   AS name,
    COALESCE(party, party_hansard)                 AS party,
    COALESCE(constituency, constituency_hansard)   AS constituency,
    party_abbreviation,
    house,
    is_current,
    contribution_count,
    (synced_at IS NOT NULL)                        AS from_members_api
FROM member;

-- ---------------------------------------------------------------------------
-- Debates and transcripts
-- ---------------------------------------------------------------------------

-- ext_id is TEXT, not UUID. Hansard uses at least three identifier formats for
-- a debate section: a UUID, a numeric string ("26011562000145"), and a
-- synthetic one ("DeferredDivisions2026-01-14"). A UUID column rejects the
-- latter two, which are 13 of the 1,962 sections in a single four-month window.
-- Identifiers are canonicalised on the way in (see normalise.canonical_ext_id)
-- so that a UUID's casing cannot break a join.
CREATE TABLE debate (
    ext_id               TEXT        PRIMARY KEY,
    hansard_id           BIGINT      NOT NULL,
    parent_ext_id        TEXT,
    parent_title         TEXT,
    -- Position in the day's section tree, from the payload's Navigator trail.
    -- 1 = day-root container, 2 = top-level section, 3+ = nested.
    depth                SMALLINT    NOT NULL DEFAULT 1,
    title                TEXT        NOT NULL,
    house                house       NOT NULL,
    sitting_date         DATE        NOT NULL,
    location             TEXT,
    hrs_tag              TEXT,
    debate_type_id       INTEGER,
    volume_no            INTEGER,
    content_last_updated TIMESTAMPTZ,
    contribution_count   INTEGER     NOT NULL DEFAULT 0,
    word_count           INTEGER     NOT NULL DEFAULT 0,
    content_hash         TEXT        NOT NULL,
    first_seen_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_fetched_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    revision             INTEGER     NOT NULL DEFAULT 1
);

-- parent_ext_id is deliberately not a foreign key: the topmost ancestor of a
-- day is a container with no fetchable payload, so a real FK would reject
-- legitimate rows. `depth` is what makes the integrity check precise instead.
CREATE INDEX debate_date_idx   ON debate (house, sitting_date);
CREATE INDEX debate_parent_idx ON debate (parent_ext_id);

CREATE TABLE contribution (
    item_id          BIGINT   PRIMARY KEY,
    debate_ext_id    TEXT     NOT NULL REFERENCES debate (ext_id) ON DELETE CASCADE,
    -- Also not always a UUID: about one in ten is a numeric string.
    external_id      TEXT,
    order_in_section INTEGER  NOT NULL,
    item_type        TEXT     NOT NULL,
    hrs_tag          TEXT,
    is_speech        BOOLEAN  NOT NULL DEFAULT FALSE,
    member_id        INTEGER  REFERENCES member (member_id),
    attributed_to    TEXT,
    speaker_name     TEXT,
    speaker_key      TEXT,
    speaker_role     TEXT,
    party            TEXT,
    constituency     TEXT,
    body_html        TEXT,
    body_text        TEXT,
    word_count       INTEGER  NOT NULL DEFAULT 0,
    timecode         TIMESTAMP,
    is_reiteration   BOOLEAN  NOT NULL DEFAULT FALSE,
    content_hash     TEXT     NOT NULL,

    -- Full-text search, maintained by the database rather than the application.
    -- GENERATED ALWAYS means it cannot drift from body_text -- there is no code
    -- path that could forget to update it, which was a live hazard under the
    -- SQLite external-content FTS5 table this replaces.
    body_tsv         tsvector GENERATED ALWAYS AS (to_tsvector('english', COALESCE(body_text, ''))) STORED
);

CREATE INDEX contribution_debate_idx  ON contribution (debate_ext_id, order_in_section);
CREATE INDEX contribution_member_idx  ON contribution (member_id);
CREATE INDEX contribution_speaker_idx ON contribution (speaker_key);
CREATE INDEX contribution_tsv_idx     ON contribution USING GIN (body_tsv)
    WHERE is_speech;

-- ---------------------------------------------------------------------------
-- Divisions
--
-- Hansard records that a division happened inside a debate's transcript, but
-- the votes themselves live behind separate endpoints. Storing them here turns
-- "who spoke" into "who spoke and how they voted".
-- ---------------------------------------------------------------------------

CREATE TYPE vote_lobby AS ENUM ('aye', 'no');

CREATE TABLE division (
    ext_id            TEXT        PRIMARY KEY,
    division_id       BIGINT      NOT NULL,
    debate_ext_id     TEXT        REFERENCES debate (ext_id) ON DELETE CASCADE,
    house             house       NOT NULL,
    division_date     DATE        NOT NULL,
    division_time     TIME,
    number            TEXT,
    debate_section    TEXT,
    -- The counts Hansard states. Deliberately kept separate from the number of
    -- rows in division_vote: the two legitimately differ (tellers are counted
    -- in the total but recorded apart), and a check verifies the relationship
    -- rather than the application quietly assuming it.
    ayes_count        INTEGER     NOT NULL DEFAULT 0,
    noes_count        INTEGER     NOT NULL DEFAULT 0,
    is_committee      BOOLEAN     NOT NULL DEFAULT FALSE,
    text_before_vote  TEXT,
    text_after_vote   TEXT,
    content_hash      TEXT        NOT NULL,
    first_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_fetched_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX division_debate_idx ON division (debate_ext_id);
CREATE INDEX division_date_idx   ON division (house, division_date);

CREATE TABLE division_vote (
    division_ext_id TEXT       NOT NULL REFERENCES division (ext_id) ON DELETE CASCADE,
    member_id       INTEGER    NOT NULL REFERENCES member (member_id),
    lobby           vote_lobby NOT NULL,
    is_teller       BOOLEAN    NOT NULL DEFAULT FALSE,
    list_as         TEXT,
    party_at_vote   TEXT,
    PRIMARY KEY (division_ext_id, member_id)
);

CREATE INDEX division_vote_member_idx ON division_vote (member_id);

-- ---------------------------------------------------------------------------
-- Convenience view
-- ---------------------------------------------------------------------------

CREATE VIEW speech AS
SELECT
    c.item_id,
    c.debate_ext_id,
    d.title        AS debate_title,
    d.sitting_date,
    d.house,
    d.location,
    c.order_in_section,
    c.member_id,
    COALESCE(m.display_name, c.speaker_name, m.display_name_hansard) AS speaker_name,
    c.speaker_role,
    COALESCE(c.party, m.party, m.party_hansard)                      AS party,
    COALESCE(c.constituency, m.constituency, m.constituency_hansard) AS constituency,
    c.body_text,
    c.body_tsv,
    c.word_count
FROM contribution AS c
JOIN debate AS d ON d.ext_id = c.debate_ext_id
LEFT JOIN member AS m ON m.member_id = c.member_id
WHERE c.is_speech;
