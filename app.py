from flask import Flask, render_template, request
from datetime import datetime, timezone
import os
from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler
import db
from scraper import run_scraper

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'dev')

def parse_dt(val):
    if not val:
        return None
    if hasattr(val, 'isoformat'):
        return val if val.tzinfo else val.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(val).replace('Z', '+00:00'))
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

    # Build WHERE clauses
    where = []
    params = []
    if search:
        where.append("(title ILIKE %s OR summary ILIKE %s)")
        params += [f'%{search}%', f'%{search}%']
    if category:
        where.append("category = %s")
        params.append(category)
    if source:
        where.append("source_name = %s")
        params.append(source)

    where_sql = ('WHERE ' + ' AND '.join(where)) if where else ''

    count_row = db.query(f"SELECT COUNT(*) as cnt FROM bsn_articles {where_sql}", params or None)
    total = count_row[0]['cnt'] if count_row else 0

    articles = db.query(
        f"SELECT * FROM bsn_articles {where_sql} ORDER BY published_at DESC LIMIT %s OFFSET %s",
        (params + [per_page, offset]) if params else [per_page, offset]
    )

    for a in articles:
        a['published_at'] = parse_dt(a.get('published_at'))

    # Source counts for sidebar
    src_rows = db.query(
        "SELECT source_name, COUNT(*) as cnt FROM bsn_articles WHERE source_name IS NOT NULL GROUP BY source_name ORDER BY cnt DESC"
    )
    sources = [{'source_name': r['source_name'], 'cnt': r['cnt']} for r in src_rows]

    # Category counts for sidebar
    cat_rows = db.query(
        "SELECT category, COUNT(*) as cnt FROM bsn_articles WHERE category IS NOT NULL GROUP BY category ORDER BY cnt DESC"
    )
    categories = [{'category': r['category'], 'cnt': r['cnt']} for r in cat_rows]

    total_articles_row = db.query("SELECT COUNT(*) as cnt FROM bsn_articles")
    total_articles = total_articles_row[0]['cnt'] if total_articles_row else 0
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
    scheduler.add_job(run_scraper, 'date', id='scraper_boot')
    scheduler.start()
    app.run(host='0.0.0.0', port=5000, debug=False)
