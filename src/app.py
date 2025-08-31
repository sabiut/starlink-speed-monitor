from flask import Flask, jsonify, request
import sys
import os
import time
import statistics
import json
from datetime import datetime, timedelta
from collections import deque
import atexit

# Add temp3 directory to path to use the working implementation
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), 'temp3'))
import starlink_grpc

# Import our new modules
from database import StarlinkDatabase
from data_collector import DataCollector
from speed_test import SpeedTestEngine

app = Flask(__name__)

# Initialize database and data collector
db = StarlinkDatabase()
collector = DataCollector(db, collection_interval=30)  # Collect every 30 seconds
speed_test_engine = SpeedTestEngine(db)

# Start background data collection
collector.start()
speed_test_engine.start_scheduler()

# Ensure collector stops when app shuts down
atexit.register(collector.stop)
atexit.register(speed_test_engine.stop_scheduler)

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

# Health check endpoint
@app.route('/health')
def health():
    """Health check endpoint for Docker"""
    try:
        # Quick database check
        stats = db.get_summary_stats(days=1)
        collector_status = collector.get_status()
        
        return jsonify({
            'status': 'healthy',
            'database': 'connected',
            'collector': 'running' if collector_status['running'] else 'stopped',
            'measurements_today': stats.get('total_measurements', 0)
        }), 200
    except Exception as e:
        return jsonify({
            'status': 'unhealthy',
            'error': str(e)
        }), 500

