-- Where a running job is (roadmap T0.3). The worker writes it while it parses, chunks, embeds and indexes a version, so
-- the page can show "page 120 of 381" and a stalled job can be told from a slow one. The columns are only meaningful
-- while a worker holds the version (PROCESSING .. VERIFYING) and are NULL otherwise.
-- Additive only: nullable columns, no existing column or row is touched.
ALTER TABLE document_versions
    ADD COLUMN progress_unit  text,         -- 'pages' (parsing), 'assembling', 'chunks' (cutting, embedding, indexing)
    ADD COLUMN progress_done  integer,      -- units finished so far
    ADD COLUMN progress_total integer,      -- units in all, NULL while not known yet
    ADD COLUMN progress_at    timestamptz,  -- when the position last changed
    ADD CONSTRAINT document_versions_progress_valid
        CHECK ((progress_done IS NULL OR progress_done >= 0) AND (progress_total IS NULL OR progress_total >= 0));
