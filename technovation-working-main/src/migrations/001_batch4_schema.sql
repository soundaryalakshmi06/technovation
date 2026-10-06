-- AthleteGuard Batch 4 PostgreSQL upgrade
-- Generated from checked-in src/models.py and the inspected public schema.
-- Run as the database/object owner (or a dedicated migration administrator), not as injury_app_user.
-- Idempotent DDL only. Existing rows are retained; injury_events.player_id is relaxed to allow unknown identity.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '0';

-- Existing table compatibility: add only columns absent from the inspected schema.
ALTER TABLE public.teams ADD COLUMN IF NOT EXISTS short_name VARCHAR(20);
ALTER TABLE public.teams ADD COLUMN IF NOT EXISTS active BOOLEAN DEFAULT true NOT NULL;
ALTER TABLE public.teams ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE public.teams ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE public.players ADD COLUMN IF NOT EXISTS position VARCHAR(50);
ALTER TABLE public.players ADD COLUMN IF NOT EXISTS active BOOLEAN DEFAULT true NOT NULL;
ALTER TABLE public.players ADD COLUMN IF NOT EXISTS reference_images JSON;
ALTER TABLE public.players ADD COLUMN IF NOT EXISTS face_embedding JSON;
ALTER TABLE public.players ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE public.players ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS full_name VARCHAR(120);
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS email VARCHAR(255);
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS phone VARCHAR(32);
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS active BOOLEAN DEFAULT true NOT NULL;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS last_login_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE public.match_sessions ADD COLUMN IF NOT EXISTS name VARCHAR;
ALTER TABLE public.match_sessions ADD COLUMN IF NOT EXISTS video_source VARCHAR;
ALTER TABLE public.match_sessions ADD COLUMN IF NOT EXISTS status VARCHAR(20) DEFAULT 'CREATED' NOT NULL;
ALTER TABLE public.match_sessions ADD COLUMN IF NOT EXISTS created_by_id INTEGER;
ALTER TABLE public.match_sessions ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE public.match_sessions ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;
ALTER TABLE public.injury_events ADD COLUMN IF NOT EXISTS injury_note VARCHAR;
ALTER TABLE public.injury_events ADD COLUMN IF NOT EXISTS clip_path VARCHAR;
ALTER TABLE public.injury_events ADD COLUMN IF NOT EXISTS track_id INTEGER;
ALTER TABLE public.injury_events ADD COLUMN IF NOT EXISTS video_time_sec FLOAT;
ALTER TABLE public.injury_events ADD COLUMN IF NOT EXISTS identified_by VARCHAR;
ALTER TABLE public.injury_events ADD COLUMN IF NOT EXISTS id_detail VARCHAR;
ALTER TABLE public.injury_events ADD COLUMN IF NOT EXISTS id_confidence FLOAT;
ALTER TABLE public.injury_events ADD COLUMN IF NOT EXISTS identified_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE public.injury_events ADD COLUMN IF NOT EXISTS video_id INTEGER;
ALTER TABLE public.injury_events ADD COLUMN IF NOT EXISTS player_track_id INTEGER;
ALTER TABLE public.injury_events ADD COLUMN IF NOT EXISTS collision_event_id INTEGER;
ALTER TABLE public.injury_events ADD COLUMN IF NOT EXISTS side VARCHAR(10);
ALTER TABLE public.injury_events ADD COLUMN IF NOT EXISTS confidence FLOAT;
ALTER TABLE public.injury_events ADD COLUMN IF NOT EXISTS risk_score FLOAT;
ALTER TABLE public.injury_events ADD COLUMN IF NOT EXISTS frame_number INTEGER;
ALTER TABLE public.injury_events ADD COLUMN IF NOT EXISTS location_x FLOAT;
ALTER TABLE public.injury_events ADD COLUMN IF NOT EXISTS location_y FLOAT;
ALTER TABLE public.injury_events ADD COLUMN IF NOT EXISTS dedupe_key VARCHAR(255);
ALTER TABLE public.injury_events ADD COLUMN IF NOT EXISTS detection_count INTEGER DEFAULT '1' NOT NULL;
ALTER TABLE public.injury_events ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE public.injury_events ADD COLUMN IF NOT EXISTS resolved_by_id INTEGER;
ALTER TABLE public.injury_events ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL;