# Historical Data API Endpoints
@app.route('/api/historical/summary')
def historical_summary():
    """Get summary statistics for different time periods"""
    try:
        days = int(request.args.get('days', 30))
        stats = db.get_summary_stats(days=days)
        
        return jsonify({
            'period_days': days,
            'total_measurements': stats.get('total_measurements', 0),
            'avg_latency_ms': round(stats.get('avg_latency', 0), 1),
            'avg_download_mbps': round(stats.get('avg_download', 0), 1),
            'avg_upload_mbps': round(stats.get('avg_upload', 0), 1),
            'avg_quality_score': round(stats.get('avg_quality', 0), 0),
            'outage_count': stats.get('outage_count', 0),
            'total_outage_hours': round(stats.get('total_outage_seconds', 0) / 3600, 1),
            'oldest_data': stats.get('oldest_data'),
            'newest_data': stats.get('newest_data')
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/historical/trends')
def historical_trends():
    """Get trend data for charts"""
    try:
        metric = request.args.get('metric', 'download_mbps')
        period = request.args.get('period', 'hour')  # hour, day, minute
        days = int(request.args.get('days', 7))
        
        # Validate metric
        valid_metrics = ['download_mbps', 'upload_mbps', 'latency_ms', 'quality_score', 'obstruction_pct']
        if metric not in valid_metrics:
            return jsonify({'error': f'Invalid metric. Must be one of: {valid_metrics}'}), 400
        
        trends = db.get_trend_data(metric, period, days)
        
        return jsonify({
            'metric': metric,
            'period': period,
            'days': days,
            'data': trends
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/historical/daily')
def daily_stats():
    """Get daily statistics"""
    try:
        days = int(request.args.get('days', 30))
        stats = db.get_daily_stats(days=days)
        
        return jsonify({
            'period_days': days,
            'daily_stats': stats
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/historical/comparison')
def performance_comparison():
    """Get performance comparison data"""
    try:
        metric = request.args.get('metric', 'download_mbps')
        comparison = db.get_performance_comparison(metric)
        
        return jsonify(comparison)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/historical/outages')
def outages():
    """Get outage history"""
    try:
        days = int(request.args.get('days', 30))
        outage_list = db.get_outages(days=days)
        
        return jsonify({
            'period_days': days,
            'outages': outage_list
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

def calculate_performance_grade(latency, download, upload, quality_score):
    """Calculate performance grade A-F based on metrics"""
    if quality_score >= 90 and latency < 30 and download > 50:
        return 'A'
    elif quality_score >= 80 and latency < 50 and download > 25:
        return 'B'
    elif quality_score >= 70 and latency < 75 and download > 15:
        return 'C'
    elif quality_score >= 60 and latency < 100 and download > 10:
        return 'D'
    else:
        return 'F'

def calculate_week_summary(week_stats, week_start):
    """Calculate weekly summary from daily stats"""
    if not week_stats:
        return {}
    
    total_days = len(week_stats)
    avg_latency = sum(s.get('avg_latency_ms', 0) for s in week_stats) / total_days
    avg_download = sum(s.get('avg_download_mbps', 0) for s in week_stats) / total_days
    avg_upload = sum(s.get('avg_upload_mbps', 0) for s in week_stats) / total_days
    avg_quality = sum(s.get('avg_quality_score', 0) for s in week_stats) / total_days
    total_outages = sum(s.get('outage_count', 0) for s in week_stats)
    total_outage_time = sum(s.get('total_outage_minutes', 0) for s in week_stats)
    
    return {
        'week_start': week_start.isoformat(),
        'week_end': (week_start + timedelta(days=6)).isoformat(),
        'days_with_data': total_days,
        'avg_latency_ms': round(avg_latency, 1),
        'avg_download_mbps': round(avg_download, 1),
        'avg_upload_mbps': round(avg_upload, 1),
        'avg_quality_score': round(avg_quality, 1),
        'total_outages': total_outages,
        'total_outage_minutes': total_outage_time,
        'performance_grade': calculate_performance_grade(avg_latency, avg_download, avg_upload, avg_quality)
    }

def calculate_month_summary(month_stats, month_key):
    """Calculate monthly summary from daily stats"""
    if not month_stats:
        return {}
    
    total_days = len(month_stats)
    avg_latency = sum(s.get('avg_latency_ms', 0) for s in month_stats) / total_days
    avg_download = sum(s.get('avg_download_mbps', 0) for s in month_stats) / total_days
    avg_upload = sum(s.get('avg_upload_mbps', 0) for s in month_stats) / total_days
    avg_quality = sum(s.get('avg_quality_score', 0) for s in month_stats) / total_days
    total_outages = sum(s.get('outage_count', 0) for s in month_stats)
    total_outage_time = sum(s.get('total_outage_minutes', 0) for s in month_stats)
    
    return {
        'month': month_key,
        'days_with_data': total_days,
        'avg_latency_ms': round(avg_latency, 1),
        'avg_download_mbps': round(avg_download, 1),
        'avg_upload_mbps': round(avg_upload, 1),
        'avg_quality_score': round(avg_quality, 1),
        'total_outages': total_outages,
        'total_outage_minutes': total_outage_time,
        'performance_grade': calculate_performance_grade(avg_latency, avg_download, avg_upload, avg_quality)
    }

@app.route('/api/historical/reports')
def get_reports():
    """Get comprehensive daily/weekly/monthly reports"""
    try:
        report_type = request.args.get('type', 'daily')  # daily, weekly, monthly
        days = int(request.args.get('days', 30))
        
        if report_type == 'daily':
            # Get daily statistics for the past N days
            daily_stats = db.get_daily_stats(days)
            
            # Calculate performance grades
            for stat in daily_stats:
                grade = calculate_performance_grade(
                    stat.get('avg_latency_ms', 0),
                    stat.get('avg_download_mbps', 0),
                    stat.get('avg_upload_mbps', 0),
                    stat.get('avg_quality_score', 0)
                )
                stat['performance_grade'] = grade
            
            return jsonify({
                'success': True,
                'report_type': 'daily',
                'data': daily_stats
            })
            
        elif report_type == 'weekly':
            # Group daily stats by week
            daily_stats = db.get_daily_stats(days)
            weekly_reports = []
            
            current_week = []
            current_week_start = None
            
            for stat in daily_stats:
                stat_date = datetime.strptime(stat['date'], '%Y-%m-%d').date()
                week_start = stat_date - timedelta(days=stat_date.weekday())
                
                if current_week_start != week_start:
                    if current_week:
                        weekly_reports.append(calculate_week_summary(current_week, current_week_start))
                    current_week = [stat]
                    current_week_start = week_start
                else:
                    current_week.append(stat)
            
            if current_week:
                weekly_reports.append(calculate_week_summary(current_week, current_week_start))
            
            return jsonify({
                'success': True,
                'report_type': 'weekly',
                'data': weekly_reports
            })
            
        elif report_type == 'monthly':
            # Group daily stats by month
            daily_stats = db.get_daily_stats(days)
            monthly_reports = {}
            
            for stat in daily_stats:
                stat_date = datetime.strptime(stat['date'], '%Y-%m-%d').date()
                month_key = stat_date.strftime('%Y-%m')
                
                if month_key not in monthly_reports:
                    monthly_reports[month_key] = []
                monthly_reports[month_key].append(stat)
            
            # Calculate monthly summaries
            monthly_data = []
            for month, stats in monthly_reports.items():
                monthly_data.append(calculate_month_summary(stats, month))
            
            return jsonify({
                'success': True,
                'report_type': 'monthly',
                'data': sorted(monthly_data, key=lambda x: x['month'], reverse=True)
            })
        
        else:
            return jsonify({'success': False, 'error': 'Invalid report type'}), 400
            
    except Exception as e:
        app.logger.error(f"Error getting reports: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/historical/analytics')
def get_advanced_analytics():
    """Get advanced performance analytics"""
    try:
        days = int(request.args.get('days', 30))
        analytics = db.get_advanced_analytics(days)
        return jsonify({
            'success': True,
            'data': analytics
        })
    except Exception as e:
        app.logger.error(f"Error getting advanced analytics: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/historical/insights')
def get_performance_insights():
    """Get actionable performance insights"""
    try:
        days = int(request.args.get('days', 30))
        insights = db.get_performance_insights(days)
        return jsonify({
            'success': True,
            'data': insights
        })
    except Exception as e:
        app.logger.error(f"Error getting performance insights: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/collector/status')
def collector_status():
    """Get data collector status"""
    return jsonify(collector.get_status())

# Speed Test API Endpoints
@app.route('/api/speedtest/run', methods=['POST'])
def run_speed_test():
    """Start a new speed test"""
    try:
        test_type = request.json.get('test_type', 'manual') if request.is_json else 'manual'
        
        # Run speed test in background thread
        import threading
        result = {'status': 'starting', 'message': 'Speed test initiated'}
        
        def run_test():
            try:
                speed_test_engine.run_server_speed_test(test_type=test_type)
            except Exception as e:
                app.logger.error(f"Speed test error: {e}")
        
        test_thread = threading.Thread(target=run_test, daemon=True)
        test_thread.start()
        
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/speedtest/history')
def get_speed_test_history():
    """Get speed test history"""
    try:
        days = int(request.args.get('days', 30))
        test_type = request.args.get('type')
        limit = int(request.args.get('limit', 50))
        
        tests = db.get_speed_tests(days=days, test_type=test_type, limit=limit)
        
        return jsonify({
            'success': True,
            'data': tests,
            'period_days': days
        })
    except Exception as e:
        app.logger.error(f"Error getting speed test history: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/speedtest/summary')
def get_speed_test_summary():
    """Get speed test summary statistics"""
    try:
        days = int(request.args.get('days', 30))
        summary = db.get_speed_test_summary(days=days)
        
        return jsonify({
            'success': True,
            'data': summary,
            'period_days': days
        })
    except Exception as e:
        app.logger.error(f"Error getting speed test summary: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/speedtest/trends')
def get_speed_test_trends():
    """Get speed test trends for charts"""
    try:
        days = int(request.args.get('days', 30))
        trends = db.get_speed_test_trends(days=days)
        
        return jsonify({
            'success': True,
            'data': trends,
            'period_days': days
        })
    except Exception as e:
        app.logger.error(f"Error getting speed test trends: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/speedtest/schedules')
def get_speed_test_schedules():
    """Get speed test schedules"""
    try:
        schedules = db.get_speed_test_schedules()
        return jsonify({
            'success': True,
            'data': schedules
        })
    except Exception as e:
        app.logger.error(f"Error getting schedules: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/speedtest/schedules', methods=['POST'])
def create_speed_test_schedule():
    """Create a new speed test schedule"""
    try:
        data = request.get_json()
        name = data.get('name')
        cron_expression = data.get('cron_expression')
        enabled = data.get('enabled', True)
        
        if not name or not cron_expression:
            return jsonify({'success': False, 'error': 'Name and cron expression required'}), 400
        
        # Validate cron expression
        from croniter import croniter
        if not croniter.is_valid(cron_expression):
            return jsonify({'success': False, 'error': 'Invalid cron expression'}), 400
        
        schedule_id = db.create_speed_test_schedule(name, cron_expression, enabled)
        
        return jsonify({
            'success': True,
            'data': {'id': schedule_id, 'message': 'Schedule created successfully'}
        })
    except Exception as e:
        app.logger.error(f"Error creating schedule: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

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
def speedtest_interface():
    """Speed test interface with built-in testing"""
    try:
        # Get recent speed test history
        recent_tests = db.get_speed_tests(days=7, limit=10) or []
        summary = db.get_speed_test_summary(days=30) or {}
        
        # Ensure all summary values are numbers
        summary = {
            'total_tests': summary.get('total_tests') or 0,
            'avg_download': summary.get('avg_download') or 0,
            'avg_upload': summary.get('avg_upload') or 0,
            'avg_ping': summary.get('avg_ping') or 0
        }
        
        return f"""
        <html>
        <head>
            <title>🚀 Starlink Speed Test</title>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <style>
                body {{
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                    margin: 0;
                    padding: 20px;
                    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                    min-height: 100vh;
                }}
                .container {{
                    max-width: 1200px;
                    margin: 0 auto;
                    background: white;
                    border-radius: 15px;
                    box-shadow: 0 8px 32px rgba(0,0,0,0.1);
                    padding: 30px;
                }}
                .header {{
                    text-align: center;
                    margin-bottom: 40px;
                }}
                .header h1 {{
                    color: #333;
                    margin: 0;
                    font-size: 2.5em;
                }}
                .nav-links {{
                    text-align: center;
                    margin-bottom: 30px;
                }}
                .nav-links a {{
                    color: #667eea;
                    text-decoration: none;
                    margin: 0 15px;
                    font-weight: 500;
                }}
                .nav-links a:hover {{
                    text-decoration: underline;
                }}
                
                .test-section {{
                    background: #f8fafc;
                    border-radius: 10px;
                    padding: 30px;
                    margin-bottom: 30px;
                    text-align: center;
                }}
                .test-button {{
                    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                    color: white;
                    border: none;
                    padding: 15px 40px;
                    font-size: 1.2em;
                    border-radius: 50px;
                    cursor: pointer;
                    transition: transform 0.2s;
                    margin: 10px;
                }}
                .test-button:hover {{
                    transform: translateY(-2px);
                }}
                .test-button:disabled {{
                    background: #ccc;
                    cursor: not-allowed;
                    transform: none;
                }}
                
                .results-section {{
                    display: grid;
                    grid-template-columns: 1fr 1fr;
                    gap: 30px;
                    margin-bottom: 30px;
                }}
                .results-card {{
                    background: white;
                    border-radius: 10px;
                    padding: 20px;
                    box-shadow: 0 2px 10px rgba(0,0,0,0.1);
                }}
                .results-card h3 {{
                    margin: 0 0 20px 0;
                    color: #333;
                }}
                
                .stats-grid {{
                    display: grid;
                    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
                    gap: 20px;
                    margin-bottom: 30px;
                }}
                .stat-card {{
                    background: linear-gradient(135deg, #f5f7fa 0%, #c3cfe2 100%);
                    padding: 20px;
                    border-radius: 10px;
                    text-align: center;
                }}
                .stat-value {{
                    font-size: 1.8em;
                    font-weight: bold;
                    color: #333;
                    margin-bottom: 5px;
                }}
                .stat-label {{
                    font-size: 0.9em;
                    color: #666;
                }}
                
                .history-table {{
                    width: 100%;
                    border-collapse: collapse;
                    margin-top: 20px;
                }}
                .history-table th,
                .history-table td {{
                    padding: 12px;
                    text-align: left;
                    border-bottom: 1px solid #eee;
                }}
                .history-table th {{
                    background: #f8fafc;
                    font-weight: 600;
                }}
                
                .status-indicator {{
                    padding: 4px 12px;
                    border-radius: 12px;
                    font-size: 0.8em;
                    font-weight: 600;
                }}
                .status-completed {{ background: #d1fae5; color: #065f46; }}
                .status-running {{ background: #dbeafe; color: #1e40af; }}
                .status-failed {{ background: #fecaca; color: #991b1b; }}
                
                .loading {{
                    display: none;
                    text-align: center;
                    padding: 20px;
                    color: #666;
                }}
                
                @media (max-width: 768px) {{
                    .results-section {{
                        grid-template-columns: 1fr;
                    }}
                    .stats-grid {{
                        grid-template-columns: 1fr 1fr;
                    }}
                }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>🚀 Starlink Speed Test</h1>
                </div>
                
                <div class="nav-links">
                    <a href="/">← Back to Monitor</a>
                    <a href="/analytics">📊 Analytics Dashboard</a>
                    <a href="#schedules">⏰ Scheduled Tests</a>
                </div>
                
                <div class="test-section">
                    <h2>Run Speed Test</h2>
                    <p>Test your Starlink connection speed using our built-in speed test engine</p>
                    <button id="runTestBtn" class="test-button" onclick="runSpeedTest()">🚀 Start Speed Test</button>
                    <button class="test-button" onclick="window.open('https://fast.com', '_blank')">🌐 External Test (Fast.com)</button>
                    
                    <div id="testStatus" class="loading">
                        <p>⏳ Running speed test... This may take 1-2 minutes</p>
                        <div style="margin: 20px 0;">
                            <div style="background: #e5e7eb; border-radius: 10px; height: 6px; overflow: hidden;">
                                <div id="progressBar" style="background: #667eea; height: 100%; width: 0%; transition: width 0.5s;"></div>
                            </div>
                        </div>
                    </div>
                </div>
                
                <div class="stats-grid">
                    <div class="stat-card">
                        <div class="stat-value">{summary.get('total_tests', 0) or 0}</div>
                        <div class="stat-label">Total Tests (30 days)</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-value">{(summary.get('avg_download') or 0):.1f} Mbps</div>
                        <div class="stat-label">Avg Download Speed</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-value">{(summary.get('avg_upload') or 0):.1f} Mbps</div>
                        <div class="stat-label">Avg Upload Speed</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-value">{(summary.get('avg_ping') or 0):.0f} ms</div>
                        <div class="stat-label">Avg Latency</div>
                    </div>
                </div>
                
                <div class="results-section">
                    <div class="results-card">
                        <h3>📊 Recent Test Results</h3>
                        <table class="history-table">
                            <thead>
                                <tr>
                                    <th>Date</th>
                                    <th>Download</th>
                                    <th>Upload</th>
                                    <th>Ping</th>
                                    <th>Status</th>
                                </tr>
                            </thead>
                            <tbody id="historyTableBody">
                                {"".join([
                                    f'''<tr>
                                        <td>{str(test.get('timestamp', ''))[:16]}</td>
                                        <td>{(test.get('download_mbps') or 0):.1f} Mbps</td>
                                        <td>{(test.get('upload_mbps') or 0):.1f} Mbps</td>
                                        <td>{(test.get('ping_ms') or 0):.0f} ms</td>
                                        <td><span class="status-indicator status-{test.get('status', 'completed')}">{str(test.get('status', 'completed')).title()}</span></td>
                                    </tr>'''
                                    for test in recent_tests[:5]
                                ])}
                            </tbody>
                        </table>
                        <p style="text-align: center; margin-top: 15px;">
                            <a href="/analytics" style="color: #667eea;">View Full History →</a>
                        </p>
                    </div>
                    
                    <div class="results-card">
                        <h3>⏰ Speed Test Scheduling</h3>
                        <p>Set up automatic speed tests to track your connection over time.</p>
                        
                        <div style="margin: 20px 0;">
                            <label for="scheduleName" style="display: block; margin-bottom: 5px; font-weight: 600;">Schedule Name:</label>
                            <input type="text" id="scheduleName" placeholder="Daily Morning Test" style="width: 100%; padding: 8px; border: 1px solid #ddd; border-radius: 4px; margin-bottom: 10px;">
                        </div>
                        
                        <div style="margin: 20px 0;">
                            <label for="scheduleType" style="display: block; margin-bottom: 5px; font-weight: 600;">Frequency:</label>
                            <select id="scheduleType" style="width: 100%; padding: 8px; border: 1px solid #ddd; border-radius: 4px;">
                                <option value="0 9 * * *">Daily at 9 AM</option>
                                <option value="0 12 * * *">Daily at Noon</option>
                                <option value="0 21 * * *">Daily at 9 PM</option>
                                <option value="0 12 * * 0">Weekly (Sunday at Noon)</option>
                                <option value="0 */6 * * *">Every 6 Hours</option>
                            </select>
                        </div>
                        
                        <button class="test-button" onclick="createSchedule()" style="width: 100%; margin-top: 10px;">⏰ Create Schedule</button>
                        
                        <div id="scheduleStatus" style="margin-top: 15px; padding: 10px; border-radius: 5px; display: none;"></div>
                    </div>
                </div>
            </div>
            
            <script>
                let testRunning = false;
                
                async function runSpeedTest() {{
                    if (testRunning) return;
                    
                    testRunning = true;
                    const button = document.getElementById('runTestBtn');
                    const status = document.getElementById('testStatus');
                    const progressBar = document.getElementById('progressBar');
                    
                    button.disabled = true;
                    button.textContent = '⏳ Testing...';
                    status.style.display = 'block';
                    
                    // Simulate progress
                    let progress = 0;
                    const progressInterval = setInterval(() => {{
                        progress += Math.random() * 10;
                        if (progress > 90) progress = 90;
                        progressBar.style.width = progress + '%';
                    }}, 1000);
                    
                    try {{
                        const response = await fetch('/api/speedtest/run', {{
                            method: 'POST',
                            headers: {{'Content-Type': 'application/json'}},
                            body: JSON.stringify({{'test_type': 'manual'}})
                        }});
                        
                        const result = await response.json();
                        
                        if (result.status === 'starting') {{
                            // Wait for test to complete (check every 5 seconds)
                            let attempts = 0;
                            const checkInterval = setInterval(async () => {{
                                attempts++;
                                if (attempts > 24) {{ // Max 2 minutes
                                    clearInterval(checkInterval);
                                    clearInterval(progressInterval);
                                    showTestComplete('Timeout - test may still be running in background');
                                    return;
                                }}
                                
                                try {{
                                    const historyResponse = await fetch('/api/speedtest/history?limit=1');
                                    const historyData = await historyResponse.json();
                                    
                                    if (historyData.success && historyData.data.length > 0) {{
                                        const latestTest = historyData.data[0];
                                        const testTime = new Date(latestTest.timestamp);
                                        const now = new Date();
                                        
                                        // If latest test is less than 2 minutes old, it's probably our test
                                        if ((now - testTime) < 120000) {{
                                            clearInterval(checkInterval);
                                            clearInterval(progressInterval);
                                            progressBar.style.width = '100%';
                                            setTimeout(() => {{
                                                showTestComplete(latestTest);
                                                location.reload(); // Refresh to show new data
                                            }}, 1000);
                                        }}
                                    }}
                                }} catch (e) {{
                                    console.error('Error checking test status:', e);
                                }}
                            }}, 5000);
                        }}
                    }} catch (error) {{
                        clearInterval(progressInterval);
                        showTestComplete('Error: ' + error.message);
                    }}
                }}
                
                function showTestComplete(result) {{
                    testRunning = false;
                    const button = document.getElementById('runTestBtn');
                    const status = document.getElementById('testStatus');
                    
                    button.disabled = false;
                    button.textContent = '🚀 Start Speed Test';
                    
                    if (typeof result === 'object') {{
                        status.innerHTML = `
                            <h3>✅ Test Complete!</h3>
                            <p><strong>Download:</strong> ${{result.download_mbps.toFixed(1)}} Mbps</p>
                            <p><strong>Upload:</strong> ${{result.upload_mbps.toFixed(1)}} Mbps</p>
                            <p><strong>Ping:</strong> ${{result.ping_ms.toFixed(0)}} ms</p>
                        `;
                    }} else {{
                        status.innerHTML = `<h3>ℹ️ ${{result}}</h3>`;
                    }}
                    
                    setTimeout(() => {{
                        status.style.display = 'none';
                    }}, 10000);
                }}
                
                async function createSchedule() {{
                    const name = document.getElementById('scheduleName').value;
                    const cronExpression = document.getElementById('scheduleType').value;
                    const statusDiv = document.getElementById('scheduleStatus');
                    
                    if (!name.trim()) {{
                        statusDiv.style.display = 'block';
                        statusDiv.style.backgroundColor = '#fecaca';
                        statusDiv.style.color = '#991b1b';
                        statusDiv.textContent = 'Please enter a schedule name';
                        return;
                    }}
                    
                    try {{
                        const response = await fetch('/api/speedtest/schedules', {{
                            method: 'POST',
                            headers: {{'Content-Type': 'application/json'}},
                            body: JSON.stringify({{
                                name: name,
                                cron_expression: cronExpression,
                                enabled: true
                            }})
                        }});
                        
                        const result = await response.json();
                        
                        statusDiv.style.display = 'block';
                        if (result.success) {{
                            statusDiv.style.backgroundColor = '#d1fae5';
                            statusDiv.style.color = '#065f46';
                            statusDiv.textContent = 'Schedule created successfully!';
                            document.getElementById('scheduleName').value = '';
                        }} else {{
                            statusDiv.style.backgroundColor = '#fecaca';
                            statusDiv.style.color = '#991b1b';
                            statusDiv.textContent = 'Error: ' + result.error;
                        }}
                        
                        setTimeout(() => {{
                            statusDiv.style.display = 'none';
                        }}, 5000);
                        
                    }} catch (error) {{
                        statusDiv.style.display = 'block';
                        statusDiv.style.backgroundColor = '#fecaca';
                        statusDiv.style.color = '#991b1b';
                        statusDiv.textContent = 'Error creating schedule: ' + error.message;
                    }}
                }}
            </script>
        </body>
        </html>
        """
    except Exception as e:
        return f"""
        <html>
        <head><title>Speed Test Error</title></head>
        <body style="font-family: Arial, sans-serif; margin: 40px;">
        <div style="background: white; padding: 30px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1);">
            <h1 style="color: #ef4444;">⚠️ Speed Test Error</h1>
            <p style="color: #666;">Unable to load speed test interface</p>
            <div style="background: #fee; padding: 15px; border-radius: 5px; margin: 20px 0;">
                <strong>Error details:</strong><br>
                <code style="color: #c00;">{str(e)}</code>
            </div>
            <p><a href="/" style="color: #667eea;">← Back to Monitor</a></p>
        </div>
        </body>
        </html>
        """

@app.route('/analytics')
def analytics():
    """Analytics dashboard with historical data and trends"""
    try:
        # Get summary stats for the overview
        summary_7d = db.get_summary_stats(days=7)
        summary_30d = db.get_summary_stats(days=30)
        
        return f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Starlink Analytics Dashboard</title>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
            <script src="https://cdn.jsdelivr.net/npm/date-fns@2.29.3/index.min.js"></script>
            <style>
                body {{
                    font-family: 'Segoe UI', Tahoma, Arial, sans-serif; 
                    margin: 0; 
                    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                    color: #333;
                }}
                .container {{
                    max-width: 1400px; 
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
                h2 {{
                    color: #555;
                    border-bottom: 2px solid #eee;
                    padding-bottom: 10px;
                    margin: 30px 0 20px 0;
                }}
                .nav-links {{
                    margin-bottom: 30px;
                    text-align: center;
                }}
                .nav-links a {{
                    margin: 0 15px;
                    padding: 10px 20px;
                    background: #667eea;
                    color: white;
                    text-decoration: none;
                    border-radius: 5px;
                    display: inline-block;
                }}
                .nav-links a:hover {{
                    background: #5a67d8;
                }}
                
                /* Summary Cards */
                .summary-grid {{ 
                    display: grid; 
                    grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); 
                    gap: 20px; 
                    margin-bottom: 30px;
                }}
                .summary-card {{
                    padding: 20px; 
                    background: linear-gradient(135deg, #f5f7fa 0%, #c3cfe2 100%); 
                    border-radius: 10px;
                    text-align: center;
                }}
                .summary-title {{ font-size: 0.9em; color: #666; margin-bottom: 5px; }}
                .summary-value {{ font-size: 1.8em; font-weight: bold; color: #333; }}
                .summary-period {{ font-size: 0.7em; color: #999; margin-top: 5px; }}
                
                /* Chart Grid */
                .charts-grid {{
                    display: grid;
                    grid-template-columns: 1fr;
                    gap: 30px;
                    margin-bottom: 30px;
                }}
                .chart-row {{
                    display: grid;
                    grid-template-columns: 1fr 1fr;
                    gap: 20px;
                }}
                .chart-container {{
                    background: #f8f9fa;
                    padding: 20px;
                    border-radius: 10px;
                    box-shadow: 0 2px 10px rgba(0,0,0,0.1);
                }}
                .chart-title {{
                    font-size: 1.2em;
                    font-weight: bold;
                    color: #333;
                    margin-bottom: 15px;
                    text-align: center;
                }}
                .chart-controls {{
                    text-align: center;
                    margin-bottom: 15px;
                }}
                .chart-controls select {{
                    margin: 0 10px;
                    padding: 5px 10px;
                    border: 1px solid #ddd;
                    border-radius: 4px;
                }}
                
                /* Responsive */
                @media (max-width: 768px) {{
                    .chart-row {{
                        grid-template-columns: 1fr;
                    }}
                    .container {{
                        margin: 10px;
                        padding: 20px;
                    }}
                }}
                
                .good {{ color: #10b981; }}
                .warning {{ color: #f59e0b; }}
                .bad {{ color: #ef4444; }}
                .loading {{ text-align: center; padding: 40px; color: #666; }}
                
                /* Reports Section */
                .reports-section {{ margin-top: 30px; }}
                .report-controls {{
                    display: flex;
                    gap: 15px;
                    margin-bottom: 20px;
                    align-items: center;
                    flex-wrap: wrap;
                }}
                .report-controls select {{
                    padding: 8px 12px;
                    border: 1px solid #ddd;
                    border-radius: 4px;
                    font-size: 0.9em;
                }}
                .refresh-btn {{
                    padding: 8px 16px;
                    background: #667eea;
                    color: white;
                    border: none;
                    border-radius: 4px;
                    cursor: pointer;
                    font-size: 0.9em;
                }}
                .refresh-btn:hover {{ background: #5a67d8; }}
                
                .reports-container {{ margin-top: 20px; }}
                .report-table {{
                    width: 100%;
                    border-collapse: collapse;
                    background: white;
                    border-radius: 8px;
                    overflow: hidden;
                    box-shadow: 0 2px 8px rgba(0,0,0,0.1);
                }}
                .report-table th,
                .report-table td {{
                    padding: 12px;
                    text-align: left;
                    border-bottom: 1px solid #eee;
                }}
                .report-table th {{
                    background: #f8fafc;
                    font-weight: 600;
                    color: #374151;
                    font-size: 0.85em;
                    text-transform: uppercase;
                    letter-spacing: 0.5px;
                }}
                .report-table tr:hover {{
                    background: #f9fafb;
                }}
                .report-table tr:last-child td {{
                    border-bottom: none;
                }}
                .grade-badge {{
                    padding: 4px 8px;
                    border-radius: 12px;
                    font-size: 0.8em;
                    font-weight: 600;
                    display: inline-block;
                    min-width: 20px;
                    text-align: center;
                }}
                .grade-A {{ background: #d1fae5; color: #065f46; }}
                .grade-B {{ background: #dbeafe; color: #1e40af; }}
                .grade-C {{ background: #fef3c7; color: #92400e; }}
                .grade-D {{ background: #fed7aa; color: #9a3412; }}
                .grade-F {{ background: #fecaca; color: #991b1b; }}
                
                /* Advanced Analytics Section */
                .analytics-section {{ margin-top: 30px; }}
                .analytics-grid {{
                    display: grid;
                    grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
                    gap: 20px;
                    margin-bottom: 30px;
                }}
                .analytics-card {{
                    background: white;
                    padding: 20px;
                    border-radius: 10px;
                    box-shadow: 0 2px 8px rgba(0,0,0,0.1);
                    border-left: 4px solid #667eea;
                }}
                .analytics-card h3 {{
                    margin: 0 0 15px 0;
                    color: #374151;
                    font-size: 1.1em;
                }}
                .comparison-row {{
                    display: flex;
                    justify-content: space-between;
                    align-items: center;
                    padding: 10px 0;
                    border-bottom: 1px solid #f3f4f6;
                }}
                .comparison-row:last-child {{
                    border-bottom: none;
                }}
                .comparison-label {{
                    font-weight: 600;
                    color: #6b7280;
                }}
                .comparison-value {{
                    font-weight: bold;
                }}
                .metric-badge {{
                    padding: 4px 12px;
                    border-radius: 12px;
                    font-size: 0.85em;
                    font-weight: 600;
                }}
                .metric-excellent {{ background: #d1fae5; color: #065f46; }}
                .metric-good {{ background: #dbeafe; color: #1e40af; }}
                .metric-fair {{ background: #fef3c7; color: #92400e; }}
                .metric-poor {{ background: #fed7aa; color: #9a3412; }}
                .metric-bad {{ background: #fecaca; color: #991b1b; }}
                
                /* Performance Insights */
                .insights-section {{ margin-top: 30px; }}
                .insights-container {{ margin-top: 20px; }}
                .insight-item {{
                    background: white;
                    padding: 20px;
                    border-radius: 8px;
                    margin-bottom: 15px;
                    box-shadow: 0 2px 4px rgba(0,0,0,0.1);
                    border-left: 4px solid #6b7280;
                }}
                .insight-high {{ border-left-color: #ef4444; }}
                .insight-medium {{ border-left-color: #f59e0b; }}
                .insight-low {{ border-left-color: #10b981; }}
                .insight-title {{
                    font-weight: bold;
                    color: #374151;
                    margin-bottom: 5px;
                }}
                .insight-description {{
                    color: #6b7280;
                    margin-bottom: 10px;
                }}
                .insight-recommendation {{
                    background: #f9fafb;
                    padding: 10px;
                    border-radius: 4px;
                    font-size: 0.9em;
                    color: #374151;
                    font-style: italic;
                }}
            </style>
        </head>
        <body>
        <div class="container">
            <h1>📊 Starlink Analytics Dashboard</h1>
            
            <div class="nav-links">
                <a href="/">🛰️ Live Monitor</a>
                <a href="/analytics">📊 Analytics</a>
                <a href="/speedtest">⚡ Speed Test</a>
            </div>
            
            <h2>📈 Summary Statistics</h2>
            <div class="summary-grid">
                <div class="summary-card">
                    <div class="summary-title">Avg Download Speed</div>
                    <div class="summary-value good">{summary_7d.get('avg_download', 0):.1f} Mbps</div>
                    <div class="summary-period">Last 7 days</div>
                </div>
                <div class="summary-card">
                    <div class="summary-title">Avg Upload Speed</div>
                    <div class="summary-value good">{summary_7d.get('avg_upload', 0):.1f} Mbps</div>
                    <div class="summary-period">Last 7 days</div>
                </div>
                <div class="summary-card">
                    <div class="summary-title">Avg Latency</div>
                    <div class="summary-value {'good' if summary_7d.get('avg_latency', 0) < 50 else 'warning'}">{summary_7d.get('avg_latency', 0):.0f} ms</div>
                    <div class="summary-period">Last 7 days</div>
                </div>
                <div class="summary-card">
                    <div class="summary-title">Connection Quality</div>
                    <div class="summary-value good">{summary_7d.get('avg_quality', 0):.0f}%</div>
                    <div class="summary-period">Last 7 days</div>
                </div>
                <div class="summary-card">
                    <div class="summary-title">Total Measurements</div>
                    <div class="summary-value">{summary_30d.get('total_measurements', 0):,}</div>
                    <div class="summary-period">Last 30 days</div>
                </div>
                <div class="summary-card">
                    <div class="summary-title">Outages</div>
                    <div class="summary-value {'good' if summary_30d.get('outage_count', 0) == 0 else 'bad'}">{summary_30d.get('outage_count', 0)}</div>
                    <div class="summary-period">Last 30 days ({summary_30d.get('total_outage_seconds', 0)/3600:.1f}h total)</div>
                </div>
            </div>
            
            <h2>📊 Historical Trends</h2>
            <div class="charts-grid">
                <div class="chart-row">
                    <div class="chart-container">
                        <div class="chart-title">Speed Trends Over Time</div>
                        <div class="chart-controls">
                            <select id="speedPeriod">
                                <option value="hour">Hourly</option>
                                <option value="day">Daily</option>
                            </select>
                            <select id="speedDays">
                                <option value="7">Last 7 days</option>
                                <option value="14">Last 14 days</option>
                                <option value="30">Last 30 days</option>
                            </select>
                        </div>
                        <canvas id="speedTrendChart" width="400" height="250"></canvas>
                    </div>
                    
                    <div class="chart-container">
                        <div class="chart-title">Latency Trends</div>
                        <div class="chart-controls">
                            <select id="latencyPeriod">
                                <option value="hour">Hourly</option>
                                <option value="day">Daily</option>
                            </select>
                            <select id="latencyDays">
                                <option value="7">Last 7 days</option>
                                <option value="14">Last 14 days</option>
                                <option value="30">Last 30 days</option>
                            </select>
                        </div>
                        <canvas id="latencyTrendChart" width="400" height="250"></canvas>
                    </div>
                </div>
                
                <div class="chart-row">
                    <div class="chart-container">
                        <div class="chart-title">Performance by Hour of Day</div>
                        <canvas id="hourlyPerformanceChart" width="400" height="250"></canvas>
                    </div>
                    
                    <div class="chart-container">
                        <div class="chart-title">Performance by Day of Week</div>
                        <canvas id="weeklyPerformanceChart" width="400" height="250"></canvas>
                    </div>
                </div>
            </div>
            
            <h2>📋 Performance Reports</h2>
            <div class="reports-section">
                <div class="report-controls">
                    <select id="reportType">
                        <option value="daily">Daily Reports</option>
                        <option value="weekly">Weekly Reports</option>
                        <option value="monthly">Monthly Reports</option>
                    </select>
                    <select id="reportDays">
                        <option value="30">Last 30 days</option>
                        <option value="60">Last 60 days</option>
                        <option value="90">Last 90 days</option>
                    </select>
                    <button id="loadReports" class="refresh-btn">🔄 Load Reports</button>
                </div>
                <div id="reportsContainer" class="reports-container">
                    <div class="loading">📊 Click "Load Reports" to view performance data</div>
                </div>
            </div>
            
            <h2>🔍 Advanced Analytics</h2>
            <div class="analytics-section">
                <div class="analytics-grid">
                    <div class="analytics-card">
                        <h3>⏰ Day vs Night Performance</h3>
                        <div id="dayNightComparison" class="comparison-data">
                            <div class="loading">Loading comparison...</div>
                        </div>
                    </div>
                    
                    <div class="analytics-card">
                        <h3>🎯 Quality Distribution</h3>
                        <div id="qualityDistribution" class="distribution-data">
                            <div class="loading">Loading distribution...</div>
                        </div>
                    </div>
                    
                    <div class="analytics-card">
                        <h3>📊 Speed Consistency</h3>
                        <div id="speedConsistency" class="consistency-data">
                            <div class="loading">Loading consistency...</div>
                        </div>
                    </div>
                    
                    <div class="analytics-card">
                        <h3>⚡ Peak Performance Hours</h3>
                        <div id="peakHours" class="peak-data">
                            <div class="loading">Loading peak hours...</div>
                        </div>
                    </div>
                </div>
            </div>
            
            <h2>🚀 Speed Test Analytics</h2>
            <div class="speedtest-section">
                <div class="analytics-grid">
                    <div class="analytics-card">
                        <h3>📊 Speed Test Summary</h3>
                        <div id="speedTestSummary" class="summary-data">
                            <div class="loading">Loading summary...</div>
                        </div>
                    </div>
                    
                    <div class="analytics-card">
                        <h3>📈 Speed Test Trends</h3>
                        <div class="chart-container">
                            <canvas id="speedTestTrendChart" width="400" height="200"></canvas>
                        </div>
                    </div>
                </div>
                
                <div class="analytics-card" style="margin-top: 20px;">
                    <h3>🚀 Quick Speed Test</h3>
                    <div style="text-align: center; padding: 20px;">
                        <p>Run a speed test directly from the analytics dashboard</p>
                        <a href="/speedtest" class="test-button" style="display: inline-block; text-decoration: none; padding: 12px 30px; background: #667eea; color: white; border-radius: 25px; margin: 10px;">🚀 Launch Speed Test Interface</a>
                    </div>
                </div>
            </div>
            
            <h2>💡 Performance Insights</h2>
            <div class="insights-section">
                <div id="performanceInsights" class="insights-container">
                    <div class="loading">Loading insights...</div>
                </div>
            </div>
            
            <div id="loading" class="loading">📊 Loading analytics data...</div>
            
            <div style="text-align: center; margin-top: 30px; color: #666; font-size: 0.9em;">
                Data collection started: {summary_30d.get('oldest_data', 'N/A')}<br>
                Last updated: {time.strftime('%Y-%m-%d %H:%M:%S')}
            </div>
        </div>

        <script>
        // Chart configurations
        const chartConfig = {{
            responsive: true,
            maintainAspectRatio: false,
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
                        maxTicksLimit: 12
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
            animation: {{ duration: 1000 }}
        }};

        // Initialize charts
        let speedTrendChart, latencyTrendChart, hourlyChart, weeklyChart;
        
        function initializeCharts() {{
            const speedCtx = document.getElementById('speedTrendChart').getContext('2d');
            const latencyCtx = document.getElementById('latencyTrendChart').getContext('2d');
            const hourlyCtx = document.getElementById('hourlyPerformanceChart').getContext('2d');
            const weeklyCtx = document.getElementById('weeklyPerformanceChart').getContext('2d');
            
            speedTrendChart = new Chart(speedCtx, {{
                type: 'line',
                data: {{ labels: [], datasets: [] }},
                options: chartConfig
            }});
            
            latencyTrendChart = new Chart(latencyCtx, {{
                type: 'line',
                data: {{ labels: [], datasets: [] }},
                options: chartConfig
            }});
            
            hourlyChart = new Chart(hourlyCtx, {{
                type: 'bar',
                data: {{ labels: [], datasets: [] }},
                options: chartConfig
            }});
            
            weeklyChart = new Chart(weeklyCtx, {{
                type: 'bar',
                data: {{ labels: [], datasets: [] }},
                options: chartConfig
            }});
        }}
        
        // Load trend data
        async function loadTrendData(metric, period, days, chart) {{
            try {{
                const response = await fetch(`/api/historical/trends?metric=${{metric}}&period=${{period}}&days=${{days}}`);
                const data = await response.json();
                
                if (!data.data || data.data.length === 0) {{
                    // Show "no data" message in chart
                    chart.data.labels = ['Collecting Data...'];
                    chart.data.datasets = [{{
                        label: `${{metric.replace('_', ' ').replace('mbps', 'Mbps').replace('ms', ' (ms)')}} - Data Collection Started`,
                        data: [0],
                        borderColor: '#cbd5e1',
                        backgroundColor: 'rgba(203, 213, 225, 0.1)',
                        tension: 0.4,
                        fill: true
                    }}];
                    chart.update();
                    return;
                }}
                
                const labels = data.data.map(d => d.period);
                const values = data.data.map(d => d.avg_value || 0);
                
                chart.data.labels = labels;
                chart.data.datasets = [{{
                    label: `${{metric.replace('_', ' ').replace('mbps', 'Mbps').replace('ms', ' (ms)')}}`,
                    data: values,
                    borderColor: metric.includes('download') ? 'rgb(34, 197, 94)' : 
                                 metric.includes('upload') ? 'rgb(239, 68, 68)' :
                                 metric.includes('latency') ? 'rgb(99, 132, 255)' : 'rgb(168, 85, 247)',
                    backgroundColor: metric.includes('download') ? 'rgba(34, 197, 94, 0.1)' : 
                                     metric.includes('upload') ? 'rgba(239, 68, 68, 0.1)' :
                                     metric.includes('latency') ? 'rgba(99, 132, 255, 0.1)' : 'rgba(168, 85, 247, 0.1)',
                    tension: 0.4,
                    fill: true
                }}];
                
                chart.update();
            }} catch (error) {{
                console.error('Error loading trend data:', error);
                // Show error in chart
                chart.data.labels = ['Error'];
                chart.data.datasets = [{{
                    label: `Error loading ${{metric}}`,
                    data: [0],
                    borderColor: '#ef4444',
                    backgroundColor: 'rgba(239, 68, 68, 0.1)'
                }}];
                chart.update();
            }}
        }}
        
        // Load comparison data
        async function loadComparisonData() {{
            try {{
                const response = await fetch('/api/historical/comparison?metric=download_mbps');
                const data = await response.json();
                
                // Hourly performance
                const hourlyLabels = data.hourly.map(d => `${{d.hour}}:00`);
                const hourlyValues = data.hourly.map(d => d.avg_value);
                
                hourlyChart.data.labels = hourlyLabels;
                hourlyChart.data.datasets = [{{
                    label: 'Avg Download Speed (Mbps)',
                    data: hourlyValues,
                    backgroundColor: 'rgba(34, 197, 94, 0.8)',
                    borderColor: 'rgb(34, 197, 94)',
                    borderWidth: 1
                }}];
                hourlyChart.update();
                
                // Weekly performance
                const weeklyLabels = data.weekly.map(d => d.day_name);
                const weeklyValues = data.weekly.map(d => d.avg_value);
                
                weeklyChart.data.labels = weeklyLabels;
                weeklyChart.data.datasets = [{{
                    label: 'Avg Download Speed (Mbps)',
                    data: weeklyValues,
                    backgroundColor: 'rgba(99, 132, 255, 0.8)',
                    borderColor: 'rgb(99, 132, 255)',
                    borderWidth: 1
                }}];
                weeklyChart.update();
                
            }} catch (error) {{
                console.error('Error loading comparison data:', error);
            }}
        }}
        
        // Initialize everything
        document.addEventListener('DOMContentLoaded', function() {{
            initializeCharts();
            
            // Load initial data
            loadTrendData('download_mbps', 'hour', 7, speedTrendChart);
            loadTrendData('latency_ms', 'hour', 7, latencyTrendChart);
            loadComparisonData();
            
            // Hide loading indicator
            document.getElementById('loading').style.display = 'none';
            
            // Event listeners for controls
            document.getElementById('speedPeriod').addEventListener('change', function() {{
                const period = this.value;
                const days = document.getElementById('speedDays').value;
                loadTrendData('download_mbps', period, days, speedTrendChart);
            }});
            
            document.getElementById('speedDays').addEventListener('change', function() {{
                const period = document.getElementById('speedPeriod').value;
                const days = this.value;
                loadTrendData('download_mbps', period, days, speedTrendChart);
            }});
            
            document.getElementById('latencyPeriod').addEventListener('change', function() {{
                const period = this.value;
                const days = document.getElementById('latencyDays').value;
                loadTrendData('latency_ms', period, days, latencyTrendChart);
            }});
            
            document.getElementById('latencyDays').addEventListener('change', function() {{
                const period = document.getElementById('latencyPeriod').value;
                const days = this.value;
                loadTrendData('latency_ms', period, days, latencyTrendChart);
            }});
            
            // Reports functionality
            document.getElementById('loadReports').addEventListener('click', async function() {{
                const reportType = document.getElementById('reportType').value;
                const days = document.getElementById('reportDays').value;
                const container = document.getElementById('reportsContainer');
                
                try {{
                    container.innerHTML = '<div class="loading">📊 Loading reports...</div>';
                    
                    const response = await fetch(`/api/historical/reports?type=${{reportType}}&days=${{days}}`);
                    const data = await response.json();
                    
                    if (!data.success) {{
                        throw new Error(data.error || 'Failed to load reports');
                    }}
                    
                    renderReportsTable(data.data, reportType, container);
                }} catch (error) {{
                    container.innerHTML = `<div style="color: #ef4444; padding: 20px; text-align: center;">❌ Error loading reports: ${{error.message}}</div>`;
                }}
            }});
            
            function renderReportsTable(data, reportType, container) {{
                if (!data || data.length === 0) {{
                    container.innerHTML = '<div style="text-align: center; padding: 40px; color: #666;">📊 No report data available for the selected period</div>';
                    return;
                }}
                
                let headers, getRowData;
                
                if (reportType === 'daily') {{
                    headers = ['Date', 'Avg Latency', 'Avg Download', 'Avg Upload', 'Quality', 'Outages', 'Grade'];
                    getRowData = (item) => [
                        item.date,
                        `${{item.avg_latency_ms?.toFixed(0) || '0'}} ms`,
                        `${{item.avg_download_mbps?.toFixed(1) || '0'}} Mbps`,
                        `${{item.avg_upload_mbps?.toFixed(1) || '0'}} Mbps`,
                        `${{item.avg_quality_score?.toFixed(0) || '0'}}%`,
                        `${{item.outage_count || 0}} (${{item.total_outage_minutes || 0}}m)`,
                        `<span class="grade-badge grade-${{item.performance_grade || 'F'}}">${{item.performance_grade || 'F'}}</span>`
                    ];
                }} else if (reportType === 'weekly') {{
                    headers = ['Week Start', 'Week End', 'Days', 'Avg Latency', 'Avg Download', 'Avg Upload', 'Quality', 'Outages', 'Grade'];
                    getRowData = (item) => [
                        item.week_start,
                        item.week_end,
                        item.days_with_data,
                        `${{item.avg_latency_ms?.toFixed(0) || '0'}} ms`,
                        `${{item.avg_download_mbps?.toFixed(1) || '0'}} Mbps`,
                        `${{item.avg_upload_mbps?.toFixed(1) || '0'}} Mbps`,
                        `${{item.avg_quality_score?.toFixed(0) || '0'}}%`,
                        `${{item.total_outages || 0}} (${{item.total_outage_minutes || 0}}m)`,
                        `<span class="grade-badge grade-${{item.performance_grade || 'F'}}">${{item.performance_grade || 'F'}}</span>`
                    ];
                }} else {{ // monthly
                    headers = ['Month', 'Days', 'Avg Latency', 'Avg Download', 'Avg Upload', 'Quality', 'Outages', 'Grade'];
                    getRowData = (item) => [
                        item.month,
                        item.days_with_data,
                        `${{item.avg_latency_ms?.toFixed(0) || '0'}} ms`,
                        `${{item.avg_download_mbps?.toFixed(1) || '0'}} Mbps`,
                        `${{item.avg_upload_mbps?.toFixed(1) || '0'}} Mbps`,
                        `${{item.avg_quality_score?.toFixed(0) || '0'}}%`,
                        `${{item.total_outages || 0}} (${{item.total_outage_minutes || 0}}m)`,
                        `<span class="grade-badge grade-${{item.performance_grade || 'F'}}">${{item.performance_grade || 'F'}}</span>`
                    ];
                }}
                
                const tableHTML = `
                    <table class="report-table">
                        <thead>
                            <tr>
                                ${{headers.map(h => `<th>${{h}}</th>`).join('')}}
                            </tr>
                        </thead>
                        <tbody>
                            ${{data.map(item => `
                                <tr>
                                    ${{getRowData(item).map(cell => `<td>${{cell}}</td>`).join('')}}
                                </tr>
                            `).join('')}}
                        </tbody>
                    </table>
                `;
                
                container.innerHTML = tableHTML;
            }}
            
            // Load advanced analytics
            async function loadAdvancedAnalytics() {{
                try {{
                    const response = await fetch('/api/historical/analytics?days=30');
                    const data = await response.json();
                    
                    if (!data.success) {{
                        throw new Error(data.error || 'Failed to load analytics');
                    }}
                    
                    renderAdvancedAnalytics(data.data);
                }} catch (error) {{
                    console.error('Error loading advanced analytics:', error);
                }}
            }}
            
            function renderAdvancedAnalytics(analytics) {{
                // Day vs Night comparison
                const dayNightEl = document.getElementById('dayNightComparison');
                if (analytics.day_vs_night && analytics.day_vs_night.day && analytics.day_vs_night.night) {{
                    const day = analytics.day_vs_night.day;
                    const night = analytics.day_vs_night.night;
                    
                    dayNightEl.innerHTML = `
                        <div class="comparison-row">
                            <span class="comparison-label">Download Speed</span>
                            <span class="comparison-value">Day: ${{day.avg_download?.toFixed(1) || 0}} Mbps | Night: ${{night.avg_download?.toFixed(1) || 0}} Mbps</span>
                        </div>
                        <div class="comparison-row">
                            <span class="comparison-label">Latency</span>
                            <span class="comparison-value">Day: ${{day.avg_latency?.toFixed(0) || 0}} ms | Night: ${{night.avg_latency?.toFixed(0) || 0}} ms</span>
                        </div>
                        <div class="comparison-row">
                            <span class="comparison-label">Quality Score</span>
                            <span class="comparison-value">Day: ${{day.avg_quality?.toFixed(0) || 0}}% | Night: ${{night.avg_quality?.toFixed(0) || 0}}%</span>
                        </div>
                    `;
                }} else {{
                    dayNightEl.innerHTML = '<div style="color: #666; text-align: center; padding: 20px;">Insufficient data for comparison</div>';
                }}
                
                // Quality distribution
                const qualityEl = document.getElementById('qualityDistribution');
                if (analytics.quality_distribution && analytics.quality_distribution.length > 0) {{
                    const qualityHTML = analytics.quality_distribution.map(item => `
                        <div class="comparison-row">
                            <span class="metric-badge metric-${{item.quality_category}}">${{item.quality_category.charAt(0).toUpperCase() + item.quality_category.slice(1)}}</span>
                            <span class="comparison-value">${{item.percentage}}% (${{item.count}} measurements)</span>
                        </div>
                    `).join('');
                    qualityEl.innerHTML = qualityHTML;
                }} else {{
                    qualityEl.innerHTML = '<div style="color: #666; text-align: center; padding: 20px;">No quality data available</div>';
                }}
                
                // Speed consistency
                const consistencyEl = document.getElementById('speedConsistency');
                if (analytics.speed_consistency) {{
                    const consistency = analytics.speed_consistency;
                    consistencyEl.innerHTML = `
                        <div class="comparison-row">
                            <span class="comparison-label">Average Speed</span>
                            <span class="comparison-value">${{consistency.avg_download?.toFixed(1) || 0}} Mbps</span>
                        </div>
                        <div class="comparison-row">
                            <span class="comparison-label">Speed Range</span>
                            <span class="comparison-value">${{consistency.min_download?.toFixed(1) || 0}} - ${{consistency.max_download?.toFixed(1) || 0}} Mbps</span>
                        </div>
                        <div class="comparison-row">
                            <span class="comparison-label">Consistency</span>
                            <span class="metric-badge metric-${{consistency.consistency_rating === 'very_consistent' ? 'excellent' : consistency.consistency_rating === 'consistent' ? 'good' : 'fair'}}">${{consistency.consistency_rating?.replace('_', ' ') || 'Unknown'}}</span>
                        </div>
                    `;
                }} else {{
                    consistencyEl.innerHTML = '<div style="color: #666; text-align: center; padding: 20px;">No consistency data available</div>';
                }}
                
                // Peak hours
                const peakEl = document.getElementById('peakHours');
                if (analytics.peak_hours && analytics.peak_hours.length > 0) {{
                    const peakHTML = analytics.peak_hours.map((hour, index) => `
                        <div class="comparison-row">
                            <span class="comparison-label">#${{index + 1}} Peak: ${{hour.hour}}:00</span>
                            <span class="comparison-value">${{hour.avg_download?.toFixed(1) || 0}} Mbps</span>
                        </div>
                    `).join('');
                    peakEl.innerHTML = peakHTML;
                }} else {{
                    peakEl.innerHTML = '<div style="color: #666; text-align: center; padding: 20px;">No peak hour data available</div>';
                }}
            }}
            
            // Load performance insights
            async function loadPerformanceInsights() {{
                try {{
                    const response = await fetch('/api/historical/insights?days=30');
                    const data = await response.json();
                    
                    if (!data.success) {{
                        throw new Error(data.error || 'Failed to load insights');
                    }}
                    
                    renderPerformanceInsights(data.data);
                }} catch (error) {{
                    console.error('Error loading performance insights:', error);
                }}
            }}
            
            function renderPerformanceInsights(insights) {{
                const insightsEl = document.getElementById('performanceInsights');
                
                if (!insights || insights.length === 0) {{
                    insightsEl.innerHTML = '<div style="background: white; padding: 30px; border-radius: 8px; text-align: center; color: #10b981;"><strong>🎉 Great Performance!</strong><br>No significant performance issues detected in the analyzed period.</div>';
                    return;
                }}
                
                const insightsHTML = insights.map(insight => `
                    <div class="insight-item insight-${{insight.severity}}">
                        <div class="insight-title">${{insight.title}}</div>
                        <div class="insight-description">${{insight.description}}</div>
                        <div class="insight-recommendation">💡 ${{insight.recommendation}}</div>
                    </div>
                `).join('');
                
                insightsEl.innerHTML = insightsHTML;
            }}
            
            // Load speed test analytics
            let speedTestTrendChart;
            
            async function loadSpeedTestAnalytics() {{
                try {{
                    // Load speed test summary
                    const summaryResponse = await fetch('/api/speedtest/summary?days=30');
                    const summaryData = await summaryResponse.json();
                    
                    if (summaryData.success) {{
                        renderSpeedTestSummary(summaryData.data);
                    }}
                    
                    // Load speed test trends
                    const trendsResponse = await fetch('/api/speedtest/trends?days=30');
                    const trendsData = await trendsResponse.json();
                    
                    if (trendsData.success) {{
                        renderSpeedTestTrends(trendsData.data);
                    }}
                }} catch (error) {{
                    console.error('Error loading speed test analytics:', error);
                }}
            }}
            
            function renderSpeedTestSummary(data) {{
                const summaryEl = document.getElementById('speedTestSummary');
                
                if (!data || data.total_tests === 0) {{
                    summaryEl.innerHTML = '<div style="text-align: center; padding: 20px; color: #666;">No speed tests available. <a href="/speedtest" style="color: #667eea;">Run your first test →</a></div>';
                    return;
                }}
                
                summaryEl.innerHTML = `
                    <div class="comparison-row">
                        <span class="comparison-label">Total Tests</span>
                        <span class="comparison-value">${{data.total_tests || 0}}</span>
                    </div>
                    <div class="comparison-row">
                        <span class="comparison-label">Success Rate</span>
                        <span class="comparison-value">${{((data.completed_tests / data.total_tests) * 100).toFixed(1)}}%</span>
                    </div>
                    <div class="comparison-row">
                        <span class="comparison-label">Avg Download</span>
                        <span class="comparison-value">${{(data.avg_download || 0).toFixed(1)}} Mbps</span>
                    </div>
                    <div class="comparison-row">
                        <span class="comparison-label">Max Download</span>
                        <span class="comparison-value">${{(data.max_download || 0).toFixed(1)}} Mbps</span>
                    </div>
                    <div class="comparison-row">
                        <span class="comparison-label">Avg Upload</span>
                        <span class="comparison-value">${{(data.avg_upload || 0).toFixed(1)}} Mbps</span>
                    </div>
                `;
            }}
            
            function renderSpeedTestTrends(data) {{
                const ctx = document.getElementById('speedTestTrendChart').getContext('2d');
                
                if (!data || data.length === 0) {{
                    // Show "no data" message
                    ctx.font = '16px Arial';
                    ctx.fillStyle = '#666';
                    ctx.textAlign = 'center';
                    ctx.fillText('No speed test data available', ctx.canvas.width / 2, ctx.canvas.height / 2);
                    return;
                }}
                
                const labels = data.map(d => d.date);
                const downloadData = data.map(d => d.avg_download || 0);
                const uploadData = data.map(d => d.avg_upload || 0);
                
                speedTestTrendChart = new Chart(ctx, {{
                    type: 'line',
                    data: {{
                        labels: labels,
                        datasets: [{{
                            label: 'Download Speed (Mbps)',
                            data: downloadData,
                            borderColor: 'rgb(34, 197, 94)',
                            backgroundColor: 'rgba(34, 197, 94, 0.1)',
                            tension: 0.4,
                            fill: true
                        }}, {{
                            label: 'Upload Speed (Mbps)',
                            data: uploadData,
                            borderColor: 'rgb(239, 68, 68)',
                            backgroundColor: 'rgba(239, 68, 68, 0.1)',
                            tension: 0.4,
                            fill: true
                        }}]
                    }},
                    options: {{
                        responsive: true,
                        plugins: {{
                            legend: {{
                                display: true,
                                position: 'top'
                            }}
                        }},
                        scales: {{
                            y: {{
                                beginAtZero: true,
                                title: {{
                                    display: true,
                                    text: 'Speed (Mbps)'
                                }}
                            }},
                            x: {{
                                title: {{
                                    display: true,
                                    text: 'Date'
                                }}
                            }}
                        }}
                    }}
                }});
            }}
            
            // Load advanced analytics when page loads
            setTimeout(() => {{
                loadAdvancedAnalytics();
                loadPerformanceInsights();
                loadSpeedTestAnalytics();
            }}, 1500);
        }});
        </script>
        </body>
        </html>
        """
    except Exception as e:
        return f"""
        <html>
        <head><title>Analytics Error</title></head>
        <body style="font-family: Arial, sans-serif; margin: 40px;">
        <div style="background: white; padding: 30px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1);">
            <h1 style="color: #ef4444;">⚠️ Analytics Dashboard Error</h1>
            <p style="color: #666;">Unable to load analytics data</p>
            <div style="background: #fee; padding: 15px; border-radius: 5px; margin: 20px 0;">
                <strong>Error details:</strong><br>
                <code style="color: #c00;">{str(e)}</code>
            </div>
            <p><a href="/" style="color: #667eea;">← Back to Monitor</a></p>
        </div>
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