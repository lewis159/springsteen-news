import os
import re
import time
import logging
import threading
import calendar
import datetime

import feedparser
import requests
from bs4 import BeautifulSoup

import db

logger = logging.getLogger(__name__)


def _get_thumbnail(entry) -> str | None:
    # 1. media_thumbnail
    media_thumbnails = getattr(entry, 'media_thumbnail', None)
    if media_thumbnails and isinstance(media_thumbnails, list) and len(media_thumbnails) > 0:
        url = media_thumbnails[0].get('url')
        if url:
            return url

    # 2. enclosures with image type
    enclosures = getattr(entry, 'enclosures', None)
    if enclosures and isinstance(enclosures, list):
        for enclosure in enclosures:
            mime = enclosure.get('type', '')
            if mime.startswith('image/'):
                href = enclosure.get('href')
                if href:
                    return href

    # 3. Parse summary or content HTML for first <img>
    html_content = None
    summary = getattr(entry, 'summary', None)
    if summary:
        html_content = summary
    else:
        content = getattr(entry, 'content', None)
        if content and isinstance(content, list) and len(content) > 0:
            html_content = content[0].get('value', '')

    if html_content:
        soup = BeautifulSoup(html_content, 'html.parser')
        img = soup.find('img')
        if img and img.get('src'):
            return img['src']

    # 4. Fetch article URL and check og:image / twitter:image
    url = entry.get('link', '')
    if url:
        try:
            headers = {'User-Agent': 'Mozilla/5.0'}
            resp = requests.get(url, timeout=5, headers=headers)
            resp.raise_for_status()
            page_soup = BeautifulSoup(resp.text, 'html.parser')
            og = page_soup.find('meta', property='og:image')
            if og and og.get('content'):
                return og['content']
            tw = page_soup.find('meta', attrs={'name': 'twitter:image'})
            if tw and tw.get('content'):
                return tw['content']
        except Exception:
            pass

    return None


def _get_category(title: str) -> str:
    t = title.lower()
    if re.search(r'tour|concert|show|live|ticket|setlist', t):
        return 'Tour'
    if re.search(r'album|record|release|song|track|single', t):
        return 'Music'
    if re.search(r'interview|speaks|says|tells', t):
        return 'Interview'
    if re.search(r'review|recap|performance', t):
        return 'Review'
    if re.search(r'trailer|gameplay|reveal|announce', t):
        return 'Trailer'
    if re.search(r'release date|launch|out now|available', t):
        return 'Release'
    return 'News'


def _scrape_source(source: dict, feed_id: str):
    feed_url = source['feed_url']
    logger.info("Scraping source '%s' (%s)", source.get('name'), feed_url)
    feed = feedparser.parse(feed_url)

    count = 0
    for entry in feed.entries:
        try:
            title = entry.get('title', '')
            url = entry.get('link', '')
            if not url:
                continue

            raw_summary = entry.get('summary', '')
            summary = BeautifulSoup(raw_summary, 'html.parser').get_text(separator=' ', strip=True)[:500]

            thumbnail_url = _get_thumbnail(entry)
            category = _get_category(title)

            published_parsed = getattr(entry, 'published_parsed', None)
            if published_parsed and isinstance(published_parsed, time.struct_time):
                ts = calendar.timegm(published_parsed)
                published_at = datetime.datetime.utcfromtimestamp(ts).isoformat() + 'Z'
            else:
                published_at = datetime.datetime.utcnow().isoformat() + 'Z'

            db.execute(
                """INSERT INTO agg_articles (feed_id, source_id, source_name, title, url, summary, thumbnail_url, category, published_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (url) DO NOTHING""",
                (feed_id, source['id'], source['name'], title, url, summary, thumbnail_url, category, published_at)
            )

            count += 1
        except Exception as exc:
            logger.exception("Error processing entry '%s': %s", entry.get('link', ''), exc)
            continue

    logger.info("Source '%s': processed %d articles", source.get('name'), count)


def run_scraper(feed_id: str = None):
    if feed_id:
        sources = db.query(
            "SELECT * FROM agg_sources WHERE feed_id = %s AND active = TRUE",
            (feed_id,)
        )
        for source in sources:
            _scrape_source(source, feed_id)
    else:
        feeds = db.query("SELECT * FROM agg_feeds WHERE active = TRUE")
        for feed in feeds:
            fid = feed['id']
            sources = db.query(
                "SELECT * FROM agg_sources WHERE feed_id = %s AND active = TRUE",
                (fid,)
            )
            for source in sources:
                _scrape_source(source, fid)
