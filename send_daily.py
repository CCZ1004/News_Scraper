

import sys
import logging
import requests as _requests
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from collections import defaultdict

# ── Config ─────────────────────────────────────────────────────────────────────
RECIPIENT     = "[EMAIL_ADDRESS]"
SENDER        = "chiuzeze@gmail.com"
REGION        = "MY"
OBSIDIAN_VAULT = Path(r"D:\NEWS\Obsidian Vault")   
OBSIDIAN_FOLDER = OBSIDIAN_VAULT / "News"
_MYT          = ZoneInfo("Asia/Kuala_Lumpur")
LOG_FILE      = Path(__file__).parent / "send_daily.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

CATEGORY_ICONS = {
    "local politics":  "🏛️",
    "economy":         "📈",
    "crime & law":     "⚖️",
    "public health":   "🏥",
    "sports":          "🏆",
    "technology":      "💻",
    "environment":     "🌿",
    "education":       "🎓",
    "foreign affairs": "🌏",
    "transport":       "🚆",
    "religion":        "🕌",
    "business":        "💼",
    "social issues":   "🤝",
    "entertainment":   "🎬",
    "labour rights":   "👷",
}


# ── Step 1: Scrape today's news ────────────────────────────────────────────────
def _scrape_news() -> list[dict]:
    """Fetch and process today's Malaysian news into SQLite. Returns articles."""
    logger.info("── Step 1: Scraping today's news ──")
    try:
        from scraper import get_news
        articles = get_news(query="Malaysia", region=REGION)
        logger.info("Scrape complete — %d articles in DB", len(articles))
        return articles
    except Exception as e:
        logger.error("Scrape failed: %s", e)
        return []


# ── Step 2: Build Markdown note ────────────────────────────────────────────────
def _build_markdown(articles: list[dict], now: datetime) -> str:
    """Generate a full Obsidian-flavoured Markdown note from today's articles."""

    date_str     = now.strftime("%Y-%m-%d")
    date_display = now.strftime("%A, %d %B %Y")
    day_of_week  = now.strftime("%A")

    # Group by category
    grouped: dict[str, list] = defaultdict(list)
    for a in articles:
        grouped[a["category"]].append(a)
    sorted_cats = sorted(grouped.items(), key=lambda x: -len(x[1]))

    lines = []

    # ── Frontmatter ──
    lines += [
        "---",
        f"date: {date_str}",
        f"day: {day_of_week}",
        f"articles: {len(articles)}",
        f"categories: {len(sorted_cats)}",
        "tags: [news, malaysia]",
        "---",
        "",
    ]

    # ── Title ──
    lines += [
        f"# 🗞️ Malaysia Today — {date_display}",
        "",
        f"> {len(articles)} articles across {len(sorted_cats)} categories",
        "",
    ]

    # ── Today at a Glance (top 5) ──
    lines += ["## Today at a Glance", ""]
    count = 0
    for cat, arts in sorted_cats:
        for a in arts:
            bullets = _parse_bullets(a.get("summary", ""))
            if bullets:
                lines.append(f"- **{a['title']}** — {bullets[0]}")
            else:
                lines.append(f"- **{a['title']}**")
            count += 1
            if count >= 5:
                break
        if count >= 5:
            break

    lines += ["", "---", ""]

    # ── Category sections ──
    for category, arts in sorted_cats:
        icon = CATEGORY_ICONS.get(category.lower(), "📰")
        lines += [f"## {icon} {category}", ""]

        for a in arts:
            lines.append(f"### {a['title']}")
            pub = a.get("published", "").strip()
            if pub:
                lines.append(f"📅 {pub} MYT")
            lines.append("")

            bullets = _parse_bullets(a.get("summary", ""))
            if bullets:
                for b in bullets:
                    lines.append(f"- {b}")
            else:
                lines.append("- No summary available.")

            lines += ["", "---", ""]

    # ── Footer ──
    lines += [
        f"*Generated automatically on {date_display} by Malaysia Today News App.*",
        f"*Powered by Google News RSS + Ollama LLM.*",
    ]

    return "\n".join(lines)


