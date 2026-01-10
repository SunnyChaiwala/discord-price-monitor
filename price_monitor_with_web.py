#!/usr/bin/env python3
"""
Discord Price Monitor - Google Shopping via Serper.dev
Uses Serper.dev API to access real Google Shopping results
"""

import os
import time
import json
import requests
from datetime import datetime
import re
import csv
from io import StringIO
from threading import Thread
from flask import Flask, render_template_string, jsonify

# Configuration
DISCORD_WEBHOOK_URL = os.getenv('DISCORD_WEBHOOK_URL', '')
GOOGLE_SHEET_URL = os.getenv('GOOGLE_SHEET_URL', '')
SERPER_API_KEY = os.getenv('SERPER_API_KEY', '')
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
    'last_results': [],
    'api_calls_used': 0
}

class PriceMonitor:
    def __init__(self):
        self.webhook_url = DISCORD_WEBHOOK_URL
        self.sheet_url = GOOGLE_SHEET_URL
        self.serper_key = SERPER_API_KEY
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
                active_value = str(row.get('Active', '')).strip().upper()
                if active_value not in ['TRUE', 'YES', '1', 'Y']:
                    continue
                
                try:
                    product = {
                        'name': row.get('Product Name', '').strip(),
                        'search_query': row.get('Search Query', '').strip(),
                        'specifications': row.get('Specifications', '').strip(),
                        'price_min': float(row.get('Target Price Min', 0) or 0),
                        'price_max': float(row.get('Target Price Max', 999999) or 999999),
                        'drop_threshold': float(row.get('Drop Alert %', 25) or 25),
                    }
                    
                    if not product['name']:
                        continue
                    
                    # Build full search query
                    query = product['search_query'] or product['name']
                    if product['specifications']:
                        query += ' ' + product['specifications']
                    product['full_query'] = query
                    
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
            
            print(f"Loaded {len(products)} active products from sheet")
            monitor_status['products_monitored'] = len(products)
            return products
            
        except Exception as e:
            error_msg = f"Failed to read Google Sheet: {str(e)}"
            self.send_error_alert(error_msg)
            monitor_status['last_error'] = error_msg
            print(f"Error reading sheet: {e}")
            return []
    
    def search_google_shopping_serper(self, product):
        """Search Google Shopping using Serper.dev API"""
        try:
            if not self.serper_key:
                raise ValueError("SERPER_API_KEY not set")
            
            headers = {
                'X-API-KEY': self.serper_key,
                'Content-Type': 'application/json'
            }
            
            payload = {
                'q': product['full_query'],
                'gl': 'uk',
                'hl': 'en',
                'location': 'London, England, United Kingdom',
                'num': 20
            }
            
            print(f"  Searching: {product['full_query']}")
            
            response = requests.post(
                'https://google.serper.dev/shopping',
                headers=headers,
                json=payload,
                timeout=15
            )
            response.raise_for_status()
            
            data = response.json()
            monitor_status['api_calls_used'] += 1
            
            results = []
            shopping_results = data.get('shopping', [])
            
            for item in shopping_results:
                try:
                    price_str = item.get('price', '')
                    if not price_str:
                        continue
                    
                    price_match = re.search(r'([\d,]+\.?\d*)', price_str)
                    if not price_match:
                        continue
                    
                    price = float(price_match.group(1).replace(',', ''))
                    retailer = item.get('source', 'Unknown')
                    retailer_lower = retailer.lower()
                    
                    if any(excluded in retailer_lower for excluded in EXCLUDED_RETAILERS):
                        continue
                    
                    link = item.get('link', '')
                    
                    results.append({
                        'retailer': retailer,
                        'price': price,
                        'link': link,
                        'title': item.get('title', product['name'])
                    })
                    
                except Exception:
                    continue
            
            if results:
                print(f"  Found {len(results)} products")
                monitor_status['last_results'] = [
                    f"£{r['price']:.2f} - {r['retailer']}" 
                    for r in sorted(results, key=lambda x: x['price'])[:5]
                ]
            else:
                print(f"  No results found")
                monitor_status['last_results'] = ["No results"]
            
            return results
            
        except Exception as e:
            error_msg = f"API error: {str(e)}"
            print(f"  {error_msg}")
            monitor_status['last_results'] = [error_msg]
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
                'message': f"Price in target range: £{current_price:.2f}"
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
                    'message': f"Price dropped {drop_percentage:.1f}%"
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
                    "title": f"Price Alert: {product['name']}",
                    "color": 0x00ff00 if alert['type'] == 'range' else 0xff9900,
                    "fields": [
                        {
                            "name": "Current Price",
                            "value": f"£{alert['current_price']:.2f}",
                            "inline": True
                        },
                        {
                            "name": "Retailer",
                            "value": alert['result']['retailer'],
                            "inline": True
                        }
                    ],
                    "timestamp": datetime.utcnow().isoformat()
                }
                
                if alert['type'] == 'drop':
                    embed['fields'].insert(1, {
                        "name": "Previous Lowest",
                        "value": f"£{alert['previous_lowest']:.2f}",
                        "inline": True
                    })
                elif alert['type'] == 'range':
                    embed['fields'].append({
                        "name": "Target Range",
                        "value": f"£{product['price_min']}-£{product['price_max']}",
                        "inline": True
                    })
                
                if alert['result']['link']:
                    embed['url'] = alert['result']['link']
                
                payload = {"embeds": [embed]}
                
                response = requests.post(self.webhook_url, json=payload, timeout=10)
                response.raise_for_status()
                print(f"  Alert sent")
                monitor_status['alerts_sent'] += 1
                time.sleep(1)
                
        except Exception as e:
            print(f"  Error sending alert: {e}")
    
    def send_error_alert(self, error_message):
        """Send error notification to Discord"""
        try:
            embed = {
                "title": "Price Monitor Error",
                "description": error_message,
                "color": 0xff0000,
                "timestamp": datetime.utcnow().isoformat()
            }
            payload = {"embeds": [embed]}
            requests.post(self.webhook_url, json=payload, timeout=10)
        except:
            pass
    
    def run_check(self):
        """Run a single check cycle"""
        print(f"\n{'='*60}")
        print(f"Price check at {datetime.now().strftime('%H:%M:%S GMT')}")
        print(f"{'='*60}")
        
        monitor_status['last_check'] = datetime.now().isoformat()
        monitor_status['total_checks'] += 1
        
        products = self.read_google_sheet()
        
        if not products:
            print("No active products")
            return
        
        for i, product in enumerate(products, 1):
            try:
                print(f"\n[{i}/{len(products)}] {product['name']}")
                
                results = self.search_google_shopping_serper(product)
                
                if results:
                    alerts = self.check_price_alerts(product, results)
                    
                    lowest_result = min(results, key=lambda x: x['price'])
                    print(f"  Best: £{lowest_result['price']:.2f} at {lowest_result['retailer']}")
                    
                    if alerts:
                        print(f"  ALERT TRIGGERED")
                        self.send_discord_alert(product, alerts)
                    else:
                        print(f"  No alerts")
                
                time.sleep(2)
                
            except Exception as e:
                print(f"  Error: {e}")
                continue
        
        print(f"\nCheck complete. API calls used: {monitor_status['api_calls_used']}")
        monitor_status['next_check'] = datetime.fromtimestamp(time.time() + CHECK_INTERVAL).isoformat()
    
    def run(self):
        """Main monitoring loop"""
        print("=" * 60)
        print("GOOGLE SHOPPING MONITOR (Serper.dev)")
        print("=" * 60)
        print(f"Check interval: {CHECK_INTERVAL/60:.0f} minutes")
        print(f"Discord: {'OK' if self.webhook_url else 'NOT SET'}")
        print(f"Sheet: {'OK' if self.sheet_url else 'NOT SET'}")
        print(f"Serper: {'OK' if self.serper_key else 'NOT SET - Get key at serper.dev'}")
        print("=" * 60)
        
        if not self.webhook_url or not self.sheet_url:
            print("\nERROR: Missing configuration!")
            return
        
        if not self.serper_key:
            print("\nERROR: SERPER_API_KEY not set!")
            print("Get free key at: https://serper.dev")
            return
        
        monitor_status['running'] = True
        
        print("\nRunning first check...")
        self.run_check()
        
        while True:
            try:
                time.sleep(CHECK_INTERVAL)
                self.run_check()
            except KeyboardInterrupt:
                print("\nStopping...")
                monitor_status['running'] = False
                break
            except Exception as e:
                error_msg = f"Error: {e}"
                print(f"\n{error_msg}")
                monitor_status['last_error'] = error_msg
                time.sleep(60)

