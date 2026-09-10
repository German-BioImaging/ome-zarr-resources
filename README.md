### OME-Zarr Resources Dashboard

Status dashboard for OME-Zarr-related repos: <https://german-bioimaging.github.io/ome-zarr-resources/>

Two runs, two costs:

| | when | script | reads | writes |
|---|---|---|---|---|
| pool refresh | Mondays 05:00 UTC | `pick_top.py` | `dashboard.yml` − `to_skip.yml` | `selected.yml`, `pool.yml` |
| status scan | daily 06:00 UTC | `make_status.py` | `selected.yml` | `generated.yml` |

`pick_top.py` is cheap (stars + topics, batched GraphQL, ~4 requests for the whole
pool). It picks ~100 repos: every `ome/*` repo and every repo tagged
`ome-zarr`, `ome-zarr-converter`, `ome-zarr-viewer`, `ome-zarr-writer` or
`ome-zarr-reader` is kept unconditionally, the rest of the slots go to the
most-starred.

`make_status.py` is expensive (~5 API calls per repo: commits, releases, workflows),
so it only runs over those ~100.

`index.html` reads `generated.yml` and offers four views: all repos, grouped by tag
(`Core` = `ome/ngff` + `ome/ngff-spec`; a repo shows up in every tag it carries), or
grouped by the manual `dashboard.yml` sections, or the full pool (`pool.yml`: every
tracked repo with its star count, ✅ marking the ones scanned daily). The tag and section
views get a row of jump links; tracked tags with no repos yet are listed greyed out.

#### Adding a repo

Add it to `dashboard.yml` under the fitting section (sections define the pool and the
"Manual sections" view), or to `to_skip.yml` to exclude it. It enters the dashboard on the
next Monday run if it makes the cut.

#### Running locally

```sh
export GITHUB_TOKEN=$(gh auth token)   # GraphQL needs auth
uv run pick_top.py
uv run make_status.py
python -m http.server   # then open index.html
DEMO=1 python pick_top.py   # selection self-check
```

CI uses `secrets.GH_PAT` (5k requests/h) — see `.github/workflows/`.