-- Existing injury rows keep their player_id values; NULL is now allowed for unidentified future events.
ALTER TABLE public.injury_events ALTER COLUMN player_id DROP NOT NULL;
-- Match the ORM defaults for future inserts; existing source/resolved values are untouched.
ALTER TABLE public.injury_events ALTER COLUMN source SET DEFAULT 'ai';
ALTER TABLE public.injury_events ALTER COLUMN resolved SET DEFAULT false;

-- Create only absent model tables, in dependency order. Their model constraints are included.
CREATE TABLE IF NOT EXISTS videos (
	id SERIAL NOT NULL, 
	match_session_id INTEGER NOT NULL, 
	source_type VARCHAR(20) DEFAULT 'file' NOT NULL, 
	original_filename VARCHAR(255), 
	file_path VARCHAR(500), 
	content_type VARCHAR(100), 
	file_size_bytes BIGINT, 
	checksum_sha256 VARCHAR(64), 
	fps FLOAT, 
	width INTEGER, 
	height INTEGER, 
	total_frames INTEGER, 
	duration_sec FLOAT, 
	status VARCHAR(20) DEFAULT 'UPLOADED' NOT NULL, 
	processed_at TIMESTAMP WITH TIME ZONE, 
	error_message TEXT, 
	uploaded_by_id INTEGER, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_videos PRIMARY KEY (id), 
	CONSTRAINT ck_videos_fps_positive CHECK (fps IS NULL OR fps > 0), 
	CONSTRAINT ck_videos_duration_non_negative CHECK (duration_sec IS NULL OR duration_sec >= 0), 
	CONSTRAINT fk_videos_match_session_id_match_sessions FOREIGN KEY(match_session_id) REFERENCES match_sessions (id) ON DELETE CASCADE, 
	CONSTRAINT fk_videos_uploaded_by_id_users FOREIGN KEY(uploaded_by_id) REFERENCES users (id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS collision_events (
	id SERIAL NOT NULL, 
	match_session_id INTEGER, 
	video_id INTEGER, 
	event_type VARCHAR(30) DEFAULT 'COLLISION' NOT NULL, 
	risk VARCHAR, 
	risk_score FLOAT, 
	confidence FLOAT, 
	iou FLOAT, 
	contact_frames INTEGER, 
	frame_number INTEGER, 
	video_time_sec FLOAT, 
	location_x FLOAT, 
	location_y FLOAT, 
	location_bbox JSON, 
	region VARCHAR(50), 
	note VARCHAR, 
	clip_path VARCHAR, 
	source VARCHAR DEFAULT 'ai' NOT NULL, 
	resolved BOOLEAN DEFAULT false NOT NULL, 
	resolved_at TIMESTAMP WITH TIME ZONE, 
	dedupe_key VARCHAR(255), 
	detection_count INTEGER DEFAULT '1' NOT NULL, 
	timestamp TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_collision_events PRIMARY KEY (id), 
	CONSTRAINT ck_collision_events_confidence_range CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)), 
	CONSTRAINT ck_collision_events_location_x_range CHECK (location_x IS NULL OR (location_x >= 0 AND location_x <= 1)), 
	CONSTRAINT ck_collision_events_location_y_range CHECK (location_y IS NULL OR (location_y >= 0 AND location_y <= 1)), 
	CONSTRAINT ck_collision_events_detection_count_positive CHECK (detection_count >= 1), 
	CONSTRAINT fk_collision_events_match_session_id_match_sessions FOREIGN KEY(match_session_id) REFERENCES match_sessions (id) ON DELETE CASCADE, 
	CONSTRAINT fk_collision_events_video_id_videos FOREIGN KEY(video_id) REFERENCES videos (id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS player_tracks (
	id SERIAL NOT NULL, 
	match_session_id INTEGER NOT NULL, 
	video_id INTEGER, 
	track_id INTEGER NOT NULL, 
	player_id INTEGER, 
	team_id INTEGER, 
	identity_status VARCHAR(20) DEFAULT 'UNKNOWN' NOT NULL, 
	identified_by VARCHAR, 
	id_detail VARCHAR, 
	id_confidence FLOAT, 
	identified_at TIMESTAMP WITH TIME ZONE, 
	predecessor_id INTEGER, 
	first_frame INTEGER, 
	last_frame INTEGER, 
	first_seen_sec FLOAT, 
	last_seen_sec FLOAT, 
	tracking_confidence FLOAT, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_player_tracks PRIMARY KEY (id), 
	CONSTRAINT ck_player_tracks_id_confidence_range CHECK (id_confidence IS NULL OR (id_confidence >= 0 AND id_confidence <= 1)), 
	CONSTRAINT ck_player_tracks_tracking_confidence_range CHECK (tracking_confidence IS NULL OR (tracking_confidence >= 0 AND tracking_confidence <= 1)), 
	CONSTRAINT ck_player_tracks_identity_status_valid CHECK (identity_status IN ('CONFIRMED', 'UNCERTAIN', 'UNKNOWN')), 
	CONSTRAINT fk_player_tracks_match_session_id_match_sessions FOREIGN KEY(match_session_id) REFERENCES match_sessions (id) ON DELETE CASCADE, 
	CONSTRAINT fk_player_tracks_video_id_videos FOREIGN KEY(video_id) REFERENCES videos (id) ON DELETE SET NULL, 
	CONSTRAINT fk_player_tracks_player_id_players FOREIGN KEY(player_id) REFERENCES players (id) ON DELETE SET NULL, 
	CONSTRAINT fk_player_tracks_team_id_teams FOREIGN KEY(team_id) REFERENCES teams (id) ON DELETE SET NULL, 
	CONSTRAINT fk_player_tracks_predecessor_id_player_tracks FOREIGN KEY(predecessor_id) REFERENCES player_tracks (id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS collision_participants (
	id SERIAL NOT NULL, 
	collision_event_id INTEGER NOT NULL, 
	slot SMALLINT NOT NULL, 
	track_id INTEGER NOT NULL, 
	player_id INTEGER, 
	player_track_id INTEGER, 
	identified_by VARCHAR, 
	id_detail VARCHAR, 
	id_confidence FLOAT, 
	identified_at TIMESTAMP WITH TIME ZONE, 
	region VARCHAR(50), 
	movement FLOAT, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_collision_participants PRIMARY KEY (id), 
	CONSTRAINT ck_collision_participants_slot_valid CHECK (slot IN (1, 2)), 
	CONSTRAINT ck_collision_participants_id_confidence_range CHECK (id_confidence IS NULL OR (id_confidence >= 0 AND id_confidence <= 1)), 
	CONSTRAINT fk_collision_participants_collision_event_id_collision_events FOREIGN KEY(collision_event_id) REFERENCES collision_events (id) ON DELETE CASCADE, 
	CONSTRAINT fk_collision_participants_player_id_players FOREIGN KEY(player_id) REFERENCES players (id) ON DELETE SET NULL, 
	CONSTRAINT fk_collision_participants_player_track_id_player_tracks FOREIGN KEY(player_track_id) REFERENCES player_tracks (id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS alerts (
	id SERIAL NOT NULL, 
	injury_event_id INTEGER, 
	collision_event_id INTEGER, 
	recipient_user_id INTEGER, 
	team_id INTEGER, 
	alert_type VARCHAR(30) DEFAULT 'NEW_EVENT' NOT NULL, 
	severity VARCHAR(10), 
	channel VARCHAR(20) DEFAULT 'web' NOT NULL, 
	title VARCHAR(200), 
	message TEXT NOT NULL, 
	status VARCHAR(20) DEFAULT 'PENDING' NOT NULL, 
	sent_at TIMESTAMP WITH TIME ZONE, 
	error_message TEXT, 
	is_read BOOLEAN DEFAULT false NOT NULL, 
	read_at TIMESTAMP WITH TIME ZONE, 
	dedupe_key VARCHAR(255), 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_alerts PRIMARY KEY (id), 
	CONSTRAINT ck_alerts_has_event CHECK (injury_event_id IS NOT NULL OR collision_event_id IS NOT NULL), 
	CONSTRAINT fk_alerts_injury_event_id_injury_events FOREIGN KEY(injury_event_id) REFERENCES injury_events (id) ON DELETE CASCADE, 
	CONSTRAINT fk_alerts_collision_event_id_collision_events FOREIGN KEY(collision_event_id) REFERENCES collision_events (id) ON DELETE CASCADE, 
	CONSTRAINT fk_alerts_recipient_user_id_users FOREIGN KEY(recipient_user_id) REFERENCES users (id) ON DELETE SET NULL, 
	CONSTRAINT fk_alerts_team_id_teams FOREIGN KEY(team_id) REFERENCES teams (id) ON DELETE SET NULL, 
	CONSTRAINT uq_alerts_dedupe_key UNIQUE (dedupe_key)
);

-- Add foreign keys for newly added injury_events references after target tables exist.
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'public.injury_events'::regclass AND conname = 'fk_injury_events_collision_event_id_collision_events') THEN
    ALTER TABLE public.injury_events ADD CONSTRAINT fk_injury_events_collision_event_id_collision_events FOREIGN KEY (collision_event_id) REFERENCES public.collision_events (id) ON DELETE SET NULL;
  END IF;
END $$;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'public.injury_events'::regclass AND conname = 'fk_injury_events_video_id_videos') THEN
    ALTER TABLE public.injury_events ADD CONSTRAINT fk_injury_events_video_id_videos FOREIGN KEY (video_id) REFERENCES public.videos (id) ON DELETE SET NULL;
  END IF;
END $$;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'public.injury_events'::regclass AND conname = 'fk_injury_events_resolved_by_id_users') THEN
    ALTER TABLE public.injury_events ADD CONSTRAINT fk_injury_events_resolved_by_id_users FOREIGN KEY (resolved_by_id) REFERENCES public.users (id) ON DELETE SET NULL;
  END IF;
END $$;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'public.injury_events'::regclass AND conname = 'fk_injury_events_player_track_id_player_tracks') THEN
    ALTER TABLE public.injury_events ADD CONSTRAINT fk_injury_events_player_track_id_player_tracks FOREIGN KEY (player_track_id) REFERENCES public.player_tracks (id) ON DELETE SET NULL;
  END IF;
END $$;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'public.match_sessions'::regclass AND conname = 'fk_match_sessions_created_by_id_users') THEN
    ALTER TABLE public.match_sessions ADD CONSTRAINT fk_match_sessions_created_by_id_users FOREIGN KEY (created_by_id) REFERENCES public.users (id) ON DELETE SET NULL;
  END IF;
END $$;

-- Align users.team_id delete behavior with checked-in ORM; existing rows are revalidated and retained.
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='public.users'::regclass AND conname='users_team_id_fkey') THEN
    ALTER TABLE public.users DROP CONSTRAINT users_team_id_fkey;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='public.users'::regclass AND conname='fk_users_team_id_teams') THEN
    ALTER TABLE public.users ADD CONSTRAINT fk_users_team_id_teams FOREIGN KEY (team_id) REFERENCES public.teams (id) ON DELETE SET NULL;
  END IF;
