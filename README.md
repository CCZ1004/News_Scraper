# News_Scraper
readme_content = """# 📰 Malaysia Today & Global News Broadcaster

An automated, AI-powered daily newspaper compiler and interactive conversational news assistant. The application automatically scrapes today's leading news, filters duplicates, uses a local Large Language Model (Ollama) to extract key bullet points and classify categories, persists them into a thread-safe SQLite cache, compiles a professional broadsheet-style PDF, and provides a ReAct (Reasoning + Acting) chat agent for deep-dive news synthesis.

---

## 🏗️ Architecture & Component Flow

1. **`app.py` (Flask Web Interface & Background Worker)**: The main entry point. It serves an interactive dashboard, coordinates loading states, exposes endpoints for manual cache refreshes, and runs an asynchronous background thread (`_ensure_job`) to handle non-blocking live news parsing.
2. **`scraper.py` (Two-Phase Extractor)**:
   * **Phase 1 (Fetch & Parse)**: Pulls headlines from Google News RSS. It bypasses aggressive paywalls and scraping bottlenecks by resolving final URLs and mapping them against a custom dictionary of raw Malaysian media outlet RSS feeds (*The Star*, *Malay Mail*, *Bernama*, *FMT*, etc.) to extract clean `<content:encoded>` text before falling back to `trafilatura` HTML body extraction.
   * **Phase 2 (Deduplication & LLM Analysis)**: Uses string similarity algorithms (`SequenceMatcher`) to discard duplicate or overlapping headlines, then distributes unique records across a thread pool to be processed by the LLM.
3. **`llm.py` (Ollama & Category Normalizer)**: Connects to a local Ollama instance running `qwen2.5:latest`. It handles structured prompt extraction (yielding precise, 3-bullet summaries limited to 20 words per sentence) and resolves diverse variations into standardized, canonical categories with custom emoji badges.
4. **`db.py` (SQLite Persistence Shield)**: Manages a local SQLite database configured with **WAL (Write-Ahead Logging) mode** for optimal multi-threaded consistency. It acts as an instant caching barrier, ensuring that identical news URLs are not analyzed twice in the same calendar day.
5. **`agent.py` (ReAct Chat Engine)**: Implements an autonomous **Think ➔ Act ➔ Observe** agent loop. When asked a complex or multi-article synthesis question, the agent leverages tools (`get_news_summary`, `search_articles`, `ask_about_topic`, `get_digest`) to gather context before formulating answers.
6. **`export.py` (Broadsheet PDF Compiler)**: Uses ReportLab layout engine to structure a classic multi-column newspaper layout complete with an automated editorial masthead, customized dark-red category banner pills, thin rules, and page geometries optimized for standard A4 printing.

---

## 🛠️ Prerequisites & Installation

### 1. Set Up the Local LLM (Ollama)
This project relies on a local installation of Ollama for cost-effective, private document processing.
1. Download and install Ollama from [ollama.com](https://ollama.com/).
2. Pull the required model via your terminal:
