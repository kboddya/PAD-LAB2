# Лабораторная работа №2. Микросервисное backend-приложение **ShopFlow**

> **Тема**: Разработка распределённой системы управления онлайн-магазином на основе микросервисной архитектуры (6 независимых бизнес-сервисов + API Gateway, *Database per Service*, синхронный REST, асинхронная событийная хореография RabbitMQ, Saga-компенсация).  
> **Выполнил**: Bohdan Kondratiuk.

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

**ShopFlow** — полноценное распределённое backend-приложение для электронной коммерции, разделённое на **6 независимых бизнес-микросервисов** и **API Gateway**. В отличие от монолита, каждая предметная область изолирована в собственном сервисе с отдельной базой данных, независимым циклом запуска, конфигурацией, обработкой ошибок и структурированным JSON-логированием.

### Ключевые возможности
- **6 бизнес-микросервисов + API Gateway**:
  1. `auth-service` (`:8001`, `auth_db`) — регистрация, вход, PBKDF2-SHA256 хеширование паролей, выпуск и ротация JWT Access/Refresh токенов, отзыв токенов по `jti`, RBAC (`customer`, `seller`, `admin`).
  2. `user-service` (`:8002`, `user_db`) — профили пользователей, уровни лояльности (`standard`, `silver`, `gold`), управление адресами доставки, автосоздание профиля по событию регистрации.
  3. `product-service` (`:8003`, `product_db`) — каталог товаров, категории, проверка уникальности SKU, резервирование и освобождение складских остатков (`StockReservation` с идемпотентностью по `order_id`), Saga-компенсация.
  4. `order-service` (`:8004`, `order_db`) — жизненный цикл заказов (`pending` → `paid` / `payment_failed` / `cancelled`), проверка пользователя, атомарное резервирование позиций заказа с откатом при частичной нехватке товара, оркестрация оплаты.
  5. `payment-service` (`:8005`, `payment_db`) — обработка платежей с идемпотентностью (`idempotency_key` / `order_id`), лимит суммы транзакции, возвраты (`refund`), симуляция отказа для проверки Saga.
  6. `notification-service` (`:8006`, `notification_db`) — событийный центр уведомлений с дедупликацией по `event_id`, фильтрацией непрочитанных и массовым прочтением.
  7. `api-gateway` (`:8000`, stateless) — маршрутизация `/api/*`, проверка JWT и RBAC, Sliding-Window Rate Limiter (`X-RateLimit-*`, HTTP 429), параллельная агрегация `GET /api/dashboard` с *graceful degradation*.
- **Отдельный Frontend-сервис (`frontend` на NGINX `:8080`) и сетевая изоляция**: в `docker-compose.yml` все базы данных, RabbitMQ, 6 бизнес-сервисов и `api-gateway` полностью закрыты от хоста (доступны только по внутренней bridge-сети `shopflow-net` через `expose:`). Наружу опубликован **только** `frontend:8080`, который отдаёт SPA-интерфейс и проксирует `/api/` на `http://api-gateway:8000`.
- **Синхронная + асинхронная коммуникация**: HTTP/REST (`httpx`) на критическом пути заказа + брокер сообщений **RabbitMQ** (`aio-pika`, Topic Exchange `shopflow.events`) с поддержкой `InMemoryEventBus` для быстрых детерминированных тестов без внешнего брокера.
- **Сквозная трассировка (`X-Request-ID`)**: единый идентификатор запроса проходит через API Gateway, все downstream HTTP-вызовы, payload событий RabbitMQ и JSON-логи каждого сервиса.
- **Готовность к контейнеризации и Kubernetes (Lab 3)**: каждый сервис имеет оптимизированный non-root `Dockerfile`, `.dockerignore`, `GET /health` и `GET /ready`.

---

## 2. Архитектура

### 2.1. Диаграмма микросервисов и хранилищ

