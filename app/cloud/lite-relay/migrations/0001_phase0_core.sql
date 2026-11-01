PRAGMA foreign_keys = ON;

CREATE TABLE travel_sessions (
  travel_session_id TEXT PRIMARY KEY,
  status TEXT NOT NULL CHECK (status IN ('armed', 'active', 'returning', 'closed', 'emergency_reclaimed')),
  retention_days INTEGER NOT NULL CHECK (retention_days IN (0, 7, 30)),
  created_at TEXT NOT NULL,
  acknowledged_at TEXT,
  content_delete_after TEXT,
  content_deleted_at TEXT
);

CREATE TABLE message_requests (
  client_message_id TEXT PRIMARY KEY,
  travel_session_id TEXT NOT NULL REFERENCES travel_sessions(travel_session_id),
  persona_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN (
    'reserved', 'provider_started', 'completed', 'partial', 'failed_known', 'outcome_unknown'
  )),
  provider TEXT NOT NULL,
  model_requested TEXT NOT NULL,
  event_id TEXT,
  receipt_id TEXT,
  reserved_at TEXT NOT NULL,
  provider_started_at TEXT,
  finalized_at TEXT
);

CREATE INDEX idx_message_requests_session
  ON message_requests(travel_session_id, persona_id, reserved_at);

CREATE TABLE travel_events (
  event_id TEXT PRIMARY KEY,
  travel_session_id TEXT NOT NULL REFERENCES travel_sessions(travel_session_id),
  persona_id TEXT NOT NULL,
  sequence_no INTEGER NOT NULL CHECK (sequence_no > 0),
  type TEXT NOT NULL,
  created_at TEXT NOT NULL,
  content TEXT,
  content_hash TEXT NOT NULL,
  provider TEXT,
  model_requested TEXT,
  model_resolved TEXT,
  route_epoch INTEGER NOT NULL DEFAULT 0,
  reply_to_event_id TEXT,
  status TEXT NOT NULL CHECK (status IN ('committed', 'partial')),
  content_deleted_at TEXT,
  UNIQUE (travel_session_id, persona_id, sequence_no)
);

CREATE INDEX idx_travel_events_cursor
  ON travel_events(travel_session_id, persona_id, sequence_no, event_id);

CREATE TABLE usage_receipts (
  receipt_id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL UNIQUE REFERENCES travel_events(event_id),
  travel_session_id TEXT NOT NULL REFERENCES travel_sessions(travel_session_id),
  persona_id TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  provider TEXT NOT NULL,
  gateway TEXT,
  credential_profile_id TEXT NOT NULL,
  model_requested TEXT NOT NULL,
  model_resolved TEXT,
  upstream_provider TEXT,
  input_tokens INTEGER,
  output_tokens INTEGER,
  cache_read_tokens INTEGER,
  cache_creation_tokens INTEGER,
  provider_reported_cost_usd REAL,
  usage_status TEXT NOT NULL CHECK (usage_status IN ('reported', 'missing', 'partial')),
  signature TEXT NOT NULL
);

CREATE INDEX idx_usage_receipts_session
  ON usage_receipts(travel_session_id, persona_id, occurred_at);

CREATE TABLE provider_profiles (
  credential_profile_id TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  provider TEXT NOT NULL,
  secret_binding_id TEXT NOT NULL,
  allowed_base_url_id TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
  created_at TEXT NOT NULL
);

CREATE TABLE audit_events (
  audit_event_id TEXT PRIMARY KEY,
  event_type TEXT NOT NULL,
  travel_session_id TEXT,
  persona_id TEXT,
  occurred_at TEXT NOT NULL,
  outcome TEXT NOT NULL,
  detail_code TEXT
);
