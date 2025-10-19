-- pragma wird in Python gesetzt
CREATE TABLE IF NOT EXISTS meta(version INTEGER NOT NULL);
INSERT INTO meta(version) SELECT 1 WHERE NOT EXISTS(SELECT 1 FROM meta);

CREATE TABLE IF NOT EXISTS commands(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  type TEXT NOT NULL,
  payload TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'NEW',       -- NEW|DONE|ERR
  corr_id TEXT,                              -- for join
  created_at INTEGER NOT NULL               -- epoch ms
);
CREATE INDEX IF NOT EXISTS ix_commands_status ON commands(status, created_at);

CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  topic TEXT NOT NULL,                       -- e.g. order.new, state.pause
  level TEXT NOT NULL DEFAULT 'INFO',        -- DEBUG|INFO|WARN|ERROR|CRITICAL
  data TEXT NOT NULL,
  corr_id TEXT,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_topic ON events(topic, created_at);
CREATE INDEX IF NOT EXISTS ix_events_level ON events(level, created_at);

CREATE TABLE IF NOT EXISTS approvals(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  command_id INTEGER NOT NULL,
  token TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'PENDING',   -- PENDING|OK|REJECT|EXPIRED
  expires_at INTEGER NOT NULL,              -- epoch ms
  created_at INTEGER NOT NULL,
  FOREIGN KEY(command_id) REFERENCES commands(id)
);
CREATE INDEX IF NOT EXISTS ix_approvals_status ON approvals(status, expires_at);

CREATE TABLE IF NOT EXISTS locks(
  name TEXT PRIMARY KEY,
  owner TEXT NOT NULL,
  acquired_at INTEGER NOT NULL,
  ttl_sec INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS kv(
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL
);
