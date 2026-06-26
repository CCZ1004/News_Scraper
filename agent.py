"""
agent.py — ReAct agent that reasons over today's cached news.

The agent runs a think → act → observe loop (max 6 turns) using Ollama
as the reasoning engine. Each turn the LLM decides which tool to call;
the tool result is fed back as an observation; the loop continues until
the LLM emits a final Answer.

Tools (all read-only, grounded in today's DB):
  get_news_summary   — overview of today's articles by category
  search_articles    — find articles matching a keyword
  ask_about_topic    — deep-dive synthesis on a topic
  get_digest         — 5-bullet brief of everything today
"""

import re
import logging
from db import get_today_articles
from llm import _call

logger = logging.getLogger(__name__)

MAX_TURNS = 6

# ── Tools ─────────────────────────────────────────────────────────────────────

def _get_news_summary(region: str = "MY") -> str:
    """Return a plain-text overview of today's articles grouped by category."""
    articles = get_today_articles(region)
    if not articles:
        return "No articles available for today."

    from collections import defaultdict
    grouped = defaultdict(list)
    for a in articles:
        grouped[a["category"]].append(a["title"])

    lines = [f"Today's news — {len(articles)} articles across {len(grouped)} categories:\n"]
    for cat, titles in sorted(grouped.items(), key=lambda x: -len(x[1])):
        lines.append(f"  {cat} ({len(titles)} articles):")
        for t in titles[:3]:
            lines.append(f"    • {t}")
        if len(titles) > 3:
            lines.append(f"    … and {len(titles) - 3} more")
    return "\n".join(lines)


def _search_articles(keyword: str, region: str = "MY") -> str:
    """Return titles + summaries of articles matching the keyword."""
    if not keyword or not keyword.strip():
        return "Please provide a keyword to search for."

    keyword = keyword.strip().lower()
    articles = get_today_articles(region)
    matches = [
        a for a in articles
        if keyword in a["title"].lower()
        or keyword in a.get("summary", "").lower()
        or keyword in a.get("category", "").lower()
        or keyword in a.get("text", "").lower()
    ]

    if not matches:
        return f"No articles found matching '{keyword}'."

    lines = [f"Found {len(matches)} article(s) matching '{keyword}':\n"]
    for a in matches[:6]:
        lines.append(f"Title: {a['title']}")
        lines.append(f"Category: {a['category']}")
        summary = a.get("summary", "").strip()
        if summary and summary != "No summary available.":
            lines.append(f"Summary: {summary}")
        lines.append("")
    if len(matches) > 6:
        lines.append(f"(+ {len(matches) - 6} more matches)")
    return "\n".join(lines)


def _ask_about_topic(topic: str, region: str = "MY") -> str:
    """Synthesise an answer about a topic from today's relevant articles."""
    if not topic or not topic.strip():
        return "Please provide a topic to ask about."

    topic = topic.strip()
    articles = get_today_articles(region)

    # Find relevant articles — keyword match on topic words
    topic_words = set(topic.lower().split())
    scored = []
    for a in articles:
        haystack = f"{a['title']} {a.get('category','')} {a.get('summary','')} {a.get('text','')}".lower()
        score = sum(1 for w in topic_words if w in haystack)
        if score > 0:
            scored.append((score, a))

    scored.sort(key=lambda x: -x[0])
    relevant = [a for _, a in scored[:5]]

    if not relevant:
        return f"No articles today are relevant to '{topic}'."

    context = f"Topic: {topic}\n\nRelevant articles from today:\n\n"
    for a in relevant:
        context += f"Title: {a['title']}\n"
        context += f"Category: {a['category']}\n"
        summary = a.get("summary", "").strip()
        if summary and summary != "No summary available.":
            context += f"Summary: {summary}\n"
        context += "\n"

    region_label = "global" if region == "GLOBAL" else "Malaysian"
    prompt = f"""You are a {region_label} news analyst. Based only on the articles below, answer the question concisely in 3-5 sentences.

{context}
Question: What is happening today regarding {topic}?

Answer:"""

    answer = _call(prompt, max_tokens=200, temperature=0.3)
    return answer if answer else f"Could not synthesise an answer about '{topic}'."


def _get_digest(region: str = "MY") -> str:
    """Generate a 5-bullet daily brief from all of today's summaries."""
    articles = get_today_articles(region)
    if not articles:
        return "No articles available to digest."

    # Build a condensed context from all summaries
    summaries = []
    for a in articles:
        s = a.get("summary", "").strip()
        if s and s != "No summary available.":
            summaries.append(f"[{a['category']}] {a['title']}: {s[:150]}")

    if not summaries:
        return "No summaries available to digest."

    # Limit context to avoid token overflow
    context = "\n".join(summaries[:20])

    region_label = "global news editor" if region == "GLOBAL" else "Malaysian news editor"
    prompt = f"""You are a {region_label} writing a morning briefing.

Based on today's news summaries below, write a 5-bullet daily digest.
Each bullet should be one sentence covering a distinct major story or theme.
Start each bullet with •

Today's summaries:
{context}

Daily digest:"""

    digest = _call(prompt, max_tokens=250, temperature=0.3)
    return digest if digest else "Could not generate digest."


