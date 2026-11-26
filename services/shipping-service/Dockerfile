FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN addgroup --system --gid 10001 appgroup && adduser --system --uid 10001 --ingroup appgroup appuser

COPY services/shipping-service/requirements.txt ./requirements.txt
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY shared ./shared
COPY services/shipping-service/shipping_service ./shipping_service

RUN chown -R 10001:10001 /app
USER 10001:10001

EXPOSE 8009

CMD ["uvicorn", "shipping_service.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8009"]
