# Отчёт по тестированию безопасности ShopFlow (`Lab2`)

**Методология**: Автоматизированный активный аудит [scripts/security_audit.py](file:///Users/kboddya/Dev/Univ/PAD/Lab2/scripts/security_audit.py) на живом ASGI-кластере (все 6 микросервисов + API Gateway) и аудит конфигурации [frontend/nginx.conf](file:///Users/kboddya/Dev/Univ/PAD/Lab2/frontend/nginx.conf) на базе **OWASP API Security Top 10 (2023)** и Web Hardening.

- **Замер ДО исправлений** ([security_before.json](file:///Users/kboddya/Dev/Univ/PAD/Lab2/experiments/security_before.json), `2026-11-22T20:35:26Z`): **2/8 PASS, 6 FAIL**
- **Замер ПОСЛЕ исправлений** ([security_after.json](file:///Users/kboddya/Dev/Univ/PAD/Lab2/experiments/security_after.json), `2026-11-22T20:38:46Z`): **8/8 PASS, 0 FAIL**

---

## Сравнительная таблица результатов (До и После)

| ID | Проверка (OWASP API 2023) | Критичность | Статус ДО | Доказательство ДО | Статус ПОСЛЕ | Доказательство ПОСЛЕ |
| --- | --- | --- | --- | --- | --- | --- |
| `SEC-01` | Саморегистрация с ролью `admin` (`API5`) | **Critical** | **FAIL** | `POST /api/auth/register` (`role='admin'`) вернул `HTTP 201` | **PASS** | Возвращён `HTTP 422` (публично разрешены только `customer`/`seller`) |
| `SEC-02` | Спуфинг `X-User-Role: admin` без внутреннего токена (`API2/API5`) | **Critical** | **FAIL** | Прямой `GET /users` с поддельным заголовком вернул `HTTP 200` | **PASS** | Возвращён `HTTP 401` (`X-Internal-Token` обязателен и проверяется через `hmac.compare_digest`) |
| `SEC-03` | Незащищённые внутренние endpoints и чужие платежи (`API1 BOLA`) | **High** | **FAIL** | `POST /users=201`, `POST /products/reserve=200`, cross-user `POST /payments=201` | **PASS** | `POST /users=401`, `POST /products/reserve=401`, cross-user `POST /payments=403` |
| `SEC-04` | Обход Rate Limiter через подмену `X-Forwarded-For` (`API4`) | **High** | **FAIL** | 5 запросов с разным `X-Forwarded-For` дали `[200, 200, 200, 200, 200]` | **PASS** | Недоверенный `X-Forwarded-For` игнорируется (`[200, 200, 200, 429, 429]`) |
| `SEC-05` | Слабая парольная политика (`password='123456'`) (`API2`) | **Medium** | **FAIL** | Регистрация с `'123456'` прошла (`HTTP 201`) | **PASS** | Отклонено с `HTTP 422` (минимум 8 символов, буква + цифра) |
| `SEC-06` | Отсутствие security-заголовков на Gateway и NGINX (`API8`) | **Medium** | **FAIL** | `gateway_security_headers=False`, `nginx_hardening=False` | **PASS** | Gateway выставляет `nosniff`, `DENY`, `no-store`; NGINX — `server_tokens off`, `CSP`, `client_max_body_size 1m` |
| `SEC-07` | Защита от SQL-инъекций и XSS (`Injection / XSS`) | **High** | **PASS** | Поиск/логин `' OR '1'='1` безопасны (`200 matches=0`, `401`), `esc()` присутствует | **PASS** | Подтверждено; дополнительно убраны inline `onclick` в пользу строгого CSP `script-src 'self'` |
| `SEC-08` | Отзыв JWT при logout и сокрытие stack trace (`API2/API8`) | **Medium** | **PASS** | После logout `HTTP 401`, при `422` `Traceback` не раскрыт | **PASS** | Подтверждено |

---

## Внесённые исправления

1. **Защита межсервисного периметра (`shared/security.py`, `shared/config.py`)**:
   - Введён `INTERNAL_SERVICE_TOKEN`. Функция `extract_user_context` доверяет заголовкам `X-User-Id` и `X-User-Role` **только** при валидном `X-Internal-Token` (`hmac.compare_digest`), иначе требует подписанный Bearer JWT.
   - Внутренние мутации `POST /users`, `POST /products/{id}/reserve`, `POST /products/{id}/release` закрыты проверкой `is_valid_internal_token` / привилегированной роли; `POST /payments` запрещает инициировать оплату за другого пользователя (`403`).
2. **Защита от эскалации привилегий и слабых паролей (`auth_service/schemas.py`, `frontend/public/index.html`)**:
   - Из публичной модели `RegisterRequest` и UI-формы убрана роль `admin` (оставлены `customer` и `seller`).
   - Введено требование к паролю: минимум 8 символов, хотя бы одна буква и одна цифра.
3. **Защита Rate Limiter от IP-спуфинга (`gateway_app/rate_limiter.py`, `gateway_app/config.py`)**:
   - Добавлен флаг `trust_proxy_headers` (по умолчанию `False`). Недоверенный `X-Forwarded-For` от клиента игнорируется; за NGINX (`TRUST_PROXY_HEADERS=true`) используется перезаписываемый прокси-заголовок `X-Real-IP`.
4. **HTTP Security Headers и строгий CSP (`gateway_app/main.py`, `frontend/nginx.conf`, `frontend/public/app.js`)**:
   - На API Gateway добавлен `SecurityHeadersMiddleware` (`X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy`, `Cache-Control: no-store`).
   - В NGINX добавлены `server_tokens off;`, `client_max_body_size 1m;`, заголовки безопасности и строгий `Content-Security-Policy` с `script-src 'self'` (без `'unsafe-inline'`, обработчики в `app.js` переведены на делегирование `data-action`).
