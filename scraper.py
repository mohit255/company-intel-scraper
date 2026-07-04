#!/usr/bin/env python3
"""
Async web scraper with a worker pool.

A queue of URLs feeds N concurrent workers. Each worker fetches a page,
extracts title/text/links, stores the result in SQLite, and pushes newly
discovered same-domain links back onto the queue.

Usage:
    python scraper.py https://quotes.toscrape.com --workers 8 --max-pages 50
"""

import argparse
import asyncio
import json
import sqlite3
import sys
import time
import urllib.robotparser
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urljoin, urldefrag, urlparse

import aiohttp
from bs4 import BeautifulSoup

USER_AGENT = "Mozilla/5.0 (compatible; MiniScraper/1.0)"


@dataclass
class Config:
    seed_urls: list
    workers: int = 8
    max_pages: int = 100
    max_depth: int = 3
    delay: float = 0.5          # per-domain politeness delay in seconds
    timeout: float = 15.0
    db_path: str = "scraped.db"
    same_domain_only: bool = True
    respect_robots: bool = True


# ---------------------------------------------------------------- storage

class Storage:
    """SQLite storage. A single writer is safe because workers hand results
    to the event loop thread; sqlite3 handles serialization here."""

    def __init__(self, db_path: str):
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS pages (
                url          TEXT PRIMARY KEY,
                status       INTEGER,
                title        TEXT,
                description  TEXT,
                text_content TEXT,
                links_found  INTEGER,
                depth        INTEGER,
                fetched_at   TEXT
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS links (
                from_url TEXT,
                to_url   TEXT,
                UNIQUE(from_url, to_url)
            )
        """)
        self.conn.commit()

    def save_page(self, url, status, title, description, text, links, depth):
        self.conn.execute(
            "INSERT OR REPLACE INTO pages VALUES (?,?,?,?,?,?,?,?)",
            (url, status, title, description, text, len(links), depth,
             datetime.now(timezone.utc).isoformat()),
        )
        self.conn.executemany(
            "INSERT OR IGNORE INTO links VALUES (?,?)",
            [(url, link) for link in links],
        )
        self.conn.commit()

    def page_count(self):
        return self.conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]

    def close(self):
        self.conn.close()


# ---------------------------------------------------------------- politeness

class DomainThrottle:
    """Enforces a minimum delay between requests to the same domain,
    shared across all workers."""

    def __init__(self, delay: float):
        self.delay = delay
        self._locks = {}
        self._last_fetch = {}

    async def wait(self, domain: str):
        lock = self._locks.setdefault(domain, asyncio.Lock())
        async with lock:
            elapsed = time.monotonic() - self._last_fetch.get(domain, 0.0)
            if elapsed < self.delay:
                await asyncio.sleep(self.delay - elapsed)
            self._last_fetch[domain] = time.monotonic()


class RobotsCache:
    """Fetches and caches robots.txt per domain."""

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self._parsers = {}

    async def allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        if base not in self._parsers:
            rp = urllib.robotparser.RobotFileParser()
            try:
                async with self.session.get(f"{base}/robots.txt") as resp:
                    if resp.status == 200:
                        rp.parse((await resp.text()).splitlines())
                    else:
                        rp.allow_all = True
            except Exception:
                rp.allow_all = True
            self._parsers[base] = rp
        return self._parsers[base].can_fetch(USER_AGENT, url)


# ---------------------------------------------------------------- parsing

def parse_page(url: str, html: str):
    soup = BeautifulSoup(html, "html.parser")

    title = soup.title.get_text(strip=True) if soup.title else ""
    desc_tag = soup.find("meta", attrs={"name": "description"})
    description = desc_tag.get("content", "") if desc_tag else ""

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = " ".join(soup.get_text(separator=" ").split())

    links = set()
    for a in soup.find_all("a", href=True):
        link, _ = urldefrag(urljoin(url, a["href"]))
        if urlparse(link).scheme in ("http", "https"):
            links.add(link)

    return title, description, text, sorted(links)


# ---------------------------------------------------------------- crawler

class Crawler:
    def __init__(self, config: Config):
        self.cfg = config
        self.queue = asyncio.Queue()
        self.seen = set()
        self.storage = Storage(config.db_path)
        self.throttle = DomainThrottle(config.delay)
        self.pages_scraped = 0
        self.pages_claimed = 0
        self.errors = 0
        self.seed_domains = {urlparse(u).netloc for u in config.seed_urls}

    def should_follow(self, url: str) -> bool:
        if self.cfg.same_domain_only:
            return urlparse(url).netloc in self.seed_domains
        return True

    async def worker(self, worker_id: int, session, robots):
        while True:
            url, depth = await self.queue.get()
            claimed = stored = False
            try:
                # Reserve a slot before the first await, otherwise concurrent
                # workers overshoot max_pages while fetches are in flight.
                if self.pages_claimed >= self.cfg.max_pages:
                    continue
                self.pages_claimed += 1
                claimed = True

                if self.cfg.respect_robots and not await robots.allowed(url):
                    print(f"[w{worker_id}] blocked by robots.txt: {url}")
                    continue

                await self.throttle.wait(urlparse(url).netloc)

                async with session.get(url) as resp:
                    if "text/html" not in resp.headers.get("Content-Type", ""):
                        continue
                    html = await resp.text()
                    status = resp.status

                title, description, text, links = parse_page(url, html)
                self.storage.save_page(
                    url, status, title, description, text, links, depth)
                self.pages_scraped += 1
                stored = True
                print(f"[w{worker_id}] {status} ({self.pages_scraped}/"
                      f"{self.cfg.max_pages}) depth={depth} {url}")

                if depth < self.cfg.max_depth:
                    for link in links:
                        if link not in self.seen and self.should_follow(link):
                            self.seen.add(link)
                            self.queue.put_nowait((link, depth + 1))

            except Exception as exc:
                self.errors += 1
                print(f"[w{worker_id}] ERROR {url}: {exc}", file=sys.stderr)
            finally:
                if claimed and not stored:
                    self.pages_claimed -= 1
                self.queue.task_done()

    async def run(self):
        for url in self.cfg.seed_urls:
            self.seen.add(url)
            self.queue.put_nowait((url, 0))

        timeout = aiohttp.ClientTimeout(total=self.cfg.timeout)
        headers = {"User-Agent": USER_AGENT}
        async with aiohttp.ClientSession(
                timeout=timeout, headers=headers) as session:
            robots = RobotsCache(session)
            workers = [
                asyncio.create_task(self.worker(i, session, robots))
                for i in range(self.cfg.workers)
            ]
            await self.queue.join()
            for w in workers:
                w.cancel()

        total = self.storage.page_count()
        self.storage.close()
        return total


def export_json(db_path: str, out_path: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT url, status, title, description, links_found, depth,"
        " fetched_at FROM pages")]
    conn.close()
    with open(out_path, "w") as f:
        json.dump(rows, f, indent=2)
    print(f"Exported {len(rows)} pages to {out_path}")


def main():
    ap = argparse.ArgumentParser(description="Worker-pool web scraper")
    ap.add_argument("seeds", nargs="+", help="Seed URL(s) to start from")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--max-pages", type=int, default=100)
    ap.add_argument("--max-depth", type=int, default=3)
    ap.add_argument("--delay", type=float, default=0.5,
                    help="Min seconds between requests to the same domain")
    ap.add_argument("--db", default="scraped.db")
    ap.add_argument("--all-domains", action="store_true",
                    help="Follow links to external domains too")
    ap.add_argument("--no-robots", action="store_true",
                    help="Skip robots.txt checks (not recommended)")
    ap.add_argument("--export-json", metavar="FILE",
                    help="After crawling, export pages table to JSON")
    args = ap.parse_args()

    cfg = Config(
        seed_urls=args.seeds,
        workers=args.workers,
        max_pages=args.max_pages,
        max_depth=args.max_depth,
        delay=args.delay,
        db_path=args.db,
        same_domain_only=not args.all_domains,
        respect_robots=not args.no_robots,
    )

    start = time.monotonic()
    crawler = Crawler(cfg)
    total = asyncio.run(crawler.run())
    elapsed = time.monotonic() - start
    print(f"\nDone: {total} pages stored in {cfg.db_path} "
          f"({crawler.errors} errors) in {elapsed:.1f}s")

    if args.export_json:
        export_json(cfg.db_path, args.export_json)


if __name__ == "__main__":
    main()
