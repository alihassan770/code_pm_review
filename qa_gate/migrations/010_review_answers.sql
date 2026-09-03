-- Human answers to the questions a review could not resolve on its own.
--
-- The interpret phase reports "ambiguities": things whose answer changes what
-- would be checked — a threshold whose boundary is unstated, a rule with two
-- readings. Until now those were rendered and nothing more, which made them
-- decoration: the run carried on and guessed, and the person reading the page
-- had no way to correct it.
--
-- Stored on the run rather than in a table of their own because they are
-- answers to *this* run's questions. The same task reviewed next month against
-- different code may raise different ones, and inheriting last month's answer
-- to a question nobody asked this time would be worse than asking again.
--
-- Shape: {"0": {"question": "...", "answer": "...", "by": "login",
--               "at": "iso8601"}} keyed by the ambiguity's position in the list.
-- Keyed by position because that is what the page has to offer a box against,
-- and the list is regenerated wholesale whenever interpretation re-runs.

ALTER TABLE review_runs
    ADD COLUMN answers jsonb NOT NULL DEFAULT '{}'::jsonb;
