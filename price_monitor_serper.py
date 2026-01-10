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
import csv
from io import StringIO
from threading import Thread
from flask import Flask, render_template_string, jsonify

# Configuration
DISCORD_WEBHOOK_URL = os.getenv('DISCORD_WEBHOOK_URL', '')
GOOGLE_SHEET_URL = os.getenv('GOOGLE_SHEET_URL', '')
SERPER_API_KEY = os.getenv('SERPER_API_KEY', '')  # You'll add this
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
        import re
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
    
    def search_google_shopping_serper(self, product):
        """Search Google Shopping using Serper.dev API"""
        try:
            if not self.serper_key:
                raise ValueError("SERPER_API_KEY not set! Get free key at serper.dev")
            
            headers = {
                'X-API-KEY': self.serper_key,
                'Content-Type': 'application/json'
            }
            
            payload = {
                'q': product['full_query'],
                'gl': 'uk',  # UK results
                'hl': 'en',  # English
                'location': 'London, England, United Kingdom',
                'num': 20  # Get top 20 results
            }
            
            print(f"  🔍 Searching: {product['full_query']}")
            
            # Call Serper.dev Google Shopping API
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
            
            # Parse shopping results
            shopping_results = data.get('shopping', [])
            
            for item in shopping_results:
                try:
                    # Extract price
                    price_str = item.get('price', '')
                    if not price_str:
                        continue
                    
                    # Clean price (remove £, commas)
                    import re
                    price_match = re.search(r'([\d,]+\.?\d*)', price_str)
                    if not price_match:
                        continue
                    
                    price = float(price_match.group(1).replace(',', ''))
                    
                    # Get retailer
                    retailer = item.get('source', 'Unknown')
                    
                    # Check if excluded
                    retailer_lower = retailer.lower()
                    if any(excluded in retailer_lower for excluded in EXCLUDED_RETAILERS):
                        continue
                    
                    # Get link
                    link = item.get('link', '')
                    
                    results.append({
                        'retailer': retailer,
                        'price': price,
                        'link': link,
                        'title': item.get('title', product['name'])
                    })
                    
                except Exception as e:
                    continue
            
            if results:
                print(f"  ✅ Found {len(results)} products from Google Shopping")
                monitor_status['last_results'] = [
                    f"£{r['price']:.2f} - {r['retailer']}" 
                    for r in sorted(results, key=lambda x: x['price'])[:5]
                ]
            else:
                print(f"  ⚠️  No valid results found")
                monitor_status['last_results'] = ["No results from Google Shopping"]
            
            return results
            
        except Exception as e:
            error_msg = f"Serper API error: {str(e)}"
            print(f"  ❌ {error_msg}")
            monitor_status['last_results'] = [error_msg]
            if 'Invalid API key' in str(e) or 'API key' in str(e):
                monitor_status['last_error'] = "Invalid Serper API key! Get one at serper.dev"
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
                            "value": alert['result']['retailer'],
                            "inline": True
                        }
                    ],
                    "timestamp": datetime.utcnow().isoformat(),
                    "footer": {
                        "text": "Google Shopping Monitor • " + datetime.now().strftime('%H:%M GMT')
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
                        "name": "🔗 Buy Now",
                        "value": f"[View Product]({alert['result']['link']})",
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
        print(f"🛍️ GOOGLE SHOPPING PRICE CHECK (via Serper.dev)")
        print(f"⏰ Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S GMT')}")
        print(f"📊 API calls used this session: {monitor_status['api_calls_used']}")
        print(f"{'='*70}")
        
        monitor_status['last_check'] = datetime.now().isoformat()
        monitor_status['total_checks'] += 1
        
        products = self.read_google_sheet()
        
        if not products:
            print("⚠️  No active products to monitor")
            return
        
        print(f"📦 Monitoring {len(products)} product(s)\n")
        
        for i, product in enumerate(products, 1):
            try:
                print(f"{'─'*70}")
                print(f"[{i}/{len(products)}] 📦 {product['name']}")
                print(f"🎯 Target: £{product['price_min']}-£{product['price_max']} | Drop Alert: {product['drop_threshold']}%")
                
                results = self.search_google_shopping_serper(product)
                
                if results:
                    alerts = self.check_price_alerts(product, results)
                    
                    lowest_result = min(results, key=lambda x: x['price'])
                    print(f"  💷 Best price: £{lowest_result['price']:.2f} at {lowest_result['retailer']}")
                    
                    # Show top 3 results
                    print(f"  📋 Top prices:")
                    for j, r in enumerate(sorted(results, key=lambda x: x['price'])[:3], 1):
                        print(f"     {j}. £{r['price']:.2f} - {r['retailer']}")
                    
                    if alerts:
                        print(f"  🔔 ALERT TRIGGERED!")
                        self.send_discord_alert(product, alerts)
                    else:
                        print(f"  ℹ️  No alerts triggered")
                else:
                    print(f"  ❌ No results found")
                
                print()
                time.sleep(2)
                
            except Exception as e:
                print(f"  ❌ Error: {str(e)}\n")
                continue
        
        print(f"{'='*70}")
        print(f"✅ CHECK COMPLETE at {datetime.now().strftime('%H:%M:%S GMT')}")
        print(f"⏰ Next check in {CHECK_INTERVAL/60:.0f} minutes")
        print(f"📊 Total API calls used: {monitor_status['api_calls_used']}")
        print(f"{'='*70}\n")
        
        monitor_status['next_check'] = datetime.fromtimestamp(time.time() + CHECK_INTERVAL).isoformat()
    
    def run(self):
        """Main monitoring loop"""
        print("=" * 70)
        print("🛍️ GOOGLE SHOPPING MONITOR (via Serper.dev API)")
        print("=" * 70)
        print(f"⏱️  Check interval: {CHECK_INTERVAL/60:.0f} minutes")
        print(f"🔗 Discord webhook: {'✅ Configured' if self.webhook_url else '❌ Not configured'}")
        print(f"📊 Google Sheet: {'✅ Configured' if self.sheet_url else '❌ Not configured'}")
        print(f"🔑 Serper API key: {'✅ Configured' if self.serper_key else '❌ NOT SET - GET ONE AT serper.dev'}")
        print(f"🌐 Web dashboard: http://0.0.0.0:{PORT}")
        print(f"📦 Data source: Real Google Shopping via Serper.dev")
        print("=" * 70)
        
        if not self.webhook_url:
            print("\n❌ ERROR: DISCORD_WEBHOOK_URL not set!")
            return
        
        if not self.sheet_url:
            print("\n❌ ERROR: GOOGLE_SHEET_URL not set!")
            return
        
        if not self.serper_key:
            print("\n❌ ERROR: SERPER_API_KEY not set!")
            print("📝 Get your FREE API key at: https://serper.dev")
            print("   - Sign up for free")
            print("   - Get 2,500 free searches/month")
            print("   - Add key to Render environment variables")
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
    <title>Google Shopping Monitor</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta http-equiv="refresh" content="30">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif;
            background: linear-gradient(135deg, #4285f4 0%, #34a853 100%);
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
        h1 { color: #4285f4; margin-bottom: 10px; font-size: 2em; }
        .subtitle { color: #666; margin-bottom: 30px; }
        .badge {
            display: inline-block;
            padding: 4px 12px;
            border-radius: 12px;
            background: #e8f0fe;
            color: #1967d2;
            font-size: 0.85em;
            font-weight: 600;
            margin-left: 10px;
        }
        .status-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
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
            color: #4285f4;
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
            background: #e8f0fe;
            border: 1px solid #d2e3fc;
            color: #1967d2;
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
            <h1>🛍️ Google Shopping Monitor</h1>
            <p class="subtitle">
                Real-time price tracking across UK retailers
                <span class="badge">Via Serper.dev API</span>
            </p>
            
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
                <div class="stat-box">
                    <div class="stat-value">{{ status.api_calls_used }}</div>
                    <div class="stat-label">API Calls Used</div>
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
                <strong>🛒 Latest Google Shopping Results:</strong>
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
