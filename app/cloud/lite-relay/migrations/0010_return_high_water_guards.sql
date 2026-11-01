PRAGMA foreign_keys = ON;

-- 帰宅開始時点の確定済みevent上限を固定し、manifest/chunk/ackで同じ値を使う。
ALTER TABLE travel_personas ADD COLUMN return_high_water_sequence INTEGER
  CHECK (return_high_water_sequence IS NULL OR return_high_water_sequence >= 0);

-- 帰宅・緊急帰還・close後に、先行していたprovider応答が遅れて確定することを防ぐ。
-- session/personaの状態変更とevent INSERTはD1上で直列化されるため、先に成立した側だけが有効になる。
CREATE TRIGGER prevent_travel_event_insert_outside_active
BEFORE INSERT ON travel_events
WHEN NOT EXISTS (
  SELECT 1
  FROM travel_sessions s
  LEFT JOIN travel_personas tp
    ON tp.travel_session_id = s.travel_session_id AND tp.persona_id = NEW.persona_id
  WHERE s.travel_session_id = NEW.travel_session_id
    AND s.status = 'active'
    AND (s.snapshot_schema_version < 4 OR tp.status = 'active')
)
BEGIN
  SELECT RAISE(ABORT, 'travel_session_not_active');
END;

UPDATE relay_schema_state
SET d1_schema_version = 10,
    latest_migration = '0010_return_high_water_guards.sql',
    applied_at = CURRENT_TIMESTAMP
WHERE singleton_id = 1;