```mermaid
flowchart TB
    Client([Browser / Client]) -->|:8080 ONLY| FE[frontend :8080<br/>NGINX Unprivileged SPA + Reverse Proxy]
    FE -->|internal /api/*| GW[API Gateway :8000<br/>JWT Auth, RBAC, Rate Limit, Aggregation]

    GW --> Auth[auth-service :8001]
    GW --> User[user-service :8002]
    GW --> Prod[product-service :8003]
    GW --> Order[order-service :8004]
    GW --> Pay[payment-service :8005]
    GW --> Notif[notification-service :8006]

    Order -->|REST: verify user| User
    Order -->|REST: reserve / release| Prod
    Order -->|REST: charge| Pay

    Auth --> DB1[(auth_db)]
    User --> DB2[(user_db)]
    Prod --> DB3[(product_db)]
    Order --> DB4[(order_db)]
    Pay --> DB5[(payment_db)]
    Notif --> DB6[(notification_db)]

    Auth -.->|user.registered| MQ((RabbitMQ<br/>shopflow.events))
    Order -.->|order.created / order.cancelled| MQ
    Pay -.->|payment.succeeded / failed / refunded| MQ

    MQ -.->|user.registered| User
    MQ -.->|payment.succeeded / failed| Order
    MQ -.->|payment.succeeded / failed / order.cancelled| Prod
    MQ -.->|user.*, order.*, payment.*| Notif
```

### 2.2. Сценарий заказа и Saga-компенсации

```mermaid
sequenceDiagram
    actor Buyer
    participant GW as api-gateway
    participant Order as order-service
    participant User as user-service
    participant Prod as product-service
    participant Pay as payment-service
    participant MQ as RabbitMQ
    participant Notif as notification-service

    Buyer->>GW: POST /api/orders (Bearer JWT)
    GW->>Order: POST /orders (X-User-Id, X-Request-ID)
    Order->>User: GET /users/{id}
    Order->>Prod: POST /products/{id}/reserve (order_id)
    Order->>MQ: publish order.created
    Order->>Pay: POST /payments (idempotency_key)
    alt Payment Succeeded
        Pay->>MQ: publish payment.succeeded
        MQ-->>Prod: commit stock reservation
        MQ-->>Order: status = paid
        MQ-->>Notif: create payment receipt
    else Payment Declined (Saga Rollback)
        Pay->>MQ: publish payment.failed
        MQ-->>Prod: release reserved stock
        MQ-->>Order: status = payment_failed
        MQ-->>Notif: create decline alert
    end
    Order-->>GW: OrderResponse
    GW-->>Buyer: HTTP 201 + X-Request-ID
```

### 2.3. Структура проекта

```text
Lab2/
├── Instruction.md                  # Задание лабораторной работы №2
├── PLAN.md                         # Архитектурный план реализации
├── README.md                       # Итоговый отчёт
├── docker-compose.yml              # PostgreSQL (6 БД), RabbitMQ, 6 сервисов + API Gateway
├── pytest.ini                      # Конфигурация pytest и pythonpath для всех сервисов
├── requirements.txt                # Единые зависимости для локального запуска и тестов
├── .env.example                    # Шаблон конфигурации окружения
├── infrastructure/
│   └── postgres/
│       └── init-databases.sql      # Инициализация 6 изолированных БД (auth_db..notification_db)
├── shared/                         # Общая инфраструктурная библиотека
│   ├── app_factory.py              # Сборка FastAPI-приложения, probes и lifespan
│   ├── config.py                   # BaseServiceSettings
│   ├── database.py                 # DatabaseManager (SQLite локально / PostgreSQL в Docker)
│   ├── errors.py                   # Иерархия AppError и безопасные exception handlers (без stack trace)
│   ├── health.py                   # /health и /ready endpoints
│   ├── logging.py                  # JSONFormatter + ContextVar request_id
│   ├── messaging.py                # EventBus: RabbitMQEventBus (aio-pika) + InMemoryEventBus
│   ├── middleware.py               # Pure ASGI RequestContextMiddleware
│   └── security.py                 # PBKDF2-SHA256, PyJWT, извлечение UserContext и RBAC
├── services/
│   ├── auth-service/               # Dockerfile, .dockerignore, requirements.txt, auth_service/
│   ├── user-service/               # Dockerfile, .dockerignore, requirements.txt, user_service/
│   ├── product-service/            # Dockerfile, .dockerignore, requirements.txt, product_service/
│   ├── order-service/              # Dockerfile, .dockerignore, requirements.txt, order_service/
│   ├── payment-service/            # Dockerfile, .dockerignore, requirements.txt, payment_service/
│   └── notification-service/       # Dockerfile, .dockerignore, requirements.txt, notification_service/
├── gateway/                        # Dockerfile, .dockerignore, requirements.txt, gateway_app/
├── frontend/                       # Отдельный NGINX-сервис: Dockerfile, nginx.conf, public/
├── scripts/
│   └── run_experiments.py          # Скрипт проведения 4 архитектурных бенчмарков
├── experiments/                    # Результаты экспериментов в CSV
├── docs/
│   ├── architecture.md
│   └── api_reference.md
└── tests/                          # Модульные, интеграционные и E2E тесты (20 тестов)
```

