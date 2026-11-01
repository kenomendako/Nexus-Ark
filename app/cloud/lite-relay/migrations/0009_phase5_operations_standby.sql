PRAGMA foreign_keys = ON;

ALTER TABLE travel_devices ADD COLUMN last_used_at TEXT;

ALTER TABLE travel_sessions ADD COLUMN activation_mode TEXT
  CHECK (activation_mode IN ('planned', 'recovery_unconfirmed'));
ALTER TABLE travel_sessions ADD COLUMN branch_divergence_possible INTEGER NOT NULL DEFAULT 0
  CHECK (branch_divergence_possible IN (0, 1));

CREATE TABLE relay_schema_state (
  singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
  d1_schema_version INTEGER NOT NULL,
  latest_migration TEXT NOT NULL,
  applied_at TEXT NOT NULL
);

INSERT INTO relay_schema_state
  (singleton_id, d1_schema_version, latest_migration, applied_at)
VALUES (1, 9, '0009_phase5_operations_standby.sql', CURRENT_TIMESTAMP);

CREATE TABLE standby_snapshots (
  standby_snapshot_id TEXT PRIMARY KEY,
  home_instance_id TEXT NOT NULL,
  generation INTEGER NOT NULL CHECK (generation > 0),
  status TEXT NOT NULL CHECK (status IN ('ready', 'superseded', 'activated', 'expired', 'deleted')),
  snapshot_schema_version INTEGER NOT NULL CHECK (snapshot_schema_version BETWEEN 1 AND 4),
  manifest_json TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  ciphertext TEXT,
  nonce TEXT,
  encryption_key_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  superseded_at TEXT,
  content_delete_after TEXT,
  content_deleted_at TEXT,
  activation_id TEXT UNIQUE,
  activation_mode TEXT CHECK (activation_mode IN ('planned', 'recovery_unconfirmed')),
  activated_session_id TEXT UNIQUE,
  activated_at TEXT,
  UNIQUE (home_instance_id, generation)
);

CREATE UNIQUE INDEX idx_standby_one_ready_home
  ON standby_snapshots(home_instance_id) WHERE status = 'ready';
CREATE INDEX idx_standby_retention
  ON standby_snapshots(status, expires_at, content_delete_after, content_deleted_at);

CREATE TABLE maintenance_runs (
  maintenance_run_id TEXT PRIMARY KEY,
  trigger_kind TEXT NOT NULL CHECK (trigger_kind IN ('cron', 'manual')),
  status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
  started_at TEXT NOT NULL,
  completed_at TEXT,
  deleted_session_count INTEGER NOT NULL DEFAULT 0,
  deleted_standby_count INTEGER NOT NULL DEFAULT 0,
  failure_code TEXT
);

CREATE INDEX idx_maintenance_runs_started ON maintenance_runs(started_at DESC);

CREATE TABLE credential_generations (
  credential_kind TEXT NOT NULL CHECK (credential_kind IN ('owner', 'bundle_signing', 'provider')),
  credential_id TEXT NOT NULL,
  generation INTEGER NOT NULL CHECK (generation > 0),
  status TEXT NOT NULL CHECK (status IN ('pending', 'active', 'retiring', 'retired', 'failed')),
  created_at TEXT NOT NULL,
  verified_at TEXT,
  retired_at TEXT,
  failure_code TEXT,
  PRIMARY KEY (credential_kind, credential_id, generation)
);
