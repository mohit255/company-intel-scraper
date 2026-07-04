FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (layer-cached)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY companies.json .
COPY db.py scraper.py company_scraper.py enrich_locations.py run_scrape.sh ./

RUN chmod +x run_scrape.sh

# DATABASE_URL must be provided at runtime
ENV DATABASE_URL=postgresql://localhost/company_intel

CMD ["/bin/bash", "run_scrape.sh"]