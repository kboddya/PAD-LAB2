"""Reproducible microservice architecture benchmarks for ShopFlow (Lab2).

Generates measured CSV results in Lab2/experiments/:
1. results_aggregation.csv      -- Parallel vs sequential API Gateway dashboard aggregation (7 services)
2. results_sync_vs_async.csv    -- Synchronous HTTP fan-out vs asynchronous EventBus choreography
3. results_rate_limiting.csv    -- Sliding-window rate limiter behavior under burst load
4. results_fault_tolerance.csv  -- Graceful degradation of /api/dashboard under partial outages
"""

from __future__ import annotations

import asyncio
import csv
import statistics
import sys
import tempfile
import time
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

from tests.conftest import INTERNAL_SERVICE_TOKEN, build_test_cluster  # noqa: E402

EXPERIMENTS_DIR = LAB2_ROOT / "experiments"


def _write_csv(filename: str, fieldnames: list[str], rows: list[dict]) -> Path:
    EXPERIMENTS_DIR.mkdir(parents=True, exist_ok=True)
    path = EXPERIMENTS_DIR / filename
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return path


async def run_aggregation_experiment() -> list[dict]:
    """Experiment 1: Parallel asyncio.gather vs sequential aggregation across 7 dashboard downstream delays."""
    rows: list[dict] = []
    with tempfile.TemporaryDirectory() as tmp:
        cluster = build_test_cluster(
            Path(tmp),
            rate_limit_requests=500,
            rate_limit_window_seconds=60,
            log_json=False,
            log_level="WARNING",
        )
        seq_client = httpx.AsyncClient(transport=cluster.transport, timeout=5.0)
        try:
            gw = cluster.gateway_client
            assert gw is not None
            reg = await gw.post(
                "/api/auth/register",
                json={"email": "bench@shop.io", "username": "bench_user", "password": "password123"},
            )
            token = reg.json()["access_token"]
            user_id = reg.json()["user"]["id"]
            headers = {"Authorization": f"Bearer {token}"}

            downstream_hosts = [
                "user-service",
                "order-service",
                "notification-service",
                "product-service",
                "cart-service",
                "shipping-service",
                "analytics-service",
            ]
            downstream_urls = [
                f"http://user-service:8002/users/{user_id}",
                "http://order-service:8004/orders?limit=10",
                "http://notification-service:8006/notifications/unread-count",
                "http://product-service:8003/products?in_stock=true&limit=6",
                "http://cart-service:8007/cart",
                "http://shipping-service:8009/shipments?limit=5",
                "http://analytics-service:8012/analytics/me/summary",
            ]
            seq_headers = {
                "X-User-Id": user_id,
                "X-User-Role": "customer",
                "X-Internal-Token": INTERNAL_SERVICE_TOKEN,
            }

            for delay_ms in (0, 20, 50, 100):
                delay_sec = delay_ms / 1000.0
                for host in downstream_hosts:
                    cluster.transport.delays[host] = delay_sec

                # Warmup
                await gw.get("/api/dashboard", headers=headers)

                parallel_times: list[float] = []
                sequential_times: list[float] = []
                runs = 5
                for _ in range(runs):
                    t0 = time.perf_counter()
                    resp = await gw.get("/api/dashboard", headers=headers)
                    assert resp.status_code == 200
                    parallel_times.append((time.perf_counter() - t0) * 1000)

                    t1 = time.perf_counter()
                    for url in downstream_urls:
                        r = await seq_client.get(url, headers=seq_headers)
                        assert r.status_code == 200
                    sequential_times.append((time.perf_counter() - t1) * 1000)

                par_avg = round(statistics.mean(parallel_times), 2)
                seq_avg = round(statistics.mean(sequential_times), 2)
                speedup = round(seq_avg / par_avg, 2) if par_avg > 0 else 1.0
                rows.append(
                    {
                        "downstream_delay_ms": delay_ms,
                        "runs": runs,
                        "sequential_avg_ms": seq_avg,
                        "parallel_avg_ms": par_avg,
                        "speedup_x": speedup,
                    }
                )
        finally:
            await seq_client.aclose()
            await cluster.aclose()

    _write_csv(
        "results_aggregation.csv",
        ["downstream_delay_ms", "runs", "sequential_avg_ms", "parallel_avg_ms", "speedup_x"],
        rows,
    )
    return rows


