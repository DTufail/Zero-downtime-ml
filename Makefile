.PHONY: download-model build run stop logs health chat memory test clean setup \
       up start-green stop-green swap-to-green swap-to-blue which-active \
       demo-swap test-zero-downtime test-inference mem logs-all reset \
       deploy state history rollback rollback-blue rollback-green \
       test-deploy test-full-cycle deploy-log \
       up-full prom-targets prom-check grafana deploy-monitored generate-traffic \
       install-locust test-baseline test-deploy-load report open-report results \
       prewarm deploy-fast prewarm-and-deploy test-prewarm prewarm-status

# === Phase 1: Single container ===

download-model:
	bash models/download_model.sh ./models

build:
	docker compose build

setup: download-model build run

run:
	docker compose up -d blue
	@echo "Waiting for blue to be healthy..."
	@docker compose up -d nginx

stop:
	docker compose --profile deploy down

logs:
	docker compose logs -f blue

health:
	@echo "=== Liveness ===" && curl -s http://localhost:8000/healthz | python3 -m json.tool
	@echo "\n=== Readiness ===" && curl -s http://localhost:8000/ready | python3 -m json.tool
	@echo "\n=== Deep Health ===" && curl -s http://localhost:8000/health/deep | python3 -m json.tool

chat:
	@curl -s -X POST http://localhost:8000/chat \
		-H "Content-Type: application/json" \
		-d '{"message": "Explain what zero-downtime deployment means in one sentence.", "user_id": "test"}' \
		| python3 -m json.tool

memory:
	docker stats --no-stream smollm2-blue

test:
	python3 -m pytest tests/test_health.py -v

clean:
	docker compose --profile deploy down -v --rmi all

# === Phase 2: Blue/Green with nginx ===

up:
	docker compose up -d blue
	@echo "Waiting for blue to be healthy..."
	@docker compose up -d nginx
	@echo "Stack is up. Blue active on port 80."

start-green:
	docker compose --profile deploy up -d green
	@echo "Green container starting... watch with: docker logs -f smollm2-green"

stop-green:
	docker compose stop green

swap-to-green:
	bash deploy/swap_upstream.sh green

swap-to-blue:
	bash deploy/swap_upstream.sh blue

which-active:
	@echo "=== Active upstream ===" && head -2 nginx/conf.d/default.conf
	@echo "\n=== Via nginx (port 80) ===" && curl -s http://localhost:80/healthz | python3 -m json.tool

demo-swap:
	@echo "=== Phase 2 Demo: Blue/Green Swap ==="
	@echo ""
	@echo "Step 1: Start green container"
	$(MAKE) start-green
	@echo ""
	@echo "Waiting 90s for green to load model and become healthy..."
	@sleep 90
	@echo ""
	@echo "Step 2: Verify green is healthy"
	@curl -s http://localhost:8001/ready | python3 -m json.tool
	@echo ""
	@echo "Step 3: Swap traffic to green"
	$(MAKE) swap-to-green
	@echo ""
	@echo "Step 4: Verify nginx routes to green"
	@curl -s http://localhost:80/healthz | python3 -m json.tool
	@echo ""
	@echo "Step 5: Chat via nginx"
	@curl -s -X POST http://localhost:80/chat \
		-H "Content-Type: application/json" \
		-d '{"message": "Say hello!", "user_id": "demo"}' \
		| python3 -m json.tool
	@echo ""
	@echo "Step 6: Swap back to blue"
	$(MAKE) swap-to-blue
	@echo ""
	@echo "=== Demo complete ==="

test-zero-downtime:
	bash tests/test_zero_downtime.sh

test-inference:
	bash tests/test_chat_during_swap.sh

mem:
	docker stats --no-stream --format "table {{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}" smollm2-blue smollm2-green smollm2-nginx 2>/dev/null || \
	docker stats --no-stream --format "table {{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}" smollm2-blue smollm2-nginx

logs-all:
	docker compose --profile deploy logs -f

reset:
	docker compose --profile deploy down -v
	cp nginx/upstream-blue.conf nginx/conf.d/default.conf
	@echo "Reset complete. Run 'make up' to start fresh."

# === Phase 3: Automated Deployment ===

deploy:
	python3 deploy/orchestrator.py deploy --project-root .

state:
	python3 deploy/orchestrator.py status --project-root .

history:
	python3 deploy/orchestrator.py history --project-root .

rollback:
	python3 deploy/rollback.py --project-root .

rollback-blue:
	python3 deploy/rollback.py --to blue --project-root .

rollback-green:
	python3 deploy/rollback.py --to green --project-root .

test-deploy:
	python3 tests/test_deployment.py

test-full-cycle:
	@echo "=== Starting full deployment cycle test ==="
	@echo "Step 1: Verify current state..."
	$(MAKE) state
	@echo ""
	@echo "Step 2: Deploy (swap to standby)..."
	$(MAKE) deploy
	@echo ""
	@echo "Step 3: Verify new state..."
	$(MAKE) state
	@echo ""
	@echo "Step 4: Deploy again (swap back)..."
	$(MAKE) deploy
	@echo ""
	@echo "Step 5: Verify final state..."
	$(MAKE) state
	@echo ""
	@echo "=== Full cycle complete ==="

deploy-log:
	cat deploy/deploy.log 2>/dev/null || echo "No deployment log found"

# === Phase 4: Observability ===

