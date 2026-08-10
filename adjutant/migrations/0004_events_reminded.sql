-- Tracks whether an event's pre-start reminder has already gone out, so the
-- background loop in cogs/events.py doesn't re-notify on every 60s tick.
ALTER TABLE events ADD COLUMN reminded INTEGER NOT NULL DEFAULT 0;
