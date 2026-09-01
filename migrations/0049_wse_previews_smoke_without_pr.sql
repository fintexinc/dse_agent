-- rc.131 — the preview smoke (`preview check ui|deployable`) proves the preview
-- OUTSIDE an item: there is no PR. `wse_previews` is keyed by work_item_id
-- (the degenerate item), and `pr_number` becomes what it always was for a
-- preview that is not a PR's: unknown.
ALTER TABLE wse_previews ALTER COLUMN pr_number DROP NOT NULL;
