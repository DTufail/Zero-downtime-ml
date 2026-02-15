#!/bin/bash
# deploy_under_load.sh - Automated deployment-under-load test
# Starts Locust in background, triggers deployment, waits for completion, stops Locust
#
# Usage: bash tests/deploy_under_load.sh
#
# This script handles everything in ONE terminal (no need for two terminals)

set -e

RESULTS_DIR="results/deploy_load_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$RESULTS_DIR"

echo "═══════════════════════════════════════════════════"
echo "  DEPLOYMENT-UNDER-LOAD TEST"
echo "  Results: $RESULTS_DIR"
echo "═══════════════════════════════════════════════════"
echo ""

# Verify service is up
HTTP=$(curl -s -o /dev/null -w "%{http_code}" http://localhost/healthz 2>/dev/null || echo "000")
if [[ "$HTTP" != "200" ]]; then
    echo "❌ Service not healthy. Run 'make up-full' first."
    exit 1
fi

# Record pre-deployment state
echo "📊 Pre-deployment state:"
docker stats --no-stream --format "  {{.Name}}: {{.MemUsage}} ({{.CPUPerc}} CPU)" 2>/dev/null | grep smollm2
echo ""

# Step 1: Start Locust in background
echo "🔄 Starting load generator (background)..."
locust -f tests/locustfile.py \
    --headless \
    --host http://localhost \
    -u 3 \
    -r 1 \
    -t 5m \
    --csv "$RESULTS_DIR/during_deploy" \
    --csv-full-history \
    --html "$RESULTS_DIR/during_deploy_report.html" \
    HealthCheckUser InferenceUser \
    > "$RESULTS_DIR/locust_output.log" 2>&1 &

LOCUST_PID=$!
echo "  Locust PID: $LOCUST_PID"

# Wait for Locust to ramp up
echo "  Waiting 15s for traffic to stabilize..."
sleep 15

# Step 2: Trigger deployment
echo ""
echo "🚀 Starting deployment..."
DEPLOY_START=$(date +%s)

python3 deploy/orchestrator.py deploy --project-root . 2>&1 | tee "$RESULTS_DIR/deploy_output.log"
DEPLOY_EXIT=${PIPESTATUS[0]}

DEPLOY_END=$(date +%s)
DEPLOY_DURATION=$((DEPLOY_END - DEPLOY_START))

if [[ $DEPLOY_EXIT -eq 0 ]]; then
    echo ""
    echo "✅ Deployment succeeded in ${DEPLOY_DURATION}s"
else
    echo ""
    echo "⚠️  Deployment failed/rolled back (exit code $DEPLOY_EXIT) in ${DEPLOY_DURATION}s"
fi

# Step 3: Let traffic continue for 30s after deployment
echo ""
echo "⏳ Continuing load for 30s post-deployment..."
sleep 30

# Step 4: Stop Locust
echo ""
echo "🛑 Stopping load generator..."
kill $LOCUST_PID 2>/dev/null || true
wait $LOCUST_PID 2>/dev/null || true

# Step 5: Post-deployment state
echo ""
echo "📊 Post-deployment state:"
docker stats --no-stream --format "  {{.Name}}: {{.MemUsage}} ({{.CPUPerc}} CPU)" 2>/dev/null | grep smollm2

# Step 6: Parse results
echo ""
echo "═══════════════════════════════════════════════════"
echo "  RESULTS"
echo "═══════════════════════════════════════════════════"

if [[ -f "$RESULTS_DIR/during_deploy_stats.csv" ]]; then
    python3 -c "
import csv
with open('$RESULTS_DIR/during_deploy_stats.csv') as f:
    reader = csv.DictReader(f)
    for row in reader:
        name = row['Name']
        total = int(row['Request Count'])
        fails = int(row['Failure Count'])
        avg = row['Average Response Time']
        p95 = row['95%']
        p99 = row['99%']
        if total > 0:
            fail_pct = 100 * fails / total
            print(f'  {name}:')
            print(f'    Requests: {total}, Failed: {fails} ({fail_pct:.1f}%)')
            print(f'    Latency: avg={avg}ms, p95={p95}ms, p99={p99}ms')
"
else
    echo "  ⚠️  No CSV results (Locust may have been interrupted)"
    echo "  Check: $RESULTS_DIR/locust_output.log"
fi

echo ""
echo "  Deployment duration: ${DEPLOY_DURATION}s"
echo "  Deployment result:   $(if [[ $DEPLOY_EXIT -eq 0 ]]; then echo 'SUCCESS'; else echo 'ROLLED BACK'; fi)"
echo ""
echo "  HTML report: $RESULTS_DIR/during_deploy_report.html"
echo "  Deploy log:  $RESULTS_DIR/deploy_output.log"
echo "  Locust log:  $RESULTS_DIR/locust_output.log"
echo "═══════════════════════════════════════════════════"
