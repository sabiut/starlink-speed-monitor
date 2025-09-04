import threading
import time
from datetime import datetime, timedelta
from typing import Optional
import logging
from database import StarlinkDatabase
from weather_service import WeatherService
import sys
import os

# Add temp3 directory to path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), 'temp3'))
import starlink_grpc

class DataCollector:
    def __init__(self, db: StarlinkDatabase, collection_interval: int = 60):
        """
        Initialize the data collector
        
        Args:
            db: Database instance
            collection_interval: How often to collect data (seconds)
        """
        self.db = db
        self.collection_interval = collection_interval
        self.running = False
        self.thread: Optional[threading.Thread] = None
        self.last_connection_state = True
        self.outage_start_time: Optional[datetime] = None
        
        # Enhanced outage tracking
        self.consecutive_failures = 0
        self.outage_severity_threshold = {
            'minor': 60,    # < 1 minute
            'major': 300,   # 1-5 minutes  
            'critical': 1800  # > 30 minutes
        }
        
        # Weather service integration
        self.weather_service = WeatherService(db)
        self.weather_collection_counter = 0
        self.weather_collection_interval = 10  # Collect weather every 10th cycle (10 minutes if collecting every minute)
        
        # Performance analysis
        self.performance_analysis_counter = 0
        self.performance_analysis_interval = 60  # Run analysis every hour
        
        # Setup logging
        logging.basicConfig(level=logging.INFO)
        self.logger = logging.getLogger(__name__)
    
    def calculate_quality_score(self, latency: float, download_mbps: float, upload_mbps: float, 
                               obstruction_pct: float, snr_good: bool) -> int:
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
    
    def get_speed_data(self):
        """Get speed data using the same logic as the main app"""
        try:
            # Try bulk history for recent high activity
            bulk_data = starlink_grpc.history_bulk_data(300)  # Last 5 minutes
            
            if bulk_data and len(bulk_data) > 1:
                data = bulk_data[1]
                
                downlink_data = data.get('downlink_throughput_bps', [])
                uplink_data = data.get('uplink_throughput_bps', [])
                
                if downlink_data and uplink_data:
                    # Look for significant activity
                    active_downlink = [x for x in downlink_data if x > 1e6]
                    active_uplink = [x for x in uplink_data if x > 1e6]
                    
                    # Also get recent activity
                    recent_downlink = [x for x in downlink_data[-60:] if x > 0]
                    recent_uplink = [x for x in uplink_data[-60:] if x > 0]
                    
                    if active_downlink and active_uplink:
                        # Use high activity data
                        return {
                            'download_mbps': sum(active_downlink) / len(active_downlink) / 1e6,
                            'upload_mbps': sum(active_uplink) / len(active_uplink) / 1e6
                        }
                    elif recent_downlink and recent_uplink:
                        # Use recent data
                        return {
                            'download_mbps': sum(recent_downlink) / len(recent_downlink) / 1e6,
                            'upload_mbps': sum(recent_uplink) / len(recent_uplink) / 1e6
                        }
        except Exception as e:
            self.logger.warning(f"Error getting bulk speed data: {e}")
        
        # Fallback to instantaneous
        try:
            status = starlink_grpc.get_status()
            return {
                'download_mbps': status.downlink_throughput_bps / 1e6 if hasattr(status, 'downlink_throughput_bps') else 0,
                'upload_mbps': status.uplink_throughput_bps / 1e6 if hasattr(status, 'uplink_throughput_bps') else 0
            }
        except Exception as e:
            self.logger.error(f"Error getting instantaneous speed data: {e}")
            return {'download_mbps': 0, 'upload_mbps': 0}
    
    def collect_data_point(self):
        """Collect a single data point and store it"""
        try:
            # Get status data
            status = starlink_grpc.get_status()
            speed_data = self.get_speed_data()
            
            # Extract metrics
            timestamp = datetime.now()
            latency_ms = status.pop_ping_latency_ms if hasattr(status, 'pop_ping_latency_ms') else 0
            download_mbps = speed_data['download_mbps']
            upload_mbps = speed_data['upload_mbps']
            
            # Obstruction info
            obstruction_pct = 0
            if hasattr(status, 'obstruction_stats') and hasattr(status.obstruction_stats, 'fraction_obstructed'):
                obstruction_pct = status.obstruction_stats.fraction_obstructed * 100
            
            # Other metrics
            snr_above_noise = status.is_snr_above_noise_floor if hasattr(status, 'is_snr_above_noise_floor') else False
            uptime_seconds = 0
            if hasattr(status, 'device_state') and hasattr(status.device_state, 'uptime_s'):
                uptime_seconds = status.device_state.uptime_s
            
            gps_valid = False
            gps_satellites = 0
            if hasattr(status, 'gps_stats'):
                gps_valid = status.gps_stats.gps_valid if hasattr(status.gps_stats, 'gps_valid') else False
                gps_satellites = status.gps_stats.gps_sats if hasattr(status.gps_stats, 'gps_sats') else 0
            
            eth_speed_mbps = status.eth_speed_mbps if hasattr(status, 'eth_speed_mbps') else 0
            
            # Calculate quality score
            quality_score = self.calculate_quality_score(
                latency_ms, download_mbps, upload_mbps, obstruction_pct, snr_above_noise
            )
            
            # Check for connection issues
            connection_ok = latency_ms > 0 and latency_ms < 2000  # Reasonable latency range
            
            # Enhanced outage detection
            if not connection_ok and self.last_connection_state:
                # Connection just went down
                self.outage_start_time = timestamp
                current_weather = self.weather_service.get_current_weather()
                weather_conditions = None
                if current_weather:
                    weather_conditions = f"{current_weather.get('weather_condition')} {current_weather.get('temperature_c')}°C"
                
                self.logger.warning("Connection outage detected")
                
            elif connection_ok and not self.last_connection_state and self.outage_start_time:
                # Connection just came back up
                outage_duration = (timestamp - self.outage_start_time).total_seconds()
                
                # Determine severity based on duration
                if outage_duration >= self.outage_severity_threshold['critical']:
                    severity = 'critical'
                elif outage_duration >= self.outage_severity_threshold['major']:
                    severity = 'major'
                else:
                    severity = 'minor'
                
                # Get current weather for context
                current_weather = self.weather_service.get_current_weather()
                weather_conditions = None
                if current_weather:
                    weather_conditions = f"{current_weather.get('weather_condition')} {current_weather.get('temperature_c')}°C"
                
                # Record enhanced outage
                self.db.record_enhanced_outage(
                    start_time=self.outage_start_time,
                    end_time=timestamp,
                    reason="Connection restored",
                    severity=severity,
                    weather_conditions=weather_conditions
                )
                
                self.logger.info(f"Connection restored after {outage_duration:.0f} seconds (severity: {severity})")
                self.outage_start_time = None
            
            self.last_connection_state = connection_ok
            
            # Store the data point
            self.db.insert_metric(
                timestamp=timestamp,
                latency_ms=latency_ms,
                download_mbps=download_mbps,
                upload_mbps=upload_mbps,
                obstruction_pct=obstruction_pct,
                quality_score=quality_score,
                snr_above_noise=snr_above_noise,
                uptime_seconds=uptime_seconds,
                gps_valid=gps_valid,
                gps_satellites=gps_satellites,
                eth_speed_mbps=eth_speed_mbps
            )
            
            # Record performance events
            if quality_score < 50:
                self.db.record_performance_event('poor_performance', {
                    'quality_score': quality_score,
                    'latency_ms': latency_ms,
                    'download_mbps': download_mbps,
                    'upload_mbps': upload_mbps,
                    'obstruction_pct': obstruction_pct
                })
            
            # Log high-speed events (might indicate speed tests)
            if download_mbps > 50 or upload_mbps > 20:
                self.db.record_performance_event('high_speed', {
                    'download_mbps': download_mbps,
                    'upload_mbps': upload_mbps
                })
            
            self.logger.debug(f"Collected: {download_mbps:.1f}Mbps↓ {upload_mbps:.1f}Mbps↑ {latency_ms:.0f}ms Q:{quality_score}%")
            
        except Exception as e:
            self.logger.error(f"Error collecting data point: {e}")
            
            # If we can't get data, it might be an outage
            if self.last_connection_state:
                self.outage_start_time = datetime.now()
                self.last_connection_state = False
    
    def _collection_loop(self):
        """Enhanced data collection loop with weather and performance analysis"""
        self.logger.info(f"Starting enhanced data collection (interval: {self.collection_interval}s)")
        
        while self.running:
            try:
                # Collect main data point
                self.collect_data_point()
                
                # Collect weather data periodically
                self.weather_collection_counter += 1
                if self.weather_collection_counter >= self.weather_collection_interval:
                    self.weather_collection_counter = 0
                    try:
                        weather_data = self.weather_service.collect_and_store_weather()
                        if weather_data:
                            self.logger.debug(f"Weather collected: {weather_data.get('weather_condition')}")
                    except Exception as e:
                        self.logger.warning(f"Weather collection failed: {e}")
                
                # Run performance analysis periodically
                self.performance_analysis_counter += 1
                if self.performance_analysis_counter >= self.performance_analysis_interval:
                    self.performance_analysis_counter = 0
                    self._run_performance_analysis()
                
                # Calculate daily stats periodically (every hour)
                if datetime.now().minute == 0:
                    self.db._calculate_daily_stats()
                
            except Exception as e:
                self.logger.error(f"Error in collection loop: {e}")
                self.consecutive_failures += 1
                
                # If we have too many consecutive failures, it might be a major outage
                if self.consecutive_failures > 5 and not self.outage_start_time:
                    self.outage_start_time = datetime.now()
                    self.last_connection_state = False
                    self.logger.warning("Multiple collection failures - possible major outage")
            else:
                self.consecutive_failures = 0
            
            # Sleep for the specified interval
            for _ in range(self.collection_interval):
                if not self.running:
                    break
                time.sleep(1)
        
        self.logger.info("Enhanced data collection stopped")
    
    def start(self):
        """Start the background data collection"""
        if self.running:
            self.logger.warning("Data collector is already running")
            return
        
        self.running = True
        self.thread = threading.Thread(target=self._collection_loop, daemon=True)
        self.thread.start()
        self.logger.info("Data collector started")
    
    def stop(self):
        """Stop the background data collection"""
        if not self.running:
            return
        
        self.running = False
        if self.thread:
            self.thread.join(timeout=10)
        
        # Record any ongoing outage
        if self.outage_start_time:
            self.db.record_outage(self.outage_start_time, datetime.now(), "System shutdown")
        
        self.logger.info("Data collector stopped")
    
    def _run_performance_analysis(self):
        """Run periodic performance analysis and store insights"""
        try:
            self.logger.debug("Running performance analysis...")
            
            # Get peak usage patterns
            patterns = self.db.analyze_peak_usage_patterns(days=7)
            
            # Log interesting findings
            if patterns.get('best_hours'):
                best_hour = patterns['best_hours'][0]
                self.logger.info(f"Best performance hour: {best_hour.get('hour')}:00 "
                               f"(Quality: {best_hour.get('avg_quality', 0):.0f}%)")
            
            if patterns.get('worst_hours'):
                worst_hour = patterns['worst_hours'][0]
                if worst_hour.get('avg_quality', 100) < 60:
                    self.logger.warning(f"Poor performance hour: {worst_hour.get('hour')}:00 "
                                      f"(Quality: {worst_hour.get('avg_quality', 0):.0f}%)")
            
            # Store hourly performance analysis
            current_hour = datetime.now().hour
            current_date = datetime.now().date()
            
            if patterns.get('hourly_patterns'):
                for hour_data in patterns['hourly_patterns']:
                    if hour_data.get('hour') == current_hour and hour_data.get('sample_count', 0) > 5:
                        # Get weather correlation if available
                        weather_data = self.weather_service.get_current_weather()
                        weather_correlation = None
                        if weather_data:
                            weather_correlation = f"{weather_data.get('weather_condition')} {weather_data.get('temperature_c')}°C"
                        
                        self.db.store_performance_analysis(
                            analysis_date=current_date,
                            hour_of_day=current_hour,
                            avg_download=hour_data.get('avg_download', 0),
                            avg_upload=hour_data.get('avg_upload', 0),
                            avg_latency=hour_data.get('avg_latency', 0),
                            avg_quality=hour_data.get('avg_quality', 0),
                            sample_count=hour_data.get('sample_count', 0),
                            weather_correlation=weather_correlation
                        )
                        break
            
        except Exception as e:
            self.logger.error(f"Performance analysis failed: {e}")
    
    def get_status(self) -> dict:
        """Get enhanced collector status"""
        return {
            'running': self.running,
            'collection_interval': self.collection_interval,
            'last_connection_state': self.last_connection_state,
            'outage_in_progress': self.outage_start_time is not None,
            'outage_start_time': self.outage_start_time.isoformat() if self.outage_start_time else None,
            'consecutive_failures': self.consecutive_failures,
            'weather_enabled': bool(self.weather_service.api_key and 
                                  (self.weather_service.latitude != 0 or self.weather_service.longitude != 0)),
            'next_weather_collection': self.weather_collection_interval - self.weather_collection_counter,
            'next_performance_analysis': self.performance_analysis_interval - self.performance_analysis_counter
        }