---

## 3. Используемые технологии

| Компонент | Выбранная технология | Обоснование выбора | Рассмотренные альтернативы |
| --- | --- | --- | --- |
| **Backend Framework** | **Python 3.13 + FastAPI** | Высокая скорость разработки, нативный `async/await` (критично для Gateway fan-out), автоматическая генерация OpenAPI/Swagger UI и строгая валидация схем через Pydantic v2. | Flask (нет нативного async и OpenAPI из коробки), Django REST Framework (тяжеловесен для лёгких микросервисов), Go/Gin (меньше интеграции с Python-экосистемой курса). |
| **База данных (Persistence)** | **PostgreSQL 16 (6 отдельных БД) + SQLAlchemy 2.0** (SQLite для локальных тестов) | Паттерн *Database per Service* гарантирует слабую связанность. PostgreSQL даёт строгую ACID-консистентность для резерваций и платежей; SQLAlchemy позволяет прозрачно запускать тесты в изолированных SQLite-файлах. | Одна общая БД (антипаттерн, создающий скрытую связанность), MongoDB (сложнее обеспечивать транзакционность инвентаря и идемпотентность платежей). |
| **Асинхронные сообщения** | **RabbitMQ (`aio-pika`, Topic Exchange) + абстракция `EventBus`** | Гибкая маршрутизация событий по шаблонам (`payment.*`, `user.registered`), долговечные очереди (`durable=True`), простой менеджмент и низкий порог эксплуатации. Абстракция `EventBus` позволяет переключаться на `InMemoryEventBus` в тестах. | Apache Kafka (избыточен для объёма транзакционных событий магазина и требует тяжёлого брокера/KRaft), Redis Streams (менее развитая маршрутизация по topic patterns). |
| **API Gateway** | **Собственный Gateway на FastAPI + `httpx.AsyncClient`** | Полный контроль над JWT-интроспекцией, RBAC, Sliding-Window Rate Limiter, пробросом `X-Request-ID` и кастомной параллельной агрегацией `/api/dashboard` с *graceful degradation*. | NGINX / Kong / Traefik (сложнее реализовать программную бизнес-агрегацию `/api/dashboard` и пользовательский формат ошибок без плагинов). |
| **Аутентификация и безопасность** | **PyJWT (HS256) + PBKDF2-HMAC-SHA256** | Стандартная криптография `hashlib.pbkdf2_hmac` без проблем сборки нативных C-расширений на новых версиях Python, stateless Access/Refresh токены с отзывом по `jti` для мгновенного `logout`. | Session Cookies в Redis (требуют централизованного состояния на каждый запрос), `passlib` (устаревающий wrapper с проблемами совместимости в Python 3.13+). |
| **Тестирование** | **pytest + pytest-asyncio + httpx `ASGITransport`** | Позволяет тестировать все 6 сервисов и API Gateway с реальными HTTP-вызовами и изолированными БД внутри одного процесса за < 1 секунды. | Ручное тестирование через Postman (не воспроизводится в CI). |

---

## 4. Установка

### Локальная установка (без Docker)

1. Перейдите в директорию `Lab2`:
   ```bash
   cd Lab2
   ```
2. Создайте и активируйте виртуальное окружение (или используйте `uv`):
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
3. Скопируйте файл конфигурации (опционально для локального запуска):
   ```bash
   cp .env.example .env
   ```

