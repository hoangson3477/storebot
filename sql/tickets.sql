BEGIN;
CREATE TABLE IF NOT EXISTS storebot.ticket_settings (
    guild_id BIGINT PRIMARY KEY,
    category_id BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS storebot.tickets (
    order_code BIGINT PRIMARY KEY REFERENCES storebot.orders(code),
    guild_id BIGINT NOT NULL,
    buyer_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL UNIQUE,
    closed BIGINT NOT NULL DEFAULT 0,
    created_at BIGINT NOT NULL
);
ALTER TABLE storebot.ticket_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE storebot.tickets ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON storebot.ticket_settings, storebot.tickets FROM PUBLIC, anon, authenticated;
COMMIT;
