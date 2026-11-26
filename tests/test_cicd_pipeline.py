"""Tests validating the GitHub Actions CI/CD workflow and deployment simulation."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from scripts.simulate_deploy import run_deploy_simulation

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_github_actions_cicd_pipeline_enforces_test_before_deploy() -> None:
    workflow_path = REPO_ROOT / ".github" / "workflows" / "ci-cd.yml"
    assert workflow_path.is_file(), "Missing .github/workflows/ci-cd.yml"

    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    triggers = workflow.get("on") if "on" in workflow else workflow.get(True)
    assert isinstance(triggers, dict)
    assert {"push", "pull_request", "workflow_dispatch"}.issubset(set(triggers.keys()))

    jobs = workflow["jobs"]
    assert "test" in jobs
    assert "build-images" in jobs
    assert "deploy-simulation" in jobs

    # Enforce strict ordering: tests must run before image build & deployment simulation
    assert jobs["build-images"]["needs"] == ["test"]
    assert jobs["deploy-simulation"]["needs"] == ["test", "build-images"]

    test_steps = "\n".join(str(step.get("run", "")) for step in jobs["test"]["steps"])
    assert "pytest" in test_steps
    assert "security_audit.py" in test_steps
    assert "k8s_security_audit.py" in test_steps

    deploy_steps = "\n".join(str(step.get("run", "")) for step in jobs["deploy-simulation"]["steps"])
    assert "scripts/simulate_deploy.py" in deploy_steps


@pytest.mark.asyncio
async def test_deploy_simulation_generates_retagged_bundle_and_passes_smoke_test(tmp_path: Path) -> None:
    report = await run_deploy_simulation(
        environment="staging",
        image_tag="ci-test-123",
        output_dir=tmp_path,
    )
    assert report["status"] == "SUCCESS"
    assert report["environment"] == "staging"
    assert report["image_tag"] == "ci-test-123"
    assert len(report["rollout"]) == 16
    assert report["preflight_gate"]["k8s_security_checks_passed"] == 4
    assert report["smoke_test"]["ready_services"] == 12
    assert report["smoke_test"]["order_status"] == "paid"

    bundle_file = tmp_path / "deploy-bundle.yaml"
    report_file = tmp_path / "deploy-report.json"
    assert bundle_file.is_file()
    assert report_file.is_file()
    bundle_text = bundle_file.read_text(encoding="utf-8")
    assert "shopflow/order-service:ci-test-123" in bundle_text
    assert "shopflow/analytics-service:ci-test-123" in bundle_text
