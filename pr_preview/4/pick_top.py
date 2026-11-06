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

import json
import os
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Dict, List

import requests
from tqdm import tqdm
from yaml import dump, safe_load

TOP_BY_STARS = 100
MAX_SELECTED = 200
MAINTAINED_TOPICS = [
    "ome-zarr-converter",
    "ome-zarr-viewer",
    "ome-zarr-writer",
    "ome-zarr-reader",
    "ome-zarr",
]
CORE_REPOS = ["ome/ngff", "ome/ngff-spec"]
COMMITS_FILE = "commits/data.json"
DISCOVERED_SECTION = "Discovered (OME-Zarr commits)"
# Build and markup files say little about what a tool is written in.
IGNORED_LANGUAGES = {"HTML", "CSS", "Shell", "Dockerfile", "Makefile", "Batchfile", "TeX"}
SECOND_LANGUAGE_MIN_SHARE = 0.2
MERGED_LANGUAGES = {"TypeScript": "JavaScript", "Svelte": "JavaScript"}


def load_repos(path: str) -> Dict[str, str]:
    """slug -> the curated section it sits in (kept for the 'legacy' view)."""
    with open(path) as f:
        sections = safe_load(f) or []
    # GitHub slugs are case-insensitive; keep one entry per repo (first wins)
    # or the same repo shows up twice on the dashboard.
    repos: Dict[str, str] = {}
    for section in sections:
        for pkg in section.get("packages") or []:
            repos.setdefault(pkg["repo"].strip().lower(), section.get("name", "Other"))
    return repos


def discovered_repos(path: str = COMMITS_FILE) -> Dict[str, str]:
    """Repos the commit feed has seen mentioning OME-Zarr, as pool entries.

    The feed (commits/fetch.py, refreshed daily) already archives every repo with
    an OME-Zarr commit, so the pool can grow on its own: new repos only cost the
    weekly stars query, and the daily deep scan is still capped at MAX_SELECTED.
    """
    if not Path(path).exists():
        return {}
    commits = json.loads(Path(path).read_text()).get("commits", [])
    return {
        c["repository"].strip().lower(): DISCOVERED_SECTION
        for c in commits
        if c.get("repository") and "/" in c["repository"]
    }


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
                "{ stargazerCount repositoryTopics(first:20){nodes{topic{name}}} "
                "languages(first:5,orderBy:{field:SIZE,direction:DESC})"
                "{totalSize edges{size node{name}}} }"
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
                "languages": top_languages(repo.get("languages") or {}),
            }
    return out


def top_languages(languages: dict) -> List[str]:
    """Main language, plus the runner-up if it is a real share of the code."""
    total = languages.get("totalSize") or 0
    sizes: Counter = Counter()
    for edge in languages.get("edges") or []:
        name = edge["node"]["name"]
        if name not in IGNORED_LANGUAGES:
            sizes[MERGED_LANGUAGES.get(name, name)] += edge["size"]
    return [
        name
        for i, (name, size) in enumerate(sizes.most_common(2))
        if i == 0 or size > SECOND_LANGUAGE_MIN_SHARE * total
    ]


def tags_for(slug: str, topics: List[str]) -> List[str]:
    tags = ["Core"] if slug in CORE_REPOS else []
    tags += [t for t in MAINTAINED_TOPICS if t in topics]
    return tags


def select(
    info: Dict[str, dict], top: int = TOP_BY_STARS, cap: int = MAX_SELECTED
) -> List[dict]:
    """ome/* and maintained-topic repos are kept unconditionally; the
    `top` most-starred others join them, up to `cap`. The keeps alone may exceed it."""
    packages = [
        {
            "repo": slug,
            "stars": d["stars"],
            "tags": tags_for(slug, d["topics"]),
            "languages": d.get("languages", []),
            "section": d.get("section", "Other"),
        }
        for slug, d in info.items()
    ]
    packages.sort(key=lambda p: -p["stars"])
    kept = [p for p in packages if p["repo"].startswith("ome/") or p["tags"]]
    kept_slugs = {p["repo"] for p in kept}
    rest = [p for p in packages if p["repo"] not in kept_slugs]
    return kept + rest[: min(top, max(0, cap - len(kept)))]