---

## 5. Запуск

### Вариант А. Запуск всей платформы в Docker Compose (PostgreSQL + RabbitMQ + 7 сервисов)

```bash
cd Lab2
docker compose up --build
```

После запуска:
- **Frontend UI (единственный внешний порт)**: `http://localhost:8080`
- **API через встроенный прокси Frontend**: `http://localhost:8080/api/*` (и Swagger UI на `http://localhost:8080/docs`, `/ready`)
- Все внутренние сервисы (`postgres`, `rabbitmq`, `auth-service`..`notification-service`, `api-gateway`) закрыты от хоста (`expose:` вместо `ports:`) и общаются исключительно внутри сети `shopflow-net`.

> *Примечание по среде проверки*: конфигурация `Lab2/docker-compose.yml` валидирована командой `docker compose -f Lab2/docker-compose.yml config --quiet`, однако на данной машине Docker-демон не был запущен в момент проверки, поэтому полная интеграционная верификация и эксперименты выполнялись на реальных ASGI-приложениях через `httpx.ASGITransport` и SQLite.

### Вариант Б. Независимый запуск сервисов локально (через SQLite + `memory://`)

Каждый сервис можно запустить независимо в отдельном терминале (из директории `Lab2/`):

```bash
PYTHONPATH=.:services/auth-service         RABBITMQ_URL=memory:// .venv/bin/uvicorn auth_service.main:create_app --factory --port 8001
PYTHONPATH=.:services/user-service         RABBITMQ_URL=memory:// .venv/bin/uvicorn user_service.main:create_app --factory --port 8002
PYTHONPATH=.:services/product-service      RABBITMQ_URL=memory:// .venv/bin/uvicorn product_service.main:create_app --factory --port 8003
PYTHONPATH=.:services/order-service        RABBITMQ_URL=memory:// USER_SERVICE_URL=http://127.0.0.1:8002 PRODUCT_SERVICE_URL=http://127.0.0.1:8003 PAYMENT_SERVICE_URL=http://127.0.0.1:8005 .venv/bin/uvicorn order_service.main:create_app --factory --port 8004
PYTHONPATH=.:services/payment-service      RABBITMQ_URL=memory:// .venv/bin/uvicorn payment_service.main:create_app --factory --port 8005
PYTHONPATH=.:services/notification-service RABBITMQ_URL=memory:// .venv/bin/uvicorn notification_service.main:create_app --factory --port 8006
PYTHONPATH=.:gateway                       AUTH_SERVICE_URL=http://127.0.0.1:8001 USER_SERVICE_URL=http://127.0.0.1:8002 PRODUCT_SERVICE_URL=http://127.0.0.1:8003 ORDER_SERVICE_URL=http://127.0.0.1:8004 PAYMENT_SERVICE_URL=http://127.0.0.1:8005 NOTIFICATION_SERVICE_URL=http://127.0.0.1:8006 .venv/bin/uvicorn gateway_app.main:create_app --factory --port 8000
```

---

## 6. Configuration

Все параметры вынесены в переменные окружения (`shared/config.py` и сервисные `config.py`):

| Переменная | Назначение | Значение по умолчанию (локально / Docker) |
| --- | --- | --- |
| `JWT_SECRET` | Секрет подписи HS256 JWT | `dev-super-secret-change-in-production-32chars` |
| `JWT_ALGORITHM` | Алгоритм подписи JWT | `HS256` |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | TTL access-токена (мин) | `30` |
| `REFRESH_TOKEN_EXPIRE_DAYS` | TTL refresh-токена (дни) | `7` |
| `RABBITMQ_URL` | URL брокера (`amqp://...` или `memory://`) | `memory://` / `amqp://guest:guest@rabbitmq:5672/` |
| `LOG_LEVEL` / `LOG_JSON` | Уровень и формат (JSON) логов | `INFO` / `true` |
| `*_DATABASE_URL` (`DATABASE_URL`) | Строка подключения SQLAlchemy для конкретного сервиса | `sqlite:///./<service>.db` / `postgresql+psycopg2://...` |
| `*_SERVICE_URL` | Адреса сервисов по DNS внутри сети | `http://<service-name>:<port>` |
| `RATE_LIMIT_REQUESTS` | Лимит запросов на окно (Gateway) | `60` |
| `RATE_LIMIT_WINDOW_SECONDS` | Размер скользящего окна Rate Limiter (сек) | `60` |
| `PROXY_TIMEOUT_SECONDS` | Таймаут проксирования в Gateway (сек) | `10.0` |

