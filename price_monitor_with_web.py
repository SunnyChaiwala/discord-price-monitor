#!/usr/bin/env python3
"""
Discord Price Monitor with Web Server
Monitors Google Shopping + provides web dashboard for Render.com
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
CHECK_INTERVAL = int(os.getenv('CHECK_INTERVAL', '1800'))  # 30 minutes default
PORT = int(os.getenv('PORT', '10000'))  # Render assigns this

# Excluded retailers
EXCLUDED_RETAILERS = ['shein', 'amazon', 'ebay']

# Global status for web dashboard
monitor_status = {
    'running': False,
    'last_check': None,
    'next_check': None,
    'total_checks': 0,
    'products_monitored': 0,
    'alerts_sent': 0,
    'last_error': None,
    'startup_time': datetime.now().isoformat()
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
                    print(f"Skipping invalid row: {e}")
                    continue
            
            print(f"✓ Loaded {len(products)} active products from sheet")
            monitor_status['products_monitored'] = len(products)
            return products
            
        except Exception as e:
            error_msg = f"Failed to read Google Sheet: {str(e)}"
            self.send_error_alert(error_msg)
            monitor_status['last_error'] = error_msg
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
            
            if product['url']:
                url = product['url']
            else:
                query = product['search_query'] or product['name']
                if product['specifications']:
                    query += ' ' + product['specifications']
                url = f"https://www.google.com/search?tbm=shop&q={requests.utils.quote(query)}&hl=en-GB&gl=GB"
            
            print(f"  Fetching: {product['name'][:50]}...")
            response = requests.get(url, headers=headers, timeout=15)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.content, 'html.parser')
            results = []
            
            product_cards = soup.find_all('div', {'class': lambda x: x and 'sh-dgr__content' in x})
            
            if not product_cards:
                product_cards = soup.find_all('div', {'data-docid': True})
            
            if not product_cards:
                product_cards = soup.find_all('div', {'class': lambda x: x and ('product' in x.lower() or 'item' in x.lower())})
            
            for card in product_cards[:15]:
                try:
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
                    
                    if not price_elem:
                        price_elem = card.find(string=re.compile(r'£\d'))
                        if price_elem:
                            price_elem = price_elem.parent
                    
                    if not price_elem:
                        continue
                    
                    price_text = price_elem.get_text()
                    price_match = re.search(r'£?\s*([\d,]+\.?\d*)', price_text)
                    
                    if not price_match:
                        continue
                    
                    price = float(price_match.group(1).replace(',', ''))
                    
                    retailer = 'Unknown'
                    retailer_elem = card.find('div', {'class': lambda x: x and 'merchant' in x.lower()})
                    if not retailer_elem:
                        retailer_elem = card.find('span', {'class': lambda x: x and 'store' in x.lower()})
                    if not retailer_elem:
                        link = card.find('a', href=True)
                        if link:
                            href = link['href']
                            domain_match = re.search(r'(?:https?://)?(?:www\.)?([^/]+)', href)
                            if domain_match:
                                retailer = domain_match.group(1)
                    
                    if retailer_elem:
                        retailer = retailer_elem.get_text().strip()
                    
                    retailer_lower = retailer.lower()
                    
                    if any(excluded in retailer_lower for excluded in EXCLUDED_RETAILERS):
                        continue
                    
                    link_elem = card.find('a', href=True)
                    product_link = link_elem['href'] if link_elem else url
                    
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
                print(f"  ✓ Alert sent: {alert['message']}")
                monitor_status['alerts_sent'] += 1
                
                time.sleep(1)
                
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
        
        monitor_status['last_check'] = datetime.now().isoformat()
        monitor_status['total_checks'] += 1
        
        products = self.read_google_sheet()
        
        if not products:
            print("⚠ No active products to monitor")
            return
        
        print(f"Checking {len(products)} products...\n")
        
        for i, product in enumerate(products, 1):
            try:
                print(f"[{i}/{len(products)}] {product['name']}")
                
                results = self.scrape_google_shopping(product)
                
                if results:
                    alerts = self.check_price_alerts(product, results)
                    
                    lowest_result = min(results, key=lambda x: x['price'])
                    print(f"  💷 Lowest: £{lowest_result['price']:.2f} at {lowest_result['retailer']}")
                    
                    if alerts:
                        self.send_discord_alert(product, alerts)
                    else:
                        print(f"  ℹ️  No alerts triggered")
                else:
                    print(f"  ⚠ No results found - check your search query")
                
                print()
                time.sleep(5)
                
            except Exception as e:
                print(f"  ✗ Error: {str(e)}\n")
                continue
        
        print(f"✅ Check complete at {datetime.now().strftime('%H:%M:%S GMT')}")
        monitor_status['next_check'] = datetime.fromtimestamp(time.time() + CHECK_INTERVAL).isoformat()
        print(f"Next check in {CHECK_INTERVAL/60:.0f} minutes...")
    
    def run(self):
        """Main monitoring loop"""
        print("=" * 60)
        print("🚀 DISCORD PRICE MONITOR STARTING")
        print("=" * 60)
        print(f"Check interval: {CHECK_INTERVAL/60:.0f} minutes")
        print(f"Discord webhook: {'✓ Configured' if self.webhook_url else '✗ Not configured'}")
        print(f"Google Sheet: {'✓ Configured' if self.sheet_url else '✗ Not configured'}")
        print(f"Web dashboard: http://0.0.0.0:{PORT}")
        print("=" * 60)
        
        if not self.webhook_url:
            print("\n⚠️ ERROR: DISCORD_WEBHOOK_URL not set!")
            return
        
        if not self.sheet_url:
            print("\n⚠️ ERROR: GOOGLE_SHEET_URL not set!")
            return
        
        monitor_status['running'] = True
        
        while True:
            try:
                self.run_check()
                time.sleep(CHECK_INTERVAL)
            except KeyboardInterrupt:
                print("\n\n🛑 Stopping monitor...")
                monitor_status['running'] = False
                break
            except Exception as e:
                error_msg = f"Unexpected error: {str(e)}"
                print(f"\n⚠️ {error_msg}")
                monitor_status['last_error'] = error_msg
                self.send_error_alert(error_msg)
                print("Waiting 60 seconds before retry...")
                time.sleep(60)

# Flask web dashboard
app = Flask(__name__)

DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Price Monitor Dashboard</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 20px;
        }
        .container {
            max-width: 800px;
            margin: 0 auto;
        }
        .card {
            background: white;
            border-radius: 12px;
            padding: 30px;
            margin-bottom: 20px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.2);
        }
        h1 {
            color: #667eea;
            margin-bottom: 10px;
            font-size: 2em;
        }
        .subtitle {
            color: #666;
            margin-bottom: 30px;
        }
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
        .stat-label {
            color: #666;
            font-size: 0.9em;
        }
        .status-badge {
            display: inline-block;
            padding: 8px 16px;
            border-radius: 20px;
            font-weight: bold;
            font-size: 0.9em;
        }
        .status-running {
            background: #d4edda;
            color: #155724;
        }
        .status-stopped {
            background: #f8d7da;
            color: #721c24;
        }
        .info-row {
            display: flex;
            justify-content: space-between;
            padding: 12px 0;
            border-bottom: 1px solid #eee;
        }
        .info-row:last-child {
            border-bottom: none;
        }
        .info-label {
            color: #666;
            font-weight: 500;
        }
        .info-value {
            color: #333;
            font-family: monospace;
        }
        .error-box {
            background: #f8d7da;
            border: 1px solid #f5c6cb;
            color: #721c24;
            padding: 15px;
            border-radius: 8px;
            margin-top: 20px;
        }
        .refresh-btn {
            background: #667eea;
            color: white;
            border: none;
            padding: 12px 24px;
            border-radius: 8px;
            cursor: pointer;
            font-size: 1em;
            margin-top: 20px;
            transition: background 0.3s;
        }
        .refresh-btn:hover {
            background: #5568d3;
        }
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }
        .pulse {
            animation: pulse 2s ease-in-out infinite;
        }
    </style>
    <script>
        function refreshData() {
            fetch('/api/status')
                .then(response => response.json())
                .then(data => {
                    document.getElementById('status').innerHTML = 
                        data.running ? 
                        '<span class="status-badge status-running pulse">🟢 RUNNING</span>' : 
                        '<span class="status-badge status-stopped">🔴 STOPPED</span>';
                    document.getElementById('total-checks').textContent = data.total_checks;
                    document.getElementById('products').textContent = data.products_monitored;
                    document.getElementById('alerts').textContent = data.alerts_sent;
                    document.getElementById('last-check').textContent = 
                        data.last_check ? new Date(data.last_check).toLocaleString() : 'Never';
                    document.getElementById('next-check').textContent = 
                        data.next_check ? new Date(data.next_check).toLocaleString() : 'Calculating...';
                    
                    if (data.last_error) {
                        document.getElementById('error-section').style.display = 'block';
                        document.getElementById('error-message').textContent = data.last_error;
                    } else {
                        document.getElementById('error-section').style.display = 'none';
                    }
                });
        }
        
        setInterval(refreshData, 5000); // Refresh every 5 seconds
        window.onload = refreshData;
    </script>
</head>
<body>
    <div class="container">
        <div class="card">
            <h1>🔔 Price Monitor Dashboard</h1>
            <p class="subtitle">Real-time monitoring status and statistics</p>
            
            <div style="text-align: center; margin-bottom: 30px;">
                <div id="status">
                    <span class="status-badge {{ 'status-running pulse' if status.running else 'status-stopped' }}">
                        {{ '🟢 RUNNING' if status.running else '🔴 STOPPED' }}
                    </span>
                </div>
            </div>
            
            <div class="status-grid">
                <div class="stat-box">
                    <div class="stat-value" id="total-checks">{{ status.total_checks }}</div>
                    <div class="stat-label">Total Checks</div>
                </div>
                <div class="stat-box">
                    <div class="stat-value" id="products">{{ status.products_monitored }}</div>
                    <div class="stat-label">Products Monitored</div>
                </div>
                <div class="stat-box">
                    <div class="stat-value" id="alerts">{{ status.alerts_sent }}</div>
                    <div class="stat-label">Alerts Sent</div>
                </div>
            </div>
            
            <div class="info-row">
                <span class="info-label">Last Check:</span>
                <span class="info-value" id="last-check">
                    {{ status.last_check or 'Never' }}
                </span>
            </div>
            <div class="info-row">
                <span class="info-label">Next Check:</span>
                <span class="info-value" id="next-check">
                    {{ status.next_check or 'Calculating...' }}
                </span>
            </div>
            <div class="info-row">
                <span class="info-label">Check Interval:</span>
                <span class="info-value">{{ interval }} minutes</span>
            </div>
            <div class="info-row">
                <span class="info-label">Started:</span>
                <span class="info-value">{{ status.startup_time }}</span>
            </div>
            
            <div id="error-section" style="display: {{ 'block' if status.last_error else 'none' }};">
                <div class="error-box">
                    <strong>⚠️ Last Error:</strong><br>
                    <span id="error-message">{{ status.last_error }}</span>
                </div>
            </div>
            
            <center>
                <button class="refresh-btn" onclick="refreshData()">🔄 Refresh Now</button>
            </center>
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