async def run_sync_vs_async_experiment() -> list[dict]:
    """Experiment 2: Sync inline HTTP fan-out vs async queue-backed event dispatch."""
    rows: list[dict] = []
    handler_delay_ms = 25.0
    handler_delay_sec = handler_delay_ms / 1000.0
    iterations = 10

    for num_subscribers in (1, 2, 4, 8):
        sync_latencies: list[float] = []
        for _ in range(iterations):
            t0 = time.perf_counter()
            for _ in range(num_subscribers):
                await asyncio.sleep(handler_delay_sec)
            sync_latencies.append((time.perf_counter() - t0) * 1000)

        queue: asyncio.Queue[dict] = asyncio.Queue()

        async def _worker() -> None:
            while True:
                item = await queue.get()
                if item is None:
                    queue.task_done()
                    break
                await asyncio.sleep(handler_delay_sec)
                queue.task_done()

        workers = [asyncio.create_task(_worker()) for _ in range(num_subscribers)]
        async_publisher_latencies: list[float] = []
        t_total_start = time.perf_counter()
        for idx in range(iterations):
            t0 = time.perf_counter()
            for sub_idx in range(num_subscribers):
                await queue.put({"event_id": f"{idx}-{sub_idx}"})
            async_publisher_latencies.append((time.perf_counter() - t0) * 1000)
        await queue.join()
        async_total_drain_ms = round((time.perf_counter() - t_total_start) * 1000, 2)

        for _ in workers:
            await queue.put(None)
        await asyncio.gather(*workers)

        sync_avg = round(statistics.mean(sync_latencies), 2)
        async_pub_avg = round(statistics.mean(async_publisher_latencies), 3)
        rows.append(
            {
                "subscribers_count": num_subscribers,
                "handler_delay_ms": handler_delay_ms,
                "sync_publisher_avg_ms": sync_avg,
                "async_publisher_avg_ms": async_pub_avg,
                "async_total_drain_ms": async_total_drain_ms,
                "publisher_latency_reduction_x": round(sync_avg / max(async_pub_avg, 0.001), 1),
            }
        )

    _write_csv(
        "results_sync_vs_async.csv",
        [
            "subscribers_count",
            "handler_delay_ms",
            "sync_publisher_avg_ms",
            "async_publisher_avg_ms",
            "async_total_drain_ms",
            "publisher_latency_reduction_x",
        ],
        rows,
    )
    return rows


async def run_rate_limiting_experiment() -> list[dict]:
    """Experiment 3: Burst traffic against API Gateway SlidingWindowRateLimiter."""
    rows: list[dict] = []
    burst_size = 60

    for limit in (10, 25, 50, 100):
        with tempfile.TemporaryDirectory() as tmp:
            cluster = build_test_cluster(
                Path(tmp),
                rate_limit_requests=limit,
                rate_limit_window_seconds=60,
                log_json=False,
                log_level="WARNING",
            )
            try:
                gw = cluster.gateway_client
                assert gw is not None
                allowed = 0
                rejected = 0
                latencies: list[float] = []
                for idx in range(burst_size):
                    t0 = time.perf_counter()
                    resp = await gw.get("/api/products", headers={"X-Forwarded-For": "203.0.113.10"})
                    latencies.append((time.perf_counter() - t0) * 1000)
                    if resp.status_code == 200:
                        allowed += 1
                    elif resp.status_code == 429:
                        rejected += 1
                    else:
                        raise AssertionError(f"Unexpected status {resp.status_code} at req {idx}")

                rows.append(
                    {
                        "burst_requests": burst_size,
                        "rate_limit": limit,
                        "allowed_200": allowed,
                        "rejected_429": rejected,
                        "rejection_rate_pct": round((rejected / burst_size) * 100, 1),
                        "avg_latency_ms": round(statistics.mean(latencies), 2),
                    }
                )
            finally:
                await cluster.aclose()

    _write_csv(
        "results_rate_limiting.csv",
        [
            "burst_requests",
            "rate_limit",
            "allowed_200",
            "rejected_429",
            "rejection_rate_pct",
            "avg_latency_ms",
        ],
        rows,
    )
    return rows


