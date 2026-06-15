from flask import Flask, render_template, request
from datetime import datetime, timezone
import os
from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler
from db import get_client
from scraper import run_scraper

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'dev')

def parse_dt(val):
    if not val:
        return None
    try:
        return datetime.fromisoformat(val.replace('Z', '+00:00'))
    except Exception:
        return None

@app.route('/')
def index():
    search   = request.args.get('q', '').strip()
    category = request.args.get('category', '').strip()
    source   = request.args.get('source', '').strip()
    page     = max(1, int(request.args.get('page', 1)))
    per_page = 20
    offset   = (page - 1) * per_page

    sb = get_client()
    schema = sb

    # Build article query
    q = schema.from_('bsn_articles').select('*', count='exact')
    if search:
        q = q.or_(f'title.ilike.%{search}%,summary.ilike.%{search}%')
    if category:
        q = q.eq('category', category)
    if source:
        q = q.eq('source_name', source)

    q = q.order('published_at', desc=True).range(offset, offset + per_page - 1)
    result = q.execute()
    articles = result.data or []
    total    = result.count or 0

    for a in articles:
        a['published_at'] = parse_dt(a.get('published_at'))

    # Sidebar: sources with counts
    src_result = schema.from_('bsn_articles').select('source_name').not_.is_('source_name', 'null').execute()
    src_counts = {}
    for row in (src_result.data or []):
        n = row['source_name']
        src_counts[n] = src_counts.get(n, 0) + 1
    sources = sorted([{'source_name': k, 'cnt': v} for k, v in src_counts.items()], key=lambda x: -x['cnt'])

    # Sidebar: categories with counts
    cat_result = schema.from_('bsn_articles').select('category').not_.is_('category', 'null').execute()
    cat_counts = {}
    for row in (cat_result.data or []):
        c = row['category']
        cat_counts[c] = cat_counts.get(c, 0) + 1
    categories = sorted([{'category': k, 'cnt': v} for k, v in cat_counts.items()], key=lambda x: -x['cnt'])

    total_articles = schema.from_('bsn_articles').select('id', count='exact').execute().count or 0
    pages = max(1, -(-total // per_page))

    return render_template('index.html',
        articles=articles,
        sources=sources,
        categories=categories,
        total_articles=total_articles,
        search=search,
        selected_category=category,
        selected_source=source,
        page=page,
        total=total,
        per_page=per_page,
        pages=pages,
    )

if __name__ == '__main__':
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(run_scraper, 'interval', hours=6, id='scraper')
    scheduler.add_job(run_scraper, 'date', id='scraper_boot')  # run once after startup
    scheduler.start()
    app.run(host='0.0.0.0', port=5000, debug=False)
