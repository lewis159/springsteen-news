import feedparser
import os
import re
import logging
import requests
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from bs4 import BeautifulSoup
from db import get_client

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

KEYWORDS = re.compile(r'springsteen|e street band|bruce|the boss', re.IGNORECASE)

CATEGORY_RULES = [
    (re.compile(r'tour|concert|show|live|ticket|gig|setlist|date|venue', re.IGNORECASE), 'Tour'),
    (re.compile(r'album|record|studio|release|song|track|single|ep|lp', re.IGNORECASE),  'Music'),
    (re.compile(r'interview|speaks|says|tells|talks|chat|conversation',  re.IGNORECASE),  'Interview'),
    (re.compile(r'review|recap|night \w+|performance',                   re.IGNORECASE),  'Review'),
]

def categorise(text):
    for pattern, label in CATEGORY_RULES:
        if pattern.search(text):
            return label
    return 'News'

def parse_date(entry):
    for field in ('published', 'updated'):
        val = entry.get(field)
        if val:
            try:
                return parsedate_to_datetime(val).astimezone(timezone.utc).isoformat()
            except Exception:
                pass
    return datetime.now(timezone.utc).isoformat()

def get_thumbnail(entry):
    # 1. media_thumbnail / media_content tags
    media = entry.get('media_thumbnail') or entry.get('media_content')
    if media and isinstance(media, list) and media[0].get('url'):
        return media[0]['url']
    # 2. enclosures
    for enc in entry.get('enclosures', []):
        if enc.get('type', '').startswith('image'):
            return enc.get('href') or enc.get('url')
    # 3. img tag inside description/summary HTML
    for field in ('summary', 'description', 'content'):
        html = entry.get(field)
        if isinstance(html, list):
            html = html[0].get('value', '') if html else ''
        if html:
            soup = BeautifulSoup(html, 'html.parser')
            img = soup.find('img', src=True)
            if img and img['src'].startswith('http'):
                return img['src']
    # 4. og:image from the article page (fast timeout, best effort)
    url = entry.get('link', '')
    if url:
        try:
            r = requests.get(url, timeout=5, headers={'User-Agent': 'Mozilla/5.0'})
            soup = BeautifulSoup(r.text, 'html.parser')
            og = soup.find('meta', property='og:image') or soup.find('meta', attrs={'name': 'twitter:image'})
            if og and og.get('content', '').startswith('http'):
                return og['content']
        except Exception:
            pass
    return None

def run_scraper():
    log.info('Scraper run started')
    sb = get_client()

    sources = sb.from_('bsn_sources').select('id, name, feed_url').eq('active', True).execute().data

    new_count = 0
    for source in sources:
        log.info(f"Fetching: {source['name']}")
        try:
            feed = feedparser.parse(source['feed_url'])
        except Exception as e:
            log.warning(f"Failed to fetch {source['name']}: {e}")
            continue

        for entry in feed.entries:
            title   = (entry.get('title') or '').strip()
            url     = (entry.get('link')  or '').strip()
            summary = (entry.get('summary') or entry.get('description') or '').strip()
            summary = re.sub(r'<[^>]+>', '', summary)[:500]

            if not title or not url:
                continue
            if not KEYWORDS.search(title + ' ' + summary):
                continue

            pub_date  = parse_date(entry)
            thumbnail = get_thumbnail(entry)
            category  = categorise(title + ' ' + summary)

            try:
                result = sb.from_('bsn_articles').upsert({
                    'title':         title,
                    'url':           url,
                    'source_id':     source['id'],
                    'source_name':   source['name'],
                    'summary':       summary,
                    'thumbnail_url': thumbnail,
                    'published_at':  pub_date,
                    'category':      category,
                }, on_conflict='url', ignore_duplicates=True).execute()
                if result.data:
                    new_count += 1
            except Exception as e:
                log.warning(f"Insert failed for {url}: {e}")
                continue

    log.info(f'Scraper done — {new_count} new articles')
