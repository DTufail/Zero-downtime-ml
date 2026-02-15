# Zero-Downtime ML Model Deployment

A system for serving SmolLM2-1.7B via FastAPI with zero-downtime blue/green deployments, optimized for 8GB Mac environments. Uses GGUF INT4 quantization with llama-cpp-python for minimal memory footprint.

## Prerequisites

- **Docker Desktop** with memory limit set to 4GB (Settings > Resources > Memory)
- **~2GB free disk** for the GGUF model file and Docker image

## Quick Start

```bash
# Download model, build image, and start the stack (blue + nginx)
make setup

# Check that everything is healthy
make health

# Send a test chat message
make chat

# Run an automated zero-downtime deployment
make deploy
```

## Architecture

```
                    ┌──────────┐
  :80 ──────────────│  nginx   │
                    └────┬─────┘
                         │  (upstream swap)
              ┌──────────┴──────────┐
              │                     │
        ┌─────┴─────┐        ┌─────┴─────┐
  :8000 │   blue     │  :8001 │   green    │
        │ (active)   │        │ (standby)  │
        └────────────┘        └────────────┘
              │                     │
              └──────┬──────────────┘
                     │  (shared read-only)
              ┌──────┴──────┐
              │ models/*.gguf│
              └─────────────┘
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/healthz` | Liveness probe (always 200 if running) |
| GET | `/ready` | Readiness probe (checks model + memory) |
| GET | `/health/deep` | Full diagnostic JSON (includes `color` and `container_id`) |
| POST | `/chat` | Send a chat message |
| POST | `/chat/{user_id}` | Chat with user-scoped history |
| GET | `/metrics` | Prometheus metrics |

## Memory Budget (8GB Mac, 4GB Docker)

| Component | mem_limit | Typical RSS |
|-----------|-----------|-------------|
| Blue container | 2GB | ~1.5-1.8GB |
| Green container | 2GB | ~1.5-1.8GB |
| nginx | 128MB | ~5MB |
| Prometheus | 256MB | ~50-100MB |
| Grafana | 256MB | ~50-100MB |
| **mmap sharing** | — | Kernel reuses model pages |

The GGUF model file is mounted read-only (`./models:/models:ro`) into both containers. macOS's unified memory + mmap means the kernel page cache shares the model's physical pages between blue and green, so running two containers does **not** double the model memory.

## Commands Reference

### Phase 1 (single container)
```bash
make run             # Start blue + nginx
make stop            # Stop all containers
make logs            # Follow blue container logs
make health          # Check all health endpoints (direct :8000)
make chat            # Send a test chat message
make memory          # Show blue container memory
make test            # Run integration tests
make clean           # Remove all containers, volumes, images
```

### Phase 2 (blue/green manual)
```bash
make up              # Start blue + nginx
make start-green     # Start green container
make stop-green      # Stop green container
make swap-to-green   # Swap nginx to green
make swap-to-blue    # Swap nginx to blue
make which-active    # Show which upstream is active
make demo-swap       # Full automated swap demo
make test-zero-downtime  # Health check continuity test
make test-inference  # Chat continuity test during swap
make mem             # Memory usage for all containers
make logs-all        # Follow all container logs
make reset           # Tear down everything and reset to blue
```

### Phase 3 (automated deployment)
```bash
make deploy          # Run automated 13-step deployment
make state           # Show current deployment state
make history         # Show deployment history
make rollback        # Rollback to previous active
make rollback-blue   # Force rollback to blue
make rollback-green  # Force rollback to green
make test-deploy     # Run full cycle integration test (blue->green->blue)
make test-full-cycle # Deploy -> verify -> deploy back -> verify
make deploy-log      # View structured deployment log
```

### Phase 4 (observability)
```bash
make up-full         # Start everything including Prometheus + Grafana
make prom-targets    # Check Prometheus scrape targets
make prom-check      # Full Prometheus health + sample metrics
make grafana         # Open Grafana dashboard in browser
make deploy-monitored # Deploy with pre/post metric snapshots
make generate-traffic # Send requests for 2 min (dashboard demo)
```

### Phase 5 (load testing)
```bash
make install-locust   # Install Locust (one-time setup)
make test-baseline    # Quick baseline test (90s, no deployment)
make test-deploy-load # Automated deployment-under-load test
make report           # Generate markdown report from latest results
make open-report      # Open latest HTML report in browser
make results          # List all test results
```

## Documentation

Detailed write-ups for each phase live in `docs/`:

- [Phase 1: Single Container ML Inference](docs/phase1-single-container.md)
- [Phase 2: Blue/Green Deployment with Nginx](docs/phase2-blue-green-deployment.md)
- [Phase 3: Automated Deployment Orchestrator](docs/phase3-automated-deployment.md)
- [Memory/mmap Gotcha](docs/memory-mmap-gotcha.md)
- [Phase 4: Observability with Prometheus + Grafana](docs/phase4-observability.md)
- [Phase 5: Load Testing with Locust](docs/phase5-load-testing.md)

## Roadmap

- **Phase 1** (complete): Single container serving SmolLM2-1.7B
- **Phase 2** (complete): Nginx reverse proxy + blue/green swap
- **Phase 3** (complete): Automated deployment orchestrator with rollback
- **Phase 4** (complete): Prometheus + Grafana monitoring
- **Phase 5** (complete): Load testing with Locust + performance profiling
- **Phase 6**: CI/CD pipeline
