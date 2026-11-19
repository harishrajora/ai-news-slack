# AI news to Slack

Posts new announcements from AI companies, and major AI stories from Hacker News, to the `#ai-news` Slack channel.

## How it works

- `.github/workflows/post-news.yml` runs every 30 minutes. It reads every source in `config.json`, posts new items to a Slack Workflow Builder webhook, and saves what it has already posted in `state.json`.
- The first time it sees a source, it only remembers that source's existing items and posts nothing, so the channel doesn't get flooded.
- It skips repeats: an item is not posted if a story with the same link or a near-identical title went out in the last 7 days. It also ignores items older than 3 days.
- It posts at most `max_posts_per_run` items per run. Anything over that waits for the next run.
- `.github/workflows/feed-health.yml` runs once a day. It fails if a feed can't be fetched or has posted nothing for longer than its `max_silence_days`. GitHub emails you when it fails.

## Setup

1. In Slack Workflow Builder, create a workflow that starts with a webhook, with three Text variables (`source`, `title`, `url`), and add a step that sends a message to the channel. Publish it and copy the webhook link.
2. Add that link to this repo as a secret: Settings → Secrets and variables → Actions → New repository secret, named `SLACK_WEBHOOK_URL`.
3. Go to Actions → "Post AI news to Slack" → Run workflow. The first run only remembers the existing items. New items are posted from the second run onwards.

## Changing things

Everything is in `config.json`:

- Add or remove feeds.
- Change a feed's silence limit.
- Change the Hacker News `min_points` (for example from 300 to 500) or its keywords.

To test locally without posting: `DRY_RUN=1 python3 news.py`. To run the health check: `python3 news.py --health`.
