CREATE TABLE IF NOT EXISTS media_items (
  id BIGSERIAL PRIMARY KEY,
  source TEXT NOT NULL,
  source_id INTEGER NOT NULL,
  media_type TEXT NOT NULL,
  title TEXT NOT NULL,
  year INTEGER,
  tmdb_id INTEGER,
  imdb_id TEXT,
  tvdb_id INTEGER,
  path TEXT,
  monitored BOOLEAN,
  raw JSONB NOT NULL,
  updated_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE(source, media_type, source_id)
);

CREATE TABLE IF NOT EXISTS media_files (
  id BIGSERIAL PRIMARY KEY,
  source TEXT NOT NULL,
  source_id INTEGER NOT NULL,
  parent_source_id INTEGER,
  media_type TEXT NOT NULL,
  path TEXT,
  size_bytes BIGINT,
  quality JSONB,
  languages JSONB,
  mediainfo JSONB,
  raw JSONB NOT NULL,
  updated_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE(source, media_type, source_id)
);

CREATE TABLE IF NOT EXISTS sync_state (
  source TEXT PRIMARY KEY,
  last_full_sync TIMESTAMPTZ,
  last_history_sync TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS media_item_tags (
  id BIGSERIAL PRIMARY KEY,
  source TEXT NOT NULL,
  media_type TEXT NOT NULL,
  source_id INTEGER NOT NULL,
  tag_label TEXT NOT NULL,
  tag_reason TEXT NOT NULL,
  arr_tag_id INTEGER,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE(source, media_type, source_id, tag_label)
);

CREATE TABLE IF NOT EXISTS media_file_tags (
  id BIGSERIAL PRIMARY KEY,
  source TEXT NOT NULL,
  media_type TEXT NOT NULL,
  source_id INTEGER NOT NULL,
  parent_source_id INTEGER,
  tag_label TEXT NOT NULL,
  tag_reason TEXT NOT NULL,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE(source, media_type, source_id, tag_label)
);

CREATE TABLE IF NOT EXISTS download_jobs (
  id BIGSERIAL PRIMARY KEY,
  source TEXT NOT NULL,
  media_type TEXT NOT NULL,
  source_id INTEGER NOT NULL,
  title TEXT NOT NULL,
  release_title TEXT NOT NULL,
  release_guid TEXT,
  indexer TEXT,
  indexer_id INTEGER,
  score INTEGER,
  status TEXT NOT NULL,
  staging_path TEXT,
  client TEXT,
  client_queue_id TEXT,
  content_path TEXT,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS rss_waitlist (
  id BIGSERIAL PRIMARY KEY,
  source TEXT NOT NULL,
  media_type TEXT NOT NULL,
  source_id INTEGER NOT NULL,
  title TEXT NOT NULL,
  year INTEGER,
  status TEXT NOT NULL,
  reason TEXT NOT NULL,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE(source, media_type, source_id)
);

CREATE TABLE IF NOT EXISTS download_artifact_scans (
  id BIGSERIAL PRIMARY KEY,
  download_job_id BIGINT REFERENCES download_jobs(id) ON DELETE SET NULL,
  client TEXT,
  client_queue_id TEXT NOT NULL,
  content_path TEXT NOT NULL,
  scan_status TEXT NOT NULL,
  file_count INTEGER NOT NULL DEFAULT 0,
  total_size_bytes BIGINT NOT NULL DEFAULT 0,
  primary_video_path TEXT,
  detected_audio_languages JSONB NOT NULL DEFAULT '{}'::jsonb,
  detected_subtitle_languages JSONB NOT NULL DEFAULT '{}'::jsonb,
  files JSONB NOT NULL DEFAULT '[]'::jsonb,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE(client_queue_id)
);

CREATE TABLE IF NOT EXISTS download_audio_extractions (
  id BIGSERIAL PRIMARY KEY,
  download_job_id BIGINT REFERENCES download_jobs(id) ON DELETE SET NULL,
  client_queue_id TEXT NOT NULL,
  source_video_path TEXT NOT NULL,
  output_root TEXT NOT NULL,
  extraction_status TEXT NOT NULL,
  extracted_tracks JSONB NOT NULL DEFAULT '[]'::jsonb,
  probe_streams JSONB NOT NULL DEFAULT '[]'::jsonb,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE(client_queue_id)
);

CREATE TABLE IF NOT EXISTS download_media_imports (
  id BIGSERIAL PRIMARY KEY,
  download_job_id BIGINT REFERENCES download_jobs(id) ON DELETE CASCADE,
  source TEXT NOT NULL,
  source_id INTEGER NOT NULL,
  client_queue_id TEXT,
  remux_path TEXT NOT NULL,
  import_folder TEXT NOT NULL,
  imported_path TEXT,
  import_status TEXT NOT NULL,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE(download_job_id)
);

CREATE TABLE IF NOT EXISTS library_audio_remux_jobs (
  id BIGSERIAL PRIMARY KEY,
  download_job_id BIGINT REFERENCES download_jobs(id) ON DELETE CASCADE,
  source TEXT NOT NULL,
  source_id INTEGER NOT NULL,
  movie_id INTEGER NOT NULL,
  client_queue_id TEXT,
  library_video_path TEXT NOT NULL,
  temp_output_path TEXT NOT NULL,
  final_library_path TEXT,
  remux_status TEXT NOT NULL,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE(download_job_id)
);
