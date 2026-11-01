PRAGMA foreign_keys = OFF;

CREATE TABLE travel_personas (
  travel_session_id TEXT NOT NULL REFERENCES travel_sessions(travel_session_id),
  persona_id TEXT NOT NULL,
  display_name TEXT NOT NULL,
  presence_mode TEXT NOT NULL CHECK (presence_mode IN ('exclusive', 'parallel')),
  status TEXT NOT NULL CHECK (status IN ('armed', 'active', 'returning', 'closed', 'emergency_reclaimed')),
  snapshot_schema_version INTEGER NOT NULL CHECK (snapshot_schema_version BETWEEN 1 AND 4),
  snapshot_hash TEXT NOT NULL,
  home_anchor_hash TEXT,
  branch_divergence_possible INTEGER NOT NULL DEFAULT 0 CHECK (branch_divergence_possible IN (0, 1)),
  budget_daily_limit_usd REAL,
  budget_session_limit_usd REAL,
  budget_max_output_tokens INTEGER NOT NULL DEFAULT 2048,
  cache_policy TEXT NOT NULL DEFAULT 'auto' CHECK (cache_policy IN ('off', 'auto', 'gemini_explicit')),
  last_ack_sequence INTEGER NOT NULL DEFAULT 0 CHECK (last_ack_sequence >= 0),
  last_ack_payload_hash TEXT,
  acknowledged_at TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY (travel_session_id, persona_id)
);

INSERT INTO travel_personas
  (travel_session_id, persona_id, display_name, presence_mode, status, snapshot_schema_version,
   snapshot_hash, budget_daily_limit_usd, budget_session_limit_usd, budget_max_output_tokens,
   cache_policy, created_at)
SELECT s.travel_session_id, s.persona_id, s.persona_id, 'exclusive', s.status,
       s.snapshot_schema_version, s.snapshot_hash, s.budget_daily_limit_usd,
       s.budget_session_limit_usd, s.budget_max_output_tokens, s.cache_policy, s.created_at
FROM travel_sessions s
WHERE s.persona_id IS NOT NULL
ON CONFLICT(travel_session_id, persona_id) DO NOTHING;

CREATE TABLE persona_snapshots_phase4 (
  travel_session_id TEXT NOT NULL REFERENCES travel_sessions(travel_session_id),
  persona_id TEXT NOT NULL,
  schema_version INTEGER NOT NULL CHECK (schema_version BETWEEN 1 AND 4),
  snapshot_json TEXT NOT NULL,
  snapshot_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (travel_session_id, persona_id)
);

INSERT INTO persona_snapshots_phase4
  (travel_session_id, persona_id, schema_version, snapshot_json, snapshot_hash, created_at)
SELECT travel_session_id, persona_id, schema_version, snapshot_json, snapshot_hash, created_at
FROM persona_snapshots;

DROP TABLE persona_snapshots;
ALTER TABLE persona_snapshots_phase4 RENAME TO persona_snapshots;

ALTER TABLE travel_events ADD COLUMN branch_id TEXT NOT NULL DEFAULT 'travel'
  CHECK (branch_id IN ('travel'));

CREATE TABLE return_ack_requests (
  ack_id TEXT PRIMARY KEY,
  travel_session_id TEXT NOT NULL REFERENCES travel_sessions(travel_session_id),
  persona_id TEXT NOT NULL,
  through_sequence INTEGER NOT NULL CHECK (through_sequence >= 0),
  payload_hash TEXT NOT NULL,
  acknowledged_at TEXT NOT NULL,
  UNIQUE (travel_session_id, persona_id, through_sequence, payload_hash),
  FOREIGN KEY (travel_session_id, persona_id)
    REFERENCES travel_personas(travel_session_id, persona_id)
);

CREATE INDEX idx_travel_personas_status
  ON travel_personas(travel_session_id, status, persona_id);
CREATE INDEX idx_return_ack_session
  ON return_ack_requests(travel_session_id, persona_id, through_sequence);

PRAGMA foreign_keys = ON;
