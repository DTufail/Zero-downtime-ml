#!/usr/bin/env python3
"""
Zero-Downtime Deployment Orchestrator

Automates blue-green deployment of SmolLM2 model server.
Runs on the HOST machine (not inside a container).

Usage:
    python deploy/orchestrator.py deploy          # Run a deployment
    python deploy/orchestrator.py status          # Show current state
    python deploy/orchestrator.py rollback        # Emergency rollback to previous
    python deploy/orchestrator.py history         # Show deployment history
"""

import argparse
import json
import logging
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


class DeploymentError(Exception):
    """Raised when a deployment step fails."""
    pass


class DeploymentOrchestrator:
    def __init__(self, project_root: str, health_timeout: int = 180, drain_seconds: int = 15):
        self.project_root = Path(project_root).resolve()
        self.state_file = self.project_root / "deploy" / "state.json"
        self.nginx_conf_dir = self.project_root / "nginx" / "conf.d"
        self.nginx_templates_dir = self.project_root / "nginx"
        self.compose_file = self.project_root / "docker-compose.yml"
        self.log_file = self.project_root / "deploy" / "deploy.log"
        self.health_timeout = health_timeout
        self.drain_seconds = drain_seconds

        self.logger = self._setup_logger()

    def _setup_logger(self) -> logging.Logger:
        logger = logging.getLogger("orchestrator")
        logger.setLevel(logging.DEBUG)

        # Stdout handler
        stdout_handler = logging.StreamHandler(sys.stdout)
        stdout_handler.setLevel(logging.INFO)
        stdout_fmt = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"
        )
        stdout_handler.setFormatter(stdout_fmt)

        # File handler (structured JSON log)
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(self.log_file)
        file_handler.setLevel(logging.DEBUG)
        file_fmt = logging.Formatter(
            '{"time": "%(asctime)s", "level": "%(levelname)s", "msg": "%(message)s"}',
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
        file_handler.setFormatter(file_fmt)

        if not logger.handlers:
            logger.addHandler(stdout_handler)
            logger.addHandler(file_handler)

        return logger

    def log(self, msg: str, level: str = "INFO"):
        getattr(self.logger, level.lower(), self.logger.info)(msg)

    # ── State Management ──────────────────────────────────────────

    def read_state(self) -> dict:
        if not self.state_file.exists():
            default_state = {
                "active_color": "blue",
                "active_port": 8000,
                "standby_color": "green",
                "standby_port": 8001,
                "last_deployment": None,
                "last_model_version": "smollm2-1.7b-q4",
                "deployment_count": 0,
                "history": [],
            }
            self.save_state(default_state)
            return default_state
        with open(self.state_file) as f:
            return json.load(f)

    def save_state(self, state: dict) -> None:
        if self.state_file.exists():
            shutil.copy2(self.state_file, str(self.state_file) + ".bak")
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.state_file, "w") as f:
            json.dump(state, f, indent=4)
            f.write("\n")

    # ── Command Execution ─────────────────────────────────────────

    def run_command(
        self, cmd, timeout: int = 30, check: bool = True
    ) -> subprocess.CompletedProcess:
        if isinstance(cmd, str):
            cmd_list = cmd.split()
            cmd_str = cmd
        else:
            cmd_list = list(cmd)
            cmd_str = " ".join(cmd)

        self.log(f"  $ {cmd_str}", level="DEBUG")
        try:
            result = subprocess.run(
                cmd_list,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(self.project_root),
            )
            if check and result.returncode != 0:
                self.log(
                    f"  Command failed (rc={result.returncode}): {result.stderr.strip()}",
                    level="ERROR",
                )
                raise DeploymentError(
                    f"Command failed: {cmd_str}\nstderr: {result.stderr.strip()}"
                )
            return result
        except subprocess.TimeoutExpired:
            raise DeploymentError(f"Command timed out after {timeout}s: {cmd_str}")

    # ── Health Checking ───────────────────────────────────────────

    def check_container_health(
        self,
        port: int,
        endpoint: str = "/ready",
        timeout: int = 120,
        poll_interval: int = 2,
    ) -> bool:
        url = f"http://localhost:{port}{endpoint}"
        start = time.time()
        attempts = 0

        while time.time() - start < timeout:
            attempts += 1
            try:
                result = subprocess.run(
                    ["curl", "-sf", "--max-time", "25", url],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if result.returncode == 0:
                    try:
                        body = json.loads(result.stdout)
                        if body.get("status") == "ready":
                            self.log(
                                f"  Health OK after {attempts} attempts "
                                f"({round(time.time() - start, 1)}s)"
                            )
                            return True
                        self.log(
                            f"  Poll {attempts}: status={body.get('status', 'unknown')}"
                        )
                    except json.JSONDecodeError:
                        self.log(f"  Poll {attempts}: non-JSON response")
                else:
                    self.log(
                        f"  Poll {attempts}: HTTP error (curl rc={result.returncode})"
                    )
            except (subprocess.TimeoutExpired, Exception) as e:
                self.log(
                    f"  Poll {attempts}: connection failed ({type(e).__name__})"
                )

            time.sleep(poll_interval)

        self.log(
            f"  Health check timed out after {timeout}s ({attempts} attempts)"
        )
        return False

    def _quick_health_check(self, port: int, timeout: int = 15) -> bool:
        """Quick health check - just one attempt with timeout.
        Used to verify a pre-warmed container is still alive."""
        try:
            result = self.run_command(
                f"curl -sf --max-time {timeout} http://localhost:{port}/ready",
                timeout=timeout + 5,
                check=False
            )
            return result.returncode == 0
        except Exception:
            return False

    def _get_container_id(self, color: str) -> str:
        """Get the Docker container ID for a given color."""
        result = self.run_command(
            f"docker inspect --format='{{{{.Id}}}}' smollm2-{color}",
            timeout=10,
            check=True
        )
        return result.stdout.strip().strip("'")

    def _is_container_running(self, color: str) -> bool:
        """Check if a container is currently running."""
        try:
            result = self.run_command(
                f"docker inspect --format='{{{{.State.Running}}}}' smollm2-{color}",
                timeout=10,
                check=False
            )
            return result.stdout.strip().strip("'") == "true"
        except Exception:
            return False

    def _stop_container(self, color: str) -> None:
        """Stop and remove a container."""
        self.run_command(
            f"docker compose --profile deploy stop smollm2-{color}",
            timeout=30, check=False
        )
        self.run_command(
            f"docker compose --profile deploy rm -f smollm2-{color}",
            timeout=10, check=False
        )

    # ── Helper: check if a compose service is running ─────────────

    def _is_service_running(self, service: str, profile: bool = False) -> bool:
        cmd = "docker compose"
        if profile:
            cmd += " --profile deploy"
        cmd += f" ps {service} --format json"
        result = self.run_command(cmd, timeout=10, check=False)
        if not result.stdout.strip():
            return False
        try:
            for line in result.stdout.strip().splitlines():
                line = line.strip()
                if line:
                    c = json.loads(line)
                    if c.get("State") == "running":
                        return True
        except json.JSONDecodeError:
            return "running" in result.stdout.lower()
        return False

    # ── Pre-flight Checks ─────────────────────────────────────────

    def preflight_checks(self, state: dict) -> None:
        active = state["active_color"]
        standby = state["standby_color"]
        active_port = state["active_port"]

        # 1. Active container running
        if not self._is_service_running(active):
            raise DeploymentError(
                f"Active container '{active}' is not running"
            )
        self.log(f"  {active} container is running")

        # 2. Active container healthy
        self.log(f"  Checking {active} readiness on port {active_port}...")
        healthy = self.check_container_health(active_port, timeout=120, poll_interval=2)
        if not healthy:
            raise DeploymentError(
                f"Active container '{active}' is not healthy on port {active_port}"
            )

        # 3. Nginx running
        if not self._is_service_running("nginx"):
            raise DeploymentError("Nginx container is not running")
        self.log("  Nginx is running")

        # 4. Nginx routing to active
        try:
            r = subprocess.run(
                ["curl", "-sf", "--max-time", "5", "http://localhost/healthz"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if r.returncode != 0:
                raise DeploymentError(
                    "Nginx is not routing traffic (port 80 /healthz failed)"
                )
        except subprocess.TimeoutExpired:
            raise DeploymentError("Nginx health check timed out")
        self.log("  Nginx routing OK via port 80")

        # 5. No leftover standby container
        if self._is_service_running(standby, profile=True):
            self.log(f"  Leftover {standby} container found, stopping it...")
            self.run_command(
                f"docker compose --profile deploy stop {standby}",
                timeout=30,
                check=False,
            )
            self.run_command(
                f"docker compose --profile deploy rm -f {standby}",
                timeout=10,
                check=False,
            )

        # 6. Disk space (informational)
        self.run_command("docker system df", timeout=10, check=False)
        self.log("  Disk space check passed")

    # ── Container Management ──────────────────────────────────────

    def start_standby(self, state: dict) -> None:
        standby = state["standby_color"]
        self.log(f"  Starting {standby} container...")
        self.run_command(
            f"docker compose --profile deploy up -d {standby}", timeout=30
        )

        # Wait for Docker to initialize
        time.sleep(5)

        # Verify container is running
        if not self._is_service_running(standby, profile=True):
            logs = self.run_command(
                f"docker compose --profile deploy logs --tail=20 {standby}",
                timeout=10,
                check=False,
            )
            self.log(f"  Container logs:\n{logs.stdout}", level="ERROR")
            raise DeploymentError(f"Container '{standby}' failed to start")

    def warmup_inference(self, port: int) -> None:
        self.log(f"  Sending warm-up inference to port {port}...")
        start = time.time()
        try:
            result = subprocess.run(
                [
                    "curl",
                    "-sf",
                    "--max-time",
                    "30",
                    "-X",
                    "POST",
                    f"http://localhost:{port}/chat",
                    "-H",
                    "Content-Type: application/json",
                    "-d",
                    json.dumps(
                        {"message": "Hello, respond in one word.", "max_tokens": 10}
                    ),
                ],
                capture_output=True,
                text=True,
                timeout=35,
            )
            elapsed = round(time.time() - start, 1)

            if result.returncode != 0:
                raise DeploymentError(
                    f"Warm-up inference failed (curl rc={result.returncode}): "
                    f"{result.stderr}"
                )

            body = json.loads(result.stdout)
            if "response" in body:
                self.log(
                    f"  Warm-up OK in {elapsed}s: "
                    f"{body['response'][:50]}..."
                )
            else:
                raise DeploymentError(
                    f"Warm-up response missing 'response' field: {body}"
                )

        except json.JSONDecodeError:
            raise DeploymentError(
                f"Warm-up returned non-JSON: {result.stdout[:200]}"
            )
        except subprocess.TimeoutExpired:
            raise DeploymentError("Warm-up inference timed out after 30s")

    # ── Nginx Management ──────────────────────────────────────────

    def swap_nginx(self, target_color: str) -> str:
        default_conf = self.nginx_conf_dir / "default.conf"
        template = self.nginx_templates_dir / f"upstream-{target_color}.conf"

        # 1. Read current config as backup
        original_config = default_conf.read_text()

        # 2. Copy template to active config
        default_conf.write_text(template.read_text())
        self.log(f"  Wrote upstream-{target_color}.conf -> conf.d/default.conf")

        # 3. Test nginx config
        try:
            self.run_command("docker exec smollm2-nginx nginx -t", timeout=5)
        except DeploymentError as e:
            self.log("  nginx -t failed, restoring original config...", level="ERROR")
            default_conf.write_text(original_config)
            raise DeploymentError(f"Nginx config test failed: {e}")

        # 4. Reload nginx
        try:
            self.run_command(
                "docker exec smollm2-nginx nginx -s reload", timeout=5
            )
        except DeploymentError as e:
            self.log(
                "  nginx reload failed, restoring original config...",
                level="ERROR",
            )
            default_conf.write_text(original_config)
            try:
                self.run_command(
                    "docker exec smollm2-nginx nginx -s reload", timeout=5
                )
            except Exception:
                pass
            raise DeploymentError(f"Nginx reload failed: {e}")

        return original_config

    def verify_traffic_switched(self, target_port: int) -> bool:
        self.log("  Sending 3 verification requests via nginx...")
        successes = 0
        for i in range(3):
            try:
                result = subprocess.run(
                    [
                        "curl",
                        "-sf",
                        "--max-time",
                        "5",
                        "http://localhost/healthz",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if result.returncode == 0:
                    body = json.loads(result.stdout)
                    if body.get("status") == "alive":
                        successes += 1
                        self.log(f"  Verification {i + 1}/3: OK")
                    else:
                        self.log(
                            f"  Verification {i + 1}/3: "
                            f"unexpected status {body.get('status')}"
                        )
                else:
                    self.log(
                        f"  Verification {i + 1}/3: "
                        f"failed (rc={result.returncode})"
                    )
            except Exception as e:
                self.log(f"  Verification {i + 1}/3: error ({e})")
            if i < 2:
                time.sleep(1)

        return successes == 3

    def rollback_nginx(self, original_config: str) -> None:
        default_conf = self.nginx_conf_dir / "default.conf"
        default_conf.write_text(original_config)
        self.run_command("docker exec smollm2-nginx nginx -t", timeout=5)
        self.run_command("docker exec smollm2-nginx nginx -s reload", timeout=5)
        self.log("  Nginx rolled back to previous upstream")

    def drain_and_stop_old(self, old_color: str, drain_seconds: int = 15) -> None:
        if drain_seconds > 0:
            self.log(f"  Draining {old_color} for {drain_seconds}s...")
            time.sleep(drain_seconds)
        self.log(f"  Stopping {old_color}...")
        self.run_command(
            f"docker compose --profile deploy stop {old_color}",
            timeout=60,
            check=False,
        )
        self.run_command(
            f"docker compose --profile deploy rm -f {old_color}",
            timeout=10,
            check=False,
        )
        self.log(f"  {old_color} stopped and removed")

    # ── Pre-Warm Sequence ─────────────────────────────────────────

    def prewarm(self) -> None:
        """
        Pre-warm the standby container without swapping traffic.

        Executes:
          1. Read state, determine standby color
          2. Pre-flight checks (active healthy, nginx routing, no stale standby)
          3. Start standby container
          4. Poll standby /ready until healthy (NO timeout ceiling - patient wait)
          5. Warm-up inference (real POST /chat)
          6. Record container ID
          7. Update state: standby_prewarmed=true, standby_prewarmed_at=now

        Does NOT:
          - Touch nginx config
          - Stop the active container
          - Change active_color in state

        After this completes, the standby container is running, warm, and idle.
        The user can then run deploy-fast at any time to complete the swap.
        """
        state = self.read_state()
        active_color = state["active_color"]
        standby_color = state["standby_color"]
        active_port = state["active_port"]
        standby_port = state["standby_port"]
        prewarm_start = time.time()

        self.log("=" * 60)
        self.log(f"PRE-WARM START: Preparing {standby_color} container")
        self.log(f"  Active: {active_color}:{active_port}")
        self.log(f"  Standby: {standby_color}:{standby_port}")
        self.log("=" * 60)

        try:
            # Step 1: Pre-flight checks
            self.log("Step 1: Pre-flight checks...")

            # Check active container is running
            if not self._is_service_running(active_color):
                raise DeploymentError(
                    f"Active container '{active_color}' is not running"
                )
            self.log(f"  {active_color} container is running")

            # Check active container healthy
            self.log(f"  Checking {active_color} readiness on port {active_port}...")
            healthy = self.check_container_health(active_port, timeout=120, poll_interval=2)
            if not healthy:
                raise DeploymentError(
                    f"Active container '{active_color}' is not healthy on port {active_port}"
                )

            # Check nginx is routing
            try:
                r = subprocess.run(
                    ["curl", "-sf", "--max-time", "5", "http://localhost/healthz"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if r.returncode != 0:
                    raise DeploymentError(
                        "Nginx is not routing traffic (port 80 /healthz failed)"
                    )
            except subprocess.TimeoutExpired:
                raise DeploymentError("Nginx health check timed out")
            self.log("  Nginx routing OK via port 80")

            # Check if standby is already running (stale from failed deploy?)
            if self._is_container_running(standby_color):
                # Check if it's already pre-warmed
                if state.get("standby_prewarmed", False):
                    self.log(f"  ⚠️  {standby_color} already pre-warmed at {state.get('standby_prewarmed_at', 'unknown')}")
                    self.log(f"  Verifying it's still healthy...")

                    # Quick health check - if still healthy, skip prewarm
                    if self._quick_health_check(standby_port, timeout=15):
                        elapsed = round(time.time() - prewarm_start, 1)
                        self.log(f"  ✓ {standby_color} still healthy. Skipping re-prewarm.")
                        self.log(f"PRE-WARM COMPLETE (already warm): {elapsed}s")
                        return
                    else:
                        self.log(f"  {standby_color} not healthy. Stopping and re-prewarming...")
                        self._stop_container(standby_color)
                else:
                    self.log(f"  ⚠️  Stale {standby_color} container found. Stopping it first...")
                    self._stop_container(standby_color)

            self.log("Step 1: ✓ Pre-flight passed")

            # Step 2: Start standby container
            self.log(f"Step 2: Starting {standby_color} container...")
            self.start_standby(state)
            self.log("Step 2: ✓ Container started")

            # Step 3: Poll health (PATIENT - use longer timeout than deploy)
            # For prewarm, we're not in a rush. Use 300s (5 min) timeout.
            # On 8GB Mac with CPU contention from active container, model load
            # can take 80-120s. 300s gives ample margin.
            self.log(f"Step 3: Polling {standby_color} health (patient mode, 300s timeout)...")
            healthy = self.check_container_health(
                standby_port,
                timeout=300,     # 5 minutes - very patient
                poll_interval=3  # Check every 3s (less aggressive than deploy's 2s)
            )
            if not healthy:
                raise DeploymentError(f"{standby_color} failed health check after 300s")
            self.log("Step 3: ✓ Health check passed")

            # Step 4: Warm-up inference
            self.log("Step 4: Warm-up inference...")
            self.warmup_inference(standby_port)
            self.log("Step 4: ✓ Inference verified")

            # Step 5: Record container ID for later verification
            container_id = self._get_container_id(standby_color)
            self.log(f"Step 5: Container ID: {container_id[:12]}")

            # Step 6: Update state (mark as pre-warmed, but DON'T change active_color)
            elapsed = round(time.time() - prewarm_start, 1)
            state["standby_prewarmed"] = True
            state["standby_prewarmed_at"] = datetime.now(timezone.utc).isoformat()
            state["standby_container_id"] = container_id
            self.save_state(state)

            self.log("=" * 60)
            self.log(f"PRE-WARM COMPLETE: {standby_color} is warm and idle ({elapsed}s)")
            self.log(f"  Run 'make deploy-fast' to swap traffic (takes <30s)")
            self.log("=" * 60)

        except DeploymentError as e:
            self.log(f"❌ PRE-WARM FAILED: {e}", level="ERROR")

            # Clean up: stop the standby container
            self.log(f"Stopping failed {standby_color} container...")
            try:
                self._stop_container(standby_color)
                self.log(f"✓ {standby_color} stopped")
            except Exception:
                self.log(f"Warning: Could not stop {standby_color}", level="WARNING")

            # Clear prewarm state
            state["standby_prewarmed"] = False
            state["standby_prewarmed_at"] = None
            state["standby_container_id"] = None
            self.save_state(state)

            raise

    # ── Main Deploy Sequence ──────────────────────────────────────

    def deploy(self) -> None:
        state = self.read_state()

        # If standby is already pre-warmed, stop it first - deploy will start fresh
        if state.get("standby_prewarmed", False):
            standby_color = state["standby_color"]
            self.log(f"Note: {standby_color} was pre-warmed. Stopping for fresh deploy.")
            if self._is_container_running(standby_color):
                self._stop_container(standby_color)
            state["standby_prewarmed"] = False
            state["standby_prewarmed_at"] = None
            state["standby_container_id"] = None
            self.save_state(state)

        active_color = state["active_color"]
        target_color = state["standby_color"]
        active_port = state["active_port"]
        target_port = state["standby_port"]
        original_nginx_config = None
        deployment_start = time.time()

        self.log("=" * 60)
        self.log(f"DEPLOYMENT START: {active_color} -> {target_color}")
        self.log("=" * 60)

        try:
            # Step 1-2: State
            self.log(
                f"Step 1-2: Active={active_color}:{active_port}, "
                f"Target={target_color}:{target_port}"
            )

            # Step 3: Pre-flight
            self.log("Step 3: Pre-flight checks...")
            self.preflight_checks(state)
            self.log("Step 3: Pre-flight passed")

            # Step 4: Start standby
            self.log("Step 4: Starting standby container...")
            self.start_standby(state)
            self.log("Step 4: Container started")

            # Step 5: Health poll
            self.log(f"Step 5: Polling health endpoint (timeout={self.health_timeout}s)...")
            healthy = self.check_container_health(target_port, timeout=self.health_timeout)
            if not healthy:
                raise DeploymentError(
                    f"Step 5: {target_color} failed health check after {self.health_timeout}s"
                )
            self.log("Step 5: Health check passed")

            # Step 6: Warm-up
            self.log("Step 6: Warm-up inference...")
            self.warmup_inference(target_port)
            self.log("Step 6: Inference verified")

            # ── POINT OF NO RETURN ──
            # Before this: rollback = just stop the new container
            # After this: rollback = swap nginx back

            # Step 7-9: Swap nginx
            self.log("Step 7-9: Swapping nginx upstream...")
            original_nginx_config = self.swap_nginx(target_color)
            self.log("Step 7-9: Nginx reloaded")

            # Step 10: Drain
            self.log(f"Step 10: Draining old connections ({self.drain_seconds}s)...")
            time.sleep(self.drain_seconds)
            self.log("Step 10: Drain complete")

            # Step 11: Verify
            self.log("Step 11: Verifying traffic switch...")
            switched = self.verify_traffic_switched(target_port)
            if not switched:
                raise DeploymentError(
                    "Step 11: Traffic not reaching target after nginx reload"
                )
            self.log("Step 11: Traffic verified on target")

            # Step 12: Stop old
            self.log("Step 12: Stopping old container...")
            self.drain_and_stop_old(active_color, drain_seconds=0)
            self.log("Step 12: Old container stopped")

            # Step 13: Update state
            elapsed = round(time.time() - deployment_start, 1)
            now = datetime.now(timezone.utc).isoformat()
            new_state = {
                "active_color": target_color,
                "active_port": target_port,
                "standby_color": active_color,
                "standby_port": active_port,
                "last_deployment": now,
                "last_model_version": state.get(
                    "last_model_version", "unknown"
                ),
                "deployment_count": state.get("deployment_count", 0) + 1,
                "standby_prewarmed": False,
                "standby_prewarmed_at": None,
                "standby_container_id": None,
                "history": state.get("history", [])
                + [
                    {
                        "timestamp": now,
                        "from_color": active_color,
                        "to_color": target_color,
                        "duration_seconds": elapsed,
                        "success": True,
                    }
                ],
            }
            new_state["history"] = new_state["history"][-20:]
            self.save_state(new_state)

            self.log("=" * 60)
            self.log(
                f"DEPLOYMENT COMPLETE: {target_color} is now active ({elapsed}s)"
            )
            self.log("=" * 60)

        except DeploymentError as e:
            self.log(f"DEPLOYMENT FAILED: {e}", level="ERROR")

            # Rollback nginx if we already swapped
            if original_nginx_config is not None:
                self.log("Rolling back nginx config...")
                try:
                    self.rollback_nginx(original_nginx_config)
                    self.log("Nginx rolled back")
                except Exception as rollback_err:
                    self.log(
                        f"CRITICAL: Nginx rollback failed: {rollback_err}",
                        level="CRITICAL",
                    )

            # Always try to stop the standby container on failure
            self.log(f"Stopping failed {target_color} container...")
            try:
                self.run_command(
                    f"docker compose --profile deploy stop {target_color}",
                    timeout=30,
                    check=False,
                )
                self.run_command(
                    f"docker compose --profile deploy rm -f {target_color}",
                    timeout=10,
                    check=False,
                )
                self.log(f"{target_color} stopped")
            except Exception:
                self.log(
                    f"Warning: Could not stop {target_color}", level="WARNING"
                )

            # Record failure in history
            state = self.read_state()
            elapsed = round(time.time() - deployment_start, 1)
            state.setdefault("history", []).append(
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "from_color": active_color,
                    "to_color": target_color,
                    "duration_seconds": elapsed,
                    "success": False,
                    "error": str(e),
                }
            )
            state["history"] = state["history"][-20:]
            self.save_state(state)

            raise

    # ── Fast Deploy Sequence (using pre-warmed container) ─────────

    def deploy_fast(self) -> None:
        """
        Fast deployment using pre-warmed standby container.

        Prerequisites:
          - make prewarm must have been run successfully
          - state.standby_prewarmed must be True
          - The same standby container must still be running

        Executes:
          1. Read state, verify standby_prewarmed=True
          2. Verify standby container is the same one that was prewarmed (container ID match)
          3. Re-verify standby health (quick check, not full poll)
          4. Quick inference verification
          5-6. Swap nginx config + reload
          7. Drain period (15s)
          8. Verify traffic switched
          9. Stop old container
          10. Update state (flip active/standby, reset prewarm flags)

        Duration target: <30s (model already loaded)
        """
        state = self.read_state()
        active_color = state["active_color"]
        standby_color = state["standby_color"]
        active_port = state["active_port"]
        standby_port = state["standby_port"]
        deploy_start = time.time()
        original_nginx_config = None

        self.log("=" * 60)
        self.log(f"FAST DEPLOY START: {active_color} → {standby_color}")
        self.log("=" * 60)

        try:
            # Step 1: Verify pre-warm state
            self.log("Step 1: Checking pre-warm state...")
            if not state.get("standby_prewarmed", False):
                raise DeploymentError(
                    "Standby not pre-warmed. Run 'make prewarm' first."
                )

            prewarm_time = state.get("standby_prewarmed_at", "unknown")
            self.log(f"  Pre-warmed at: {prewarm_time}")

            # Check staleness - warn if prewarm was more than 1 hour ago
            if state.get("standby_prewarmed_at"):
                try:
                    prewarm_dt = datetime.fromisoformat(
                        state["standby_prewarmed_at"].replace("Z", "+00:00")
                    )
                    age_seconds = (datetime.now(timezone.utc) - prewarm_dt).total_seconds()
                    age_minutes = age_seconds / 60

                    if age_minutes > 60:
                        self.log(f"  ⚠️  Pre-warm is {age_minutes:.0f} minutes old. "
                                f"Running thorough health check...")
                    else:
                        self.log(f"  Pre-warm age: {age_minutes:.1f} minutes")
                except Exception:
                    self.log("  Could not parse prewarm timestamp, proceeding with health check")

            self.log("Step 1: ✓ Pre-warm state verified")

            # Step 2: Verify container ID matches
            self.log("Step 2: Verifying container identity...")
            expected_id = state.get("standby_container_id")

            if not self._is_container_running(standby_color):
                raise DeploymentError(
                    f"{standby_color} container is not running. "
                    f"It may have been stopped or OOM-killed since prewarm. "
                    f"Run 'make prewarm' again."
                )

            if expected_id:
                current_id = self._get_container_id(standby_color)
                if current_id != expected_id:
                    raise DeploymentError(
                        f"Container ID mismatch. Expected {expected_id[:12]}, "
                        f"got {current_id[:12]}. Container was recreated since prewarm. "
                        f"Run 'make prewarm' again."
                    )
                self.log(f"  Container ID match: {current_id[:12]}")
            else:
                self.log("  No container ID in state (skipping ID verification)")

            self.log("Step 2: ✓ Container identity verified")

            # Step 3: Re-verify health (quick, not full poll)
            self.log("Step 3: Quick health re-check...")
            healthy = self._quick_health_check(standby_port, timeout=30)
            if not healthy:
                raise DeploymentError(
                    f"{standby_color} failed quick health check. "
                    f"Model may have crashed since prewarm. "
                    f"Run 'make prewarm' again."
                )
            self.log("Step 3: ✓ Standby still healthy")

            # Step 4: Quick inference verification
            self.log("Step 4: Quick inference verification...")
            self.warmup_inference(standby_port)
            self.log("Step 4: ✓ Inference working")

            # ─── POINT OF NO RETURN ───

            # Step 5-6: Swap nginx
            self.log("Step 5-6: Swapping nginx upstream...")
            original_nginx_config = self.swap_nginx(standby_color)
            self.log("Step 5-6: ✓ Nginx reloaded")

            # Step 7: Drain period
            drain = self.drain_seconds
            self.log(f"Step 7: Draining old connections ({drain}s)...")
            time.sleep(drain)
            self.log("Step 7: ✓ Drain complete")

            # Step 8: Verify traffic switched
            self.log("Step 8: Verifying traffic switch...")
            switched = self.verify_traffic_switched(standby_port)
            if not switched:
                raise DeploymentError("Traffic not reaching standby after nginx reload")
            self.log("Step 8: ✓ Traffic verified on standby")

            # Step 9: Stop old container
            self.log(f"Step 9: Stopping {active_color}...")
            self.drain_and_stop_old(active_color, drain_seconds=0)
            self.log(f"Step 9: ✓ {active_color} stopped")

            # Step 10: Update state
            elapsed = round(time.time() - deploy_start, 1)
            now = datetime.now(timezone.utc).isoformat()
            new_state = {
                "active_color": standby_color,
                "active_port": standby_port,
                "standby_color": active_color,
                "standby_port": active_port,
                "last_deployment": now,
                "last_model_version": state.get("last_model_version", "unknown"),
                "deployment_count": state.get("deployment_count", 0) + 1,
                "standby_prewarmed": False,       # Reset prewarm flags
                "standby_prewarmed_at": None,
                "standby_container_id": None,
                "history": state.get("history", []) + [{
                    "timestamp": now,
                    "from_color": active_color,
                    "to_color": standby_color,
                    "duration_seconds": elapsed,
                    "success": True,
                    "mode": "fast",               # Distinguish from normal deploy
                }]
            }
            new_state["history"] = new_state["history"][-20:]
            self.save_state(new_state)

            self.log("=" * 60)
            self.log(f"FAST DEPLOY COMPLETE: {standby_color} is now active ({elapsed}s)")
            self.log("=" * 60)

        except DeploymentError as e:
            self.log(f"❌ FAST DEPLOY FAILED: {e}", level="ERROR")

            # Rollback nginx if we already swapped
            if original_nginx_config is not None:
                self.log("Rolling back nginx config...")
                try:
                    self.rollback_nginx(original_nginx_config)
                    self.log("✓ Nginx rolled back")
                except Exception as rollback_err:
                    self.log(f"❌ CRITICAL: Nginx rollback failed: {rollback_err}", level="CRITICAL")

            # Do NOT stop the standby container on fast-deploy failure.
            # It's still pre-warmed and potentially useful. Just leave it running.
            # The user can retry deploy-fast or run make deploy (full sequence).
            self.log(f"Note: {standby_color} container left running (still pre-warmed)")

            # Reset prewarm state ONLY if the container is actually dead
            if not self._is_container_running(standby_color):
                state["standby_prewarmed"] = False
                state["standby_prewarmed_at"] = None
                state["standby_container_id"] = None
                self.save_state(state)

            # Record failure
            state = self.read_state()
            elapsed = round(time.time() - deploy_start, 1)
            state.setdefault("history", []).append({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "from_color": active_color,
                "to_color": standby_color,
                "duration_seconds": elapsed,
                "success": False,
                "mode": "fast",
                "error": str(e),
            })
            state["history"] = state["history"][-20:]
            self.save_state(state)

            raise

    # ── Rollback Command ──────────────────────────────────────────

    def rollback(self) -> None:
        state = self.read_state()
        target_color = state["standby_color"]
        target_port = state["standby_port"]
        current_active = state["active_color"]

        self.log("=" * 60)
        self.log(f"ROLLBACK: {current_active} -> {target_color}")
        self.log("=" * 60)

        # Check if target container is running; if not, start it
        if not self._is_service_running(target_color, profile=True):
            self.log(f"  {target_color} not running, starting it...")
            self.run_command(
                f"docker compose --profile deploy up -d {target_color}",
                timeout=30,
            )
            self.log(f"  Waiting for {target_color} health check...")
            healthy = self.check_container_health(target_port, timeout=60)
            if not healthy:
                raise DeploymentError(
                    f"Rollback target '{target_color}' failed health check"
                )

        # Swap nginx
        template = self.nginx_templates_dir / f"upstream-{target_color}.conf"
        default_conf = self.nginx_conf_dir / "default.conf"
        default_conf.write_text(template.read_text())
        self.run_command("docker exec smollm2-nginx nginx -t", timeout=5)
        self.run_command(
            "docker exec smollm2-nginx nginx -s reload", timeout=5
        )

        # Verify
        time.sleep(2)
        switched = self.verify_traffic_switched(target_port)
        if not switched:
            self.log(
                "WARNING: Traffic verification failed after rollback",
                level="WARNING",
            )

        # Update state
        now = datetime.now(timezone.utc).isoformat()
        new_state = {
            "active_color": target_color,
            "active_port": target_port,
            "standby_color": current_active,
            "standby_port": state["active_port"],
            "last_deployment": now,
            "last_model_version": state.get("last_model_version", "unknown"),
            "deployment_count": state.get("deployment_count", 0) + 1,
            "standby_prewarmed": False,
            "standby_prewarmed_at": None,
            "standby_container_id": None,
            "history": state.get("history", [])
            + [
                {
                    "timestamp": now,
                    "from_color": current_active,
                    "to_color": target_color,
                    "duration_seconds": 0,
                    "success": True,
                    "rollback": True,
                }
            ],
        }
        new_state["history"] = new_state["history"][-20:]
        self.save_state(new_state)

        self.log("=" * 60)
        self.log(f"ROLLBACK COMPLETE: {target_color} is now active")
        self.log("=" * 60)

    # ── Status & History ──────────────────────────────────────────

    def status(self) -> None:
        state = self.read_state()
        print(f"\n{'=' * 50}")
        print("  Deployment State")
        print(f"{'=' * 50}")
        print(
            f"  Active:      {state['active_color']} "
            f"(port {state['active_port']})"
        )
        print(
            f"  Standby:     {state['standby_color']} "
            f"(port {state['standby_port']})"
        )
        print(f"  Deployments: {state.get('deployment_count', 0)}")
        print(f"  Last Deploy: {state.get('last_deployment', 'never')}")
        print(f"  Model:       {state.get('last_model_version', 'unknown')}")
        print()

        # Container status
        print("  Container Status:")
        result = self.run_command(
            "docker compose --profile deploy ps", timeout=10, check=False
        )
        if result.stdout.strip():
            for line in result.stdout.strip().splitlines():
                print(f"    {line}")
        print()

        # Memory usage
        print("  Memory Usage:")
        result = self.run_command(
            "docker stats --no-stream --format"
            " table {{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}",
            timeout=10,
            check=False,
        )
        if result.stdout.strip():
            for line in result.stdout.strip().splitlines():
                if "smollm2" in line.lower() or "NAME" in line:
                    print(f"    {line}")
        print()

        # Active nginx config
        print("  Nginx Upstream:")
        default_conf = self.nginx_conf_dir / "default.conf"
        if default_conf.exists():
            content = default_conf.read_text()
            for line in content.splitlines():
                if "server " in line and "listen" not in line:
                    print(f"    {line.strip()}")
        print()

        # Pre-warm status
        if state.get("standby_prewarmed", False):
            standby_color = state["standby_color"]
            prewarm_at = state.get("standby_prewarmed_at", "unknown")
            container_id = state.get("standby_container_id", "unknown")

            is_running = self._is_container_running(standby_color)

            print(f"  Pre-warm Status:")
            print(f"    Standby:     {standby_color} (pre-warmed)")
            print(f"    Pre-warmed:  {prewarm_at}")
            print(f"    Container:   {container_id[:12] if container_id else 'unknown'}")
            print(f"    Running:     {'YES ✓' if is_running else 'NO ❌ (needs re-prewarm)'}")

            if is_running:
                standby_port = state["standby_port"]
                healthy = self._quick_health_check(standby_port, timeout=10)
                print(f"    Healthy:     {'YES ✓' if healthy else 'NO ⚠️ (may need re-prewarm)'}")
                print(f"\n  ✅ Ready for: make deploy-fast")
            else:
                print(f"\n  ⚠️  Standby stopped. Run: make prewarm")
        else:
            print(f"  Pre-warm Status: Not pre-warmed")
            print(f"  ℹ️  Options: make deploy (full) or make prewarm + make deploy-fast")

        print(f"{'=' * 50}\n")

    def show_history(self) -> None:
        state = self.read_state()
        history = state.get("history", [])

        if not history:
            print("No deployment history.")
            return

        print(f"\n{'=' * 70}")
        print(f"  Deployment History (last {len(history)} entries)")
        print(f"{'=' * 70}")
        for i, entry in enumerate(reversed(history), 1):
            status = "OK" if entry.get("success") else "FAILED"
            rollback = " [ROLLBACK]" if entry.get("rollback") else ""
            error = f" - {entry['error']}" if entry.get("error") else ""
            print(
                f"  {i}. [{status}{rollback}] "
                f"{entry.get('from_color', '?')} -> "
                f"{entry.get('to_color', '?')} "
                f"| {entry.get('duration_seconds', '?')}s "
                f"| {entry.get('timestamp', '?')}{error}"
            )
        print(f"{'=' * 70}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Zero-Downtime Deployment Orchestrator"
    )
    parser.add_argument(
        "command",
        choices=["deploy", "deploy-fast", "prewarm", "status", "rollback", "history"],
        help="Command to execute",
    )
    parser.add_argument(
        "--project-root",
        default=".",
        help="Path to project root (default: current directory)",
    )
    parser.add_argument(
        "--drain-seconds",
        type=int,
        default=15,
        help="Seconds to wait for connection draining",
    )
    parser.add_argument(
        "--health-timeout",
        type=int,
        default=180,
        help="Seconds to wait for health check (default: 180)",
    )
    args = parser.parse_args()

    orchestrator = DeploymentOrchestrator(
        project_root=args.project_root,
        health_timeout=args.health_timeout,
        drain_seconds=args.drain_seconds,
    )

    try:
        if args.command == "deploy":
            orchestrator.deploy()
        elif args.command == "prewarm":
            orchestrator.prewarm()
        elif args.command == "deploy-fast":
            orchestrator.deploy_fast()
        elif args.command == "status":
            orchestrator.status()
        elif args.command == "rollback":
            orchestrator.rollback()
        elif args.command == "history":
            orchestrator.show_history()
    except DeploymentError as e:
        print(f"\nDeployment error: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nAborted by user.", file=sys.stderr)
        sys.exit(130)
