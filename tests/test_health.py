import os

import requests

BASE_URL = os.getenv("TEST_URL", "http://localhost:8000")


def test_liveness():
    r = requests.get(f"{BASE_URL}/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "alive"


def test_readiness_when_model_loaded():
    r = requests.get(f"{BASE_URL}/ready")
    assert r.status_code == 200
    assert r.json()["status"] == "ready"


def test_deep_health():
    r = requests.get(f"{BASE_URL}/health/deep")
    assert r.status_code == 200
    data = r.json()
    for field in [
        "status", "model_loaded", "model_name", "inference_test_ms",
        "memory_used_mb", "memory_limit_mb", "memory_pct",
        "uptime_seconds", "requests_served", "avg_latency_ms",
        "active_requests", "version",
    ]:
        assert field in data, f"Missing field: {field}"


def test_chat_basic():
    r = requests.post(f"{BASE_URL}/chat", json={"message": "Hello"})
    assert r.status_code == 200
    assert "response" in r.json()


def test_chat_with_user_id():
    r = requests.post(f"{BASE_URL}/chat/testuser", json={"message": "Hello"})
    assert r.status_code == 200
    assert "response" in r.json()


def test_chat_returns_metadata():
    r = requests.post(f"{BASE_URL}/chat", json={"message": "Hi"})
    assert r.status_code == 200
    data = r.json()
    assert "request_id" in data
    assert "tokens_generated" in data
    assert "inference_ms" in data
