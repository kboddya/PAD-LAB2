# Архитектура системы ShopFlow (12 бизнес-микросервисов)

## 1. Общая схема взаимодействия

```mermaid
flowchart TB
    Client([Browser / Client]) -->|":8080 ONLY"| FE[frontend :8080<br/>NGINX Unprivileged SPA + Proxy]
    FE -->|"internal /api/*"| GW[api-gateway :8000<br/>JWT Auth, RBAC, Rate Limit, 7-way Aggregation]

    subgraph Core["Базовое ядро (сервисы 1-6)"]
        Auth[auth-service :8001<br/>auth_db]
        User[user-service :8002<br/>user_db]
        Prod[product-service :8003<br/>product_db]
        Order[order-service :8004<br/>order_db]
        Pay[payment-service :8005<br/>payment_db]
        Notif[notification-service :8006<br/>notification_db]
    end

    subgraph Extended["Расширение до 12 сервисов (сервисы 7-12)"]
        Cart[cart-service :8007<br/>cart_db]
        Promo[promotion-service :8008<br/>promotion_db]
        Ship[shipping-service :8009<br/>shipping_db]
        Rev[review-service :8010<br/>review_db]
        Sup[support-service :8011<br/>support_db]
        Ana[analytics-service :8012<br/>analytics_db]
    end

    GW --> Core
    GW --> Extended

    Cart -->|"REST: stock & prices"| Prod
    Cart -->|"REST: validate coupon"| Promo
    Cart -->|"REST: loyalty tier"| User
    Cart -->|"REST: checkout"| Order

    Order -->|"REST: verify user"| User
    Order -->|"REST: reserve / release"| Prod
    Order -->|"REST: redeem / release"| Promo
    Order -->|"REST: shipping quote"| Ship
    Order -->|"REST: charge"| Pay

    Rev -->|"REST: check product"| Prod
    Rev -->|"REST: verify purchase"| Order

    Sup -->|"REST: tier"| User
    Sup -->|"REST: verify order"| Order
    Sup -->|"REST: check shipment"| Ship
    Sup -->|"REST: approve refund"| Pay

    MQ((RabbitMQ<br/>shopflow.events))
    Auth & Order & Pay & Prod & Promo & Ship & Rev & Sup & Ana -.->|"publish / consume"| MQ
    User & Notif & Cart -.->|"consume"| MQ
```

## 2. Расширенный паттерн Saga, Fulfillment и CQRS

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

    Buyer->>GW: POST /api/cart/checkout (coupon_code, shipping_method)
    GW->>Cart: POST /cart/checkout
    Cart->>Order: POST /orders
    Order->>User: GET /users/{id}
    Order->>Prod: POST /products/{id}/reserve (order_id)
    Order->>Promo: POST /promotions/redeem (order_id)
    Order->>Ship: POST /shipping/quotes
    Order->>Pay: POST /payments (idempotency_key)
    alt Payment Succeeded
        Pay->>MQ: payment.succeeded
        MQ-->>Prod: commit stock reservation
        MQ-->>Promo: commit coupon + promotion.redeemed
        MQ-->>Cart: clear cart idempotently
        MQ-->>Ship: auto-create Shipment (SF-TRK-*)
        MQ-->>Ana: update GMV, top products & CustomerLtv
        Ana->>MQ: loyalty.upgraded (if LTV crosses threshold)
        MQ-->>User: promote loyalty_tier
    else Payment Declined (3-Way Saga Rollback)
        Pay->>MQ: payment.failed
        MQ-->>Prod: release reserved stock
        MQ-->>Promo: release reserved coupon quota
        MQ-->>Order: status = payment_failed
        MQ-->>Ana: record failed metric
    end
```

## 3. Принятые архитектурные решения

1. **Database per Service (12 БД)**: каждый из 12 микросервисов владеет собственной схемой данных (`auth_db` .. `analytics_db`), полностью исключая связанность на уровне SQL-таблиц.
2. **Ацикличный граф синхронных зависимостей (DAG) + Хореография RabbitMQ**:
   - Критичный путь оформления заказа, промокодов, расчёта доставки, верификации покупок и возвратов в саппорте работает синхронно через REST с таймаутами и компенсацией.
   - Побочные эффекты (создание профиля, welcome-купоны, очистка корзины, создание отгрузок, обновление рейтингов товаров, CQRS-проекции аналитики и автоматический апгрейд лояльности) выполняются асинхронно через Topic Exchange `shopflow.events`.
3. **CQRS Read Model (`analytics-service`)**: хранит денормализованные агрегаты (`DailyPlatformMetric`, `ProductAnalytics`, `CustomerLtv`) с дедупликацией по `event_id` (`ProcessedAnalyticsEvent`), не нагружая OLTP-таблицы `order-service` и `payment-service`.
4. **Единый `X-Request-ID`**: сквозной идентификатор запроса пробрасывается через Gateway, downstream HTTP-заголовки и `EventEnvelope.request_id`.
