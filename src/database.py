import sqlite3
import os
from datetime import datetime, timedelta
from contextlib import contextmanager
from typing import List, Dict, Optional, Tuple
import json

class StarlinkDatabase:
    def __init__(self, db_path: str = None):
        if db_path is None:
            db_path = os.environ.get('DATABASE_PATH', '../data/starlink_data.db')
        self.db_path = db_path
        self.init_database()
    
    def init_database(self):
        """Initialize database with required tables"""
        # Ensure directory exists
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        
        with self.get_connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME NOT NULL,
                    latency_ms REAL NOT NULL,
                    download_mbps REAL NOT NULL,
                    upload_mbps REAL NOT NULL,
                    obstruction_pct REAL NOT NULL DEFAULT 0,
                    quality_score INTEGER NOT NULL DEFAULT 0,
                    snr_above_noise BOOLEAN NOT NULL DEFAULT 0,
                    uptime_seconds INTEGER DEFAULT 0,
                    gps_valid BOOLEAN DEFAULT 0,
                    gps_satellites INTEGER DEFAULT 0,
                    eth_speed_mbps INTEGER DEFAULT 0,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                
                CREATE TABLE IF NOT EXISTS outages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    start_time DATETIME NOT NULL,
                    end_time DATETIME,
                    duration_seconds INTEGER,
                    reason TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                
                CREATE TABLE IF NOT EXISTS daily_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date DATE NOT NULL UNIQUE,
                    avg_latency_ms REAL,
                    max_latency_ms REAL,
                    min_latency_ms REAL,
                    avg_download_mbps REAL,
                    max_download_mbps REAL,
                    min_download_mbps REAL,
                    avg_upload_mbps REAL,
                    max_upload_mbps REAL,
                    min_upload_mbps REAL,
                    avg_quality_score REAL,
                    avg_obstruction_pct REAL,
                    total_outage_minutes INTEGER DEFAULT 0,
                    outage_count INTEGER DEFAULT 0,
                    data_points INTEGER DEFAULT 0,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                
                CREATE TABLE IF NOT EXISTS performance_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME NOT NULL,
                    event_type TEXT NOT NULL, -- 'speed_test', 'outage', 'poor_performance'
                    details TEXT, -- JSON data
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                
                CREATE TABLE IF NOT EXISTS speed_tests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME NOT NULL,
                    test_type TEXT NOT NULL DEFAULT 'manual', -- 'manual', 'scheduled', 'automated'
                    server_location TEXT,
                    ping_ms REAL NOT NULL DEFAULT 0,
                    download_mbps REAL NOT NULL DEFAULT 0,
                    upload_mbps REAL NOT NULL DEFAULT 0,
                    jitter_ms REAL DEFAULT 0,
                    packet_loss_pct REAL DEFAULT 0,
                    test_duration_seconds INTEGER DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'completed', -- 'running', 'completed', 'failed', 'cancelled'
                    error_message TEXT,
                    test_data TEXT, -- JSON data with detailed results
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                
                CREATE TABLE IF NOT EXISTS speed_test_schedules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    cron_expression TEXT NOT NULL, -- e.g., '0 12 * * *' for daily at noon
                    enabled BOOLEAN NOT NULL DEFAULT 1,
                    last_run DATETIME,
                    next_run DATETIME,
                    run_count INTEGER DEFAULT 0,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                
                -- Create indexes for better query performance
                CREATE INDEX IF NOT EXISTS idx_metrics_timestamp ON metrics(timestamp);
                CREATE INDEX IF NOT EXISTS idx_metrics_date ON metrics(date(timestamp));
                CREATE INDEX IF NOT EXISTS idx_outages_start_time ON outages(start_time);
                CREATE INDEX IF NOT EXISTS idx_daily_stats_date ON daily_stats(date);
                CREATE INDEX IF NOT EXISTS idx_performance_events_timestamp ON performance_events(timestamp);
                CREATE INDEX IF NOT EXISTS idx_speed_tests_timestamp ON speed_tests(timestamp);
                CREATE INDEX IF NOT EXISTS idx_speed_tests_type ON speed_tests(test_type);
                CREATE INDEX IF NOT EXISTS idx_speed_test_schedules_enabled ON speed_test_schedules(enabled);
                CREATE INDEX IF NOT EXISTS idx_speed_test_schedules_next_run ON speed_test_schedules(next_run);
            """)
    
    @contextmanager
    def get_connection(self):
        """Context manager for database connections"""
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row  # Enable dict-like access to rows
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()
    
    def insert_metric(self, timestamp: datetime, latency_ms: float, download_mbps: float, 
                     upload_mbps: float, obstruction_pct: float = 0, quality_score: int = 0,
                     snr_above_noise: bool = False, uptime_seconds: int = 0, 
                     gps_valid: bool = False, gps_satellites: int = 0, eth_speed_mbps: int = 0):
        """Insert a new metric data point"""
        with self.get_connection() as conn:
            conn.execute("""
                INSERT INTO metrics (timestamp, latency_ms, download_mbps, upload_mbps, 
                                   obstruction_pct, quality_score, snr_above_noise, uptime_seconds,
                                   gps_valid, gps_satellites, eth_speed_mbps)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (timestamp, latency_ms, download_mbps, upload_mbps, obstruction_pct, 
                  quality_score, snr_above_noise, uptime_seconds, gps_valid, gps_satellites, eth_speed_mbps))
    
    def get_metrics(self, start_time: datetime = None, end_time: datetime = None, 
                   limit: int = 1000) -> List[Dict]:
        """Retrieve metrics within time range"""
        with self.get_connection() as conn:
            query = "SELECT * FROM metrics"
            params = []
            
            if start_time or end_time:
                query += " WHERE "
                conditions = []
                
                if start_time:
                    conditions.append("timestamp >= ?")
                    params.append(start_time)
                
                if end_time:
                    conditions.append("timestamp <= ?")
                    params.append(end_time)
                
                query += " AND ".join(conditions)
            
            query += " ORDER BY timestamp DESC LIMIT ?"
            params.append(limit)
            
            cursor = conn.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]
    
    def get_hourly_averages(self, days: int = 7) -> List[Dict]:
        """Get hourly averages for the last N days"""
        start_time = datetime.now() - timedelta(days=days)
        
        with self.get_connection() as conn:
            cursor = conn.execute("""
                SELECT 
                    strftime('%Y-%m-%d %H:00:00', timestamp) as hour,
                    AVG(latency_ms) as avg_latency,
                    AVG(download_mbps) as avg_download,
                    AVG(upload_mbps) as avg_upload,
                    AVG(quality_score) as avg_quality,
                    AVG(obstruction_pct) as avg_obstruction,
                    COUNT(*) as data_points
                FROM metrics 
                WHERE timestamp >= ?
                GROUP BY strftime('%Y-%m-%d %H', timestamp)
                ORDER BY hour
            """, (start_time,))
            
            return [dict(row) for row in cursor.fetchall()]
    
    def get_daily_stats(self, days: int = 30) -> List[Dict]:
        """Get or calculate daily statistics"""
        start_date = (datetime.now() - timedelta(days=days)).date()
        
        with self.get_connection() as conn:
            # First, try to get existing daily stats
            cursor = conn.execute("""
                SELECT * FROM daily_stats 
                WHERE date >= ? 
                ORDER BY date DESC
            """, (start_date,))
            
            existing_stats = [dict(row) for row in cursor.fetchall()]
            
            # If we don't have recent stats, calculate them
            if not existing_stats or existing_stats[0]['date'] < datetime.now().date().isoformat():
                self._calculate_daily_stats()
                
                # Re-fetch after calculation
                cursor = conn.execute("""
                    SELECT * FROM daily_stats 
                    WHERE date >= ? 
                    ORDER BY date DESC
                """, (start_date,))
                
                return [dict(row) for row in cursor.fetchall()]
            
            return existing_stats
    
    def _calculate_daily_stats(self):
        """Calculate and store daily statistics"""
        with self.get_connection() as conn:
            # Get the last 7 days of data to process
            start_date = datetime.now() - timedelta(days=7)
            
            cursor = conn.execute("""
                SELECT 
                    date(timestamp) as date,
                    AVG(latency_ms) as avg_latency,
                    MAX(latency_ms) as max_latency,
                    MIN(latency_ms) as min_latency,
                    AVG(download_mbps) as avg_download,
                    MAX(download_mbps) as max_download,
                    MIN(download_mbps) as min_download,
                    AVG(upload_mbps) as avg_upload,
                    MAX(upload_mbps) as max_upload,
                    MIN(upload_mbps) as min_upload,
                    AVG(quality_score) as avg_quality,
                    AVG(obstruction_pct) as avg_obstruction,
                    COUNT(*) as data_points
                FROM metrics 
                WHERE timestamp >= ?
                GROUP BY date(timestamp)
            """, (start_date,))
            
            daily_data = cursor.fetchall()
            
            for row in daily_data:
                # Insert or replace daily stats
                conn.execute("""
                    INSERT OR REPLACE INTO daily_stats 
                    (date, avg_latency_ms, max_latency_ms, min_latency_ms,
                     avg_download_mbps, max_download_mbps, min_download_mbps,
                     avg_upload_mbps, max_upload_mbps, min_upload_mbps,
                     avg_quality_score, avg_obstruction_pct, data_points)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, row)
    
    def get_performance_comparison(self, metric: str = 'download_mbps') -> Dict:
        """Compare performance by time of day, day of week, etc."""
        with self.get_connection() as conn:
            # Performance by hour of day
            cursor = conn.execute(f"""
                SELECT 
                    strftime('%H', timestamp) as hour,
                    AVG({metric}) as avg_value,
                    COUNT(*) as data_points
                FROM metrics 
                WHERE timestamp >= datetime('now', '-30 days')
                GROUP BY strftime('%H', timestamp)
                ORDER BY hour
            """)
            
            hourly_data = [dict(row) for row in cursor.fetchall()]
            
            # Performance by day of week
            cursor = conn.execute(f"""
                SELECT 
                    strftime('%w', timestamp) as day_of_week,
                    AVG({metric}) as avg_value,
                    COUNT(*) as data_points
                FROM metrics 
                WHERE timestamp >= datetime('now', '-30 days')
                GROUP BY strftime('%w', timestamp)
                ORDER BY day_of_week
            """)
            
            weekly_data = [dict(row) for row in cursor.fetchall()]
            
            # Add day names
            day_names = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']
            for row in weekly_data:
                row['day_name'] = day_names[int(row['day_of_week'])]
            
            return {
                'hourly': hourly_data,
                'weekly': weekly_data,
                'metric': metric
            }
    
    def record_outage(self, start_time: datetime, end_time: datetime = None, reason: str = None):
        """Record a connection outage"""
        duration = None
        if end_time:
            duration = int((end_time - start_time).total_seconds())
        
        with self.get_connection() as conn:
            conn.execute("""
                INSERT INTO outages (start_time, end_time, duration_seconds, reason)
                VALUES (?, ?, ?, ?)
            """, (start_time, end_time, duration, reason))
    
    def get_outages(self, days: int = 30) -> List[Dict]:
        """Get outage history"""
        start_time = datetime.now() - timedelta(days=days)
        
        with self.get_connection() as conn:
            cursor = conn.execute("""
                SELECT * FROM outages 
                WHERE start_time >= ? 
                ORDER BY start_time DESC
            """, (start_time,))
            
            return [dict(row) for row in cursor.fetchall()]
    
    def record_performance_event(self, event_type: str, details: Dict = None):
        """Record a performance-related event"""
        details_json = json.dumps(details) if details else None
        
        with self.get_connection() as conn:
            conn.execute("""
                INSERT INTO performance_events (timestamp, event_type, details)
                VALUES (?, ?, ?)
            """, (datetime.now(), event_type, details_json))
    
    def get_summary_stats(self, days: int = 30) -> Dict:
        """Get summary statistics for the dashboard"""
        start_time = datetime.now() - timedelta(days=days)
        
        with self.get_connection() as conn:
            cursor = conn.execute("""
                SELECT 
                    COUNT(*) as total_measurements,
                    AVG(latency_ms) as avg_latency,
                    AVG(download_mbps) as avg_download,
                    AVG(upload_mbps) as avg_upload,
                    AVG(quality_score) as avg_quality,
                    MIN(timestamp) as oldest_data,
                    MAX(timestamp) as newest_data
                FROM metrics 
                WHERE timestamp >= ?
            """, (start_time,))
            
            stats = dict(cursor.fetchone())
            
            # Get outage statistics
            cursor = conn.execute("""
                SELECT 
                    COUNT(*) as outage_count,
                    COALESCE(SUM(duration_seconds), 0) as total_outage_seconds
                FROM outages 
                WHERE start_time >= ?
            """, (start_time,))
            
            outage_stats = dict(cursor.fetchone())
            stats.update(outage_stats)
            
            return stats
    
    def cleanup_old_data(self, days_to_keep: int = 90):
        """Clean up old detailed data (keep daily stats)"""
        cutoff_date = datetime.now() - timedelta(days=days_to_keep)
        
        with self.get_connection() as conn:
            cursor = conn.execute("DELETE FROM metrics WHERE timestamp < ?", (cutoff_date,))
            deleted_count = cursor.rowcount
            
            cursor = conn.execute("DELETE FROM outages WHERE start_time < ?", (cutoff_date,))
            deleted_outages = cursor.rowcount
            
            return {
                'deleted_metrics': deleted_count,
                'deleted_outages': deleted_outages
            }
    
    def get_trend_data(self, metric: str, period: str = 'hour', days: int = 7) -> List[Dict]:
        """Get trend data for charts"""
        start_time = datetime.now() - timedelta(days=days)
        
        if period == 'hour':
            group_format = '%Y-%m-%d %H:00:00'
        elif period == 'day':
            group_format = '%Y-%m-%d'
        else:  # minute
            group_format = '%Y-%m-%d %H:%M:00'
        
        with self.get_connection() as conn:
            cursor = conn.execute(f"""
                SELECT 
                    strftime('{group_format}', timestamp) as period,
                    AVG({metric}) as avg_value,
                    MIN({metric}) as min_value,
                    MAX({metric}) as max_value,
                    COUNT(*) as data_points
                FROM metrics 
                WHERE timestamp >= ?
                GROUP BY strftime('{group_format}', timestamp)
                ORDER BY period
            """, (start_time,))
            
            return [dict(row) for row in cursor.fetchall()]
    
    def get_advanced_analytics(self, days: int = 30) -> Dict:
        """Get advanced performance analytics"""
        start_time = datetime.now() - timedelta(days=days)
        
        with self.get_connection() as conn:
            # Day vs Night comparison
            cursor = conn.execute("""
                SELECT 
                    CASE 
                        WHEN strftime('%H', timestamp) BETWEEN '06' AND '18' THEN 'day'
                        ELSE 'night'
                    END as time_period,
                    AVG(latency_ms) as avg_latency,
                    AVG(download_mbps) as avg_download,
                    AVG(upload_mbps) as avg_upload,
                    AVG(quality_score) as avg_quality,
                    COUNT(*) as data_points
                FROM metrics 
                WHERE timestamp >= ?
                GROUP BY time_period
            """, (start_time,))
            
            day_night_data = {row['time_period']: dict(row) for row in cursor.fetchall()}
            
            # Peak hours analysis
            cursor = conn.execute("""
                SELECT 
                    strftime('%H', timestamp) as hour,
                    AVG(latency_ms) as avg_latency,
                    AVG(download_mbps) as avg_download,
                    AVG(upload_mbps) as avg_upload,
                    AVG(quality_score) as avg_quality,
                    COUNT(*) as data_points
                FROM metrics 
                WHERE timestamp >= ?
                GROUP BY strftime('%H', timestamp)
                ORDER BY avg_download DESC
                LIMIT 3
            """, (start_time,))
            
            peak_hours = [dict(row) for row in cursor.fetchall()]
            
            # Quality distribution
            cursor = conn.execute("""
                SELECT 
                    CASE 
                        WHEN quality_score >= 90 THEN 'excellent'
                        WHEN quality_score >= 80 THEN 'good'
                        WHEN quality_score >= 70 THEN 'fair'
                        WHEN quality_score >= 50 THEN 'poor'
                        ELSE 'bad'
                    END as quality_category,
                    COUNT(*) as count,
                    ROUND(COUNT(*) * 100.0 / (SELECT COUNT(*) FROM metrics WHERE timestamp >= ?), 1) as percentage
                FROM metrics 
                WHERE timestamp >= ?
                GROUP BY quality_category
                ORDER BY count DESC
            """, (start_time, start_time))
            
            quality_distribution = [dict(row) for row in cursor.fetchall()]
            
            # Speed consistency analysis
            cursor = conn.execute("""
                SELECT 
                    AVG(download_mbps) as avg_download,
                    MIN(download_mbps) as min_download,
                    MAX(download_mbps) as max_download,
                    (MAX(download_mbps) - MIN(download_mbps)) as download_range,
                    CASE 
                        WHEN (MAX(download_mbps) - MIN(download_mbps)) < 10 THEN 'very_consistent'
                        WHEN (MAX(download_mbps) - MIN(download_mbps)) < 25 THEN 'consistent'
                        WHEN (MAX(download_mbps) - MIN(download_mbps)) < 50 THEN 'moderate'
                        ELSE 'inconsistent'
                    END as consistency_rating
                FROM metrics 
                WHERE timestamp >= ? AND download_mbps > 0
            """, (start_time,))
            
            speed_consistency = dict(cursor.fetchone())
            
            return {
                'day_vs_night': day_night_data,
                'peak_hours': peak_hours,
                'quality_distribution': quality_distribution,
                'speed_consistency': speed_consistency,
                'analysis_period_days': days
            }
    
    def get_performance_insights(self, days: int = 30) -> List[Dict]:
        """Get actionable performance insights"""
        start_time = datetime.now() - timedelta(days=days)
        insights = []
        
        with self.get_connection() as conn:
            # Check for consistent poor performance times
            cursor = conn.execute("""
                SELECT 
                    strftime('%H', timestamp) as hour,
                    AVG(quality_score) as avg_quality,
                    COUNT(*) as data_points
                FROM metrics 
                WHERE timestamp >= ? AND quality_score < 60
                GROUP BY strftime('%H', timestamp)
                HAVING data_points > 5
                ORDER BY avg_quality
                LIMIT 3
            """, (start_time,))
            
            poor_hours = cursor.fetchall()
            for hour in poor_hours:
                insights.append({
                    'type': 'poor_performance_time',
                    'title': f"Consistent Poor Performance at {hour['hour']}:00",
                    'description': f"Quality score averages {hour['avg_quality']:.0f}% during this hour",
                    'severity': 'high' if hour['avg_quality'] < 40 else 'medium',
                    'recommendation': 'Consider network usage patterns or schedule heavy tasks for other hours'
                })
            
            # Check obstruction patterns
            cursor = conn.execute("""
                SELECT 
                    AVG(obstruction_pct) as avg_obstruction,
                    COUNT(*) as affected_measurements
                FROM metrics 
                WHERE timestamp >= ? AND obstruction_pct > 0
            """, (start_time,))
            
            obstruction_data = cursor.fetchone()
            if obstruction_data and obstruction_data['avg_obstruction'] > 1:
                insights.append({
                    'type': 'obstruction_issue',
                    'title': 'Obstruction Detected',
                    'description': f"Average obstruction: {obstruction_data['avg_obstruction']:.1f}%",
                    'severity': 'high' if obstruction_data['avg_obstruction'] > 5 else 'medium',
                    'recommendation': 'Check dish positioning and clear any obstructions'
                })
            
            # Check for excessive outages
            cursor = conn.execute("""
                SELECT COUNT(*) as outage_count, SUM(duration_seconds) as total_downtime
                FROM outages 
                WHERE start_time >= ?
            """, (start_time,))
            
            outage_data = cursor.fetchone()
            if outage_data and outage_data['outage_count'] > 5:
                insights.append({
                    'type': 'frequent_outages',
                    'title': 'Frequent Connection Issues',
                    'description': f"{outage_data['outage_count']} outages in {days} days ({outage_data['total_downtime']/3600:.1f}h total)",
                    'severity': 'high',
                    'recommendation': 'Contact support if outages persist or check power/cable connections'
                })
        
        return insights
    
    def insert_speed_test(self, timestamp: datetime, test_type: str, server_location: str,
                         ping_ms: float, download_mbps: float, upload_mbps: float,
                         jitter_ms: float = 0, packet_loss_pct: float = 0,
                         test_duration_seconds: int = 0, status: str = 'completed',
                         error_message: str = None, test_data: Dict = None):
        """Insert a new speed test result"""
        test_data_json = json.dumps(test_data) if test_data else None
        
        with self.get_connection() as conn:
            cursor = conn.execute("""
                INSERT INTO speed_tests (timestamp, test_type, server_location, ping_ms,
                                       download_mbps, upload_mbps, jitter_ms, packet_loss_pct,
                                       test_duration_seconds, status, error_message, test_data)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (timestamp, test_type, server_location, ping_ms, download_mbps, upload_mbps,
                  jitter_ms, packet_loss_pct, test_duration_seconds, status, error_message, test_data_json))
            
            return cursor.lastrowid
    
    def get_speed_tests(self, days: int = 30, test_type: str = None, limit: int = 100) -> List[Dict]:
        """Get speed test history"""
        start_time = datetime.now() - timedelta(days=days)
        
        with self.get_connection() as conn:
            query = "SELECT * FROM speed_tests WHERE timestamp >= ?"
            params = [start_time]
            
            if test_type:
                query += " AND test_type = ?"
                params.append(test_type)
            
            query += " ORDER BY timestamp DESC LIMIT ?"
            params.append(limit)
            
            cursor = conn.execute(query, params)
            results = [dict(row) for row in cursor.fetchall()]
            
            # Parse JSON test_data
            for result in results:
                if result.get('test_data'):
                    try:
                        result['test_data'] = json.loads(result['test_data'])
                    except:
                        result['test_data'] = None
            
            return results
    
    def get_speed_test_summary(self, days: int = 30) -> Dict:
        """Get speed test summary statistics"""
        start_time = datetime.now() - timedelta(days=days)
        
        with self.get_connection() as conn:
            cursor = conn.execute("""
                SELECT 
                    COUNT(*) as total_tests,
                    COUNT(CASE WHEN status = 'completed' THEN 1 END) as completed_tests,
                    COUNT(CASE WHEN status = 'failed' THEN 1 END) as failed_tests,
                    AVG(CASE WHEN status = 'completed' THEN download_mbps END) as avg_download,
                    MAX(CASE WHEN status = 'completed' THEN download_mbps END) as max_download,
                    MIN(CASE WHEN status = 'completed' THEN download_mbps END) as min_download,
                    AVG(CASE WHEN status = 'completed' THEN upload_mbps END) as avg_upload,
                    MAX(CASE WHEN status = 'completed' THEN upload_mbps END) as max_upload,
                    MIN(CASE WHEN status = 'completed' THEN upload_mbps END) as min_upload,
                    AVG(CASE WHEN status = 'completed' THEN ping_ms END) as avg_ping,
                    MIN(timestamp) as oldest_test,
                    MAX(timestamp) as newest_test
                FROM speed_tests 
                WHERE timestamp >= ?
            """, (start_time,))
            
            return dict(cursor.fetchone())
    
    def create_speed_test_schedule(self, name: str, cron_expression: str, enabled: bool = True):
        """Create a new speed test schedule"""
        with self.get_connection() as conn:
            cursor = conn.execute("""
                INSERT INTO speed_test_schedules (name, cron_expression, enabled)
                VALUES (?, ?, ?)
            """, (name, cron_expression, enabled))
            
            return cursor.lastrowid
    
    def get_speed_test_schedules(self, enabled_only: bool = False) -> List[Dict]:
        """Get speed test schedules"""
        with self.get_connection() as conn:
            query = "SELECT * FROM speed_test_schedules"
            params = []
            
            if enabled_only:
                query += " WHERE enabled = 1"
            
            query += " ORDER BY name"
            
            cursor = conn.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]
    
    def update_speed_test_schedule(self, schedule_id: int, last_run: datetime = None, next_run: datetime = None):
        """Update speed test schedule run times"""
        with self.get_connection() as conn:
            if last_run and next_run:
                conn.execute("""
                    UPDATE speed_test_schedules 
                    SET last_run = ?, next_run = ?, run_count = run_count + 1, updated_at = ?
                    WHERE id = ?
                """, (last_run, next_run, datetime.now(), schedule_id))
            elif next_run:
                conn.execute("""
                    UPDATE speed_test_schedules 
                    SET next_run = ?, updated_at = ?
                    WHERE id = ?
                """, (next_run, datetime.now(), schedule_id))
    
    def get_speed_test_trends(self, days: int = 30) -> List[Dict]:
        """Get speed test trends for charts"""
        start_time = datetime.now() - timedelta(days=days)
        
        with self.get_connection() as conn:
            cursor = conn.execute("""
                SELECT 
                    date(timestamp) as date,
                    AVG(download_mbps) as avg_download,
                    MAX(download_mbps) as max_download,
                    MIN(download_mbps) as min_download,
                    AVG(upload_mbps) as avg_upload,
                    MAX(upload_mbps) as max_upload,
                    MIN(upload_mbps) as min_upload,
                    AVG(ping_ms) as avg_ping,
                    MIN(ping_ms) as min_ping,
                    MAX(ping_ms) as max_ping,
                    COUNT(*) as test_count
                FROM speed_tests 
                WHERE timestamp >= ? AND status = 'completed'
                GROUP BY date(timestamp)
                ORDER BY date
            """, (start_time,))
            
            return [dict(row) for row in cursor.fetchall()]