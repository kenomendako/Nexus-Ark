ALTER TABLE message_requests ADD COLUMN provider_terminal_code TEXT;
ALTER TABLE message_requests ADD COLUMN provider_stream_record_count INTEGER;
ALTER TABLE message_requests ADD COLUMN provider_text_chars INTEGER;
ALTER TABLE message_requests ADD COLUMN provider_usage_status TEXT;
ALTER TABLE message_requests ADD COLUMN provider_unknown_event_count INTEGER;