# Flask app
app = Flask(__name__)

HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Price Monitor</title>
    <meta http-equiv="refresh" content="30">
    <style>
        body { font-family: Arial; background: #f0f0f0; padding: 20px; }
        .card { background: white; border-radius: 8px; padding: 20px; max-width: 800px; margin: 0 auto; }
        h1 { color: #4285f4; }
        .stat { display: inline-block; margin: 10px 20px 10px 0; }
        .value { font-size: 2em; font-weight: bold; color: #4285f4; }
        .label { color: #666; }
        .status { padding: 5px 15px; border-radius: 15px; display: inline-block; }
        .running { background: #d4edda; color: #155724; }
        .stopped { background: #f8d7da; color: #721c24; }
        .results { background: #e8f0fe; padding: 15px; border-radius: 5px; margin: 15px 0; }
    </style>
</head>
<body>
    <div class="card">
        <h1>Google Shopping Monitor</h1>
        <p><span class="status {{ 'running' if status.running else 'stopped' }}">
            {{ 'RUNNING' if status.running else 'STOPPED' }}
        </span></p>
        
        <div class="stat">
            <div class="value">{{ status.total_checks }}</div>
            <div class="label">Checks</div>
        </div>
        <div class="stat">
            <div class="value">{{ status.products_monitored }}</div>
            <div class="label">Products</div>
        </div>
        <div class="stat">
            <div class="value">{{ status.alerts_sent }}</div>
            <div class="label">Alerts</div>
        </div>
        <div class="stat">
            <div class="value">{{ status.api_calls_used }}</div>
            <div class="label">API Calls</div>
        </div>
        
        <p><strong>Last Check:</strong> {{ status.last_check or 'Never' }}</p>
        <p><strong>Next Check:</strong> {{ status.next_check or 'Pending' }}</p>
        
        {% if status.last_results %}
        <div class="results">
            <strong>Latest Results:</strong>
            <ul>
            {% for result in status.last_results %}
                <li>{{ result }}</li>
            {% endfor %}
            </ul>
        </div>
        {% endif %}
        
        {% if status.last_error %}
        <div style="background: #f8d7da; padding: 15px; border-radius: 5px; color: #721c24;">
            <strong>Error:</strong> {{ status.last_error }}
        </div>
        {% endif %}
    </div>
</body>
</html>
"""

@app.route('/')
def dashboard():
    return render_template_string(HTML, status=monitor_status)

@app.route('/health')
def health():
    return jsonify({'status': 'ok'})

def run_monitor():
    monitor = PriceMonitor()
    monitor.run()

if __name__ == "__main__":
    monitor_thread = Thread(target=run_monitor, daemon=True)
    monitor_thread.start()
    print(f"\nStarting dashboard on port {PORT}...")
    app.run(host='0.0.0.0', port=PORT)
