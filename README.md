# Лабораторная работа №2. Микросервисное backend-приложение **ShopFlow**

> **Тема**: Разработка распределённой системы управления онлайн-магазином на основе микросервисной архитектуры (**12 независимых бизнес-микросервисов** + API Gateway + NGINX Frontend, *Database per Service*, синхронный REST, асинхронная событийная хореография RabbitMQ, распределённая Saga-компенсация и CQRS-проекции).  
> **Выполнили**: Bohdan Kondratiuk и Vladco Alina.

---

## Содержание
1. [Описание проекта](#1-описание-проекта)
2. [Архитектура](#2-архитектура)
3. [Используемые технологии](#3-используемые-технологии)
4. [Установка](#4-установка)
5. [Запуск](#5-запуск)
6. [Configuration](#6-configuration)
7. [API](#7-api)
8. [Примеры использования](#8-примеры-использования)
9. [Тестирование](#9-тестирование)
10. [Результаты экспериментов](#10-результаты-экспериментов)
11. [Выводы и ответы на вопросы к защите](#11-выводы-и-ответы-на-вопросы-к-защите)

---

## 1. Описание проекта

**ShopFlow** — распределённая e-commerce платформа, разделённая на **12 независимых бизнес-микросервисов**, **API Gateway** и изолированный **Frontend-сервис**. Каждая предметная область выделена в собственный сервис с отдельной базой данных (`auth_db` .. `analytics_db`), собственным циклом запуска, конфигурацией, обработкой ошибок и структурированным JSON-логированием.

### Ключевые возможности (12 бизнес-микросервисов + Gateway + Frontend)
1. `auth-service` (`:8001`, `auth_db`) — регистрация, вход, PBKDF2-SHA256 хеширование паролей, выпуск и ротация JWT Access/Refresh токенов, отзыв токенов по `jti`, RBAC (`customer`, `seller`, `admin`).
2. `user-service` (`:8002`, `user_db`) — профили пользователей, уровни лояльности (`standard`, `silver`, `gold`), адреса доставки, автосоздание профиля (`user.registered`) и автоповышение уровня (`loyalty.upgraded`).
3. `product-service` (`:8003`, `product_db`) — каталог товаров, SKU, резервирование и освобождение остатков (`StockReservation` с Saga-компенсацией), проекция рейтинга `average_rating`/`review_count` из отзывов и событие `product.deactivated`.
4. `order-service` (`:8004`, `order_db`) — жизненный цикл заказов (`pending` → `paid` / `payment_failed` / `shipped` / `delivered` / `cancelled` / `refunded`), атомарный резерв склада, интеграция скидок и тарифов доставки, 3-сторонняя Saga-компенсация.
5. `payment-service` (`:8005`, `payment_db`) — идемпотентные платежи (`idempotency_key` / `order_id`), лимит суммы, возвраты (`refund`), симуляция отказов.
6. `notification-service` (`:8006`, `notification_db`) — событийный центр уведомлений с дедупликацией по `event_id` (подписки на `user.*`, `order.*`, `payment.*`, `shipment.*`, `review.published`, `ticket.resolved`, `loyalty.upgraded`).
7. `cart-service` (`:8007`, `cart_db`) — персистентная серверная корзина, live-синхронизация цен (`price_changed`) и остатков с `product-service`, предпросмотр купонов, `POST /cart/checkout` и автоочистка после `payment.succeeded`.
8. `promotion-service` (`:8008`, `promotion_db`) — движок промокодов (`percentage` / `fixed`), правила минимальной суммы, категории, уровня лояльности и квот, автовыпуск welcome-купона по `user.registered`, Saga-резервирование и компенсация квот при `payment.failed` / `order.cancelled`.
9. `shipping-service` (`:8009`, `shipping_db`) — тарифный калькулятор (`standard`, `express`, `pickup` + льготы `gold`), автосоздание отгрузки (`SF-TRK-*`) по `payment.succeeded`, конечный автомат чекпоинтов (`preparing` → `dispatched` → `in_transit` → `delivered`), синхронизирующий статусы заказа.
10. `review-service` (`:8010`, `review_db`) — отзывы и рейтинги со строгой проверкой реальной покупки (**Verified Purchase** через `order-service`), анти-спам модерация (`flagged`), голосование за полезность и ответы продавцов.
11. `support-service` (`:8011`, `support_db`) — тикеты поддержки и споры по заказам, матрица приоритетов и **SLA** (`2h`/`6h`/`24h`/`48h`), скрытые внутренние заметки и **автоматическое проведение возврата средств** (`approve-refund` → `payment-service`).
12. `analytics-service` (`:8012`, `analytics_db`) — **CQRS / Event-Driven Read Model**: идемпотентная проекция всех событий RabbitMQ в суточные KPI (`GMV`, `Net Revenue`, `AOV`), воронку конверсии, тренды товаров и расчёт **LTV клиентов** с публикацией `loyalty.upgraded`.
13. `api-gateway` (`:8000`, stateless) — маршрутизация `/api/*` на все 12 сервисов, JWT + RBAC, Sliding-Window Rate Limiter (`HTTP 429`), 7-поточная параллельная агрегация `GET /api/dashboard` с *graceful degradation* и агрегированный `GET /ready`.
14. `frontend` (`:8080`, NGINX Unprivileged) — единая внешняя точка входа в Docker Compose (все 15 внутренних контейнеров закрыты в сети `shopflow-net` через `expose:`).

---

## 2. Архитектура

### 2.1. Диаграмма микросервисов и хранилищ

```mermaid
flowchart TB
    Client([Browser / Client]) -->|":8080 ONLY"| FE[frontend :8080<br/>NGINX Unprivileged SPA + Reverse Proxy]
    FE -->|internal /api/*| GW[API Gateway :8000<br/>JWT, RBAC, Rate Limit, 7-way Aggregation]

    subgraph Core["Базовое ядро (1-6)"]
        Auth[auth-service :8001]
        User[user-service :8002]
        Prod[product-service :8003]
        Order[order-service :8004]
        Pay[payment-service :8005]
        Notif[notification-service :8006]
    end

    subgraph Extended["Расширенные сервисы (7-12)"]
        Cart[cart-service :8007]
        Promo[promotion-service :8008]
        Ship[shipping-service :8009]
        Rev[review-service :8010]
        Sup[support-service :8011]
        Ana[analytics-service :8012]
    end

    GW --> Core
    GW --> Extended

    Cart -->|"REST: stock & prices"| Prod
    Cart -->|"REST: validate coupon"| Promo
    Cart -->|"REST: checkout"| Order

    Order -->|"REST: verify user"| User
    Order -->|"REST: reserve / release"| Prod
    Order -->|"REST: redeem / release"| Promo
    Order -->|"REST: quote shipping"| Ship
    Order -->|"REST: charge"| Pay

    Rev -->|"REST: verify purchase"| Order
    Rev -->|"REST: check product"| Prod

    Sup -->|"REST: verify order"| Order
    Sup -->|"REST: check shipment"| Ship
    Sup -->|"REST: approve refund"| Pay

    MQ((RabbitMQ<br/>shopflow.events))
    Auth & Order & Pay & Prod & Promo & Ship & Rev & Sup & Ana -.-> MQ
    User & Notif & Cart -.-> MQ
```

### 2.2. Сценарий Checkout, 3-сторонней Saga и CQRS-лояльности

```mermaid
sequenceDiagram
    actor Buyer
    participant GW as api-gateway
    participant Cart as cart-service
    participant Order as order-service
    participant Prod as product-service
    participant Promo as promotion-service
    participant Ship as shipping-service
    participant Pay as payment-service
    participant MQ as RabbitMQ
    participant Ana as analytics-service
    participant User as user-service

    Buyer->>GW: POST /api/cart/checkout (shipping_method)
    GW->>Cart: POST /cart/checkout
    Cart->>Order: POST /orders (items, coupon_code, shipping_method)
    Order->>User: GET /users/{id}
    Order->>Prod: POST /products/{id}/reserve (order_id)
    Order->>Promo: POST /promotions/redeem (order_id)
    Order->>Ship: POST /shipping/quotes
    Order->>Pay: POST /payments (idempotency_key)
    alt Payment Succeeded
        Pay->>MQ: payment.succeeded
        MQ-->>Prod: commit stock
        MQ-->>Promo: commit coupon + promotion.redeemed
        MQ-->>Cart: clear cart idempotently
        MQ-->>Ship: auto-create Shipment (SF-TRK-*)
        MQ-->>Ana: update GMV & CustomerLtv
        Ana->>MQ: loyalty.upgraded (if LTV >= $500 / $1500)
        MQ-->>User: promote loyalty_tier
    else Payment Declined (Saga Rollback)
        Pay->>MQ: payment.failed
        MQ-->>Prod: release reserved stock
        MQ-->>Promo: release reserved coupon quota
        MQ-->>Order: status = payment_failed
        MQ-->>Ana: record failed metric
    end
    Order-->>Cart: OrderResponse
    Cart-->>GW: OrderResponse
    GW-->>Buyer: HTTP 201 + X-Request-ID
```

### 2.3. Структура проекта

```text
Lab2/
├── .github/
│   └── workflows/
│       └── ci-cd.yml               # GitHub Actions CI/CD (test -> build-images -> deploy-simulation)
├── Instruction.md                  # Задание лабораторной работы №2
├── PLAN.md                         # Архитектурный план реализации
├── README.md                       # Итоговый единый отчёт (микросервисы + K8s + CI/CD)
├── docker-compose.yml              # PostgreSQL (12 БД), RabbitMQ, 12 сервисов + Gateway + Frontend
├── docker/
│   └── build-images.sh             # Сборка всех 14 Docker-образов (локально или --minikube)
├── k8s/
│   ├── namespace.yaml              # Namespace shopflow
│   ├── kustomization.yaml          # Единый манифест (16 Deployments, 16 Services, 2 HPA, 17 NetworkPolicy)
│   ├── configmaps/                 # app-config.yaml + postgres-init.yaml (12 БД)
│   ├── secrets/                    # app-secrets.yaml (точечный secretKeyRef)
│   ├── infrastructure/             # postgres.yaml (PVC) + rabbitmq.yaml
│   ├── deployments/                # 14 прикладных Deployment с probes, limits и PSS Restricted
│   ├── services/                   # 13 ClusterIP + 1 NodePort (frontend:30080)
│   ├── hpa/                        # order-service-hpa.yaml + cart-service-hpa.yaml
│   └── network-policies/           # Zero-trust 17 NetworkPolicy (default-deny-ingress + allow)
├── pytest.ini                      # Конфигурация pytest и pythonpath для всех 12 сервисов
├── requirements.txt                # Единые зависимости для локального запуска и тестов
├── .env.example                    # Шаблон конфигурации окружения
├── infrastructure/
│   └── postgres/
│       └── init-databases.sql      # Инициализация 12 изолированных БД (auth_db..analytics_db)
├── shared/                         # Общая инфраструктурная библиотека (factory, db, errors, messaging, security)
├── services/                       # 12 независимых микросервисов (:8001..:8012)
│   ├── auth-service/               # :8001
│   ├── user-service/               # :8002
│   ├── product-service/            # :8003
│   ├── order-service/              # :8004
│   ├── payment-service/            # :8005
│   ├── notification-service/       # :8006
│   ├── cart-service/               # :8007
│   ├── promotion-service/          # :8008
│   ├── shipping-service/           # :8009
│   ├── review-service/             # :8010
│   ├── support-service/            # :8011
│   └── analytics-service/          # :8012
├── gateway/                        # :8000 (API Gateway)
├── frontend/                       # :8080 (NGINX Unprivileged SPA + Reverse Proxy)
├── scripts/
│   ├── deploy-k8s.sh               # Развёртывание в OrbStack / Kubernetes
│   ├── deploy-minikube.sh          # Развёртывание в Minikube
│   ├── demo-scaling-and-recovery.sh# Демонстрация scale и self-healing в K8s
│   ├── k8s_security_audit.py       # CIS Kubernetes & PSS Restricted аудит
│   ├── simulate_deploy.py          # Имитация деплоя (Kustomize bundle + rollout + E2E smoke test)
│   ├── run_experiments.py          # Скрипт 4 архитектурных бенчмарков
│   └── security_audit.py           # OWASP API Security Top 10 аудит
├── experiments/                    # Результаты экспериментов (CSV) и аудитов (JSON)
├── docs/                           # Подробная архитектура, API Reference и отчёты безопасности
└── tests/                          # Модульные, интеграционные, K8s, CI/CD, security и E2E тесты (37 тестов)
```

---

## 3. Используемые технологии

| Компонент | Выбранная технология | Обоснование выбора | Рассмотренные альтернативы |
| --- | --- | --- | --- |
| **Backend Framework** | **Python 3.13 + FastAPI** | Нативный `async/await` критичен для 7-поточного fan-out в API Gateway и sync-sync вызовов в `cart`/`order`/`review`/`support`; встроенный OpenAPI/Swagger UI и Pydantic v2. | Flask, Django REST Framework, Go/Gin. |
| **База данных (Persistence)** | **PostgreSQL 16 (12 отдельных БД) + SQLAlchemy 2.0** (SQLite для быстрых тестов) | Строгий паттерн *Database per Service* (12 изолированных БД), ACID-транзакции для инвентаря, квот купонов и платежей, а также CQRS-таблиц аналитики. | Одна общая БД (антипаттерн), MongoDB. |
| **Асинхронные сообщения** | **RabbitMQ (`aio-pika`, Topic Exchange) + абстракция `EventBus`** | Гибкая topic-маршрутизация (`payment.*`, `shipment.*`, `review.*`, `loyalty.upgraded`), долговечные очереди и `InMemoryEventBus` для детерминированных тестов. | Kafka (тяжеловесна для учебного кластера), Redis Streams. |
| **API Gateway** | **Собственный Gateway на FastAPI + `httpx.AsyncClient`** | Полный контроль над JWT/RBAC, Sliding-Window Rate Limiter, пробросом `X-Request-ID` и параллельной 7-поточной агрегацией `/api/dashboard`. | NGINX/Kong без кастомной агрегации. |
| **Безопасность** | **PyJWT (HS256) + PBKDF2-SHA256 + `X-Internal-Token` HMAC** | Безголовые сервисы доверяют внутренним заголовкам `X-User-Id`/`X-User-Role` только при валидном `X-Internal-Token`; отзыв JWT по `jti`; строгий CSP в NGINX. | Session cookies в Redis. |

---

## 4. Установка

```bash
cd Lab2
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

---

## 5. Запуск

### Вариант А. Полная платформа в Docker Compose (16 контейнеров: PostgreSQL + RabbitMQ + 12 сервисов + Gateway + Frontend)

```bash
cd Lab2
docker compose up --build
```

- **Frontend UI (единственный опубликованный наружу порт)**: `http://localhost:8080`
- **API через встроенный прокси Frontend**: `http://localhost:8080/api/*` (Swagger `/docs`, агрегированный `/ready`)
- Все 15 внутренних контейнеров (`postgres`, `rabbitmq`, 12 микросервисов и `api-gateway`) закрыты от хоста (`expose:`) в сети `shopflow-net`.

### Вариант Б. Локальный запуск сервисов (SQLite + `memory://`)

Каждый из 12 микросервисов запускается независимо по порту `:8001`..`:8012` из директории `Lab2/`:

```bash
PYTHONPATH=.:services/cart-service      RABBITMQ_URL=memory:// .venv/bin/uvicorn cart_service.main:create_app --factory --port 8007
PYTHONPATH=.:services/promotion-service RABBITMQ_URL=memory:// .venv/bin/uvicorn promotion_service.main:create_app --factory --port 8008
PYTHONPATH=.:services/shipping-service  RABBITMQ_URL=memory:// .venv/bin/uvicorn shipping_service.main:create_app --factory --port 8009
PYTHONPATH=.:services/review-service    RABBITMQ_URL=memory:// .venv/bin/uvicorn review_service.main:create_app --factory --port 8010
PYTHONPATH=.:services/support-service   RABBITMQ_URL=memory:// .venv/bin/uvicorn support_service.main:create_app --factory --port 8011
PYTHONPATH=.:services/analytics-service RABBITMQ_URL=memory:// .venv/bin/uvicorn analytics_service.main:create_app --factory --port 8012
```

### Вариант В. Развёртывание в Kubernetes (`k8s/`)

```bash
./docker/build-images.sh
kubectl apply -k k8s
# либо автоматически:
./scripts/deploy-k8s.sh --start-k8s
```

- Снаружи через `NodePort` (`:30080`) опубликован **только** `Service/frontend`; остальные 15 сервисов используют `ClusterIP` и изолированы **17** политиками `NetworkPolicy` (`default-deny-ingress`).

### Вариант Г. CI/CD Pipeline и имитация деплоя (GitHub Actions)

Пайплайн описан в [.github/workflows/ci-cd.yml](file:///Users/kboddya/Dev/Univ/PAD/Lab2/.github/workflows/ci-cd.yml) и строго гарантирует запуск тестов до сборки образов и деплоя:

```mermaid
flowchart LR
    T["1. Job: test<br/>pytest (37 тестов) + docker compose config +<br/>kubectl kustomize + OWASP (8/8) + K8s CIS (4/4)"] -->|"needs: [test]"| B["2. Job: build-images<br/>Сборка 14 образов shopflow/*"]
    B -->|"needs: [test, build-images]"| D["3. Job: deploy-simulation<br/>scripts/simulate_deploy.py:<br/>Kustomize bundle -> Pre-flight -> 16-Deployment Rollout -> 12-service Smoke Test"]
```

Локальный запуск имитации деплоя:
```bash
.venv/bin/python scripts/simulate_deploy.py --environment staging --image-tag v1.0.0
```
Скрипт генерирует перетегированный релизный бандл `deploy-artifacts/deploy-bundle.yaml`, проверяет все pre-flight гейты безопасности, моделирует поэтапный rollout 16 Deployment'ов в порядке зависимостей (5 волн) и выполняет E2E smoke-тест на живом 12-сервисном кластере.

---

## 6. Configuration

Все параметры вынесены в переменные окружения (`shared/config.py`, `.env.example`):

| Переменная | Назначение | Значение по умолчанию |
| --- | --- | --- |
| `*_DATABASE_URL` (`AUTH_DATABASE_URL` .. `ANALYTICS_DATABASE_URL`) | Строка подключения SQLAlchemy для каждого из 12 сервисов | `sqlite:///./<service>.db` / PostgreSQL `auth_db`..`analytics_db` |
| `*_SERVICE_URL` (`AUTH_SERVICE_URL` .. `ANALYTICS_SERVICE_URL`) | Внутренние DNS-адреса сервисов | `http://<service-name>:<port>` |
| `RABBITMQ_URL` | URL брокера сообщений (`amqp://...` или `memory://`) | `memory://` / `amqp://guest:guest@rabbitmq:5672/` |
| `JWT_SECRET`, `INTERNAL_SERVICE_TOKEN` | Подпись HS256 JWT и защита межсервисных заголовков | Секреты окружения |
| `RATE_LIMIT_REQUESTS`, `RATE_LIMIT_WINDOW_SECONDS`, `PROXY_TIMEOUT_SECONDS` | Лимиты и таймауты API Gateway | `60`, `60`, `10.0` |

---

## 7. API

Полная спецификация приведена в [docs/api_reference.md](file:///Users/kboddya/Dev/Univ/PAD/Lab2/docs/api_reference.md) и в Swagger UI (`/docs`).

| Сервис | Основные endpoints через API Gateway | Доступ |
| --- | --- | --- |
| **Health & Aggregation** | `GET /health`, `GET /ready`, `GET /api/dashboard` | Public / Auth |
| **Auth (`:8001`)** | `POST /api/auth/{register,login,refresh,logout}`, `GET /api/auth/me` | Public / Auth |
| **Users (`:8002`)** | `GET/PUT /api/users/{id}`, `POST/GET /api/users/{id}/addresses` | Self / Admin |
| **Products (`:8003`)** | `GET /api/products`, `POST/PUT/DELETE /api/products` | Public / Seller / Admin |
| **Orders (`:8004`)** | `POST/GET /api/orders`, `POST /api/orders/{id}/cancel`, `PATCH /api/orders/{id}/status` | Auth / Admin |
| **Payments (`:8005`)** | `POST /api/payments`, `GET /api/payments/by-order/{id}`, `POST /api/payments/{id}/refund` | Auth |
| **Notifications (`:8006`)** | `GET /api/notifications`, `GET /api/notifications/unread-count`, `PATCH /api/notifications/{id}/read` | Auth |
| **Cart (`:8007`)** | `GET/DELETE /api/cart`, `POST/PATCH/DELETE /api/cart/items`, `POST/DELETE /api/cart/coupon`, `POST /api/cart/checkout` | Auth |
| **Promotions (`:8008`)** | `GET /api/promotions`, `POST /api/promotions`, `POST /api/promotions/validate`, `PATCH /api/promotions/{id}/deactivate` | Public / Auth / Seller |
| **Shipping (`:8009`)** | `POST /api/shipping/quotes`, `GET /api/shipments/track/{tn}`, `GET /api/shipments`, `POST /api/shipments/{id}/status` | Public / Auth / Seller |
| **Reviews (`:8010`)** | `GET /api/reviews`, `GET /api/reviews/product/{id}/summary`, `POST /api/reviews`, `POST /api/reviews/{id}/{helpful,reply}`, `PATCH .../moderate` | Public / Auth / Seller / Admin |
| **Support (`:8011`)** | `POST/GET /api/support/tickets`, `POST /api/support/tickets/{id}/messages`, `PATCH .../status`, `POST .../approve-refund` | Auth / Seller / Admin |
| **Analytics (`:8012`)** | `GET /api/analytics/trending`, `GET /api/analytics/me/summary`, `GET /api/analytics/{overview,products/top,funnel}` | Public / Auth / Seller / Admin |

---

## 8. Примеры использования

```bash
# 1. Создание промокода продавцом и добавление товара в серверную корзину
curl -s -X POST http://localhost:8080/api/promotions -H "Authorization: Bearer $SELLER_TOKEN" -H "Content-Type: application/json" \
  -d '{"code":"VIP20","discount_type":"percentage","discount_value":20,"min_order_amount":100,"max_discount_amount":250}'
curl -s -X POST http://localhost:8080/api/cart/items -H "Authorization: Bearer $BUYER_TOKEN" -H "Content-Type: application/json" \
  -d '{"product_id":"'$PRODUCT_ID'","quantity":1}'
curl -s -X POST http://localhost:8080/api/cart/coupon -H "Authorization: Bearer $BUYER_TOKEN" -H "Content-Type: application/json" \
  -d '{"code":"VIP20"}'

# 2. Checkout корзины -> заказ, отгрузка и пересчёт аналитики/лояльности
curl -s -X POST http://localhost:8080/api/cart/checkout -H "Authorization: Bearer $BUYER_TOKEN" -H "Content-Type: application/json" \
  -d '{"shipping_address":"Bd. Dacia 24, Chisinau","shipping_method":"express"}'

# 3. Verified-purchase отзыв, спор в поддержке и KPI-аналитика
curl -s -X POST http://localhost:8080/api/reviews -H "Authorization: Bearer $BUYER_TOKEN" -H "Content-Type: application/json" \
  -d '{"product_id":"'$PRODUCT_ID'","rating":5,"title":"Excellent","comment":"Fast shipping and solid build."}'
curl -s http://localhost:8080/api/analytics/overview -H "Authorization: Bearer $SELLER_TOKEN"
```

---

## 9. Тестирование

Набор тестов в [tests/](file:///Users/kboddya/Dev/Univ/PAD/Lab2/tests) покрывает все 12 микросервисов, API Gateway, обработку ошибок, OWASP-безопасность, `docker-compose.yml` и два полных E2E-сценария:

- Базовые сервисы: `test_auth_service.py`, `test_user_service.py`, `test_product_service.py`, `test_order_service.py`, `test_payment_service.py`, `test_notification_service.py`.
- Новые сервисы: `test_cart_service.py`, `test_promotion_service.py`, `test_shipping_service.py`, `test_review_service.py`, `test_support_service.py`, `test_analytics_service.py`.
- Инфраструктура, Kubernetes, CI/CD и сквозные сценарии: `test_gateway.py`, `test_error_handling_and_logging.py`, `test_frontend_and_compose.py`, `test_security.py`, `test_k8s_and_docker.py`, `test_k8s_security.py`, `test_cicd_pipeline.py`, `test_e2e_microservices.py`.

```bash
cd Lab2
.venv/bin/pytest -v
```

Фактический результат прогона: **`37 passed`** (включая тесты Dockerfiles, Compose, Kustomize, CIS/PSS аудита, CI/CD workflow и имитации деплоя).

### 9.1. Тестирование безопасности (OWASP API Security Top 10 и Kubernetes CIS/PSS)

- **API Security** ([scripts/security_audit.py](file:///Users/kboddya/Dev/Univ/PAD/Lab2/scripts/security_audit.py)): **`8/8 PASS, 0 FAIL`** (`SEC-01`..`SEC-08`, отчёт в [docs/security_report.md](file:///Users/kboddya/Dev/Univ/PAD/Lab2/docs/security_report.md)).
- **Kubernetes & Containers** ([scripts/k8s_security_audit.py](file:///Users/kboddya/Dev/Univ/PAD/Lab2/scripts/k8s_security_audit.py)): **`4/4 PASS, 0 FAIL`** (`K8S-01` Secret для RabbitMQ, `K8S-02` точечный `secretKeyRef`, `K8S-03` 14/14 hardened Deployments с `drop ALL` и `RuntimeDefault`, `K8S-04` 17 `NetworkPolicy`; отчёт в [docs/k8s_security_report.md](file:///Users/kboddya/Dev/Univ/PAD/Lab2/docs/k8s_security_report.md)).

---

## 10. Результаты экспериментов

Скрипт [scripts/run_experiments.py](file:///Users/kboddya/Dev/Univ/PAD/Lab2/scripts/run_experiments.py) обновлён и выполнен на 12-сервисном кластере (CSV сохранены в [experiments/](file:///Users/kboddya/Dev/Univ/PAD/Lab2/experiments)).

### Эксперимент 1. Параллельная 7-поточная агрегация `/api/dashboard` (`asyncio.gather`) vs последовательные вызовы
Источник: [results_aggregation.csv](file:///Users/kboddya/Dev/Univ/PAD/Lab2/experiments/results_aggregation.csv)

| Задержка сервиса, мс | Последовательно (avg), мс | Параллельно `asyncio.gather` (avg), мс | Ускорение |
| ---: | ---: | ---: | ---: |
| 0 | 4.35 | 5.11 | 0.85x |
| 20 | 160.06 | 29.49 | 5.43x |
| 50 | 380.06 | 63.01 | 6.03x |
| 100 | 723.94 | 117.18 | **6.18x** |

**Вывод**: При 7 параллельных секциях `/api/dashboard` (`user`, `order`, `notification`, `product`, `cart`, `shipping`, `analytics`) выигрыш от `asyncio.gather` достигает **6.18×** при задержке 100 мс.

### Эксперимент 2. Синхронный HTTP fan-out vs Асинхронная событийная очередь (`EventBus`)
Источник: [results_sync_vs_async.csv](file:///Users/kboddya/Dev/Univ/PAD/Lab2/experiments/results_sync_vs_async.csv)

| Подписчиков ($N$) | Синхронный fan-out (avg), мс | Асинхронная публикация в шину (avg), мс | Полный дренаж фоновых воркеров, мс |
| ---: | ---: | ---: | ---: |
| 1 | 25.94 | 0.001 | 260.59 |
| 2 | 52.12 | 0.003 | 259.63 |
| 4 | 103.49 | 0.003 | 260.85 |
| 8 | 208.58 | 0.005 | 260.64 |

### Эксперимент 3. Работа Sliding-Window Rate Limiter под burst-нагрузкой (60 запросов)
Источник: [results_rate_limiting.csv](file:///Users/kboddya/Dev/Univ/PAD/Lab2/experiments/results_rate_limiting.csv)

| Лимит запросов | Пропущено (`HTTP 200`) | Отклонено (`HTTP 429`) | Доля отказов, % | Средняя задержка, мс |
| ---: | ---: | ---: | ---: | ---: |
| 10 | 10 | 50 | 83.3% | 0.30 |
| 25 | 25 | 35 | 58.3% | 0.43 |
| 50 | 50 | 10 | 16.7% | 0.74 |
| 100 | 60 | 0 | 0.0% | 0.72 |

### Эксперимент 4. Отказоустойчивость `/api/dashboard` при частичных и критических сбоях (7 секций)
Источник: [results_fault_tolerance.csv](file:///Users/kboddya/Dev/Univ/PAD/Lab2/experiments/results_fault_tolerance.csv)

| Сценарий | Отключённые сервисы | HTTP-статус | `degraded_count` | Доступно секций | Задержка, мс |
| --- | --- | ---: | ---: | ---: | ---: |
| Все работают | — | 200 | 0 | 7/7 | 21.57 |
| Сбой уведомлений | `notification-service` | 200 | 1 | 6/7 | 5.24 |
| Сбой уведомлений и аналитики | `notification`, `analytics` | 200 | 2 | 5/7 | 5.87 |
| Сбой 4 некритичных сервисов | `notification`, `cart`, `shipping`, `analytics` | 200 | 4 | 3/7 | 3.06 |
| Сбой всех 7 сервисов dashboard | `user`, `order`, `notification`, `product`, `cart`, `shipping`, `analytics` | 200 | 7 | 0/7 | 1.18 |
| Сбой критической аутентификации | `auth-service` | **503** | 1 | 0/7 | 0.32 |

---

## 11. Выводы и ответы на вопросы к защите

Платформа **ShopFlow** расширена до **12 полноценных бизнес-микросервисов** + **API Gateway** + **Frontend**, покрывающих весь сквозной цикл электронной коммерции (аутентификация, профиль и лояльность, каталог, корзина, промокоды, заказы, платежи, доставка, верифицированные отзывы, поддержка со SLA и авто-возвратами, CQRS-аналитика и уведомления).

1. **Почему приложение разделено именно на 12 микросервисов?** Каждый сервис соответствует отдельному *Bounded Context* со своей нагрузкой и жизненным циклом данных: предзаказ (`cart`, `promotion`), транзакционное ядро (`order`, `product`, `payment`), логистика (`shipping`), пользовательский контент (`review`), саппорт (`support`) и аналитика CQRS (`analytics`).
2. **Что происходит при отказе оплаты или отмене заказа?** Запускается 3-сторонняя распределённая Saga: `product-service` освобождает складской резерв, `promotion-service` возвращает квоту купона покупателю, а `order-service` и `analytics-service` обновляют статусы и метрики без ручного вмешательства.
3. **Зачем выделять `analytics-service` по паттерну CQRS?** Сложные аналитические расчёты (GMV, воронка, LTV, тренды) обновляются асинхронно по событиям RabbitMQ в денормализованных таблицах с дедупликацией по `event_id`, не блокируя транзакционные БД заказов и платежей.
