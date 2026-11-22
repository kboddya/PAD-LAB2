"""Reproducible microservice architecture benchmarks for ShopFlow (Lab2).

Generates measured CSV results in Lab2/experiments/:
1. results_aggregation.csv      -- Parallel vs sequential API Gateway dashboard aggregation
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
    "gateway",
):
    candidate = str(LAB2_ROOT / rel_path)
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from auth_service.config import AuthSettings  # noqa: E402
from auth_service.main import create_app as create_auth_app  # noqa: E402
from gateway_app.config import GatewaySettings  # noqa: E402
from gateway_app.main import create_app as create_gateway_app  # noqa: E402
from notification_service.config import NotificationSettings  # noqa: E402
from notification_service.main import create_app as create_notification_app  # noqa: E402
from order_service.config import OrderSettings  # noqa: E402
from order_service.main import create_app as create_order_app  # noqa: E402
from payment_service.config import PaymentSettings  # noqa: E402
from payment_service.main import create_app as create_payment_app  # noqa: E402
from product_service.config import ProductSettings  # noqa: E402
from product_service.main import create_app as create_product_app  # noqa: E402
from shared.messaging import InMemoryEventBus  # noqa: E402
from tests.conftest import ServiceRouterTransport  # noqa: E402
from user_service.config import UserSettings  # noqa: E402
from user_service.main import create_app as create_user_app  # noqa: E402

EXPERIMENTS_DIR = LAB2_ROOT / "experiments"


def _build_silent_cluster(
    db_dir: Path,
    *,
    rate_limit_requests: int = 500,
    rate_limit_window_seconds: int = 60,
):
    bus = InMemoryEventBus()
    jwt_secret = "benchmark-secret-key-at-least-32-chars"

    apps = {
        "auth-service": create_auth_app(
            AuthSettings(
                database_url=f"sqlite:///{db_dir / 'auth.db'}",
                rabbitmq_url="memory://",
                jwt_secret=jwt_secret,
                log_level="WARNING",
            ),
            event_bus=bus,
        ),
        "user-service": create_user_app(
            UserSettings(
                database_url=f"sqlite:///{db_dir / 'user.db'}",
                rabbitmq_url="memory://",
                jwt_secret=jwt_secret,
                log_level="WARNING",
            ),
            event_bus=bus,
        ),
        "product-service": create_product_app(
            ProductSettings(
                database_url=f"sqlite:///{db_dir / 'product.db'}",
                rabbitmq_url="memory://",
                jwt_secret=jwt_secret,
                log_level="WARNING",
            ),
            event_bus=bus,
        ),
        "payment-service": create_payment_app(
            PaymentSettings(
                database_url=f"sqlite:///{db_dir / 'payment.db'}",
                rabbitmq_url="memory://",
                jwt_secret=jwt_secret,
                log_level="WARNING",
            ),
            event_bus=bus,
        ),
        "notification-service": create_notification_app(
            NotificationSettings(
                database_url=f"sqlite:///{db_dir / 'notification.db'}",
                rabbitmq_url="memory://",
                jwt_secret=jwt_secret,
                log_level="WARNING",
            ),
            event_bus=bus,
        ),
    }

    transport = ServiceRouterTransport(apps)
    order_client = httpx.AsyncClient(transport=transport, timeout=5.0)
    order_app = create_order_app(
        OrderSettings(
            database_url=f"sqlite:///{db_dir / 'order.db'}",
            rabbitmq_url="memory://",
            jwt_secret=jwt_secret,
            log_level="WARNING",
        ),
        event_bus=bus,
        http_client=order_client,
    )
    apps["order-service"] = order_app
    transport._transports["order-service"] = httpx.ASGITransport(app=order_app, raise_app_exceptions=False)

    gw_client = httpx.AsyncClient(transport=transport, timeout=5.0)
    gateway_app = create_gateway_app(
        GatewaySettings(
            jwt_secret=jwt_secret,
            rate_limit_requests=rate_limit_requests,
            rate_limit_window_seconds=rate_limit_window_seconds,
            log_level="WARNING",
        ),
        http_client=gw_client,
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway_app, raise_app_exceptions=False),
        base_url="http://api-gateway",
    )
    return bus, transport, client, order_client, gw_client


def _write_csv(filename: str, fieldnames: list[str], rows: list[dict]) -> Path:
    EXPERIMENTS_DIR.mkdir(parents=True, exist_ok=True)
    path = EXPERIMENTS_DIR / filename
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return path


async def run_aggregation_experiment() -> list[dict]:
    """Experiment 1: Parallel asyncio.gather vs sequential aggregation across downstream delays."""
    rows: list[dict] = []
    with tempfile.TemporaryDirectory() as tmp:
        _, transport, gw, order_c, gw_c = _build_silent_cluster(Path(tmp))
        try:
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
            ]
            downstream_urls = [
                f"http://user-service:8002/users/{user_id}",
                "http://order-service:8004/orders?limit=10",
                "http://notification-service:8006/notifications/unread-count",
                "http://product-service:8003/products?in_stock=true&limit=6",
            ]
            seq_headers = {"X-User-Id": user_id, "X-User-Role": "customer"}

            for delay_ms in (0, 20, 50, 100):
                delay_sec = delay_ms / 1000.0
                for host in downstream_hosts:
                    transport.delays[host] = delay_sec

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
                        r = await gw_c.get(url, headers=seq_headers)
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
            await gw.aclose()
            await order_c.aclose()
            await gw_c.aclose()
            await transport.aclose()

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
        # Mode A: Synchronous sequential HTTP-like fan-out
        sync_latencies: list[float] = []
        for _ in range(iterations):
            t0 = time.perf_counter()
            for _ in range(num_subscribers):
                await asyncio.sleep(handler_delay_sec)
            sync_latencies.append((time.perf_counter() - t0) * 1000)

        # Mode B: Asynchronous decoupled event queue (publisher enqueues, workers consume in background)
        queue: asyncio.Queue[dict] = asyncio.Queue()
        processed_count = 0

        async def _worker() -> None:
            nonlocal processed_count
            while True:
                item = await queue.get()
                if item is None:
                    queue.task_done()
                    break
                await asyncio.sleep(handler_delay_sec)
                processed_count += 1
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
            _, transport, gw, order_c, gw_c = _build_silent_cluster(
                Path(tmp),
                rate_limit_requests=limit,
                rate_limit_window_seconds=60,
            )
            try:
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
                await gw.aclose()
                await order_c.aclose()
                await gw_c.aclose()
                await transport.aclose()

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
        _, transport, gw, order_c, gw_c = _build_silent_cluster(Path(tmp))
        try:
            reg = await gw.post(
                "/api/auth/register",
                json={"email": "ft@shop.io", "username": "ft_user", "password": "password123"},
            )
            token = reg.json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}

            scenarios = [
                ("all_healthy", []),
                ("notification_down", ["notification-service"]),
                ("notification_and_order_down", ["notification-service", "order-service"]),
                (
                    "all_dashboard_downstreams_down",
                    ["user-service", "order-service", "notification-service", "product-service"],
                ),
                ("auth_service_down", ["auth-service"]),
            ]

            for scenario_name, down_services in scenarios:
                transport.faults.clear()
                for svc in down_services:
                    transport.faults[svc] = "connect_error"

                t0 = time.perf_counter()
                resp = await gw.get("/api/dashboard", headers=headers)
                latency_ms = round((time.perf_counter() - t0) * 1000, 2)

                body = resp.json()
                degraded = body.get("degraded_services", []) if resp.status_code == 200 else ["auth-service"]
                available_sections = (
                    sum(
                        1
                        for key in ("profile", "recent_orders", "unread_notifications", "featured_products")
                        if body.get(key) is not None
                    )
                    if resp.status_code == 200
                    else 0
                )

                rows.append(
                    {
                        "scenario": scenario_name,
                        "down_services": ";".join(down_services) if down_services else "none",
                        "http_status": resp.status_code,
                        "degraded_count": len(degraded),
                        "available_sections": f"{available_sections}/4",
                        "latency_ms": latency_ms,
                    }
                )
        finally:
            await gw.aclose()
            await order_c.aclose()
            await gw_c.aclose()
            await transport.aclose()

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
