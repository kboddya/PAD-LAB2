"""Active security audit suite for ShopFlow microservices (Lab2).

Executes 8 OWASP API Security & Web Hardening probes against a live ASGI cluster
and NGINX configuration, saving structured JSON findings for before/after comparisons.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

LAB2_ROOT = Path(__file__).resolve().parent.parent
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
    candidate = str(LAB2_ROOT / rel_path)
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from tests.conftest import build_test_cluster  # noqa: E402


@dataclass
class SecurityFinding:
    check_id: str
    title: str
    severity: str
    owasp_category: str
    status: str  # PASS | FAIL
    evidence: str
    remediation: str


async def run_audit() -> list[SecurityFinding]:
    findings: list[SecurityFinding] = []

    with tempfile.TemporaryDirectory() as tmp:
        cluster = build_test_cluster(Path(tmp), rate_limit_requests=100, rate_limit_window_seconds=60, log_json=False)
        try:
            gw = cluster.gateway_client
            assert gw is not None
            auth = cluster.service_clients["auth-service"]
            users = cluster.service_clients["user-service"]
            products = cluster.service_clients["product-service"]
            payments = cluster.service_clients["payment-service"]
            promotions = cluster.service_clients["promotion-service"]

            # Seed one legitimate seller and product for BOLA / injection tests
            seller_reg = await gw.post(
                "/api/auth/register",
                json={
                    "email": "sec_seller@shopflow.io",
                    "username": "sec_seller",
                    "password": "Password123",
                    "role": "seller",
                },
            )
            seller_token = seller_reg.json()["access_token"]
            seller_prod = (
                await gw.post(
                    "/api/products",
                    headers={"Authorization": f"Bearer {seller_token}"},
                    json={
                        "sku": "SEC-ITEM-1",
                        "name": "Security Audit Item",
                        "category": "security",
                        "price": 50.0,
                        "stock_quantity": 10,
                    },
                )
            ).json()

            # SEC-01: Self-registration privilege escalation (role=admin)
            admin_reg = await gw.post(
                "/api/auth/register",
                json={
                    "email": "attacker_admin@evil.com",
                    "username": "evil_admin",
                    "password": "Password123",
                    "role": "admin",
                },
            )
            sec01_pass = admin_reg.status_code in (400, 403, 422)
            findings.append(
                SecurityFinding(
                    check_id="SEC-01",
                    title="Public self-registration as admin role",
                    severity="Critical",
                    owasp_category="API5:2023 Broken Function Level Authorization",
                    status="PASS" if sec01_pass else "FAIL",
                    evidence=f"POST /api/auth/register with role='admin' returned HTTP {admin_reg.status_code}",
                    remediation="Restrict public RegisterRequest.role to customer/seller only.",
                )
            )

            # SEC-02: Trusted internal header spoofing directly against user-service
            victim_reg = await gw.post(
                "/api/auth/register",
                json={
                    "email": "victim@shopflow.io",
                    "username": "victim_user",
                    "password": "Password123",
                    "role": "customer",
                },
            )
            spoof_resp = await users.get(
                "/users",
                headers={"X-User-Id": "attacker", "X-User-Role": "admin"},
            )
            sec02_pass = spoof_resp.status_code in (401, 403)
            findings.append(
                SecurityFinding(
                    check_id="SEC-02",
                    title="Header spoofing (X-User-Role: admin) without internal service token",
                    severity="Critical",
                    owasp_category="API2:2023 Broken Authentication / API5",
                    status="PASS" if sec02_pass else "FAIL",
                    evidence=f"GET user-service /users with forged X-User-Role='admin' returned HTTP {spoof_resp.status_code}",
                    remediation="Require HMAC-verified X-Internal-Token alongside X-User-Id/X-User-Role headers.",
                )
            )

            # SEC-03: Unauthenticated / cross-user access to internal mutation endpoints
            unauth_user_create = await users.post(
                "/users",
                json={"user_id": "forged-1", "email": "f@x.io", "username": "forged1", "full_name": "Forged"},
            )
            unauth_reserve = await products.post(
                f"/products/{seller_prod['id']}/reserve",
                json={"quantity": 2, "order_id": "forged-ord"},
            )
            unauth_promo_redeem = await promotions.post(
                "/promotions/redeem",
                json={"order_id": "forged-o", "user_id": "forged-u", "code": "VIP20", "order_subtotal": 50.0},
            )
            victim_token = victim_reg.json()["access_token"]
            cross_pay = await payments.post(
                "/payments",
                headers={"Authorization": f"Bearer {victim_token}"},
                json={"order_id": "cross-ord-1", "user_id": "someone-else-id", "amount": 20.0},
            )
            sec03_pass = (
                unauth_user_create.status_code in (401, 403)
                and unauth_reserve.status_code in (401, 403)
                and unauth_promo_redeem.status_code in (401, 403)
                and cross_pay.status_code in (401, 403)
            )
            findings.append(
                SecurityFinding(
                    check_id="SEC-03",
                    title="Unauthenticated internal endpoints & cross-user payment initiation",
                    severity="High",
                    owasp_category="API1:2023 Broken Object Level Authorization",
                    status="PASS" if sec03_pass else "FAIL",
                    evidence=(
                        f"POST /users={unauth_user_create.status_code}, "
                        f"POST /products/reserve={unauth_reserve.status_code}, "
                        f"POST /promotions/redeem={unauth_promo_redeem.status_code}, "
                        f"cross-user POST /payments={cross_pay.status_code}"
                    ),
                    remediation="Enforce require_internal_service or caller ownership/admin check on mutation endpoints.",
                )
            )

            # SEC-04: Rate limit bypass via spoofed X-Forwarded-For header (isolated cluster with limit=3)
            with tempfile.TemporaryDirectory() as tmp_rl:
                rl_cluster = build_test_cluster(
                    Path(tmp_rl),
                    rate_limit_requests=3,
                    rate_limit_window_seconds=60,
                    log_json=False,
                )
                try:
                    rl_gw = rl_cluster.gateway_client
                    assert rl_gw is not None
                    statuses_spoofed: list[int] = []
                    for idx in range(5):
                        r = await rl_gw.get(
                            "/api/products",
                            headers={"X-Forwarded-For": f"198.51.100.{idx + 1}"},
                        )
                        statuses_spoofed.append(r.status_code)
                finally:
                    await rl_cluster.aclose()
            sec04_pass = 429 in statuses_spoofed
            findings.append(
                SecurityFinding(
                    check_id="SEC-04",
                    title="Rate Limiter bypass via rotating X-Forwarded-For header",
                    severity="High",
                    owasp_category="API4:2023 Unrestricted Resource Consumption",
                    status="PASS" if sec04_pass else "FAIL",
                    evidence=f"5 requests with rotating X-Forwarded-For yielded statuses {statuses_spoofed}",
                    remediation="Ignore untrusted X-Forwarded-For by default; trust X-Real-IP only behind NGINX.",
                )
            )

            # SEC-05: Weak password acceptance (e.g. '123456')
            weak_pw_resp = await auth.post(
                "/auth/register",
                json={"email": "weakpw@shopflow.io", "username": "weakpw_user", "password": "123456"},
            )
            sec05_pass = weak_pw_resp.status_code in (400, 422)
            findings.append(
                SecurityFinding(
                    check_id="SEC-05",
                    title="Weak password policy allows short numeric-only passwords",
                    severity="Medium",
                    owasp_category="API2:2023 Broken Authentication",
                    status="PASS" if sec05_pass else "FAIL",
                    evidence=f"Registration with password='123456' returned HTTP {weak_pw_resp.status_code}",
                    remediation="Require >= 8 chars with at least one letter and one digit.",
                )
            )

            # SEC-06: Security HTTP headers on Gateway & NGINX hardening
            gw_resp = await gw.get("/api/products", headers={"X-Forwarded-For": "203.0.113.200"})
            gw_headers = {k.lower(): v for k, v in gw_resp.headers.items()}
            has_gw_headers = (
                gw_headers.get("x-content-type-options") == "nosniff"
                and gw_headers.get("x-frame-options") == "DENY"
                and "no-store" in gw_headers.get("cache-control", "")
            )
            nginx_conf = (LAB2_ROOT / "frontend" / "nginx.conf").read_text(encoding="utf-8")
            has_nginx_hardening = (
                "server_tokens off;" in nginx_conf
                and "X-Content-Type-Options" in nginx_conf
                and "X-Frame-Options" in nginx_conf
                and "Content-Security-Policy" in nginx_conf
                and "client_max_body_size" in nginx_conf
            )
            sec06_pass = has_gw_headers and has_nginx_hardening
            findings.append(
                SecurityFinding(
                    check_id="SEC-06",
                    title="Missing HTTP security headers & NGINX hardening directives",
                    severity="Medium",
                    owasp_category="API8:2023 Security Misconfiguration",
                    status="PASS" if sec06_pass else "FAIL",
                    evidence=f"gateway_security_headers={has_gw_headers}, nginx_hardening={has_nginx_hardening}",
                    remediation="Add nosniff/DENY/Cache-Control headers in Gateway and CSP/server_tokens off in NGINX.",
                )
            )

            # SEC-07: SQL injection resilience in search/login + XSS escaping in frontend JS
            sqli_search = await gw.get("/api/products", params={"search": "' OR '1'='1' --"})
            sqli_login = await gw.post(
                "/api/auth/login",
                json={"email": "admin' OR '1'='1", "password": "' OR '1'='1"},
            )
            app_js = (LAB2_ROOT / "frontend" / "public" / "app.js").read_text(encoding="utf-8")
            sec07_pass = (
                sqli_search.status_code == 200
                and len(sqli_search.json()) == 0
                and sqli_login.status_code == 401
                and "function esc(" in app_js
            )
            findings.append(
                SecurityFinding(
                    check_id="SEC-07",
                    title="SQL Injection & Stored/Reflected XSS protection",
                    severity="High",
                    owasp_category="Injection / XSS",
                    status="PASS" if sec07_pass else "FAIL",
                    evidence=(
                        f"SQLi search HTTP {sqli_search.status_code} (matches={len(sqli_search.json())}), "
                        f"SQLi login HTTP {sqli_login.status_code}, esc() present={('function esc(' in app_js)}"
                    ),
                    remediation="Use parameterized SQLAlchemy queries and HTML escaping on all rendered fields.",
                )
            )

            # SEC-08: Token revocation & no stack trace leakage on 422/500
            logout_r = await gw.post("/api/auth/logout", headers={"Authorization": f"Bearer {victim_token}"})
            post_logout = await gw.get("/api/dashboard", headers={"Authorization": f"Bearer {victim_token}"})
            malformed = await gw.post("/api/auth/login", json={"email": 123})
            sec08_pass = (
                logout_r.status_code == 200
                and post_logout.status_code == 401
                and malformed.status_code == 422
                and "Traceback" not in malformed.text
            )
            findings.append(
                SecurityFinding(
                    check_id="SEC-08",
                    title="JWT revocation enforcement & error trace non-disclosure",
                    severity="Medium",
                    owasp_category="API2 / API8 Security Misconfiguration",
                    status="PASS" if sec08_pass else "FAIL",
                    evidence=(
                        f"post-logout dashboard HTTP {post_logout.status_code}, "
                        f"validation error HTTP {malformed.status_code}, Traceback leaked={('Traceback' in malformed.text)}"
                    ),
                    remediation="Enforce JTI revocation check on Gateway and uniform sanitized error envelopes.",
                )
            )
        finally:
            await cluster.aclose()

    return findings


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Lab2 ShopFlow security audit")
    parser.add_argument("--phase", choices=["before", "after"], required=True)
    args = parser.parse_args()

    findings = asyncio.run(run_audit())
    passed = sum(1 for f in findings if f.status == "PASS")
    failed = sum(1 for f in findings if f.status == "FAIL")

    report = {
        "phase": args.phase,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_checks": len(findings),
        "passed": passed,
        "failed": failed,
        "findings": [asdict(f) for f in findings],
    }

    out_path = LAB2_ROOT / "experiments" / f"security_{args.phase}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"=== Lab2 Security Audit ({args.phase.upper()}): {passed}/{len(findings)} PASS, {failed} FAIL ===")
    for f in findings:
        print(f"[{f.status}] {f.check_id} ({f.severity}) {f.title} -> {f.evidence}")
    print(f"Saved report: {out_path}")


if __name__ == "__main__":
    main()
