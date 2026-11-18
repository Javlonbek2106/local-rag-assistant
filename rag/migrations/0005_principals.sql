-- Who acts (roadmap M4.1a, ADR-P16). A principal is the stable identity that the connector's sessions, ownership and
-- audit refer to. Exactly one exists for now: the local owner of this installation, created below. It is never renamed,
-- remapped or deleted; when real users arrive (a later phase) the first administrator CLAIMS this principal with a
-- compare-and-set on user_id, so nothing that refers to it has to be rewritten.
-- Additive only: a new table; no existing table or row is touched. user_id has no foreign key yet: there is no users
-- table until that later phase adds it, together with the constraint.
CREATE TABLE principals (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind          text        NOT NULL,
    display_label text        NOT NULL DEFAULT 'local owner',
    user_id       uuid,                              -- NULL until the principal is claimed by a user
    claimed_at    timestamptz,                       -- set together with user_id, never alone
    disabled_at   timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT principals_claim_consistent CHECK ((user_id IS NULL) = (claimed_at IS NULL))
);

-- At most one local owner, whatever happens at start-up: two processes that both try to make it cannot both succeed.
CREATE UNIQUE INDEX principals_local_owner_key ON principals (kind) WHERE kind = 'local-owner';

INSERT INTO principals (kind) VALUES ('local-owner') ON CONFLICT DO NOTHING;
