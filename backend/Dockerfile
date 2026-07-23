FROM beveradb/audio-separator:latest

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

ENTRYPOINT []
WORKDIR /app

COPY pyproject.toml ./
COPY app ./app
RUN --mount=type=cache,target=/root/.cache/pip pip install .

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
