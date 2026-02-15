"""
Zero-Downtime ML Deployment - Load Test Suite

Test scenarios:
1. HealthCheckUser    - Lightweight /healthz polling (proves routing works)
2. InferenceUser      - Real /chat requests (proves model serving continues)
3. MixedUser          - Realistic mix of health checks + inference
4. DeploymentWatcher  - Monitors /health/deep during deployment for metadata

Usage:
    # Baseline test (single instance, no deployment)
    locust -f tests/locustfile.py --headless -u 5 -r 1 -t 3m --csv results/baseline

    # Light load during deployment
    locust -f tests/locustfile.py --headless -u 3 -r 1 -t 5m --csv results/deploy-light

    # Web UI mode (interactive)
    locust -f tests/locustfile.py
"""

import logging
import random
from locust import HttpUser, between, task


class HealthCheckUser(HttpUser):
    """
    Lightweight health check polling.
    Simulates monitoring systems or load balancer health probes.

    Expected behavior during deployment:
    - Should NEVER fail (health endpoint is fast, no inference)
    - Proves nginx routing continues working during swap
    """
    wait_time = between(0.5, 1.5)
    weight = 3

    @task
    def check_health(self):
        with self.client.get(
            "/healthz",
            name="/healthz",
            timeout=10,
            catch_response=True
        ) as response:
            if response.status_code == 200:
                response.success()
            else:
                response.failure(f"Status {response.status_code}")

    @task(1)
    def check_ready(self):
        with self.client.get(
            "/ready",
            name="/ready",
            timeout=15,
            catch_response=True
        ) as response:
            if response.status_code in [200, 503]:
                response.success()
            else:
                response.failure(f"Unexpected status {response.status_code}")


class InferenceUser(HttpUser):
    """
    Real inference requests.
    Simulates actual users chatting with the model.

    Expected behavior during deployment:
    - On 8GB Mac: latency may spike during overlap window (CPU contention)
    - Requests should NOT fail (503/500) — they may be slow but should complete
    - This is the hardest test for zero-downtime on constrained hardware
    """
    wait_time = between(3, 8)
    weight = 1

    prompts = [
        {"message": "What is machine learning?", "max_tokens": 30},
        {"message": "Explain Docker in one sentence.", "max_tokens": 20},
        {"message": "Hello, how are you?", "max_tokens": 15},
        {"message": "What is zero-downtime deployment?", "max_tokens": 30},
        {"message": "Count to five.", "max_tokens": 20},
        {"message": "Name three programming languages.", "max_tokens": 20},
        {"message": "What is a container?", "max_tokens": 25},
        {"message": "Say something short.", "max_tokens": 10},
    ]

    @task
    def chat(self):
        prompt = random.choice(self.prompts)

        with self.client.post(
            "/chat",
            json=prompt,
            name="/chat",
            timeout=60,
            catch_response=True
        ) as response:
            if response.status_code == 200:
                try:
                    data = response.json()
                    if data.get("response") or data.get("text"):
                        response.success()
                    else:
                        response.failure("Empty response body")
                except Exception:
                    response.failure("Invalid JSON response")
            elif response.status_code == 503:
                response.failure("Service unavailable (503)")
            else:
                response.failure(f"Status {response.status_code}")


class MixedUser(HttpUser):
    """
    Realistic mixed workload: mostly health checks, occasional inference.
    This simulates a production environment where monitoring systems
    hit health endpoints frequently while real users send inference requests
    less often.
    """
    wait_time = between(1, 3)
    weight = 2

    @task(5)
    def health_check(self):
        self.client.get("/healthz", name="/healthz [mixed]", timeout=10)

    @task(1)
    def inference(self):
        with self.client.post(
            "/chat",
            json={"message": "Hello", "max_tokens": 10},
            name="/chat [mixed]",
            timeout=60,
            catch_response=True
        ) as response:
            if response.status_code == 200:
                response.success()
            else:
                response.failure(f"Status {response.status_code}")

    @task(1)
    def deep_health(self):
        with self.client.get(
            "/health/deep",
            name="/health/deep [mixed]",
            timeout=15,
            catch_response=True
        ) as response:
            if response.status_code == 200:
                response.success()
            else:
                response.failure(f"Status {response.status_code}")


class DeploymentWatcher(HttpUser):
    """
    Monitors deployment metadata during a swap.
    Polls /health/deep to track which container is serving
    and records the exact moment traffic switches from blue to green.

    Run this ONLY during deployment tests, not baseline.
    """
    wait_time = between(1, 2)
    weight = 0

    @task
    def watch_deployment(self):
        with self.client.get(
            "/health/deep",
            name="/health/deep [watcher]",
            timeout=15,
            catch_response=True
        ) as response:
            if response.status_code == 200:
                try:
                    data = response.json()
                    color = data.get("color", "unknown")
                    container_id = data.get("container_id", "unknown")[:12]
                    memory_mb = data.get("memory_used_mb", 0)
                    logging.info(
                        f"DEPLOYMENT_WATCH: color={color}, "
                        f"container={container_id}, mem={memory_mb}MB"
                    )
                    response.success()
                except Exception:
                    response.success()
            else:
                response.failure(f"Status {response.status_code}")
