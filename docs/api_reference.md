# ShopFlow API Reference (через API Gateway `:8000`)

Все защищённые запросы принимают заголовок `Authorization: Bearer <access_token>` и поддерживают заголовок корреляции `X-Request-ID`.

## 1. Системные и агрегирующие endpoints

| Метод | Путь | Доступ | Описание |
| --- | --- | --- | --- |
| `GET` | `/health` | Public | Liveness-проба API Gateway |
| `GET` | `/ready` | Public | Агрегированная проверка готовности всех 6 downstream-сервисов |
| `GET` | `/api/dashboard` | Auth | Параллельная агрегация профиля, заказов, непрочитанных уведомлений и каталога (`degraded_services` при частичном сбое) |

## 2. Auth Service (`/api/auth/*`)

| Метод | Путь | Доступ | Описание |
| --- | --- | --- | --- |
| `POST` | `/api/auth/register` | Public | Регистрация пользователя (`customer`, `seller`, `admin`) + публикация `user.registered` |
| `POST` | `/api/auth/login` | Public | Аутентификация и выдача пары JWT (`access_token`, `refresh_token`) |
| `POST` | `/api/auth/refresh` | Public | Ротация refresh-токена и выдача новой пары JWT |
| `POST` | `/api/auth/logout` | Auth | Отзыв access (и опционально refresh) токена по `jti` |
| `GET` | `/api/auth/me` | Auth | Информация о текущем пользователе |

## 3. User Service (`/api/users/*`)

| Метод | Путь | Доступ | Описание |
| --- | --- | --- | --- |
| `GET` | `/api/users` | Admin | Список всех профилей пользователей |
| `GET` | `/api/users/{user_id}` | Self / Admin | Профиль пользователя и адреса доставки |
| `PUT` | `/api/users/{user_id}` | Self / Admin | Обновление имени, телефона (смена `loyalty_tier` только для `admin`) |
| `POST` | `/api/users/{user_id}/addresses` | Self / Admin | Добавление адреса доставки |
| `GET` | `/api/users/{user_id}/addresses` | Self / Admin | Список адресов доставки |

## 4. Product Service (`/api/products/*`)

| Метод | Путь | Доступ | Описание |
| --- | --- | --- | --- |
| `GET` | `/api/products` | Public | Каталог товаров с фильтрами `category`, `search`, `in_stock` |
| `GET` | `/api/products/{id}` | Public | Детальная карточка товара с `available_quantity` |
| `POST` | `/api/products` | Seller / Admin | Создание товара (проверка уникальности SKU) |
| `PUT` | `/api/products/{id}` | Owner Seller / Admin | Обновление цены, описания или складского остатка |
| `DELETE` | `/api/products/{id}` | Owner Seller / Admin | Деактивация товара (при отсутствии активного резерва) |

## 5. Order Service (`/api/orders/*`)

| Метод | Путь | Доступ | Описание |
| --- | --- | --- | --- |
| `POST` | `/api/orders` | Auth | Создание заказа: проверка профиля → резерв на складе → оплата |
| `GET` | `/api/orders` | Auth | Заказы текущего пользователя (для `admin` — все с фильтром `user_id`) |
| `GET` | `/api/orders/{id}` | Owner / Admin | Детали заказа и позиции |
| `POST` | `/api/orders/{id}/cancel` | Owner / Admin | Отмена заказа + публикация `order.cancelled` (возврат резерва) |
| `PATCH` | `/api/orders/{id}/status` | Admin | Перевод оплаченного заказа в `shipped` / `delivered` |

## 6. Payment Service (`/api/payments/*`)

| Метод | Путь | Доступ | Описание |
| --- | --- | --- | --- |
| `POST` | `/api/payments` | Auth | Идемпотентная обработка платежа (`idempotency_key` / `order_id`) |
| `GET` | `/api/payments` | Owner / Admin | История платежей |
| `GET` | `/api/payments/by-order/{order_id}` | Owner / Admin | Поиск платежа по `order_id` |
| `POST` | `/api/payments/{id}/refund` | Owner / Admin | Возврат успешного платежа + публикация `payment.refunded` |

## 7. Notification Service (`/api/notifications/*`)

| Метод | Путь | Доступ | Описание |
| --- | --- | --- | --- |
| `GET` | `/api/notifications` | Auth | Список уведомлений (фильтр `unread_only=true`) |
| `GET` | `/api/notifications/unread-count` | Auth | Количество непрочитанных уведомлений |
| `PATCH` | `/api/notifications/{id}/read` | Owner / Admin | Пометить уведомление прочитанным |
| `POST` | `/api/notifications/mark-all-read` | Auth | Пометить все уведомления пользователя прочитанными |
