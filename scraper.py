#!/usr/bin/env python3
"""
Company Intelligence Scraper with Proxy Support
Scrapes news, jobs, and products for companies and stores in PostgreSQL
"""

import argparse
import asyncio
import json
import sys
import time
import re
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Set, Tuple
from urllib.parse import urljoin, urldefrag, urlparse

import aiohttp
from bs4 import BeautifulSoup

# Try to import dateutil, fallback to simple parsing
try:
    from dateutil import parser as date_parser
    HAS_DATEUTIL = True
except ImportError:
    HAS_DATEUTIL = False
    print("Warning: python-dateutil not installed. Using simple date parsing.", file=sys.stderr)

# Import database
try:
    from db import connect, init_db, DB_URL
    HAS_POSTGRES = True
except ImportError:
    print("Error: db.py not found!", file=sys.stderr)
    sys.exit(1)

# Import proxy manager
try:
    from proxy_manager import ProxyManager
    HAS_PROXY_MANAGER = True
except ImportError:
    HAS_PROXY_MANAGER = False
    print("Warning: proxy_manager.py not found. Using fallback.", file=sys.stderr)
    
    class ProxyManager:
        def __init__(self, proxy_list=None, rotation_strategy='random', max_failures=3, **kwargs):
            self.proxy_list = proxy_list or []
            self.rotation_strategy = rotation_strategy
            self.current_index = 0
            self.failed_proxies = {}
        
        def get_proxy(self):
            if not self.proxy_list:
                return None
            working = [p for p in self.proxy_list if self.failed_proxies.get(p, 0) < 3]
            if not working:
                self.failed_proxies.clear()
                working = self.proxy_list
            if self.rotation_strategy == 'random':
                import random
                return random.choice(working)
            else:
                self.current_index = (self.current_index + 1) % len(working)
                return working[self.current_index]
        
        def mark_success(self, proxy_url):
            if proxy_url:
                self.failed_proxies[proxy_url] = max(0, self.failed_proxies.get(proxy_url, 0) - 1)
        
        def mark_failure(self, proxy_url):
            if proxy_url:
                self.failed_proxies[proxy_url] = self.failed_proxies.get(proxy_url, 0) + 1
        
        def get_stats(self):
            working = [p for p in self.proxy_list if self.failed_proxies.get(p, 0) < 3]
            return {
                'total_proxies': len(self.proxy_list),
                'working_proxies': len(working),
                'dead_proxies': len(self.proxy_list) - len(working)
            }
        
        def close(self):
            pass
        
        @staticmethod
        def load_from_file(file_path):
            proxies = []
            try:
                with open(file_path, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#'):
                            if not line.startswith(('http://', 'https://')):
                                line = f'http://{line}'
                            proxies.append(line)
                print(f"Loaded {len(proxies)} proxies from {file_path}")
            except Exception as e:
                print(f"Error loading proxies: {e}")
            return proxies

USER_AGENT = "Mozilla/5.0 (compatible; CompanyIntelScraper/1.0)"


# Simple date parser fallback
def parse_date_simple(date_string):
    if not date_string:
        return None
    formats = [
        '%a, %d %b %Y %H:%M:%S %Z',
        '%a, %d %b %Y %H:%M:%S %z',
        '%Y-%m-%dT%H:%M:%S',
        '%Y-%m-%dT%H:%M:%S.%f',
        '%Y-%m-%d',
        '%d %b %Y',
        '%B %d, %Y',
    ]
    for fmt in formats:
        try:
            return datetime.strptime(date_string, fmt)
        except ValueError:
            continue
    return None


@dataclass
class Config:
    companies_file: str = "companies.json"
    workers: int = 8
    max_news_per_company: int = 15
    max_jobs_per_company: int = 15
    max_products_per_company: int = 10
    delay: float = 1.0
    timeout: float = 30.0
    respect_robots: bool = True
    proxies: Optional[List[str]] = None
    proxy_rotation: str = 'random'
    proxy_health_check: bool = True
    max_proxy_failures: int = 3


def load_companies(file_path: str) -> List[Dict]:
    try:
        with open(file_path, 'r') as f:
            companies = json.load(f)
        print(f"Loaded {len(companies)} companies from {file_path}")
        return companies
    except FileNotFoundError:
        print(f"Error: {file_path} not found!", file=sys.stderr)
        return []
    except json.JSONDecodeError as e:
        print(f"Error parsing {file_path}: {e}", file=sys.stderr)
        return []


class CompanyStorage:
    def __init__(self):
        self.conn = connect()
        init_db(self.conn)
        print(f"Connected to PostgreSQL: {DB_URL}")
    
    def save_news(self, company: str, field: str, title: str, link: str, 
                  source: str = None, published: str = None, topic: str = 'general'):
        try:
            pub_date = None
            if published:
                if HAS_DATEUTIL:
                    try:
                        pub_date = date_parser.parse(published)
                    except:
                        pub_date = None
                else:
                    pub_date = parse_date_simple(published)
            
            with self.conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO news (company, field, title, link, source, published, topic)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (link) DO UPDATE SET
                        title = EXCLUDED.title,
                        published = EXCLUDED.published,
                        fetched_at = NOW()
                    RETURNING id
                """, (company, field, title, link, source, pub_date, topic))
                self.conn.commit()
                return cur.fetchone()['id']
        except Exception as e:
            print(f"Error saving news: {e}", file=sys.stderr)
            self.conn.rollback()
            return None
    
    def save_job(self, company: str, field: str, title: str, location: str, 
                 url: str, posted_at: str = None):
        try:
            with self.conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO jobs (company, field, title, location, url, posted_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (url) DO UPDATE SET
                        title = EXCLUDED.title,
                        location = EXCLUDED.location,
                        posted_at = EXCLUDED.posted_at,
                        fetched_at = NOW()
                    RETURNING id
                """, (company, field, title, location, url, posted_at))
                self.conn.commit()
                return cur.fetchone()['id']
        except Exception as e:
            print(f"Error saving job: {e}", file=sys.stderr)
            self.conn.rollback()
            return None
    
    def save_product(self, company: str, field: str, url: str, 
                     page_title: str = None, prices: List = None, 
                     text_snippet: str = None, image: str = None):
        try:
            prices = prices or []
            with self.conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO products (company, field, url, page_title, prices, text_snippet, image)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (url) DO UPDATE SET
                        page_title = EXCLUDED.page_title,
                        prices = EXCLUDED.prices,
                        text_snippet = EXCLUDED.text_snippet,
                        image = EXCLUDED.image,
                        fetched_at = NOW()
                    RETURNING id
                """, (company, field, url, page_title, json.dumps(prices), text_snippet, image))
                self.conn.commit()
                return cur.fetchone()['id']
        except Exception as e:
            print(f"Error saving product: {e}", file=sys.stderr)
            self.conn.rollback()
            return None
    
    def get_stats(self):
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM news")
            news_count = cur.fetchone()['count']
            cur.execute("SELECT COUNT(*) FROM jobs")
            jobs_count = cur.fetchone()['count']
            cur.execute("SELECT COUNT(*) FROM products")
            products_count = cur.fetchone()['count']
            return {
                'news': news_count,
                'jobs': jobs_count,
                'products': products_count
            }
    
    def close(self):
        self.conn.close()


class CompanyScraper:
    def __init__(self, config: Config):
        self.config = config
        self.storage = CompanyStorage()
        self.proxy_manager = None
        self.proxy_url = None
        
        if config.proxies:
            self.proxy_manager = ProxyManager(
                proxy_list=config.proxies,
                rotation_strategy=config.proxy_rotation,
                max_failures=config.max_proxy_failures
            )
            print(f"Proxy manager initialized with {len(config.proxies)} proxies")
        
        self.session = None
        self.total_news = 0
        self.total_jobs = 0
        self.total_products = 0
        self.errors = 0
    
    def _get_proxy(self):
        """Get proxy URL string - handles both string and dict returns"""
        if not self.proxy_manager:
            return None
        
        proxy = self.proxy_manager.get_proxy()
        
        if proxy is None:
            return None
        
        if isinstance(proxy, dict):
            return proxy.get('http') or proxy.get('https')
        
        if isinstance(proxy, str):
            return proxy
        
        return str(proxy)
    
    def _mark_proxy_success(self):
        if self.proxy_manager and self.proxy_url:
            if isinstance(self.proxy_url, dict):
                proxy_str = self.proxy_url.get('http') or self.proxy_url.get('https')
            else:
                proxy_str = self.proxy_url
            if proxy_str:
                self.proxy_manager.mark_success(proxy_str)
    
    def _mark_proxy_failure(self):
        if self.proxy_manager and self.proxy_url:
            if isinstance(self.proxy_url, dict):
                proxy_str = self.proxy_url.get('http') or self.proxy_url.get('https')
            else:
                proxy_str = self.proxy_url
            if proxy_str:
                self.proxy_manager.mark_failure(proxy_str)
    
    async def fetch_url(self, url: str, headers: Dict = None) -> Optional[str]:
        self.proxy_url = self._get_proxy()
        
        proxy_str = None
        if self.proxy_url:
            if isinstance(self.proxy_url, dict):
                proxy_str = self.proxy_url.get('http') or self.proxy_url.get('https')
            elif isinstance(self.proxy_url, str):
                proxy_str = self.proxy_url
        
        try:
            await asyncio.sleep(self.config.delay)
            
            async with self.session.get(url, proxy=proxy_str, timeout=self.config.timeout) as resp:
                if resp.status == 200:
                    self._mark_proxy_success()
                    return await resp.text()
                else:
                    self._mark_proxy_failure()
                    print(f"  HTTP {resp.status} for {url}")
                    return None
        except Exception as e:
            self._mark_proxy_failure()
            print(f"  Error fetching {url}: {e}")
            return None
    
    async def scrape_news(self, company: Dict) -> List[Dict]:
        news_items = []
        company_name = company['name']
        field = company.get('field', 'general')
        news_query = company.get('news_query', company_name)
        
        search_url = f"https://news.google.com/rss/search?q={news_query.replace(' ', '+')}&hl=en-US&gl=US&ceid=US:en"
        
        html = await self.fetch_url(search_url)
        if not html:
            return news_items
        
        try:
            soup = BeautifulSoup(html, 'xml')
            items = soup.find_all('item')[:self.config.max_news_per_company]
            
            for item in items:
                title = item.title.text if item.title else ''
                link = item.link.text if item.link else ''
                pub_date = item.pubDate.text if item.pubDate else ''
                source = item.source.text if item.source else ''
                
                if title and link:
                    self.storage.save_news(
                        company=company_name,
                        field=field,
                        title=title,
                        link=link,
                        source=source,
                        published=pub_date,
                        topic='general'
                    )
                    news_items.append({'title': title, 'link': link})
        except Exception as e:
            print(f"  Error parsing news RSS: {e}")
        
        print(f"  Scraped {len(news_items)} news articles")
        return news_items
    
    async def scrape_jobs(self, company: Dict) -> List[Dict]:
        jobs = []
        company_name = company['name']
        field = company.get('field', 'general')
        
        ats = company.get('ats', {})
        
        if ats.get('type') == 'greenhouse':
            jobs = await self._scrape_greenhouse_jobs(company, ats)
        elif ats.get('type') == 'lever':
            jobs = await self._scrape_lever_jobs(company, ats)
        elif ats.get('type') == 'ashby':
            jobs = await self._scrape_ashby_jobs(company, ats)
        elif ats.get('type') == 'workday':
            jobs = await self._scrape_workday_jobs(company, ats)
        
        for job in jobs:
            self.storage.save_job(
                company=company_name,
                field=field,
                title=job.get('title', ''),
                location=job.get('location', ''),
                url=job.get('url', ''),
                posted_at=job.get('posted_at')
            )
        
        print(f"  Scraped {len(jobs)} jobs")
        return jobs
    
    async def _scrape_greenhouse_jobs(self, company: Dict, ats: Dict) -> List[Dict]:
        board = ats.get('board')
        if not board:
            return []
        
        url = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs"
        html = await self.fetch_url(url)
        if not html:
            return []
        
        try:
            data = json.loads(html)
            jobs = []
            for job in data.get('jobs', [])[:self.config.max_jobs_per_company]:
                jobs.append({
                    'title': job.get('title', ''),
                    'location': job.get('location', {}).get('name', ''),
                    'url': job.get('absolute_url', ''),
                    'posted_at': job.get('updated_at', '').split('T')[0]
                })
            return jobs
        except Exception as e:
            print(f"  Error parsing Greenhouse jobs: {e}")
            return []
    
    async def _scrape_lever_jobs(self, company: Dict, ats: Dict) -> List[Dict]:
        org = ats.get('org')
        if not org:
            return []
        
        url = f"https://api.lever.co/v0/postings/{org}"
        html = await self.fetch_url(url)
        if not html:
            return []
        
        try:
            data = json.loads(html)
            jobs = []
            for job in data[:self.config.max_jobs_per_company]:
                jobs.append({
                    'title': job.get('text', ''),
                    'location': job.get('categories', {}).get('location', ''),
                    'url': job.get('hostedUrl', ''),
                    'posted_at': job.get('createdAt', '').split('T')[0]
                })
            return jobs
        except Exception as e:
            print(f"  Error parsing Lever jobs: {e}")
            return []
    
    async def _scrape_ashby_jobs(self, company: Dict, ats: Dict) -> List[Dict]:
        board = ats.get('board')
        if not board:
            return []
        
        url = f"https://api.ashbyhq.com/posting-api/{board}/list"
        html = await self.fetch_url(url)
        if not html:
            return []
        
        try:
            data = json.loads(html)
            jobs = []
            for job in data.get('jobs', [])[:self.config.max_jobs_per_company]:
                jobs.append({
                    'title': job.get('title', ''),
                    'location': job.get('location', {}).get('name', ''),
                    'url': job.get('jobUrl', ''),
                    'posted_at': job.get('publishedAt', '').split('T')[0]
                })
            return jobs
        except Exception as e:
            print(f"  Error parsing Ashby jobs: {e}")
            return []
    
    async def _scrape_workday_jobs(self, company: Dict, ats: Dict) -> List[Dict]:
        return []
    
    async def scrape_products(self, company: Dict) -> List[Dict]:
        products = []
        company_name = company['name']
        field = company.get('field', 'general')
        products_url = company.get('products_url')
        
        if not products_url:
            return products
        
        html = await self.fetch_url(products_url)
        if not html:
            return products
        
        try:
            soup = BeautifulSoup(html, 'html.parser')
            
            product_elements = soup.find_all(['div', 'li', 'article'], 
                                             class_=re.compile(r'product|item|card'))[:self.config.max_products_per_company]
            
            for elem in product_elements:
                title_elem = elem.find(['h2', 'h3', 'h4', 'span', 'div'], 
                                       class_=re.compile(r'title|name|product'))
                price_elem = elem.find(['span', 'div'], class_=re.compile(r'price|cost|amount'))
                link_elem = elem.find('a', href=True)
                img_elem = elem.find('img', src=True)
                
                title = title_elem.text.strip() if title_elem else ''
                price = price_elem.text.strip() if price_elem else ''
                link = urljoin(products_url, link_elem['href']) if link_elem else products_url
                image = img_elem['src'] if img_elem else ''
                
                if title:
                    self.storage.save_product(
                        company=company_name,
                        field=field,
                        url=link,
                        page_title=title,
                        prices=[price] if price else [],
                        text_snippet=title,
                        image=image
                    )
                    products.append({
                        'title': title,
                        'price': price,
                        'url': link
                    })
        except Exception as e:
            print(f"  Error parsing products: {e}")
        
        print(f"  Scraped {len(products)} products")
        return products
    
    async def scrape_company(self, company: Dict) -> Dict:
        company_name = company['name']
        print(f"\nScraping {company_name}...")
        
        try:
            news = await self.scrape_news(company)
            jobs = await self.scrape_jobs(company)
            products = await self.scrape_products(company)
            
            self.total_news += len(news)
            self.total_jobs += len(jobs)
            self.total_products += len(products)
            
            return {
                'company': company_name,
                'news': len(news),
                'jobs': len(jobs),
                'products': len(products)
            }
        except Exception as e:
            print(f"  Error scraping {company_name}: {e}")
            self.errors += 1
            return {
                'company': company_name,
                'error': str(e)
            }
    
    async def run(self):
        companies = load_companies(self.config.companies_file)
        if not companies:
            print("No companies to scrape")
            return
        
        print(f"\n{'='*60}")
        print(f"Starting scraper with {len(companies)} companies")
        print(f"Workers: {self.config.workers}")
        print(f"Max news per company: {self.config.max_news_per_company}")
        print(f"Max jobs per company: {self.config.max_jobs_per_company}")
        print(f"Max products per company: {self.config.max_products_per_company}")
        print(f"{'='*60}\n")
        
        start_time = time.time()
        
        headers = {'User-Agent': USER_AGENT}
        timeout = aiohttp.ClientTimeout(total=self.config.timeout)
        
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            self.session = session
            
            semaphore = asyncio.Semaphore(self.config.workers)
            
            async def process_company(company):
                async with semaphore:
                    return await self.scrape_company(company)
            
            tasks = [process_company(company) for company in companies]
            results = await asyncio.gather(*tasks, return_exceptions=True)
        
        elapsed = time.time() - start_time
        
        print(f"\n{'='*60}")
        print("SCRAPING COMPLETE")
        print(f"{'='*60}")
        print(f"Companies processed: {len(companies)}")
        print(f"Total news scraped: {self.total_news}")
        print(f"Total jobs scraped: {self.total_jobs}")
        print(f"Total products scraped: {self.total_products}")
        print(f"Errors: {self.errors}")
        print(f"Time elapsed: {elapsed:.1f}s")
        
        stats = self.storage.get_stats()
        print(f"\nDatabase totals:")
        print(f"  News: {stats['news']}")
        print(f"  Jobs: {stats['jobs']}")
        print(f"  Products: {stats['products']}")
        
        if self.proxy_manager:
            proxy_stats = self.proxy_manager.get_stats()
            print(f"\nProxy Statistics:")
            print(f"  Total proxies: {proxy_stats['total_proxies']}")
            print(f"  Working: {proxy_stats['working_proxies']}")
            print(f"  Dead: {proxy_stats['dead_proxies']}")
        
        print(f"{'='*60}\n")
        
        self.storage.close()
        if self.proxy_manager:
            self.proxy_manager.close()


def main():
    parser = argparse.ArgumentParser(description="Company Intelligence Scraper with Proxy Support")
    
    parser.add_argument("--companies", "-c", default="companies.json",
                        help="JSON file with company data")
    parser.add_argument("--workers", "-w", type=int, default=8,
                        help="Number of concurrent workers")
    parser.add_argument("--news-limit", type=int, default=15,
                        help="Max news articles per company")
    parser.add_argument("--jobs-limit", type=int, default=15,
                        help="Max jobs per company")
    parser.add_argument("--products-limit", type=int, default=10,
                        help="Max products per company")
    parser.add_argument("--delay", type=float, default=1.0,
                        help="Delay between requests (seconds)")
    parser.add_argument("--timeout", type=float, default=30.0,
                        help="Request timeout (seconds)")
    
    parser.add_argument("--proxies", "-p", help="File containing proxy list (one per line)")
    parser.add_argument("--proxy-rotation", "-r", choices=['random', 'round-robin'],
                        default='random', help="Proxy rotation strategy")
    parser.add_argument("--max-proxy-failures", type=int, default=3,
                        help="Max failures before proxy is removed")
    parser.add_argument("--test-proxy", help="Test a specific proxy URL and exit")
    
    args = parser.parse_args()
    
    if args.test_proxy:
        try:
            import requests
            proxies = {'http': args.test_proxy, 'https': args.test_proxy}
            response = requests.get('https://httpbin.org/ip', proxies=proxies, timeout=10)
            if response.status_code == 200:
                print(f"✅ Proxy {args.test_proxy} is working!")
                print(f"   Response: {response.json()}")
            else:
                print(f"❌ Proxy {args.test_proxy} returned status {response.status_code}")
        except Exception as e:
            print(f"❌ Proxy {args.test_proxy} is NOT working!")
            print(f"   Error: {e}")
        return
    
    proxies = []
    if args.proxies:
        proxies = ProxyManager.load_from_file(args.proxies)
    
    if proxies:
        print(f"Using {len(proxies)} proxies with {args.proxy_rotation} rotation")
    
    config = Config(
        companies_file=args.companies,
        workers=args.workers,
        max_news_per_company=args.news_limit,
        max_jobs_per_company=args.jobs_limit,
        max_products_per_company=args.products_limit,
        delay=args.delay,
        timeout=args.timeout,
        proxies=proxies if proxies else None,
        proxy_rotation=args.proxy_rotation,
        max_proxy_failures=args.max_proxy_failures
    )
    
    scraper = CompanyScraper(config)
    asyncio.run(scraper.run())


if __name__ == "__main__":
    main()
