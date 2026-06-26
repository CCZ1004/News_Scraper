"""
export.py — Generate a newspaper-style PDF from today's cached news.

Standalone:
    python export.py
    → malaysia_today_YYYY-MM-DD.pdf

From Flask:
    from export import build_newspaper_pdf
    pdf_bytes = build_newspaper_pdf(region="MY")
"""

import sys
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from io import BytesIO
from collections import defaultdict

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_JUSTIFY, TA_RIGHT
from reportlab.platypus import (
    BaseDocTemplate, PageTemplate, Frame,
    Paragraph, Spacer, HRFlowable, FrameBreak,
    KeepTogether, PageBreak, Table, TableStyle,
)
from reportlab.lib.utils import simpleSplit

# ── Constants ──────────────────────────────────────────────────────────────────
_MYT       = ZoneInfo("Asia/Kuala_Lumpur")
PAPER_NAME = "Malaysia Today"
PAGE_W, PAGE_H = A4          # 595 x 842 pt
MARGIN     = 14 * mm
COL_GAP    = 5  * mm

# Colours
C_BLACK      = colors.HexColor("#0D0D0D")
C_DARK       = colors.HexColor("#1A1A1A")
C_DARK_GREY  = colors.HexColor("#2B2B2B")
C_MID_GREY   = colors.HexColor("#666666")
C_LIGHT_GREY = colors.HexColor("#DDDDDD")
C_ACCENT     = colors.HexColor("#8B0000")   # dark red — classic broadsheet
C_WHITE      = colors.white

CATEGORY_ICONS = {
    "local politics": "▪", "economy": "▪", "crime & law": "▪",
    "public health": "▪", "sports": "▪", "technology": "▪",
    "environment": "▪", "education": "▪", "foreign affairs": "▪",
    "transport": "▪", "religion": "▪", "business": "▪",
    "social issues": "▪", "entertainment": "▪", "labour rights": "▪",
}


# ── Styles ─────────────────────────────────────────────────────────────────────
def _make_styles():
    return {
        "masthead": ParagraphStyle(
            "masthead", fontName="Times-Bold", fontSize=42, leading=46,
            textColor=C_BLACK, alignment=TA_CENTER, spaceAfter=1,
        ),
        "tagline": ParagraphStyle(
            "tagline", fontName="Times-Italic", fontSize=8.5, leading=11,
            textColor=C_MID_GREY, alignment=TA_CENTER, spaceAfter=1,
        ),
        "dateline": ParagraphStyle(
            "dateline", fontName="Times-Roman", fontSize=8, leading=10,
            textColor=C_MID_GREY, alignment=TA_LEFT, spaceAfter=0,
        ),
        "edition": ParagraphStyle(
            "edition", fontName="Times-Roman", fontSize=8, leading=10,
            textColor=C_MID_GREY, alignment=TA_RIGHT, spaceAfter=0,
        ),
        "section_banner": ParagraphStyle(
            "section_banner", fontName="Times-Bold", fontSize=8, leading=10,
            textColor=C_WHITE, alignment=TA_CENTER,
        ),
        "digest_title": ParagraphStyle(
            "digest_title", fontName="Times-Bold", fontSize=9.5, leading=12,
            textColor=C_ACCENT, alignment=TA_LEFT, spaceBefore=4, spaceAfter=2,
        ),
        "digest_bullet": ParagraphStyle(
            "digest_bullet", fontName="Times-Roman", fontSize=8.5, leading=12,
            textColor=C_DARK_GREY, alignment=TA_LEFT,
            leftIndent=10, firstLineIndent=-8, spaceAfter=2,
        ),
        "headline": ParagraphStyle(
            "headline", fontName="Times-Bold", fontSize=11.5, leading=14,
            textColor=C_BLACK, alignment=TA_LEFT, spaceBefore=6, spaceAfter=2,
        ),
        "subhead": ParagraphStyle(
            "subhead", fontName="Times-Italic", fontSize=8.5, leading=11,
            textColor=C_MID_GREY, alignment=TA_LEFT, spaceAfter=2,
        ),
        "body": ParagraphStyle(
            "body", fontName="Times-Roman", fontSize=8.5, leading=12,
            textColor=C_DARK_GREY, alignment=TA_JUSTIFY, spaceAfter=2,
        ),
        "bullet": ParagraphStyle(
            "bullet", fontName="Times-Roman", fontSize=8.5, leading=12,
            textColor=C_DARK_GREY, alignment=TA_LEFT,
            leftIndent=10, firstLineIndent=-8, spaceAfter=2,
        ),
        "footer": ParagraphStyle(
            "footer", fontName="Times-Roman", fontSize=7, leading=9,
            textColor=C_MID_GREY, alignment=TA_CENTER,
        ),
    }


