PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS traders (
    address TEXT PRIMARY KEY, rank INTEGER, pnl REAL, worth REAL, volume REAL,
    trades INTEGER, in_top INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    key TEXT PRIMARY KEY, block INTEGER NOT NULL, ts INTEGER, tx TEXT NOT NULL,
    wallet TEXT NOT NULL, kind TEXT NOT NULL, token TEXT NOT NULL, qty TEXT NOT NULL,
    hype_wei TEXT, counter_token TEXT, counter_qty TEXT, approx INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_events_token_ts ON events(token, kind, ts);
CREATE INDEX IF NOT EXISTS ix_events_wallet ON events(wallet, block);
CREATE TABLE IF NOT EXISTS positions (
    wallet TEXT NOT NULL, token TEXT NOT NULL, qty TEXT NOT NULL, cost_wei TEXT NOT NULL,
    realized_wei TEXT NOT NULL, opened_at INTEGER, PRIMARY KEY (wallet, token));
CREATE TABLE IF NOT EXISTS tokens (address TEXT PRIMARY KEY, symbol TEXT, decimals INTEGER);
CREATE TABLE IF NOT EXISTS contracts (address TEXT PRIMARY KEY, is_contract INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS cluster_alerts (token TEXT PRIMARY KEY, ts INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE NOT NULL, text TEXT NOT NULL,
    created_at INTEGER NOT NULL, sent_at INTEGER, attempts INTEGER NOT NULL DEFAULT 0);
