"""
news_monitor.py – Real‑time surveillance‑news watcher
---------------------------------------------------
Watches a list of news RSS/Atom feeds, scores fresh items for
surveillance‑camera / ALPR / AI‑policing themes, and posts matching links
into a Slack channel via Incoming Webhook.

Python-3.11+.  No DB.  No cloud.  Drop it on a box, give it a webhook,
run it with systemd or cron.
"""
from __future__ import annotations
import os
import re
import json
import time
import logging
from pathlib import Path
from typing import List, Dict, Tuple

import feedparser   # pip install feedparser
import requests     # pip install requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# 1. Configuration
# ---------------------------------------------------------------------------
# Slack Webhook – create one in Slack → Apps & Integrations → Incoming Webhooks
load_dotenv()  # Loads variables from a .env file into process environment

SLACK_WEBHOOK_URL: str | None = os.getenv("SLACK_WEBHOOK_URL")  # required
SLACK_USERNAME: str = os.getenv("SLACK_USERNAME", "newsstand")
SLACK_ICON_EMOJI: str = os.getenv("SLACK_ICON_EMOJI", ":newspaper:")
POLL_INTERVAL: int = int(os.getenv("POLL_INTERVAL", 300))  # seconds
SEEN_FILE: Path = Path(os.getenv("SEEN_FILE", "seen.json"))

# News feeds likely to carry policing‑tech & surveillance stories
FEEDS: List[str] = [
    "https://www.404media.co/feed",                  # 404 Media (tech & policy)
    "https://www.theguardian.com/us/rss",            # Guardian US
    "https://feeds.feedburner.com/policeone/all",    # Police1
    "https://www.govtech.com/rss",                   # Government Technology
    "https://qz.com/feed",                           # Quartz
    "https://knowridge.com/feed/",                   # Knowridge (sci/tech)
]

# ---------------------------------------------------------------------
# 2. TOPIC FEATURE BANK – *focus narrowed as requested*
# ---------------------------------------------------------------------
FEATURES: Dict[str, List[str]] = {
    # Surveillance cameras in general
    "camera_surveillance": [
        r"\bsurveillance\s+cameras?\b",
        r"public\s+safety\s+cameras?",
        r"camera\s+network",
        r"video\s+analytics",
        r"cctv", r"closed\s*circuit\s+television",
    ],
    # Automatic/AI license‑plate recognition
    "alpr": [
        r"\balpr\b",
        r"automatic\s+license\s+plate",
        r"license\s+plate\s+(reader|recognition)",
        r"plate\s+reader",
        r"flock",
        #r"flock\s+safety",
        #r"flock\s+nova",
    ],
    # AI or ML explicitly tied to policing / investigations
    "ai_policing": [
        r"ai‑enabled\s+(camera|surveillance|polic(ing|e))",
        r"machine\s+learning\s+(camera|surveillance|polic(ing|e))",
        r"predictive\s+policing",
        r"algorithmic\s+policing",
        r"\bAI\b[^\n]{0,60}?law\s+enforcement",
        r"law\s+enforcement[^\n]{0,60}?\bAI\b",
    ],
}

FEATURE_PATTERNS: Dict[str, List[re.Pattern]] = {
    k: [re.compile(p, re.I) for p in pats] for k, pats in FEATURES.items()
}

# ---------------------------------------------------------------------
# 3. UTILITIES
# ---------------------------------------------------------------------

def load_seen() -> set[str]:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()))
        except Exception:
            logging.warning("Seen‑file unreadable; starting fresh.")
    return set()

def save_seen(seen: set[str]):
    SEEN_FILE.write_text(json.dumps(sorted(seen)))

def fetch_feed(url: str):
    return feedparser.parse(url)

def article_matches(entry: dict) -> Tuple[bool, List[str]]:
    text = f"{entry.get('title', '')} {entry.get('summary', '')}"
    hits: List[str] = []
    for topic, patterns in FEATURE_PATTERNS.items():
        if any(p.search(text) for p in patterns):
            hits.append(topic)
    return bool(hits), hits

def post_to_slack(message: str):
    payload = {"username": SLACK_USERNAME, "icon_emoji": SLACK_ICON_EMOJI, "text": message}
    r = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=10)
    r.raise_for_status()

# ---------------------------------------------------------------------
# 4. MAIN LOOP
# ---------------------------------------------------------------------

def main():
    if not SLACK_WEBHOOK_URL:
        raise RuntimeError("SLACK_WEBHOOK_URL env var not set – cannot post to Slack.")

    seen = load_seen()
    logging.info("Loaded %d GUIDs", len(seen))

    while True:
        for feed_url in FEEDS:
            try:
                feed = fetch_feed(feed_url)
            except Exception as e:
                logging.warning("Feed %s failed: %s", feed_url, e)
                continue

            for entry in feed.entries:
                guid = entry.get("id") or entry.get("link")
                if not guid or guid in seen:
                    continue

                matched, topics = article_matches(entry)
                if matched:
                    title = entry.get("title", "(no title)")
                    link = entry.get("link", "")
                    slack_msg = f"*{title}*\n{link}\n• topics: {', '.join(topics)}"
                    try:
                        post_to_slack(slack_msg)
                        logging.info("Posted: %s", title)
                    except Exception as e:
                        logging.error("Slack post failed: %s", e)
                        continue  # don’t mark; retry later

                seen.add(guid)
        save_seen(seen)
        try:
            time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            logging.info("Shutting down.")
            break


if __name__ == "__main__":
    main()