# ── Helpers ────────────────────────────────────────────────────────────────────
def _rule(thick=0.5, color=C_DARK, sb=2, sa=3):
    return HRFlowable(width="100%", thickness=thick, color=color,
                      spaceBefore=sb, spaceAfter=sa)


def _thick_rule(sb=0, sa=3):
    return HRFlowable(width="100%", thickness=2.5, color=C_BLACK,
                      spaceBefore=sb, spaceAfter=sa)


def _double_rule():
    """Two close horizontal rules — classic broadsheet divider."""
    return [
        HRFlowable(width="100%", thickness=2.5, color=C_BLACK, spaceBefore=2, spaceAfter=1),
        HRFlowable(width="100%", thickness=0.5, color=C_BLACK, spaceBefore=0, spaceAfter=4),
    ]


def _safe(text: str) -> str:
    """Escape XML special chars for ReportLab Paragraph."""
    return (text or "")                 \
        .replace("&", "&amp;")          \
        .replace("<", "&lt;")           \
        .replace(">", "&gt;")           \
        .replace("\u2019", "'")         \
        .replace("\u2018", "'")         \
        .replace("\u201c", '"')         \
        .replace("\u201d", '"')         \
        .replace("\u2013", "-")         \
        .replace("\u2014", "--")


def _parse_bullets(summary: str) -> list[str]:
    """Extract bullet lines from LLM summary text."""
    lines = []
    for line in summary.splitlines():
        line = line.strip()
        # strip common bullet prefixes
        line = re.sub(r"^[\u2022\u2013\-\*]\s*", "", line)
        line = re.sub(r"^\d+[\.\)]\s+", "", line)
        if len(line) > 10:
            lines.append(line)
    return lines[:3]


def _section_banner(text: str, col_width: float, st: dict):
    """Dark-red pill banner with white label."""
    cell = Paragraph(_safe(text.upper()), st["section_banner"])
    t = Table([[cell]], colWidths=[col_width])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), C_ACCENT),
        ("TOPPADDING",    (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING",   (0, 0), (-1, -1), 4),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 4),
    ]))
    return t


# ── Page layout helpers ────────────────────────────────────────────────────────
def _header_footer(canvas, doc):
    """Draw thin top rule and footer on every page."""
    canvas.saveState()
    now_str = datetime.now(_MYT).strftime("%d %B %Y")

    # Footer rule
    y_foot = MARGIN - 6 * mm
    canvas.setStrokeColor(C_LIGHT_GREY)
    canvas.setLineWidth(0.4)
    canvas.line(MARGIN, y_foot + 4 * mm, PAGE_W - MARGIN, y_foot + 4 * mm)

    # Footer text
    canvas.setFont("Times-Roman", 7)
    canvas.setFillColor(C_MID_GREY)
    canvas.drawString(MARGIN, y_foot, PAPER_NAME)
    canvas.drawCentredString(PAGE_W / 2, y_foot, now_str)
    canvas.drawRightString(PAGE_W - MARGIN, y_foot, f"Page {doc.page}")

    canvas.restoreState()


