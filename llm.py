import re
import logging as _logging
import requests
from difflib import SequenceMatcher

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "qwen2.5:latest"

_llm_logger = _logging.getLogger(__name__)

ICON_RULES = [
    (["health", "medical", "hospital", "disease", "covid", "drug"],        "🏥"),
    (["politic", "government", "parliament", "minister", "election"],       "🏛️"),
    (["economy", "gdp", "inflation", "ringgit", "finance", "budget"],       "📈"),
    (["crime", "court", "law", "police", "murder", "fraud", "jail"],        "⚖️"),
    (["sport", "football", "badminton", "tennis", "olympic", "match"],      "🏆"),
    (["tech", "ai", "digital", "cyber", "software", "startup"],             "💻"),
    (["environment", "climate", "flood", "forest", "pollution"],            "🌿"),
    (["education", "school", "university", "student", "exam"],              "🎓"),
    (["foreign", "diplomat", "asean", "international", "trade"],            "🌏"),
    (["transport", "train", "highway", "aviation", "road", "mrt"],          "🚆"),
    (["religion", "islam", "church", "temple", "mosque"],                   "🕌"),
    (["business", "company", "market", "stock", "corporate"],               "💼"),
    (["social", "welfare", "poverty", "housing", "community"],              "🤝"),
    (["labour", "worker", "wage", "employment", "union"],                   "👷"),
    (["entertainment", "film", "music", "celebrity", "arts"],               "🎬"),
]
DEFAULT_ICON = "📰"

CANONICAL = {
    "economics":  "Economy",
    "economic":   "Economy",
    "politics":   "Local Politics",
    "political":  "Local Politics",
    "healthcare": "Public Health",
    "tech":       "Technology",
    "it":         "Technology",
    "law":        "Crime & Law",
    "legal":      "Crime & Law",
    "sports":     "Sports",
    "sport":      "Sports",
}

_CATEGORY_RE      = re.compile(r"^CATEGORY\s*:\s*(.+)",  re.IGNORECASE | re.MULTILINE)
_SUMMARY_HEADER_RE = re.compile(r"SUMMARY\s*:",           re.IGNORECASE)
_BULLET_RE         = re.compile(r"^[\•\-\*\u2022]|\d+[\.\)]\s+", re.MULTILINE)
_PLACEHOLDER_RE    = re.compile(r"<[^>]+>|\[[^\]]{3,}\]")  # <...> and [...]
_FORMAT_DESC_RE    = re.compile(r"\d+-\d+")                 # e.g. "2-3" in placeholders


def _call(prompt: str, max_tokens: int = 100, temperature: float = 0.2,
          retries: int = 1) -> str:
    """Call Ollama with retry on timeout. Returns empty string on total failure."""
    payload = {
        "model":   MODEL,
        "prompt":  prompt,
        "stream":  False,
        "options": {"temperature": temperature, "num_predict": max_tokens},
    }
    for attempt in range(1, retries + 2):
        try:
            response = requests.post(OLLAMA_URL, json=payload, timeout=45)
            response.raise_for_status()
            return response.json().get("response", "").strip()
        except requests.exceptions.Timeout:
            _llm_logger.warning(
                "Ollama timeout (attempt %d/%d)", attempt, retries + 1
            )
            if attempt > retries:
                return ""
        except Exception as e:
            _llm_logger.error("Ollama error: %s", e)
            return ""
    return ""


def icon_for(category: str) -> str:
    label = category.lower()
    for keywords, icon in ICON_RULES:
        if any(k in label for k in keywords):
            return icon
    return DEFAULT_ICON


def normalise(raw: str, seen: list[str], threshold: float = 0.82) -> str:
    """
    Collapse near-duplicate category labels.
    `seen` should be pre-sorted by the caller for deterministic results.
    """
    raw = raw.strip().strip('"\'.,')
    if raw.lower() in CANONICAL:
        raw = CANONICAL[raw.lower()]
    for existing in seen:
        if SequenceMatcher(None, raw.lower(), existing.lower()).ratio() >= threshold:
            return existing
    return raw


def _extract_summary(raw: str) -> str:
    """
    Three-strategy summary extractor — handles all real Llama response patterns:
      1. Content after SUMMARY: header (strips placeholders, checks for real words)
      2. Any bullet/numbered lines in the full response
      3. All non-CATEGORY prose as last resort
    """
    # Strategy 1
    sm = _SUMMARY_HEADER_RE.search(raw)
    if sm:
        after = _PLACEHOLDER_RE.sub("", raw[sm.end():]).strip()
        if after and re.search(r"[a-zA-Z]{3,}", after):
            return after

    # Strategy 2
    bullet_lines = [
        l.strip() for l in raw.splitlines()
        if _BULLET_RE.match(l.strip()) and not _PLACEHOLDER_RE.search(l)
    ]
    if bullet_lines:
        return "\n".join(bullet_lines)

    # Strategy 3
    prose = _PLACEHOLDER_RE.sub("", _CATEGORY_RE.sub("", raw)).strip()
    if prose and re.search(r"[a-zA-Z]{3,}", prose):
        return prose

    return "No summary available."


def analyse(title: str, text: str) -> dict:
    """Single LLM call → {'category': str, 'summary': str}."""
    text  = (text  or "").strip()
    title = (title or "").strip()

    if len(text) >= 200:
        article_block = f"Article:\n{text[:600]}"
        bullet_count  = "Write exactly 3 bullet points."
    elif len(text) >= 50:
        article_block = f"Snippet:\n{text[:600]}"
        bullet_count  = "Write exactly 2 bullet points."
    else:
        article_block = ""
        bullet_count  = "Write exactly 2 bullet points based only on the headline."

    prompt = f"""You are a Malaysian news editor. Read the content below and respond in this exact format:

CATEGORY: [2-3 word label, e.g. Local Politics / Economy / Crime & Law / Public Health / Education / Environment / Sports / Technology / Business / Foreign Affairs / Transport / Religion / Entertainment / Social Issues / Labour Rights]
SUMMARY:
• [first key point]
• [second key point]
• [third key point]

Rules:
- Each bullet point is one sentence, max 20 words
- Do not copy the format placeholders above — write real content
- {bullet_count}
- If only a headline is given, infer likely context from it

Headline: {title}
{article_block}"""

    raw = _call(prompt, max_tokens=100)

    # Parse category
    category = "General"
    m = _CATEGORY_RE.search(raw)
    if m:
        val = m.group(1).strip().strip('"\'.,[]')
        is_placeholder = bool(_PLACEHOLDER_RE.search(val) or _FORMAT_DESC_RE.search(val))
        if val and not is_placeholder:
            category = val

    return {"category": category, "summary": _extract_summary(raw)}


# Backward-compat shims
def classify(title: str, text: str = "") -> str:
    return analyse(title, text or title)["category"]

def summarize(text: str) -> str:
    return analyse(text[:80], text)["summary"]