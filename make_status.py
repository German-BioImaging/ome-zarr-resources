# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pyyaml",
#     "requests",
#     "tqdm"
# ]
# ///

import os
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Dict, List, Optional, Callable
from yaml import load, dump, Loader
import requests
from tqdm import tqdm

# Concurrency control for secondary rate limits
# GitHub allows max 100 concurrent requests; we stay well below
MAX_WORKERS = 4
REQUEST_DELAY = 0.1  # seconds between requests per thread (avoid burst)
_request_lock = threading.Lock()
_last_request_time = 0.0


def throttled_request(
    session: requests.Session,
    method: str,
    url: str,
    max_retries: int = 3,
    **kwargs,
) -> requests.Response:
    """
    Make a rate-limit-aware request with retry logic for secondary limits.
    Implements exponential backoff on 403/429 responses.
    """
    global _last_request_time

    for attempt in range(max_retries):
        # Throttle requests to avoid secondary rate limits
        with _request_lock:
            now = time.time()
            wait_time = REQUEST_DELAY - (now - _last_request_time)
            if wait_time > 0:
                time.sleep(wait_time)
            _last_request_time = time.time()

        resp = session.request(method, url, **kwargs)

        # Check for secondary rate limit
        if resp.status_code in (403, 429):
            retry_after = resp.headers.get("Retry-After")
            remaining = resp.headers.get("X-RateLimit-Remaining")

            # Detect secondary rate limit (abuse detection)
            is_secondary = False
            try:
                body = resp.json()
                message = body.get("message", "").lower()
                if "secondary" in message or "abuse" in message:
                    is_secondary = True
            except Exception:
                pass

            # Also secondary if we still have remaining primary quota
            if remaining and int(remaining) > 0:
                is_secondary = True

            if is_secondary or retry_after:
                if retry_after:
                    wait = int(retry_after)
                else:
                    # Exponential backoff: 1s, 2s, 4s...
                    wait = 2**attempt

                print(
                    f"[SECONDARY RATE LIMIT] {url} - "
                    f"Retry-After: {retry_after or 'N/A'}, "
                    f"attempt {attempt + 1}/{max_retries}, waiting {wait}s",
                    file=sys.stderr,
                )
                print_rate_limit_info(resp, "secondary-limit")
                time.sleep(wait)
                continue

        return resp

    return resp  # Return last response after all retries exhausted


def print_rate_limit_info(response: requests.Response, context: str = "") -> None:
    """Print rate limit headers from a GitHub API response."""
    # Primary rate limit headers
    limit = response.headers.get("X-RateLimit-Limit", "?")
    remaining = response.headers.get("X-RateLimit-Remaining", "?")
    used = response.headers.get("X-RateLimit-Used", "?")
    reset = response.headers.get("X-RateLimit-Reset", "?")
    resource = response.headers.get("X-RateLimit-Resource", "?")

    # Secondary rate limit header
    retry_after = response.headers.get("Retry-After")

    reset_time = ""
    if reset and reset != "?":
        try:
            reset_dt = datetime.utcfromtimestamp(int(reset))
            reset_time = f" (resets at {reset_dt.isoformat()}Z)"
        except (ValueError, OSError):
            pass

    prefix = f"[{context}] " if context else ""
    msg = (
        f"{prefix}Rate limit: {used}/{limit} used, {remaining} remaining, "
        f"resource={resource}{reset_time}"
    )
    if retry_after:
        msg += f", Retry-After: {retry_after}s"

    print(msg, file=sys.stderr)


def check_response(
    response: requests.Response, context: str, print_on_success: bool = False
) -> bool:
    """
    Check response status and print rate limit info.
    Returns True if response is OK, False otherwise.
    Always prints rate limit info on failure; optionally on success.
    """
    if not response.ok:
        print(f"[{context}] HTTP {response.status_code}: {response.reason}", file=sys.stderr)
        print_rate_limit_info(response, context)

        # Print response body for rate limit errors (useful debugging info)
        if response.status_code in (403, 429):
            try:
                body = response.json()
                print(f"[{context}] Response: {body.get('message', body)}", file=sys.stderr)
            except Exception:
                pass
        return False
    if print_on_success:
        print_rate_limit_info(response, context)
    return True

with open("dashboard.yml") as f:
    config = load(f, Loader=Loader)

session = requests.Session()
session.headers.update(
    {
        "Accept": "application/vnd.github+json",
        "User-Agent": "ome-status-dashboard",
    }
)

# Set via https://github.com/settings/personal-access-tokens
token = os.getenv("GITHUB_TOKEN")
if token:
    session.headers["Authorization"] = f"Bearer {token}"


def build_session() -> requests.Session:
    new_session = requests.Session()
    new_session.headers.update(session.headers)
    return new_session


def format_date(iso_timestamp: str) -> str:
    return (
        datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00")).date().isoformat()
    )


