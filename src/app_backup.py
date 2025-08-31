from flask import Flask, jsonify
import sys
import os
import time
import statistics
import json
from datetime import datetime, timedelta
from collections import deque

# Add temp3 directory to path to use the working implementation
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), 'temp3'))
import starlink_grpc

app = Flask(__name__)

# In-memory storage for historical data (in production, use Redis/database)
class DataStore:
    def __init__(self, max_points=300):  # Store 5 minutes at 1-second intervals
        self.max_points = max_points
        self.timestamps = deque(maxlen=max_points)
        self.latency_data = deque(maxlen=max_points)
        self.download_speeds = deque(maxlen=max_points)
        self.upload_speeds = deque(maxlen=max_points)
        self.obstruction_data = deque(maxlen=max_points)
        self.quality_scores = deque(maxlen=max_points)
    
    def add_data_point(self, latency, download_mbps, upload_mbps, obstruction_pct, quality_score):
        now = datetime.now()
        self.timestamps.append(now.strftime('%H:%M:%S'))
        self.latency_data.append(latency)
        self.download_speeds.append(download_mbps)
        self.upload_speeds.append(upload_mbps)
        self.obstruction_data.append(obstruction_pct)
        self.quality_scores.append(quality_score)
    
    def get_chart_data(self):
        return {
            'timestamps': list(self.timestamps),
            'latency': list(self.latency_data),
            'download_speeds': list(self.download_speeds),
            'upload_speeds': list(self.upload_speeds),
            'obstruction': list(self.obstruction_data),
            'quality_scores': list(self.quality_scores)
        }

# Global data store
data_store = DataStore()

def calculate_quality_score(latency, download_mbps, upload_mbps, obstruction_pct, snr_good):
    """Calculate connection quality score (0-100)"""
    score = 100
    
    # Latency impact (0-30 points)
    if latency > 100:
        score -= 30
    elif latency > 75:
        score -= 20
    elif latency > 50:
        score -= 10
    
    # Speed impact (0-25 points each)
    if download_mbps < 10:
        score -= 25
    elif download_mbps < 25:
        score -= 15
    elif download_mbps < 50:
        score -= 5
    
    if upload_mbps < 3:
        score -= 25
    elif upload_mbps < 8:
        score -= 15
    elif upload_mbps < 15:
        score -= 5
    
    # Obstruction impact (0-20 points)
    if obstruction_pct > 10:
        score -= 20
    elif obstruction_pct > 5:
        score -= 15
    elif obstruction_pct > 1:
        score -= 10
    elif obstruction_pct > 0.1:
        score -= 5
    
    # SNR impact (0-10 points)
    if not snr_good:
        score -= 10
    
    return max(0, min(100, score))

def get_speed_data():
    """Get speed data - try multiple approaches to find actual speeds"""
    try:
        # Method 1: Try bulk history for recent high activity
        bulk_data = starlink_grpc.history_bulk_data(300)  # Last 5 minutes
        
        if bulk_data and len(bulk_data) > 1:
            data = bulk_data[1]
            
            downlink_data = data.get('downlink_throughput_bps', [])
            uplink_data = data.get('uplink_throughput_bps', [])
            
            if downlink_data and uplink_data:
                # Look for significant activity in the last 5 minutes
                active_downlink = [x for x in downlink_data if x > 1e6]  # > 1 Mbps
                active_uplink = [x for x in uplink_data if x > 1e6]      # > 1 Mbps
                
                # Also get recent activity regardless of threshold
                recent_downlink = [x for x in downlink_data[-60:] if x > 0]
                recent_uplink = [x for x in uplink_data[-60:] if x > 0]
                
                result = {}
                
                if active_downlink and active_uplink:
                    # We found periods of high activity
                    result['download_mbps'] = statistics.mean(active_downlink) / 1e6
                    result['upload_mbps'] = statistics.mean(active_uplink) / 1e6
                    result['peak_download'] = max(active_downlink) / 1e6
                    result['peak_upload'] = max(active_uplink) / 1e6
                    result['type'] = f"Active usage (last 5min, {len(active_downlink)} samples)"
                    return result
                
                elif recent_downlink and recent_uplink:
                    # Use recent data even if low
                    avg_down = statistics.mean(recent_downlink)
                    avg_up = statistics.mean(recent_uplink)
                    result['download_mbps'] = avg_down / 1e6
                    result['upload_mbps'] = avg_up / 1e6
                    result['peak_download'] = max(recent_downlink) / 1e6
                    result['peak_upload'] = max(recent_uplink) / 1e6
                    result['type'] = f"Recent activity ({len(recent_downlink)} samples)"
                    return result
    
    except Exception as e:
        print(f"Error getting bulk speeds: {e}")
    
    # Method 2: Fallback to instantaneous from status
    try:
        status = starlink_grpc.get_status()
        downlink_mbps = status.downlink_throughput_bps / 1e6 if hasattr(status, 'downlink_throughput_bps') else 0
        uplink_mbps = status.uplink_throughput_bps / 1e6 if hasattr(status, 'uplink_throughput_bps') else 0
        
        return {
            'download_mbps': downlink_mbps,
            'upload_mbps': uplink_mbps,
            'peak_download': downlink_mbps,
            'peak_upload': uplink_mbps,
            'type': 'Current instantaneous'
        }
    except Exception as e:
        print(f"Error getting status speeds: {e}")
    
    return None

