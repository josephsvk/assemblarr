FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN groupadd --system assemblarr \
    && useradd --system --gid assemblarr --create-home --home-dir /home/assemblarr assemblarr

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg nodejs npm \
    && npm install -g synaudio-cli \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt

RUN python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

RUN chown -R assemblarr:assemblarr /app

USER assemblarr

CMD ["sleep", "infinity"]
