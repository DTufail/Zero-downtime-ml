#!/usr/bin/env bash
set -euo pipefail

# Usage: ./deploy/swap_upstream.sh [blue|green]
# Swaps nginx upstream to the specified color

TARGET="${1:-}"
if [[ -z "$TARGET" ]]; then
    echo "Usage: $0 [blue|green]"
    exit 1
fi

if [[ "$TARGET" != "blue" && "$TARGET" != "green" ]]; then
    echo "Error: target must be 'blue' or 'green', got '$TARGET'"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="$SCRIPT_DIR/nginx/upstream-${TARGET}.conf"
ACTIVE="$SCRIPT_DIR/nginx/conf.d/default.conf"
CONTAINER="smollm2-${TARGET}"
NGINX_CONTAINER="smollm2-nginx"

echo "=== Swap upstream to $TARGET ==="

# 1. Check that target container is running and healthy
echo "[1/4] Checking $CONTAINER is healthy..."
HEALTH=$(docker inspect --format='{{.State.Health.Status}}' "$CONTAINER" 2>/dev/null || echo "not_found")
if [[ "$HEALTH" != "healthy" ]]; then
    echo "Error: $CONTAINER is not healthy (status: $HEALTH)"
    echo "Start it first: docker compose --profile deploy up -d $TARGET"
    exit 1
fi

# 2. Check readiness endpoint
echo "[2/4] Checking readiness endpoint..."
PORT_MAP=("blue:8000" "green:8001")
for pair in "${PORT_MAP[@]}"; do
    color="${pair%%:*}"
    port="${pair##*:}"
    if [[ "$color" == "$TARGET" ]]; then
        READY=$(curl -sf "http://localhost:${port}/ready" 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || echo "failed")
        if [[ "$READY" != "ready" ]]; then
            echo "Error: $CONTAINER /ready returned '$READY'"
            exit 1
        fi
        break
    fi
done
echo "  $CONTAINER is ready"

# 3. Swap the config
echo "[3/4] Swapping upstream config..."
cp "$TEMPLATE" "$ACTIVE"
echo "  Copied upstream-${TARGET}.conf → conf.d/default.conf"

# 4. Reload nginx
echo "[4/4] Reloading nginx..."
docker exec "$NGINX_CONTAINER" nginx -s reload
sleep 1

# Verify nginx is serving through new upstream
VERIFY=$(curl -sf http://localhost:80/healthz 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status',''))" 2>/dev/null || echo "failed")
if [[ "$VERIFY" == "ok" ]]; then
    echo ""
    echo "=== Success: traffic now routes to $TARGET ==="
else
    echo ""
    echo "Warning: nginx reload may have failed. Check: curl http://localhost:80/healthz"
fi
