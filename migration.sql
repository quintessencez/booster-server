-- Booster Platform v6 migration for Neon PostgreSQL
-- Run once in Neon SQL Editor. Safe to re-run.

ALTER TABLE users ADD COLUMN IF NOT EXISTS level INTEGER NOT NULL DEFAULT 1;
ALTER TABLE users ADD COLUMN IF NOT EXISTS xp BIGINT NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS admin_level_delta INTEGER NOT NULL DEFAULT 0;

ALTER TABLE boosts ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'platform';
ALTER TABLE boosts ADD COLUMN IF NOT EXISTS source_owner_id INTEGER NULL REFERENCES users(id);
ALTER TABLE boosts ADD COLUMN IF NOT EXISTS external_order_id TEXT NULL;
ALTER TABLE boosts ADD COLUMN IF NOT EXISTS source_meta TEXT NOT NULL DEFAULT '{}';
ALTER TABLE boosts ADD COLUMN IF NOT EXISTS booster_share NUMERIC(6,4) NOT NULL DEFAULT 0;
ALTER TABLE boosts ADD COLUMN IF NOT EXISTS owner_share NUMERIC(6,4) NOT NULL DEFAULT 0;
ALTER TABLE boosts ADD COLUMN IF NOT EXISTS platform_share NUMERIC(6,4) NOT NULL DEFAULT 0;

ALTER TABLE boosts ADD COLUMN IF NOT EXISTS pool_executor_share NUMERIC(6,4) NOT NULL DEFAULT 0;
ALTER TABLE boosts ADD COLUMN IF NOT EXISTS pool_owner_share NUMERIC(6,4) NOT NULL DEFAULT 0;

CREATE UNIQUE INDEX IF NOT EXISTS boosts_funpay_external_uidx
ON boosts(source, external_order_id)
WHERE source = 'funpay' AND external_order_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS level_audit (
    id BIGSERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    delta INTEGER NOT NULL,
    old_level INTEGER NOT NULL,
    new_level INTEGER NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    admin_id INTEGER NULL REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS funpay_accounts (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    golden_key TEXT NOT NULL,
    funpay_username TEXT,
    funpay_connected BOOLEAN NOT NULL DEFAULT FALSE,
    funpay_connected_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS boosts_source_owner_idx ON boosts(source_owner_id, status);
CREATE INDEX IF NOT EXISTS boosts_status_idx ON boosts(status);
CREATE INDEX IF NOT EXISTS level_audit_user_idx ON level_audit(user_id, created_at DESC);

-- Existing boosters start at Level 1 unless you want to seed XP manually.
UPDATE users
SET level = GREATEST(1, LEAST(50, COALESCE(level, 1))),
    xp = GREATEST(0, COALESCE(xp, 0)),
    admin_level_delta = COALESCE(admin_level_delta, 0)
WHERE role = 'booster';

-- Главный ADMIN_KEY не трогаем: он уже задаётся через Render Environment.

-- FunPay chat mirror: private chats and messages are persisted on the server.
CREATE TABLE IF NOT EXISTS funpay_chats (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    chat_id TEXT NOT NULL,
    chat_name TEXT NOT NULL DEFAULT 'FunPay',
    last_message_text TEXT NOT NULL DEFAULT '',
    unread BOOLEAN NOT NULL DEFAULT FALSE,
    last_synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, chat_id)
);

CREATE TABLE IF NOT EXISTS funpay_messages (
    id BIGSERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    chat_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    author_id TEXT,
    author_name TEXT NOT NULL DEFAULT 'FunPay',
    message TEXT NOT NULL DEFAULT '',
    by_bot BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, chat_id, message_id)
);

CREATE INDEX IF NOT EXISTS funpay_chats_user_sync_idx
ON funpay_chats(user_id, last_synced_at DESC);

CREATE INDEX IF NOT EXISTS funpay_messages_chat_created_idx
ON funpay_messages(user_id, chat_id, created_at ASC);


-- FunPay public seller profile/cache and calculator lot pricing.
CREATE TABLE IF NOT EXISTS funpay_profiles (
    funpay_user_id TEXT PRIMARY KEY,
    username TEXT NOT NULL DEFAULT '',
    avatar_url TEXT,
    rating NUMERIC(5,2),
    review_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT '',
    profile_url TEXT,
    raw_profile JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS funpay_profile_reviews (
    funpay_user_id TEXT NOT NULL REFERENCES funpay_profiles(funpay_user_id) ON DELETE CASCADE,
    review_id TEXT NOT NULL,
    author_id TEXT,
    author_name TEXT NOT NULL DEFAULT '',
    rating INTEGER,
    text TEXT NOT NULL DEFAULT '',
    reply TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ,
    PRIMARY KEY (funpay_user_id, review_id)
);
CREATE TABLE IF NOT EXISTS funpay_profile_lots (
    funpay_user_id TEXT NOT NULL REFERENCES funpay_profiles(funpay_user_id) ON DELETE CASCADE,
    lot_id TEXT NOT NULL,
    game TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    price NUMERIC(12,2),
    url TEXT,
    raw_lot JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (funpay_user_id, lot_id)
);
CREATE INDEX IF NOT EXISTS funpay_profile_lots_game_idx ON funpay_profile_lots(funpay_user_id, game, updated_at DESC);
CREATE INDEX IF NOT EXISTS funpay_profile_reviews_idx ON funpay_profile_reviews(funpay_user_id, created_at DESC);
