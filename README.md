# Docker Resource Intelligence (DRI)

DRI monitors running Docker containers, flags unusual resource use and recommends CPU and memory limits from observed usage. Its default policies raise warnings. Operators can configure individual rules to throttle, pin or restart a container.

New to the project? Pick a first issue, run a backend test without Docker, or build the dashboard. [Contributor guide](CONTRIBUTING.md)

## Why DRI exists

Anyone who has run more than a handful of containers has met the same three problems:

1. **Noisy neighbors.** One greedy container quietly starves the others, and nobody notices until users do.
2. **Surprise OOM kills.** A slow memory leak or a sudden spike takes a service down at 3 a.m. and the only clue is an exit code of 137.
3. **Guesswork limits.** CPU and memory limits are usually copied from a blog post or set to "something big", so teams either overpay for idle capacity or get throttled and killed.

## What you get

- **Real-Time Metrics**: CPU, memory, network, block I/O and PID counts sampled in parallel from running containers.
- **Trend Awareness**: 5, 15, and 60-minute moving averages calculated from available samples in memory and storage.
- **Smart Anomaly Detection**: per-container z-score analysis, memory growth-rate spikes, and warnings when memory usage or growth indicates OOM risk.
- **Right-Sizing Recommendations**: p95-based CPU and memory limits with a configurable safety margin, confidence tiers, data-completeness reporting, idle (scale-to-zero) detection, and restart-aware memory-leak detection.
- **Usage Forecasting**: project CPU or memory up to 7 days ahead with confidence intervals (Holt’s linear trend method, or linear regression as the fallback).
- **Policy Engine**: global YAML rules and additional rules selected by image pattern or container ID. Rules can require a sustained breach. Restart the agent to apply YAML changes.
- **Automated Actions**:
  - **Throttle**: tighten limits for the resource that triggered the rule and restore them when the condition clears
  - **Isolate**: pin containers to specific CPU sets
  - **Heal**: stop and start a container when a configured restart rule fires
  - **Webhooks**: trigger custom HTTP callbacks, protected by a circuit breaker that pauses delivery after repeated failures
- **Log Intelligence** groups masked container logs into templates and correlates error patterns with Docker events and resource metrics. [Evidence and verdict rules](docs/log-intelligence.md)
- **Docker Event Tracking**: OOM, die, start, stop, create, and destroy events, plus automatic restart counting.
- **Container Lifecycle Awareness**: stopped containers are flagged and cleanly evicted after a grace period.
- **Modern Dashboard**: a React + Vite control-tower UI with seven live views.
- **REST API**: FastAPI with optional API-key auth, structured JSON errors, and cached recommendations and forecasts.
- **Prometheus Integration**: scrape API request counts and latency plus the Python client’s default runtime metrics at `/metrics`. Container CPU, memory and I/O readings are available as JSON at `/api/v1/metrics`.
- **Pluggable Storage**: SQLite by default or TimescaleDB with a PostgreSQL connection.

---

## Architecture

```
Docker Daemon
    |
    v
Stats Collector (parallel)  -->  Docker Event Listener
    |                                   |
    v                                   v
Metrics Storage (SQLite / TimescaleDB) <-- Events & Restart Tracking
    |
    v
Aggregator (Moving Averages & Anomaly Detection)
    |
    +--> Recommender & Forecaster (background cache worker)
    |
    v
Policy Engine (Rule Evaluation)
    |
    v
Actions (Throttle / Isolate / Heal / Webhooks)
    |
    v
HTTP API (FastAPI :8000)  -->  Dashboard (Vite :5173)
    |
    v
Prometheus (:9090) scrapes the API at :8000/metrics
```

---

## Prerequisites

| Requirement | Version |
|---|---|
| Python | 3.9 or newer |
| Docker Desktop / Engine | 20.10 or newer |
| Node.js (dashboard only) | 20.19+ on Node 20, or 22.12+ |

> **Windows note**: Docker Desktop must be running before starting the agent.

---

## Project Structure

```
docker-resouce-intelligence/
├── src/dri/
│   ├── core/          # Collector, storage (SQLite/Timescale), aggregator,
│   │                  # policy engine, recommender, forecaster, cache worker
│   ├── actions/       # Throttle, isolate, heal, scale action handlers
│   ├── events/        # Docker event listener
│   ├── api/           # FastAPI routes, models, server
│   ├── utils/         # Logger, metrics types, webhooks
│   ├── config.py      # Configuration loader & schema
│   └── main.py        # Agent entry point
├── config/
│   └── config.yaml    # Main configuration file
├── dashboard/         # React + Vite frontend
├── deployment/
│   ├── Dockerfile
│   ├── Dockerfile.dashboard
│   ├── docker-compose.yml
│   └── prometheus.yml
├── docs/              # Guides (e.g. TimescaleDB migration)
├── tests/             # Unit & integration tests
├── requirements.txt
└── pyproject.toml
```

