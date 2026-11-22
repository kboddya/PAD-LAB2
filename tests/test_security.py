"""Automated OWASP API Security & Web Hardening regression tests for ShopFlow (Lab2)."""

from __future__ import annotations

import pytest

from scripts.security_audit import run_audit


@pytest.mark.asyncio
async def test_all_eight_owasp_security_controls_pass() -> None:
    findings = await run_audit()
    assert len(findings) == 8
    failures = [f"{f.check_id}: {f.evidence}" for f in findings if f.status != "PASS"]
    assert not failures, f"Security audit failures detected: {failures}"
