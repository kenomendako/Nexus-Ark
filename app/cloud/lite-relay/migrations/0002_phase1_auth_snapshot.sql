PRAGMA foreign_keys = ON;

ALTER TABLE travel_sessions ADD COLUMN persona_id TEXT;
ALTER TABLE travel_sessions ADD COLUMN model_id TEXT;
ALTER TABLE travel_sessions ADD COLUMN snapshot_hash TEXT;

CREATE TABLE pairing_codes (
  pairing_code_id TEXT PRIMARY KEY,
  code_hash TEXT NOT NULL UNIQUE,
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  consumed_at TEXT,
  consumed_by_device_id TEXT
);

CREATE INDEX idx_pairing_codes_expiry ON pairing_codes(expires_at, consumed_at);

CREATE TABLE travel_devices (
  device_id TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  access_token_hash TEXT NOT NULL UNIQUE,
  access_expires_at TEXT NOT NULL,
  refresh_token_hash TEXT NOT NULL UNIQUE,
  refresh_expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  last_refreshed_at TEXT,
  revoked_at TEXT
);

CREATE INDEX idx_travel_devices_access ON travel_devices(access_token_hash, access_expires_at, revoked_at);
CREATE INDEX idx_travel_devices_refresh ON travel_devices(refresh_token_hash, refresh_expires_at, revoked_at);

-- 1セッションで課金APIへ到達できる要求は常に1件だけにする。
-- 複数タブから別client_message_idが同時送信されても、2件目はprovider呼出し前に拒否される。
CREATE UNIQUE INDEX idx_message_requests_single_active_session
  ON message_requests(travel_session_id)
  WHERE status IN ('reserved', 'provider_started');

CREATE TABLE persona_snapshots (
  travel_session_id TEXT PRIMARY KEY REFERENCES travel_sessions(travel_session_id),
  persona_id TEXT NOT NULL,
  schema_version INTEGER NOT NULL CHECK (schema_version = 1),
  snapshot_json TEXT NOT NULL,
  snapshot_hash TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE imported_bundle_exports (
  export_id TEXT PRIMARY KEY,
  travel_session_id TEXT NOT NULL REFERENCES travel_sessions(travel_session_id),
  exported_at TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  UNIQUE (travel_session_id, payload_hash)
);
