#!/usr/bin/env python3
"""
Deployment Integration Test

Runs a full blue->green->blue deployment cycle and verifies:
1. Zero dropped requests during each swap
2. State file updates correctly
3. Both directions work (blue->green AND green->blue)

Prerequisites:
- Blue must be running and healthy on port 8000
- Nginx must be running on port 80
- Green must NOT be running

Usage:
    python tests/test_deployment.py
"""

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

from deploy.orchestrator import DeploymentOrchestrator, DeploymentError


class RequestMonitor:
    """Background thread that continuously sends requests through nginx."""

    def __init__(self, interval: float = 0.5):
        self.interval = interval
        self.running = False
        self.thread = None
        self.results = []
        self.lock = threading.Lock()

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=10)

    def _run(self):
        while self.running:
            start = time.time()
            try:
                result = subprocess.run(
                    ["curl", "-sf", "--max-time", "5", "http://localhost/healthz"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                elapsed = round((time.time() - start) * 1000, 1)
                success = result.returncode == 0
                with self.lock:
                    self.results.append(
                        {
                            "time": time.time(),
                            "success": success,
                            "latency_ms": elapsed,
                            "status": "ok" if success else f"rc={result.returncode}",
                        }
                    )
            except Exception as e:
                with self.lock:
                    self.results.append(
                        {
                            "time": time.time(),
                            "success": False,
                            "latency_ms": 0,
                            "status": str(e),
                        }
                    )
            time.sleep(self.interval)

    def get_summary(self):
        with self.lock:
            total = len(self.results)
            successes = sum(1 for r in self.results if r["success"])
            failures = total - successes
            avg_latency = (
                round(
                    sum(r["latency_ms"] for r in self.results if r["success"])
                    / max(successes, 1),
                    1,
                )
            )
            failed_details = [
                r for r in self.results if not r["success"]
            ]
            return {
                "total": total,
                "successes": successes,
                "failures": failures,
                "avg_latency_ms": avg_latency,
                "failed_details": failed_details[:10],
            }


def check_prerequisites():
    """Verify the system is in the right state to run the test."""
    print("Checking prerequisites...")

    # Blue running?
    r = subprocess.run(
        ["curl", "-sf", "--max-time", "5", "http://localhost:8000/ready"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if r.returncode != 0:
        print("FAIL: Blue (port 8000) is not healthy")
        print("Run: make up  # and wait for healthy")
        return False

    # Nginx running?
    r = subprocess.run(
        ["curl", "-sf", "--max-time", "5", "http://localhost/healthz"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if r.returncode != 0:
        print("FAIL: Nginx (port 80) is not routing")
        print("Run: make up")
        return False

    print("  Blue is healthy, nginx is routing. Ready to test.\n")
    return True


def run_test():
    """Run the full deployment cycle test."""
    if not check_prerequisites():
        sys.exit(1)

    orchestrator = DeploymentOrchestrator(project_root=str(PROJECT_ROOT), health_timeout=180)

    # Read initial state
    initial_state = orchestrator.read_state()
    initial_count = initial_state.get("deployment_count", 0)
    print(f"Initial state: active={initial_state['active_color']}, "
          f"deployments={initial_count}\n")

    # Start request monitor
    print("Starting continuous request monitor (every 0.5s)...")
    monitor = RequestMonitor(interval=0.5)
    monitor.start()

    # Give monitor a moment to start
    time.sleep(2)

    try:
        # ── FIRST DEPLOYMENT ──
        print("\n" + "=" * 60)
        print("TEST 1: First deployment (should swap to standby)")
        print("=" * 60 + "\n")

        orchestrator.deploy()

        state_after_first = orchestrator.read_state()
        expected_first = initial_state["standby_color"]
        assert state_after_first["active_color"] == expected_first, (
            f"Expected active={expected_first}, "
            f"got {state_after_first['active_color']}"
        )
        assert state_after_first["deployment_count"] == initial_count + 1
        print(f"\nFirst deployment verified: active={expected_first}\n")

        # Brief pause between deployments
        time.sleep(5)

        # ── SECOND DEPLOYMENT (round trip) ──
        print("\n" + "=" * 60)
        print("TEST 2: Second deployment (round trip back)")
        print("=" * 60 + "\n")

        orchestrator.deploy()

        state_after_second = orchestrator.read_state()
        expected_second = state_after_first["standby_color"]
        assert state_after_second["active_color"] == expected_second, (
            f"Expected active={expected_second}, "
            f"got {state_after_second['active_color']}"
        )
        assert state_after_second["deployment_count"] == initial_count + 2
        print(f"\nSecond deployment verified: active={expected_second}\n")

    except DeploymentError as e:
        print(f"\nDEPLOYMENT FAILED: {e}")
        monitor.stop()
        summary = monitor.get_summary()
        print(f"\nRequest monitor: {summary['total']} total, "
              f"{summary['failures']} failures")
        sys.exit(1)
    except AssertionError as e:
        print(f"\nASSERTION FAILED: {e}")
        monitor.stop()
        sys.exit(1)

    # Stop monitor and check results
    time.sleep(3)
    monitor.stop()

    summary = monitor.get_summary()

    print("\n" + "=" * 60)
    print("  TEST RESULTS")
    print("=" * 60)
    print(f"  Total requests:   {summary['total']}")
    print(f"  Successes:        {summary['successes']}")
    print(f"  Failures:         {summary['failures']}")
    print(f"  Avg latency:      {summary['avg_latency_ms']}ms")

    if summary["failed_details"]:
        print(f"\n  Failed request details:")
        for f in summary["failed_details"]:
            print(f"    - {f['status']} (latency={f['latency_ms']}ms)")

    # Final state check
    final_state = orchestrator.read_state()
    history = final_state.get("history", [])
    recent = [h for h in history if h.get("success")]
    print(f"\n  Deployment count:  {final_state['deployment_count']}")
    print(f"  History entries:   {len(history)}")
    print(f"  Successful swaps:  {len(recent)}")

    if recent:
        durations = [h["duration_seconds"] for h in recent[-2:]]
        print(f"  Deploy durations:  {durations}")

    print("=" * 60)

    if summary["failures"] == 0:
        print("\n  PASS: Zero dropped requests across both deployments!\n")
        return 0
    else:
        print(f"\n  FAIL: {summary['failures']} request(s) dropped!\n")
        return 1


if __name__ == "__main__":
    sys.exit(run_test())