# ── Masthead ───────────────────────────────────────────────────────────────────
def _build_masthead(now: datetime, total: int, st: dict, full_width: float) -> list:
    story = []

    # Top thin rule
    story.append(_rule(thick=0.75, sb=0, sa=4))

    # Paper name
    story.append(Paragraph(PAPER_NAME, st["masthead"]))

    # Tagline
    story.append(Paragraph(
        "Your trusted source for Malaysian news — concise, clear, complete.",
        st["tagline"]
    ))

    # Date / edition row using a table
    date_str  = now.strftime("%A, %d %B %Y")
    price_str = f"{total} stories today  •  Free"
    row = Table(
        [[Paragraph(_safe(date_str), st["dateline"]),
          Paragraph(_safe(price_str), st["edition"])]],
        colWidths=[full_width / 2, full_width / 2],
    )
    row.setStyle(TableStyle([
        ("LEFTPADDING",   (0, 0), (-1, -1), 0),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 0),
        ("TOPPADDING",    (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(row)
    story.extend(_double_rule())

    return story


# ── Front-page digest ──────────────────────────────────────────────────────────
def _build_digest(articles: list[dict], st: dict) -> list:
    """5-bullet digest box across the full width."""
    story = []
    story.append(Paragraph("TODAY AT A GLANCE", st["digest_title"]))
    story.append(_rule(thick=0.5, sb=0, sa=3))

    count = 0
    for a in articles:
        bullets = _parse_bullets(a.get("summary", ""))
        if bullets:
            line = f"<b>{_safe(a['title'][:70])}</b> — {_safe(bullets[0])}"
        else:
            line = f"<b>{_safe(a['title'][:90])}</b>"
        story.append(Paragraph(f"• {line}", st["digest_bullet"]))
        count += 1
        if count >= 5:
            break

    story.append(_rule(thick=0.5, sb=3, sa=4))
    return story


# ── Article block (fits inside one column) ─────────────────────────────────────
def _build_article(article: dict, col_width: float, st: dict) -> list:
    items = []

    # Headline
    items.append(Paragraph(_safe(article["title"]), st["headline"]))

    # Published time
    pub = article.get("published", "").strip()
    if pub:
        items.append(Paragraph(f"Published {_safe(pub)} MYT", st["subhead"]))

    # Bullet summary
    bullets = _parse_bullets(article.get("summary", ""))
    if bullets:
        for b in bullets:
            items.append(Paragraph(f"• {_safe(b)}", st["bullet"]))
    else:
        items.append(Paragraph("No summary available.", st["body"]))

    items.append(_rule(thick=0.4, color=C_LIGHT_GREY, sb=4, sa=2))
    return items


# ── Two-column category section ────────────────────────────────────────────────
def _build_category_section(
    category: str,
    articles: list[dict],
    col_width: float,
    st: dict,
) -> list:
    """
    One Table row per pair of articles so no single cell ever
    exceeds the page height — fixes reportlab LayoutError.
    """
    story = []
    inner_col = (col_width - COL_GAP) / 2

    # Section banner
    story.append(Spacer(1, 4))
    story.append(_section_banner(category, col_width, st))
    story.append(Spacer(1, 4))

    # Pair articles: (left, right) — right is None for odd counts
    for i in range(0, len(articles), 2):
        left  = articles[i]
        right = articles[i + 1] if i + 1 < len(articles) else None

        left_block  = _build_article(left,  inner_col, st)
        right_block = _build_article(right, inner_col, st) if right else [Spacer(1, 1)]

        row_table = Table(
            [[left_block, right_block]],
            colWidths=[inner_col, inner_col],
            hAlign="LEFT",
            splitByRow=1,
        )
        row_table.setStyle(TableStyle([
            ("VALIGN",        (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING",   (0, 0), (-1, -1), 0),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 0),
            ("TOPPADDING",    (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ("LINEAFTER",     (0, 0), (0, -1), 0.4, C_LIGHT_GREY),
        ]))
        story.append(row_table)

    story.append(Spacer(1, 6))
    return story


# ── Main builder ───────────────────────────────────────────────────────────────
def build_newspaper_pdf(region: str = "MY") -> bytes:
    """
    Build and return a newspaper-style PDF as bytes.
    Reads today's articles from the SQLite DB via db.get_today_articles().
    """
    # Import here so export.py can be tested standalone without the full app
    try:
        from db import get_today_articles
        articles = get_today_articles(region)
    except Exception as e:
        # Fallback: use sample data for testing
        print(f"[export] Could not load DB ({e}), using sample data.")
        articles = _sample_articles()

    if not articles:
        articles = _sample_articles()

    now = datetime.now(_MYT)
    st  = _make_styles()

    # ── Page geometry ──────────────────────────────────────────────────────────
    full_width  = PAGE_W - 2 * MARGIN
    frame_h     = PAGE_H - 2 * MARGIN - 8 * mm   # leave room for footer

    buf = BytesIO()
    doc = BaseDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=MARGIN,
        bottomMargin=MARGIN + 8 * mm,
        title=f"{PAPER_NAME} — {now.strftime('%d %B %Y')}",
        author=PAPER_NAME,
    )

    single_frame = Frame(
        MARGIN, MARGIN + 8 * mm,
        full_width, frame_h,
        id="single", leftPadding=0, rightPadding=0,
        topPadding=0, bottomPadding=0,
    )
    doc.addPageTemplates([
        PageTemplate(id="main", frames=[single_frame], onPage=_header_footer),
    ])

    # ── Group articles by category ─────────────────────────────────────────────
    grouped: dict[str, list] = defaultdict(list)
    for a in articles:
        grouped[a["category"]].append(a)
    # Sort categories by article count descending
    sorted_cats = sorted(grouped.items(), key=lambda x: -len(x[1]))

    # ── Build story ────────────────────────────────────────────────────────────
    story = []

    # Masthead
    story.extend(_build_masthead(now, len(articles), st, full_width))
    story.append(Spacer(1, 4))

    # Front-page digest (top 5 stories from biggest categories)
    top_articles = [a for cat, arts in sorted_cats for a in arts][:10]
    story.extend(_build_digest(top_articles, st))
    story.append(Spacer(1, 6))

    # Category sections
    for category, arts in sorted_cats:
        section = _build_category_section(category, arts, full_width, st)
        story.extend(section)

    # Back page note
    story.append(Spacer(1, 12))
    story.append(_thick_rule())
    story.append(Paragraph(
        f"End of edition  •  {PAPER_NAME}  •  {now.strftime('%d %B %Y')}  •  "
        f"Powered by Google News &amp; Ollama",
        st["footer"],
    ))

    doc.build(story)
    return buf.getvalue()


# ── Sample data (for standalone testing without DB) ───────────────────────────
def _sample_articles() -> list[dict]:
    return [
        {
            "title": "PM Announces New National Budget Framework for 2027",
            "category": "Local Politics",
            "published": "08:30 AM",
            "summary": "• Government plans RM400 billion allocation for next fiscal year\n• Focus on education and healthcare spending increases\n• Opposition calls for more transparency in budget process",
        },
        {
            "title": "Ringgit Strengthens Against US Dollar Amid Trade Optimism",
            "category": "Economy",
            "published": "09:15 AM",
            "summary": "• MYR trades at 4.42 against USD, best in three months\n• Analysts credit improved trade balance figures\n• BNM expected to hold rates at next policy meeting",
        },
        {
            "title": "Malaysia Wins Three Gold Medals at SEA Games Badminton",
            "category": "Sports",
            "published": "07:45 AM",
            "summary": "• National team sweeps men's singles, doubles and mixed doubles\n• Lee Zii Jia delivers dominant performance in final\n• Malaysia leads overall badminton medal tally",
        },
        {
            "title": "MOH Issues Dengue Warning as Cases Rise in Selangor",
            "category": "Public Health",
            "published": "10:00 AM",
            "summary": "• Weekly dengue cases up 18% compared to last month\n• Hotspots identified in Petaling Jaya and Shah Alam\n• Public urged to clear stagnant water around homes",
        },
        {
            "title": "Penang Tech Startup Raises RM50 Million Series B Funding",
            "category": "Technology",
            "published": "11:30 AM",
            "summary": "• Funding led by Singapore-based venture capital firm\n• Startup focuses on AI-driven supply chain solutions\n• Plans to expand operations to Indonesia and Thailand",
        },
        {
            "title": "Flash Floods Hit Parts of Kelantan After Heavy Overnight Rain",
            "category": "Environment",
            "published": "06:20 AM",
            "summary": "• Three districts affected with water levels still rising\n• Evacuation centres opened for 200 displaced residents\n• JPS monitoring river levels across affected areas",
        },
    ]


# ── CLI entry point ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    from datetime import date
    print("Building newspaper PDF…")
    pdf_bytes = build_newspaper_pdf(region="MY")
    filename  = f"malaysia_today_{date.today()}.pdf"
    with open(filename, "wb") as f:
        f.write(pdf_bytes)
    print(f"Saved: {filename}  ({len(pdf_bytes):,} bytes)")