---

## Quick Start

### 1. Clone the repository

```bash
git clone https://github.com/Suganth46/docker-resouce-intelligence.git
cd docker-resouce-intelligence
```

### 2. Install Python dependencies

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

python -m pip install -e .
```

### 3. Run the DRI agent

From the project root, set `storage.sqlite_path` in `config/config.yaml` to a writable location such as `metrics.db`, then run

```bash
python -m dri.main --config config/config.yaml
```

Expected startup output:

```
[INFO] dri.main      - Initializing DRI agent...
[INFO] dri.main      - Connected to Docker daemon
[INFO] dri.storage   - Storage initialized
[INFO] dri.collector - Connected to Docker daemon
[INFO] dri.main      - DRI agent initialized successfully
[INFO] dri.main      - Starting metrics collection (interval: 5s)
[INFO] dri.main      - Starting metrics aggregation (interval: 10s)
[INFO] dri.main      - Starting policy checks (interval: 10s)
[INFO] dri.main      - Starting Docker event listener
```

The REST API is now live at **http://localhost:8000** (interactive docs at `/docs`).

### 4. Open the dashboard

In a **separate terminal**:

```bash
cd dashboard
npm install
npm run dev
```

The dashboard opens at **http://localhost:5173**. To point it at a different backend:

```bash
VITE_API_BASE=http://192.168.1.10:8000 VITE_API_KEY=your-api-key npm run dev
```

**Dashboard views:**

| View | What it shows |
|---|---|
| **Overview** | Agent health, live per-container CPU/memory, I/O, restart counts, anomaly and stopped badges, with a click-through detail panel |
| **Alert Feed** | Sortable feed of policy violations |
| **Recommendations** | Right-sized CPU/memory limits, confidence tier, leak and idle signals |
| **Forecast** | On-demand CPU/memory projection with confidence intervals |
| **Actions History** | Everything DRI did automatically, and whether it worked |
| **Docker Events** | OOM / die / start / stop lifecycle stream with search |
| **Policies** | The active global and per-container rules |

The polling interval (5–60s) is adjustable from the dashboard's Settings panel.

---

## Full Docker Deployment

As an alternative to the local quick start, run the agent, dashboard, Prometheus and local Ollama service together. The bundled Prometheus configuration scrapes `dri-agent:8000/metrics` on the Compose network.

```bash
cd deployment
docker compose up -d --build
```

| Service | URL |
|---|---|
| DRI Dashboard | http://localhost:5173 |
| DRI Agent API | http://localhost:8000 |
| Prometheus | http://localhost:9090 |

Stop the services with `docker compose down`.

---

## Local LLM analysis (Ollama)

The container detail page shows the rule-based verdict first. **Analyze with AI** requests an advisory explanation from the local model. The same analysis runs in the background for alerts with a rule-based verdict. Model suggestions are displayed as text. The server sends masked templates, counts, rule reasons, resource readings and event actions to Ollama over the private Compose network. Raw sample lines and event attributes stay out of the prompt. With the default local URL, inference data stays on the host.

The Python agent defaults to AI disabled. Plain Compose starts Ollama and enables analysis in the agent. On first start the model pull can take several minutes. Allow several GB for the image plus roughly 1 GB for the default quantized 1.5B model. Weights persist in the named `ollama-data` volume across container recreations. The agent starts immediately. Ollama becomes healthy when its selected model appears in `ollama list`.

From `deployment/`, copy `.env.example` to `.env`, set `DRI_LLM_MODEL` to select a different model, then run `docker compose up -d`. Set `DRI_LLM_ENABLED=false` to disable analysis in the agent. The Ollama service still follows the Compose startup and download settings. For an NVIDIA host with the container runtime installed, use `docker compose -f docker-compose.yml -f docker-compose.gpu.yml up`.

The global policy excludes names matching `dri-*` through `policy_exclude_names`. DRI still collects their metrics and logs. The shipped rules only warn. To opt in to Docker updates, set a specific rule’s action to `throttle` and configure the corresponding CPU or memory target. Supply custom shell-style patterns or an empty list to include every container. Ollama's own Compose limits are 2 GiB and 2 CPUs and require a Docker daemon that supports cgroups.

See the [benchmark results and selection criteria](docs/log-intelligence.md#local-model-benchmark) for the status of the provisional default.

## Configuration

Edit `config/config.yaml` to customise the agent. Docker log collection requires a readable log driver such as `json-file`, `local` or `journald`, or a readable Docker logging cache.

```yaml
log_level: INFO
log_json: false

