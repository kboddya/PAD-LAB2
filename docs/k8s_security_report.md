# Отчёт по аудиту безопасности Kubernetes-манифестов ShopFlow

**Методология**: Автоматизированный аудит [scripts/k8s_security_audit.py](file:///Users/kboddya/Dev/Univ/PAD/Lab2/scripts/k8s_security_audit.py) итогового вывода `kubectl kustomize k8s` на соответствие стандартам **CIS Kubernetes Benchmark** и **Pod Security Standards (Restricted)**.

- **Замер ДО исправлений** ([k8s_security_before.json](file:///Users/kboddya/Dev/Univ/PAD/Lab2/experiments/k8s_security_before.json)): **0/4 PASS, 4 FAIL**
- **Замер ПОСЛЕ исправлений** ([k8s_security_after.json](file:///Users/kboddya/Dev/Univ/PAD/Lab2/experiments/k8s_security_after.json)): **4/4 PASS, 0 FAIL**

---

## Сравнительная таблица результатов (До и После)

| ID | Проверка (Стандарт) | Критичность | Статус ДО | Доказательство ДО | Статус ПОСЛЕ | Доказательство ПОСЛЕ |
| --- | --- | --- | --- | --- | --- | --- |
| `K8S-01` | Отсутствие захардкоженных паролей в манифесте RabbitMQ (`CIS 5.4.1`) | **High** | **FAIL** | В `rabbitmq.yaml` заданы открытые `RABBITMQ_DEFAULT_USER`/`PASS` (`guest`) | **PASS** | Учётные данные передаются через `secretKeyRef: shopflow-secrets` |
| `K8S-02` | Принцип наименьших привилегий для `Secret` (`CIS 5.4.2`) | **High** | **FAIL** | Сервисы монтировали весь `shopflow-secrets` через `envFrom` (доступ к DSN чужих БД) | **PASS** | Точечный `secretKeyRef`: каждый из 12 бизнес-сервисов и Gateway получает только свои ключи |
| `K8S-03` | Профиль контейнеров `Pod Security Standards: Restricted` | **Medium** | **FAIL** | В Deployment отсутствовали `capabilities.drop=["ALL"]` и `seccompProfile: RuntimeDefault` | **PASS** | Все **14** прикладных Deployment сбрасывают `ALL` capabilities, используют `RuntimeDefault` seccomp и числовые UID (`10001`/`101`) |
| `K8S-04` | Сетевая микросегментация `NetworkPolicy` (`CIS 5.3.2`) | **High** | **FAIL** | Найдено `0` политик `NetworkPolicy` (`default-deny=False`) | **PASS** | Добавлено **17** политик `NetworkPolicy` (включая `default-deny-ingress` и точечные правила между всеми 12 сервисами и БД/MQ) |
