# Архитектура системы ShopFlow

## 1. Общая схема взаимодействия

```mermaid
flowchart TB
    Client([Client / Frontend / CLI]) --> GW[API Gateway :8000<br/>JWT Auth, RBAC, Rate Limit, Aggregation]

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

## 2. Паттерн Saga (хореография с компенсацией)

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

## 3. Принятые архитектурные решения

1. **Database per Service**: 6 независимых БД (`auth_db` .. `notification_db`) исключают скрытую связанность через общие SQL-таблицы и позволяют масштабировать или менять СУБД каждого сервиса отдельно.
2. **Гибридная коммуникация (REST + RabbitMQ)**:
   - Критичный путь оформления заказа (`verify user`, `reserve stock`, `charge payment`) работает синхронно по REST для мгновенного отклика покупателю.
   - Побочные эффекты (создание профиля, уведомления, финальный коммит/откат склада, обновление статусов) идут асинхронно через RabbitMQ Topic Exchange `shopflow.events` с идемпотентной обработкой по `event_id` и `order_id`.
3. **Единый `X-Request-ID`**: генерируется или принимается на Gateway и пробрасывается как по HTTP-заголовкам, так и внутри `EventEnvelope.request_id` в RabbitMQ, связывая распределённые JSON-логи.
