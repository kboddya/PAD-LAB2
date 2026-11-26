#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TAG="${IMAGE_TAG:-1.0.0}"

if [[ "${1:-}" == "--minikube" ]]; then
  echo "[build-images] Switching Docker environment to Minikube daemon..."
  eval "$(minikube docker-env)"
fi

declare -a SERVICES=(
  "auth-service:services/auth-service/Dockerfile"
  "user-service:services/user-service/Dockerfile"
  "product-service:services/product-service/Dockerfile"
  "order-service:services/order-service/Dockerfile"
  "payment-service:services/payment-service/Dockerfile"
  "notification-service:services/notification-service/Dockerfile"
  "cart-service:services/cart-service/Dockerfile"
  "promotion-service:services/promotion-service/Dockerfile"
  "shipping-service:services/shipping-service/Dockerfile"
  "review-service:services/review-service/Dockerfile"
  "support-service:services/support-service/Dockerfile"
  "analytics-service:services/analytics-service/Dockerfile"
  "api-gateway:gateway/Dockerfile"
  "frontend:frontend/Dockerfile"
)

for entry in "${SERVICES[@]}"; do
  name="${entry%%:*}"
  dockerfile="${entry##*:}"
  echo "[build-images] Building shopflow/${name}:${TAG} ..."
  docker build \
    -t "shopflow/${name}:${TAG}" \
    -f "${REPO_ROOT}/${dockerfile}" \
    "${REPO_ROOT}"
done

echo "[build-images] Successfully built all 14 ShopFlow Docker images."