collector_interval: 5       # seconds between metric samples
aggregator_interval: 10     # seconds between aggregations
policy_check_interval: 10   # seconds between policy evaluations
container_status_interval: 15          # poll for stopped/started containers
container_status_cleanup_minutes: 5    # evict stopped containers after this long

logs:
  warmup_minutes: 10  # Observe each container before flagging new error patterns.
  enabled: true
  retention_hours: 24  # Log volume typically exceeds metrics volume.
  max_line_length: 1000
  max_lines_per_second: 200  # Per container; warnings, errors and crash signals are exempt.
  flush_interval_seconds: 2.0
  flush_batch_size: 200
  discover_interval_seconds: 15
  max_followed_containers: 50
  error_patterns: [ERROR, FATAL, CRITICAL, Exception, Traceback, panic]
  crash_patterns: [OutOfMemoryError, Killed, connection refused, timed out, too many open files, ECONNREFUSED, no space left]

storage:
  backend: "sqlite"         # or "timescaledb"
  sqlite_path: "metrics.db"
  retention_hours: 168      # keep >= recommender lookback (7 days)

api:
  host: "0.0.0.0"
  port: 8000
  enable_cors: true
  allowed_origins: ["*"]    # set explicit origins in production
  api_key: null             # set a secret to require X-Api-Key

actions:
  throttle:
    cpu_quota: 50000
    memory_limit: "512m"
  isolate_cpuset: "0-1"
  webhook_url: null
  restart_grace_period: 30

global_policy:
  name: "global"
  enabled: true
  rules:
    - metric: "memory"
      operator: ">="
      value: 85.0
      duration_seconds: 60
      action: "warning"

    - metric: "memory_growth_rate_5m"
      operator: ">"
      value: 20.0
      duration_seconds: 30
      action: "warning"

    - metric: "memory"
      operator: ">="
      value: 95.0
      duration_seconds: 10
      action: "warning"
```

Available policy metrics: `cpu`, `cpu_5m`, `cpu_15m`, `cpu_60m`, `memory`, `memory_5m`, `memory_15m`, `memory_60m`, `memory_growth_rate_1m`, `memory_growth_rate_5m`, `log_errors_1m`, `log_errors_5m`, `log_warnings_5m`, `log_crash_signals_5m`, `log_new_templates_5m`.

CPU rules apply only CPU settings. CPU shares control relative weight under contention. A positive CPU quota can throttle a container with an unlimited quota. Memory rules check Docker’s current usage immediately before tightening an existing limit and leave swap unchanged. DRI skips settings whose unset value Docker cannot restore with a live update (including an unlimited memory limit and unset CPU shares). Choose targets appropriate for the workload.

The action history records the previous and applied limits. While monitoring the container, DRI restores its settings once all matching throttle conditions for that resource clear. Pending restorations survive agent restarts in the configured storage backend. Values changed externally after throttling are preserved.

### Per-container policy example

```yaml
container_policies:
  - name: "redis-policy"
    selector:
      image: "redis"
    enabled: true
    rules:
      - metric: "memory"
        operator: ">="
        value: 90.0
        duration_seconds: 30
        action: "throttle"
```

---

## REST API Reference

Base URL: `http://localhost:8000`

### Authentication

By default (`api_key: null`) authentication is off, which keeps local development frictionless. For any non-localhost deployment, set `api.api_key`. Then all `/api/v1/*` data endpoints and `/metrics` require an `X-Api-Key` header and return `401` otherwise. Health endpoints stay open so liveness probes keep working.