async def run_fault_tolerance_experiment() -> list[dict]:
    """Experiment 4: Graceful degradation of /api/dashboard under partial and critical outages."""
    rows: list[dict] = []
    with tempfile.TemporaryDirectory() as tmp:
        cluster = build_test_cluster(
            Path(tmp),
            rate_limit_requests=500,
            rate_limit_window_seconds=60,
            log_json=False,
            log_level="WARNING",
        )
        try:
            gw = cluster.gateway_client
            assert gw is not None
            reg = await gw.post(
                "/api/auth/register",
                json={"email": "ft@shop.io", "username": "ft_user", "password": "password123"},
            )
            token = reg.json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}

            scenarios = [
                ("all_healthy", []),
                ("notification_down", ["notification-service"]),
                ("notification_and_analytics_down", ["notification-service", "analytics-service"]),
                ("non_critical_4_down", ["notification-service", "cart-service", "shipping-service", "analytics-service"]),
                (
                    "all_dashboard_downstreams_down",
                    [
                        "user-service",
                        "order-service",
                        "notification-service",
                        "product-service",
                        "cart-service",
                        "shipping-service",
                        "analytics-service",
                    ],
                ),
                ("auth_service_down", ["auth-service"]),
            ]

            section_keys = (
                "profile",
                "recent_orders",
                "unread_notifications",
                "featured_products",
                "cart",
                "recent_shipments",
                "loyalty_summary",
            )

            for scenario_name, down_services in scenarios:
                cluster.transport.faults.clear()
                for svc in down_services:
                    cluster.transport.faults[svc] = "connect_error"

                t0 = time.perf_counter()
                resp = await gw.get("/api/dashboard", headers=headers)
                latency_ms = round((time.perf_counter() - t0) * 1000, 2)

                body = resp.json()
                degraded = body.get("degraded_services", []) if resp.status_code == 200 else ["auth-service"]
                available_sections = (
                    sum(1 for key in section_keys if body.get(key) is not None)
                    if resp.status_code == 200
                    else 0
                )

                rows.append(
                    {
                        "scenario": scenario_name,
                        "down_services": ";".join(down_services) if down_services else "none",
                        "http_status": resp.status_code,
                        "degraded_count": len(degraded),
                        "available_sections": f"{available_sections}/7",
                        "latency_ms": latency_ms,
                    }
                )
        finally:
            await cluster.aclose()

    _write_csv(
        "results_fault_tolerance.csv",
        ["scenario", "down_services", "http_status", "degraded_count", "available_sections", "latency_ms"],
        rows,
    )
    return rows


async def main() -> None:
    print("=== Running ShopFlow Architectural Experiments ===")
    agg = await run_aggregation_experiment()
    print("[1/4] Aggregation:", agg)

    sync_async = await run_sync_vs_async_experiment()
    print("[2/4] Sync vs Async:", sync_async)

    rate = await run_rate_limiting_experiment()
    print("[3/4] Rate Limiting:", rate)

    ft = await run_fault_tolerance_experiment()
    print("[4/4] Fault Tolerance:", ft)
    print(f"Saved CSV files to: {EXPERIMENTS_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
