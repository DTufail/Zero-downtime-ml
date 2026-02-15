#!/usr/bin/env bash
set -euo pipefail

# Continuously hits /healthz via nginx during a swap and counts failures.
# Usage: ./tests/test_zero_downtime.sh
# Run this in one terminal, then swap upstream in another.

URL="http://localhost:80/healthz"
DURATION=30
INTERVAL=0.5

echo "=== Zero-Downtime Test ==="
echo "Hitting $URL every ${INTERVAL}s for ${DURATION}s"
echo "Run 'make swap-to-green' or 'make swap-to-blue' in another terminal during this test"
echo ""

TOTAL=0
OK=0
FAIL=0
START=$(date +%s)

while true; do
    NOW=$(date +%s)
    ELAPSED=$((NOW - START))
    if [[ $ELAPSED -ge $DURATION ]]; then
        break
    fi

    TOTAL=$((TOTAL + 1))
    HTTP_CODE=$(curl -sf -o /dev/null -w "%{http_code}" "$URL" 2>/dev/null || echo "000")

    if [[ "$HTTP_CODE" == "200" ]]; then
        OK=$((OK + 1))
        echo "[${ELAPSED}s] request #${TOTAL}: ${HTTP_CODE} OK"
    else
        FAIL=$((FAIL + 1))
        echo "[${ELAPSED}s] request #${TOTAL}: ${HTTP_CODE} FAIL <---"
    fi

    sleep "$INTERVAL"
done

echo ""
echo "=== Results ==="
echo "Total requests: $TOTAL"
echo "  Success: $OK"
echo "  Failed:  $FAIL"

if [[ $FAIL -eq 0 ]]; then
    echo ""
    echo "PASS: Zero dropped requests during test window"
else
    echo ""
    echo "FAIL: $FAIL requests dropped"
    exit 1
fi
