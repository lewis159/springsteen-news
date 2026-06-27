CREATE TABLE IF NOT EXISTS bsn_sources (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL,
    feed_url text NOT NULL UNIQUE,
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS bsn_articles (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id uuid REFERENCES bsn_sources(id) ON DELETE SET NULL,
    source_name text,
    title text NOT NULL,
    url text UNIQUE NOT NULL,
    summary text,
    thumbnail_url text,
    category text,
    published_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS bsn_articles_published_at_idx ON bsn_articles(published_at DESC);
CREATE INDEX IF NOT EXISTS bsn_articles_category_idx ON bsn_articles(category);
CREATE INDEX IF NOT EXISTS bsn_articles_source_name_idx ON bsn_articles(source_name);
