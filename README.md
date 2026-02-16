# Zero-Downtime ML Model Deployment

Production-grade blue-green deployment system for serving LLMs on resource-constrained hardware. Serves SmolLM2-1.7B via FastAPI with automated zero-downtime deployments, full observability, and quantitative load testing — all within a 4GB Docker memory budget on an 8GB Mac.

## Why This Exists

Deploying ML models in production without dropping requests is hard. Models are large, slow to load, and memory-hungry. This project solves that with blue-green deployments, an automated orchestrator with rollback, and a pre-warm strategy that eliminates CPU contention on constrained hardware.

**Key results:**

| Scenario | Duration | Failed Requests | Outcome |
|----------|----------|-----------------|---------|
| Standard deploy (no traffic) | 73–149s | 0 | ✅ Zero-downtime |
| Standard deploy (under load, 8GB Mac) | 217s | 69% | ❌ Rolled back safely |
| Pre-warm + fast deploy (under load) | 80s + 50s | 0 | ✅ Zero-downtime |

## Architecture

```
                         ┌──────────┐
       :80 ──────────────│  nginx   │
                         └────┬─────┘
                              │ upstream swap
                    ┌─────────┴────────┐
                    │                  │
              ┌─────┴─────┐      ┌─────┴─────┐
        :8000 │   blue    │:8001 │   green   │
              │ (active)  │      │ (standby) │
              └─────┬─────┘      └─────┬─────┘
                    └────────┬─────────┘
                             │ shared read-only
                      ┌──────┴──────┐
                      │models/*.gguf│  mmap page cache
                      └──────┬──────┘  sharing (~1GB saved)
                             │
                    ┌────────┴───────┐
                    │                │
             ┌──────┴──────┐  ┌──────┴──────┐
             │ Prometheus  │  │  Grafana    │
             │   :9090     │  │   :3000     │
             └─────────────┘  └─────────────┘
```

Both containers mount the same GGUF model file read-only. The kernel's mmap page cache shares the model's physical pages between blue and green, so running two containers uses ~3.2GB total instead of ~4.4GB.

## Quick Start

```bash
git clone https://github.com/youruser/zero-downtime-ml.git
cd zero-downtime-ml

# Download model, build image, start blue + nginx + monitoring
make setup

# Verify everything is healthy
make health

# Send a test chat request
make chat

# Run a zero-downtime deployment (blue → green)
make deploy
```

**Prerequisites:** Docker Desktop with 4GB memory limit (Settings → Resources → Memory), ~2GB free disk.

## How Deployment Works

### Standard Deploy (`make deploy`)

A 13-step automated pipeline with safety gates at every step:

```
 Step 1–3   Pre-flight checks (active healthy, nginx routing, disk space)
 Step 4     Start standby container
 Step 5     Poll /ready until model loaded (180s timeout)
 Step 6     Warm-up inference (real chat request)
 Step 7–9   Swap nginx config → test → reload
 Step 10    Drain period (15s for in-flight requests)
 Step 11    Verify traffic reaching new container
 Step 12    Stop old container
 Step 13    Update state file
```

If any step fails, the orchestrator rolls back automatically. Before nginx swap: just stop the new container. After nginx swap: restore config, reload, stop new container.

### Pre-Warm + Fast Deploy (`make prewarm` then `make deploy-fast`)

On 8GB hardware, the standard deploy causes CPU contention during model loading. The pre-warm strategy separates the expensive operation from the time-sensitive swap:

```
Step 1: make prewarm  (60–90s, no time pressure)
  Start standby → Load model → Warm-up inference → Mark ready
  Active container serves traffic normally throughout.

Step 2: make deploy-fast  (<30s)
  Verify standby healthy → Swap nginx → Drain → Stop old
  Model already loaded. No CPU contention.
```

