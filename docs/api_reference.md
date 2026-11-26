# ShopFlow API Reference (через API Gateway `:8000`)

Все защищённые запросы принимают заголовок `Authorization: Bearer <access_token>` и поддерживают заголовок корреляции `X-Request-ID`.

## 1. Системные и агрегирующие endpoints

| Метод | Путь | Доступ | Описание |
| --- | --- | --- | --- |
| `GET` | `/health` | Public | Liveness-проба API Gateway |
| `GET` | `/ready` | Public | Агрегированная проверка готовности всех **12** бизнес-микросервисов |
| `GET` | `/api/dashboard` | Auth | Параллельная 7-поточная агрегация: профиль, заказы, уведомления, каталог, корзина, доставки и LTV-лояльность (`degraded_services` при частичных сбоях) |

## 2. Auth Service (`/api/auth/*`)

| Метод | Путь | Доступ | Описание |
| --- | --- | --- | --- |
| `POST` | `/api/auth/register` | Public | Регистрация (`customer`, `seller`) + событие `user.registered` |
| `POST` | `/api/auth/login` | Public | Аутентификация и выдача пары JWT (`access_token`, `refresh_token`) |
| `POST` | `/api/auth/refresh` | Public | Ротация refresh-токена |
| `POST` | `/api/auth/logout` | Auth | Отзыв токена по `jti` |
| `GET` | `/api/auth/me` | Auth | Информация о текущем пользователе |

## 3. User Service (`/api/users/*`)

| Метод | Путь | Доступ | Описание |
| --- | --- | --- | --- |
| `GET` | `/api/users` | Admin | Список всех профилей |
| `GET` | `/api/users/{user_id}` | Self / Admin | Профиль пользователя, `loyalty_tier` и адреса |
| `PUT` | `/api/users/{user_id}` | Self / Admin | Обновление профиля (ручная смена `loyalty_tier` только для `admin`) |
| `POST` | `/api/users/{user_id}/addresses` | Self / Admin | Добавление адреса доставки |
| `GET` | `/api/users/{user_id}/addresses` | Self / Admin | Список адресов доставки |

## 4. Product Service (`/api/products/*`)

| Метод | Путь | Доступ | Описание |
| --- | --- | --- | --- |
| `GET` | `/api/products` | Public | Каталог товаров (`category`, `search`, `in_stock`) с рейтингом `average_rating` / `review_count` |
| `GET` | `/api/products/{id}` | Public | Детальная карточка товара с `available_quantity` и рейтингом |
| `POST` | `/api/products` | Seller / Admin | Создание товара (уникальность SKU) |
| `PUT` | `/api/products/{id}` | Owner / Admin | Обновление цены, описания или остатка (событие `product.deactivated` при выключении) |
| `DELETE` | `/api/products/{id}` | Owner / Admin | Деактивация товара + публикация `product.deactivated` |

## 5. Order Service (`/api/orders/*`)

| Метод | Путь | Доступ | Описание |
| --- | --- | --- | --- |
| `POST` | `/api/orders` | Auth | Создание заказа: проверка профиля → резерв склада → опц. купон (`coupon_code`) → расчёт доставки (`shipping_method`) → оплата |
| `GET` | `/api/orders` | Auth | Заказы пользователя (`admin` — все) |
| `GET` | `/api/orders/{id}` | Owner / Admin | Детали заказа (включая `subtotal_amount`, `discount_amount`, `shipping_cost`, `total_amount`) |
| `POST` | `/api/orders/{id}/cancel` | Owner / Admin | Отмена заказа + компенсация склада и промокода через `order.cancelled` |
| `PATCH` | `/api/orders/{id}/status` | Admin | Ручной перевод статуса заказа (`shipped`, `delivered`) |

## 6. Payment Service (`/api/payments/*`)

| Метод | Путь | Доступ | Описание |
| --- | --- | --- | --- |
| `POST` | `/api/payments` | Auth | Идемпотентная оплата (`idempotency_key` / `order_id`) |
| `GET` | `/api/payments` | Owner / Admin | История платежей |
| `GET` | `/api/payments/by-order/{order_id}` | Owner / Admin | Поиск платежа по `order_id` |
| `POST` | `/api/payments/{id}/refund` | Owner / Admin | Возврат средств + событие `payment.refunded` |

## 7. Notification Service (`/api/notifications/*`)

| Метод | Путь | Доступ | Описание |
| --- | --- | --- | --- |
| `GET` | `/api/notifications` | Auth | Лента уведомлений (`unread_only=true`) |
| `GET` | `/api/notifications/unread-count` | Auth | Счётчик непрочитанных |
| `PATCH` | `/api/notifications/{id}/read` | Owner / Admin | Отметка прочтения |
| `POST` | `/api/notifications/mark-all-read` | Auth | Массовое прочтение |

## 8. Cart Service (`/api/cart/*`)

