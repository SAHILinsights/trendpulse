-- TrendPulse migration 002: pipeline_log_records: persisted run-scoped structured logs (generated)
SET search_path TO trendpulse;

CREATE TABLE IF NOT EXISTS pipeline_log_records (
	id SERIAL NOT NULL, 
	run_id VARCHAR(36) NOT NULL, 
	stage VARCHAR(48), 
	level VARCHAR(10) NOT NULL, 
	logger VARCHAR(80) NOT NULL, 
	message TEXT NOT NULL, 
	exc_text TEXT, 
	logged_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS ix_logs_level ON pipeline_log_records (level);
CREATE INDEX IF NOT EXISTS ix_logs_logged_at ON pipeline_log_records (logged_at);
CREATE INDEX IF NOT EXISTS ix_logs_run ON pipeline_log_records (run_id, logged_at);

INSERT INTO schema_migrations (version, description, applied_at) VALUES (2, 'pipeline_log_records: persisted run-scoped structured logs', now()) ON CONFLICT (version) DO NOTHING;
