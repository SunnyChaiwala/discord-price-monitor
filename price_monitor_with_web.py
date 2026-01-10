#!/usr/bin/env python3
"""
Discord Price Monitor with Enhanced Scraping
Improved Google Shopping scraper with detailed debugging
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
from threading import Thread
from flask import Flask, render_template_string, jsonify

# Configuration
DISCORD_WEBHOOK_URL = os.getenv('DISCORD_WEBHOOK_URL', '')
GOOGLE_SHEET_URL = os.getenv('GOOGLE_SHEET_URL', '')
CHECK_INTERVAL = int(os.getenv('CHECK_INTERVAL', '1800'))
PORT = int(os.getenv('PORT', '10000'))

# Excluded retailers
EXCLUDED_RETAILERS = ['shein', 'amazon', 'ebay']

# Global status
monitor_status = {
    'running': False,
    'last_check': None,
    'next_check': None,
    'total_checks': 0,
    'products_monitored': 0,
    'alerts_sent': 0,
    'last_error': None,
    'startup_time': datetime.now().isoformat(),
    'last_results': []  # For debugging
}

class PriceMonitor:
    def __init__(self):
        self.webhook_url = DISCORD_WEBHOOK_URL
        self.sheet_url = GOOGLE_SHEET_URL
        self.price_history = {}
        
    def get_csv_export_url(self, sheet_url):
        """Convert Google Sheet URL to CSV export URL"""
        match = re.search(r'/spreadsheets/d/([a-zA-Z0-9-_]+)', sheet_url)
        if not match:
            raise ValueError("Invalid Google Sheet URL")
        
        sheet_id = match.group(1)
        gid_match = re.search(r'[#&]gid=([0-9]+)', sheet_url)
        gid = gid_match.group(1) if gid_match else '0'
        
        return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"
    
    def read_google_sheet(self):
        """Read products from Google Sheet CSV export"""
        try:
            csv_url = self.get_csv_export_url(self.sheet_url)
            response = requests.get(csv_url, timeout=10)
            response.raise_for_status()
            
            csv_data = StringIO(response.text)
            reader = csv.DictReader(csv_data)
            
            products = []
            for row in reader:
                # More flexible active check
                active_value = str(row.get('Active', '')).strip().upper()
                if active_value not in ['TRUE', 'YES', '1', 'Y']:
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
                    
                    if not product['name']:
                        continue
                    
                    product_key = product['name'].lower()
                    if product_key in self.price_history:
                        product['lowest_price'] = self.price_history[product_key]['lowest']
                        product['last_alert_type'] = self.price_history[product_key]['last_alert']
                    else:
                        product['lowest_price'] = 999999
                        product['last_alert_type'] = ''
                    
                    products.append(product)
                    
                except (ValueError, TypeError) as e:
                    print(f"⚠️ Skipping invalid row: {e}")
                    continue
            
            print(f"✓ Loaded {len(products)} active products from sheet")
            monitor_status['products_monitored'] = len(products)
            return products
            
        except Exception as e:
            error_msg = f"Failed to read Google Sheet: {str(e)}"
            self.send_error_alert(error_msg)
            monitor_status['last_error'] = error_msg
            print(f"❌ Error reading sheet: {e}")
            return []
    
    def scrape_google_shopping_enhanced(self, product):
        """Enhanced Google Shopping scraper with multiple strategies"""
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                'Accept-Language': 'en-GB,en-US;q=0.9,en;q=0.8',
                'Accept-Encoding': 'gzip, deflate, br',
                'DNT': '1',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1'
            }
            
            # Build URL
            if product['url']:
                url = product['url']
                print(f"  🔗 Using provided URL")
            else:
                query = product['search_query'] or product['name']
                if product['specifications']:
                    query += ' ' + product['specifications']
                # Use the shopping parameter that works better
                url = f"https://www.google.com/search?q={requests.utils.quote(query)}&tbm=shop&hl=en-GB&gl=GB"
                print(f"  🔍 Searching: {query}")
            
            print(f"  📡 Fetching URL...")
            response = requests.get(url, headers=headers, timeout=15)
            response.raise_for_status()
            
            # Save HTML for debugging if needed
            html_length = len(response.content)
            print(f"  ✓ Received {html_length:,} bytes of HTML")
            
            soup = BeautifulSoup(response.content, 'html.parser')
            results = []
            
            # Strategy 1: Look for any element with a price pattern
            print(f"  🔍 Strategy 1: Searching for price patterns...")
            price_elements = soup.find_all(string=re.compile(r'£\s*\d+'))
            print(f"  Found {len(price_elements)} price elements")
            
            # Strategy 2: Find shopping result containers
            print(f"  🔍 Strategy 2: Looking for product containers...")
            
            # Try multiple container patterns
            containers = []
            container_patterns = [
                {'class': re.compile(r'.*sh-dgr.*')},
                {'class': re.compile(r'.*product.*')},
                {'class': re.compile(r'.*result.*')},
                {'data-docid': True},
                {'class': re.compile(r'.*item.*')},
            ]
            
            for pattern in container_patterns:
                found = soup.find_all('div', pattern)
                if found:
                    containers.extend(found)
                    print(f"  Found {len(found)} containers with pattern {pattern}")
            
            # Remove duplicates
            containers = list(set(containers))
            print(f"  📦 Total unique containers: {len(containers)}")
            
            # Strategy 3: Parse the containers
            for i, container in enumerate(containers[:20]):  # Check top 20
                try:
                    # Look for price in this container
                    price_text = None
                    price_elem = None
                    
                    # Try multiple price finding methods
                    price_patterns = [
                        container.find(string=re.compile(r'£\d+')),
                        container.find('span', string=re.compile(r'£\d+')),
                        container.find('div', string=re.compile(r'£\d+')),
                        container.find(attrs={'aria-label': re.compile(r'£\d+')}),
                    ]
                    
                    for pattern in price_patterns:
                        if pattern:
                            if hasattr(pattern, 'get_text'):
                                price_text = pattern.get_text()
                            else:
                                price_text = str(pattern)
                            break
                    
                    if not price_text:
                        continue
                    
                    # Extract numeric price
                    price_match = re.search(r'£?\s*([\d,]+\.?\d*)', price_text)
                    if not price_match:
                        continue
                    
                    price = float(price_match.group(1).replace(',', ''))
                    
                    # Find retailer name
                    retailer = 'Unknown'
                    retailer_text = container.get_text()
                    
                    # Try to extract domain from any links
                    links = container.find_all('a', href=True)
                    for link in links:
                        href = link.get('href', '')
                        domain_match = re.search(r'(?:https?://)?(?:www\.)?([^/]+\.[^/]+)', href)
                        if domain_match:
                            domain = domain_match.group(1)
                            # Clean up domain
                            retailer = domain.split('.')[0].replace('-', ' ').title()
                            break
                    
                    retailer_lower = retailer.lower()
                    
                    # Skip excluded retailers
                    if any(excluded in retailer_lower for excluded in EXCLUDED_RETAILERS):
                        print(f"  ⏭️  Skipping {retailer} (excluded)")
                        continue
                    
                    # Get product link
                    product_link = url
                    if links:
                        product_link = links[0].get('href', url)
                        # Clean Google redirect
                        if 'google.com/url?' in product_link:
                            url_match = re.search(r'[?&]url=([^&]+)', product_link)
                            if url_match:
                                product_link = requests.utils.unquote(url_match.group(1))
                    
                    results.append({
                        'retailer': retailer,
                        'price': price,
                        'link': product_link
                    })
                    
                    print(f"  ✓ Found: £{price:.2f} at {retailer}")
                    
                except Exception as e:
                    continue
            
            # Remove duplicates based on price and retailer
            unique_results = []
            seen = set()
            for result in results:
                key = (result['price'], result['retailer'])
                if key not in seen:
                    seen.add(key)
                    unique_results.append(result)
            
            if unique_results:
                print(f"  ✅ Successfully found {len(unique_results)} unique products")
                monitor_status['last_results'] = [
                    f"£{r['price']:.2f} at {r['retailer']}" 
                    for r in unique_results[:5]
                ]
            else:
                print(f"  ⚠️  No valid results after parsing")
                print(f"  💡 Debug info:")
                print(f"     - Price elements found: {len(price_elements)}")
                print(f"     - Containers found: {len(containers)}")
                print(f"     - URL: {url[:100]}...")
                monitor_status['last_results'] = [f"No results - {len(containers)} containers checked"]
            
            return unique_results
            
        except Exception as e:
            print(f"  ❌ Scraping error: {str(e)}")
            monitor_status['last_results'] = [f"Error: {str(e)}"]
            return []
    
    def check_price_alerts(self, product, current_results):
        """Check if price meets alert criteria"""
        if not current_results:
            return None
        
        lowest_result = min(current_results, key=lambda x: x['price'])
        current_price = lowest_result['price']
        
        alerts = []
        
        in_range = product['price_min'] <= current_price <= product['price_max']
        if in_range and product['last_alert_type'] != 'range':
            alerts.append({
                'type': 'range',
                'current_price': current_price,
                'result': lowest_result,
                'message': f"Price in target range: £{current_price:.2f} (£{product['price_min']}-£{product['price_max']})"
            })
        
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
        
        product_key = product['name'].lower()
        if product_key not in self.price_history:
            self.price_history[product_key] = {
                'lowest': current_price,
                'last_alert': ''
            }
        else:
            if current_price < self.price_history[product_key]['lowest']:
                self.price_history[product_key]['lowest'] = current_price
        
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
                print(f"  ✅ Alert sent: {alert['message']}")
                monitor_status['alerts_sent'] += 1
                
                time.sleep(1)
                
        except Exception as e:
            print(f"  ❌ Error sending Discord alert: {str(e)}")
    
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
        print(f"\n{'='*70}")
        print(f"🔍 PRICE CHECK STARTING")
        print(f"⏰ Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S GMT')}")
        print(f"{'='*70}")
        
        monitor_status['last_check'] = datetime.now().isoformat()
        monitor_status['total_checks'] += 1
        
        products = self.read_google_sheet()
        
        if not products:
            print("⚠️  No active products to monitor")
            return
        
        print(f"📊 Monitoring {len(products)} product(s)\n")
        
        for i, product in enumerate(products, 1):
            try:
                print(f"{'─'*70}")
                print(f"[{i}/{len(products)}] 📦 {product['name']}")
                print(f"🎯 Target: £{product['price_min']}-£{product['price_max']} | Drop Alert: {product['drop_threshold']}%")
                
                results = self.scrape_google_shopping_enhanced(product)
                
                if results:
                    alerts = self.check_price_alerts(product, results)
                    
                    lowest_result = min(results, key=lambda x: x['price'])
                    print(f"  💷 Lowest found: £{lowest_result['price']:.2f} at {lowest_result['retailer']}")
                    
                    # Show top 3 results
                    print(f"  📋 Top results:")
                    for j, r in enumerate(sorted(results, key=lambda x: x['price'])[:3], 1):
                        print(f"     {j}. £{r['price']:.2f} - {r['retailer']}")
                    
                    if alerts:
                        print(f"  🔔 ALERT TRIGGERED!")
                        self.send_discord_alert(product, alerts)
                    else:
                        print(f"  ℹ️  No alerts (price not in range or already alerted)")
                else:
                    print(f"  ❌ No results found")
                
                print()
                time.sleep(5)
                
            except Exception as e:
                print(f"  ❌ Error processing product: {str(e)}\n")
                continue
        
        print(f"{'='*70}")
        print(f"✅ CHECK COMPLETE at {datetime.now().strftime('%H:%M:%S GMT')}")
        print(f"⏰ Next check in {CHECK_INTERVAL/60:.0f} minutes")
        print(f"{'='*70}\n")
        
        monitor_status['next_check'] = datetime.fromtimestamp(time.time() + CHECK_INTERVAL).isoformat()
    
    def run(self):
        """Main monitoring loop"""
        print("=" * 70)
        print("🚀 DISCORD PRICE MONITOR - ENHANCED VERSION")
        print("=" * 70)
        print(f"⏱️  Check interval: {CHECK_INTERVAL/60:.0f} minutes")
        print(f"🔗 Discord webhook: {'✅ Configured' if self.webhook_url else '❌ Not configured'}")
        print(f"📊 Google Sheet: {'✅ Configured' if self.sheet_url else '❌ Not configured'}")
        print(f"🌐 Web dashboard: http://0.0.0.0:{PORT}")
        print("=" * 70)
        
        if not self.webhook_url:
            print("\n❌ ERROR: DISCORD_WEBHOOK_URL not set!")
            return
        
        if not self.sheet_url:
            print("\n❌ ERROR: GOOGLE_SHEET_URL not set!")
            return
        
        monitor_status['running'] = True
        
        # Run first check immediately
        print("\n🎬 Running initial check...")
        self.run_check()
        
        while True:
            try:
                time.sleep(CHECK_INTERVAL)
                self.run_check()
            except KeyboardInterrupt:
                print("\n\n🛑 Stopping monitor...")
                monitor_status['running'] = False
                break
            except Exception as e:
                error_msg = f"Unexpected error: {str(e)}"
                print(f"\n⚠️  {error_msg}")
                monitor_status['last_error'] = error_msg
                self.send_error_alert(error_msg)
                print("⏳ Waiting 60 seconds before retry...")
                time.sleep(60)

# Flask web dashboard
app = Flask(__name__)

DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Price Monitor Dashboard</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta http-equiv="refresh" content="30">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 20px;
        }
        .container { max-width: 900px; margin: 0 auto; }
        .card {
            background: white;
            border-radius: 12px;
            padding: 30px;
            margin-bottom: 20px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.2);
        }
        h1 { color: #667eea; margin-bottom: 10px; font-size: 2em; }
        .subtitle { color: #666; margin-bottom: 30px; }
        .status-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }
        .stat-box {
            background: #f8f9fa;
            padding: 20px;
            border-radius: 8px;
            text-align: center;
        }
        .stat-value {
            font-size: 2em;
            font-weight: bold;
            color: #667eea;
            margin-bottom: 5px;
        }
        .stat-label { color: #666; font-size: 0.9em; }
        .status-badge {
            display: inline-block;
            padding: 8px 16px;
            border-radius: 20px;
            font-weight: bold;
            font-size: 0.9em;
        }
        .status-running { background: #d4edda; color: #155724; }
        .status-stopped { background: #f8d7da; color: #721c24; }
        .info-row {
            display: flex;
            justify-content: space-between;
            padding: 12px 0;
            border-bottom: 1px solid #eee;
        }
        .info-row:last-child { border-bottom: none; }
        .info-label { color: #666; font-weight: 500; }
        .info-value { color: #333; font-family: monospace; font-size: 0.9em; }
        .error-box {
            background: #f8d7da;
            border: 1px solid #f5c6cb;
            color: #721c24;
            padding: 15px;
            border-radius: 8px;
            margin-top: 20px;
        }
        .results-box {
            background: #d1ecf1;
            border: 1px solid #bee5eb;
            color: #0c5460;
            padding: 15px;
            border-radius: 8px;
            margin-top: 20px;
        }
        .results-box ul { margin-left: 20px; margin-top: 10px; }
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }
        .pulse { animation: pulse 2s ease-in-out infinite; }
    </style>
</head>
<body>
    <div class="container">
        <div class="card">
            <h1>🔔 Price Monitor Dashboard</h1>
            <p class="subtitle">Enhanced version with detailed scraping</p>
            
            <div style="text-align: center; margin-bottom: 30px;">
                <span class="status-badge {{ 'status-running pulse' if status.running else 'status-stopped' }}">
                    {{ '🟢 RUNNING' if status.running else '🔴 STOPPED' }}
                </span>
            </div>
            
            <div class="status-grid">
                <div class="stat-box">
                    <div class="stat-value">{{ status.total_checks }}</div>
                    <div class="stat-label">Total Checks</div>
                </div>
                <div class="stat-box">
                    <div class="stat-value">{{ status.products_monitored }}</div>
                    <div class="stat-label">Products Monitored</div>
                </div>
                <div class="stat-box">
                    <div class="stat-value">{{ status.alerts_sent }}</div>
                    <div class="stat-label">Alerts Sent</div>
                </div>
            </div>
            
            <div class="info-row">
                <span class="info-label">Last Check:</span>
                <span class="info-value">{{ status.last_check or 'Never' }}</span>
            </div>
            <div class="info-row">
                <span class="info-label">Next Check:</span>
                <span class="info-value">{{ status.next_check or 'Calculating...' }}</span>
            </div>
            <div class="info-row">
                <span class="info-label">Check Interval:</span>
                <span class="info-value">{{ interval }} minutes</span>
            </div>
            
            {% if status.last_results %}
            <div class="results-box">
                <strong>📊 Last Search Results:</strong>
                <ul>
                {% for result in status.last_results %}
                    <li>{{ result }}</li>
                {% endfor %}
                </ul>
            </div>
            {% endif %}
            
            {% if status.last_error %}
            <div class="error-box">
                <strong>⚠️ Last Error:</strong><br>
                {{ status.last_error }}
            </div>
            {% endif %}
        </div>
    </div>
</body>
</html>
"""

@app.route('/')
def dashboard():
    return render_template_string(
        DASHBOARD_HTML,
        status=monitor_status,
        interval=CHECK_INTERVAL/60
    )

@app.route('/api/status')
def api_status():
    return jsonify(monitor_status)

@app.route('/health')
def health():
    return jsonify({'status': 'healthy', 'running': monitor_status['running']})

def run_monitor():
    """Run the price monitor in a separate thread"""
    monitor = PriceMonitor()
    monitor.run()

if __name__ == "__main__":
    # Start monitor in background thread
    monitor_thread = Thread(target=run_monitor, daemon=True)
    monitor_thread.start()
    
    # Start web server on main thread
    print(f"\n🌐 Starting web dashboard on port {PORT}...")
    app.run(host='0.0.0.0', port=PORT)
