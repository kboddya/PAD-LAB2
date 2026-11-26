"""Configuration for api-gateway."""

from shared.config import BaseServiceSettings


class GatewaySettings(BaseServiceSettings):
    """Settings specific to api-gateway."""

    service_name: str = "api-gateway"
    service_port: int = 8000

    auth_service_url: str = "http://auth-service:8001"
    user_service_url: str = "http://user-service:8002"
    product_service_url: str = "http://product-service:8003"
    order_service_url: str = "http://order-service:8004"
    payment_service_url: str = "http://payment-service:8005"
    notification_service_url: str = "http://notification-service:8006"
    cart_service_url: str = "http://cart-service:8007"
    promotion_service_url: str = "http://promotion-service:8008"
    shipping_service_url: str = "http://shipping-service:8009"
    review_service_url: str = "http://review-service:8010"
    support_service_url: str = "http://support-service:8011"
    analytics_service_url: str = "http://analytics-service:8012"

    rate_limit_requests: int = 60
    rate_limit_window_seconds: int = 60
    proxy_timeout_seconds: float = 10.0
    verify_tokens_remotely: bool = True
    trust_proxy_headers: bool = False
