FROM python:3.12-slim
WORKDIR /service
COPY pyproject.toml requirements.lock ./
COPY app ./app
RUN pip install --no-cache-dir -r requirements.lock && pip install --no-cache-dir --no-deps . && useradd --create-home trader
COPY alembic.ini ./
COPY migrations ./migrations
USER trader
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
