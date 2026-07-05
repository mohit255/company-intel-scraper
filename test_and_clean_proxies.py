#!/usr/bin/env python3
"""
Test and clean proxy list - keep only working proxies
"""

import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from proxy_manager import ProxyManager

def test_proxy(proxy, timeout=5):
    """Test if a proxy is working"""
    try:
        test_proxies = {'http': proxy, 'https': proxy}
        start = time.time()
        response = requests.get(
            'https://httpbin.org/ip',
            proxies=test_proxies,
            timeout=timeout
        )
        if response.status_code == 200:
            elapsed = time.time() - start
            return (proxy, True, elapsed)
    except:
        pass
    return (proxy, False, None)

def main():
    print("=" * 60)
    print("PROXY TESTER AND CLEANER")
    print("=" * 60)
    
    # Load proxies
    try:
        with open('proxies.txt', 'r') as f:
            proxies = [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        print("❌ proxies.txt not found!")
        return
    
    print(f"\n📊 Total proxies: {len(proxies)}")
    print("🔄 Testing proxies (this may take a few minutes)...")
    print("-" * 60)
    
    working = []
    failed = []
    total = len(proxies)
    
    # Test proxies in parallel
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = {executor.submit(test_proxy, p): p for p in proxies}
        
        for i, future in enumerate(as_completed(futures), 1):
            proxy, success, elapsed = future.result()
            if success:
                working.append(proxy)
                print(f"✅ {proxy} ({elapsed:.2f}s)")
            else:
                failed.append(proxy)
                print(f"❌ {proxy}")
            
            # Show progress every 10 proxies
            if i % 10 == 0:
                print(f"   Progress: {i}/{total}")
    
    print("-" * 60)
    print(f"\n📊 Results:")
    print(f"  ✅ Working: {len(working)}/{total}")
    print(f"  ❌ Failed: {len(failed)}/{total}")
    print(f"  📈 Success Rate: {(len(working)/total*100):.1f}%")
    
    # Save working proxies
    if working:
        with open('proxies_working.txt', 'w') as f:
            for proxy in working:
                f.write(f"{proxy}\n")
        print(f"\n💾 Working proxies saved to: proxies_working.txt")
        
        # Replace the main proxy file
        import shutil
        shutil.copy('proxies_working.txt', 'proxies.txt')
        print(f"✅ Replaced proxies.txt with {len(working)} working proxies")
    
    # Save failed proxies for reference
    if failed:
        with open('proxies_failed.txt', 'w') as f:
            for proxy in failed:
                f.write(f"{proxy}\n")
        print(f"💾 Failed proxies saved to: proxies_failed.txt")
    
    print("\n✅ Done!")

if __name__ == "__main__":
    main()
