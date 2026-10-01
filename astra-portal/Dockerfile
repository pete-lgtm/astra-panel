FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock \
    && groupadd --gid 10001 astra \
    && useradd --uid 10001 --gid 10001 --no-create-home astra \
    && install -d -o astra -g astra -m 0700 /var/lib/astra
COPY app.py protocol.py store.py manage.py ./
COPY static ./static
USER 10001:10001
EXPOSE 8000
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "4", "--timeout", "20", "--umask", "0o077", "--worker-tmp-dir", "/dev/shm", "app:create_app()"]
