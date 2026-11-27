# Contributing to Docker Resource Intelligence

You can help by trying a setup step on your machine, explaining a confusing dashboard reading, reproducing a bug, or changing code. A documentation change can go straight through GitHub’s file editor and Markdown preview.

## Find a first task

Browse the [open good first issues](https://github.com/Suganth46/docker-resouce-intelligence/issues?q=is%3Aissue%20is%3Aopen%20label%3A%22good%20first%20issue%22). Read the latest discussion and check for a linked pull request before starting. If something is unclear, ask on the issue and include what you tried.

| If you like working on | A place to start |
| --- | --- |
| Python and APIs | Follow the container filter in `get_metrics` and compare the response for a known and an unknown ID. [Metrics route](src/dri/api/routes.py) · [Issue #38](https://github.com/Suganth46/docker-resouce-intelligence/issues/38) |
| React and CSS | Pick one component and compare its colours and icons with the shared dashboard styles. [Design tokens](dashboard/src/index.css) · [Icons](dashboard/src/components/icons.tsx) · [Issue #53](https://github.com/Suganth46/docker-resouce-intelligence/issues/53) |
| Documentation | Try the setup instructions on your OS. If a step fails, include the command, what happened, and the wording that would have helped. [Quick start](README.md#quick-start) |

Found another problem or have an idea? [Open a bug report or feature request](https://github.com/Suganth46/docker-resouce-intelligence/issues/new/choose).

## Get a working copy

On GitHub, fork the repository to your account. Copy the HTTPS clone URL from the **Code** menu on your fork and substitute it for `YOUR_FORK_URL` below. Run the setup commands for the part you want to change.

```sh
git clone YOUR_FORK_URL
cd docker-resouce-intelligence
git switch -c my-first-change
```

### Python backend

Use Python 3.9–3.12 to match the CI matrix. From the repository root, create a virtual environment with `python -m venv .venv`. Activate it with `source .venv/bin/activate` on macOS or Linux, or `.\.venv\Scripts\Activate.ps1` in Windows PowerShell.

```sh
python -m pip install -e ".[dev]"
python -m pytest tests/test_phase7b_logs.py::test_template_identity -q
```

Look for `1 passed`. This test runs entirely in Python. It checks that two login messages with different IDs and IP addresses produce the same template. Read the example inputs alongside the implementation to see how a change gets tested. [Test](tests/test_phase7b_logs.py) · [Log parser](src/dri/core/log_parser.py)

Run a single file while editing, then run the suite from the repository root.

```sh
python -m pytest tests/test_phase7b_logs.py -q
python -m pytest tests/ -q
```

The default suite uses SQLite and Docker test doubles and can run with Docker stopped. Live TimescaleDB and Ollama tests skip unless you configure their services. Use a **disposable test database** for TimescaleDB because the fixtures drop tables during cleanup. To run the agent against real containers, follow the [quick start](README.md#quick-start).

### React dashboard

Use Node.js 20.19+ on the 20.x line or Node.js 22.12+. Install from the lockfile, check the code, then build the dashboard.

```sh
cd dashboard
npm ci
npm run lint
npm run build
```

A successful build creates `dashboard/dist/`. To preview changes in the browser, run `npm run dev` from `dashboard/` and open the local URL printed by Vite. The live views read from the DRI API at `http://localhost:8000` by default. [Agent setup and dashboard settings](README.md#quick-start)

## When your change crosses components

- A new storage operation needs an abstract method and implementations in both SQLite and TimescaleDB. Store database timestamps as naive UTC to match existing rows. [Storage contract](src/dri/core/storage_base.py) · [SQLite](src/dri/core/storage.py) · [TimescaleDB](src/dri/core/storage_timescale.py)
- Docker log streams block while waiting for the next line. Keep long-running readers on dedicated daemon threads so database calls can use the default executor. [Log collector](src/dri/core/log_collector.py)
- For dashboard changes, use the shared CSS colour variables, existing stroke icons and monospace styles for numeric readings. [Styles](dashboard/src/index.css) · [Icon library](dashboard/src/components/icons.tsx)

## Open a pull request

1. Review `git diff` and stage the files you intended to change with `git add path/to/file` (substitute the actual path). Commit them with a short description of the result.
2. Push your branch with `git push -u origin my-first-change`. On GitHub choose **Compare & pull request**, with this repository’s `main` as the base.
3. Explain what someone will see change, link the related issue if there is one, and list the checks you ran. For a visual change, a screenshot helps reviewers. For a docs change, preview the Markdown and try the edited step.
4. Check the results in the pull request’s **Checks** tab. If a job fails, open the failed step for its command and output. Push follow-up commits to the same branch to update the pull request.
