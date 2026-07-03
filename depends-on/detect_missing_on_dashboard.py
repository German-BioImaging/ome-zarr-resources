#!/usr/bin/env python3
"""
Parse github-dependents-info JSON output and a dashboard.yml, then write a new
YAML listing all dependent repos that are NOT already in the dashboard.

Usage:
    python diff_deps.py <dependents.json> <dashboard.yml> [output.yml]
"""

import json
import sys
from pathlib import Path

import yaml


def load_dashboard_repos(dashboard_path: Path) -> set[str]:
    """Return the set of repo slugs (owner/name) already listed in dashboard.yml."""
    data = yaml.safe_load(dashboard_path.read_text())
    repos: set[str] = set()
    # dashboard is a list of groups, each with a 'packages' list of {'repo': ...}
    if isinstance(data, list):
        groups = data
    elif isinstance(data, dict) and "packages" in data:
        groups = [data]
    else:
        groups = data.values() if isinstance(data, dict) else []
    for group in groups:
        for pkg in (group or {}).get("packages", []) or []:
            repo = pkg.get("repo")
            if repo:
                repos.add(repo.strip().lower())
    return repos


def load_dependents(json_path: Path) -> list[str]:
    """Return ordered list of repo slugs from the dependents JSON."""
    data = json.loads(json_path.read_text())
    entries = (
        data.get("all_public_dependent_repos", data) if isinstance(data, dict) else data
    )
    slugs: list[str] = []
    seen: set[str] = set()
    for e in entries:
        slug = e.get("name") or f"{e.get('owner')}/{e.get('repo_name')}"
        key = slug.strip().lower()
        if key not in seen:
            seen.add(key)
            slugs.append(slug.strip())
    return slugs


def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    json_path = Path(sys.argv[1])
    dashboard_path = Path(sys.argv[2])
    out_path = (
        Path(sys.argv[3])
        if len(sys.argv) > 3
        else Path("ome-zarr-py-deps-not-in-dashboard.yml")
    )

    existing = load_dashboard_repos(dashboard_path)
    dependents = load_dependents(json_path)

    missing = [slug for slug in dependents if slug.lower() not in existing]

    # Build YAML preserving order: name + packages list of {repo: ...}
    lines = ["name: OME-Zarr Py Dependencies", "", "packages:"]
    for slug in missing:
        lines.append(f"  - repo: {slug}")
    out_path.write_text("\n".join(lines) + "\n")

    print(f"Dependents found : {len(dependents)}")
    print(f"Already in board : {len(existing)}")
    print(f"Not in dashboard : {len(missing)}")
    print(f"Written to       : {out_path}")


if __name__ == "__main__":
    main()
