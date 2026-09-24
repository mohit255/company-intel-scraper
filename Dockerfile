FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first (layer-cached)
COPY requirements.txt .
RUN pip install -r requirements.txt

# Copy source (scraper + proxy tooling)
COPY companies.json ./
COPY *.py *.sh ./

RUN chmod +x *.sh && mkdir -p logs

# DATABASE_URL comes from .env; localhost is rewritten to the Docker host
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["/bin/bash", "run_scrape.sh"]
