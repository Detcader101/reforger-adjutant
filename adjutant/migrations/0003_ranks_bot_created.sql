-- Tracks whether a rank's backing Discord role was created by the bot
-- (e.g. /setup's default ladder) versus linked to an existing admin role
-- via /rank ladder-add. /teardown only deletes roles it created.
ALTER TABLE ranks ADD COLUMN bot_created INTEGER NOT NULL DEFAULT 0;
