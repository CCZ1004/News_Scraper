import logging
import threading
from collections import defaultdict
from flask import Flask, render_template, redirect, url_for, jsonify, request

from scraper import get_news
from agent import run_agent
from llm import icon_for
from db import init_db, clear_today, prune_old

# ── Background job cache ──────────────────────────────────────────────────────
# _jobs[region] = {"status": "loading"|"done"|"error", "data": list|None}
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _ensure_job(region: str, query: str) -> None:
    """Start a background fetch for `region` if one isn't already running/done."""
    with _jobs_lock:
        job = _jobs.get(region)
        if job and job["status"] in ("loading", "done"):
            return  # already in-flight or finished
        _jobs[region] = {"status": "loading", "data": None}

    def _worker():
        try:
            data = get_news(query, region=region)
            with _jobs_lock:
                _jobs[region] = {"status": "done", "data": data}
        except Exception as e:
            logging.error("Background fetch failed for %s: %s", region, e)
            with _jobs_lock:
                _jobs[region] = {"status": "error", "data": None}

    t = threading.Thread(target=_worker, daemon=True)
    t.start()

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

logging.getLogger("trafilatura").setLevel(logging.ERROR)
logging.getLogger("trafilatura.core").setLevel(logging.ERROR)

app = Flask(__name__)

init_db()
prune_old(keep_days=3)


def _build_categories(news: list[dict]) -> tuple[list[dict], int]:
    """Group articles into sorted category dicts and return (categories, total)."""
    grouped: defaultdict[str, list] = defaultdict(list)
    for item in news:
        grouped[item["category"]].append(item)

    categories = [
        {
            "name":     cat,
            "icon":     icon_for(cat),
            "articles": articles,
            "count":    len(articles),
        }
        for cat, articles in grouped.items()
    ]
    categories.sort(key=lambda x: x["count"], reverse=True)
    total = sum(c["count"] for c in categories)
    return categories, total


@app.route("/")
def home():
    """Immediately show the loading page and kick off a background fetch."""
    _ensure_job("MY", "Malaysia")
    with _jobs_lock:
        job = _jobs.get("MY", {})
    # If already done (warm cache), skip the loading page
    if job.get("status") == "done":
        return redirect(url_for("news_my"))
    return render_template("loading.html",
                           region="MY",
                           title="Malaysia Today",
                           subtitle="Fetching today's Malaysian news…")


@app.route("/news")
def news_my():
    """Render the Malaysia news page — only called once the job is done."""
    with _jobs_lock:
        job = _jobs.get("MY", {})
    if job.get("status") != "done":
        return redirect(url_for("home"))
    categories, total = _build_categories(job["data"] or [])
    return render_template("index.html", categories=categories, total=total)


@app.route("/api/news/status")
def news_status():
    """Return current fetch status for the requested region."""
    region = request.args.get("region", "MY").upper()
    with _jobs_lock:
        job = _jobs.get(region, {"status": "idle"})
    return jsonify({"status": job["status"]})


@app.route("/refresh", methods=["POST"])
def refresh():
    """Clear today's MY DB cache and mark job as idle so next visit re-fetches."""
    cleared = clear_today(region="MY")
    logging.info("/refresh: cleared %d cached articles (MY)", cleared)
    with _jobs_lock:
        _jobs.pop("MY", None)
    return redirect(url_for("home"))


@app.route("/global")
def global_news():
    """Immediately show the loading page and kick off a background fetch."""
    _ensure_job("GLOBAL", "Top Stories")
    with _jobs_lock:
        job = _jobs.get("GLOBAL", {})
    if job.get("status") == "done":
        return redirect(url_for("news_global"))
    return render_template("loading.html",
                           region="GLOBAL",
                           title="Global News",
                           subtitle="Fetching top global stories…")


@app.route("/global/news")
def news_global():
    """Render the Global news page — only called once the job is done."""
    with _jobs_lock:
        job = _jobs.get("GLOBAL", {})
    if job.get("status") != "done":
        return redirect(url_for("global_news"))
    categories, total = _build_categories(job["data"] or [])
    return render_template("global.html", categories=categories, total=total)


@app.route("/global/refresh", methods=["POST"])
def global_refresh():
    """Clear today's GLOBAL DB cache and mark job as idle so next visit re-fetches."""
    cleared = clear_today(region="GLOBAL")
    logging.info("/global/refresh: cleared %d cached articles (GLOBAL)", cleared)
    with _jobs_lock:
        _jobs.pop("GLOBAL", None)
    return redirect(url_for("global_news"))


@app.route("/chat", methods=["POST"])
def chat():
    """Agent chat endpoint. Accepts JSON {message: str, region: str}, returns {answer: str}."""
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    region  = (data.get("region")  or "MY").upper()
    if not message:
        return jsonify({"answer": "Please ask me something about today's news."}), 400
    result = run_agent(message, region=region)
    return jsonify({"answer": result["answer"], "turns": result["turns"]})


if __name__ == "__main__":
    # use_reloader=False avoids OSError: [WinError 10038] on Python 3.12+ / Windows
    app.run(debug=True, use_reloader=False)