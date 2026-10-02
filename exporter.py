import pandas as pd
import os
import math
import hashlib
import json
import requests
from datetime import datetime

def export_traffic_mcis(timestamp, mcis, config):
    """
    Append one row of per-sensor MCI readings to the daily MCI CSV, 
    and one row of rounded class readings to the class CSV.
    """
    month_str = timestamp[:6]
    mci_filename = f"traffic_mci_{month_str}.csv"
    class_filename = f"traffic_class_{month_str}.csv"
    
    mci_output_path = os.path.join(config["output_dir"], mci_filename)
    class_output_path = os.path.join(config["output_dir"], class_filename)

    dt = datetime.strptime(timestamp, "%Y%m%d_%H%M%S")
    base_row = {
        "timestamp": timestamp,
        "time": dt.strftime("%H:%M"),
        "day": dt.strftime("%A"),
    }
    
    # MCI Row
    mci_row = dict(base_row)
    mci_row.update({k: ("" if v is None else v) for k, v in mcis.items()})
    
    # Class Row (rounded to nearest integer 1, 2, 3, or 4)
    class_row = dict(base_row)
    class_row.update({k: ("" if v is None else round(v)) for k, v in mcis.items()})

    df_mci = pd.DataFrame([mci_row])
    df_class = pd.DataFrame([class_row])

    # Append mode: check if file exists to decide whether to write header
    mci_header = not os.path.exists(mci_output_path)
    df_mci.to_csv(mci_output_path, mode='a', index=False, header=mci_header)
    
    class_header = not os.path.exists(class_output_path)
    df_class.to_csv(class_output_path, mode='a', index=False, header=class_header)
    
    print(f"  Data appended -> {mci_output_path} and {class_output_path}")


def haversine(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def get_osrm_distances(sensors):
    """
    Fetch road distances (km) for all sensor pairs using OSRM Table API.
    """
    # Format: lon,lat;lon,lat...
    coords = ";".join([
        f"{s.get('path', [{'lon': s.get('lon', 0)}])[0]['lon']},{s.get('path', [{'lat': s.get('lat', 0)}])[0]['lat']}" 
        for s in sensors
    ])
    url = f"http://router.project-osrm.org/table/v1/driving/{coords}?annotations=distance"
    
    headers = {"User-Agent": "DhakaTrafficCollector/1.0"}
    max_retries = 3
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, headers=headers, timeout=60)
            if resp.status_code == 200:
                data = resp.json()
                if "distances" not in data:
                    print("  [!] OSRM response missing 'distances' field.")
                    return None
                # OSRM returns distances in meters
                matrix = [[round(d / 1000.0, 3) for d in row] for row in data["distances"]]
                return matrix
            if resp.status_code == 429:
                import time
                wait_time = (2 ** attempt) + 5
                print(f"  [!] OSRM Table API HTTP 429 (Rate Limit). Retrying in {wait_time}s...")
                time.sleep(wait_time)
                continue
            resp.raise_for_status()
        except Exception as e:
            print(f"  [!] OSRM Request attempt {attempt+1} failed: {e}")
            if attempt < max_retries - 1:
                import time
                time.sleep(2)
                continue
            return None
    return None


def check_binary_adjacency(s1: dict, s2: dict, threshold_km=0.25):
    path1 = s1.get("path", [])
    path2 = s2.get("path", [])
    if not path1 or not path2:
        # Fallback to points if path doesn't exist
        if "lat" in s1 and "lon" in s1 and "lat" in s2 and "lon" in s2:
            return 1 if haversine(s1['lat'], s1['lon'], s2['lat'], s2['lon']) < threshold_km else 0
        return 0

    # Check minimum distance between any two points in the two paths
    min_dist = float('inf')
    for p1 in path1:
        for p2 in path2:
            dist = haversine(p1['lat'], p1['lon'], p2['lat'], p2['lon'])
            if dist < min_dist:
                min_dist = dist
            if min_dist < threshold_km:
                return 1
    return 0


def create_adjacency_matrix(config, force=False):
    """
    Write a sensor × sensor distance matrix (km) to data/adjacency_matrix.csv.
    Uses OSRM for road distance if configured, else Haversine.
    Includes caching to prevent redundant API calls.
    """
    output_dir = config.get("output_dir", "data")
    output_path = os.path.join(output_dir, "adjacency_matrix.csv")
    meta_path = os.path.join(output_dir, "metadata.json")
    
    # 1. Sort sensors North-to-South (Latitude Descending)
    sensors = sorted(config["sensors"], key=lambda x: x.get("path", [{"lat": x.get("lat", 0)}])[0].get("lat", 0), reverse=True)
    names = [s["name"] for s in sensors]
    
    # 2. Generate a fingerprint of the current sensors list
    sensor_fingerprint = hashlib.md5(json.dumps(sensors, sort_keys=True).encode()).hexdigest()
    method = config.get("distance_method", "haversine").lower()
    
    # 3. Check Cache
    if not force and os.path.exists(output_path) and os.path.exists(meta_path):
        with open(meta_path, "r") as f:
            meta = json.load(f)
            if meta.get("sensor_fingerprint") == sensor_fingerprint and meta.get("distance_method") == method:
                # print("  Adjacency matrix is up-to-date (cached).")
                return

    print(f"  Generating Adjacency Matrix ({method})...")
    
    matrix = None
    if method == "osrm":
        matrix = get_osrm_distances(sensors)
        if not matrix:
            print("  Falling back to Haversine due to OSRM failure.")
            method = "haversine"

    if not matrix:
        # Calculate Haversine matrix
        matrix = []
        for i, s1 in enumerate(sensors):
            row = []
            for j, s2 in enumerate(sensors):
                lat1 = s1.get("path", [{"lat": s1.get("lat")}])[0]["lat"]
                lon1 = s1.get("path", [{"lon": s1.get("lon")}])[0]["lon"]
                lat2 = s2.get("path", [{"lat": s2.get("lat")}])[0]["lat"]
                lon2 = s2.get("path", [{"lon": s2.get("lon")}])[0]["lon"]
                dist = haversine(lat1, lon1, lat2, lon2)
                row.append(round(dist, 3))
            matrix.append(row)

    # Save to CSV
    df = pd.DataFrame(matrix, index=names, columns=names)
    os.makedirs(output_dir, exist_ok=True)
    df.to_csv(output_path)
    
    # Build Binary Adjacency Matrix
    bin_matrix = []
    for i, s1 in enumerate(sensors):
        row = []
        for j, s2 in enumerate(sensors):
            if i == j:
                row.append(0)
            else:
                row.append(check_binary_adjacency(s1, s2))
        bin_matrix.append(row)
        
    df_bin = pd.DataFrame(bin_matrix, index=names, columns=names)
    bin_path = os.path.join(output_dir, "binary_adjacency_matrix.csv")
    df_bin.to_csv(bin_path)
    print(f"  Binary adjacency matrix -> {bin_path}")
    
    # Update state metadata
    meta = {}
    if os.path.exists(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8-sig") as f: meta = json.load(f)
        except: pass
        
    meta.update({
        "sensor_fingerprint": sensor_fingerprint,
        "distance_method": method,
        "last_updated_matrix": datetime.now().isoformat(),
        "sensor_count": len(sensors)
    })
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=4)
        
    print(f"  Adjacency matrix -> {output_path} ({method})")
