-- Attribute continuation paragraphs to the speaker who began the speech.
--
-- depends: 0002_canonical_party_in_speech_view
--
-- Hansard splits a long speech into paragraphs and names the speaker on the
-- first one only; every later paragraph arrives with no MemberId at all. The
-- effect is that per-member word counts undercount exactly the people who make
-- long speeches. In one Sentencing Bill debate the shadow justice minister is
-- recorded as saying 1,361 words when he actually said about 2,535 -- 46% of
-- his contribution invisible.
--
-- This is not recoverable from the API. The three contribution endpoints return
-- only the opening paragraph of each turn, and Hansard's own search index does
-- not contain the continuations: searching an exact phrase from one returns
-- nothing, while a phrase from the attributed paragraph is found immediately.
--
-- What makes it recoverable is that ItemIds run consecutively through a speech,
-- so an unnamed prose paragraph belongs to the last named speaker before it.
--
-- The inference is kept separate from the fact. `member_id` remains exactly
-- what Hansard stated. `speaker_member_id` is the resolved answer, and
-- `attribution` records which of the two it came from -- so any analysis can
-- decide for itself whether to trust a carried row.

CREATE TYPE attribution_source AS ENUM ('stated', 'carried', 'unattributed');

ALTER TABLE contribution
    ADD COLUMN speaker_member_id INTEGER REFERENCES member (member_id),
    ADD COLUMN attribution attribution_source NOT NULL DEFAULT 'unattributed';

-- Backfill the rows we already hold: anything Hansard named keeps its speaker
-- and is marked 'stated'. Carried rows are left for `hansard reattribute`,
-- which applies the same Python rules used at ingest, so there is one
-- implementation of the logic rather than a SQL copy that could drift from it.
UPDATE contribution
   SET speaker_member_id = member_id,
       attribution = 'stated'
 WHERE member_id IS NOT NULL;

CREATE INDEX contribution_speaker_member_idx ON contribution (speaker_member_id);
CREATE INDEX contribution_attribution_idx ON contribution (attribution)
    WHERE attribution = 'carried';

-- ---------------------------------------------------------------------------
-- The speech view now resolves through speaker_member_id.
-- ---------------------------------------------------------------------------

DROP VIEW IF EXISTS speech;

CREATE VIEW speech AS
SELECT
    c.item_id,
    c.debate_ext_id,
    d.title        AS debate_title,
    d.sitting_date,
    d.house,
    d.location,
    c.order_in_section,

    -- The resolved speaker, and how we know.
    c.speaker_member_id AS member_id,
    c.attribution,
    -- What Hansard itself named on this row, for anyone who wants to exclude
    -- inferred attributions entirely.
    c.member_id    AS stated_member_id,

    COALESCE(m.display_name, c.speaker_name, m.display_name_hansard) AS speaker_name,
    c.speaker_role,
    COALESCE(m.party, c.party, m.party_hansard)                      AS party,
    m.party_abbreviation,
    c.party                                                          AS party_stated,
    COALESCE(m.constituency, c.constituency, m.constituency_hansard) AS constituency,
    c.body_text,
    c.body_tsv,
    c.word_count
FROM contribution AS c
JOIN debate AS d ON d.ext_id = c.debate_ext_id
LEFT JOIN member AS m ON m.member_id = c.speaker_member_id
WHERE c.is_speech;
