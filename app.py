import os
import threading
import logging
from datetime import datetime, timezone
from functools import wraps

from flask import (
    Flask, request, redirect, url_for, jsonify,
    render_template, session, g
)
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

import db
from auth import get_current_user, require_auth
from scraper import run_scraper

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ["FLASK_SECRET_KEY"]

PLATFORM_DOMAIN = os.environ.get("PLATFORM_DOMAIN", "bentech.dev")
CLERK_PUBLISHABLE_KEY = os.environ.get("CLERK_PUBLISHABLE_KEY", "")
CLERK_DOMAIN = os.environ.get("CLERK_DOMAIN", "")
PAGE_SIZE = 20


def parse_dt(value):
    if not value:
        return None
    if hasattr(value, 'isoformat'):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%fZ",
    ):
        try:
            dt = datetime.strptime(str(value), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def get_feed_from_request():
    host = request.host.split(":")[0].lower()

    if host == PLATFORM_DOMAIN or host == "localhost" or host == "127.0.0.1":
        return None

    if host.endswith("." + PLATFORM_DOMAIN):
        slug = host[: -(len(PLATFORM_DOMAIN) + 1)]
    else:
        parts = host.split(".")
        if len(parts) >= 2:
            slug = parts[0]
        else:
            return None

    if not slug:
        return None

    rows = db.query(
        "SELECT * FROM agg_feeds WHERE subdomain = %s AND active = TRUE LIMIT 1",
        (slug,)
    )
    return rows[0] if rows else None


def _require_feed_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        user = get_current_user(request)
        if not user:
            return redirect(url_for("auth_login"))

        feed = get_feed_from_request()
        if not feed:
            return ("No feed found for this subdomain.", 404)

        if feed.get("owner_clerk_id") != user.get("sub"):
            return ("Forbidden — you do not own this feed.", 403)

        kwargs["user"] = user
        kwargs["feed"] = feed
        return f(*args, **kwargs)

    return wrapper


def _background_scrape(feed_id):
    t = threading.Thread(target=run_scraper, args=(feed_id,), daemon=True)
    t.start()


@app.context_processor
def inject_globals():
    return {
        "clerk_publishable_key": CLERK_PUBLISHABLE_KEY,
        "platform_domain": PLATFORM_DOMAIN,
        "current_user": get_current_user(request),
    }


@app.route("/")
def index():
    feed = get_feed_from_request()

    if feed:
        page = max(1, int(request.args.get("page", 1)))
        search = request.args.get("q", "").strip()
        category = request.args.get("category", "").strip()

        offset = (page - 1) * PAGE_SIZE

        where = ["feed_id = %s"]
        params = [feed["id"]]
        if search:
            where.append("title ILIKE %s")
            params.append(f"%{search}%")
        if category:
            where.append("category = %s")
            params.append(category)

        where_sql = "WHERE " + " AND ".join(where)

        count_row = db.query(f"SELECT COUNT(*) as cnt FROM agg_articles {where_sql}", params)
        total = count_row[0]["cnt"] if count_row else 0

        articles = db.query(
            f"SELECT * FROM agg_articles {where_sql} ORDER BY published_at DESC LIMIT %s OFFSET %s",
            params + [PAGE_SIZE, offset]
        )

        for a in articles:
            a["published_at_dt"] = parse_dt(a.get("published_at"))

        total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

        cat_rows = db.query(
            "SELECT DISTINCT category FROM agg_articles WHERE feed_id = %s AND category IS NOT NULL ORDER BY category",
            (feed["id"],)
        )
        categories = [r["category"] for r in cat_rows]

        return render_template(
            "feed.html",
            feed=feed,
            articles=articles,
            page=page,
            total_pages=total_pages,
            total=total,
            search=search,
            category=category,
            categories=categories,
        )

    else:
        user = get_current_user(request)
        user_feeds = []
        if user:
            user_feeds = db.query(
                "SELECT * FROM agg_feeds WHERE owner_clerk_id = %s ORDER BY name",
                (user["sub"],)
            )

        return render_template(
            "platform.html",
            user=user,
            user_feeds=user_feeds,
        )


@app.route("/admin")
@_require_feed_auth
def admin_dashboard(user, feed):
    count_row = db.query(
        "SELECT COUNT(*) as cnt FROM agg_articles WHERE feed_id = %s",
        (feed["id"],)
    )
    article_count = count_row[0]["cnt"] if count_row else 0

    sources = db.query(
        "SELECT * FROM agg_sources WHERE feed_id = %s",
        (feed["id"],)
    )

    return render_template(
        "admin/dashboard.html",
        feed=feed,
        user=user,
        article_count=article_count,
        sources=sources,
    )


@app.route("/admin/sources")
@_require_feed_auth
def admin_sources(user, feed):
    sources = db.query(
        "SELECT * FROM agg_sources WHERE feed_id = %s ORDER BY name",
        (feed["id"],)
    )
    return render_template(
        "admin/sources.html",
        feed=feed,
        user=user,
        sources=sources,
    )


@app.route("/admin/sources/add", methods=["POST"])
@_require_feed_auth
def admin_sources_add(user, feed):
    name = request.form.get("name", "").strip()
    feed_url = request.form.get("feed_url", "").strip()

    if not name or not feed_url:
        return redirect(url_for("admin_sources"))

    rows = db.query(
        "INSERT INTO agg_sources (feed_id, name, feed_url, active) VALUES (%s, %s, %s, TRUE) RETURNING id",
        (feed["id"], name, feed_url)
    )

    if rows:
        _background_scrape(feed["id"])

    return redirect(url_for("admin_sources"))


@app.route("/admin/sources/toggle/<source_id>", methods=["POST"])
@_require_feed_auth
def admin_sources_toggle(user, feed, source_id):
    rows = db.query(
        "SELECT active FROM agg_sources WHERE id = %s AND feed_id = %s",
        (source_id, feed["id"])
    )
    if not rows:
        return ("Source not found.", 404)

    new_state = not rows[0]["active"]
    db.execute(
        "UPDATE agg_sources SET active = %s WHERE id = %s AND feed_id = %s",
        (new_state, source_id, feed["id"])
    )
    return redirect(url_for("admin_sources"))


@app.route("/admin/sources/delete/<source_id>", methods=["POST"])
@_require_feed_auth
def admin_sources_delete(user, feed, source_id):
    db.execute(
        "DELETE FROM agg_sources WHERE id = %s AND feed_id = %s",
        (source_id, feed["id"])
    )
    return redirect(url_for("admin_sources"))


@app.route("/admin/appearance")
@_require_feed_auth
def admin_appearance(user, feed):
    return render_template("admin/appearance.html", feed=feed, user=user)


@app.route("/admin/appearance", methods=["POST"])
@_require_feed_auth
def admin_appearance_save(user, feed):
    name = request.form.get("name", "").strip()
    accent_color = request.form.get("accent_color", "").strip()
    logo_url = request.form.get("logo_url", "").strip()

    sets = []
    params = []
    if name:
        sets.append("name = %s"); params.append(name)
    if accent_color:
        sets.append("accent_color = %s"); params.append(accent_color)
    if logo_url is not None:
        sets.append("logo_url = %s"); params.append(logo_url or None)

    if sets:
        params.append(feed["id"])
        db.execute(
            f"UPDATE agg_feeds SET {', '.join(sets)} WHERE id = %s",
            params
        )

    return redirect(url_for("admin_appearance"))


@app.route("/auth/login")
def auth_login():
    clerk_domain = os.environ.get("CLERK_DOMAIN", "")
    sign_in_url = f"https://clerk.{clerk_domain}/sign-in"
    return redirect(sign_in_url)


@app.route("/auth/callback")
def auth_callback():
    user = get_current_user(request)
    if user:
        feed = get_feed_from_request()
        if feed:
            return redirect(url_for("admin_dashboard"))
    return redirect(url_for("index"))


@app.route("/auth/logout")
def auth_logout():
    session.clear()
    clerk_domain = os.environ.get("CLERK_DOMAIN", "")
    sign_out_url = f"https://clerk.{clerk_domain}/sign-out?redirect_url={request.host_url}"
    return redirect(sign_out_url)


@app.route("/api/feeds")
def api_feeds():
    rows = db.query(
        "SELECT id, slug, name, subdomain, accent_color, logo_url FROM agg_feeds WHERE active = TRUE ORDER BY name"
    )
    return jsonify(rows)


def _start_scheduler():
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(
        func=run_scraper,
        trigger="interval",
        hours=6,
        id="global_scrape",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("APScheduler started — scraper runs every 6 hours.")


def _initial_scrape():
    t = threading.Thread(target=run_scraper, daemon=True)
    t.start()
    logger.info("Initial scrape launched in background thread.")


if __name__ == "__main__":
    _start_scheduler()
    _initial_scrape()
    app.run(host="0.0.0.0", port=5000, debug=False)
