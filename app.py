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

from db import get_client
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_dt(value):
    """Parse an ISO timestamp string into a timezone-aware datetime (UTC)."""
    if not value:
        return None
    # Supabase returns strings like "2026-06-24T10:00:00+00:00"
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%fZ",
    ):
        try:
            dt = datetime.strptime(value, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def get_feed_from_request():
    """
    Extract subdomain from request.host and look up the matching agg_feeds row.
    Returns None when the request is for the root/platform domain.
    """
    host = request.host.split(":")[0].lower()  # strip port

    # Determine subdomain slug
    if host == PLATFORM_DOMAIN or host == "localhost" or host == "127.0.0.1":
        return None

    if host.endswith("." + PLATFORM_DOMAIN):
        slug = host[: -(len(PLATFORM_DOMAIN) + 1)]
    else:
        # Running locally with e.g. bruce.localhost — treat everything before
        # the last dot-segment as the slug.
        parts = host.split(".")
        if len(parts) >= 2:
            slug = parts[0]
        else:
            return None

    if not slug:
        return None

    try:
        resp = (
            get_client()
            .table("agg_feeds")
            .select("*")
            .eq("subdomain", slug)
            .eq("active", True)
            .single()
            .execute()
        )
        return resp.data
    except Exception:
        return None


def _require_feed_auth(f):
    """
    Decorator that: (1) verifies Clerk auth, (2) ensures the authed user owns
    the feed on the current subdomain, (3) injects feed + user into kwargs.
    """
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
    """Run scraper in a daemon thread so it never blocks a request."""
    t = threading.Thread(target=run_scraper, args=(feed_id,), daemon=True)
    t.start()


# ---------------------------------------------------------------------------
# Template context — inject common vars into every render
# ---------------------------------------------------------------------------

@app.context_processor
def inject_globals():
    return {
        "clerk_publishable_key": CLERK_PUBLISHABLE_KEY,
        "platform_domain": PLATFORM_DOMAIN,
        "current_user": get_current_user(request),
    }


# ---------------------------------------------------------------------------
# Public reader — GET /
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    feed = get_feed_from_request()

    if feed:
        # ---- Subdomain feed reader ----
        page = max(1, int(request.args.get("page", 1)))
        search = request.args.get("q", "").strip()
        category = request.args.get("category", "").strip()

        offset = (page - 1) * PAGE_SIZE

        query = (
            get_client()
            .table("agg_articles")
            .select("*", count="exact")
            .eq("feed_id", feed["id"])
            .order("published_at", desc=True)
            .range(offset, offset + PAGE_SIZE - 1)
        )

        if search:
            # PostgREST full-text-style ilike on title
            query = query.ilike("title", f"%{search}%")
        if category:
            query = query.eq("category", category)

        resp = query.execute()
        articles = resp.data or []
        total = resp.count or 0

        # Parse datetimes for template use
        for a in articles:
            a["published_at_dt"] = parse_dt(a.get("published_at"))

        total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

        # Available categories for filter dropdown
        cats_resp = (
            get_client()
            .table("agg_articles")
            .select("category")
            .eq("feed_id", feed["id"])
            .execute()
        )
        categories = sorted(
            {r["category"] for r in (cats_resp.data or []) if r.get("category")}
        )

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
        # ---- Platform home ----
        user = get_current_user(request)
        user_feeds = []
        if user:
            resp = (
                get_client()
                .table("agg_feeds")
                .select("*")
                .eq("owner_clerk_id", user["sub"])
                .order("name")
                .execute()
            )
            user_feeds = resp.data or []

        return render_template(
            "platform.html",
            user=user,
            user_feeds=user_feeds,
        )


# ---------------------------------------------------------------------------
# Admin routes
# ---------------------------------------------------------------------------

@app.route("/admin")
@_require_feed_auth
def admin_dashboard(user, feed):
    # Recent article count + source count
    arts_resp = (
        get_client()
        .table("agg_articles")
        .select("id", count="exact")
        .eq("feed_id", feed["id"])
        .execute()
    )
    src_resp = (
        get_client()
        .table("agg_sources")
        .select("*")
        .eq("feed_id", feed["id"])
        .execute()
    )
    return render_template(
        "admin/dashboard.html",
        feed=feed,
        user=user,
        article_count=arts_resp.count or 0,
        sources=src_resp.data or [],
    )


@app.route("/admin/sources")
@_require_feed_auth
def admin_sources(user, feed):
    resp = (
        get_client()
        .table("agg_sources")
        .select("*")
        .eq("feed_id", feed["id"])
        .order("name")
        .execute()
    )
    return render_template(
        "admin/sources.html",
        feed=feed,
        user=user,
        sources=resp.data or [],
    )


@app.route("/admin/sources/add", methods=["POST"])
@_require_feed_auth
def admin_sources_add(user, feed):
    name = request.form.get("name", "").strip()
    feed_url = request.form.get("feed_url", "").strip()

    if not name or not feed_url:
        return redirect(url_for("admin_sources"))

    resp = (
        get_client()
        .table("agg_sources")
        .insert({"feed_id": feed["id"], "name": name, "feed_url": feed_url, "active": True})
        .execute()
    )

    if resp.data:
        # Fire off a scrape for this feed in the background
        _background_scrape(feed["id"])

    return redirect(url_for("admin_sources"))


@app.route("/admin/sources/toggle/<source_id>", methods=["POST"])
@_require_feed_auth
def admin_sources_toggle(user, feed, source_id):
    # Fetch current state
    resp = (
        get_client()
        .table("agg_sources")
        .select("active")
        .eq("id", source_id)
        .eq("feed_id", feed["id"])
        .single()
        .execute()
    )
    if not resp.data:
        return ("Source not found.", 404)

    new_state = not resp.data["active"]
    get_client().table("agg_sources").update({"active": new_state}).eq("id", source_id).execute()
    return redirect(url_for("admin_sources"))


@app.route("/admin/sources/delete/<source_id>", methods=["POST"])
@_require_feed_auth
def admin_sources_delete(user, feed, source_id):
    get_client().table("agg_sources").delete().eq("id", source_id).eq("feed_id", feed["id"]).execute()
    return redirect(url_for("admin_sources"))


@app.route("/admin/appearance")
@_require_feed_auth
def admin_appearance(user, feed):
    return render_template("admin/appearance.html", feed=feed, user=user)


@app.route("/admin/appearance", methods=["POST"])
@_require_feed_auth
def admin_appearance_save(user, feed):
    updates = {}
    name = request.form.get("name", "").strip()
    accent_color = request.form.get("accent_color", "").strip()
    logo_url = request.form.get("logo_url", "").strip()

    if name:
        updates["name"] = name
    if accent_color:
        updates["accent_color"] = accent_color
    if logo_url is not None:  # allow clearing
        updates["logo_url"] = logo_url or None

    if updates:
        get_client().table("agg_feeds").update(updates).eq("id", feed["id"]).execute()

    return redirect(url_for("admin_appearance"))


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.route("/auth/login")
def auth_login():
    """Redirect to Clerk hosted sign-in page."""
    clerk_domain = os.environ.get("CLERK_DOMAIN", "")
    sign_in_url = f"https://clerk.{clerk_domain}/sign-in"
    return redirect(sign_in_url)


@app.route("/auth/callback")
def auth_callback():
    """
    Clerk sets the __session cookie automatically after sign-in; this route
    just receives the redirect and sends the user to the appropriate page.
    """
    user = get_current_user(request)
    if user:
        feed = get_feed_from_request()
        if feed:
            return redirect(url_for("admin_dashboard"))
    return redirect(url_for("index"))


@app.route("/auth/logout")
def auth_logout():
    session.clear()
    # Clerk also needs its own session cleared — redirect to Clerk sign-out
    clerk_domain = os.environ.get("CLERK_DOMAIN", "")
    sign_out_url = f"https://clerk.{clerk_domain}/sign-out?redirect_url={request.host_url}"
    return redirect(sign_out_url)


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.route("/api/feeds")
def api_feeds():
    """Return JSON list of all active feeds (for platform home widgets etc.)."""
    resp = (
        get_client()
        .table("agg_feeds")
        .select("id, slug, name, subdomain, accent_color, logo_url")
        .eq("active", True)
        .order("name")
        .execute()
    )
    return jsonify(resp.data or [])


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

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
    """Run the first scrape in a background thread so Flask start isn't blocked."""
    t = threading.Thread(target=run_scraper, daemon=True)
    t.start()
    logger.info("Initial scrape launched in background thread.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    _start_scheduler()
    _initial_scrape()
    app.run(host="0.0.0.0", port=5000, debug=False)
