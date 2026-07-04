#!/usr/bin/env python3
"""
Company intelligence scraper — news, job listings, and product/pricing pages
for a configurable list of companies (see companies.json).

Data sources per company:
  news     Google News RSS (works for any company, no API key)
  jobs     public ATS APIs — Greenhouse / Lever / Workday, per config
  products the company's product or pricing page, with price extraction

Usage:
    python company_scraper.py                          # everything
    python company_scraper.py --only news --field AI   # filter
"""

import argparse
import asyncio
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from urllib.parse import quote, urlparse

import aiohttp
from bs4 import BeautifulSoup

import db
from scraper import USER_AGENT, DomainThrottle, parse_page

PRICE_RE = re.compile(r"[\$€£₹]\s?\d[\d,.]*")

# Standing market feeds — scraped like companies, but tagged with a topic so
# the site can filter IPO / market-update news separately.
MARKET_TOPICS = [
    {"name": "IPO Watch",           "field": "Markets", "topic": "ipo",
     "news_query": "upcoming IPO stock market listing debut"},
    {"name": "IPO India",           "field": "Markets", "topic": "ipo",
     "news_query": "IPO India GMP listing NSE BSE"},
    {"name": "US Stock Market",     "field": "Markets", "topic": "market",
     "news_query": "stock market today S&P 500 Nasdaq Dow"},
    {"name": "Indian Stock Market", "field": "Markets", "topic": "market",
     "news_query": "Sensex Nifty stock market today"},
]


# ---------------------------------------------------------------- storage