| Метод | Путь | Доступ | Описание |
| --- | --- | --- | --- |
| `GET` | `/api/cart` | Auth | Серверная корзина с live-синхронизацией цен (`price_changed`), остатков и скидки |
| `POST` | `/api/cart/items` | Auth | Добавление товара с проверкой остатков в `product-service` |
| `PATCH` | `/api/cart/items/{product_id}` | Auth | Изменение количества (`0` удаляет позицию) |
| `DELETE` | `/api/cart/items/{product_id}` | Auth | Удаление позиции из корзины |
| `DELETE` | `/api/cart` | Auth | Полная очистка корзины |
| `POST` | `/api/cart/coupon` | Auth | Применение и валидация промокода через `promotion-service` |
| `DELETE` | `/api/cart/coupon` | Auth | Снятие промокода с корзины |
| `PATCH` | `/api/cart/shipping-method` | Auth | Сохранение способа доставки (`standard` / `express` / `pickup`) |
| `POST` | `/api/cart/checkout` | Auth | Оформление заказа в `order-service` (при успехе корзина очищается) |

## 9. Promotion Service (`/api/promotions/*`)

| Метод | Путь | Доступ | Описание |
| --- | --- | --- | --- |
| `GET` | `/api/promotions` | Public / Auth | Активные публичные кампании (+ персональные welcome-купоны при наличии JWT) |
| `POST` | `/api/promotions` | Seller / Admin | Создание купона (`percentage`/`fixed`, `min_order_amount`, `required_tier`, `category_filter`, квоты) |
| `POST` | `/api/promotions/validate` | Auth | Проверка правил и предпросмотр скидки без списания квоты |
| `PATCH` | `/api/promotions/{id}/deactivate` | Seller / Admin | Деактивация промо-кампании |

## 10. Shipping Service (`/api/shipping/*`, `/api/shipments/*`)

| Метод | Путь | Доступ | Описание |
| --- | --- | --- | --- |
| `POST` | `/api/shipping/quotes` | Public | Калькулятор тарифа (`standard`, `express`, `pickup`) с учётом `loyalty_tier` и суммы заказа |
| `GET` | `/api/shipments/track/{tracking_number}` | Public | Публичный трекинг по номеру `SF-TRK-*` с чекпоинтами (без утечки PII) |
| `GET` | `/api/shipments` | Auth | Список отправлений (свои для покупателя, все для `seller`/`admin`) |
| `GET` | `/api/shipments/by-order/{order_id}` | Owner / Seller / Admin | Отправление по `order_id` |
| `POST` | `/api/shipments/{id}/status` | Seller / Admin | Переход по конечному автомату (`dispatched`, `in_transit`, `delivered`, `cancelled`) + события в `order-service` |

## 11. Review Service (`/api/reviews/*`)

| Метод | Путь | Доступ | Описание |
| --- | --- | --- | --- |
| `GET` | `/api/reviews` | Public | Отзывы (фильтр `product_id`, `rating`; отклонённый/flagged спам скрыт от публики) |
| `GET` | `/api/reviews/product/{product_id}/summary` | Public | Средний рейтинг, количество и распределение оценок `1..5` |
| `POST` | `/api/reviews` | Auth | Создание отзыва только после подтверждённой покупки (**Verified Purchase** через `order-service`) + авто-модерация |
| `POST` | `/api/reviews/{id}/helpful` | Auth | Голос за полезность (защита от самолайка и повторного голоса) |
| `POST` | `/api/reviews/{id}/reply` | Seller / Admin | Ответ продавца на отзыв |
| `PATCH` | `/api/reviews/{id}/moderate` | Admin | Одобрение или отклонение flagged-отзыва |
| `DELETE` | `/api/reviews/{id}` | Author / Admin | Удаление отзыва с пересчётом рейтинга товара |

## 12. Support Service (`/api/support/*`)

| Метод | Путь | Доступ | Описание |
| --- | --- | --- | --- |
| `POST` | `/api/support/tickets` | Auth | Создание тикета/спора (проверка заказа и доставки, авто-расчёт `priority` и `sla_hours`) |
| `GET` | `/api/support/tickets` | Auth | Список тикетов (свои или все для `seller`/`admin`) |
| `GET` | `/api/support/tickets/{id}` | Owner / Seller / Admin | Тред тикета (внутренние заметки скрыты от клиента) |
| `POST` | `/api/support/tickets/{id}/messages` | Owner / Seller / Admin | Добавление сообщения в тред |
| `PATCH` | `/api/support/tickets/{id}/status` | Seller / Admin | Смена статуса тикета (`in_progress`, `resolved`, `rejected`, `closed`) |
| `POST` | `/api/support/tickets/{id}/approve-refund` | Admin | Оркестрация автоматического возврата средств через `payment-service` + закрытие спора |

## 13. Analytics Service (`/api/analytics/*`)

| Метод | Путь | Доступ | Описание |
| --- | --- | --- | --- |
| `GET` | `/api/analytics/trending` | Public | Топ трендовых товаров по продажам и рейтингу |
| `GET` | `/api/analytics/me/summary` | Auth | LTV покупателя, `computed_tier` и остаток до следующего уровня лояльности |
| `GET` | `/api/analytics/overview` | Seller / Admin | Агрегированные KPI (GMV, скидки, доставка, возвраты, Net Revenue, AOV, успешность оплат) |
| `GET` | `/api/analytics/products/top` | Seller / Admin | Топ товаров по выручке и проданным единицам |
| `GET` | `/api/analytics/funnel` | Seller / Admin | Воронка конверсии (`registered -> orders_created -> orders_paid -> delivered`) |
