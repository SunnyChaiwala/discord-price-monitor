#!/usr/bin/env python3
"""
Discord Price Monitor - Simplified Public Sheet Version
Monitors Google Shopping for price drops and alerts via Discord webhook
"""

import os
import time
import json
import requests
from datetime import datetime
from bs4 import BeautifulSoup
import re
import csv
from io import StringIO

# Configuration
DISCORD_WEBHOOK_URL = os.getenv('DISCORD_WEBHOOK_URL', '')
GOOGLE_SHEET_URL = os.getenv('GOOGLE_SHEET_URL', '')
CHECK_INTERVAL = int(os.getenv('CHECK_INTERVAL', '1800'))  # 30 minutes default

# Excluded retailers (lowercase for comparison)
EXCLUDED_RETAILERS = ['shein', 'amazon', 'ebay']

class PriceMonitor:
    def __init__(self):
        self.webhook_url = DISCORD_WEBHOOK_URL
        self.sheet_url = GOOGLE_SHEET_URL
        self.price_history = {}  # Store in memory since we can't write to public sheet
        
    def get_csv_export_url(self, sheet_url):
        """Convert Google Sheet URL to CSV export URL"""
        # Extract sheet ID
        match = re.search(r'/spreadsheets/d/([a-zA-Z0-9-_]+)', sheet_url)
        if not match:
            raise ValueError("Invalid Google Sheet URL")
        
        sheet_id = match.group(1)
        
        # Check if specific sheet/tab is specified
        gid_match = re.search(r'[#&]gid=([0-9]+)', sheet_url)
        gid = gid_match.group(1) if gid_match else '0'
        
        return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"
    
    def read_google_sheet(self):
        """Read products from Google Sheet CSV export"""
        try:
            csv_url = self.get_csv_export_url(self.sheet_url)
            response = requests.get(csv_url, timeout=10)
            response.raise_for_status()
            
            # Parse CSV
            csv_data = StringIO(response.text)
            reader = csv.DictReader(csv_data)
            
            products = []
            for row in reader:
                # Skip inactive products
                if row.get('Active', '').upper() not in ['TRUE', 'YES', '1', 'Y']:
                    continue
                
                try:
                    product = {
                        'name': row.get('Product Name', '').strip(),
                        'url': row.get('Product URL', '').strip(),
                        'search_query': row.get('Search Query', '').strip(),
                        'specifications': row.get('Specifications', '').strip(),
                        'price_min': float(row.get('Target Price Min', 0) or 0),
                        'price_max': float(row.get('Target Price Max', 999999) or 999999),
                        'drop_threshold': float(row.get('Drop Alert %', 25) or 25),
                    }
                    
                    # Skip if no name
                    if not product['name']:
                        continue
                    
                    # Load price history from memory
                    product_key = product['name'].lower()
                    if product_key in self.price_history:
                        product['lowest_price'] = self.price_history[product_key]['lowest']
                        product['last_alert_type'] = self.price_history[product_key]['last_alert']
                    else:
                        product['lowest_price'] = 999999
                        product['last_alert_type'] = ''
                    
                    products.append(product)
                    
                except (ValueError, TypeError) as e:
                    print(f"Skipping invalid row: {e}")
                    continue
            
            print(f"✓ Loaded {len(products)} active products from sheet")
            return products
            
        except Exception as e:
            self.send_error_alert(f"Failed to read Google Sheet: {str(e)}")
            print(f"Error reading sheet: {e}")
            return []
    
    def scrape_google_shopping(self, product):
        """Scrape Google Shopping for product prices"""
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept-Language': 'en-GB,en;q=0.9',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            }
            
            # Use URL if provided, otherwise construct search
            if product['url']:
                url = product['url']
            else:
                # Construct Google Shopping search URL
                query = product['search_query'] or product['name']
                if product['specifications']:
                    query += ' ' + product['specifications']
                # Use shopping tab parameter
                url = f"https://www.google.com/search?tbm=shop&q={requests.utils.quote(query)}&hl=en-GB&gl=GB"
            
            print(f"  Fetching: {product['name'][:50]}...")
            response = requests.get(url, headers=headers, timeout=15)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.content, 'html.parser')
            results = []
            
            # Try multiple possible selectors for Google Shopping
            # Google frequently changes their HTML structure
            
            # Method 1: Try standard shopping results
            product_cards = soup.find_all('div', {'class': lambda x: x and 'sh-dgr__content' in x})
            
            # Method 2: Try alternative selectors
            if not product_cards:
                product_cards = soup.find_all('div', {'data-docid': True})
            
            # Method 3: Look for price-containing divs
            if not product_cards:
                product_cards = soup.find_all('div', {'class': lambda x: x and ('product' in x.lower() or 'item' in x.lower())})
            
            for card in product_cards[:15]:  # Check top 15 results
                try:
                    # Try to find price
                    price_elem = None
                    price_patterns = [
                        ('span', {'class': lambda x: x and 'price' in x.lower()}),
                        ('span', {'aria-label': lambda x: x and '£' in str(x)}),
                        ('div', {'class': lambda x: x and 'price' in x.lower()}),
                    ]
                    
                    for tag, attrs in price_patterns:
                        price_elem = card.find(tag, attrs)
                        if price_elem:
                            break
                    
                    # Also try finding any element with £ symbol
                    if not price_elem:
                        price_elem = card.find(string=re.compile(r'£\d'))
                        if price_elem:
                            price_elem = price_elem.parent
                    
                    if not price_elem:
                        continue
                    
                    # Extract price
                    price_text = price_elem.get_text()
                    price_match = re.search(r'£?\s*([\d,]+\.?\d*)', price_text)
                    
                    if not price_match:
                        continue
                    
                    price = float(price_match.group(1).replace(',', ''))
                    
                    # Try to find retailer
                    retailer = 'Unknown'
                    retailer_elem = card.find('div', {'class': lambda x: x and 'merchant' in x.lower()})
                    if not retailer_elem:
                        retailer_elem = card.find('span', {'class': lambda x: x and 'store' in x.lower()})
                    if not retailer_elem:
                        # Try to find any link that might be the retailer
                        link = card.find('a', href=True)
                        if link:
                            href = link['href']
                            domain_match = re.search(r'(?:https?://)?(?:www\.)?([^/]+)', href)
                            if domain_match:
                                retailer = domain_match.group(1)
                    
                    if retailer_elem:
                        retailer = retailer_elem.get_text().strip()
                    
                    retailer_lower = retailer.lower()
                    
                    # Skip excluded retailers
                    if any(excluded in retailer_lower for excluded in EXCLUDED_RETAILERS):
                        continue
                    
                    # Try to find product link
                    link_elem = card.find('a', href=True)
                    product_link = link_elem['href'] if link_elem else url
                    
                    # Clean up Google redirect URLs
                    if 'google.com/url?' in product_link:
                        url_match = re.search(r'[?&]url=([^&]+)', product_link)
                        if url_match:
                            product_link = requests.utils.unquote(url_match.group(1))
                    
                    results.append({
                        'retailer': retailer,
                        'price': price,
                        'link': product_link
                    })
                    
                except Exception as e:
                    continue
            
            if results:
                print(f"  ✓ Found {len(results)} valid results")
            else:
                print(f"  ⚠ No valid results found")
            
            return results
            
        except Exception as e:
            print(f"  ✗ Error scraping: {str(e)}")
            return []
    
    def check_price_alerts(self, product, current_results):
        """Check if price meets alert criteria"""
        if not current_results:
            return None
        
        # Get lowest current price
        lowest_result = min(current_results, key=lambda x: x['price'])
        current_price = lowest_result['price']
        
        alerts = []
        
        # Check if price is in target range
        in_range = product['price_min'] <= current_price <= product['price_max']
        if in_range and product['last_alert_type'] != 'range':
            alerts.append({
                'type': 'range',
                'current_price': current_price,
                'result': lowest_result,
                'message': f"Price in target range: £{current_price:.2f} (£{product['price_min']}-£{product['price_max']})"
            })
        
        # Check for percentage drop from lowest price ever
        if product['lowest_price'] < 999999:
            drop_percentage = ((product['lowest_price'] - current_price) / product['lowest_price']) * 100
            
            if drop_percentage >= product['drop_threshold'] and product['last_alert_type'] != 'drop':
                alerts.append({
                    'type': 'drop',
                    'current_price': current_price,
                    'result': lowest_result,
                    'previous_lowest': product['lowest_price'],
                    'drop_percentage': drop_percentage,
                    'message': f"Price dropped {drop_percentage:.1f}%: £{current_price:.2f} (was £{product['lowest_price']:.2f})"
                })
        
        # Update price history in memory
        product_key = product['name'].lower()
        if product_key not in self.price_history:
            self.price_history[product_key] = {
                'lowest': current_price,
                'last_alert': ''
            }
        else:
            if current_price < self.price_history[product_key]['lowest']:
                self.price_history[product_key]['lowest'] = current_price
        
        # Update last alert type if alert is being sent
        if alerts:
            self.price_history[product_key]['last_alert'] = alerts[0]['type']
        
        return alerts if alerts else None
    
    def send_discord_alert(self, product, alerts):
        """Send price alert to Discord"""
        try:
            for alert in alerts:
                embed = {
                    "title": f"🔔 Price Alert: {product['name']}",
                    "color": 0x00ff00 if alert['type'] == 'range' else 0xff9900,
                    "fields": [
                        {
                            "name": "💰 Current Price",
                            "value": f"**£{alert['current_price']:.2f}**",
                            "inline": True
                        },
                        {
                            "name": "🏪 Retailer",
                            "value": alert['result']['retailer'].title(),
                            "inline": True
                        }
                    ],
                    "timestamp": datetime.utcnow().isoformat(),
                    "footer": {
                        "text": "Price Monitor • " + datetime.now().strftime('%H:%M GMT')
                    }
                }
                
                if alert['type'] == 'drop':
                    embed['description'] = f"Price dropped by **{alert['drop_percentage']:.1f}%**!"
                    embed['fields'].insert(1, {
                        "name": "📉 Previous Lowest",
                        "value": f"£{alert['previous_lowest']:.2f}",
                        "inline": True
                    })
                elif alert['type'] == 'range':
                    embed['description'] = "Price is now in your target range!"
                    embed['fields'].append({
                        "name": "🎯 Target Range",
                        "value": f"£{product['price_min']}-£{product['price_max']}",
                        "inline": True
                    })
                
                # Add product link as button/URL
                if alert['result']['link']:
                    embed['url'] = alert['result']['link']
                    embed['fields'].append({
                        "name": "🔗 View Product",
                        "value": f"[Click here]({alert['result']['link']})",
                        "inline": False
                    })
                
                payload = {
                    "embeds": [embed],
                    "username": "Price Monitor"
                }
                
                response = requests.post(self.webhook_url, json=payload, timeout=10)
                response.raise_for_status()
                print(f"  ✓ Alert sent: {alert['message']}")
                
                time.sleep(1)  # Rate limit Discord webhooks
                
        except Exception as e:
            print(f"  ✗ Error sending Discord alert: {str(e)}")
    
    def send_error_alert(self, error_message):
        """Send error notification to Discord"""
        try:
            embed = {
                "title": "⚠️ Price Monitor Error",
                "description": error_message,
                "color": 0xff0000,
                "timestamp": datetime.utcnow().isoformat(),
                "footer": {
                    "text": "Price Monitor Error"
                }
            }
            
            payload = {
                "embeds": [embed],
                "username": "Price Monitor"
            }
            
            requests.post(self.webhook_url, json=payload, timeout=10)
        except:
            pass
    
    def run_check(self):
        """Run a single check cycle"""
        print(f"\n{'='*60}")
        print(f"🔍 Running price check at {datetime.now().strftime('%Y-%m-%d %H:%M:%S GMT')}")
        print(f"{'='*60}")
        
        products = self.read_google_sheet()
        
        if not products:
            print("⚠ No active products to monitor")
            return
        
        print(f"Checking {len(products)} products...\n")
        
        for i, product in enumerate(products, 1):
            try:
                print(f"[{i}/{len(products)}] {product['name']}")
                
                # Scrape current prices
                results = self.scrape_google_shopping(product)
                
                if results:
                    # Check for alerts
                    alerts = self.check_price_alerts(product, results)
                    
                    # Get lowest current price for logging
                    lowest_result = min(results, key=lambda x: x['price'])
                    print(f"  💷 Lowest: £{lowest_result['price']:.2f} at {lowest_result['retailer']}")
                    
                    # Send alerts if criteria met
                    if alerts:
                        self.send_discord_alert(product, alerts)
                    else:
                        print(f"  ℹ️  No alerts triggered")
                else:
                    print(f"  ⚠ No results found - check your search query")
                
                print()  # Blank line for readability
                
                # Be respectful to Google - wait between requests
                time.sleep(5)
                
            except Exception as e:
                print(f"  ✗ Error: {str(e)}\n")
                continue
        
        print(f"✅ Check complete at {datetime.now().strftime('%H:%M:%S GMT')}")
        print(f"Next check in {CHECK_INTERVAL/60:.0f} minutes...")
    
    def run(self):
        """Main monitoring loop"""
        print("=" * 60)
        print("🚀 DISCORD PRICE MONITOR STARTING")
        print("=" * 60)
        print(f"Check interval: {CHECK_INTERVAL/60:.0f} minutes")
        print(f"Discord webhook: {'✓ Configured' if self.webhook_url else '✗ Not configured'}")
        print(f"Google Sheet: {'✓ Configured' if self.sheet_url else '✗ Not configured'}")
        print("=" * 60)
        
        if not self.webhook_url:
            print("\n⚠️ ERROR: DISCORD_WEBHOOK_URL not set!")
            return
        
        if not self.sheet_url:
            print("\n⚠️ ERROR: GOOGLE_SHEET_URL not set!")
            return
        
        while True:
            try:
                self.run_check()
                time.sleep(CHECK_INTERVAL)
            except KeyboardInterrupt:
                print("\n\n🛑 Stopping monitor...")
                break
            except Exception as e:
                error_msg = f"Unexpected error: {str(e)}"
                print(f"\n⚠️ {error_msg}")
                self.send_error_alert(error_msg)
                print("Waiting 60 seconds before retry...")
                time.sleep(60)

if __name__ == "__main__":
    monitor = PriceMonitor()
    monitor.run()
