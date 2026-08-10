-- Guild-level configuration. One row per guild the bot is set up in.
CREATE TABLE guilds (
    guild_id        INTEGER PRIMARY KEY,
    minimal_mode    INTEGER NOT NULL DEFAULT 0,
    audit_channel   INTEGER,            -- channel id for admin audit log, nullable
    features        TEXT NOT NULL DEFAULT '{}',  -- json: {"map": true, ...}
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Configurable rank ladder per guild; higher position = more senior.
CREATE TABLE ranks (
    guild_id    INTEGER NOT NULL REFERENCES guilds(guild_id) ON DELETE CASCADE,
    role_id     INTEGER NOT NULL,       -- discord role backing this rank
    position    INTEGER NOT NULL,
    name        TEXT NOT NULL,
    PRIMARY KEY (guild_id, role_id)
);

-- Minimum rank position required per bot permission, per guild.
CREATE TABLE permissions (
    guild_id    INTEGER NOT NULL REFERENCES guilds(guild_id) ON DELETE CASCADE,
    permission  TEXT NOT NULL,          -- e.g. 'map.edit', 'events.create', 'teams.manage'
    min_rank    INTEGER NOT NULL,
    PRIMARY KEY (guild_id, permission)
);

-- Role grants the bot is responsible for (perma / temp / event).
CREATE TABLE role_grants (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL REFERENCES guilds(guild_id) ON DELETE CASCADE,
    user_id     INTEGER NOT NULL,
    role_id     INTEGER NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('perma', 'temp', 'event')),
    expires_at  TEXT,                   -- ISO utc; NULL for perma
    event_id    INTEGER,                -- set when kind='event'
    granted_by  INTEGER NOT NULL,
    granted_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_role_grants_expiry ON role_grants (expires_at)
    WHERE expires_at IS NOT NULL;

-- Team units: a role plus a locked category of channels.
CREATE TABLE teams (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL REFERENCES guilds(guild_id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    role_id     INTEGER NOT NULL,
    category_id INTEGER NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Events / ops.
CREATE TABLE events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id      INTEGER NOT NULL REFERENCES guilds(guild_id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    starts_at     TEXT NOT NULL,        -- ISO utc
    created_by    INTEGER NOT NULL,
    announce_channel INTEGER,
    announce_message INTEGER,
    event_role_id INTEGER,              -- temp role granted to signups
    status        TEXT NOT NULL DEFAULT 'open'
                  CHECK (status IN ('open', 'closed', 'done', 'cancelled')),
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE event_signups (
    event_id    INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    user_id     INTEGER NOT NULL,
    signed_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (event_id, user_id)
);

-- Map state: one named map per guild-channel, markers as rows.
CREATE TABLE maps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL REFERENCES guilds(guild_id) ON DELETE CASCADE,
    channel_id  INTEGER NOT NULL,
    message_id  INTEGER,
    terrain     TEXT NOT NULL DEFAULT 'everon',
    team_id     INTEGER REFERENCES teams(id) ON DELETE CASCADE,  -- NULL = shared map
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE map_markers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    map_id      INTEGER NOT NULL REFERENCES maps(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,          -- 'objective', 'enemy', 'friendly', 'note', ...
    label       TEXT NOT NULL DEFAULT '',
    x           REAL NOT NULL,          -- world coords; transform to px at render
    y           REAL NOT NULL,
    placed_by   INTEGER NOT NULL,
    placed_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Optional game-server link config per guild.
CREATE TABLE server_links (
    guild_id    INTEGER PRIMARY KEY REFERENCES guilds(guild_id) ON DELETE CASCADE,
    backend     TEXT NOT NULL DEFAULT 'null'
                CHECK (backend IN ('null', 'a2s', 'rcon', 'feed')),
    host        TEXT,
    port        INTEGER,
    secret      TEXT                    -- rcon password / feed token, if applicable
);

-- Abuse / rate-limit log (admin-facing, never echoed across teams).
CREATE TABLE incidents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    kind        TEXT NOT NULL,          -- 'rate_limit', 'permission_denied', ...
    detail      TEXT NOT NULL DEFAULT '',
    at          TEXT NOT NULL DEFAULT (datetime('now'))
);
