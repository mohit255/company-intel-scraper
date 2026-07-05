#!/usr/bin/env python3
"""
Daily proxy updater - Runs automatically to refresh proxy list
"""

import os
import sys
import time
import json
from datetime import datetime
from proxy_manager import ProxyFetcher, ProxyManager

def main():
    print("=" * 60)
    print("DAILY PROXY UPDATER")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)
    
    # Load existing working proxies
    working_proxies = []
    cache_file = "working_proxies_cache.json"
    
    if os.path.exists(cache_file):
        try:
            with open(cache_file, 'r') as f:
                data = json.load(f)
                working_proxies = data.get('proxies', [])
                cache_date = data.get('date', 'unknown')
                print(f"📊 Existing proxies: {len(working_proxies)} (from {cache_date})")
        except Exception as e:
            print(f"Error loading cache: {e}")
    
    # Fetch new proxies
    print("\n🔄 Fetching and validating new proxies...")
    new_proxies = ProxyFetcher.fetch_and_validate(
        limit=300,
        timeout=5,
        max_valid=150
    )
    
    if not new_proxies:
        print("❌ No new proxies found!")
        # Keep existing proxies
        if working_proxies:
            print(f"📊 Keeping {len(working_proxies)} existing proxies")
        return
    
    print(f"\n📊 Found {len(new_proxies)} new working proxies")
    
    # Merge with existing
    combined = list(dict.fromkeys(working_proxies + new_proxies))
    
    # Keep top 500
    if len(combined) > 500:
        combined = combined[:500]
    
    # Save to cache
    with open(cache_file, 'w') as f:
        json.dump({
            'date': datetime.now().isoformat(),
            'proxies': combined
        }, f, indent=2)
    
    # Save to plain text file
    with open('working_proxies.txt', 'w') as f:
        for proxy in combined:
            f.write(f"{proxy}\n")
    
    print(f"\n✅ Updated proxy pool: {len(combined)} working proxies")
    print(f"💾 Saved to: working_proxies.txt and {cache_file}")
    print("=" * 60)

if __name__ == "__main__":
    main()
