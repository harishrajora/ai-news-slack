"""Post new AI news items from RSS/Atom feeds and Hacker News to a Slack workflow webhook.

Usage:
  python3 news.py            # check feeds, post new items, update state.json
  python3 news.py --health   # report feeds that fail or have gone quiet (exit 1 if any)

Environment:
  SLACK_WEBHOOK_URL  Slack Workflow Builder webhook (required unless DRY_RUN=1)
  DRY_RUN=1          print what would be posted; do not post or save state
"""

import difflib
import html
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
SUMMARY_MAX = 280
NO_SUMMARY = "Open the link for details."


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


def clean_text(fragment):
    fragment = re.sub(r"</h[1-6]>", ": ", fragment, flags=re.I)  # "Heading: text", not "Heading text"
    text = html.unescape(re.sub(r"<[^>]+>", " ", fragment))
    text = re.sub(r"\s+:", ":", text)
    return re.sub(r"\s+", " ", text).strip()


def make_summary(el, title):
    """Short summary from the feed's own description, or "" if it adds nothing."""
    raw = child_text(el, "description", "summary") or child_text(el, "content", "encoded")
    bullets = [clean_text(li) for li in re.findall(r"<li[^>]*>(.*?)</li>", raw, re.S | re.I)]
    bullets = [b for b in bullets if b]
    if len(bullets) >= 2:  # release notes: keep the first few changes
        text = " • ".join(bullets[:3]) + (" • …" if len(bullets) > 3 else "")
    else:
        text = clean_text(raw)
    text = re.sub(r"\s*The post .* appeared first on .*$", "", text)  # WordPress boilerplate
    if len(text) > SUMMARY_MAX:
        text = text[:SUMMARY_MAX].rsplit(" ", 1)[0].rstrip(",;:.") + "…"
    t, s = norm_title(title), norm_title(text)
    if len(text) < 40 or not s or s in t or t in s:
        return ""  # too short, or just repeats the title
    return text


def parse_feed(data):
    """Return a list of {id, title, url, date, summary} from RSS or Atom bytes."""
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
            items.append({"id": item_id, "title": title, "url": link, "date": date,
                          "summary": make_summary(el, title)})
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
        discussion = f"https://news.ycombinator.com/item?id={h['objectID']}"
        items.append({
            "id": h["objectID"],
            "title": title,
            "url": h.get("url") or discussion,
            "date": datetime.fromtimestamp(h["created_at_i"], timezone.utc),
            "summary": f"{h['points']} points and {h.get('num_comments') or 0} comments on Hacker News. "
                       f"Discussion: {discussion}",
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
    # Keep the #fragment: changelogs give every entry the same page with a different anchor.
    key = p.netloc.lower().removeprefix("www.") + p.path.rstrip("/")
    return (key + ("#" + p.fragment if p.fragment else "")).lower()


def norm_title(title):
    return re.sub(r"[^a-z0-9 ]", "", title.lower()).strip()


def is_duplicate(item, recent):
    u, t = norm_url(item["url"]), norm_title(item["title"])
    for r in recent:
        if r["url"] == u or difflib.SequenceMatcher(None, r["title"], t).ratio() > 0.85:
            return True
    return False


def post(source, item):
    body = json.dumps({
        "source": source,
        "title": item["title"],
        "url": item["url"],
        "summary": item["summary"] or NO_SUMMARY,  # Slack workflow variables can't be empty
    }).encode()
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
    queues = {}  # source -> new items, oldest first

    for name, items, error in all_sources():
        if error:
            print(f"WARN {name}: {error}")
            continue
        current = [i["id"] for i in items]
        if name not in state["seen"]:  # first time we see this feed: remember everything, post nothing
            state["seen"][name] = current
            print(f"seeded {name} with {len(items)} items")
            continue
        seen_set = set(state["seen"][name])
        new = [i for i in items if i["id"] not in seen_set
               and not (i["date"] and i["date"] < cutoff)]  # old items resurfacing are skipped quietly
        new.sort(key=lambda i: i["date"] or NOW)
        if new:
            queues[name] = new
        # Remember every item in the feed except the ones still waiting to be posted.
        waiting = {i["id"] for i in new}
        state["seen"][name] = [x for x in current if x not in waiting]

    # Take turns between sources so one busy feed can't use up every slot:
    # the source that posted least recently goes first.
    last_post = state.setdefault("last_post", {})
    posted, failed = 0, False
    while queues and posted < CONFIG["max_posts_per_run"] and not failed:
        for name in sorted(queues, key=lambda n: last_post.get(n, 0)):
            if posted >= CONFIG["max_posts_per_run"]:
                break
            item = queues[name].pop(0)
            if not queues[name]:
                del queues[name]
            if is_duplicate(item, state["recent"]):
                print(f"dup  {name}: {item['title']}")
            else:
                print(f"post {name}: {item['title']} {item['url']}\n     {item['summary'] or NO_SUMMARY}")
                if not DRY_RUN:
                    try:
                        post(name, item)
                    except Exception as e:  # stop, but still save what was already posted
                        print(f"ERROR posting to Slack: {type(e).__name__}: {e}")
                        failed = True
                        break
                    time.sleep(2)
                posted += 1
                last_post[name] = NOW.timestamp()
                state["recent"].append({"url": norm_url(item["url"]),
                                        "title": norm_title(item["title"]), "ts": NOW.timestamp()})
            state["seen"][name].insert(0, item["id"])

    left = sum(len(q) for q in queues.values())
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
