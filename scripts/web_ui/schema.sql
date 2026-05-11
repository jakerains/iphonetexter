-- imsg local web UI schema, version 1.

CREATE TABLE IF NOT EXISTS schema_meta (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS contacts (
    id                  INTEGER PRIMARY KEY,
    normalized_handle   TEXT NOT NULL UNIQUE,
    kind                TEXT NOT NULL DEFAULT 'unknown',
    region              TEXT,
    display_name        TEXT,
    last_known_service  TEXT,
    opted_out           INTEGER NOT NULL DEFAULT 0,
    opted_out_at        TEXT,
    opted_out_reason    TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_contacts_handle ON contacts(normalized_handle);
CREATE INDEX IF NOT EXISTS idx_contacts_opted_out ON contacts(opted_out);

CREATE TABLE IF NOT EXISTS lists (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    kind        TEXT NOT NULL DEFAULT 'named',  -- 'named' | 'adhoc'
    notes       TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS list_members (
    list_id     INTEGER NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
    contact_id  INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    added_at    TEXT NOT NULL,
    PRIMARY KEY (list_id, contact_id)
);

CREATE INDEX IF NOT EXISTS idx_list_members_contact ON list_members(contact_id);

CREATE TABLE IF NOT EXISTS sends (
    id              INTEGER PRIMARY KEY,
    contact_id      INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
    list_id         INTEGER REFERENCES lists(id) ON DELETE SET NULL,
    job_id          TEXT,
    chat_id         INTEGER,
    target_type     TEXT NOT NULL,    -- 'handle' | 'chat'
    target          TEXT NOT NULL,
    service         TEXT,
    region          TEXT,
    message_body    TEXT NOT NULL DEFAULT '',
    attachment_path TEXT,
    status          TEXT NOT NULL,
    message_rowid   INTEGER,
    guid            TEXT,
    error           TEXT,
    ts              TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sends_contact ON sends(contact_id);
CREATE INDEX IF NOT EXISTS idx_sends_list ON sends(list_id);
CREATE INDEX IF NOT EXISTS idx_sends_job ON sends(job_id);
CREATE INDEX IF NOT EXISTS idx_sends_ts ON sends(ts);
CREATE INDEX IF NOT EXISTS idx_sends_status ON sends(status);

CREATE TABLE IF NOT EXISTS received (
    id              INTEGER PRIMARY KEY,
    guid            TEXT NOT NULL UNIQUE,
    chat_id         INTEGER,
    contact_id      INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
    sender_handle   TEXT,
    text            TEXT NOT NULL DEFAULT '',
    is_reaction     INTEGER NOT NULL DEFAULT 0,
    received_at     TEXT,
    ingested_at     TEXT NOT NULL,
    message_rowid   INTEGER
);

CREATE INDEX IF NOT EXISTS idx_received_contact ON received(contact_id);
CREATE INDEX IF NOT EXISTS idx_received_chat ON received(chat_id);
CREATE INDEX IF NOT EXISTS idx_received_rowid ON received(message_rowid);
CREATE INDEX IF NOT EXISTS idx_received_ingested ON received(ingested_at);

CREATE TABLE IF NOT EXISTS opt_outs (
    id              INTEGER PRIMARY KEY,
    contact_id      INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    received_id     INTEGER REFERENCES received(id) ON DELETE SET NULL,
    matched_phrase  TEXT,
    processed_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_optouts_contact ON opt_outs(contact_id);

CREATE TABLE IF NOT EXISTS watch_state (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    last_message_rowid  INTEGER,
    last_seen_at        TEXT
);

INSERT OR IGNORE INTO watch_state (id, last_message_rowid, last_seen_at)
VALUES (1, NULL, NULL);
