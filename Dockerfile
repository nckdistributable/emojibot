FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg procps fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py /app/

HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD pgrep -f "python bot.py" || exit 1

CMD ["python", "bot.py"]