def demo() -> None:
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".yml") as f:
        f.write("- name: A\n  packages:\n    - repo: Ome/Ngio\n"
                "- name: B\n  packages:\n    - repo: ome/ngio\n")
        f.flush()
        assert load_repos(f.name) == {"ome/ngio": "A"}, "case-insensitive dedupe"

    with tempfile.NamedTemporaryFile("w", suffix=".json") as f:
        f.write('{"commits": [{"repository": "Foo/Bar"}, {"repository": "foo/bar"},'
                ' {"repository": "junk"}, {}]}')
        f.flush()
        assert discovered_repos(f.name) == {"foo/bar": DISCOVERED_SECTION}
    assert discovered_repos("does/not/exist.json") == {}

    info = {
        "ome/quiet": {"stars": 0, "topics": []},
        "x/tagged": {"stars": 1, "topics": ["ome-zarr-viewer"]},
        "x/popular": {"stars": 999, "topics": []},
        "x/meh": {"stars": 5, "topics": []},
    }
    picked = {p["repo"] for p in select(info, top=1)}
    assert picked == {"ome/quiet", "x/tagged", "x/popular"}, picked
    assert len(select(info, top=5, cap=3)) == 3, "cap limits the top-by-stars"
    assert len(select(info, cap=1)) == 2, "unconditional keeps may exceed cap"
    assert tags_for("ome/ngff", []) == ["Core"]

    def langs(**sizes):
        edges = [{"size": v, "node": {"name": k}} for k, v in sizes.items()]
        return {"totalSize": sum(sizes.values()), "edges": edges}

    assert top_languages(langs(HTML=70, Python=10, Rust=20)) == ["Rust"], "20% is not enough"
    assert top_languages(langs(Python=79, Rust=21)) == ["Python", "Rust"]
    assert top_languages(langs(Python=50, TypeScript=30, Svelte=30)) == ["JavaScript", "Python"]
    assert top_languages(langs(Shell=5)) == []
    assert top_languages({}) == []
    print("ok")


def main() -> None:
    sections = load_repos("dashboard.yml")
    curated = set(sections)
    # Curated entries win, so a hand-placed repo keeps its section.
    for slug, section in discovered_repos().items():
        sections.setdefault(slug, section)
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

    print(f"pool: {len(repos)} repos "
          f"({len(curated & set(repos))} curated, "
          f"{len(repos) - len(curated & set(repos))} from the commit feed)")
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

    # Worth a look: these came in from the commit feed, not from dashboard.yml.
    # Off-topic ones (big monorepos that merely mention OME-Zarr) belong in to_skip.yml.
    newcomers = [p for p in packages if p["repo"] not in curated]
    if newcomers:
        print(f"newly selected from the commit feed ({len(newcomers)}):")
        for p in newcomers:
            print(f"  {p['repo']} ({p['stars']}⭐)")

    now = datetime.utcnow().isoformat() + "Z"
    pool_tags = {slug: tags_for(slug, d["topics"]) for slug, d in info.items()}
    with open("selected.yml", "w") as f:
        dump(
            {
                "generated_at": now,
                "pool_count": len(info),
                "tagged_count": sum(1 for p in pool_tags.values() if p),
                "packages": packages,
            },
            f,
            sort_keys=False,
        )

    # The full pool: stars and topics, already in hand from the weekly query — no
    # extra requests. It is what the site links to when explaining why just 100
    # repos get the daily deep scan.
    selected_slugs = {p["repo"] for p in packages}
    pool = sorted(
        (
            {
                "repo": slug,
                "stars": d["stars"],
                "tags": tags_for(slug, d["topics"]),
                "topics": d["topics"],
                "languages": d["languages"],
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
