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
from googlenewsdecoder import gnewsdecoder

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
CONFIG_FILE_ENV: str | None   = os.getenv("CONFIG_FILE")
if not CONFIG_FILE_ENV:
    raise RuntimeError("CONFIG_FILE environment variable must be set to a JSON config path.")
CONFIG_FILE: Path             = Path(CONFIG_FILE_ENV)

# ------------------------------------------------------------------
# 2. TOPIC FEATURE BANK
# ------------------------------------------------------------------

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

def load_config() -> Tuple[List[str], Dict[str, List[str]]]:
    data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    feeds = data.get("feeds")
    features = data.get("features")
    if not (isinstance(feeds, list) and isinstance(features, dict)):
        raise ValueError("Invalid config: 'feeds' must be a list and 'features' must be an object")
    return feeds, {str(k): list(v) for k, v in features.items()}

def fetch_feed(url: str):
    return feedparser.parse(url)

def article_matches(entry: dict, feature_patterns: Dict[str, List[re.Pattern]]) -> Tuple[bool, List[str]]:
    text = f"{entry.get('title', '')} {entry.get('summary', '')}"
    hits: List[str] = []
    for topic, patterns in feature_patterns.items():
        if any(p.search(text) for p in patterns):
            hits.append(topic)
    return bool(hits), hits

# -- Google News link extraction ------------------------------------

def original_link(entry: dict) -> str:
    try:
        link = entry.get("link", "")
        if "news.google." not in link:
            return link

        decoded_url = gnewsdecoder(link, interval=1)
        if decoded_url.get("status"):
            return decoded_url["decoded_url"]

        logging.error("Error:", str(decoded_url["message"]))
        return entry.get("link", "")
    except Exception as e:
        logging.error("Error decoding URL:", e)
        return entry.get("link", "")


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
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(message)s")

    if not (SLACK_WEBHOOK_URL or SLACK_BOT_TOKEN):
        raise RuntimeError("Configure SLACK_WEBHOOK_URL or bot credentials.")

    seen = load_seen()
    logging.info("Loaded %d GUIDs", len(seen))

    feeds, features = load_config()
    feature_patterns: Dict[str, List[re.Pattern]] = {k: [re.compile(p, re.I) for p in v] for k, v in features.items()}

    while True:
        try:
            try:
                new_feeds, new_features = load_config()
                feeds = new_feeds
                features = new_features
                feature_patterns = {k: [re.compile(p, re.I) for p in v] for k, v in features.items()}
            except Exception as e:
                logging.warning("Using previous config due to error: %s", e)

            for feed_url in feeds:
                try:
                    feed = fetch_feed(feed_url)
                except Exception as e:
                    logging.warning("Feed %s failed: %s", feed_url, e)
                    continue

                for entry in feed.entries:
                    guid = entry.get("id") or entry.get("link")
                    if not guid or guid in seen:
                        continue

                    matched, topics = article_matches(entry, feature_patterns)
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
