FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --create-home --uid 1001 kryten

COPY . /tmp/kryten-economy

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir /tmp/kryten-economy

RUN mkdir -p /etc/kryten/kryten-economy /var/lib/kryten/kryten-economy \
    && chown -R kryten:kryten /etc/kryten /var/lib/kryten

USER kryten

ENTRYPOINT ["python", "-m", "kryten_economy"]
CMD ["--config", "/etc/kryten/kryten-economy/config.yaml"]