STATUS_ROLLUP_QUERY = """
query($owner:String!,$name:String!){
  repository(owner:$owner,name:$name){
    defaultBranchRef{
      target{
        ... on Commit{
          oid
          commitUrl
          committedDate
          author{ user{login} name }
          statusCheckRollup{ state }
        }
      }
    }
  }
}
"""


def fetch_workflow_runs_status(
    owner: str, repo: str, session: requests.Session
) -> Optional[str]:
    """
    Fetch the status of the latest workflow runs for the default branch.
    Returns one of: SUCCESS, FAILURE, PENDING, NO_WORKFLOWS, or None if unknown.
    """
    ctx = f"{owner}/{repo}"

    # Get the default branch name
    repo_resp = throttled_request(
        session, "GET", f"https://api.github.com/repos/{owner}/{repo}"
    )
    if not check_response(repo_resp, f"{ctx} repo"):
        return None

    default_branch = repo_resp.json().get("default_branch")

    # Get active workflows
    active_workflow_ids = set()
    active_workflows_resp = throttled_request(
        session,
        "GET",
        f"https://api.github.com/repos/{owner}/{repo}/actions/workflows",
        params={"per_page": 100},
    )
    if not check_response(active_workflows_resp, f"{ctx} workflows"):
        return None

    workflows = active_workflows_resp.json().get("workflows", [])
    for workflow in workflows:
        if (workflow.get("state") or "").lower() == "active":
            workflow_id = workflow.get("id")
            if workflow_id is not None:
                active_workflow_ids.add(workflow_id)

    if not active_workflow_ids:
        return "NO_WORKFLOWS"

    resp = throttled_request(
        session,
        "GET",
        f"https://api.github.com/repos/{owner}/{repo}/actions/runs",
        params={"branch": default_branch, "per_page": 50},
    )
    if not check_response(resp, f"{ctx} runs"):
        return None

    runs = resp.json().get("workflow_runs", [])
    if not runs:
        return "NO_WORKFLOWS"

    # Check latest run for active workflows
    latest_per_workflow = {}
    for run in runs:
        workflow_id = run.get("workflow_id")
        if (
            workflow_id not in latest_per_workflow
            and workflow_id in active_workflow_ids
        ):
            latest_per_workflow[workflow_id] = run

    if not latest_per_workflow:
        return "NO_WORKFLOWS"

    # Determine overall status based on the latest runs.
    has_failure = False
    has_pending = False

    for run in latest_per_workflow.values():
        conclusion = run.get("conclusion")
        status = run.get("status")
        if status in ("queued", "in_progress", "waiting", "pending", "requested"):
            has_pending = True
        elif conclusion in ("failure", "timed_out", "action_required", "stale"):
            has_failure = True

    if has_failure:
        return "FAILURE"
    elif has_pending:
        return "PENDING"
    return "SUCCESS"


def fetch_last_commit_info(
    owner: str, repo: str, session: requests.Session
) -> Optional[dict]:
    """
    Fetch latest default-branch commit and its merged checks/status rollup via GraphQL.
    """
    ctx = f"{owner}/{repo}"
    resp = throttled_request(
        session,
        "POST",
        "https://api.github.com/graphql",
        json={
            "query": STATUS_ROLLUP_QUERY,
            "variables": {"owner": owner, "name": repo},
        },
    )
    if not check_response(resp, f"{ctx} graphql"):
        return None
    repo_data = (resp.json().get("data") or {}).get("repository") or {}
    branch_ref = repo_data.get("defaultBranchRef") or {}
    commit = branch_ref.get("target") or {}
    if not commit:
        return None
    author_block = commit.get("author") or {}
    author = (author_block.get("user") or {}).get("login") or author_block.get("name")
    committed_date = commit.get("committedDate")
    status_rollup = (commit.get("statusCheckRollup") or {}).get("state")

    return {
        "url": commit.get("commitUrl"),
        "date": format_date(committed_date) if committed_date else None,
        "author": author,
        "status": status_rollup,  # The checks of the commit upon merge
        "sha": commit.get("oid"),
    }


def fetch_repo_info(owner: str, repo: str, session: requests.Session) -> Optional[dict]:
    """
    Fetch repository metadata from the GitHub API.
    """
    ctx = f"{owner}/{repo}"
    resp = throttled_request(
        session, "GET", f"https://api.github.com/repos/{owner}/{repo}"
    )
    if resp.status_code == 404:
        print(f"[{ctx}] Repository not found (404)", file=sys.stderr)
        return None
    if not check_response(resp, f"{ctx} repo-info"):
        return None
    info = resp.json()
    return {
        "created_at": info.get("created_at"),
        "updated_at": info.get("updated_at"),
        "open_issues": info.get("open_issues_count"),
        "stargazers_count": info.get("stargazers_count"),
        "description": info.get("description"),
        "topics": info.get("topics", []),
        "size": info.get("size"),
    }


