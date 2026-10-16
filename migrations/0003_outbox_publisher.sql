ALTER TABLE outbox_events ADD COLUMN IF NOT EXISTS event_key TEXT;
UPDATE outbox_events SET event_key = 'legacy:' || outbox_id::text WHERE event_key IS NULL;
ALTER TABLE outbox_events ALTER COLUMN event_key SET NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS outbox_event_key_uidx ON outbox_events(event_key);

ALTER TABLE outbox_events ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE outbox_events ADD COLUMN IF NOT EXISTS available_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE outbox_events ADD COLUMN IF NOT EXISTS lease_owner TEXT;
ALTER TABLE outbox_events ADD COLUMN IF NOT EXISTS lease_until TIMESTAMPTZ;
ALTER TABLE outbox_events ADD COLUMN IF NOT EXISTS last_error TEXT;

CREATE INDEX IF NOT EXISTS outbox_claim_idx
ON outbox_events(available_at, outbox_id)
WHERE published_at IS NULL;
