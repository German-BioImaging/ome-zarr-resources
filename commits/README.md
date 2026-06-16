# OME-Zarr commit feed

A lean static page listing recent commits matching `OME-Zarr` across GitHub,
aggregated by day and repo. A scheduled GitHub Action fetches the data (using
the built-in `GITHUB_TOKEN`) and writes `data.json` — no token touches the client.

## Incremental & full history

`scripts/fetch.py` reads the existing `data.json`, finds the newest commit date,
and only queries commits authored on/after that day. New commits are merged into
the **full historical set** (deduplicated by commit URL) — nothing old is dropped.
So `data.json` grows into a complete archive, while the page shows just the last
`RECENT_DAYS` (10).

Each commit is stored flat:

```json
{ "commit_date": "...", "author": "...", "repository": "owner/name", "commit_url": "..." }
```

## Run locally

```bash
export GITHUB_TOKEN=ghp_xxx   # any token with public-repo read scope
python scripts/fetch.py       # stdlib only, no pip install
```

## Setup on GitHub

1. Push these files to a repo.
2. Settings → Pages → Source: **GitHub Actions**.
3. Settings → Actions → General → Workflow permissions: **Read and write**.
4. Run the workflow once (Actions → "Update commit feed" → Run workflow), or wait
   for the daily 05:17 UTC run. It fetches, commits `data.json`, and deploys Pages.

## Files

- `index.html` — renders `data.json`, aggregates the last 10 days client-side
- `scripts/fetch.py` — incremental fetch + merge (Python stdlib only)
- `.github/workflows/update.yml` — daily fetch + commit + deploy

## Tweaks

- Query / history window for the page: `QUERY` / `RECENT_DAYS` in `fetch.py`.
- Schedule: the `cron` in `update.yml`.
