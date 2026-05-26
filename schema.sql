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
