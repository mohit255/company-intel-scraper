#!/usr/bin/env python3
"""
Check proxy health and update if needed
"""

import sys
import time
from proxy_manager import ProxyManager

def check_proxy_health():
    """Check proxy pool health and fetch new proxies if needed"""
    print(f"Checking proxy health at {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("-" * 60)
    
    try:
        # Initialize proxy manager
        pm = ProxyManager(proxy_list=[])
        
        # Try to load proxies
        import os
        if os.path.exists('proxies.txt'):
            with open('proxies.txt', 'r') as f:
                proxies = [line.strip() for line in f if line.strip()]
            pm.proxy_list = proxies
            print(f"📊 Loaded {len(proxies)} proxies from file")
        else:
            print("❌ No proxies file found")
            return
        
        # Get stats
        stats = pm.get_stats()
        working = stats.get('working_proxies', 0)
        total = stats.get('total_proxies', 0)
        
        print(f"📊 Proxy Pool Stats:")
        print(f"   Total: {total}")
        print(f"   Working: {working}")
        print(f"   Dead: {stats.get('dead_proxies', 0)}")
        print(f"   Success Rate: {stats.get('success_rate', '0%')}")
        
        # If too few working proxies, fetch new ones
        if working < 20:
            print(f"⚠️ Only {working} working proxies. Fetching fresh ones...")
            import subprocess
            subprocess.run([sys.executable, 'fetch_fresh_proxies.py'])
        
        pm.close()
        
    except Exception as e:
        print(f"❌ Error checking proxy health: {e}")

if __name__ == "__main__":
    check_proxy_health()