## API

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/healthz` | Liveness probe — always 200 if process is alive |
| `GET` | `/ready` | Readiness probe — checks model loaded, inference working, memory OK |
| `GET` | `/health/deep` | Full diagnostic JSON (color, container ID, memory, uptime) |
| `POST` | `/chat` | Chat completion. Body: `{"message": "...", "max_tokens": 30}` |
| `POST` | `/chat/{user_id}` | Chat with user-scoped conversation history |
| `GET` | `/metrics` | Prometheus metrics (8 custom metrics, ML-optimized histogram buckets) |

## Memory Budget

| Component | Limit | Typical RSS | Notes |
|-----------|-------|-------------|-------|
| Blue container | 2GB | ~1.5–1.8GB | Includes mmap'd model pages in RSS |
| Green container | 2GB | ~1.5–1.8GB | Shares model pages via kernel page cache |
| nginx | 128MB | ~5MB | Config-driven routing, graceful reload |
| Prometheus | 256MB | ~50–100MB | 10s scrape interval, 24h retention |
| Grafana | 256MB | ~50–100MB | Auto-provisioned 6-panel dashboard |
| **Total (normal)** | | **~1.7GB** | Single container + monitoring |
| **Total (deploy)** | | **~3.2GB** | Both containers + monitoring (fits 4GB limit) |

## Observability

Grafana dashboard at `http://localhost:3000` (admin/admin) with six panels:

| Panel | What It Shows |
|-------|---------------|
| Request Rate | `rate(http_requests_total[1m])` — no dips during deployment |
| Error Rate | Percentage of 5xx responses — must stay at 0% during deploy |
| Request Latency | p50/p95/p99 with ML-optimized buckets (up to 60s) |
| Model Inference Time | Pure generation time, separate from HTTP overhead |
| Memory Usage | Per-container RSS — visual story of blue-green swap |
| Container Status | Which color is UP/DOWN |

Prometheus scrapes both containers every 10s. Green showing "down" during normal operation is expected — it means only the active container is running.

## Commands Reference

### Core Operations
```bash
make setup              # First-time setup (download model, build, start)
make up-full            # Start full stack (blue + nginx + Prometheus + Grafana)
make health             # Check all service health
make chat               # Send a test chat request
make deploy             # Full 13-step automated deployment
make rollback           # Emergency rollback to previous active
make state              # Show current deployment state
make history            # Show deployment history (last 20)
```

### Pre-Warm Mode (8GB Mac)
```bash
make prewarm            # Pre-warm standby container (no traffic impact)
make deploy-fast        # Fast swap using pre-warmed standby (<30s)
make prewarm-and-deploy # Both steps in sequence
make prewarm-status     # Check if standby is ready for fast deploy
```

### Blue-Green Manual Control
```bash
make start-green        # Start green container
make stop-green         # Stop green container
make swap-to-green      # Manually swap nginx to green
make swap-to-blue       # Manually swap nginx to blue
make which-active       # Show which upstream nginx is using
make mem                # Memory usage for all containers
```

### Monitoring
```bash
make grafana            # Open Grafana dashboard in browser
make prom-targets       # Check Prometheus scrape targets
make deploy-monitored   # Deploy with pre/post metric snapshots
make generate-traffic   # Send requests for 2 minutes (dashboard demo)
```

### Load Testing
```bash
make install-locust     # Install Locust (one-time, on host Mac)
make test-baseline      # Quick baseline test (90s, 3 users, no deployment)
make test-deploy-load   # Deployment under load (single terminal)
make test-prewarm       # Pre-warm + fast deploy with health monitoring
make report             # Generate markdown report from latest results
make open-report        # Open latest HTML report in browser
```

### Maintenance
```bash
make logs               # Follow active container logs
make logs-all           # Follow all container logs
make deploy-log         # View structured deployment log
make reset              # Tear down everything and reset to blue
make clean              # Remove all containers, volumes, images
make test               # Run integration tests
```

## Project Structure

```
zero-downtime-ml/
├── app/
│   ├── main.py              # FastAPI app with lifespan management
│   ├── model_manager.py     # Model lifecycle (load, inference, health)
│   ├── health.py            # Three-tier health endpoints
│   ├── metrics.py           # Prometheus metrics + middleware
│   ├── database.py          # SQLite WAL mode for chat history
│   └── logging_config.py    # Structured JSON logging
├── deploy/
│   ├── orchestrator.py      # 13-step deployment + prewarm + fast deploy
│   ├── rollback.py          # Emergency rollback (separate, simpler)
│   ├── state.json           # Deployment state + prewarm tracking
│   └── deploy.log           # Structured deployment log
├── nginx/
│   ├── nginx.conf           # Main nginx config
│   ├── conf.d/default.conf  # Active upstream (blue or green)
│   ├── upstream-blue.conf   # Blue upstream template
│   └── upstream-green.conf  # Green upstream template
├── monitoring/
│   ├── prometheus.yml       # Scrape config for both containers
│   └── grafana/             # Auto-provisioned datasource + dashboard
├── tests/
│   ├── test_health.py       # Unit/integration tests
│   ├── test_deployment.py   # Full deployment cycle test
│   ├── test_prewarm_deploy.sh  # Pre-warm + fast deploy test
│   ├── locustfile.py        # 4 Locust user classes
│   ├── deploy_under_load.sh # Automated deploy-under-load
│   ├── quick_baseline.sh    # Quick performance baseline
│   └── generate_report.py   # CSV → markdown report generator
├── models/                  # GGUF model file (git-ignored)
├── results/                 # Load test results (git-ignored)
├── docs/                    # Phase-by-phase documentation
├── Dockerfile               # Multi-stage build (286MB final image)
├── docker-compose.yml       # Full stack definition
├── Makefile                 # All commands
└── .env                     # Environment configuration
```

