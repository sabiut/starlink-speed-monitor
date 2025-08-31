from flask import Flask
import sys
import os
import time
import statistics

# Add temp3 directory to path to use the working implementation
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), 'temp3'))
import starlink_grpc

app = Flask(__name__)

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
                # Filter for speeds above 1 Mbps to find actual usage periods
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
        
        return f"""
        <html>
        <head>
            <title>Starlink Monitor</title>
            <style>
                body {{ font-family: 'Segoe UI', Tahoma, Arial, sans-serif; margin: 0; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); }}
                .container {{ max-width: 900px; margin: 40px auto; background: white; padding: 30px; border-radius: 15px; box-shadow: 0 10px 40px rgba(0,0,0,0.2); }}
                h1 {{ color: #333; margin-bottom: 30px; display: flex; align-items: center; }}
                .emoji {{ margin-right: 10px; font-size: 1.2em; }}
                .stats-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 20px; }}
                .stat {{ padding: 15px; background: linear-gradient(135deg, #f5f7fa 0%, #c3cfe2 100%); border-radius: 10px; }}
                .stat-label {{ font-size: 0.9em; color: #666; margin-bottom: 5px; text-transform: uppercase; letter-spacing: 1px; }}
                .stat-value {{ font-size: 1.8em; font-weight: bold; color: #333; }}
                .stat-unit {{ font-size: 0.7em; color: #666; margin-left: 5px; }}
                .stat-note {{ font-size: 0.7em; color: #999; margin-top: 5px; }}
                .stat-peak {{ font-size: 0.8em; color: #666; margin-top: 3px; }}
                .good {{ color: #10b981; }}
                .warning {{ color: #f59e0b; }}
                .bad {{ color: #ef4444; }}
                .info-section {{ margin-top: 30px; padding: 15px; background: #f8f9fa; border-radius: 10px; }}
                .info-item {{ display: flex; justify-content: space-between; padding: 5px 0; border-bottom: 1px solid #e0e0e0; }}
                .info-item:last-child {{ border-bottom: none; }}
                .timestamp {{ text-align: center; color: #666; font-size: 0.9em; margin-top: 20px; }}
                .status-indicator {{ display: inline-block; width: 12px; height: 12px; border-radius: 50%; margin-right: 8px; animation: pulse 2s infinite; }}
                .status-connected {{ background: #10b981; }}
                .status-disconnected {{ background: #ef4444; }}
                .success-notice {{ background: #d1fae5; border: 1px solid #10b981; border-radius: 8px; padding: 12px; margin-bottom: 20px; color: #065f46; }}
                .info-notice {{ background: #fef3c7; border: 1px solid #fbbf24; border-radius: 8px; padding: 12px; margin-bottom: 20px; color: #92400e; }}
                .notice-icon {{ display: inline-block; margin-right: 8px; }}
                .speed-test-btn {{ background: #667eea; color: white; padding: 10px 20px; border-radius: 5px; text-decoration: none; display: inline-block; margin-top: 10px; }}
                @keyframes pulse {{ 0% {{ opacity: 1; }} 50% {{ opacity: 0.5; }} 100% {{ opacity: 1; }} }}
            </style>
        </head>
        <body>
        <div class="container">
            <h1>
                <span class="emoji">🛰️</span>
                Starlink Speed Monitor
                <span style="margin-left: auto;">
                    <span class="status-indicator {'status-connected' if state == 'CONNECTED' else 'status-disconnected'}"></span>
                    <span style="font-size: 0.5em; color: #666;">{state}</span>
                </span>
            </h1>
            
            {f'<div class="success-notice"><span class="notice-icon">✅</span><strong>Real Speed Data:</strong> Showing actual measured speeds from recent high-usage periods!</div>' if has_real_data else ''}
            
            {f'<div class="info-notice"><span class="notice-icon">ℹ️</span><strong>Low Activity Detected:</strong> The speeds shown reflect current low usage. <a href="/speedtest" class="speed-test-btn">Run Speed Test →</a></div>' if not has_real_data and downlink_mbps < 5 else ''}
            
            <div class="stats-grid">
                <div class="stat">
                    <div class="stat-label">Download Speed</div>
                    <div class="stat-value {'good' if downlink_mbps > 50 else 'warning' if downlink_mbps > 20 else 'bad'}">
                        {downlink_mbps:.1f}<span class="stat-unit">Mbps</span>
                    </div>
                    <div class="stat-note">{speed_note}</div>
                    {f'<div class="stat-peak">Peak: {peak_down:.1f} Mbps</div>' if peak_down > downlink_mbps else ''}
                </div>
                
                <div class="stat">
                    <div class="stat-label">Upload Speed</div>
                    <div class="stat-value {'good' if uplink_mbps > 10 else 'warning' if uplink_mbps > 5 else 'bad'}">
                        {uplink_mbps:.1f}<span class="stat-unit">Mbps</span>
                    </div>
                    <div class="stat-note">{speed_note}</div>
                    {f'<div class="stat-peak">Peak: {peak_up:.1f} Mbps</div>' if peak_up > uplink_mbps else ''}
                </div>
                
                <div class="stat">
                    <div class="stat-label">Network Latency</div>
                    <div class="stat-value {'good' if latency < 50 else 'warning' if latency < 100 else 'bad'}">
                        {latency:.0f}<span class="stat-unit">ms</span>
                    </div>
                    <div class="stat-note">To Starlink POP</div>
                </div>
                
                <div class="stat">
                    <div class="stat-label">Obstruction</div>
                    <div class="stat-value {'good' if fraction_obstructed < 1 else 'warning' if fraction_obstructed < 5 else 'bad'}">
                        {fraction_obstructed:.1f}<span class="stat-unit">%</span>
                    </div>
                    <div class="stat-note">Sky visibility blocked</div>
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
        setTimeout(function(){{ location.reload(); }}, 10000);
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