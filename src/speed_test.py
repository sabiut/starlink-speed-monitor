import time
import threading
import subprocess
import requests
import json
import socket
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
import logging
import os
import tempfile
from croniter import croniter

class SpeedTestEngine:
    def __init__(self, db):
        self.db = db
        self.logger = logging.getLogger(__name__)
        self.running_tests = {}
        self.scheduler_thread = None
        self.scheduler_running = False
        
    def run_server_speed_test(self, test_type: str = 'manual') -> Dict:
        """Run a comprehensive speed test using multiple methods"""
        test_id = f"test_{int(time.time())}"
        self.running_tests[test_id] = {
            'status': 'running',
            'start_time': datetime.now(),
            'progress': 0
        }
        
        try:
            # Try multiple speed test methods in order of preference
            result = None
            
            # Method 1: Try speedtest-cli if available
            result = self._run_speedtest_cli()
            if result:
                result['method'] = 'speedtest-cli'
            else:
                # Method 2: Try custom HTTP-based speed test
                result = self._run_http_speed_test()
                if result:
                    result['method'] = 'http_test'
                else:
                    # Method 3: Fallback to basic network test
                    result = self._run_basic_network_test()
                    result['method'] = 'basic_test'
            
            # Add test metadata
            result.update({
                'test_id': test_id,
                'test_type': test_type,
                'timestamp': datetime.now(),
                'duration_seconds': (datetime.now() - self.running_tests[test_id]['start_time']).total_seconds()
            })
            
            # Store in database
            self._store_speed_test_result(result)
            
            # Update running test status
            self.running_tests[test_id]['status'] = 'completed'
            self.running_tests[test_id]['result'] = result
            
            return result
            
        except Exception as e:
            self.logger.error(f"Speed test failed: {e}")
            error_result = {
                'test_id': test_id,
                'test_type': test_type,
                'timestamp': datetime.now(),
                'status': 'failed',
                'error_message': str(e),
                'ping_ms': 0,
                'download_mbps': 0,
                'upload_mbps': 0,
                'method': 'failed'
            }
            
            self._store_speed_test_result(error_result)
            self.running_tests[test_id]['status'] = 'failed'
            self.running_tests[test_id]['result'] = error_result
            
            return error_result
    
    def _run_speedtest_cli(self) -> Optional[Dict]:
        """Run speed test using speedtest-cli"""
        try:
            # Check if speedtest-cli is available
            result = subprocess.run(['speedtest-cli', '--version'], 
                                  capture_output=True, text=True, timeout=10)
            if result.returncode != 0:
                return None
            
            # Run the actual speed test
            cmd = ['speedtest-cli', '--json', '--timeout', '60']
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            
            if result.returncode == 0:
                data = json.loads(result.stdout)
                return {
                    'ping_ms': data.get('ping', 0),
                    'download_mbps': data.get('download', 0) / 1_000_000,  # Convert to Mbps
                    'upload_mbps': data.get('upload', 0) / 1_000_000,     # Convert to Mbps
                    'server_location': f"{data.get('server', {}).get('name', 'Unknown')} - {data.get('server', {}).get('country', 'Unknown')}",
                    'status': 'completed',
                    'raw_data': data
                }
        except Exception as e:
            self.logger.warning(f"speedtest-cli failed: {e}")
        
        return None
    
    def _run_http_speed_test(self) -> Optional[Dict]:
        """Run HTTP-based speed test"""
        try:
            start_time = time.time()
            
            # Test download speed using multiple sources
            download_speeds = []
            test_urls = [
                'http://speedtest.ftp.otenet.gr/files/test10Mb.db',
                'http://212.183.159.230/10MB.zip',
                'http://speedtest.belwue.net/10M'
            ]
            
            for url in test_urls:
                try:
                    download_speed = self._test_download_speed(url, timeout=30)
                    if download_speed > 0:
                        download_speeds.append(download_speed)
                        break  # Use first successful test
                except:
                    continue
            
            # Test ping to common servers
            ping_ms = self._test_ping('8.8.8.8')
            
            # Basic upload test (smaller due to limitations)
            upload_mbps = self._test_upload_speed()
            
            avg_download = sum(download_speeds) / len(download_speeds) if download_speeds else 0
            
            return {
                'ping_ms': ping_ms,
                'download_mbps': avg_download,
                'upload_mbps': upload_mbps,
                'server_location': 'HTTP Test Servers',
                'status': 'completed' if avg_download > 0 else 'failed',
                'test_duration_seconds': time.time() - start_time
            }
            
        except Exception as e:
            self.logger.warning(f"HTTP speed test failed: {e}")
        
        return None
    
    def _run_basic_network_test(self) -> Dict:
        """Run basic network connectivity test"""
        ping_ms = self._test_ping('8.8.8.8')
        
        # Very basic speed estimation based on ping and known network characteristics
        # This is a fallback method with limited accuracy
        estimated_download = max(10, min(100, 100 - ping_ms)) if ping_ms > 0 else 0
        estimated_upload = estimated_download * 0.3  # Typical upload ratio
        
        return {
            'ping_ms': ping_ms,
            'download_mbps': estimated_download,
            'upload_mbps': estimated_upload,
            'server_location': 'Basic Network Test',
            'status': 'completed' if ping_ms > 0 else 'failed',
            'note': 'Estimated values based on basic network test'
        }
    
    def _test_download_speed(self, url: str, timeout: int = 30) -> float:
        """Test download speed from a URL"""
        try:
            start_time = time.time()
            
            with requests.get(url, stream=True, timeout=timeout) as response:
                response.raise_for_status()
                
                total_bytes = 0
                for chunk in response.iter_content(chunk_size=8192):
                    total_bytes += len(chunk)
                    # Stop after reasonable amount for speed test
                    if total_bytes > 10 * 1024 * 1024:  # 10MB
                        break
                
                elapsed_time = time.time() - start_time
                if elapsed_time > 0:
                    mbps = (total_bytes * 8) / (elapsed_time * 1_000_000)  # Convert to Mbps
                    return mbps
        
        except Exception as e:
            self.logger.debug(f"Download test failed for {url}: {e}")
        
        return 0
    
    def _test_upload_speed(self) -> float:
        """Test upload speed (simplified)"""
        try:
            # Create test data (1MB)
            test_data = b'0' * (1024 * 1024)
            
            # Use httpbin.org for upload testing
            url = 'https://httpbin.org/post'
            
            start_time = time.time()
            response = requests.post(url, data=test_data, timeout=30)
            elapsed_time = time.time() - start_time
            
            if response.status_code == 200 and elapsed_time > 0:
                mbps = (len(test_data) * 8) / (elapsed_time * 1_000_000)
                return mbps
        
        except Exception as e:
            self.logger.debug(f"Upload test failed: {e}")
        
        return 0
    
    def _test_ping(self, host: str, count: int = 4) -> float:
        """Test ping to a host"""
        try:
            # Use ping command
            cmd = ['ping', '-c', str(count), host]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            
            if result.returncode == 0:
                # Parse ping output to get average
                lines = result.stdout.split('\n')
                for line in lines:
                    if 'avg' in line and '/' in line:
                        # Format: min/avg/max/stddev = x/y/z/w ms
                        parts = line.split('=')[1].strip().split('/')
                        if len(parts) >= 2:
                            return float(parts[1])
            
            # Fallback: basic socket connection test
            start_time = time.time()
            sock = socket.create_connection((host, 80), timeout=10)
            sock.close()
            return (time.time() - start_time) * 1000
            
        except Exception as e:
            self.logger.debug(f"Ping test failed: {e}")
        
        return 0
    
    def _store_speed_test_result(self, result: Dict):
        """Store speed test result in database"""
        try:
            self.db.insert_speed_test(
                timestamp=result.get('timestamp', datetime.now()),
                test_type=result.get('test_type', 'manual'),
                server_location=result.get('server_location', 'Unknown'),
                ping_ms=result.get('ping_ms', 0),
                download_mbps=result.get('download_mbps', 0),
                upload_mbps=result.get('upload_mbps', 0),
                jitter_ms=result.get('jitter_ms', 0),
                packet_loss_pct=result.get('packet_loss_pct', 0),
                test_duration_seconds=int(result.get('duration_seconds', 0)),
                status=result.get('status', 'completed'),
                error_message=result.get('error_message'),
                test_data=result.get('raw_data')
            )
        except Exception as e:
            self.logger.error(f"Failed to store speed test result: {e}")
    
    def get_test_status(self, test_id: str) -> Optional[Dict]:
        """Get status of a running test"""
        return self.running_tests.get(test_id)
    
    def cancel_test(self, test_id: str) -> bool:
        """Cancel a running test"""
        if test_id in self.running_tests:
            self.running_tests[test_id]['status'] = 'cancelled'
            return True
        return False
    
    def start_scheduler(self):
        """Start the speed test scheduler"""
        if self.scheduler_running:
            return
        
        self.scheduler_running = True
        self.scheduler_thread = threading.Thread(target=self._scheduler_loop, daemon=True)
        self.scheduler_thread.start()
        self.logger.info("Speed test scheduler started")
    
    def stop_scheduler(self):
        """Stop the speed test scheduler"""
        self.scheduler_running = False
        if self.scheduler_thread:
            self.scheduler_thread.join(timeout=10)
        self.logger.info("Speed test scheduler stopped")
    
    def _scheduler_loop(self):
        """Main scheduler loop"""
        while self.scheduler_running:
            try:
                # Check for scheduled tests
                schedules = self.db.get_speed_test_schedules(enabled_only=True)
                current_time = datetime.now()
                
                for schedule in schedules:
                    next_run = datetime.fromisoformat(schedule['next_run']) if schedule['next_run'] else None
                    
                    if not next_run:
                        # Calculate next run time
                        cron = croniter(schedule['cron_expression'], current_time)
                        next_run = cron.get_next(datetime)
                        self.db.update_speed_test_schedule(schedule['id'], next_run=next_run)
                        continue
                    
                    if current_time >= next_run:
                        # Run the scheduled test
                        self.logger.info(f"Running scheduled speed test: {schedule['name']}")
                        
                        # Run test in background thread
                        test_thread = threading.Thread(
                            target=self._run_scheduled_test,
                            args=(schedule,),
                            daemon=True
                        )
                        test_thread.start()
                        
                        # Calculate next run time
                        cron = croniter(schedule['cron_expression'], current_time)
                        next_next_run = cron.get_next(datetime)
                        self.db.update_speed_test_schedule(
                            schedule['id'], 
                            last_run=current_time, 
                            next_run=next_next_run
                        )
                
            except Exception as e:
                self.logger.error(f"Scheduler error: {e}")
            
            # Sleep for 60 seconds before checking again
            time.sleep(60)
    
    def _run_scheduled_test(self, schedule: Dict):
        """Run a scheduled speed test"""
        try:
            result = self.run_server_speed_test(test_type='scheduled')
            self.logger.info(f"Scheduled test '{schedule['name']}' completed: {result.get('download_mbps', 0):.1f} Mbps down")
        except Exception as e:
            self.logger.error(f"Scheduled test '{schedule['name']}' failed: {e}")