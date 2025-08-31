# Starlink Monitor 🛰️

A comprehensive real-time monitoring application for your Starlink dish, built with Flask and Docker.

## Features

- **Real-time Speed Monitoring** - Shows actual measured throughput during usage periods
- **Connection Health** - Network latency, obstruction detection, and GPS status  
- **Device Information** - Hardware/software versions, uptime, and system status
- **Smart Speed Detection** - Captures actual speeds during active usage (speed tests, downloads)
- **Auto-refresh Interface** - Updates every 10 seconds with live data
- **Docker Ready** - Easy deployment with Docker Compose

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
Open `http://localhost:5000` in your browser

3. **To see real speeds:**
- If showing low activity, click "Run Speed Test"  
- Run a speed test at fast.com or speedtest.net
- Return to monitor to see actual measured speeds!

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
- Connection uptime
- Hardware/software versions
- Ethernet link speed
- Device status indicators

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