"""
news_monitor.py – Real-time surveillance-news watcher
---------------------------------------------------
Watches a list of news RSS/Atom feeds, scores fresh items for
surveillance-camera / ALPR / AI-policing themes, and posts matching links
into a Slack channel.  One message per link with rich preview, title,
and topic tags.

Google News quirk: RSS items point back to *news.google.com* redirection
URLs.  We now strip those and replace them with the canonical source
URL to avoid extra hops and preview glitches.

Two delivery modes:
  1. **Incoming Webhook** – set `SLACK_WEBHOOK_URL`.
  2. **Bot token via chat.postMessage** – set `SLACK_BOT_TOKEN` *and*
     `SLACK_CHANNEL_ID` (guaranteed link unfurls).

Python 3.11+.  Required libs:
  pip install feedparser requests python-dotenv
"""
from __future__ import annotations
import os
import re
import json
import time
import logging
from pathlib import Path
from typing import List, Dict, Tuple
import sys
from urllib.parse import urlparse, parse_qs, unquote

from dotenv import load_dotenv
import feedparser
import requests

# UTF-8 stdout for logs
sys.stdout.reconfigure(encoding="utf-8")

# ------------------------------------------------------------------
# 1. CONFIGURATION
# ------------------------------------------------------------------
load_dotenv()

SLACK_WEBHOOK_URL: str | None = os.getenv("SLACK_WEBHOOK_URL")
SLACK_BOT_TOKEN: str | None   = os.getenv("SLACK_BOT_TOKEN")
SLACK_CHANNEL_ID: str | None  = os.getenv("SLACK_CHANNEL_ID")
SLACK_USERNAME: str           = os.getenv("SLACK_USERNAME", "news-bot")
SLACK_ICON_EMOJI: str         = os.getenv("SLACK_ICON_EMOJI", ":police_car:")
POLL_INTERVAL: int            = int(os.getenv("POLL_INTERVAL", 300))
SEEN_FILE: Path               = Path(os.getenv("SEEN_FILE", "seen.json"))

FEEDS: List[str] = [
    "https://www.404media.co/feed",
    "https://www.theguardian.com/us/rss",
    "https://feeds.feedburner.com/policeone/all",
    "https://www.govtech.com/rss",
    "https://qz.com/feed",
    "https://knowridge.com/feed/",
    "https://www.ground.news/rss",
    "https://news.google.com/rss/search?q=surveillance+OR+ALPR+OR+AI+law+enforcement&hl=en-US&gl=US&ceid=US:en",
]

# ------------------------------------------------------------------
# 2. TOPIC FEATURE BANK
# ------------------------------------------------------------------
FEATURES: Dict[str, List[str]] = {
    "camera_surveillance": [
        r"\bsurveillance\s+cameras?\b",
        r"public\s+safety\s+cameras?",
        r"camera\s+network",
        r"video\s+analytics",
        r"cctv", r"closed\s*circuit\s+television",
    ],
    "alpr": [
        r"\balpr\b",
        r"automatic\s+license\s+plate",
        r"license\s+plate\s+(reader|recognition)",
        r"plate\s+reader",
        r"flock",
        #r"flock\s+safety",
        #r"flock\s+nova",
    ],
    "ai_policing": [
        r"ai-enabled\s+(camera|surveillance|polic(ing|e))",
        r"machine\s+learning\s+(camera|surveillance|polic(ing|e))",
        r"predictive\s+policing",
        r"algorithmic\s+policing",
        r"\bAI\b[^\n]{0,60}?law\s+enforcement",
        r"law\s+enforcement[^\n]{0,60}?\bAI\b",
    ],
}
FEATURE_PATTERNS = {k: [re.compile(p, re.I) for p in v] for k, v in FEATURES.items()}

# ------------------------------------------------------------------
# 3. UTILITIES
# ------------------------------------------------------------------

def load_seen() -> set[str]:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()))
        except Exception:
            logging.warning("Seen file unreadable; starting fresh.")
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

# -- Google News link extraction ------------------------------------

def original_link(entry: dict) -> str:
    """Return canonical link, stripping Google News redirect when needed."""
    link = entry.get("link", "")
    if "news.google." not in link:
        return link

    # First try to extract the full article URL from the query string parameter 'url'
    parsed = urlparse(link)
    qs = parse_qs(parsed.query)
    if "url" in qs:
        return unquote(qs["url"][0])

    # Next, try to extract the URL embedded after the last comma
    if "," in link:
        tail = link.rsplit(",", 1)[-1]
        if tail.startswith("https://"):
            return tail

    # Finally, fall back to the 'source' element if provided and appears to be an article URL
    src = entry.get("source")
    if src and isinstance(src, dict) and src.get("href"):
        candidate = src["href"]
        parsed_candidate = urlparse(candidate)
        # If the candidate URL has a nontrivial path, assume it's the full article URL
        if parsed_candidate.path not in ["", "/"]:
            return candidate

    return link  # fallback

# ------------------------------------------------------------------
# 4. SLACK POSTER
# ------------------------------------------------------------------

def post_via_webhook(message: str):
    if not SLACK_WEBHOOK_URL:
        raise RuntimeError("SLACK_WEBHOOK_URL not set; cannot use webhook mode.")
    payload = {
        "username": SLACK_USERNAME,
        "icon_emoji": SLACK_ICON_EMOJI,
        "text": message,
        "unfurl_links": True,
        "unfurl_media": True,
    }
    r = requests.post(
        SLACK_WEBHOOK_URL,
        headers={"Content-Type": "application/json"},
        data=json.dumps(payload).encode(),
        timeout=10,
    )
    r.raise_for_status()

def post_via_bot_token(message: str):
    if not (SLACK_BOT_TOKEN and SLACK_CHANNEL_ID):
        raise RuntimeError("Bot token mode needs SLACK_BOT_TOKEN & SLACK_CHANNEL_ID.")
    payload = {
        "channel": SLACK_CHANNEL_ID,
        "text": message,
        "unfurl_links": True,
        "unfurl_media": True,
        "username": SLACK_USERNAME,
        "icon_emoji": SLACK_ICON_EMOJI,
    }
    r = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={
            "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
            "Content-Type": "application/json;charset=utf-8",
        },
        data=json.dumps(payload).encode(),
        timeout=10,
    )
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack API error: {data.get('error')}")

POST = post_via_bot_token if SLACK_BOT_TOKEN else post_via_webhook

# ------------------------------------------------------------------
# 5. MAIN LOOP
# ------------------------------------------------------------------

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if not (SLACK_WEBHOOK_URL or SLACK_BOT_TOKEN):
        raise RuntimeError("Configure SLACK_WEBHOOK_URL or bot credentials.")

    seen = load_seen()
    logging.info("Loaded %d GUIDs", len(seen))

    while True:
        try:
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
                    if not matched:
                        seen.add(guid)
                        continue

                    title = entry.get("title", "(no title)")
                    link = original_link(entry)
                    logging.info("Found: %s (%s)", title, link)

                    message = f"*{title}*\n{link}\n• topics: {', '.join(topics)}"
                    try:
                        POST(message)
                        logging.info("Posted: %s", title)
                    except Exception as e:
                        logging.error("Slack post failed: %s", e)
                        continue  # retry next loop

                    seen.add(guid)

            save_seen(seen)
        except Exception as e:
            logging.error("Unhandled exception in main loop: %s", e)

        try:
            time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            break


if __name__ == "__main__":
    main()
