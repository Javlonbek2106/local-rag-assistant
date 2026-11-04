-- Documents and the indexing job queue. A document row IS its job: state says where it is.
-- 'uploading' means the file is still being saved: workers never take it until it becomes 'queued'.
CREATE TABLE documents (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name          text NOT NULL,
    size_bytes    bigint NOT NULL,
    state         text NOT NULL CHECK (state IN ('uploading', 'queued', 'processing', 'done', 'failed')),
    chunks        integer,
    error         text,
    attempts      integer NOT NULL DEFAULT 0,
    claimed_by    text,
    heartbeat_at  timestamptz,
    started_at    timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

-- Re-uploading the same name replaces the document. (Stage 3 will scope names per department.)
CREATE UNIQUE INDEX documents_name_key ON documents (name);
CREATE INDEX documents_queue_idx ON documents (created_at) WHERE state = 'queued';
CREATE INDEX documents_processing_idx ON documents (heartbeat_at) WHERE state = 'processing';
