# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pyyaml",
#     "requests",
#     "tqdm",
# ]
# ///
"""Weekly cheap pass: stars + topics for the whole pool, pick the repos worth
scanning daily (see make_status.py)."""

import os
from collections import Counter
from datetime import datetime
from typing import Dict, List

import requests
from tqdm import tqdm
from yaml import dump, load, Loader

TARGET = 100
MAINTAINED_TOPICS = [
    "ome-zarr-converter",
    "ome-zarr-viewer",
    "ome-zarr-writer",
    "ome-zarr-reader",
    "ome-zarr",
]
CORE_REPOS = ["ome/ngff", "ome/ngff-spec"]


def load_repos(path: str) -> Dict[str, str]:
    """slug -> the curated section it sits in (kept for the 'legacy' view)."""
    with open(path) as f:
        sections = load(f, Loader=Loader) or []
    # GitHub slugs are case-insensitive; keep one entry per repo (first wins)
    # or the same repo shows up twice on the dashboard.
    repos: Dict[str, str] = {}
    for section in sections:
        for pkg in section.get("packages") or []:
            repos.setdefault(pkg["repo"].strip().lower(), section.get("name", "Other"))
    return repos


def fetch_stars_and_topics(repos: List[str], session) -> Dict[str, dict]:
    """One GraphQL request per 100 repos, aliased."""
    out: Dict[str, dict] = {}
    batches = range(0, len(repos), 100)
    for start in tqdm(batches, desc="stars+topics", unit="batch"):
        chunk = repos[start : start + 100]
        parts = []
        for i, slug in enumerate(chunk):
            owner, name = slug.split("/")
            parts.append(
                f'r{i}: repository(owner:"{owner}",name:"{name}")'
                "{ stargazerCount repositoryTopics(first:20){nodes{topic{name}}} }"
            )
        resp = session.post(
            "https://api.github.com/graphql", json={"query": "{" + " ".join(parts) + "}"}
        )
        resp.raise_for_status()
        data = resp.json().get("data") or {}
        for i, slug in enumerate(chunk):
            repo = data.get(f"r{i}")
            if not repo:  # deleted / renamed / private
                tqdm.write(f"  unreachable, dropped: {slug}")
                continue
            out[slug] = {
                "stars": repo.get("stargazerCount") or 0,
                "topics": [
                    n["topic"]["name"]
                    for n in (repo.get("repositoryTopics") or {}).get("nodes") or []
                ],
            }
    return out


def tags_for(slug: str, topics: List[str]) -> List[str]:
    tags = ["Core"] if slug in CORE_REPOS else []
    tags += [t for t in MAINTAINED_TOPICS if t in topics]
    return tags


def select(info: Dict[str, dict], target: int = TARGET) -> List[dict]:
    """ome/* and maintained-topic repos are kept unconditionally; the rest of the
    slots go to the most-starred. May exceed `target` if the keeps alone do."""
    packages = [
        {
            "repo": slug,
            "stars": d["stars"],
            "tags": tags_for(slug, d["topics"]),
            "section": d.get("section", "Other"),
        }
        for slug, d in info.items()
    ]
    packages.sort(key=lambda p: -p["stars"])
    kept = [p for p in packages if p["repo"].startswith("ome/") or p["tags"]]
    kept_slugs = {p["repo"] for p in kept}
    rest = [p for p in packages if p["repo"] not in kept_slugs]
    return kept + rest[: max(0, target - len(kept))]


def demo() -> None:
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".yml") as f:
        f.write("- name: A\n  packages:\n    - repo: Ome/Ngio\n"
                "- name: B\n  packages:\n    - repo: ome/ngio\n")
        f.flush()
        assert load_repos(f.name) == {"ome/ngio": "A"}, "case-insensitive dedupe"

    info = {
        "ome/quiet": {"stars": 0, "topics": []},
        "x/tagged": {"stars": 1, "topics": ["ome-zarr-viewer"]},
        "x/popular": {"stars": 999, "topics": []},
        "x/meh": {"stars": 5, "topics": []},
    }
    picked = {p["repo"] for p in select(info, target=3)}
    assert picked == {"ome/quiet", "x/tagged", "x/popular"}, picked
    assert select(info, target=1)[0]["repo"] in ("ome/quiet", "x/tagged")
    assert len(select(info, target=1)) == 2, "unconditional keeps may exceed target"
    assert tags_for("ome/ngff", []) == ["Core"]
    print("ok")


def main() -> None:
    sections = load_repos("dashboard.yml")
    for skipped in load_repos("to_skip.yml"):
        sections.pop(skipped, None)
    repos = list(sections)
    session = requests.Session()
    session.headers.update(
        {"Accept": "application/vnd.github+json", "User-Agent": "ome-status-dashboard"}
    )
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        raise SystemExit("GITHUB_TOKEN is required (the GraphQL API needs auth)")
    session.headers["Authorization"] = f"Bearer {token}"

    print(f"pool: {len(repos)} repos")
    info = fetch_stars_and_topics(repos, session)
    for slug, d in info.items():
        d["section"] = sections[slug]
    packages = select(info)

    keeps = [p for p in packages if p["repo"].startswith("ome/") or p["tags"]]
    counts = Counter(t for p in packages for t in p["tags"])
    print(f"kept unconditionally: {len(keeps)} "
          f"({sum(1 for p in keeps if p['repo'].startswith('ome/'))} ome/*, "
          f"{sum(1 for p in keeps if p['tags'])} tagged)")
    for tag in ["Core"] + MAINTAINED_TOPICS:
        print(f"  {tag}: {counts[tag]}")
    print(f"star cutoff: {packages[-1]['stars']} (lowest selected)")

    now = datetime.utcnow().isoformat() + "Z"
    with open("selected.yml", "w") as f:
        dump(
            {"generated_at": now, "pool_count": len(info), "packages": packages},
            f,
            sort_keys=False,
        )

    # The full pool, stars only: cheap to keep, and it is what the site links to
    # when explaining why just 100 repos get the daily deep scan.
    selected_slugs = {p["repo"] for p in packages}
    pool = sorted(
        (
            {
                "repo": slug,
                "stars": d["stars"],
                "tags": tags_for(slug, d["topics"]),
                "section": d["section"],
                "selected": slug in selected_slugs,
            }
            for slug, d in info.items()
        ),
        key=lambda p: -p["stars"],
    )
    with open("pool.yml", "w") as f:
        dump({"generated_at": now, "packages": pool}, f, sort_keys=False)
    print(f"wrote selected.yml ({len(packages)} repos) "
          f"and pool.yml ({len(info)} repos)")


if __name__ == "__main__":
    if os.getenv("DEMO"):
        demo()
    else:
        main()
