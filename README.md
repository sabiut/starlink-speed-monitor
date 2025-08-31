# Starlink Speed Monitor 🛰️

A comprehensive real-time monitoring application for your Starlink dish, built with Flask and Docker.

## Features

### 📊 Live Monitoring
- **Real-time Speed Monitoring** - Shows actual measured throughput during usage periods
- **Connection Health** - Network latency, obstruction detection, and GPS status  
- **Device Information** - Hardware/software versions, uptime, and system status
- **Smart Speed Detection** - Captures actual speeds during active usage (speed tests, downloads)
- **Auto-refresh Interface** - Updates every 10 seconds with live data

### ⚡ Built-in Speed Tests
- **Multi-method Speed Testing** - Uses speedtest-cli, HTTP-based tests, and network fallbacks
- **Historical Speed Test Results** - Track performance over time with charts
- **Automated Scheduling** - Set up daily, weekly, or custom speed test schedules
- **Speed Test Analytics** - Compare performance trends and identify patterns

### 📈 Analytics Dashboard  
- **Historical Data Analysis** - View performance trends over days, weeks, and months
- **Interactive Charts** - Real-time graphs of speed, latency, and connection quality
- **Performance Insights** - Automated analysis of your connection patterns
- **Outage Tracking** - Monitor and log connection disruptions

### 🎨 Enhanced Interface
- **Unified Navigation** - Consistent navigation across all pages with active indicators
- **Mobile Responsive** - Works perfectly on phones, tablets, and desktops
- **Location Display** - Shows dish location with reverse geocoding and map links
- **Account Customization** - Set custom account names and location information

### 🐳 Docker Ready
- **Easy Deployment** - One-command setup with Docker Compose
- **Health Monitoring** - Built-in container health checks
- **Persistent Data** - SQLite database with volume mounting
- **Environment Configuration** - Customizable via environment variables

## Prerequisites

- Docker and Docker Compose installed
- Starlink dish accessible on your network (typically at 192.168.100.1)

## Quick Start

1. **Clone and run:**
```bash
git clone <repository-url>
cd starlink-monitor
docker-compose up --build -d
```

2. **Access the monitor:**
Open `http://localhost:8080` in your browser

3. **To see real speeds:**
- If showing low activity, click "Run Speed Test"  
- Run a speed test at fast.com or speedtest.net
- Return to monitor to see actual measured speeds!

## Configuration

You can customize the application using environment variables in `docker-compose.yml`:

```yaml
environment:
  # Optional: Set your account name (default: "Starlink User") 
  - STARLINK_ACCOUNT_NAME=Your Name Here
  
  # Optional: Set location manually (if GPS not available)
  - STARLINK_LOCATION=San Francisco, CA, USA
  # Or set coordinates (will reverse geocode to city/country):
  - STARLINK_LATITUDE=37.7749
  - STARLINK_LONGITUDE=-122.4194
```

**Location Display:**
- Shows in device information with clickable map link
- Auto-detects from dish GPS if available
- Falls back to manual setting if GPS unavailable
- Supports both text location and GPS coordinates

## How It Works

The Starlink API reports **actual data throughput** at any moment, not maximum capacity. This monitor:

- **Detects active usage** periods (>1 Mbps) in the last 5 minutes
- **Shows measured speeds** during those periods
- **Falls back to current activity** when idle
- **Provides speed test guidance** when only showing low usage

## Architecture

- **Flask web server** for the dashboard interface
- **starlink-grpc-tools** for dish communication  
- **Bulk history API** for actual speed measurements
- **Docker containerization** for easy deployment

## Monitoring Data

**Speed Metrics:**
- Download/Upload speeds (averaged over active periods)
- Peak speeds achieved during measurement window
- Current vs historical usage indication

**Connection Health:**
- Network latency to Starlink POP
- Sky obstruction percentage  
- Signal-to-noise ratio status
- GPS satellite tracking (count and validity)

**System Information:**
- Account name (customizable)
- Connection uptime
- Hardware/software versions
- Ethernet link speed
- Device location (with map links)
- GPS coordinates and satellite count
- Device status indicators

**Navigation:**
- 📊 **Live Monitor** - Real-time dashboard with current status
- ⚡ **Speed Test** - Built-in speed testing with history and scheduling  
- 📈 **Analytics** - Historical data analysis and performance trends

## Troubleshooting

**Low Speed Readings:**
- This is normal when not actively using internet
- Run a speed test to see actual capacity
- Monitor shows real data transfer, not maximum capability

**Connection Issues:**
- Ensure dish is at 192.168.100.1
- Check if Starlink app works on same network
- Verify Docker has network access to dish

**Docker Issues:**
```bash
# View logs
docker-compose logs -f

# Restart container
docker-compose restart

# Rebuild if needed
docker-compose up --build -d
```

## Development

The monitor uses live volume mounts for development:
- Edit files in `src/` or `temp3/` 
- Changes reflect immediately (no rebuild needed)
- Just refresh browser after saving

## Credits

Built using the excellent [starlink-grpc-tools](https://github.com/sparky8512/starlink-grpc-tools) project for Starlink dish communication.