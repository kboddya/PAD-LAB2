# Архитектурный план Лабораторной работы №2: ShopFlow E-Commerce Platform (`Lab2/`)

## 1. Концепция проекта и предметная область

**ShopFlow** — распределённое backend-приложение для управления онлайн-магазином, спроектированное на базе микросервисной архитектуры. Платформа разделена на **6 независимых бизнес-микросервисов** и **API Gateway**, взаимодействующих как синхронно (HTTP/REST с таймаутами и обработкой отказов), так и асинхронно (событийная хореография через RabbitMQ Topic Exchange `shopflow.events` с поддержкой Saga-компенсации).

Каждый бизнес-сервис реализует паттерн **Database per Service** (изолированная база данных), имеет собственную конфигурацию, независимый цикл запуска, health/ready пробы, структурированное JSON-логирование и единый безопасный формат ошибок.

---

## 2. Состав сервисов и распределение ответственности

| Сервис | Порт | БД | Ответственность |
| --- | ---: | --- | --- |
| **`auth-service`** | `8001` | `auth_db` | Регистрация, логин, PBKDF2-SHA256 пароли, выпуск JWT Access/Refresh токенов, отзыв токенов (`jti`), RBAC (`customer`, `seller`, `admin`), интроспекция токена. |
| **`user-service`** | `8002` | `user_db` | Профили пользователей, уровни лояльности (`standard`, `silver`, `gold`), адреса доставки, авто-создание профиля по событию регистрации. |
| **`product-service`** | `8003` | `product_db` | Каталог товаров, категории, уникальность SKU (`409`), цены, резервирование и освобождение складских остатков (`reserve` / `release`), Saga-компенсация. |
| **`order-service`** | `8004` | `order_db` | Жизненный цикл заказов (`pending` → `paid` / `payment_failed` / `cancelled`), проверка пользователя, резервирование товаров, запуск оплаты. |
| **`payment-service`** | `8005` | `payment_db` | Проведение платежей с идемпотентностью по `idempotency_key` / `order_id`, поддержка возвратов (`refund`), симуляция отказа для тестирования Saga. |
| **`notification-service`** | `8006` | `notification_db` | Событийный центр уведомлений: приветствие, статус заказа, квитанция оплаты, ошибка оплаты, пометка прочитанным. |
| **`api-gateway`** | `8000` | — | Единая точка входа (`/api/*`), проверка JWT и RBAC, проброс контекста (`X-User-Id`, `X-User-Role`, `X-Request-ID`), Sliding-Window Rate Limiter, параллельная агрегация `/api/dashboard` с graceful degradation. |

---

## 3. Синхронное, асинхронное взаимодействие и Saga

### Синхронные вызовы (REST / HTTP)
- `Client → api-gateway → downstream`: проксирование с единым таймаутом и преобразованием `ConnectError → 503`, `TimeoutException → 504`.
- `api-gateway → user-service + product-service + order-service + notification-service`: параллельный `asyncio.gather` в `GET /api/dashboard` с graceful degradation.
- `order-service → user-service`: проверка существования профиля покупателя.
- `order-service → product-service`: резервирование позиций заказа (при ошибке резервирования второй позиции первая откатывается через `/release`).
- `order-service → payment-service`: инициация оплаты заказа.

### Асинхронная событийная хореография (RabbitMQ / InMemoryEventBus)
- `auth-service` публикует `user.registered` → `user-service` (создаёт профиль) и `notification-service` (создаёт welcome-уведомление).
- `order-service` публикует `order.created` → `notification-service`.
- `payment-service` публикует `payment.succeeded` → `order-service` (переводит заказ в `paid`) и `notification-service`.
- `payment-service` публикует `payment.failed` → `order-service` (статус `payment_failed`), `product-service` (Saga-компенсация: освобождает резерв товаров), `notification-service`.
- `order-service` публикует `order.cancelled` → `product-service` (освобождает резерв), `notification-service`.

---

## 4. Общая библиотека `shared/` и изоляция пакетов

Каждый микросервис оформлен как отдельный Python-пакет (`auth_service`, `user_service`, `product_service`, `order_service`, `payment_service`, `notification_service`, `gateway_app`), что полностью исключает конфликты `sys.modules` при совместном запуске в сквозных интеграционных тестах.

Общие инфраструктурные примитивы вынесены в `shared/`:
- `shared/config.py`: `BaseServiceSettings` на базе `pydantic-settings`.
- `shared/logging.py`: JSON-логгер (`timestamp`, `service`, `level`, `request_id`, `message`) с `contextvars`.
- `shared/errors.py`: иерархия `AppError` и единые FastAPI-обработчики без утечки stack trace.
- `shared/middleware.py`: чистое ASGI middleware для `X-Request-ID` и access-логов.
- `shared/security.py`: `PBKDF2-SHA256` и `PyJWT` (`HS256`).
- `shared/database.py`: `DatabaseManager` для SQLite (локально/тесты) и PostgreSQL (Docker).
- `shared/messaging.py`: абстракция `EventBus` с реализациями `RabbitMQEventBus` (`aio-pika`) и `InMemoryEventBus`.
