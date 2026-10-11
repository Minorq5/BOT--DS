-- Veritas: начальная схема. Все времена — UNIX-секунды (UTC).

CREATE TABLE IF NOT EXISTS managed_channels (
    key         TEXT PRIMARY KEY,
    channel_id  INTEGER NOT NULL,
    category_id INTEGER
);

CREATE TABLE IF NOT EXISTS managed_messages (
    key        TEXT PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS applications (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    answers_json TEXT    NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'pending',
    flags        TEXT    NOT NULL DEFAULT '',
    decided_by   INTEGER,
    decided_at   REAL,
    reason       TEXT,
    created_at   REAL    NOT NULL,
    message_id   INTEGER,
    channel_id   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_applications_user ON applications(user_id);

CREATE TABLE IF NOT EXISTS verification_attempts (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    ts      REAL    NOT NULL,
    result  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attempts_user ON verification_attempts(user_id, ts);

CREATE TABLE IF NOT EXISTS tickets (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    number            INTEGER NOT NULL UNIQUE,
    channel_id        INTEGER,
    user_id           INTEGER NOT NULL,
    type              TEXT    NOT NULL,
    status            TEXT    NOT NULL DEFAULT 'open',
    claimed_by        INTEGER,
    created_at        REAL    NOT NULL,
    closed_at         REAL,
    close_reason      TEXT,
    closed_by         INTEGER,
    last_activity     REAL    NOT NULL,
    inactivity_warned INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tickets_user ON tickets(user_id, status);
CREATE INDEX IF NOT EXISTS idx_tickets_channel ON tickets(channel_id);

CREATE TABLE IF NOT EXISTS role_snapshots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    kind       TEXT    NOT NULL,
    roles_json TEXT    NOT NULL,
    ts         REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_user ON role_snapshots(user_id);

CREATE TABLE IF NOT EXISTS sanctions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    kind         TEXT    NOT NULL,
    moderator_id INTEGER,
    reason       TEXT,
    ts           REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sanctions_user ON sanctions(user_id, kind);

CREATE TABLE IF NOT EXISTS exiled (
    user_id INTEGER PRIMARY KEY,
    ts      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS liars (
    user_id INTEGER PRIMARY KEY,
    ts      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS activity (
    user_id  INTEGER NOT NULL,
    day      TEXT    NOT NULL,
    messages INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day)
);

CREATE TABLE IF NOT EXISTS log_settings (
    category TEXT PRIMARY KEY,
    enabled  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS raid_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