up-full:
	docker compose up -d
	@echo "Waiting for services..."
	@sleep 10
	@echo ""
	@echo "Services:"
	@echo "  App (via nginx): http://localhost"
	@echo "  Blue (direct):   http://localhost:8000"
	@echo "  Prometheus:      http://localhost:9090"
	@echo "  Grafana:         http://localhost:3000 (admin/admin)"
	@echo ""
	@echo "Checking health..."
	@curl -s http://localhost/healthz | python3 -m json.tool 2>/dev/null || echo "App: still loading..."
	@curl -s -o /dev/null -w "Prometheus: HTTP %{http_code}\n" http://localhost:9090/-/healthy 2>/dev/null || echo "Prometheus: starting..."
	@curl -s -o /dev/null -w "Grafana: HTTP %{http_code}\n" http://localhost:3000/api/health 2>/dev/null || echo "Grafana: starting..."

prom-targets:
	@curl -s http://localhost:9090/api/v1/targets | python3 -c "import sys,json; data=json.load(sys.stdin); [print(f\"  {t['labels']['job']}: {t['health']}\") for t in data['data']['activeTargets']]" 2>/dev/null || echo "Prometheus not reachable"

prom-check:
	@echo "=== Prometheus Target Health ==="
	@make prom-targets
	@echo ""
	@echo "=== Sample Metrics ==="
	@echo "Request count:"
	@curl -s "http://localhost:9090/api/v1/query?query=http_requests_total" | python3 -c "import sys,json; data=json.load(sys.stdin); print(f\"  Results: {len(data['data']['result'])} series\")" 2>/dev/null || echo "  Not available"
	@echo "Memory usage:"
	@curl -s "http://localhost:9090/api/v1/query?query=memory_usage_bytes" | python3 -c "import sys,json; data=json.load(sys.stdin); [print(f\"  {r['metric'].get('job','?')}: {int(float(r['value'][1]))/1024/1024:.0f} MB\") for r in data['data']['result']]" 2>/dev/null || echo "  Not available"
	@echo "Model loaded:"
	@curl -s "http://localhost:9090/api/v1/query?query=model_loaded" | python3 -c "import sys,json; data=json.load(sys.stdin); [print(f\"  {r['metric'].get('job','?')}: {'YES' if r['value'][1]=='1' else 'NO'}\") for r in data['data']['result']]" 2>/dev/null || echo "  Not available"

grafana:
	@open http://localhost:3000/d/ml-deployment-monitor/ml-deployment-monitor 2>/dev/null || echo "Open http://localhost:3000 in your browser (admin/admin)"

deploy-monitored:
	@echo "=== Pre-deployment metrics ==="
	@make prom-check
	@echo ""
	make deploy
	@echo ""
	@echo "=== Post-deployment metrics ==="
	@sleep 15
	@make prom-check

generate-traffic:
	@echo "Generating traffic for 2 minutes (1 request/second)..."
	@for i in $$(seq 1 120); do \
		curl -s -X POST http://localhost/chat \
			-H "Content-Type: application/json" \
			-d "{\"message\": \"Count to $$i\", \"max_tokens\": 10}" \
			-o /dev/null -w "Request $$i: HTTP %{http_code} (%{time_total}s)\n"; \
		sleep 1; \
	done
	@echo "Traffic generation complete."

# === Phase 5: Load Testing ===

install-locust:
	pip3 install locust --break-system-packages 2>/dev/null || pip3 install locust

test-baseline:
	bash tests/quick_baseline.sh

test-deploy-load:
	bash tests/deploy_under_load.sh

report:
	@LATEST=$$(ls -td results/*/ 2>/dev/null | head -1); \
	if [ -z "$$LATEST" ]; then echo "No results found. Run tests first."; exit 1; fi; \
	echo "Generating report from: $$LATEST"; \
	python3 tests/generate_report.py "$$LATEST"

open-report:
	@LATEST=$$(ls -t results/*/*.html 2>/dev/null | head -1); \
	if [ -z "$$LATEST" ]; then echo "No reports found."; exit 1; fi; \
	echo "Opening: $$LATEST"; \
	open "$$LATEST" 2>/dev/null || echo "Open $$LATEST in your browser"

results:
	@echo "Test results:"
	@ls -la results/*/report.md results/*/*.html 2>/dev/null || echo "  No results yet. Run 'make test-baseline' first."

# === Phase 6: Pre-Warm Standby Mode ===

# Pre-warm the standby container (no traffic impact)
prewarm:
	python3 deploy/orchestrator.py prewarm --project-root .

# Fast deploy using pre-warmed standby (<30s)
deploy-fast:
	python3 deploy/orchestrator.py deploy-fast --project-root .

# Full prewarm + fast deploy workflow
prewarm-and-deploy:
	@echo "─── Phase 1: Pre-warming standby ───"
	make prewarm
	@echo ""
	@echo "─── Phase 2: Fast deploy ───"
	make deploy-fast

# Test the prewarm + fast deploy workflow with monitoring
test-prewarm:
	bash tests/test_prewarm_deploy.sh

# Check if standby is pre-warmed and ready
prewarm-status:
	@python3 -c "import json; s = json.load(open('deploy/state.json')); pw = s.get('standby_prewarmed', False); color = s.get('standby_color', '?'); at = s.get('standby_prewarmed_at', 'never'); print(f'Standby: {color}'); print(f'Pre-warmed: {\"YES\" if pw else \"NO\"}'); print(f'Pre-warmed at: {at}'); print(f'Ready for: make deploy-fast' if pw else 'Run: make prewarm')"

