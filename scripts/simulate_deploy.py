"""Simulated Kubernetes deployment pipeline with Kustomize rendering, security gate, staged rollout, and E2E smoke test."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
for rel_path in (
    ".",
    "services/auth-service",
    "services/user-service",
    "services/product-service",
    "services/order-service",
    "services/payment-service",
    "services/notification-service",
    "services/cart-service",
    "services/promotion-service",
    "services/shipping-service",
    "services/review-service",
    "services/support-service",
    "services/analytics-service",
    "gateway",
):
    candidate = str(REPO_ROOT / rel_path)
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from scripts.k8s_security_audit import run_k8s_audit  # noqa: E402
from tests.conftest import build_test_cluster, register_user_via_gateway  # noqa: E402

_ROLLOUT_WAVES: list[tuple[str, list[str]]] = [
    ("wave-0-infra", ["postgres", "rabbitmq"]),
    (
        "wave-1-core",
        [
            "auth-service",
            "user-service",
            "product-service",
            "payment-service",
            "notification-service",
            "promotion-service",
            "shipping-service",
            "analytics-service",
        ],
    ),
    ("wave-2-orchestration", ["order-service"]),
    ("wave-3-domain-consumers", ["cart-service", "review-service", "support-service"]),
    ("wave-4-edge", ["api-gateway", "frontend"]),
]


def _render_and_retag_manifests(image_tag: str) -> list[dict[str, Any]]:
    proc = subprocess.run(
        ["kubectl", "kustomize", str(REPO_ROOT / "k8s")],
        capture_output=True,
        text=True,
        check=True,
    )
    docs = [doc for doc in yaml.safe_load_all(proc.stdout) if doc]
    for doc in docs:
        if doc.get("kind") != "Deployment":
            continue
        containers = doc.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        for container in containers:
            image = str(container.get("image", ""))
            if image.startswith("shopflow/"):
                repo_part = image.split(":", 1)[0]
                container["image"] = f"{repo_part}:{image_tag}"
    return docs


def _verify_preflight_gates(docs: list[dict[str, Any]]) -> dict[str, Any]:
    findings = run_k8s_audit()
    failures = [f"{f.check_id}: {f.evidence}" for f in findings if f.status != "PASS"]
    if failures:
        raise RuntimeError(f"Pre-flight Kubernetes security gate failed: {failures}")

    by_kind: dict[str, list[dict[str, Any]]] = {}
    for doc in docs:
        by_kind.setdefault(str(doc.get("kind")), []).append(doc)

    deployments = by_kind.get("Deployment", [])
    services = by_kind.get("Service", [])
    hpas = by_kind.get("HorizontalPodAutoscaler", [])
    netpols = by_kind.get("NetworkPolicy", [])
    if len(deployments) != 16 or len(services) != 16:
        raise RuntimeError(f"Expected 16 Deployments and 16 Services, got {len(deployments)}/{len(services)}")

    app_cm = next(
        cm for cm in by_kind.get("ConfigMap", []) if cm.get("metadata", {}).get("name") == "shopflow-config"
    )
    for k, v in app_cm.get("data", {}).items():
        if "localhost" in str(v) or "127.0.0.1" in str(v):
            raise RuntimeError(f"ConfigMap key {k} uses disallowed localhost address: {v}")

    return {
        "k8s_security_checks_passed": len(findings),
        "deployments": len(deployments),
        "services": len(services),
        "hpas": len(hpas),
        "network_policies": len(netpols),
    }


def _simulate_staged_rollout(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_name = {
        doc["metadata"]["name"]: doc
        for doc in docs
        if doc.get("kind") == "Deployment"
    }
    rollout_steps: list[dict[str, Any]] = []
    step_num = 1
    seen: set[str] = set()

    for wave_name, names in _ROLLOUT_WAVES:
        for name in names:
            dep = by_name.get(name)
            if dep is None:
                raise RuntimeError(f"Expected Deployment '{name}' missing from Kustomize output")
            seen.add(name)
            spec = dep["spec"]
            container = spec["template"]["spec"]["containers"][0]
            port = container.get("ports", [{}])[0].get("containerPort")
            probe = container.get("readinessProbe", {})
            probe_desc = (
                probe["httpGet"]["path"]
                if "httpGet" in probe
                else ("exec" if "exec" in probe else "tcp")
            )
            rollout_steps.append(
                {
                    "step": step_num,
                    "wave": wave_name,
                    "deployment": name,
                    "replicas": int(spec.get("replicas", 1)),
                    "image": container.get("image"),
                    "port": port,
                    "readiness_probe": probe_desc,
                    "status": "rolled_out",
                }
            )
            step_num += 1

    missing_from_waves = set(by_name) - seen
    if missing_from_waves:
        raise RuntimeError(f"Deployments not covered in rollout plan: {sorted(missing_from_waves)}")
    return rollout_steps


async def _run_post_deploy_smoke_test() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        cluster = build_test_cluster(Path(tmp), log_json=False, log_level="WARNING")
        try:
            gw = cluster.gateway_client
            assert gw is not None

            health = await gw.get("/health")
            if health.status_code != 200:
                raise RuntimeError(f"Gateway /health failed: {health.status_code}")

            ready = await gw.get("/ready")
            if ready.status_code != 200:
                raise RuntimeError(f"Gateway /ready failed: {ready.status_code} {ready.text}")
            checks = ready.json().get("checks", {})
            if len(checks) != 12 or not all(v == "ok" for v in checks.values()):
                raise RuntimeError(f"Unexpected /ready checks: {checks}")

            seller = await register_user_via_gateway(
                gw, email="smoke_seller@shopflow.io", username="smoke_seller", role="seller"
            )
            buyer = await register_user_via_gateway(
                gw, email="smoke_buyer@shopflow.io", username="smoke_buyer", role="customer"
            )
            s_hdr = {"Authorization": f"Bearer {seller['access_token']}"}
            b_hdr = {"Authorization": f"Bearer {buyer['access_token']}"}

            prod_resp = await gw.post(
                "/api/products",
                headers=s_hdr,
                json={
                    "sku": "SMOKE-01",
                    "name": "Smoke Test Laptop",
                    "category": "laptops",
                    "price": 600.0,
                    "stock_quantity": 5,
                },
            )
            if prod_resp.status_code != 201:
                raise RuntimeError(f"Product creation failed: {prod_resp.text}")
            product_id = prod_resp.json()["id"]

            add_cart = await gw.post(
                "/api/cart/items",
                headers=b_hdr,
                json={"product_id": product_id, "quantity": 1},
            )
            if add_cart.status_code != 201:
                raise RuntimeError(f"Cart add failed: {add_cart.text}")

            checkout = await gw.post(
                "/api/cart/checkout",
                headers=b_hdr,
                json={"shipping_address": "101 Deploy Ave", "shipping_method": "express"},
            )
            if checkout.status_code != 201:
                raise RuntimeError(f"Cart checkout failed: {checkout.text}")
            order = checkout.json()
            if order.get("status") != "paid":
                raise RuntimeError(f"Expected paid order in smoke test, got {order.get('status')}")

            shipment = await gw.get(f"/api/shipments/by-order/{order['id']}", headers=b_hdr)
            if shipment.status_code != 200:
                raise RuntimeError(f"Shipment verification failed: {shipment.text}")

            overview = await gw.get("/api/analytics/overview", headers=s_hdr)
            if overview.status_code != 200 or overview.json().get("orders_paid") != 1:
                raise RuntimeError(f"Analytics smoke verification failed: {overview.text}")

            return {
                "gateway_health": "ok",
                "ready_services": len(checks),
                "order_id": order["id"],
                "order_status": order["status"],
                "tracking_number": shipment.json()["tracking_number"],
                "net_revenue": overview.json()["net_revenue"],
            }
        finally:
            await cluster.aclose()


async def run_deploy_simulation(
    *,
    environment: str = "staging",
    image_tag: str = "1.0.0",
    output_dir: Path | None = None,
) -> dict[str, Any]:
    """Execute staged deployment simulation and return structured deployment report."""
    target_dir = output_dir or (REPO_ROOT / "deploy-artifacts")
    target_dir.mkdir(parents=True, exist_ok=True)

    docs = _render_and_retag_manifests(image_tag)
    bundle_path = target_dir / "deploy-bundle.yaml"
    bundle_path.write_text(yaml.safe_dump_all(docs, sort_keys=False), encoding="utf-8")

    preflight = _verify_preflight_gates(docs)
    rollout = _simulate_staged_rollout(docs)
    smoke_test = await _run_post_deploy_smoke_test()

    report = {
        "status": "SUCCESS",
        "environment": environment,
        "namespace": "shopflow",
        "image_tag": image_tag,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "bundle_path": str(bundle_path),
        "preflight_gate": preflight,
        "rollout": rollout,
        "smoke_test": smoke_test,
    }
    report_path = target_dir / "deploy-report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        lines = [
            f"## ShopFlow Deployment Simulation (`{environment}` · tag `{image_tag}`)",
            "",
            f"- **Status**: `{report['status']}`",
            f"- **Pre-flight Gate**: {preflight['deployments']} Deployments, {preflight['services']} Services, "
            f"{preflight['network_policies']} NetworkPolicies, {preflight['k8s_security_checks_passed']}/4 Security Checks PASS",
            f"- **Post-Deploy Smoke Test**: {smoke_test['ready_services']}/12 services ready, "
            f"order `{smoke_test['order_id']}` (`{smoke_test['order_status']}`), "
            f"tracking `{smoke_test['tracking_number']}`",
            "",
            "| Step | Wave | Deployment | Replicas | Image | Probe |",
            "| ---: | --- | --- | ---: | --- | --- |",
        ]
        for r in rollout:
            lines.append(
                f"| {r['step']} | `{r['wave']}` | `{r['deployment']}` | {r['replicas']} | `{r['image']}` | `{r['readiness_probe']}` |"
            )
        with Path(summary_file).open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Simulate staged Kubernetes deployment for ShopFlow")
    parser.add_argument("--environment", choices=["staging", "production"], default="staging")
    parser.add_argument("--image-tag", default="1.0.0")
    parser.add_argument("--output-dir", default=str(REPO_ROOT / "deploy-artifacts"))
    args = parser.parse_args()

    report = asyncio.run(
        run_deploy_simulation(
            environment=args.environment,
            image_tag=args.image_tag,
            output_dir=Path(args.output_dir),
        )
    )
    print(
        f"=== Deployment Simulation [{report['environment']}] SUCCEEDED "
        f"(tag={report['image_tag']}, rollout_deployments={len(report['rollout'])}, "
        f"smoke_ready={report['smoke_test']['ready_services']}/12) ==="
    )
    print(f"Bundle: {report['bundle_path']}")
    print(f"Report: {Path(args.output_dir) / 'deploy-report.json'}")


if __name__ == "__main__":
    main()