| Method | Endpoint | Auth | Description |
|---|---|---|---|
| `GET` | `/health`, `/api/v1/health` | No | Agent health and live counters |
| `GET` | `/api/v1/metrics[?container_id=<id>]` | When configured | Current metrics for all (or one) container |
| `GET` | `/api/v1/alerts` | When configured | Recent triggered alerts (paginated) |
| `GET` | `/api/v1/actions/history` | When configured | Action execution history (paginated) |
| `GET` | `/api/v1/events` | When configured | Docker lifecycle events (paginated, filterable) |
| `GET` | `/api/v1/policies` | When configured | Active policies |
| `GET` | `/api/v1/recommendations` | When configured | Right-sized limits per container (cached; `?force=true` to recompute) |
| `GET` | `/api/v1/llm/status` | When configured | Local model availability and queue statistics |
| `POST` | `/api/v1/containers/{container_id}/explain/ai` | When configured | Queue an advisory summary (202), once per container every 30 seconds |
| `GET` | `/api/v1/logs/{container_id}` | When configured | Masked recent lines, filtered by minutes, level and limit |
| `GET` | `/api/v1/containers/{container_id}/explain` | When configured | Heuristic verdict and supporting evidence for a 1–120 minute window |
| `GET` | `/api/v1/forecast/<container_id>` | When configured | Usage forecast (cached; `?force=true` to recompute) |
| `GET` | `/metrics` | When configured | API request counter (`dri_api_requests_total`), latency histogram (`dri_api_request_duration_seconds`) and Python client defaults |

### Quick examples

```bash
curl http://localhost:8000/health

# With api_key configured:
curl -H "X-Api-Key: your-secret" http://localhost:8000/api/v1/metrics
curl -H "X-Api-Key: your-secret" "http://localhost:8000/api/v1/recommendations?lookback_days=7&safety_margin=0.2"
curl -H "X-Api-Key: your-secret" "http://localhost:8000/api/v1/forecast/abc123def456?horizon_hours=24&metric=memory_percent"
```

---

## Development

### Install dev dependencies

```bash
pip install -e ".[dev]"
```

### Run tests

```bash
pytest tests/
```

The suite covers the collector, storage, aggregation, policy engine, recommender, forecaster, auth/CORS, Docker events, and API caching. TimescaleDB integration tests run only when `TIMESCALE_TEST_URL` is set. Point it at a disposable test database because the fixtures drop tables after use.

```bash
export TIMESCALE_TEST_URL=postgresql+psycopg2://dri:secret@localhost:5432/dri
pytest tests/test_timescale.py -v
```

### Lint & format

```bash
black src/
flake8 src/
mypy src/
```

### Dashboard development

```bash
cd dashboard
npm run lint      # oxlint
npm run build     # production build into dist/
npm run preview   # preview the production build
```

---

## Optional Extras

### TimescaleDB (production-scale history)

The TimescaleDB backend uses synchronous SQLAlchemy. Install the psycopg2 driver in the agent environment for the documented `postgresql+psycopg2://` URL. The current `timescaledb` package extra installs asyncpg. See the [migration guide](docs/timescaledb-migration.md).

```bash
python -m pip install psycopg2-binary
```

### Advanced forecasting (statsmodels)

```bash
pip install ".[forecasting]"
```

Enables Holt’s linear trend method; DRI falls back to linear regression when it is not installed.

---

## Troubleshooting

**Agent can't connect to Docker.** Make sure Docker is running (`docker info`). On Linux, check the socket: `ls -la /var/run/docker.sock`.

**Storage path errors on Windows.** Use a Windows-style path, e.g. `sqlite_path: "C:/ProgramData/dri/metrics.db"`.

**No recommendations yet.** Recommendations need at least 10 samples per container. They appear after a few minutes of collection, and confidence improves as more history builds up.

**High memory usage.** Lower `storage.retention_hours` (for example `12`), but keep it at or above your recommender lookback if you want full-window recommendations.

**High CPU / collection overhead.** Increase the intervals:

```yaml
collector_interval: 10
aggregator_interval: 15
policy_check_interval: 15
```

**Dashboard not loading data.** Confirm the agent is running, CORS is enabled, and `VITE_API_BASE` / `VITE_API_KEY` match your backend.

---

## Capacity planning

At the default five-second collection interval, seven days of continuous monitoring produces about 120,960 metric samples per container. Disk usage also depends on log volume, indexes and retention settings.

## Roadmap

- Live configuration reload
- InfluxDB export (the current `influxdb` extra installs only the client library)
- Per-container resource metrics in Prometheus format
- Smarter automated remediation (auto-apply accepted recommendations)
- Multi-host support with a central dashboard
- More notification channels (Slack, email, PagerDuty)
- Config-driven runtime API URL for the dashboard container

Ideas and contributions are very welcome.

---

## Contributing

For a first contribution, follow the fork-to-pull-request walkthrough. It includes separate setup paths for Python, the dashboard and documentation. [Contributing guide](CONTRIBUTING.md) · [Good first issues](https://github.com/Suganth46/docker-resouce-intelligence/issues?q=is%3Aissue%20is%3Aopen%20label%3A%22good%20first%20issue%22)

---

## License

MIT License. See [LICENSE](LICENSE) for details.

---
