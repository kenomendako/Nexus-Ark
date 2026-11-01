PRAGMA foreign_keys = ON;

-- Phase 1のsnapshot v1を保持したまま、初期routeを持つsnapshot v2を受け入れる。
CREATE TABLE persona_snapshots_phase2 (
  travel_session_id TEXT PRIMARY KEY REFERENCES travel_sessions(travel_session_id),
  persona_id TEXT NOT NULL,
  schema_version INTEGER NOT NULL CHECK (schema_version IN (1, 2)),
  snapshot_json TEXT NOT NULL,
  snapshot_hash TEXT NOT NULL,
  created_at TEXT NOT NULL
);

INSERT INTO persona_snapshots_phase2
  (travel_session_id, persona_id, schema_version, snapshot_json, snapshot_hash, created_at)
SELECT travel_session_id, persona_id, schema_version, snapshot_json, snapshot_hash, created_at
FROM persona_snapshots;

DROP TABLE persona_snapshots;
ALTER TABLE persona_snapshots_phase2 RENAME TO persona_snapshots;

ALTER TABLE travel_sessions ADD COLUMN snapshot_schema_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE travel_sessions ADD COLUMN credential_profile_id TEXT;
ALTER TABLE travel_sessions ADD COLUMN route_epoch INTEGER NOT NULL DEFAULT 0;

ALTER TABLE message_requests ADD COLUMN credential_profile_id TEXT;
ALTER TABLE message_requests ADD COLUMN route_epoch INTEGER NOT NULL DEFAULT 0;

INSERT INTO provider_profiles
  (credential_profile_id, display_name, provider, secret_binding_id, allowed_base_url_id, enabled, created_at)
VALUES
  ('gemini-personal-1', 'Gemini（個人用1）', 'gemini', 'GEMINI_PERSONAL_1', 'gemini-official', 1,
   '2026-07-16T00:00:00.000Z')
ON CONFLICT(credential_profile_id) DO NOTHING;

CREATE UNIQUE INDEX idx_provider_profiles_secret_binding
  ON provider_profiles(secret_binding_id);

CREATE TABLE persona_routes (
  travel_session_id TEXT NOT NULL REFERENCES travel_sessions(travel_session_id),
  persona_id TEXT NOT NULL,
  credential_profile_id TEXT NOT NULL REFERENCES provider_profiles(credential_profile_id),
  provider TEXT NOT NULL CHECK (provider IN ('gemini', 'openai', 'anthropic', 'xai', 'openrouter')),
  model_id TEXT NOT NULL,
  route_epoch INTEGER NOT NULL CHECK (route_epoch >= 0),
  changed_at TEXT NOT NULL,
  PRIMARY KEY (travel_session_id, persona_id)
);

CREATE INDEX idx_persona_routes_profile
  ON persona_routes(credential_profile_id, travel_session_id);

CREATE TABLE route_change_requests (
  route_change_id TEXT PRIMARY KEY,
  travel_session_id TEXT NOT NULL REFERENCES travel_sessions(travel_session_id),
  persona_id TEXT NOT NULL,
  credential_profile_id TEXT NOT NULL REFERENCES provider_profiles(credential_profile_id),
  provider TEXT NOT NULL,
  model_id TEXT NOT NULL,
  route_epoch INTEGER NOT NULL CHECK (route_epoch >= 0),
  event_id TEXT,
  changed_at TEXT NOT NULL
);

CREATE INDEX idx_route_change_requests_session
  ON route_change_requests(travel_session_id, persona_id, route_epoch);

-- 既存Phase 1セッションへGemini routeを補完する。既存snapshot／会話／receiptは変更しない。
INSERT INTO persona_routes
  (travel_session_id, persona_id, credential_profile_id, provider, model_id, route_epoch, changed_at)
SELECT travel_session_id, persona_id, 'gemini-personal-1', 'gemini', model_id, 0, created_at
FROM travel_sessions
WHERE persona_id IS NOT NULL AND model_id IS NOT NULL
ON CONFLICT(travel_session_id, persona_id) DO NOTHING;

UPDATE travel_sessions
SET credential_profile_id = 'gemini-personal-1', route_epoch = 0
WHERE persona_id IS NOT NULL AND model_id IS NOT NULL AND credential_profile_id IS NULL;
