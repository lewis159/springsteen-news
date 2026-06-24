-- Aggregator Platform: initial schema migration
-- Run in the Supabase SQL editor

CREATE TABLE IF NOT EXISTS public.agg_feeds (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  slug text UNIQUE NOT NULL,
  name text NOT NULL,
  subdomain text UNIQUE NOT NULL,
  accent_color text NOT NULL DEFAULT '#c0392b',
  logo_url text,
  owner_clerk_id text,
  active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.agg_sources (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  feed_id uuid NOT NULL REFERENCES public.agg_feeds(id) ON DELETE CASCADE,
  name text NOT NULL,
  feed_url text NOT NULL,
  active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.agg_articles (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  feed_id uuid NOT NULL REFERENCES public.agg_feeds(id) ON DELETE CASCADE,
  source_id uuid REFERENCES public.agg_sources(id) ON DELETE SET NULL,
  source_name text,
  title text NOT NULL,
  url text UNIQUE NOT NULL,
  summary text,
  thumbnail_url text,
  category text,
  published_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS agg_articles_feed_id_idx ON public.agg_articles(feed_id);
CREATE INDEX IF NOT EXISTS agg_articles_published_at_idx ON public.agg_articles(published_at DESC);
CREATE INDEX IF NOT EXISTS agg_sources_feed_id_idx ON public.agg_sources(feed_id);

-- Seed: migrate existing Bruce Springsteen feed data
INSERT INTO public.agg_feeds (slug, name, subdomain, accent_color, active)
VALUES ('bruce', 'Bruce Springsteen News', 'bruce', '#c0392b', true)
ON CONFLICT (slug) DO NOTHING;
