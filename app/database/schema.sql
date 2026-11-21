-- Momentrix AI — database schema (generated from app/models/models.py, PostgreSQL dialect)
-- Generated in Prompt 06 (adds ranking_results.score_breakdown JSON);
-- updated in Prompt 12 (adds cost_events + job_id indexes on
-- storyboards / narrations / edit_instructions). Apply with: psql "$DATABASE_URL" -f app/database/schema.sql
-- For local testing, the app creates the same tables in SQLite via SQLAlchemy create_all().
-- Status vocabularies are enforced by CHECK constraints below.


CREATE TABLE jobs (
	id VARCHAR(36) NOT NULL, 
	topic VARCHAR(200) NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	current_stage VARCHAR(32) NOT NULL, 
	requested_by VARCHAR(120) NOT NULL, 
	error_message TEXT, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_jobs_status CHECK (status IN ('created','running','awaiting_approval','approved','failed','cancelled'))
)




CREATE TABLE sources (
	id VARCHAR(36) NOT NULL, 
	platform VARCHAR(64) NOT NULL, 
	external_id VARCHAR(255) NOT NULL, 
	title VARCHAR(500), 
	url TEXT, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_sources_platform_external UNIQUE (platform, external_id)
)




CREATE TABLE agent_execution_logs (
	id VARCHAR(36) NOT NULL, 
	job_id VARCHAR(36) NOT NULL, 
	agent_name VARCHAR(64) NOT NULL, 
	task_id VARCHAR(36), 
	status VARCHAR(32) NOT NULL, 
	detail TEXT, 
	duration_ms INTEGER, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_agent_log_status CHECK (status IN ('started','succeeded','failed','skipped','repair_round')), 
	FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE CASCADE
)




CREATE TABLE candidate_videos (
	id VARCHAR(36) NOT NULL, 
	job_id VARCHAR(36) NOT NULL, 
	source_id VARCHAR(36) NOT NULL, 
	external_video_id VARCHAR(255) NOT NULL, 
	title VARCHAR(500), 
	url TEXT, 
	duration_seconds FLOAT, 
	view_count INTEGER, 
	published_at TIMESTAMP WITH TIME ZONE, 
	status VARCHAR(32) NOT NULL, 
	relevance_score FLOAT, 
	creator VARCHAR(500), 
	description TEXT, 
	licence VARCHAR(255), 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_candidates_status CHECK (status IN ('discovered','rights_pending','rights_approved','rights_rejected','analyzed','selected','rejected')), 
	FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE CASCADE, 
	FOREIGN KEY(source_id) REFERENCES sources (id)
)




CREATE TABLE storyboards (
	id VARCHAR(36) NOT NULL, 
	job_id VARCHAR(36) NOT NULL, 
	title VARCHAR(500), 
	hook TEXT, 
	narration_script TEXT, 
	scenes JSON, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE CASCADE
)




CREATE TABLE analyzed_moments (
	id VARCHAR(36) NOT NULL, 
	job_id VARCHAR(36) NOT NULL, 
	candidate_video_id VARCHAR(36) NOT NULL, 
	start_seconds FLOAT NOT NULL, 
	end_seconds FLOAT NOT NULL, 
	description TEXT, 
	score FLOAT, 
	labels JSON, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE CASCADE, 
	FOREIGN KEY(candidate_video_id) REFERENCES candidate_videos (id) ON DELETE CASCADE
)




CREATE TABLE edit_instructions (
	id VARCHAR(36) NOT NULL, 
	job_id VARCHAR(36) NOT NULL, 
	storyboard_id VARCHAR(36), 
	edl JSON, 
	output_width INTEGER NOT NULL, 
	output_height INTEGER NOT NULL, 
	fps INTEGER NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE CASCADE, 
	FOREIGN KEY(storyboard_id) REFERENCES storyboards (id)
)




CREATE TABLE narrations (
	id VARCHAR(36) NOT NULL, 
	job_id VARCHAR(36) NOT NULL, 
	storyboard_id VARCHAR(36), 
	script_text TEXT NOT NULL, 
	audio_path TEXT, 
	duration_seconds FLOAT, 
	status VARCHAR(32) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_narration_status CHECK (status IN ('draft','generated','approved')), 
	FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE CASCADE, 
	FOREIGN KEY(storyboard_id) REFERENCES storyboards (id)
)