END $$;

-- Add model indexes not already present. Existing indexes are left untouched.
CREATE INDEX IF NOT EXISTS ix_players_team_jersey ON players (team_id, jersey_number);
CREATE UNIQUE INDEX IF NOT EXISTS ix_users_email ON users (email);
CREATE INDEX IF NOT EXISTS ix_videos_match ON videos (match_session_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_videos_match_checksum ON videos (match_session_id, checksum_sha256);
CREATE INDEX IF NOT EXISTS ix_collision_events_match_time ON collision_events (match_session_id, video_time_sec);
CREATE INDEX IF NOT EXISTS ix_collision_events_timestamp ON collision_events (timestamp);
CREATE UNIQUE INDEX IF NOT EXISTS uq_collision_match_dedupe ON collision_events (match_session_id, dedupe_key);
CREATE INDEX IF NOT EXISTS ix_player_tracks_match_player ON player_tracks (match_session_id, player_id);
CREATE INDEX IF NOT EXISTS ix_player_tracks_player ON player_tracks (player_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_player_tracks_match_track ON player_tracks (match_session_id, track_id);
CREATE INDEX IF NOT EXISTS ix_collision_participants_player ON collision_participants (player_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_collision_participants_player ON collision_participants (collision_event_id, player_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_collision_participants_slot ON collision_participants (collision_event_id, slot);
CREATE UNIQUE INDEX IF NOT EXISTS uq_collision_participants_track ON collision_participants (collision_event_id, track_id);
CREATE INDEX IF NOT EXISTS ix_injury_events_collision ON injury_events (collision_event_id);
CREATE INDEX IF NOT EXISTS ix_injury_events_match_time ON injury_events (match_session_id, video_time_sec);
CREATE INDEX IF NOT EXISTS ix_injury_events_match_track ON injury_events (match_session_id, track_id);
CREATE INDEX IF NOT EXISTS ix_injury_events_player ON injury_events (player_id);
CREATE INDEX IF NOT EXISTS ix_injury_events_timestamp ON injury_events (timestamp);
CREATE UNIQUE INDEX IF NOT EXISTS uq_injury_match_dedupe ON injury_events (match_session_id, dedupe_key);
CREATE INDEX IF NOT EXISTS ix_alerts_collision_event ON alerts (collision_event_id);
CREATE INDEX IF NOT EXISTS ix_alerts_created_at ON alerts (created_at);
CREATE INDEX IF NOT EXISTS ix_alerts_injury_event ON alerts (injury_event_id);
CREATE INDEX IF NOT EXISTS ix_alerts_recipient_unread ON alerts (recipient_user_id, is_read);
CREATE INDEX IF NOT EXISTS ix_alerts_team ON alerts (team_id);

-- Least-privilege runtime access; no CREATE, ownership, role membership, or SUPERUSER grant.
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.alerts, public.collision_events, public.collision_participants, public.injury_events, public.match_sessions, public.player_tracks, public.players, public.teams, public.users, public.videos TO injury_app_user;
GRANT USAGE, SELECT, UPDATE ON SEQUENCE public.alerts_id_seq TO injury_app_user;
GRANT USAGE, SELECT, UPDATE ON SEQUENCE public.collision_events_id_seq TO injury_app_user;
GRANT USAGE, SELECT, UPDATE ON SEQUENCE public.collision_participants_id_seq TO injury_app_user;
GRANT USAGE, SELECT, UPDATE ON SEQUENCE public.injury_events_id_seq TO injury_app_user;
GRANT USAGE, SELECT, UPDATE ON SEQUENCE public.match_sessions_id_seq TO injury_app_user;
GRANT USAGE, SELECT, UPDATE ON SEQUENCE public.player_tracks_id_seq TO injury_app_user;
GRANT USAGE, SELECT, UPDATE ON SEQUENCE public.players_id_seq TO injury_app_user;
GRANT USAGE, SELECT, UPDATE ON SEQUENCE public.teams_id_seq TO injury_app_user;
GRANT USAGE, SELECT, UPDATE ON SEQUENCE public.users_id_seq TO injury_app_user;
GRANT USAGE, SELECT, UPDATE ON SEQUENCE public.videos_id_seq TO injury_app_user;

COMMIT;
