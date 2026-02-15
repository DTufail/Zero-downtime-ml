#!/bin/bash
# quick_baseline.sh - Quick baseline performance test (no deployment)
# Runs for 90 seconds with 3 users, produces a single report
#
# Usage: bash tests/quick_baseline.sh

set -e

RESULTS_DIR="results/quick_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$RESULTS_DIR"

echo "Quick baseline test (90s, 3 users)"
echo "Results: $RESULTS_DIR"
echo ""

# Verify service is up
HTTP=$(curl -s -o /dev/null -w "%{http_code}" http://localhost/healthz 2>/dev/null || echo "000")
if [[ "$HTTP" != "200" ]]; then
    echo "❌ Service not healthy. Run 'make up-full' first."
    exit 1
fi

locust -f tests/locustfile.py \
    --headless \
    --host http://localhost \
    -u 3 \
    -r 1 \
    -t 90s \
    --csv "$RESULTS_DIR/baseline" \
    --csv-full-history \
    --html "$RESULTS_DIR/baseline_report.html" \
    HealthCheckUser InferenceUser

echo ""
echo "Results:"
python3 -c "
import csv
with open('$RESULTS_DIR/baseline_stats.csv') as f:
    reader = csv.DictReader(f)
    for row in reader:
        name = row['Name']
        total = int(row['Request Count'])
        fails = int(row['Failure Count'])
        avg = row['Average Response Time']
        p95 = row['95%']
        if total > 0:
            print(f'  {name}: {total} requests, {fails} failed, avg={avg}ms, p95={p95}ms')
"

echo ""
echo "HTML report: $RESULTS_DIR/baseline_report.html"
