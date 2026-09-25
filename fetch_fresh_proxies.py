#!/usr/bin/env python3
"""
Fetch fresh working proxies from multiple sources
"""

import time
import requests
from proxy_manager import ProxyFetcher, ProxyManager

def main():
    print("=" * 60)
    print("FETCH FRESH WORKING PROXIES")
    print("=" * 60)
    
    # Fetch proxies from all sources
    print("\n🔄 Fetching proxies from all sources...")
    start = time.time()
    
    # Try multiple sources
    all_proxies = []
    
    # Source 1: ProxyScrape
    try:
        url = "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=10000&country=all&ssl=all&anonymity=all&limit=300"
        response = requests.get(url, timeout=15)
        if response.status_code == 200:
            for line in response.text.strip().split('\n'):
                proxy = line.strip()
                if proxy and not proxy.startswith('#'):
                    if not proxy.startswith(('http://', 'https://')):
                        proxy = f'http://{proxy}'
                    all_proxies.append(proxy)
            print(f"  ✓ ProxyScrape: {len(all_proxies)} proxies")
    except Exception as e:
        print(f"  ✗ ProxyScrape: {e}")
    
    # Source 2: ProxyList
    try:
        url = "https://www.proxy-list.download/api/v1/get?type=http&limit=300"
        response = requests.get(url, timeout=15)
        if response.status_code == 200:
            count = 0
            for line in response.text.strip().split('\n'):
                proxy = line.strip()
                if proxy and not proxy.startswith('#'):
                    if not proxy.startswith(('http://', 'https://')):
                        proxy = f'http://{proxy}'
                    all_proxies.append(proxy)
                    count += 1
            print(f"  ✓ ProxyList: {count} proxies")
    except Exception as e:
        print(f"  ✗ ProxyList: {e}")
    
    # Source 3: GitHub proxy lists
    try:
        urls = [
            "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
            "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt",
        ]
        count = 0
        for url in urls:
            try:
                response = requests.get(url, timeout=15)
                if response.status_code == 200:
                    for line in response.text.strip().split('\n'):
                        proxy = line.strip()
                        if proxy and not proxy.startswith('#'):
                            if not proxy.startswith(('http://', 'https://')):
                                proxy = f'http://{proxy}'
                            all_proxies.append(proxy)
                            count += 1
            except:
                continue
        print(f"  ✓ GitHub: {count} proxies")
    except Exception as e:
        print(f"  ✗ GitHub: {e}")
    
    # Remove duplicates
    all_proxies = list(dict.fromkeys(all_proxies))
    print(f"\n📊 Total unique proxies: {len(all_proxies)}")
    
    # Test proxies
    print("\n🔄 Testing proxies (this may take a few minutes)...")
    working = []
    tested = 0
    max_to_test = min(200, len(all_proxies))
    
    for proxy in all_proxies[:max_to_test]:
        try:
            test_proxies = {'http': proxy, 'https': proxy}
            start_time = time.time()
            response = requests.get(
                'https://httpbin.org/ip',
                proxies=test_proxies,
                timeout=5
            )
            if response.status_code == 200:
                working.append(proxy)
                print(f"  ✅ {proxy}")
            tested += 1
            if len(working) >= 100:  # Stop after finding 100 working
                break
        except:
            print(f"  ❌ {proxy}")
    
    elapsed = time.time() - start
    print("-" * 60)
    print(f"\n📊 Results:")
    print(f"  ✅ Working: {len(working)}/{tested}")
    print(f"  ⏱️  Time: {elapsed:.1f}s")
    
    # Save working proxies
    if working:
        with open('proxies.txt', 'w') as f:
            for proxy in working:
                f.write(f"{proxy}\n")
        print(f"\n💾 Saved {len(working)} working proxies to proxies.txt")
    else:
        print("\n❌ No working proxies found!")
        print("Using existing proxies...")

if __name__ == "__main__":
    import requests
    main()
