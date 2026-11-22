"""Tests verifying the standalone frontend service and isolated docker-compose topology."""

from __future__ import annotations

from pathlib import Path

import yaml

LAB2_ROOT = Path(__file__).resolve().parent.parent


def test_docker_compose_exposes_only_frontend_port() -> None:
    compose_path = LAB2_ROOT / "docker-compose.yml"
    data = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    services = data["services"]

    expected_services = {
        "postgres",
        "rabbitmq",
        "auth-service",
        "user-service",
        "product-service",
        "order-service",
        "payment-service",
        "notification-service",
        "api-gateway",
        "frontend",
    }
    assert set(services.keys()) == expected_services

    services_with_host_ports = [name for name, cfg in services.items() if "ports" in cfg]
    assert services_with_host_ports == ["frontend"]
    assert services["frontend"]["ports"] == ["8080:8080"]

    for name, cfg in services.items():
        if name != "frontend":
            assert "expose" in cfg, f"Service {name} must expose internal port only"


def test_frontend_nginx_proxies_api_to_gateway_and_defines_health() -> None:
    nginx_conf = (LAB2_ROOT / "frontend" / "nginx.conf").read_text(encoding="utf-8")
    assert "listen 8080;" in nginx_conf
    assert "location = /health" in nginx_conf
    assert "location /api/" in nginx_conf
    assert "proxy_pass http://api-gateway:8000;" in nginx_conf

    dockerfile = (LAB2_ROOT / "frontend" / "Dockerfile").read_text(encoding="utf-8")
    assert "nginx-unprivileged" in dockerfile
    assert "EXPOSE 8080" in dockerfile

    for asset in ("index.html", "styles.css", "app.js"):
        assert (LAB2_ROOT / "frontend" / "public" / asset).is_file()