def _parse_bullets(summary: str) -> list[str]:
    """Extract clean bullet lines from LLM summary text."""
    import re
    lines = []
    for line in summary.splitlines():
        line = line.strip()
        line = re.sub(r"^[\u2022\u2013\-\*]\s*", "", line)
        line = re.sub(r"^\d+[\.\)]\s+", "", line)
        if len(line) > 10:
            lines.append(line)
    return lines[:3]


# ── Step 3: Save to Obsidian ───────────────────────────────────────────────────
def _save_to_obsidian(markdown: str, now: datetime) -> Path:
    """Write the Markdown note into the Obsidian vault."""
    logger.info("── Step 2: Saving to Obsidian ──")

    # Create News folder if it doesn't exist
    OBSIDIAN_FOLDER.mkdir(parents=True, exist_ok=True)

    filename = f"{now.strftime('%Y-%m-%d')}.md"
    note_path = OBSIDIAN_FOLDER / filename
    note_path.write_text(markdown, encoding="utf-8")

    logger.info("Note saved → %s", note_path)
    return note_path


# ── Step 4: Email the note ─────────────────────────────────────────────────────
def _send_email(markdown: str, article_count: int, now: datetime) -> None:
    """Send the Markdown note content as an email via Gmail MCP."""
    logger.info("── Step 3: Sending email via Gmail MCP ──")

    today_str = now.strftime("%A, %d %B %Y")
    subject   = f"Malaysia Today — {today_str}"

    # Plain-text email body — convert key markdown to readable text
    body = markdown \
        .replace("**", "") \
        .replace("##", "") \
        .replace("#", "") \
        .replace("---", "─" * 40) \
        .replace("> ", "")

    prompt = (
        f"Please send an email using Gmail with these exact details:\n\n"
        f"To: {RECIPIENT}\n"
        f"From: {SENDER}\n"
        f"Subject: {subject}\n"
        f"Body:\n{body}\n\n"
        f"Send the email now and confirm when done."
    )

    response = _requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"Content-Type": "application/json"},
        json={
            "model":      "claude-sonnet-4-20250514",
            "max_tokens": 1000,
            "messages":   [{"role": "user", "content": prompt}],
            "mcp_servers": [
                {
                    "type": "url",
                    "url":  "https://gmailmcp.googleapis.com/mcp/v1",
                    "name": "gmail-mcp",
                }
            ],
        },
        timeout=120,
    )
    response.raise_for_status()
    data = response.json()

    text_blocks = [
        b.get("text", "") for b in data.get("content", [])
        if b.get("type") == "text"
    ]
    reply = " ".join(text_blocks).strip()
    logger.info("Gmail MCP reply: %s", reply[:300])

    print(f"\n✅  Email sent!")
    print(f"    To      : {RECIPIENT}")
    print(f"    Subject : {subject}")
    print(f"    Stories : {article_count}")


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    now = datetime.now(_MYT)
    logger.info("════════════════════════════════════════════")
    logger.info("  Malaysia Today Daily Pipeline")
    logger.info("  %s", now.strftime("%A, %d %B %Y — %H:%M MYT"))
    logger.info("════════════════════════════════════════════")

    try:
        # 1. Scrape
        articles = _scrape_news()

        # 2. Build Markdown
        logger.info("── Step 2: Building Markdown note ──")
        markdown = _build_markdown(articles, now)
        logger.info("Markdown built — %d characters", len(markdown))

        # 3. Save to Obsidian
        note_path = _save_to_obsidian(markdown, now)

        # 4. Email
        _send_email(markdown, len(articles), now)

        logger.info("════ Pipeline complete ════")
        print(f"\n📓 Note saved : {note_path}")

    except Exception as e:
        logger.error("Pipeline failed: %s", e, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()