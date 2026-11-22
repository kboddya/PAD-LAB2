FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser

COPY services/user-service/requirements.txt ./requirements.txt
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY shared ./shared
COPY services/user-service/user_service ./user_service

RUN chown -R appuser:appgroup /app
USER appuser

EXPOSE 8002

CMD ["uvicorn", "user_service.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8002"]