class CompanyStorage:
    """Writes to PostgreSQL (DATABASE_URL, default local company_intel)."""

    def __init__(self):
        self.conn = db.connect()
        db.init_db(self.conn)

    def save_companies(self, companies):
        self.conn.cursor().executemany(
            """INSERT INTO companies (name, field, website)
               VALUES (%s,%s,%s) ON CONFLICT (name) DO UPDATE SET
                   field = EXCLUDED.field, website = EXCLUDED.website""",
            [(c["name"], c["field"], c.get("website", "")) for c in companies])
        self.conn.commit()

    def save_company_image(self, name, image):
        self.conn.execute(
            "UPDATE companies SET image = %s WHERE name = %s", (image, name))
        self.conn.commit()

    def save_news(self, company, field, items, topic="general"):
        self.conn.cursor().executemany(
            """INSERT INTO news (company, field, title, link, source,
                                 published, source_url, topic)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (link) DO UPDATE SET
                   source_url = COALESCE(EXCLUDED.source_url,
                                         news.source_url)""",
            [(company, field, i["title"], i["link"], i["source"],
              db.parse_pubdate(i["published"]), i["source_url"], topic)
             for i in items])
        self.conn.commit()

    def save_jobs(self, company, field, items):
        self.conn.cursor().executemany(
            """INSERT INTO jobs (company, field, title, location, url,
                                 posted_at)
               VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (url) DO NOTHING""",
            [(company, field, i["title"], i["location"], i["url"],
              i["posted_at"]) for i in items])
        self.conn.commit()

    def save_product_page(self, company, field, url, title, prices, snippet,
                          image):
        self.conn.execute(
            """INSERT INTO products (company, field, url, page_title, prices,
                                     text_snippet, image)
               VALUES (%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (url) DO UPDATE SET
                   page_title = EXCLUDED.page_title,
                   prices = EXCLUDED.prices,
                   text_snippet = EXCLUDED.text_snippet,
                   image = COALESCE(EXCLUDED.image, products.image),
                   fetched_at = now()""",
            (company, field, url, title, json.dumps(prices), snippet, image))
        self.conn.commit()

    def counts(self):
        return {t: self.conn.execute(
                    f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
                for t in ("news", "jobs", "products")}

    def close(self):
        self.conn.close()


# ---------------------------------------------------------------- fetchers

async def fetch_news(session, query, limit):
    """Google News RSS search for an arbitrary query."""
    url = (f"https://news.google.com/rss/search?q={quote(query)}"
           f"&hl=en-US&gl=US&ceid=US:en")
    async with session.get(url) as resp:
        resp.raise_for_status()
        xml_text = await resp.text()

    items = []
    root = ET.fromstring(xml_text)
    for item in root.iter("item"):
        source = item.find("source")
        items.append({
            "title": item.findtext("title", ""),
            "link": item.findtext("link", ""),
            "source": source.text if source is not None else "",
            "source_url": source.get("url", "") if source is not None else "",
            "published": item.findtext("pubDate", ""),
        })
        if len(items) >= limit:
            break
    return items


async def fetch_jobs(session, company, limit):
    """Job listings via the company's public ATS API."""
    ats = company["ats"]
    kind = ats["type"]

    if kind == "greenhouse":
        url = f"https://boards-api.greenhouse.io/v1/boards/{ats['board']}/jobs"
        async with session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.json()
        return [{
            "title": j.get("title", ""),
            "location": (j.get("location") or {}).get("name", ""),
            "url": j.get("absolute_url", ""),
            "posted_at": j.get("updated_at", ""),
        } for j in data.get("jobs", [])[:limit]]

    if kind == "lever":
        url = f"https://api.lever.co/v0/postings/{ats['org']}?mode=json"
        async with session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.json()
        return [{
            "title": j.get("text", ""),
            "location": (j.get("categories") or {}).get("location", ""),
            "url": j.get("hostedUrl", ""),
            "posted_at": str(j.get("createdAt", "")),
        } for j in data[:limit]]

    if kind == "ashby":
        url = f"https://api.ashbyhq.com/posting-api/job-board/{ats['board']}"
        async with session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.json()
        return [{
            "title": j.get("title", ""),
            "location": j.get("location", ""),
            "url": j.get("jobUrl") or j.get("applyUrl", ""),
            "posted_at": j.get("publishedAt", ""),
        } for j in data.get("jobs", [])[:limit]]

    if kind == "workday":
        url = (f"https://{ats['host']}/wday/cxs/{ats['tenant']}"
               f"/{ats['site']}/jobs")
        payload = {"appliedFacets": {}, "limit": min(limit, 20),
                   "offset": 0, "searchText": ""}
        async with session.post(url, json=payload) as resp:
            resp.raise_for_status()
            data = await resp.json()
        return [{
            "title": j.get("title", ""),
            "location": j.get("locationsText", ""),
            "url": f"https://{ats['host']}/en-US/{ats['site']}"
                   f"{j.get('externalPath', '')}",
            "posted_at": j.get("postedOn", ""),
        } for j in data.get("jobPostings", [])[:limit]]

    raise ValueError(f"unknown ATS type: {kind}")


async def fetch_brand_image(session, company):
    """og:image (or twitter:image) of the company homepage — used as the
    card banner for news and jobs on the website."""
    async with session.get(company["website"]) as resp:
        resp.raise_for_status()
        html = await resp.text()
    soup = BeautifulSoup(html, "html.parser")
    tag = (soup.find("meta", attrs={"property": "og:image"})
           or soup.find("meta", attrs={"name": "twitter:image"}))
    return tag.get("content") if tag else None


async def fetch_products(session, company):
    """Fetch the company's product/pricing page, pull out price strings
    and the page's og:image for the card thumbnail."""
    url = company["products_url"]
    async with session.get(url) as resp:
        resp.raise_for_status()
        html = await resp.text()

    og = BeautifulSoup(html, "html.parser").find(
        "meta", attrs={"property": "og:image"})
    image = og.get("content") if og else None

    title, _, text, _ = parse_page(url, html)
    prices = list(dict.fromkeys(PRICE_RE.findall(text)))[:25]
    return url, title, prices, text[:2000], image


# ---------------------------------------------------------------- runner

async def worker(worker_id, queue, session, throttle, storage, cfg, stats):
    while True:
        kind, company = await queue.get()
        name, field = company["name"], company["field"]
        try:
            host = {"news": "news.google.com", "stocks": "news.google.com",
                    "market": "news.google.com", "jobs": "ats",
                    "products": None, "brand": None}[kind]
            if kind == "products":
                host = urlparse(company["products_url"]).netloc
            elif kind == "brand":
                host = urlparse(company["website"]).netloc
            if kind != "jobs":          # ATS endpoints are APIs, no throttle
                await throttle.wait(host)

            if kind == "news":
                query = company.get("news_query", name)
                items = await fetch_news(session, query, cfg.news_limit)
                storage.save_news(name, field, items)
                stats["news"] += len(items)
                print(f"[w{worker_id}] news     {name}: {len(items)} articles")
            elif kind == "stocks":
                query = f'{company.get("news_query", name)} stock price'
                items = await fetch_news(session, query, cfg.news_limit)
                storage.save_news(name, field, items, topic="stock")
                stats["news"] += len(items)
                print(f"[w{worker_id}] stocks   {name}: {len(items)} articles")
            elif kind == "market":
                items = await fetch_news(
                    session, company["news_query"], cfg.news_limit)
                storage.save_news(name, field, items,
                                  topic=company["topic"])
                stats["news"] += len(items)
                print(f"[w{worker_id}] market   {name}: {len(items)} articles")
            elif kind == "jobs":
                items = await fetch_jobs(session, company, cfg.jobs_limit)
                storage.save_jobs(name, field, items)
                stats["jobs"] += len(items)
                print(f"[w{worker_id}] jobs     {name}: {len(items)} openings")
            elif kind == "products":
                url, title, prices, snippet, image = await fetch_products(
                    session, company)
                storage.save_product_page(
                    name, field, url, title, prices, snippet, image)
                stats["products"] += 1
                print(f"[w{worker_id}] products {name}: "
                      f"{len(prices)} prices found")
            elif kind == "brand":
                image = await fetch_brand_image(session, company)
                if image:
                    storage.save_company_image(name, image)
                print(f"[w{worker_id}] brand    {name}: "
                      f"{'image found' if image else 'no og:image'}")
        except Exception as exc:
            stats["errors"] += 1
            print(f"[w{worker_id}] ERROR {kind}/{name}: {exc}",
                  file=sys.stderr)
        finally:
            queue.task_done()


async def run(cfg):
    with open(cfg.companies_file) as f:
        companies = json.load(f)
    if cfg.field:
        companies = [c for c in companies
                     if cfg.field.lower() in c["field"].lower()]

    queue = asyncio.Queue()
    for c in companies:
        if cfg.only in (None, "news"):
            queue.put_nowait(("news", c))
        if cfg.only in (None, "stocks"):
            queue.put_nowait(("stocks", c))
        if cfg.only in (None, "jobs") and c.get("ats"):
            queue.put_nowait(("jobs", c))
        if cfg.only in (None, "products") and c.get("products_url"):
            queue.put_nowait(("products", c))
        if cfg.only in (None, "brand") and c.get("website"):
            queue.put_nowait(("brand", c))
    if cfg.only in (None, "market") and not cfg.field:
        for topic in MARKET_TOPICS:
            queue.put_nowait(("market", topic))

    storage = CompanyStorage()
    storage.save_companies(companies)
    throttle = DomainThrottle(cfg.delay)
    stats = {"news": 0, "jobs": 0, "products": 0, "errors": 0}

    timeout = aiohttp.ClientTimeout(total=cfg.timeout)
    async with aiohttp.ClientSession(
            timeout=timeout, headers={"User-Agent": USER_AGENT}) as session:
        workers = [asyncio.create_task(worker(
            i, queue, session, throttle, storage, cfg, stats))
            for i in range(cfg.workers)]
        await queue.join()
        for w in workers:
            w.cancel()

    counts = storage.counts()
    storage.close()
    return len(companies), stats, counts


def main():
    ap = argparse.ArgumentParser(description="Company news/jobs/products scraper")
    ap.add_argument("--companies", dest="companies_file",
                    default="companies.json")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--news-limit", type=int, default=15,
                    help="Max news articles per company")
    ap.add_argument("--jobs-limit", type=int, default=50,
                    help="Max job listings per company")
    ap.add_argument("--only",
                    choices=["news", "stocks", "market", "jobs", "products",
                             "brand"],
                    help="Scrape only one data type")
    ap.add_argument("--field", help="Only companies whose field matches"
                    " (e.g. AI, Finance, Technology)")
    ap.add_argument("--delay", type=float, default=0.5)
    ap.add_argument("--timeout", type=float, default=20.0)
    args = ap.parse_args()

    start = time.monotonic()
    n_companies, stats, counts = asyncio.run(run(args))
    elapsed = time.monotonic() - start
    print(f"\nDone in {elapsed:.1f}s — {n_companies} companies, "
          f"{stats['errors']} errors this run")
    print(f"Database totals ({db.DB_URL}): "
          f"{counts['news']} news, {counts['jobs']} jobs, "
          f"{counts['products']} product pages")


if __name__ == "__main__":
    main()
