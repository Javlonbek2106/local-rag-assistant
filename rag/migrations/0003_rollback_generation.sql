-- A rollback is a durable user decision. Each document counts its rollbacks (activation_generation); each version
-- remembers the count at the moment it was uploaded (uploaded_generation). A version may become active only if no
-- rollback has happened since it was uploaded, so a version that was still queued or being indexed when the user
-- rolled back can never replace the chosen version. Additive only; existing rows start at generation 0.
ALTER TABLE documents ADD COLUMN activation_generation bigint NOT NULL DEFAULT 0;
ALTER TABLE document_versions ADD COLUMN uploaded_generation bigint NOT NULL DEFAULT 0;
