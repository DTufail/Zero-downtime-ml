#!/usr/bin/env python3
"""
Emergency Rollback Script

Reads the current state, swaps nginx back to the previously active container,
and restarts it if needed. Use this when the orchestrator's built-in rollback
didn't work or when you need a manual escape hatch.

Usage:
    python deploy/rollback.py
    python deploy/rollback.py --to blue
    python deploy/rollback.py --to green
"""

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def run(cmd, timeout=30, check=True):
    """Run a shell command and return the result."""
    print(f"  $ {cmd}")
    result = subprocess.run(
        cmd.split(), capture_output=True, text=True, timeout=timeout
    )
    if check and result.returncode != 0:
        print(f"  FAILED (rc={result.returncode}): {result.stderr.strip()}")
        if check:
            raise RuntimeError(f"Command failed: {cmd}")
    return result


def main():
    parser = argparse.ArgumentParser(description="Emergency Rollback")
    parser.add_argument(
        "--to",
        choices=["blue", "green"],
        help="Rollback to specific color (default: previous standby)",
    )
    parser.add_argument(
        "--project-root",
        default=".",
        help="Path to project root",
    )
    args = parser.parse_args()

    root = Path(args.project_root).resolve()
    state_file = root / "deploy" / "state.json"
    nginx_conf = root / "nginx" / "conf.d" / "default.conf"

    # 1. Read state
    if state_file.exists():
        with open(state_file) as f:
            state = json.load(f)
    else:
        print("ERROR: No state file found at deploy/state.json")
        print("Manual fix: Copy the correct upstream config and reload nginx:")
        print("  cp nginx/upstream-blue.conf nginx/conf.d/default.conf")
        print("  docker exec smollm2-nginx nginx -s reload")
        sys.exit(1)

    # 2. Determine target
    if args.to:
        target = args.to
    else:
        target = state.get("standby_color", "blue")

    port_map = {"blue": 8000, "green": 8001}
    target_port = port_map[target]

    current = state.get("active_color", "unknown")
    print(f"\n{'=' * 50}")
    print(f"  EMERGENCY ROLLBACK: {current} -> {target}")
    print(f"{'=' * 50}\n")

    # 3. Check if target container is running
    print(f"[1/6] Checking if {target} container is running...")
    result = run(
        f"docker compose --profile deploy ps {target} --format json",
        check=False,
    )
    is_running = False
    if result.stdout.strip():
        try:
            for line in result.stdout.strip().splitlines():
                c = json.loads(line.strip())
                if c.get("State") == "running":
                    is_running = True
                    break
        except json.JSONDecodeError:
            is_running = "running" in result.stdout.lower()

    if not is_running:
        print(f"  {target} is not running. Starting it...")
        try:
            run(f"docker compose --profile deploy up -d {target}")
        except RuntimeError:
            print(f"\nFAILED: Could not start {target} container.")
            print("Manual fix:")
            print(f"  docker compose --profile deploy up -d {target}")
            print(f"  # Wait for it to be healthy, then:")
            print(f"  cp nginx/upstream-{target}.conf nginx/conf.d/default.conf")
            print("  docker exec smollm2-nginx nginx -s reload")
            sys.exit(1)

        # Wait for health
        print(f"[2/6] Waiting for {target} to become healthy (60s timeout)...")
        start = time.time()
        healthy = False
        while time.time() - start < 60:
            try:
                r = subprocess.run(
                    ["curl", "-sf", "--max-time", "5",
                     f"http://localhost:{target_port}/ready"],
                    capture_output=True, text=True, timeout=10,
                )
                if r.returncode == 0:
                    body = json.loads(r.stdout)
                    if body.get("status") == "ready":
                        healthy = True
                        break
            except Exception:
                pass
            time.sleep(2)

        if not healthy:
            print(f"\nFAILED: {target} did not become healthy within 60s.")
            print("Manual fix:")
            print(f"  docker compose --profile deploy logs {target}")
            print(f"  curl http://localhost:{target_port}/ready")
            sys.exit(1)
        print(f"  {target} is healthy!")
    else:
        print(f"  {target} is already running")

    # 4. Copy nginx config
    print(f"[3/6] Swapping nginx to {target}...")
    template = root / "nginx" / f"upstream-{target}.conf"
    if not template.exists():
        print(f"\nFAILED: Template not found: {template}")
        sys.exit(1)
    nginx_conf.write_text(template.read_text())
    print(f"  Wrote upstream-{target}.conf -> conf.d/default.conf")

    # 5. Test and reload nginx
    print("[4/6] Testing nginx config...")
    try:
        run("docker exec smollm2-nginx nginx -t")
    except RuntimeError:
        print("\nFAILED: nginx config test failed.")
        print("Manual fix: Check nginx/conf.d/default.conf for errors")
        sys.exit(1)

    print("[5/6] Reloading nginx...")
    try:
        run("docker exec smollm2-nginx nginx -s reload")
    except RuntimeError:
        print("\nFAILED: nginx reload failed.")
        print("Manual fix: docker exec smollm2-nginx nginx -s reload")
        sys.exit(1)

    # 6. Verify and update state
    time.sleep(2)
    print("[6/6] Verifying traffic...")
    try:
        r = subprocess.run(
            ["curl", "-sf", "--max-time", "5", "http://localhost/healthz"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            print("  Traffic verified OK")
        else:
            print("  WARNING: Verification failed, but nginx was reloaded")
    except Exception:
        print("  WARNING: Could not verify, but nginx was reloaded")

    # Update state
    now = datetime.now(timezone.utc).isoformat()
    new_state = {
        "active_color": target,
        "active_port": target_port,
        "standby_color": "green" if target == "blue" else "blue",
        "standby_port": 8001 if target == "blue" else 8000,
        "last_deployment": now,
        "last_model_version": state.get("last_model_version", "unknown"),
        "deployment_count": state.get("deployment_count", 0) + 1,
        "standby_prewarmed": False,
        "standby_prewarmed_at": None,
        "standby_container_id": None,
        "history": state.get("history", []) + [{
            "timestamp": now,
            "from_color": current,
            "to_color": target,
            "duration_seconds": 0,
            "success": True,
            "rollback": True,
        }],
    }
    new_state["history"] = new_state["history"][-20:]
    with open(state_file, "w") as f:
        json.dump(new_state, f, indent=4)
        f.write("\n")

    print(f"\n{'=' * 50}")
    print(f"  ROLLBACK COMPLETE: {target} is now active")
    print(f"{'=' * 50}\n")


if __name__ == "__main__":
    main()
