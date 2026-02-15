#!/usr/bin/env bash
set -euo pipefail

# Sends chat requests via nginx during a swap to verify inference continuity.
# Usage: ./tests/test_chat_during_swap.sh
# Run this in one terminal, then swap upstream in another.

URL="http://localhost:80/chat"
COUNT=6
DELAY=5

echo "=== Chat During Swap Test ==="
echo "Sending $COUNT chat requests via nginx (${DELAY}s apart)"
echo "Run 'make swap-to-green' or 'make swap-to-blue' in another terminal during this test"
echo ""

OK=0
FAIL=0

for i in $(seq 1 "$COUNT"); do
    echo "--- Request $i/$COUNT ---"
    START_MS=$(python3 -c "import time; print(int(time.time()*1000))")

    RESPONSE=$(curl -sf -X POST "$URL" \
        -H "Content-Type: application/json" \
        -d '{"message": "Say hello in one word.", "user_id": "swap-test"}' \
        2>/dev/null) || RESPONSE=""

    END_MS=$(python3 -c "import time; print(int(time.time()*1000))")
    LATENCY=$(( END_MS - START_MS ))

    if [[ -n "$RESPONSE" ]]; then
        REPLY=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('response','')[:80])" 2>/dev/null || echo "parse error")
        echo "  OK (${LATENCY}ms): $REPLY"
        OK=$((OK + 1))
    else
        echo "  FAIL (${LATENCY}ms): no response"
        FAIL=$((FAIL + 1))
    fi

    if [[ $i -lt $COUNT ]]; then
        sleep "$DELAY"
    fi
done

echo ""
echo "=== Results ==="
echo "Total: $COUNT  Success: $OK  Failed: $FAIL"

if [[ $FAIL -eq 0 ]]; then
    echo "PASS: All chat requests succeeded during test window"
else
    echo "FAIL: $FAIL chat requests failed"
    exit 1
fi