# API Endpoints for Charts
@app.route('/api/chart-data')
def chart_data():
    """Return chart data in JSON format"""
    return jsonify(data_store.get_chart_data())

@app.route('/api/current-stats')
def current_stats():
    """Return current statistics for updating charts"""
    try:
        status = starlink_grpc.get_status()
        speed_data = get_speed_data()
        
        # Get current metrics
        latency = status.pop_ping_latency_ms if hasattr(status, 'pop_ping_latency_ms') else 0
        downlink_mbps = speed_data['download_mbps'] if speed_data else 0
        uplink_mbps = speed_data['upload_mbps'] if speed_data else 0
        
        # Obstruction info
        fraction_obstructed = 0
        if hasattr(status, 'obstruction_stats') and hasattr(status.obstruction_stats, 'fraction_obstructed'):
            fraction_obstructed = status.obstruction_stats.fraction_obstructed * 100
        
        # SNR status
        snr_above_noise = status.is_snr_above_noise_floor if hasattr(status, 'is_snr_above_noise_floor') else False
        
        # Calculate quality score
        quality_score = calculate_quality_score(latency, downlink_mbps, uplink_mbps, fraction_obstructed, snr_above_noise)
        
        # Add to data store
        data_store.add_data_point(latency, downlink_mbps, uplink_mbps, fraction_obstructed, quality_score)
        
        return jsonify({
            'latency': round(latency, 1),
            'download_mbps': round(downlink_mbps, 1),
            'upload_mbps': round(uplink_mbps, 1),
            'obstruction_pct': round(fraction_obstructed, 2),
            'quality_score': round(quality_score, 0),
            'timestamp': datetime.now().strftime('%H:%M:%S')
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/speedtest')
def speedtest_trigger():
    """Trigger a speed test message"""
    return """
    <html>
    <head><title>Speed Test Trigger</title></head>
    <body style="font-family: Arial, sans-serif; padding: 40px;">
    <h1>🚀 Speed Test Instructions</h1>
    <p><strong>To see your real speeds in the monitor:</strong></p>
    <ol>
        <li>Keep this browser tab open</li>
        <li>In another tab, go to <a href="https://fast.com" target="_blank">fast.com</a> or <a href="https://speedtest.net" target="_blank">speedtest.net</a></li>
        <li>Run a speed test</li>
        <li>Return to the main monitor page - it should now show your actual speeds!</li>
    </ol>
    <p><a href="/" style="color: #667eea;">← Back to Monitor</a></p>
    </body>
    </html>
    """

@app.route('/')
def index():
    try:
        # Get the status using the working temp3 implementation
        status = starlink_grpc.get_status()
        
        # Get speed data
        speed_data = get_speed_data()
        
        if speed_data:
            downlink_mbps = speed_data['download_mbps']
            uplink_mbps = speed_data['upload_mbps']
            peak_down = speed_data['peak_download']
            peak_up = speed_data['peak_upload']
            speed_note = speed_data['type']
            has_real_data = 'Active usage' in speed_note
        else:
            downlink_mbps = uplink_mbps = peak_down = peak_up = 0
            speed_note = "No data available"
            has_real_data = False
        
        # Get connection state from device_state
        if hasattr(status, 'device_state') and hasattr(status.device_state, 'uptime_s'):
            uptime = status.device_state.uptime_s
            state = "CONNECTED"
        else:
            uptime = 0
            state = "UNKNOWN"
        
        # Format uptime
        uptime_str = f"{uptime // 3600}h {(uptime % 3600) // 60}m" if uptime > 0 else "N/A"
        
        # Get latency
        latency = status.pop_ping_latency_ms if hasattr(status, 'pop_ping_latency_ms') else 0
        
        # Get obstruction info
        fraction_obstructed = 0
        if hasattr(status, 'obstruction_stats') and hasattr(status.obstruction_stats, 'fraction_obstructed'):
            fraction_obstructed = status.obstruction_stats.fraction_obstructed * 100
        
        # Get device info
        hardware_version = ""
        software_version = ""
        if hasattr(status, 'device_info'):
            hardware_version = status.device_info.hardware_version if hasattr(status.device_info, 'hardware_version') else ""
            software_version = status.device_info.software_version if hasattr(status.device_info, 'software_version') else ""
        
        # Get GPS status
        gps_valid = False
        gps_sats = 0
        if hasattr(status, 'gps_stats'):
            gps_valid = status.gps_stats.gps_valid if hasattr(status.gps_stats, 'gps_valid') else False
            gps_sats = status.gps_stats.gps_sats if hasattr(status.gps_stats, 'gps_sats') else 0
        
        # Get SNR status
        snr_above_noise = status.is_snr_above_noise_floor if hasattr(status, 'is_snr_above_noise_floor') else False
        
        # Get Ethernet speed
        eth_speed = status.eth_speed_mbps if hasattr(status, 'eth_speed_mbps') else 0
        
        # Calculate quality score
        quality_score = calculate_quality_score(latency, downlink_mbps, uplink_mbps, fraction_obstructed, snr_above_noise)
        
        return f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Starlink Speed Monitor</title>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
            <style>
                body {{
                    font-family: 'Segoe UI', Tahoma, Arial, sans-serif; 
                    margin: 0; 
                    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                    color: #333;
                }}
                .container {{
                    max-width: 1200px; 
                    margin: 20px auto; 
                    background: white; 
                    padding: 30px; 
                    border-radius: 15px; 
                    box-shadow: 0 10px 40px rgba(0,0,0,0.2);
                }}
                h1 {{
                    color: #333; 
                    margin-bottom: 30px; 
                    display: flex; 
                    align-items: center;
                }}
                .emoji {{ margin-right: 10px; font-size: 1.2em; }}
                
                /* Stats Grid */
                .stats-grid {{ 
                    display: grid; 
                    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); 
                    gap: 20px; 
                    margin-bottom: 30px;
                }}
                .stat {{
                    padding: 15px; 
                    background: linear-gradient(135deg, #f5f7fa 0%, #c3cfe2 100%); 
                    border-radius: 10px;
                    text-align: center;
                }}
                .stat-label {{ font-size: 0.9em; color: #666; margin-bottom: 5px; }}
                .stat-value {{ font-size: 1.8em; font-weight: bold; color: #333; }}
                .stat-unit {{ font-size: 0.7em; color: #666; margin-left: 5px; }}
                .stat-note {{ font-size: 0.7em; color: #999; margin-top: 5px; }}
                
                /* Quality Score Styling */
                .quality-excellent {{ color: #10b981; }}
                .quality-good {{ color: #3b82f6; }}
                .quality-fair {{ color: #f59e0b; }}
                .quality-poor {{ color: #ef4444; }}
                
                /* Charts Grid */
                .charts-grid {{
                    display: grid;
                    grid-template-columns: 1fr 1fr;
                    gap: 20px;
                    margin-bottom: 30px;
                }}
                .chart-container {{
                    background: #f8f9fa;
                    padding: 20px;
                    border-radius: 10px;
                    box-shadow: 0 2px 10px rgba(0,0,0,0.1);
                }}
                .chart-title {{
                    font-size: 1.1em;
                    font-weight: bold;
                    color: #333;
                    margin-bottom: 15px;
                    text-align: center;
                }}
                
                /* Responsive */
                @media (max-width: 768px) {{
                    .charts-grid {{
                        grid-template-columns: 1fr;
                    }}
                    .container {{
                        margin: 10px;
                        padding: 20px;
                    }}
                }}
                
                /* Status indicators */
                .status-indicator {{
                    display: inline-block; 
                    width: 12px; 
                    height: 12px; 
                    border-radius: 50%; 
                    margin-right: 8px; 
                    animation: pulse 2s infinite;
                }}
                .status-connected {{ background: #10b981; }}
                .status-disconnected {{ background: #ef4444; }}
                @keyframes pulse {{ 0% {{ opacity: 1; }} 50% {{ opacity: 0.5; }} 100% {{ opacity: 1; }} }}
                
                .good {{ color: #10b981; }}
                .warning {{ color: #f59e0b; }}
                .bad {{ color: #ef4444; }}
                
                .info-section {{
                    margin-top: 30px; 
                    padding: 15px; 
                    background: #f8f9fa; 
                    border-radius: 10px;
                    font-size: 0.9em;
                }}
                .info-item {{
                    display: flex; 
                    justify-content: space-between; 
                    padding: 5px 0; 
                    border-bottom: 1px solid #e0e0e0;
                }}
                .info-item:last-child {{ border-bottom: none; }}
                .timestamp {{
                    text-align: center; 
                    color: #666; 
                    font-size: 0.9em; 
                    margin-top: 20px;
                }}
                
                .success-notice {{ 
                    background: #d1fae5; 
                    border: 1px solid #10b981; 
                    border-radius: 8px; 
                    padding: 12px; 
                    margin-bottom: 20px; 
                    color: #065f46; 
                }}
                .info-notice {{ 
                    background: #fef3c7; 
                    border: 1px solid #fbbf24; 
                    border-radius: 8px; 
                    padding: 12px; 
                    margin-bottom: 20px; 
                    color: #92400e; 
                }}
                .notice-icon {{ display: inline-block; margin-right: 8px; }}
                .speed-test-btn {{ 
                    background: #667eea; 
                    color: white; 
                    padding: 10px 20px; 
                    border-radius: 5px; 
                    text-decoration: none; 
                    display: inline-block; 
                    margin-top: 10px; 
                }}
            </style>
        </head>
        <body>
        <div class="container">
            <h1>
                <span class="emoji">🛰️</span>
                Starlink Speed Monitor - Interactive Dashboard
                <span style="margin-left: auto;">
                    <span class="status-indicator {'status-connected' if state == 'CONNECTED' else 'status-disconnected'}"></span>
                    <span style="font-size: 0.5em; color: #666;">{state}</span>
                </span>
            </h1>
            
            {f'<div class="success-notice"><span class="notice-icon">✅</span><strong>Real Speed Data:</strong> Showing actual measured speeds from recent high-usage periods!</div>' if has_real_data else ''}
            
            {f'<div class="info-notice"><span class="notice-icon">ℹ️</span><strong>Low Activity Detected:</strong> The speeds shown reflect current low usage. <a href="/speedtest" class="speed-test-btn">Run Speed Test →</a></div>' if not has_real_data and downlink_mbps < 5 else ''}
            
            <!-- Stats Grid -->
            <div class="stats-grid">
                <div class="stat">
                    <div class="stat-label">Download Speed</div>
                    <div class="stat-value {'good' if downlink_mbps > 50 else 'warning' if downlink_mbps > 20 else 'bad'}">
                        {downlink_mbps:.1f}<span class="stat-unit">Mbps</span>
                    </div>
                    <div class="stat-note">{speed_note}</div>
                </div>
                
                <div class="stat">
                    <div class="stat-label">Upload Speed</div>
                    <div class="stat-value {'good' if uplink_mbps > 10 else 'warning' if uplink_mbps > 5 else 'bad'}">
                        {uplink_mbps:.1f}<span class="stat-unit">Mbps</span>
                    </div>
                    <div class="stat-note">{speed_note}</div>
                </div>
                
                <div class="stat">
                    <div class="stat-label">Latency</div>
                    <div class="stat-value {'good' if latency < 50 else 'warning' if latency < 100 else 'bad'}">
                        {latency:.0f}<span class="stat-unit">ms</span>
                    </div>
                    <div class="stat-note">To Starlink POP</div>
                </div>
                
                <div class="stat">
                    <div class="stat-label">Connection Quality</div>
                    <div class="stat-value {'quality-excellent' if quality_score >= 90 else 'quality-good' if quality_score >= 75 else 'quality-fair' if quality_score >= 50 else 'quality-poor'}">
                        {quality_score:.0f}<span class="stat-unit">%</span>
                    </div>
                    <div class="stat-note">Overall Score</div>
                </div>
                
                <div class="stat">
                    <div class="stat-label">Obstruction</div>
                    <div class="stat-value {'good' if fraction_obstructed < 1 else 'warning' if fraction_obstructed < 5 else 'bad'}">
                        {fraction_obstructed:.1f}<span class="stat-unit">%</span>
                    </div>
                    <div class="stat-note">Sky blocked</div>
                </div>
            </div>
            
            <!-- Charts Grid -->
            <div class="charts-grid">
                <div class="chart-container">
                    <div class="chart-title">📊 Network Latency (Last 5 Minutes)</div>
                    <canvas id="latencyChart" width="400" height="200"></canvas>
                </div>
                
                <div class="chart-container">
                    <div class="chart-title">⚡ Speed Trends (Mbps)</div>
                    <canvas id="speedChart" width="400" height="200"></canvas>
                </div>
                
                <div class="chart-container">
                    <div class="chart-title">🎯 Connection Quality Score</div>
                    <canvas id="qualityChart" width="400" height="200"></canvas>
                </div>
                
                <div class="chart-container">
                    <div class="chart-title">🚧 Obstruction Levels (%)</div>
                    <canvas id="obstructionChart" width="400" height="200"></canvas>
                </div>
            </div>
            
            <div class="info-section">
                <div class="info-item">
                    <span>Connection Uptime</span>
                    <strong>{uptime_str}</strong>
                </div>
                <div class="info-item">
                    <span>Ethernet Link Speed</span>
                    <strong>{eth_speed} Mbps</strong>
                </div>
                <div class="info-item">
                    <span>SNR Above Noise Floor</span>
                    <strong>{'✅ Yes' if snr_above_noise else '❌ No'}</strong>
                </div>
                <div class="info-item">
                    <span>GPS Status</span>
                    <strong>{'✅ Valid' if gps_valid else '❌ Invalid'} ({gps_sats} satellites)</strong>
                </div>
                <div class="info-item">
                    <span>Hardware Version</span>
                    <strong>{hardware_version}</strong>
                </div>
                <div class="info-item">
                    <span>Software Version</span>
                    <strong>{software_version}</strong>
                </div>
            </div>
            
            <div class="timestamp">Last updated: {time.strftime('%Y-%m-%d %H:%M:%S')}</div>
        </div>

        <script>
        // Chart configurations
        const chartConfig = {{
            responsive: true,
            maintainAspectRatio: true,
            scales: {{
                y: {{
                    beginAtZero: true,
                    grid: {{ color: 'rgba(0,0,0,0.1)' }},
                    ticks: {{ font: {{ size: 10 }} }}
                }},
                x: {{
                    grid: {{ color: 'rgba(0,0,0,0.1)' }},
                    ticks: {{ 
                        font: {{ size: 10 }},
                        maxTicksLimit: 10
                    }}
                }}
            }},
            plugins: {{
                legend: {{ 
                    display: true,
                    position: 'top',
                    labels: {{ font: {{ size: 11 }} }}
                }}
            }},
            animation: {{ duration: 0 }}
        }};

        // Initialize charts
        const latencyCtx = document.getElementById('latencyChart').getContext('2d');
        const speedCtx = document.getElementById('speedChart').getContext('2d');
        const qualityCtx = document.getElementById('qualityChart').getContext('2d');
        const obstructionCtx = document.getElementById('obstructionChart').getContext('2d');

        const latencyChart = new Chart(latencyCtx, {{
            type: 'line',
            data: {{
                labels: [],
                datasets: [{{
                    label: 'Latency (ms)',
                    data: [],
                    borderColor: 'rgb(99, 132, 255)',
                    backgroundColor: 'rgba(99, 132, 255, 0.2)',
                    tension: 0.4,
                    fill: true
                }}]
            }},
            options: {{
                ...chartConfig,
                scales: {{
                    ...chartConfig.scales,
                    y: {{
                        ...chartConfig.scales.y,
                        suggestedMax: 100
                    }}
                }}
            }}
        }});

        const speedChart = new Chart(speedCtx, {{
            type: 'line',
            data: {{
                labels: [],
                datasets: [{{
                    label: 'Download (Mbps)',
                    data: [],
                    borderColor: 'rgb(34, 197, 94)',
                    backgroundColor: 'rgba(34, 197, 94, 0.1)',
                    tension: 0.4
                }}, {{
                    label: 'Upload (Mbps)',
                    data: [],
                    borderColor: 'rgb(239, 68, 68)',
                    backgroundColor: 'rgba(239, 68, 68, 0.1)',
                    tension: 0.4
                }}]
            }},
            options: chartConfig
        }});

        const qualityChart = new Chart(qualityCtx, {{
            type: 'line',
            data: {{
                labels: [],
                datasets: [{{
                    label: 'Quality Score (%)',
                    data: [],
                    borderColor: 'rgb(168, 85, 247)',
                    backgroundColor: 'rgba(168, 85, 247, 0.2)',
                    tension: 0.4,
                    fill: true
                }}]
            }},
            options: {{
                ...chartConfig,
                scales: {{
                    ...chartConfig.scales,
                    y: {{
                        ...chartConfig.scales.y,
                        min: 0,
                        max: 100
                    }}
                }}
            }}
        }});

        const obstructionChart = new Chart(obstructionCtx, {{
            type: 'bar',
            data: {{
                labels: [],
                datasets: [{{
                    label: 'Obstruction (%)',
                    data: [],
                    backgroundColor: 'rgba(245, 158, 11, 0.8)',
                    borderColor: 'rgb(245, 158, 11)',
                    borderWidth: 1
                }}]
            }},
            options: {{
                ...chartConfig,
                scales: {{
                    ...chartConfig.scales,
                    y: {{
                        ...chartConfig.scales.y,
                        suggestedMax: 10
                    }}
                }}
            }}
        }});

        // Function to update charts
        function updateCharts() {{
            fetch('/api/current-stats')
                .then(response => response.json())
                .then(data => {{
                    if (data.error) {{
                        console.error('API Error:', data.error);
                        return;
                    }}
                    
                    // Add new data point
                    const timestamp = data.timestamp;
                    
                    // Update latency chart
                    latencyChart.data.labels.push(timestamp);
                    latencyChart.data.datasets[0].data.push(data.latency);
                    
                    // Update speed chart
                    speedChart.data.labels.push(timestamp);
                    speedChart.data.datasets[0].data.push(data.download_mbps);
                    speedChart.data.datasets[1].data.push(data.upload_mbps);
                    
                    // Update quality chart
                    qualityChart.data.labels.push(timestamp);
                    qualityChart.data.datasets[0].data.push(data.quality_score);
                    
                    // Update obstruction chart
                    obstructionChart.data.labels.push(timestamp);
                    obstructionChart.data.datasets[0].data.push(data.obstruction_pct);
                    
                    // Keep only last 50 data points
                    const maxPoints = 50;
                    [latencyChart, speedChart, qualityChart, obstructionChart].forEach(chart => {{
                        if (chart.data.labels.length > maxPoints) {{
                            chart.data.labels.shift();
                            chart.data.datasets.forEach(dataset => dataset.data.shift());
                        }}
                        chart.update('none'); // Update without animation
                    }});
                }})
                .catch(error => console.error('Error updating charts:', error));
        }}

        // Initial chart update
        updateCharts();
        
        // Update charts every 5 seconds
        setInterval(updateCharts, 5000);
        
        // Refresh page every 5 minutes to prevent memory leaks
        setTimeout(function(){{ location.reload(); }}, 300000);
        </script>
        </body>
        </html>
        """
    except Exception as e:
        return f"""
        <html>
        <head><title>Starlink Monitor - Error</title></head>
        <body style="font-family: Arial, sans-serif; margin: 40px;">
        <div style="background: white; padding: 30px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1);">
            <h1 style="color: #ef4444;">⚠️ Starlink Monitor - Connection Error</h1>
            <p style="color: #666;">Unable to connect to Starlink dish</p>
            <div style="background: #fee; padding: 15px; border-radius: 5px; margin: 20px 0;">
                <strong>Error details:</strong><br>
                <code style="color: #c00;">{str(e)}</code>
            </div>
            <p style="color: #666;">Please ensure:</p>
            <ul style="color: #666;">
                <li>Your Starlink dish is powered on and connected</li>
                <li>The dish is accessible at IP address 192.168.100.1</li>
                <li>Your network configuration allows access to the dish</li>
            </ul>
            <p style="color: #999; font-size: 0.9em;">This page will automatically retry in 30 seconds...</p>
        </div>
        <script>
        setTimeout(function(){{ location.reload(); }}, 30000);
        </script>
        </body>
        </html>
        """

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)