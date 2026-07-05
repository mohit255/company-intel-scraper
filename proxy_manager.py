#!/usr/bin/env python3
"""
Proxy Manager for Company Intel Scraper
"""

import random
import time
import json
import os
import re
from typing import List, Dict, Optional
from collections import defaultdict
import requests


class ProxyManager:
    """Proxy manager with rotation and failure tracking"""
    
    def __init__(self, 
                 proxy_list: Optional[List[str]] = None,
                 rotation_strategy: str = 'random',
                 max_failures: int = 3,
                 health_check_interval: int = 300,
                 min_working_proxies: int = 3,
                 max_proxies: int = 500,
                 auto_update: bool = False,
                 update_interval_hours: int = 24):
        
        self.proxy_list = proxy_list or []
        self.rotation_strategy = rotation_strategy
        self.max_failures = max_failures
        self.health_check_interval = health_check_interval
        self.min_working_proxies = min_working_proxies
        self.max_proxies = max_proxies
        self.current_index = 0
        self.failed_proxies = defaultdict(int)
        self.proxy_stats = defaultdict(lambda: {'successes': 0, 'failures': 0})
        
        print(f"ProxyManager initialized with {len(self.proxy_list)} proxies")
    
    @staticmethod
    def load_from_file(file_path: str) -> List[str]:
        """Load proxies from a file"""
        proxies = []
        try:
            with open(file_path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#'):
                        if not line.startswith(('http://', 'https://', 'socks4://', 'socks5://')):
                            line = f'http://{line}'
                        proxies.append(line)
            print(f"✅ Loaded {len(proxies)} proxies from {file_path}")
            return proxies
        except FileNotFoundError:
            print(f"❌ Proxy file not found: {file_path}")
            return []
        except Exception as e:
            print(f"❌ Error loading proxies from {file_path}: {e}")
            return []
    
    def get_proxy(self) -> Optional[Dict[str, str]]:
        """Get a proxy for the next request"""
        if not self.proxy_list:
            return None
        
        # Filter out failed proxies
        working = [p for p in self.proxy_list 
                  if self.failed_proxies.get(p, 0) < self.max_failures]
        
        if not working:
            # Reset failures if all proxies failed
            self.failed_proxies.clear()
            working = self.proxy_list
        
        # Select proxy based on strategy
        if self.rotation_strategy == 'random':
            proxy = random.choice(working)
        else:  # round-robin
            self.current_index = (self.current_index + 1) % len(working)
            proxy = working[self.current_index]
        
        return {'http': proxy, 'https': proxy}
    
    def mark_success(self, proxy_url: str):
        """Mark a proxy as successful"""
        if proxy_url:
            self.proxy_stats[proxy_url]['successes'] += 1
            self.failed_proxies[proxy_url] = max(0, self.failed_proxies[proxy_url] - 1)
    
    def mark_failure(self, proxy_url: str):
        """Mark a proxy as failed"""
        if proxy_url:
            self.proxy_stats[proxy_url]['failures'] += 1
            self.failed_proxies[proxy_url] += 1
    
    def get_stats(self) -> Dict:
        """Get proxy statistics"""
        working = [p for p in self.proxy_list 
                  if self.failed_proxies.get(p, 0) < self.max_failures]
        
        return {
            'total_proxies': len(self.proxy_list),
            'working_proxies': len(working),
            'dead_proxies': len(self.proxy_list) - len(working),
            'success_rate': f"{(len(working) / max(1, len(self.proxy_list)) * 100):.1f}%",
            'stats': dict(self.proxy_stats)
        }
    
    def close(self):
        """Clean up"""
        pass


class ProxyFetcher:
    """Fetch proxies from various sources"""
    
    @staticmethod
    def fetch_and_validate(limit: int = 200, timeout: int = 5, max_valid: int = 100) -> List[str]:
        """Fetch and validate proxies"""
        print(f"ProxyFetcher: Fetching up to {limit} proxies...")
        
        proxies = []
        sources = [
            ProxyFetcher._fetch_proxyscrape,
            ProxyFetcher._fetch_proxy_list,
            ProxyFetcher._fetch_github,
        ]
        
        for source in sources:
            try:
                result = source()
                if result:
                    proxies.extend(result)
                    print(f"  ✓ {source.__name__}: {len(result)} proxies")
            except Exception as e:
                print(f"  ✗ {source.__name__}: {e}")
        
        # Remove duplicates
        proxies = list(dict.fromkeys(proxies))
        print(f"Total unique proxies: {len(proxies)}")
        
        if proxies:
            print(f"Validating proxies...")
            working = []
            for proxy in proxies[:min(len(proxies), 100)]:
                if ProxyFetcher._test_proxy(proxy, timeout):
                    working.append(proxy)
                    if len(working) >= max_valid:
                        break
            
            print(f"Found {len(working)} working proxies")
            return working
        
        return []
    
    @staticmethod
    def _test_proxy(proxy: str, timeout: int = 5) -> bool:
        """Test if a proxy works"""
        try:
            test_proxies = {'http': proxy, 'https': proxy}
            response = requests.get(
                'https://httpbin.org/ip',
                proxies=test_proxies,
                timeout=timeout
            )
            return response.status_code == 200
        except:
            return False
    
    @staticmethod
    def _fetch_proxyscrape() -> List[str]:
        """Fetch from proxyscrape"""
        proxies = []
        try:
            url = "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=10000&country=all&ssl=all&anonymity=all&limit=200"
            response = requests.get(url, timeout=10)
            if response.status_code == 200:
                for line in response.text.strip().split('\n'):
                    proxy = line.strip()
                    if proxy and not proxy.startswith('#'):
                        if not proxy.startswith(('http://', 'https://')):
                            proxy = f'http://{proxy}'
                        proxies.append(proxy)
        except:
            pass
        return proxies
    
    @staticmethod
    def _fetch_proxy_list() -> List[str]:
        """Fetch from proxy-list.download"""
        proxies = []
        try:
            url = "https://www.proxy-list.download/api/v1/get?type=http&limit=200"
            response = requests.get(url, timeout=10)
            if response.status_code == 200:
                for line in response.text.strip().split('\n'):
                    proxy = line.strip()
                    if proxy and not proxy.startswith('#'):
                        if not proxy.startswith(('http://', 'https://')):
                            proxy = f'http://{proxy}'
                        proxies.append(proxy)
        except:
            pass
        return proxies
    
    @staticmethod
    def _fetch_github() -> List[str]:
        """Fetch from GitHub proxy lists"""
        proxies = []
        try:
            urls = [
                "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
                "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt",
            ]
            for url in urls:
                try:
                    response = requests.get(url, timeout=10)
                    if response.status_code == 200:
                        for line in response.text.strip().split('\n'):
                            proxy = line.strip()
                            if proxy and not proxy.startswith('#'):
                                if not proxy.startswith(('http://', 'https://')):
                                    proxy = f'http://{proxy}'
                                proxies.append(proxy)
                except:
                    continue
        except:
            pass
        return proxies
    
    @staticmethod
    def save_proxies_to_file(proxies: List[str], file_path: str):
        """Save proxies to file"""
        try:
            with open(file_path, 'w') as f:
                for proxy in proxies:
                    f.write(f"{proxy}\n")
            print(f"✅ Saved {len(proxies)} proxies to {file_path}")
        except Exception as e:
            print(f"❌ Error saving proxies: {e}")

