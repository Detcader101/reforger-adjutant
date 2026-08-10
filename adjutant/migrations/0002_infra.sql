-- Cross-feature infrastructure: durable panel messages, generic per-guild
-- key/value state, and an idempotency log for one-shot posts. Ported from
-- Ehrgeiz Godhand's crash-safe idempotent-posting substrate — see
-- adjutant/services/persistence.py for the access layer and its contract.

-- One durable message per (guild, kind) — a cog's editable "control
-- panel" (a live map render, a setup summary, a roster, ...). Re-posting
-- upserts the row rather than accumulating stale messages.
CREATE TABLE panels (
    guild_id    INTEGER NOT NULL REFERENCES guilds(guild_id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    channel_id  INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    PRIMARY KEY (guild_id, kind)
);

-- Generic per-guild key/value store for small bits of state that don't
-- justify a dedicated table (last-sweep timestamps, one-shot
-- dismissals, ...). Keep keys namespaced (e.g. 'events:last_reminder_sweep')
-- so unrelated callers don't collide.
CREATE TABLE bot_state (
    guild_id    INTEGER NOT NULL REFERENCES guilds(guild_id) ON DELETE CASCADE,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (guild_id, key)
);

-- Idempotency log for "post once" operations. A background task that
-- emits a Discord message and might be retried after a crash should
-- check here first — a present row means the work already happened.
-- `identity` is caller-chosen and deterministic for the occurrence
-- being posted (e.g. an event id, or an ISO week like '2026-W17' for a
-- weekly digest) — it is NOT a Discord message id.
CREATE TABLE posted_messages (
    kind        TEXT NOT NULL,
    identity    TEXT NOT NULL,
    guild_id    INTEGER NOT NULL REFERENCES guilds(guild_id) ON DELETE CASCADE,
    channel_id  INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    posted_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (kind, identity, guild_id)
);

CREATE INDEX idx_posted_messages_message ON posted_messages(message_id);
