PRAGMA foreign_keys = ON;

-- snapshot v1/v2を保持したまま、budget/cache設定を持つsnapshot v3を受け入れる。
CREATE TABLE persona_snapshots_phase3 (
  travel_session_id TEXT PRIMARY KEY REFERENCES travel_sessions(travel_session_id),
  persona_id TEXT NOT NULL,
  schema_version INTEGER NOT NULL CHECK (schema_version IN (1, 2, 3)),
  snapshot_json TEXT NOT NULL,
  snapshot_hash TEXT NOT NULL,
  created_at TEXT NOT NULL
);

INSERT INTO persona_snapshots_phase3
  (travel_session_id, persona_id, schema_version, snapshot_json, snapshot_hash, created_at)
SELECT travel_session_id, persona_id, schema_version, snapshot_json, snapshot_hash, created_at
FROM persona_snapshots;

DROP TABLE persona_snapshots;
ALTER TABLE persona_snapshots_phase3 RENAME TO persona_snapshots;

ALTER TABLE travel_sessions ADD COLUMN budget_daily_limit_usd REAL;
ALTER TABLE travel_sessions ADD COLUMN budget_session_limit_usd REAL;
ALTER TABLE travel_sessions ADD COLUMN budget_warning_ratio REAL NOT NULL DEFAULT 0.8;
ALTER TABLE travel_sessions ADD COLUMN budget_allow_unknown_price INTEGER NOT NULL DEFAULT 0;
ALTER TABLE travel_sessions ADD COLUMN budget_max_output_tokens INTEGER NOT NULL DEFAULT 2048;
ALTER TABLE travel_sessions ADD COLUMN budget_timezone TEXT NOT NULL DEFAULT 'UTC';
ALTER TABLE travel_sessions ADD COLUMN cache_policy TEXT NOT NULL DEFAULT 'auto';

ALTER TABLE message_requests ADD COLUMN budget_reserved_usd REAL;
ALTER TABLE message_requests ADD COLUMN budget_settled_usd REAL;
ALTER TABLE message_requests ADD COLUMN budget_state TEXT;
ALTER TABLE message_requests ADD COLUMN budget_settled_at TEXT;

ALTER TABLE usage_receipts ADD COLUMN pricing_version TEXT;
ALTER TABLE usage_receipts ADD COLUMN cost_basis TEXT;
ALTER TABLE usage_receipts ADD COLUMN estimate_status TEXT;
ALTER TABLE usage_receipts ADD COLUMN input_cost_usd REAL;
ALTER TABLE usage_receipts ADD COLUMN output_cost_usd REAL;
ALTER TABLE usage_receipts ADD COLUMN cache_read_cost_usd REAL;
ALTER TABLE usage_receipts ADD COLUMN cache_creation_cost_usd REAL;
ALTER TABLE usage_receipts ADD COLUMN cache_storage_cost_usd REAL;
ALTER TABLE usage_receipts ADD COLUMN estimated_cost_usd REAL;
ALTER TABLE usage_receipts ADD COLUMN estimated_savings_usd REAL;
ALTER TABLE usage_receipts ADD COLUMN unknown_reason TEXT;
ALTER TABLE usage_receipts ADD COLUMN cache_status TEXT;
ALTER TABLE usage_receipts ADD COLUMN cache_ttl_seconds INTEGER;
ALTER TABLE usage_receipts ADD COLUMN cache_creation_5m_tokens INTEGER;
ALTER TABLE usage_receipts ADD COLUMN cache_creation_1h_tokens INTEGER;

CREATE TABLE provider_cache_entries (
  cache_entry_id TEXT PRIMARY KEY,
  travel_session_id TEXT NOT NULL REFERENCES travel_sessions(travel_session_id),
  persona_id TEXT NOT NULL,
  credential_profile_id TEXT NOT NULL REFERENCES provider_profiles(credential_profile_id),
  provider TEXT NOT NULL,
  model_id TEXT NOT NULL,
  route_epoch INTEGER NOT NULL,
  strategy_version TEXT NOT NULL,
  logical_key TEXT NOT NULL,
  remote_cache_name TEXT,
  cached_tokens INTEGER,
  ttl_seconds INTEGER,
  created_at TEXT NOT NULL,
  expires_at TEXT,
  last_used_at TEXT,
  deleted_at TEXT,
  status TEXT NOT NULL CHECK (status IN ('creating', 'ready', 'unavailable', 'expired', 'deleted')),
  failure_code TEXT,
  UNIQUE (travel_session_id, persona_id, logical_key)
);

CREATE INDEX idx_provider_cache_entries_cleanup
  ON provider_cache_entries(travel_session_id, provider, status, expires_at);
