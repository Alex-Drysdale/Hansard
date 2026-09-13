-- Make party naming consistent in the `speech` view.
--
-- depends: 0001_initial_schema
--
-- The defect this fixes was visible the moment there was enough data to group
-- by: the same party appeared twice in every breakdown.
--
--     Labour              13,400
--     Lab                  4,877
--     Conservative         5,114
--     Con                  3,160
--
-- The two sources name parties differently. A transcript attribution carries an
-- abbreviation ("Lab"); the Members API states the full name ("Labour"). The
-- original view preferred whichever the transcript had, so a speech was labelled
-- one way or the other depending on whether Hansard happened to state the party
-- on that particular row -- which it only does on a member's first turn.
--
-- Preferring the Members API value makes the naming consistent. That is a real
-- trade, not a free win: `member.party` is the member's *latest* party, so a
-- speech made before a defection is now labelled with the party they ended up
-- in. The row-level truth is not discarded -- it is exposed as `party_stated`,
-- which is NULL except where Hansard actually said so, and is the column to use
-- if you care what someone's affiliation was on the day.

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
    c.member_id,
    COALESCE(m.display_name, c.speaker_name, m.display_name_hansard) AS speaker_name,
    c.speaker_role,

    -- Canonical party name, consistent across every row for a given member.
    COALESCE(m.party, c.party, m.party_hansard)                      AS party,
    m.party_abbreviation,
    -- What Hansard stated on this row, if anything. Use this for the
    -- affiliation at the time of the speech rather than today.
    c.party                                                          AS party_stated,

    COALESCE(m.constituency, c.constituency, m.constituency_hansard) AS constituency,
    c.body_text,
    c.body_tsv,
    c.word_count
FROM contribution AS c
JOIN debate AS d ON d.ext_id = c.debate_ext_id
LEFT JOIN member AS m ON m.member_id = c.member_id
WHERE c.is_speech;