## Technical Decisions

**mmap model loading** — The kernel memory-maps the 1.2GB GGUF file instead of copying it to heap. This enables page cache sharing between containers (saving ~1GB) and makes warm deployments 2× faster than cold (73s vs 149s). Trade-off: RSS reporting includes file-backed pages, which inflated our readiness check until we added 500MB headroom to the threshold.

**Single Uvicorn worker** — The llama-cpp-python model object is not fork-safe. Multiple workers would each load a separate 1.2GB copy. A single async worker handles concurrent HTTP via asyncio while the inference bottleneck is in the C++ layer, not Python.

**Config-file nginx swap** — Traffic switching is done by copying an upstream template to `default.conf` and running `nginx -s reload`. Stateless, debuggable (`cat default.conf` shows current state), survives container restarts. Zero dropped requests validated across 52+ health checks during swap.

**Three-tier health checks** — `/healthz` (liveness, always 200), `/ready` (readiness, runs test inference), `/health/deep` (diagnostics, never fails). Follows Kubernetes conventions. The readiness check takes 2–5s because it runs actual inference — this caught issues where the model loaded but inference crashed.

**Extended Prometheus histogram buckets** — Default buckets stop at 10s. ML inference takes 5–10s, putting everything in the "+Inf" bucket. Extended to 60s for meaningful p95/p99 tracking.

## Deployment Modes

| Mode | Command | Duration | CPU Impact | Best For |
|------|---------|----------|------------|----------|
| Full deploy | `make deploy` | 73–149s | High during overlap | Production hardware (16GB+) |
| Pre-warm + fast | `make prewarm` → `make deploy-fast` | 60–90s + <30s | Low (separated) | 8GB Mac, scheduled deploys |
| Manual swap | `make swap-to-green` | <5s | None | Testing, debugging |
| Emergency rollback | `make rollback` | ~60s | Moderate | Disaster recovery |

## Known Constraints

**8GB Mac CPU contention** — When both containers run simultaneously under load, they compete for the 2 CPU cores allocated to Docker. The active container's inference latency spikes from 5s to 35–64s. The orchestrator detects this and rolls back safely. This is a hardware limitation, not an architectural flaw. On production hardware with 16GB+ RAM and 4+ cores, the overlap window has minimal impact. The pre-warm strategy (`make prewarm` + `make deploy-fast`) eliminates this issue entirely.

**Single-threaded inference** — SmolLM2-1.7B runs on CPU with a single Uvicorn worker. Peak throughput is ~0.1–0.2 inferences/sec. This is expected for CPU-only inference. GPU acceleration or model distillation would improve throughput but are out of scope.


## Technology Stack

| Component | Technology | Notes |
|-----------|-----------|-------|
| Model | SmolLM2-1.7B Instruct | INT4 quantized (Q4_K_M), 1.2GB GGUF |
| Runtime | llama-cpp-python | CPU-only, mmap-enabled |
| Framework | FastAPI + Uvicorn | Async, single worker |
| Proxy | nginx | Config-file routing, graceful reload |
| Containers | Docker Compose | Multi-stage builds, 4GB limit |
| Metrics | Prometheus | 10s scrape, 24h retention |
| Dashboards | Grafana | 6-panel auto-provisioned |
| Load Testing | Locust | 4 user classes, HTML/CSV reports |
| Database | SQLite | WAL mode for concurrent readers |
| Logging | Structured JSON | stdout, aggregation-ready |

