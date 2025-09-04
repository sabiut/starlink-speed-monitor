import requests
import os
from datetime import datetime, timedelta
import logging
from typing import Dict, Optional
import time

class WeatherService:
    def __init__(self, db):
        self.db = db
        self.logger = logging.getLogger(__name__)
        
        # Use OpenWeatherMap API (free tier allows 60 calls/minute)
        self.api_key = os.environ.get('WEATHER_API_KEY', '')
        self.base_url = "http://api.openweathermap.org/data/2.5/weather"
        
        # Location from environment or default
        self.latitude = float(os.environ.get('STARLINK_LATITUDE', '0'))
        self.longitude = float(os.environ.get('STARLINK_LONGITUDE', '0'))
        
        # Cache weather data for 10 minutes to avoid API limits
        self.last_fetch = None
        self.cached_data = None
        self.cache_duration = 600  # 10 minutes
    
    def get_current_weather(self) -> Optional[Dict]:
        """Get current weather data"""
        # Check if we have valid cached data
        if (self.cached_data and self.last_fetch and 
            (datetime.now() - self.last_fetch).seconds < self.cache_duration):
            return self.cached_data
        
        if not self.api_key:
            self.logger.warning("No weather API key provided. Set WEATHER_API_KEY environment variable.")
            return None
        
        if self.latitude == 0 and self.longitude == 0:
            self.logger.warning("No location coordinates provided. Set STARLINK_LATITUDE and STARLINK_LONGITUDE.")
            return None
        
        try:
            params = {
                'lat': self.latitude,
                'lon': self.longitude,
                'appid': self.api_key,
                'units': 'metric'  # Celsius, km/h
            }
            
            response = requests.get(self.base_url, params=params, timeout=10)
            response.raise_for_status()
            
            data = response.json()
            
            # Extract relevant weather information
            weather_data = {
                'temperature_c': data.get('main', {}).get('temp'),
                'humidity_pct': data.get('main', {}).get('humidity'),
                'wind_speed_kmh': data.get('wind', {}).get('speed', 0) * 3.6,  # Convert m/s to km/h
                'wind_direction': self._get_wind_direction(data.get('wind', {}).get('deg', 0)),
                'weather_condition': data.get('weather', [{}])[0].get('main', 'Unknown'),
                'precipitation_mm': data.get('rain', {}).get('1h', 0) + data.get('snow', {}).get('1h', 0),
                'visibility_km': data.get('visibility', 10000) / 1000,  # Convert m to km
                'pressure_hpa': data.get('main', {}).get('pressure'),
                'cloud_coverage_pct': data.get('clouds', {}).get('all', 0)
            }
            
            # Cache the data
            self.cached_data = weather_data
            self.last_fetch = datetime.now()
            
            return weather_data
            
        except requests.RequestException as e:
            self.logger.error(f"Failed to fetch weather data: {e}")
            return None
        except Exception as e:
            self.logger.error(f"Error processing weather data: {e}")
            return None
    
    def _get_wind_direction(self, degrees: float) -> str:
        """Convert wind direction degrees to cardinal direction"""
        directions = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE',
                     'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW']
        
        # Normalize to 0-360 and calculate index
        degrees = degrees % 360
        index = round(degrees / 22.5) % 16
        return directions[index]
    
    def collect_and_store_weather(self):
        """Collect current weather and store in database"""
        weather_data = self.get_current_weather()
        
        if weather_data:
            try:
                self.db.store_weather_data(
                    timestamp=datetime.now(),
                    temperature_c=weather_data.get('temperature_c'),
                    humidity_pct=weather_data.get('humidity_pct'),
                    wind_speed_kmh=weather_data.get('wind_speed_kmh'),
                    wind_direction=weather_data.get('wind_direction'),
                    weather_condition=weather_data.get('weather_condition'),
                    precipitation_mm=weather_data.get('precipitation_mm'),
                    visibility_km=weather_data.get('visibility_km')
                )
                
                self.logger.debug(f"Stored weather data: {weather_data.get('weather_condition')}, "
                                f"{weather_data.get('temperature_c')}°C")
                return weather_data
                
            except Exception as e:
                self.logger.error(f"Failed to store weather data: {e}")
        
        return None
    
    def get_weather_impact_analysis(self, days: int = 7) -> Dict:
        """Analyze how weather conditions impact Starlink performance"""
        try:
            start_time = datetime.now() - timedelta(days=days)
            
            # Get weather correlation from database
            correlation_data = self.db.get_weather_correlation(days)
            
            # Generate insights
            insights = []
            
            for condition in correlation_data.get('weather_correlation', []):
                quality_impact = condition.get('avg_quality', 0)
                
                if quality_impact < 60:
                    insights.append({
                        'condition': condition.get('weather_condition'),
                        'impact': 'negative',
                        'quality_score': quality_impact,
                        'recommendation': f"Poor performance during {condition.get('weather_condition').lower()} conditions"
                    })
                elif quality_impact > 85:
                    insights.append({
                        'condition': condition.get('weather_condition'),
                        'impact': 'positive', 
                        'quality_score': quality_impact,
                        'recommendation': f"Excellent performance during {condition.get('weather_condition').lower()} conditions"
                    })
            
            # Check severe weather impact
            impact_analysis = correlation_data.get('impact_analysis', {})
            
            if impact_analysis.get('heavy_rain_quality', 100) < impact_analysis.get('normal_quality', 100):
                rain_impact = impact_analysis.get('normal_quality', 100) - impact_analysis.get('heavy_rain_quality', 100)
                if rain_impact > 10:
                    insights.append({
                        'condition': 'Heavy Rain',
                        'impact': 'negative',
                        'quality_impact': rain_impact,
                        'recommendation': 'Heavy rain significantly impacts performance'
                    })
            
            if impact_analysis.get('high_wind_quality', 100) < impact_analysis.get('normal_wind_quality', 100):
                wind_impact = impact_analysis.get('normal_wind_quality', 100) - impact_analysis.get('high_wind_quality', 100)
                if wind_impact > 5:
                    insights.append({
                        'condition': 'High Wind',
                        'impact': 'negative',
                        'quality_impact': wind_impact,
                        'recommendation': 'High winds may affect dish stability'
                    })
            
            return {
                'analysis_period': days,
                'weather_insights': insights,
                'correlation_data': correlation_data
            }
            
        except Exception as e:
            self.logger.error(f"Error analyzing weather impact: {e}")
            return {'analysis_period': days, 'weather_insights': [], 'error': str(e)}
    
    def get_weather_forecast_impact(self) -> Dict:
        """Get weather forecast and predict potential performance impact"""
        if not self.api_key or (self.latitude == 0 and self.longitude == 0):
            return {'forecast': [], 'impact_prediction': 'Weather data unavailable'}
        
        try:
            # Use 5-day forecast API
            forecast_url = "http://api.openweathermap.org/data/2.5/forecast"
            params = {
                'lat': self.latitude,
                'lon': self.longitude,
                'appid': self.api_key,
                'units': 'metric',
                'cnt': 8  # Next 24 hours (3-hour intervals)
            }
            
            response = requests.get(forecast_url, params=params, timeout=10)
            response.raise_for_status()
            
            data = response.json()
            forecast = []
            impact_warnings = []
            
            for item in data.get('list', []):
                weather_info = {
                    'datetime': datetime.fromtimestamp(item.get('dt')),
                    'temperature_c': item.get('main', {}).get('temp'),
                    'weather_condition': item.get('weather', [{}])[0].get('main'),
                    'wind_speed_kmh': item.get('wind', {}).get('speed', 0) * 3.6,
                    'precipitation_mm': item.get('rain', {}).get('3h', 0) + item.get('snow', {}).get('3h', 0)
                }
                
                forecast.append(weather_info)
                
                # Predict impact based on conditions
                if weather_info['precipitation_mm'] > 5:
                    impact_warnings.append({
                        'time': weather_info['datetime'].strftime('%H:%M'),
                        'warning': 'Heavy precipitation expected - may impact performance'
                    })
                
                if weather_info['wind_speed_kmh'] > 40:
                    impact_warnings.append({
                        'time': weather_info['datetime'].strftime('%H:%M'),
                        'warning': 'High winds expected - dish stability may be affected'
                    })
            
            impact_prediction = 'Good conditions expected' if not impact_warnings else f"{len(impact_warnings)} potential issues"
            
            return {
                'forecast': forecast,
                'impact_prediction': impact_prediction,
                'warnings': impact_warnings
            }
            
        except Exception as e:
            self.logger.error(f"Error getting weather forecast: {e}")
            return {'forecast': [], 'impact_prediction': 'Forecast unavailable', 'error': str(e)}