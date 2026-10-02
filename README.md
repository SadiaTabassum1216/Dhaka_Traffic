# Dhaka Traffic Monitoring System (Path-Based)

This project implements a production-grade, cost-optimized traffic monitoring system for Dhaka using the **Google Maps Tile API**. Unlike traditional point-based sensors, this system uses **Road Path Polylines** to sample traffic conditions across entire road segments, ensuring geographic coverage of the study area.

The system spatially interpolates the paths every 25 meters, scans the map tiles, and logs the traffic color values of each segment.

The system extracts **classified traffic density** by matching pixel colors at each probe point to a 4-class system:
- **Class 1 (Free Flow)**: (Green)
- **Class 2 (Moderate)**: (Yellow/Orange)
- **Class 3 (Congested)**: (Red)
- **Class 4 (Standstill)**: (Dark Red)


## Quick Start

1. **Install Dependencies**:
```powershell
pip install -r requirements.txt
```

2. **Configure API Key**:
Add your key to `config.json` or set the `GOOGLE_MAPS_API_KEY` environment variable.

3. **Start the Collector**:
```powershell
python main.py --mode full
```

4. **Verify Dashboards**:
Start a local web server to bypass CORS restrictions and serve the dashboards:
```powershell
python -m http.server
```
Then navigate to `http://localhost:8000/dashboard.html` or `http://localhost:8000/sensor_map.html` in your browser.
