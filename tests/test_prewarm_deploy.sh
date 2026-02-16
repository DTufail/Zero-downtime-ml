#!/bin/bash
# test_prewarm_deploy.sh - Test the prewarm + fast-deploy workflow
# Proves zero-downtime with separated model loading and traffic swap
#
# Usage: bash tests/test_prewarm_deploy.sh

set -e

RESULTS_DIR="results/prewarm_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$RESULTS_DIR"

echo "═══════════════════════════════════════════════════════════"
echo "  PRE-WARM + FAST DEPLOY TEST"
echo "  Results: $RESULTS_DIR"
echo "═══════════════════════════════════════════════════════════"
echo ""

# ─── Verify Prerequisites ───
echo "🔍 Checking prerequisites..."
HTTP=$(curl -s -o /dev/null -w "%{http_code}" http://localhost/healthz 2>/dev/null || echo "000")
if [[ "$HTTP" != "200" ]]; then
    echo "❌ Service not healthy. Run 'make up-full' first."
    exit 1
fi
echo "  ✓ Active container healthy"
echo ""

# ─── Record initial state ───
echo "📊 Initial state:"
python3 deploy/orchestrator.py status --project-root .
echo ""

ACTIVE_BEFORE=$(python3 -c "import json; print(json.load(open('deploy/state.json'))['active_color'])")
echo "  Active color: $ACTIVE_BEFORE"
echo ""

# ─── Step 1: Pre-warm ───
echo "═══════════════════════════════════════════════════════════"
echo "  STEP 1: PRE-WARM STANDBY"
echo "═══════════════════════════════════════════════════════════"
echo ""

PREWARM_START=$(date +%s)
python3 deploy/orchestrator.py prewarm --project-root . 2>&1 | tee "$RESULTS_DIR/prewarm_output.log"
PREWARM_EXIT=$?
PREWARM_END=$(date +%s)
PREWARM_DURATION=$((PREWARM_END - PREWARM_START))

if [[ $PREWARM_EXIT -ne 0 ]]; then
    echo "❌ Pre-warm failed (exit code $PREWARM_EXIT)"
    exit 1
fi
echo ""
echo "✓ Pre-warm completed in ${PREWARM_DURATION}s"
echo ""

# ─── Verify both containers are running ───
echo "📊 Memory with both containers running:"
docker stats --no-stream --format "  {{.Name}}: {{.MemUsage}} ({{.CPUPerc}} CPU)" 2>/dev/null | grep smollm2
echo ""

# ─── Step 2: Start continuous health check monitor ───
echo "═══════════════════════════════════════════════════════════"
echo "  STEP 2: FAST DEPLOY WITH CONTINUOUS MONITORING"
echo "═══════════════════════════════════════════════════════════"
echo ""

# Start health check monitor in background
echo "🔍 Starting continuous health check monitor..."
MONITOR_LOG="$RESULTS_DIR/health_monitor.log"
> "$MONITOR_LOG"

(
    while true; do
        TIMESTAMP=$(date +%H:%M:%S)
        HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 http://localhost/healthz 2>/dev/null || echo "000")
        if [[ "$HTTP_CODE" == "200" ]]; then
            echo "✓ [$TIMESTAMP] HTTP $HTTP_CODE" >> "$MONITOR_LOG"
        else
            echo "❌ [$TIMESTAMP] HTTP $HTTP_CODE" >> "$MONITOR_LOG"
        fi
        sleep 0.5
    done
) &
MONITOR_PID=$!

# Wait a moment for monitor to start
sleep 2

# ─── Step 3: Run fast deploy ───
echo "🚀 Running fast deploy..."
DEPLOY_START=$(date +%s)
python3 deploy/orchestrator.py deploy-fast --project-root . 2>&1 | tee "$RESULTS_DIR/deploy_fast_output.log"
DEPLOY_EXIT=$?
DEPLOY_END=$(date +%s)
DEPLOY_DURATION=$((DEPLOY_END - DEPLOY_START))

# Let monitor run a few more seconds
sleep 5

# Stop monitor
kill $MONITOR_PID 2>/dev/null
wait $MONITOR_PID 2>/dev/null

# ─── Analyze results ───
echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  RESULTS"
echo "═══════════════════════════════════════════════════════════"
echo ""

TOTAL=$(wc -l < "$MONITOR_LOG" | tr -d ' ')
SUCCESS=$(grep -c "✓" "$MONITOR_LOG" || echo "0")
FAILURE=$(grep -c "❌" "$MONITOR_LOG" || echo "0")

echo "  Pre-warm duration:    ${PREWARM_DURATION}s"
echo "  Fast deploy duration: ${DEPLOY_DURATION}s"
echo "  Deploy result:        $(if [[ $DEPLOY_EXIT -eq 0 ]]; then echo '✓ SUCCESS'; else echo '❌ FAILED'; fi)"
echo ""
echo "  Health check results during fast deploy:"
echo "    Total requests:  $TOTAL"
echo "    Successful:      $SUCCESS"
echo "    Failed:          $FAILURE"
echo ""

if [[ $FAILURE -eq 0 && $DEPLOY_EXIT -eq 0 ]]; then
    echo "  🎉 ZERO-DOWNTIME CONFIRMED with pre-warm strategy!"
    echo "  Fast deploy completed in ${DEPLOY_DURATION}s (vs ~73-149s for full deploy)"
else
    if [[ $DEPLOY_EXIT -ne 0 ]]; then
        echo "  ⚠️  Fast deploy failed. Check: $RESULTS_DIR/deploy_fast_output.log"
    fi
    if [[ $FAILURE -gt 0 ]]; then
        echo "  ⚠️  $FAILURE health checks failed during deploy"
        echo "  Failed requests:"
        grep "❌" "$MONITOR_LOG" | head -5
    fi
fi

# ─── Verify final state ───
echo ""
echo "📊 Final state:"
python3 deploy/orchestrator.py status --project-root .

ACTIVE_AFTER=$(python3 -c "import json; print(json.load(open('deploy/state.json'))['active_color'])")
echo ""
echo "  Color swap: $ACTIVE_BEFORE → $ACTIVE_AFTER"

if [[ "$ACTIVE_BEFORE" != "$ACTIVE_AFTER" && $DEPLOY_EXIT -eq 0 ]]; then
    echo "  ✓ Active color changed correctly"
else
    echo "  ❌ Active color did not change (deploy may have failed)"
fi

echo ""
echo "  Logs: $RESULTS_DIR/"
echo "═══════════════════════════════════════════════════════════"
