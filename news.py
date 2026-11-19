"""Post new AI news items from RSS/Atom feeds and Hacker News to a Slack workflow webhook.

Usage:
  python3 news.py            # check feeds, post new items, update state.json
  python3 news.py --health   # report feeds that fail or have gone quiet (exit 1 if any)

Environment:
  SLACK_WEBHOOK_URL  Slack Workflow Builder webhook (required unless DRY_RUN=1)
  DRY_RUN=1          print what would be posted; do not post or save state
"""

import difflib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "config.json").read_text())
STATE_FILE = ROOT / "state.json"
USER_AGENT = "Mozilla/5.0 (compatible; ai-news-feed/1.0)"
NOW = datetime.now(timezone.utc)
DRY_RUN = os.environ.get("DRY_RUN") == "1"


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def local(tag):
    return tag.rsplit("}", 1)[-1].lower()


def child_text(el, *names):
    for c in el:
        if local(c.tag) in names and (c.text or "").strip():
            return c.text.strip()
    return ""


def parse_date(text):
    if not text:
        return None
    try:
        d = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def parse_feed(data):
    """Return a list of {id, title, url, date} from RSS or Atom bytes."""
    root = ET.fromstring(data)
    items = []
    for el in root.iter():
        if local(el.tag) not in ("item", "entry"):
            continue
        link = child_text(el, "link")
        if not link:  # Atom: <link href="..." rel="alternate"/>
            for c in el:
                if local(c.tag) == "link" and c.get("rel", "alternate") == "alternate" and c.get("href"):
                    link = c.get("href")
                    break
        title = re.sub(r"\s+", " ", child_text(el, "title"))
        date = parse_date(child_text(el, "pubdate", "published", "updated", "date"))
        item_id = child_text(el, "guid", "id") or link or title
        if title and link:
            items.append({"id": item_id, "title": title, "url": link, "date": date})
    return items


def hacker_news_items():
    hn = CONFIG["hacker_news"]
    since = int((NOW - timedelta(days=3)).timestamp())
    q = urllib.parse.urlencode({
        "tags": "story",
        "numericFilters": f"points>={hn['min_points']},created_at_i>{since}",
        "hitsPerPage": 500,
    })
    data = json.loads(fetch("https://hn.algolia.com/api/v1/search_by_date?" + q))
    pattern = re.compile(r"\b(" + "|".join(re.escape(k) for k in hn["keywords"]) + r")\b", re.I)
    items = []
    for h in data["hits"]:
        title = h.get("title") or ""
        if not pattern.search(title):
            continue
        items.append({
            "id": h["objectID"],
            "title": title,
            "url": h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}",
            "date": datetime.fromtimestamp(h["created_at_i"], timezone.utc),
        })
    return items


def all_sources():
    """Yield (name, items or None, error)."""
    for feed in CONFIG["feeds"]:
        try:
            yield feed["name"], parse_feed(fetch(feed["url"])), None
        except Exception as e:  # one broken feed must not stop the others
            yield feed["name"], None, f"{type(e).__name__}: {e}"
    try:
        yield "Hacker News", hacker_news_items(), None
    except Exception as e:
        yield "Hacker News", None, f"{type(e).__name__}: {e}"


def norm_url(url):
    p = urllib.parse.urlsplit(url)
    return (p.netloc.lower().removeprefix("www.") + p.path.rstrip("/")).lower()


def norm_title(title):
    return re.sub(r"[^a-z0-9 ]", "", title.lower()).strip()


def is_duplicate(item, recent):
    u, t = norm_url(item["url"]), norm_title(item["title"])
    for r in recent:
        if r["url"] == u or difflib.SequenceMatcher(None, r["title"], t).ratio() > 0.85:
            return True
    return False


def post(source, item):
    body = json.dumps({"source": source, "title": item["title"], "url": item["url"]}).encode()
    req = urllib.request.Request(
        os.environ["SLACK_WEBHOOK_URL"], data=body,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


def run():
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {"seen": {}, "recent": []}
    cutoff = NOW - timedelta(days=CONFIG["max_item_age_days"])
    state["recent"] = [r for r in state["recent"] if r["ts"] > (NOW - timedelta(days=7)).timestamp()]
    pending = []

    for name, items, error in all_sources():
        if error:
            print(f"WARN {name}: {error}")
            continue
        seen = state["seen"].get(name)
        if seen is None:  # first time we see this feed: remember everything, post nothing
            state["seen"][name] = [i["id"] for i in items][:500]
            print(f"seeded {name} with {len(items)} items")
            continue
        seen_set = set(seen)
        for i in items:
            if i["id"] in seen_set:
                continue
            if i["date"] and i["date"] < cutoff:  # old item resurfacing; skip quietly
                seen.insert(0, i["id"])
                continue
            pending.append((name, i))

    pending.sort(key=lambda p: p[1]["date"] or NOW)
    posted = 0
    for name, item in pending:
        if posted >= CONFIG["max_posts_per_run"]:
            break  # leave the rest unseen; they go out next run
        seen = state["seen"][name]
        if is_duplicate(item, state["recent"]):
            print(f"dup  {name}: {item['title']}")
        else:
            print(f"post {name}: {item['title']} {item['url']}")
            if not DRY_RUN:
                try:
                    post(name, item)
                except Exception as e:  # stop, but still save what was already posted
                    print(f"ERROR posting to Slack: {type(e).__name__}: {e}")
                    break
                time.sleep(2)
            posted += 1
            state["recent"].append({"url": norm_url(item["url"]),
                                    "title": norm_title(item["title"]), "ts": NOW.timestamp()})
        seen.insert(0, item["id"])
        del seen[500:]

    left = len(pending) - sum(1 for n, i in pending if i["id"] in state["seen"][n])
    print(f"posted {posted}, waiting for next run {left}")
    if not DRY_RUN:
        STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n")


def health():
    limits = {f["name"]: f["max_silence_days"] for f in CONFIG["feeds"]}
    problems = []
    for name, items, error in all_sources():
        if error:
            problems.append(f"{name}: cannot fetch/parse ({error})")
            continue
        if name == "Hacker News":
            print(f"ok   {name}: {len(items)} matching stories in the last 3 days")
            continue
        dates = [i["date"] for i in items if i["date"]]
        if not dates:
            problems.append(f"{name}: no dated items in feed")
            continue
        newest = max(dates)
        age = (NOW - newest).days
        status = "ok  " if age <= limits[name] else "STALE"
        print(f"{status} {name}: newest item {newest:%Y-%m-%d} ({age} days, limit {limits[name]})")
        if age > limits[name]:
            problems.append(f"{name}: no new items for {age} days (limit {limits[name]})")
    if problems:
        print("\nProblems:\n- " + "\n- ".join(problems))
        sys.exit(1)
    print("\nAll feeds healthy.")


if __name__ == "__main__":
    health() if "--health" in sys.argv else run()