CREATE TABLE rights_evidence (
	id VARCHAR(36) NOT NULL, 
	candidate_video_id VARCHAR(36) NOT NULL, 
	licence VARCHAR(255), 
	verdict VARCHAR(32) NOT NULL, 
	evidence_url TEXT, 
	notes TEXT, 
	checked_by VARCHAR(64) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	license_name VARCHAR(255), 
	license_url TEXT, 
	permission_evidence TEXT, 
	verification_date TIMESTAMP WITH TIME ZONE, 
	commercial_use BOOLEAN, 
	modification_allowed BOOLEAN, 
	redistribution_allowed BOOLEAN, 
	attribution_required BOOLEAN, 
	attribution_text TEXT, 
	geographic_restrictions TEXT, 
	time_restrictions TEXT, 
	expires_at TIMESTAMP WITH TIME ZONE, 
	verification_method VARCHAR(64), 
	reviewer_status VARCHAR(32), 
	reviewed_by VARCHAR(120), 
	reviewed_at TIMESTAMP WITH TIME ZONE, 
	reasons JSON, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_rights_verdict CHECK (verdict IN ('approved','rejected','needs_review','expired','not_checked')), 
	FOREIGN KEY(candidate_video_id) REFERENCES candidate_videos (id) ON DELETE CASCADE
)




CREATE TABLE approved_footage (
	id VARCHAR(36) NOT NULL, 
	job_id VARCHAR(36) NOT NULL, 
	candidate_video_id VARCHAR(36) NOT NULL, 
	rights_evidence_id VARCHAR(36), 
	storage_path TEXT, 
	duration_seconds FLOAT, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE CASCADE, 
	FOREIGN KEY(candidate_video_id) REFERENCES candidate_videos (id), 
	FOREIGN KEY(rights_evidence_id) REFERENCES rights_evidence (id)
)




CREATE TABLE ranking_results (
	id VARCHAR(36) NOT NULL, 
	job_id VARCHAR(36) NOT NULL, 
	moment_id VARCHAR(36) NOT NULL, 
	candidate_video_id VARCHAR(36) NOT NULL, 
	rank INTEGER NOT NULL, 
	score FLOAT NOT NULL, 
	reason TEXT, 
	score_breakdown JSON, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_ranking_rank CHECK (rank BETWEEN 1 AND 5), 
	CONSTRAINT uq_ranking_job_rank UNIQUE (job_id, rank), 
	CONSTRAINT uq_ranking_job_moment UNIQUE (job_id, moment_id), 
	FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE CASCADE, 
	FOREIGN KEY(moment_id) REFERENCES analyzed_moments (id), 
	FOREIGN KEY(candidate_video_id) REFERENCES candidate_videos (id)
)




CREATE TABLE rendered_videos (
	id VARCHAR(36) NOT NULL, 
	job_id VARCHAR(36) NOT NULL, 
	edit_instruction_id VARCHAR(36), 
	file_path TEXT, 
	duration_seconds FLOAT, 
	width INTEGER, 
	height INTEGER, 
	status VARCHAR(32) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_rendered_status CHECK (status IN ('rendering','ready','failed')), 
	FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE CASCADE, 
	FOREIGN KEY(edit_instruction_id) REFERENCES edit_instructions (id)
)




CREATE TABLE cost_events (
	id VARCHAR(36) NOT NULL, 
	job_id VARCHAR(36) NOT NULL, 
	category VARCHAR(32) NOT NULL, 
	label VARCHAR(500) NOT NULL, 
	quantity FLOAT NOT NULL, 
	unit VARCHAR(32) NOT NULL, 
	unit_cost_usd FLOAT NOT NULL, 
	amount_usd FLOAT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_cost_category CHECK (category IN ('compute','external_api','tts','music_sfx')), 
	FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE CASCADE
)



CREATE INDEX ix_storyboards_job ON storyboards (job_id)
CREATE INDEX ix_narrations_job ON narrations (job_id)
CREATE INDEX ix_edit_instructions_job ON edit_instructions (job_id)
	id VARCHAR(36) NOT NULL, 
	job_id VARCHAR(36) NOT NULL, 
	rendered_video_id VARCHAR(36), 
	verdict VARCHAR(32) NOT NULL, 
	checks JSON, 
	issues JSON, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_qa_verdict CHECK (verdict IN ('pass','fail','needs_review','not_tested')), 
	FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE CASCADE, 
	FOREIGN KEY(rendered_video_id) REFERENCES rendered_videos (id)
)


