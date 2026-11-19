-- Document versions. PostgreSQL is the source of truth: a document has many versions and at most one active one.
-- A version row IS its indexing job (claimed_by, attempts, heartbeat...), exactly like a document row was before.
-- Additive only: the job columns of `documents` (attempts, claimed_by, heartbeat_at, started_at) stay in place,
-- unused; `documents.state / chunks / error / size_bytes` become a summary kept in step by rag/db.py.
CREATE TABLE document_versions (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id    uuid NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    version_number integer NOT NULL CHECK (version_number >= 1),
    file_name      text NOT NULL,
    -- NULL only for versions registered by this migration; rag.versioning.backfill_legacy fills it from the file
    file_hash      text,
    file_size      bigint NOT NULL,
    -- relative to STORAGE_DIR. Old files stay where they are: "<document id>/<name>"
    file_path      text NOT NULL,
    status         text NOT NULL CHECK (status IN (
                       'UPLOADED', 'QUEUED', 'PROCESSING', 'EMBEDDING', 'INDEXING', 'VERIFYING',
                       'READY', 'SUPERSEDED', 'FAILED')),
    chunk_count    integer,
    error_message  text,
    attempts       integer NOT NULL DEFAULT 0,
    claimed_by     text,
    heartbeat_at   timestamptz,
    started_at     timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    indexed_at     timestamptz,
    activated_at   timestamptz,
    UNIQUE (document_id, version_number),
    UNIQUE (id, document_id)  -- lets documents point at a version of ITSELF only (see the foreign key below)
);

CREATE INDEX document_versions_document_status_idx ON document_versions (document_id, status);
CREATE INDEX document_versions_queue_idx ON document_versions (created_at) WHERE status = 'QUEUED';
CREATE INDEX document_versions_inflight_idx ON document_versions (heartbeat_at)
    WHERE status IN ('PROCESSING', 'EMBEDDING', 'INDEXING', 'VERIFYING');

-- documents <-> document_versions reference each other. The composite key makes the active version belong to the
-- same document, and the check is deferred so a document and its versions can be deleted together.
ALTER TABLE documents ADD COLUMN active_version_id uuid;
ALTER TABLE documents ADD CONSTRAINT documents_active_version_fk
    FOREIGN KEY (active_version_id, id) REFERENCES document_versions (id, document_id)
    DEFERRABLE INITIALLY DEFERRED;

-- Every existing document becomes version 1. A finished one (state 'done') is READY and active; its vectors in
-- Qdrant get their version_id from rag.versioning.backfill_legacy at the next start.
INSERT INTO document_versions (document_id, version_number, file_name, file_size, file_path, status, chunk_count,
                               error_message, attempts, claimed_by, heartbeat_at, started_at, created_at,
                               updated_at, indexed_at, activated_at)
SELECT id, 1, name, size_bytes, id::text || '/' || name,
       CASE state WHEN 'uploading' THEN 'UPLOADED' WHEN 'queued' THEN 'QUEUED' WHEN 'processing' THEN 'PROCESSING'
                  WHEN 'done' THEN 'READY' ELSE 'FAILED' END,
       chunks, error, attempts, claimed_by, heartbeat_at, started_at, created_at, updated_at,
       CASE WHEN state = 'done' THEN updated_at END, CASE WHEN state = 'done' THEN updated_at END
FROM documents;

UPDATE documents d SET active_version_id = v.id
FROM document_versions v WHERE v.document_id = d.id AND v.status = 'READY';