---

## 7. API

Полная спецификация приведена в [docs/api_reference.md](file:///Users/kboddya/Dev/Univ/PAD/Lab2/docs/api_reference.md), а также доступна интерактивно в Swagger UI на `/docs` каждого сервиса и Gateway.

### Основные endpoints (через API Gateway `:8000`)

| Группа | Endpoint | Доступ | Назначение |
| --- | --- | --- | --- |
| **Health** | `GET /health`, `GET /ready` | Public | Liveness- и агрегированная Readiness-пробы |
| **Auth** | `POST /api/auth/register`, `POST /api/auth/login`, `POST /api/auth/refresh` | Public | Регистрация, логин и ротация refresh-токена |
| **Auth** | `POST /api/auth/logout`, `GET /api/auth/me` | Auth | Отзыв токена (`jti`) и текущий пользователь |
| **Users** | `GET /api/users/{id}`, `PUT /api/users/{id}`, `POST /api/users/{id}/addresses` | Self / Admin | Профиль пользователя и адреса доставки |
| **Products** | `GET /api/products`, `GET /api/products/{id}` | Public | Поиск и фильтрация каталога товаров |
| **Products** | `POST /api/products`, `PUT /api/products/{id}`, `DELETE /api/products/{id}` | Seller / Admin | Управление товарами и складскими остатками |
| **Orders** | `POST /api/orders`, `GET /api/orders`, `GET /api/orders/{id}`, `POST /api/orders/{id}/cancel` | Auth | Оформление заказа (резерв + оплата) и отмена (компенсация) |
| **Payments** | `POST /api/payments`, `GET /api/payments/by-order/{order_id}`, `POST /api/payments/{id}/refund` | Auth | Идемпотентные платежи и возвраты |
| **Notifications** | `GET /api/notifications`, `GET /api/notifications/unread-count`, `PATCH /api/notifications/{id}/read` | Auth | Лента событийных уведомлений |
| **Aggregation** | `GET /api/dashboard` | Auth | Параллельная агрегация профиля, заказов, уведомлений и каталога |

### Единый формат ошибок (без утечки stack trace)

```json
{
  "error": {
    "code": "INSUFFICIENT_STOCK",
    "message": "Insufficient stock for 'ShopFlow Ultrabook 15': available 1, requested 3",
    "details": null,
    "request_id": "trace-req-998877",
    "service": "order-service",
    "timestamp": "2026-11-22T19:55:05.998815+00:00"
  }
}
```

### Формат структурированных логов

```json
{
  "timestamp": "2026-11-22T19:55:05.998965+00:00",
  "service": "api-gateway",
  "level": "INFO",
  "request_id": "69c9dd654eb641a8a0fa4f2b9daaf806",
  "message": "HTTP request completed",
  "method": "GET",
  "path": "/api/products",
  "status_code": 200,
  "duration_ms": 3.65
}
```

---

## 8. Примеры использования

### 1. Регистрация продавца и покупателя
```bash
# Регистрация продавца
curl -s -X POST http://localhost:8000/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"merchant@shopflow.io","username":"merchant","password":"password123","role":"seller"}'

# Регистрация покупателя
curl -s -X POST http://localhost:8000/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"buyer@shopflow.io","username":"alice","password":"password123","role":"customer"}'
```

### 2. Добавление товара продавцом и просмотр каталога
```bash
curl -s -X POST http://localhost:8000/api/products \
  -H "Authorization: Bearer $SELLER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"sku":"SF-LAP-15","name":"ShopFlow Ultrabook 15","category":"laptops","price":1200.0,"stock_quantity":5}'

curl -s "http://localhost:8000/api/products?in_stock=true"
```

### 3. Создание заказа (happy path) и проверка Saga-отката при ошибке оплаты
```bash
# Успешный заказ
curl -s -X POST http://localhost:8000/api/orders \
  -H "Authorization: Bearer $BUYER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"items":[{"product_id":"'$PRODUCT_ID'","quantity":1}],"shipping_address":"Bd. Dacia 24, Chisinau"}'

# Симуляция отказа платежа -> статус payment_failed и автоматический возврат резерва на склад
curl -s -X POST http://localhost:8000/api/orders \
  -H "Authorization: Bearer $BUYER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"items":[{"product_id":"'$PRODUCT_ID'","quantity":1}],"shipping_address":"Bd. Dacia 24","simulate_payment_failure":true}'
```

### 4. Агрегированный Dashboard и уведомления
```bash
curl -s http://localhost:8000/api/dashboard -H "Authorization: Bearer $BUYER_TOKEN"
curl -s http://localhost:8000/api/notifications -H "Authorization: Bearer $BUYER_TOKEN"
```

---

## 9. Тестирование

Набор тестов в [tests/](file:///Users/kboddya/Dev/Univ/PAD/Lab2/tests) покрывает бизнес-логику каждого сервиса по отдельности, сквозную маршрутизацию и защиту API Gateway, обработку ошибок/логирование и полный E2E Saga-сценарий:

- `test_auth_service.py` — регистрация, конфликт email (409), вход, ротация refresh-токена, отзыв токена при `logout`, отклонение истёкшего JWT (`TOKEN_EXPIRED`), пробы `/health` и `/ready`.
- `test_user_service.py` — автосоздание профиля по событию `user.registered`, управление адресами доставки, RBAC-защита поля `loyalty_tier` и изоляция чужих профилей.
- `test_product_service.py` — CRUD и фильтры каталога, уникальность SKU, идемпотентное резервирование, переполнение остатка (409), коммит по `payment.succeeded` и Saga-компенсация по `payment.failed`.
- `test_payment_service.py` — идемпотентность по `idempotency_key`, возвраты (`refund`), симуляция отказа и лимит суммы транзакции.
- `test_order_service.py` — успешное оформление и отмена заказа, откат первой позиции при нехватке второй позиции (409), компенсация резерва при падении `payment-service` (503).
- `test_notification_service.py` — подписки на 6 типов доменных событий, счётчик непрочитанных, `mark-all-read`, контроль доступа.
- `test_gateway.py` — JWT/RBAC, Sliding-Window Rate Limiter (429 + `X-RateLimit-*`), параллельная агрегация `/api/dashboard` с *graceful degradation*, маппинг таймаутов в 504.
- `test_error_handling_and_logging.py` — структура JSON-логов, сквозной `X-Request-ID`, отсутствие stack trace и внутренних деталей при `500`, `422` и `503` (`SQLAlchemyError`), проверка ASGI `lifespan`.
- `test_e2e_microservices.py` — полный путь покупателя и продавца через API Gateway (happy path + отказ оплаты с Saga-компенсацией + отмена оплаченного заказа + logout).

### Запуск и результат

```bash
cd Lab2
.venv/bin/pytest -v
```

Фактический результат прогона: **`22 passed in 0.9s`** (включая проверку сетевой изоляции `docker-compose.yml` и конфигурации `frontend/nginx.conf`).

---

## 10. Результаты экспериментов

Для объективного анализа принятых архитектурных решений разработан воспроизводимый скрипт [scripts/run_experiments.py](file:///Users/kboddya/Dev/Univ/PAD/Lab2/scripts/run_experiments.py), который замеряет характеристики работающей системы и сохраняет отчёты в [experiments/](file:///Users/kboddya/Dev/Univ/PAD/Lab2/experiments).

### Эксперимент 1. Параллельная агрегация `/api/dashboard` (`asyncio.gather`) vs последовательные запросы

**Что сравнивалось**: 4 вызова из Gateway (`user-service`, `order-service`, `notification-service`, `product-service`) последовательно против параллельного `asyncio.gather` при искусственной сетевой задержке каждого downstream-сервиса (`0`, `20`, `50`, `100` мс; усреднение по 5 запускам).  
Источник: [results_aggregation.csv](file:///Users/kboddya/Dev/Univ/PAD/Lab2/experiments/results_aggregation.csv)

| Задержка сервиса, мс | Последовательно (avg), мс | Параллельно `asyncio.gather` (avg), мс | Ускорение |
| ---: | ---: | ---: | ---: |
| 0 | 2.37 | 2.97 | 0.80x |
| 20 | 94.37 | 29.52 | 3.20x |
| 50 | 221.77 | 63.73 | 3.48x |
| 100 | 421.22 | 115.69 | **3.64x** |

**Вывод**: При нулевой сетевой задержке последовательные и параллельные вызовы сопоставимы (~2–3 мс), однако уже при реалистичных задержках 20–100 мс параллельный fan-out даёт ускорение в **3.2–3.64 раза**, так как суммарное время определяется самым медленным сервисом (`max`), а не суммой всех задержек (`sum`).

### Эксперимент 2. Синхронный HTTP fan-out vs Асинхронная событийная очередь (`EventBus`)

**Что сравнивалось**: время ожидания клиента/издателя при уведомлении `1..8` подписчиков (время обработки одного события у каждого подписчика — `25 мс`, 10 итераций).  
Источник: [results_sync_vs_async.csv](file:///Users/kboddya/Dev/Univ/PAD/Lab2/experiments/results_sync_vs_async.csv)

| Подписчиков ($N$) | Синхронный fan-out (avg), мс | Асинхронная публикация в шину (avg), мс | Полный дренаж фоновых воркеров, мс |
| ---: | ---: | ---: | ---: |
| 1 | 26.01 | 0.004 | 258.35 |
| 2 | 52.13 | 0.004 | 260.71 |
| 4 | 104.58 | 0.007 | 259.87 |
| 8 | 208.31 | 0.012 | 262.29 |

**Вывод**: При синхронной рассылке задержка ответа издателя растёт линейно $O(N)$ (с `26.01 мс` до `208.31 мс`). При переходе на брокер сообщений время публикации остаётся $O(1)$ (`< 0.015 мс`), а новые подписчики (`analytics`, `notification`) подключаются без деградации времени отклика критического пути заказа.

### Эксперимент 3. Работа Sliding-Window Rate Limiter под burst-нагрузкой

**Что сравнивалось**: серия из 60 последовательных запросов к `/api/products` через API Gateway при разных лимитах (`10`, `25`, `50`, `100` запросов в окно 60 с).  
Источник: [results_rate_limiting.csv](file:///Users/kboddya/Dev/Univ/PAD/Lab2/experiments/results_rate_limiting.csv)

| Лимит запросов | Пропущено (`HTTP 200`) | Отклонено (`HTTP 429`) | Доля отказов, % | Средняя задержка, мс |
| ---: | ---: | ---: | ---: | ---: |
| 10 | 10 | 50 | 83.3% | 0.32 |
| 25 | 25 | 35 | 58.3% | 0.40 |
| 50 | 50 | 10 | 16.7% | 0.57 |
| 100 | 60 | 0 | 0.0% | 0.70 |

**Вывод**: Rate Limiter на уровне Gateway отсекает всплеск трафика за `< 0.4 мс` до проксирования в бизнес-сервисы и базы данных, защищая downstream-сервисы от каскадной перегрузки.

### Эксперимент 4. Отказоустойчивость (`/api/dashboard`) при частичных и критических сбоях

**Что сравнивалось**: поведение агрегирующего endpoint'а `GET /api/dashboard` при принудительном отключении (`ConnectError`) различных комбинаций сервисов.  
Источник: [results_fault_tolerance.csv](file:///Users/kboddya/Dev/Univ/PAD/Lab2/experiments/results_fault_tolerance.csv)

| Сценарий | Отключённые сервисы | HTTP-статус | `degraded_count` | Доступно секций | Задержка, мс |
| --- | --- | ---: | ---: | ---: | ---: |
| Все работают | — | 200 | 0 | 4/4 | 10.74 |
| Сбой уведомлений | `notification-service` | 200 | 1 | 3/4 | 2.90 |
| Сбой уведомлений и заказов | `notification-service`, `order-service` | 200 | 2 | 2/4 | 2.18 |
| Сбой всех 4 сервисов dashboard | `user`, `order`, `notification`, `product` | 200 | 4 | 0/4 | 1.03 |
| Сбой критической аутентификации | `auth-service` | **503** | 1 | 0/4 | 0.69 |

**Вывод**: Система чётко разделяет критичные зависимости (`auth-service` при сбое корректно возвращает `503 Service Unavailable`) и некритичные секции агрегации (при падении `notification-service` пользователь продолжает видеть свой профиль, заказы и каталог, получая явный список `degraded_services`).

---

## 11. Выводы и ответы на вопросы к защите

В результате выполнения работы создана полноценная микросервисная платформа **ShopFlow**, состоящая из 6 независимых бизнес-микросервисов и API Gateway, удовлетворяющая всем обязательным и дополнительным архитектурным требованиям.

### Ответы на контрольные вопросы к защите

1. **Почему была выбрана именно эта технология и какие альтернативы рассматривались?**
   - **FastAPI + Pydantic v2 + httpx**: нативный `asyncio` критичен для API Gateway и I/O-интенсивного взаимодействия; автоматический OpenAPI избавляет от ручной поддержки документации. Альтернативы: Django/Flask (синхронные по умолчанию), Go (быстрее CPU-код, но не единый стек с Lab 1).
   - **PostgreSQL (отдельная БД на сервис)**: гарантирует ACID при списании/резерве остатков и идемпотентности платежей, не допуская скрытой связанности через общие SQL-таблицы. Альтернатива: общая БД или NoSQL (MongoDB).
   - **RabbitMQ**: надёжный AMQP-брокер с удобной topic-маршрутизацией. Альтернатива: Kafka (сложнее для малого числа сервисов) или NATS.

2. **Почему приложение разделено именно на такие микросервисы?**
   Разделение выполнено по принципу *Bounded Context* (DDD): безопасность и токены (`auth`), клиентские данные (`user`), склад и каталог (`product`), транзакционная координация заказов (`order`), биллинг (`payment`) и коммуникации с клиентом (`notification`). У каждого сервиса своя скорость изменений, нагрузка (каталог читается в разы чаще, чем проводятся платежи) и собственные требования к безопасности.

3. **Как сервисы взаимодействуют друг с другом?**
   Используется гибридный подход:
   - **Синхронно (REST/HTTP)** там, где клиенту нужен немедленный результат проверки (`order-service` → `user-service`, `product-service`, `payment-service`; `api-gateway` → все сервисы).
   - **Асинхронно (RabbitMQ Topic Exchange `shopflow.events`)** для слабой связанности, уведомлений и паттерна **Saga** (`user.registered`, `order.created`, `order.cancelled`, `payment.succeeded`, `payment.failed`, `payment.refunded`).

4. **Что произойдёт при падении одного сервиса?**
   - При падении `notification-service`: заказы и оплата продолжают работать без сбоев (события накапливаются в durability-очереди RabbitMQ), а `/api/dashboard` возвращает HTTP 200 с пометкой `degraded_services: ["notification-service"]`.
   - При падении `payment-service` во время оформления заказа: `order-service` ловит ошибку/таймаут, автоматически откатывает зарезервированные товары в `product-service`, переводит заказ в `payment_failed` и возвращает клиенту безопасный `503`/`504` без stack trace.
   - При падении `auth-service`: публичный просмотр каталога `GET /api/products` остаётся доступным, а защищённые операции возвращают контролируемый `503`.

5. **Как приложение масштабируется?**
   - Все 6 сервисов и API Gateway **stateless** (состояние хранится только в изолированных БД и RabbitMQ), а rate limiter и токены легко выносятся или уже используют JWT + внешнее хранилище.
   - Любой сервис (например, наиболее нагруженный `product-service` или `order-service`) масштабируется горизонтально запуском нескольких реплик за балансировщиком / Kubernetes Service, а конкурирующие потребители очередей RabbitMQ автоматически распределяют нагрузку событий между репликами.
