"""Automated Kubernetes CIS & Pod Security Standards tests."""

from __future__ import annotations

from scripts.k8s_security_audit import run_k8s_audit


def test_all_four_kubernetes_security_controls_pass() -> None:
    findings = run_k8s_audit()
    assert len(findings) == 4
    failures = [f"{f.check_id}: {f.evidence}" for f in findings if f.status != "PASS"]
    assert not failures, f"Kubernetes security audit failures detected: {failures}"