def fetch_last_release_info(
    owner: str, repo: str, session: requests.Session
) -> Optional[dict]:
    """
    Fetch latest release from the GitHub API.
    """
    ctx = f"{owner}/{repo}"
    releases_resp = throttled_request(
        session,
        "GET",
        f"https://api.github.com/repos/{owner}/{repo}/releases",
        params={"per_page": 1},
    )
    if releases_resp.status_code == 404:
        return None
    if not check_response(releases_resp, f"{ctx} releases"):
        return None

    releases = releases_resp.json()
    if not releases:
        return None

    last_release = releases[0]
    published_at = last_release.get("published_at") or last_release.get("created_at")
    return {
        "url": last_release.get("html_url"),
        "tag_name": last_release.get("tag_name"),
        "date": format_date(published_at) if published_at else None,
    }


def fetch_disabled_inactive_workflows(
    owner: str, repo: str, session: requests.Session
) -> List[str]:
    """
    Return names/paths for workflows auto-disabled due to inactivity.
    """
    ctx = f"{owner}/{repo}"
    page = 1
    disabled: List[str] = []
    while True:
        resp = throttled_request(
            session,
            "GET",
            f"https://api.github.com/repos/{owner}/{repo}/actions/workflows",
            params={"per_page": 100, "page": page},
        )
        if resp.status_code in (403, 404):
            if resp.status_code == 403:
                print(f"[{ctx}] Forbidden fetching workflows", file=sys.stderr)
                print_rate_limit_info(resp, f"{ctx} disabled-workflows")
            break
        if not resp.ok:
            check_response(resp, f"{ctx} disabled-workflows")
            break
        data = resp.json() or {}
        workflows = data.get("workflows") or []
        for workflow in workflows:
            if (workflow.get("state") or "").lower() == "disabled_inactivity":
                label = (
                    workflow.get("name")
                    or workflow.get("path")
                    or str(workflow.get("id") or "")
                )
                if label:
                    disabled.append(label)
        if len(workflows) < 100:
            break
        page += 1
    return disabled


def process_package(package: dict) -> None:
    """
    Populate metadata for a single package. Runs in worker threads.
    """
    local_session = build_session()
    package["user"], package["name"] = package["repo"].split("/")

    workflow_run_status = fetch_workflow_runs_status(
        package["user"], package["name"], local_session
    )
    package["workflow_run_status"] = workflow_run_status

    repo_info = fetch_repo_info(package["user"], package["name"], local_session)
    if repo_info:
        package["repo_info"] = repo_info
    else:
        package["error"] = True

    last_commit_info = fetch_last_commit_info(
        package["user"], package["name"], local_session
    )
    if last_commit_info:
        package["last_commit"] = last_commit_info

    last_release_info = fetch_last_release_info(
        package["user"], package["name"], local_session
    )
    if last_release_info:
        package["last_release"] = last_release_info

    disabled_workflows = fetch_disabled_inactive_workflows(
        package["user"], package["name"], local_session
    )
    if disabled_workflows:
        package["disabled_workflows"] = disabled_workflows


all_packages: List[dict] = []
for section in config:
    all_packages.extend(section["packages"])

print(f"Processing {len(all_packages)} packages with {MAX_WORKERS} workers...", file=sys.stderr)
print(f"Request throttling: {REQUEST_DELAY}s minimum between requests", file=sys.stderr)

with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
    futures = [executor.submit(process_package, package) for package in all_packages]
    for future in tqdm(as_completed(futures), total=len(futures)):
        # re-raise any worker exceptions
        future.result()

# Print final rate limit status (covers both primary and some secondary info)
print("\n=== Final Rate Limit Check ===", file=sys.stderr)
final_resp = throttled_request(session, "GET", "https://api.github.com/rate_limit")
if final_resp.ok:
    rate_data = final_resp.json()
    for resource_name, resource_info in rate_data.get("resources", {}).items():
        limit = resource_info.get("limit", "?")
        used = resource_info.get("used", "?")
        remaining = resource_info.get("remaining", "?")
        reset = resource_info.get("reset", 0)
        reset_time = datetime.utcfromtimestamp(reset).isoformat() + "Z" if reset else "?"
        print(
            f"  {resource_name}: {used}/{limit} used, {remaining} remaining, resets {reset_time}",
            file=sys.stderr,
        )
    # Note about secondary limits
    print(
        "\nNote: Secondary rate limits (abuse detection) are not queryable via API.",
        file=sys.stderr,
    )
    print(
        "If you see 403/429 with 'Retry-After' header above, you hit a secondary limit.",
        file=sys.stderr,
    )
else:
    print(f"Failed to fetch rate limit: HTTP {final_resp.status_code}", file=sys.stderr)
    print_rate_limit_info(final_resp, "rate_limit")

snapshot = {
    "generated_at": datetime.utcnow().isoformat() + "Z",
    "sections": config,
}

with open("generated.yml", "w") as generated_output:
    dump(snapshot, generated_output)

print(f"\nWrote generated.yml with {len(all_packages)} packages.", file=sys.stderr)
