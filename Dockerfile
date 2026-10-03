FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY *.py ./
COPY pages ./pages
COPY fonts ./fonts
COPY sample ./sample
ENV PATH=/app/.venv/bin:$PATH DATA_DIR=/data
VOLUME /data
EXPOSE 8000
# No --access-logfile on purpose: request URLs contain recipients' first names.
CMD ["gunicorn", "-b", "0.0.0.0:8000", "-w", "2", "--threads", "4", "app:app"]
