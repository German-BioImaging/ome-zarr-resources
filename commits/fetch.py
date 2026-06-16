#!/usr/bin/env python3
"""Fetch OME-Zarr commits from the GitHub commit search API.

Incremental: reads the existing data.json, finds the newest commit date it
already has, and only queries commits authored after that. New commits are
merged into the full historical set — nothing older is discarded.

Each commit is stored flat with: commit_date, author, repository, commit_url.

Run locally:
    export GITHUB_TOKEN=ghp_xxx
    python scripts/fetch.py
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

QUERY = '"OME-Zarr"'
DATA_FILE = Path("data.json")
RECENT_DAYS = 10  # how many days the page shows; full history is kept in the file
API = "https://api.github.com/search/commits"

TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
if not TOKEN:
    sys.exit("Set GITHUB_TOKEN (or GH_TOKEN) in your environment.")

HEADERS = {
    "Accept": "application/vnd.github+json",
    "Authorization": f"Bearer {TOKEN}",
    "User-Agent": "ome-zarr-commits-feed",
    "X-GitHub-Api-Version": "2022-11-28",
}


def load_existing() -> tuple[list[dict], str | None]:
    """Return (commits, newest_iso_date) from data.json if present."""
    if not DATA_FILE.exists():
        return [], None
    try:
        data = json.loads(DATA_FILE.read_text())
    except json.JSONDecodeError:
        return [], None
    commits = data.get("commits", [])
    newest = max((c["commit_date"] for c in commits), default=None)
    return commits, newest


def build_query(since_iso: str | None) -> str:
    q = QUERY
    if since_iso:
        # author-date qualifier; '>' is exclusive enough with dedup by URL
        day = since_iso[:10]
        q = f"{QUERY} author-date:>={day}"
    return q


def fetch(since_iso: str | None) -> list[dict]:
    q = build_query(since_iso)
    results: list[dict] = []
    page = 1
    while page <= 10:  # API caps search at 1000 results (10 pages x 100)
        params = urllib.parse.urlencode(
            {
                "q": q,
                "sort": "author-date",
                "order": "desc",
                "per_page": 100,
                "page": page,
            }
        )
        req = urllib.request.Request(f"{API}?{params}", headers=HEADERS)
        try:
            with urllib.request.urlopen(req) as resp:
                payload = json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):  # rate limited; back off and retry once
                reset = e.headers.get("X-RateLimit-Reset")
                wait = max(1, int(reset) - int(time.time())) if reset else 30
                print(f"Rate limited; waiting {wait}s...", file=sys.stderr)
                time.sleep(min(wait, 90))
                continue
            body = e.read().decode("utf-8", "replace")
            sys.exit(f"GitHub API {e.code}: {body}")

        items = payload.get("items", [])
        if not items:
            break

        for it in items:
            commit = it.get("commit", {})
            author_info = commit.get("author", {})
            repo = it.get("repository", {})
            login = (it.get("author") or {}).get("login")
            results.append(
                {
                    "commit_date": author_info.get("date"),
                    "author": login or author_info.get("name") or "unknown",
                    "repository": repo.get("full_name") or "unknown/unknown",
                    "commit_url": it.get("html_url"),
                }
            )

        if len(items) < 100:
            break
        page += 1
        time.sleep(2)  # commit search secondary-rate-limit friendliness

    return results


def merge(existing: list[dict], fetched: list[dict]) -> list[dict]:
    by_url = {c["commit_url"]: c for c in existing if c.get("commit_url")}
    for c in fetched:
        if c.get("commit_url"):
            by_url[c["commit_url"]] = c
    merged = list(by_url.values())
    merged.sort(key=lambda c: c["commit_date"] or "", reverse=True)
    return merged


def main() -> None:
    existing, newest = load_existing()
    if newest:
        print(f"Existing: {len(existing)} commits, newest {newest}.")
    else:
        print("No existing data.json; doing a full fetch.")

    fetched = fetch(newest)
    print(f"Fetched {len(fetched)} commits this run.")

    commits = merge(existing, fetched)

    out = {
        "query": QUERY,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "recent_days": RECENT_DAYS,
        "total_commits": len(commits),
        "commits": commits,
    }
    DATA_FILE.write_text(json.dumps(out, indent=2) + "\n")
    print(f"Wrote {DATA_FILE}: {len(commits)} commits total.")


if __name__ == "__main__":
    main()