# Tool registry — maps name → (function, description)
TOOLS = {
    "get_news_summary": (_get_news_summary, "No arguments needed. Returns overview of today's news by category."),
    "search_articles":  (_search_articles,  "Argument: keyword (string). Searches today's articles by keyword."),
    "ask_about_topic":  (_ask_about_topic,  "Argument: topic (string). Synthesises what today's news says about a topic."),
    "get_digest":       (_get_digest,       "No arguments needed. Returns a 5-bullet summary of today's most important news."),
}


# ── System prompt ─────────────────────────────────────────────────────────────

def _system_prompt(region: str = "MY") -> str:
    tool_docs = "\n".join(
        f"  {name}: {desc}" for name, desc in TOOLS.items()
    )
    scope = "global" if region == "GLOBAL" else "Malaysian"
    return f"""You are a helpful {scope} news assistant. You have access to today's news articles and can answer questions about them.

You reason step by step using this format:

Thought: [your reasoning about what to do next]
Action: tool_name
Input: argument (or "none" if no argument needed)

After seeing the tool result (prefixed with Observation:), continue with another Thought/Action/Input or give a final answer:

Answer: [your final response to the user]

Available tools:
{tool_docs}

Rules:
- Always start with a Thought
- Only use one tool per turn
- Base answers only on the Observation results, not prior knowledge
- If a tool returns no results, say so honestly
- Give the Answer once you have enough information — do not over-call tools
- Keep answers concise and relevant to {scope} news"""


# ── ReAct loop ────────────────────────────────────────────────────────────────

_ACTION_RE = re.compile(r"Action:\s*(\w+)", re.IGNORECASE)
_INPUT_RE  = re.compile(r"Input:\s*(.+)",   re.IGNORECASE)
_ANSWER_RE = re.compile(r"Answer:\s*(.+)",  re.IGNORECASE | re.DOTALL)


def _parse_action(text: str) -> tuple[str, str]:
    """Extract (tool_name, argument) from LLM output. Returns ('', '') if not found."""
    action_m = _ACTION_RE.search(text)
    input_m  = _INPUT_RE.search(text)

    tool = action_m.group(1).strip().lower() if action_m else ""
    arg  = input_m.group(1).strip().strip('"\'') if input_m else ""

    # "none" or empty → no argument
    if arg.lower() in ("none", "n/a", ""):
        arg = ""

    return tool, arg


def _call_tool_bound(tool_name: str, arg: str, tools: dict) -> str:
    """Dispatch a tool call using the region-bound tools dict."""
    if tool_name not in tools:
        available = ", ".join(tools.keys())
        return f"Unknown tool '{tool_name}'. Available: {available}"
    fn, _ = tools[tool_name]
    try:
        # No-argument tools
        if tool_name in ("get_news_summary", "get_digest"):
            return fn()
        # Argument tools
        return fn(arg)
    except Exception as e:
        logger.error("Tool '%s' error: %s", tool_name, e)
        return f"Tool '{tool_name}' failed: {e}"


def run_agent(user_message: str, region: str = "MY") -> dict:
    """
    Run the ReAct agent loop.

    Returns:
      {
        "answer":  str,           # final answer to show the user
        "steps":   list[dict],    # thought/action/observation trail for debugging
        "turns":   int,           # how many loop turns were used
      }
    """
    if not user_message or not user_message.strip():
        return {"answer": "Please ask me something about today's news.", "steps": [], "turns": 0}

    # Bind region into every tool call via closures
    tools_bound = {
        "get_news_summary": (lambda: _get_news_summary(region),       TOOLS["get_news_summary"][1]),
        "search_articles":  (lambda kw: _search_articles(kw, region), TOOLS["search_articles"][1]),
        "ask_about_topic":  (lambda t: _ask_about_topic(t, region),   TOOLS["ask_about_topic"][1]),
        "get_digest":       (lambda: _get_digest(region),              TOOLS["get_digest"][1]),
    }

    system = _system_prompt(region)
    history = f"User: {user_message.strip()}\n\n"
    steps = []

    for turn in range(1, MAX_TURNS + 1):
        prompt = f"{system}\n\n{history}"
        raw = _call(prompt, max_tokens=300, temperature=0.2)

        if not raw:
            logger.warning("Agent got empty response on turn %d", turn)
            break

        logger.info("Agent turn %d:\n%s", turn, raw)

        # Check for final answer first
        answer_m = _ANSWER_RE.search(raw)
        if answer_m:
            answer = answer_m.group(1).strip()
            steps.append({"type": "answer", "content": answer})
            return {"answer": answer, "steps": steps, "turns": turn}

        # Parse action
        tool_name, arg = _parse_action(raw)

        if not tool_name:
            # Model produced output but neither Answer nor Action — treat as answer
            clean = raw.strip()
            steps.append({"type": "answer", "content": clean})
            return {"answer": clean, "steps": steps, "turns": turn}

        # Call the region-bound tool
        observation = _call_tool_bound(tool_name, arg, tools_bound)
        steps.append({
            "type":        "action",
            "tool":        tool_name,
            "input":       arg,
            "observation": observation,
        })

        # Append to history so next turn has context
        history += f"{raw}\nObservation: {observation}\n\n"

    # Max turns reached — ask for a final answer with what we have
    prompt = f"{system}\n\n{history}\nYou have used all available turns. Provide your best Answer now based on the observations above.\nAnswer:"
    final = _call(prompt, max_tokens=200, temperature=0.2)
    answer = final.strip() if final else "I wasn't able to find a complete answer. Please try rephrasing your question."
    steps.append({"type": "answer", "content": answer})
    return {"answer": answer, "steps": steps, "turns": MAX_TURNS}