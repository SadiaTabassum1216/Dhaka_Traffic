"""
tile_collector.py — Google Maps Tile API traffic speed collector

Workflow per iteration:
  1. Compute which map tiles cover each sensor (Mercator projection).
  2. Fetch each unique tile ONCE via the Maps Tile API.
  3. Sample a pixel neighbourhood at each sensor's location.
  4. Map the dominant traffic colour to an approximate speed (km/h).
  5. Return {sensor_name: speed_kmh} — same interface as routes_collector.

Cost (as of 2026-03-23):
  Maps Tile API: $2 / 1,000 tile requests.
  With $200/month free credit that equals 100,000 free tiles/month.
  At zoom 13 Dhaka needs ~10–15 unique tiles/iteration:
    15 × 8,640 iter/month = 129,600 tiles → $59.20 after the credit → $0 net
    if total platform spend ≤ $200/month (Tile API only usage).

Prerequisites:
  • Enable 'Map Tiles API' in your Google Cloud Console project.
  • The same API key used for Distance Matrix / Routes API works here.

Dependencies: requests, Pillow, numpy
"""

import json
import math
import os
import time
from datetime import datetime, timezone, timedelta
tz_dhaka = timezone(timedelta(hours=6))
from io import BytesIO

import cv2
import numpy as np
import requests
from PIL import Image

# ---------------------------------------------------------------------------
# OpenCV Computer Vision Config
# ---------------------------------------------------------------------------

TRAFFIC_CLASSES = {
    "free_flow": {"mci": 1.0, "color": (22, 224, 152)},
    "moderate": {"mci": 2.0, "color": (255, 219, 0)},     # Yellow
    "slow": {"mci": 2.0, "color": (255, 135, 0)},         # Orange mapped to 2.0
    "congested": {"mci": 3.0, "color": (224, 22, 22)},
    "standstill": {"mci": 4.0, "color": (168, 0, 0)},
}

# (lower_hsv, upper_hsv) values (OpenCV Hue goes 0-179, S 0-255, V 0-255)
TRAFFIC_HSV_RANGES = {
    "free_flow": [ 
        ([40, 50, 50], [80, 255, 255]) 
    ],
    "moderate": [
        ([20, 100, 100], [35, 255, 255])
    ],
    "slow": [
        ([10, 100, 100], [20, 255, 255])
    ],
    "congested": [
        # Standard Red
        ([0, 100, 190], [10, 255, 255]),
        ([170, 100, 190], [179, 255, 255])
    ],
    "standstill": [
        # Dark Red
        ([0, 100, 20], [10, 255, 189]),
        ([170, 100, 20], [179, 255, 189])
    ]
}


TRAFFIC_REFS = [
    ("free_flow",  (22, 224, 152), 1.0),    # For autofix_coordinates.py only
    ("moderate",   (255, 219, 0),  2.0),
    ("slow",       (255, 135, 0),  2.0),
    ("congested",  (224, 22, 22),  3.0),
    ("standstill", (168, 0, 0),    4.0),
]
COLOR_THRESHOLD = 60  # For autofix_coordinates.py only

# Maps Tile API strict policy requires a referer header
_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "http://localhost/"
}

# Maps Tile API base URL
_API_BASE = "https://tile.googleapis.com/v1"

# Session cache — stored in output_dir so it persists across runs
_SESSION_FILE = os.path.join("data", ".tile_session.json")


# ---------------------------------------------------------------------------
# Mercator projection helpers
# ---------------------------------------------------------------------------

def _world_coords(lat: float, lon: float, zoom: int):
    """Return floating-point world pixel coordinates at the given zoom level."""
    n = 2 ** zoom
    lat_rad = math.radians(lat)
    wx = (lon + 180.0) / 360.0 * n
    wy = (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n
    return wx, wy


def lat_lon_to_tile_and_pixel(lat: float, lon: float, zoom: int):
    """
    Convert geographic coordinates to:
      tile_x, tile_y   — which 256×256 tile the point falls in
      pixel_x, pixel_y — pixel offset *within* that tile (0–255)
    """
    wx, wy = _world_coords(lat, lon, zoom)
    tile_x = int(wx)
    tile_y = int(wy)
    pixel_x = int((wx - tile_x) * 256)
    pixel_y = int((wy - tile_y) * 256)
    n = 2 ** zoom
    tile_x = max(0, min(tile_x, n - 1))
    tile_y = max(0, min(tile_y, n - 1))
    pixel_x = max(0, min(pixel_x, 255))
    pixel_y = max(0, min(pixel_y, 255))
    return tile_x, tile_y, pixel_x, pixel_y


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

def _load_cached_session() -> str | None:
    """Return a valid cached session token, or None if missing/expired."""
    if not os.path.exists(_SESSION_FILE):
        return None
    try:
        with open(_SESSION_FILE, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        expiry_str = data.get("expiry", "")
        if expiry_str:
            # expiry is Unix timestamp (seconds) returned by the API
            expiry_ts = float(expiry_str)
            if datetime.now(timezone.utc).timestamp() < expiry_ts - 300:
                return data["session"]
    except Exception:
        pass
    return None


def _save_session(session: str, expiry_str: str) -> None:
    os.makedirs(os.path.dirname(_SESSION_FILE) or ".", exist_ok=True)
    with open(_SESSION_FILE, "w") as f:
        json.dump({"session": session, "expiry": expiry_str}, f)


def create_session(api_key: str) -> str:
    """
    Create a Maps Tile API session (roadmap type).
    Returns the session token.

    The roadmap tile type renders Google Maps traffic colours when the
    traffic layer is active on the platform — no extra parameter required
    beyond mapType=roadmap.  If tiles appear without traffic colour, ensure
    'Map Tiles API' is enabled in Google Cloud Console.
    """
    url = f"{_API_BASE}/createSession?key={api_key}"
    payload = {
        "mapType": "roadmap",
        "language": "en-US",
        "region": "BD",
        "layerTypes": ["layerTraffic"],
        "styles": [
            {
                "elementType": "labels",
                "stylers": [{"visibility": "off"}]
            },
            {
                "featureType": "poi",
                "stylers": [{"visibility": "off"}]
            },
            {
                "featureType": "transit",
                "stylers": [{"visibility": "off"}]
            },
            {
                "featureType": "road",
                "stylers": [{"weight": 6}]
            }
        ]
    }
    
    headers = dict(_HTTP_HEADERS)
    headers["Content-Type"] = "application/json"

    max_retries = 5
    for attempt in range(max_retries):
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=15)
            if response.status_code == 200:
                break
            if response.status_code == 429:
                import random
                wait_time = (2 ** attempt) + random.random() * 2.0
                print(f"  [!] Session creation HTTP 429 (Rate Limit). Retrying in {wait_time:.2f}s...")
                time.sleep(wait_time)
                continue
            # Other errors
            raise RuntimeError(f"Session creation failed HTTP {response.status_code}: {response.text[:400]}")
        except requests.exceptions.RequestException as exc:
            if attempt == max_retries - 1:
                raise RuntimeError(f"Session creation request failed after {max_retries} attempts: {exc}") from exc
            time.sleep(2)
            continue
    else:
        raise RuntimeError(f"Session creation failed after {max_retries} attempts (429).")

    data = response.json()
    session = data.get("session", "")
    expiry = str(data.get("expiry", ""))
    if not session:
        raise RuntimeError(f"No session token in response: {data}")
    _save_session(session, expiry)
    print(f"  New tile session created (expires: {expiry})")
    return session


def get_or_create_session(api_key: str) -> str:
    """Return a valid session token, creating one if the cache is stale."""
    cached = _load_cached_session()
    if cached:
        return cached
    return create_session(api_key)


# ---------------------------------------------------------------------------
# Tile fetching
# ---------------------------------------------------------------------------

def fetch_tile(tile_x: int, tile_y: int, zoom: int, session: str, api_key: str):
    """
    Download a 256×256 PNG map tile.
    Returns a numpy uint8 array shaped (256, 256, 3), or None on error.
    """
    url = f"{_API_BASE}/2dtiles/{zoom}/{tile_x}/{tile_y}?session={session}&key={api_key}"
    
    # --- Local Fallback Check (Faster & Free) ---
    snap_dir = os.path.join("data", "snapshots")
    if os.path.exists(snap_dir):
        pattern = f"tile_{zoom}_{tile_x}_{tile_y}"
        snaps = [f for f in os.listdir(snap_dir) if f.startswith(pattern) and f.endswith(".png")]
        if snaps:
            latest = sorted(snaps, reverse=True)[0]
            snap_path = os.path.join(snap_dir, latest)
            try:
                img = Image.open(snap_path).convert("RGB")
                print(f"    [+] Using local snapshot: {latest}")
                return np.array(img, dtype=np.uint8)
            except Exception: pass
    # --------------------------------------------

    # --- Robust Retrying for Month-Long Unattended Runs ---
    attempt = 0
    while True:
        try:
            resp = requests.get(
                url,
                headers=_HTTP_HEADERS,
                timeout=15,
            )
        except requests.exceptions.RequestException as exc:
            print(f"    [!] Tile ({tile_x},{tile_y}) network error: {exc}")
            attempt += 1
            time.sleep(min(30, 2 ** attempt))
            continue

        if resp.status_code == 401:
            return "EXPIRED"

        if resp.status_code == 429:
            import random
            wait_time = min(300, (2 ** attempt) + 10 + random.random() * 5.0)
            print(f"    [!] Tile ({tile_x},{tile_y}) HTTP 429 (Rate Limit). Pausing {wait_time:.1f}s for reset...")
            time.sleep(wait_time)
            attempt += 1
            # Dynamically increase global pacing to be safer
            current_pacing = float(os.environ.get("TILE_FETCH_DELAY", 1.0))
            os.environ["TILE_FETCH_DELAY"] = str(min(10.0, current_pacing + 0.5))
            continue

        if resp.status_code != 200:
            print(f"    [!] Tile ({tile_x},{tile_y}) HTTP {resp.status_code} - Skipping")
            return None

        try:
            img = Image.open(BytesIO(resp.content)).convert("RGB")
            return np.array(img, dtype=np.uint8)
        except Exception as exc:
            print(f"    [!] Tile decode error: {exc}")
            return None


def fetch_all_tiles(
    unique_tiles: set, zoom: int, session: str, api_key: str
) -> dict:
    """
    Fetch all unique (tile_x, tile_y) tiles. If a 401 is received the session
    is refreshed once and the failing tile is retried.
    Returns {(tile_x, tile_y): np.array or None}.
    """
    cache = {}
    for tile_x, tile_y in unique_tiles:
        result = fetch_tile(tile_x, tile_y, zoom, session, api_key)
        if isinstance(result, str) and result == "EXPIRED":
            print("  Session expired — refreshing...")
            try:
                session = create_session(api_key)
                result = fetch_tile(tile_x, tile_y, zoom, session, api_key)
            except RuntimeError as e:
                print(f"    [!] Session refresh failed: {e}")
                result = None
        
        # Ensure we don't store the "EXPIRED" string if retry also failed or something
        if isinstance(result, str):
            result = None
            
        cache[(tile_x, tile_y)] = result
        
        # Pacing: delay between tiles to avoid hitting QPS limits
        pacing = float(os.environ.get("TILE_FETCH_DELAY", 1.0))
        import time
        time.sleep(pacing)   # increased baseline pacing — ~1 req/s default
    return cache


# ---------------------------------------------------------------------------
# Colour analysis (OpenCV Pipeline)
# ---------------------------------------------------------------------------

def process_tile_cv(img_rgb, save_debug_images=False, debug_prefix=""):
    """
    Apply pure computer vision background removal and HSV bounding to isolate roads.
    Morphological operations remove small artifacts like text.
    """
    img_hsv = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2HSV)
    masks = {}
    
    # 3x3 circular kernel
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    
    for label, ranges in TRAFFIC_HSV_RANGES.items():
        combined_mask = np.zeros(img_hsv.shape[:2], dtype=np.uint8)
        for (lower, upper) in ranges:
            lower_np = np.array(lower, dtype=np.uint8)
            upper_np = np.array(upper, dtype=np.uint8)
            mask = cv2.inRange(img_hsv, lower_np, upper_np)
            combined_mask = cv2.bitwise_or(combined_mask, mask)
        
        # Morphological Opening: erode then dilate to wipe small text/icons
        cleaned_mask = cv2.morphologyEx(combined_mask, cv2.MORPH_OPEN, kernel)
        masks[label] = cleaned_mask
        
    if save_debug_images and debug_prefix:
        # Save a visualization where only detected lines are bright over a dimmed original
        debug_img = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2BGR)
        debug_img = cv2.addWeighted(debug_img, 0.2, np.zeros_like(debug_img), 0.8, 0)
        for label, mask_ in masks.items():
            color_bgr = TRAFFIC_CLASSES[label]["color"][::-1] # RGB to BGR
            debug_img[mask_ > 0] = color_bgr
            
        cv2.imwrite(f"{debug_prefix}_cvmask.png", debug_img)
        cv2.imwrite(f"{debug_prefix}_orig.png", cv2.cvtColor(img_rgb, cv2.COLOR_RGB2BGR))
        
    return masks


def classify_pixel_cv(masks, px, py, radius=7):
    """
    Finds the best traffic classification for a given pixel by:
    1. Checking a 3x3 modal vote at the exact center (robust to anti-aliasing).
    2. Falling back to a radial search with geometric nearest-neighbor logic if no center match.
    """
    
    # --- Step 1: 3x3 Modal Vote at the exact center ---
    votes = {}
    for label, mask in masks.items():
        # Crop 3x3 around center
        y_m = max(0, py - 1); y_p = min(255, py + 1)
        x_m = max(0, px - 1); x_p = min(255, px + 1)
        roi = mask[y_m : y_p + 1, x_m : x_p + 1]
        count = np.sum(roi > 0)
        if count > 0:
            votes[label] = count
            
    if votes:
        # Pick label with most votes in the 3x3
        best_label = max(votes, key=votes.get)
        return best_label, TRAFFIC_CLASSES[best_label]["mci"]

    # --- Step 2: Radial search with nearest-neighbor distance ---
    best_dist = float('inf')
    best_label = None
    
    y_min = max(0, py - radius)
    y_max = min(255, py + radius)
    x_min = max(0, px - radius)
    x_max = min(255, px + radius)
    
    for label, mask in masks.items():
        roi = mask[y_min:y_max+1, x_min:x_max+1]
        active_points = np.argwhere(roi > 0)
        
        if len(active_points) == 0:
            continue
            
        for pt in active_points:
            gy, gx = pt[0] + y_min, pt[1] + x_min
            dist = math.hypot(gx - px, gy - py)
            if dist < best_dist:
                best_dist = dist
                best_label = label
                
    if best_label and best_dist <= radius:
        return best_label, TRAFFIC_CLASSES[best_label]["mci"]
        
    return None, None


# ---------------------------------------------------------------------------
# Sensor grouping
# ---------------------------------------------------------------------------

def haversine_m(lat1, lon1, lat2, lon2):
    import math
    R = 6371000.0 # meters
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2)**2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2)**2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def interpolate_path(path_points, spacing_m):
    if not path_points: return []
    if len(path_points) == 1: return path_points
    
    sampled = [path_points[0]]
    for i in range(len(path_points) - 1):
        p1, p2 = path_points[i], path_points[i+1]
        dist = haversine_m(p1['lat'], p1['lon'], p2['lat'], p2['lon'])
        if dist < spacing_m:
            continue
            
        steps = int(dist // spacing_m)
        if steps <= 0: continue
        
        for j in range(1, steps + 1):
            f = j / steps
            lat = p1['lat'] + (p2['lat'] - p1['lat']) * f
            lon = p1['lon'] + (p2['lon'] - p1['lon']) * f
            sampled.append({'lat': lat, 'lon': lon})
            
    sampled.append(path_points[-1])
    return sampled

def build_sensor_tile_map(config: dict) -> dict:
    '''
    For each sensor return a list of (tile_x, tile_y, pixel_x, pixel_y) points.
    Returns {sensor_name: [(tx, ty, px, py), ...]}.
    '''
    zoom = int(config.get("tile_zoom_level", 13))
    spacing = float(config.get("segment_sample_spacing_m", 25.0))
    mapping = {}
    
    for sensor in config["sensors"]:
        path = sensor.get("path", [])
        if not path and "lat" in sensor and "lon" in sensor:
            path = [{"lat": sensor["lat"], "lon": sensor["lon"]}]
            
        sampled = interpolate_path(path, spacing)
        points = []
        for p in sampled:
            tx, ty, px, py = lat_lon_to_tile_and_pixel(p["lat"], p["lon"], zoom)
            points.append((tx, ty, px, py))
            
        mapping[sensor["name"]] = points
    return mapping


def estimate_tile_calls_per_iteration(config: dict) -> int:
    '''Return the count of unique tiles needed per iteration.'''
    mapping = build_sensor_tile_map(config)
    unique_tiles = set()
    for pts in mapping.values():
        for tx, ty, px, py in pts:
            unique_tiles.add((tx, ty))
    return len(unique_tiles)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

# Predefined highly distinct BGR colors (converted from Tab20 RGB)
DISTINCT_COLORS_BGR = [
    (180, 119, 31), (14, 127, 255), (44, 160, 44), (40, 39, 214),
    (189, 103, 148), (75, 86, 140), (194, 119, 227), (127, 127, 127),
    (34, 189, 188), (207, 190, 23), (232, 199, 174), (120, 187, 255),
    (138, 223, 152), (150, 152, 255), (213, 176, 197), (148, 156, 196),
    (210, 182, 247), (199, 199, 199), (141, 219, 219), (229, 218, 158)
]

def draw_sensor_overviews(tile_cache, tile_map, config, dbg_dir):
    """
    Generate 'tile_overview_{tx}_{ty}.png' files showing the full road paths 
    for every sensor that touches that tile, including the search radius corridor.
    """
    radius = int(config.get("tile_sample_radius", 7))
    SCALE = 3
    for (tx, ty), img in tile_cache.items():
        if img is None: continue
        
        # Process the tile to get the traffic color masks
        masks = process_tile_cv(img, save_debug_images=False)
        
        # Create a dimmed original image for context
        base_img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
        base_img = cv2.addWeighted(base_img, 0.2, np.zeros_like(base_img), 0.8, 0)
        
        # Apply the bright traffic color masks
        for label, mask_ in masks.items():
            color_bgr = TRAFFIC_CLASSES[label]["color"][::-1] # RGB to BGR
            base_img[mask_ > 0] = color_bgr
            
        # Upscale for drawing overlays
        base_img = cv2.resize(base_img, (0, 0), fx=SCALE, fy=SCALE, interpolation=cv2.INTER_NEAREST)
        
        overlay = base_img.copy()
        
        occupied_boxes = [] # Track (x, y, w, h) of drawn text
        
        # Find sensors that have points on this tile
        color_idx = 0
        for s_name, pts in tile_map.items():
            tile_pts = [np.array([p[2] * SCALE, p[3] * SCALE]) for p in pts if p[0] == tx and p[1] == ty]
            if not tile_pts: continue
            
            # Pick a unique distinct color for this sensor path
            path_color = DISTINCT_COLORS_BGR[color_idx % len(DISTINCT_COLORS_BGR)]
            color_idx += 1
            
            # 1. Draw the "Search Corridor" on overlay using the distinct color
            polyline = np.array(tile_pts, dtype=np.int32).reshape((-1, 1, 2))
            cv2.polylines(overlay, [polyline], isClosed=False, color=path_color, thickness=radius*2*SCALE)
            
            # 2. Draw the core path line (black for contrast)
            cv2.polylines(base_img, [polyline], isClosed=False, color=(0, 0, 0), thickness=max(1, int(1.5*SCALE)))
            
            # 3. Draw a circle for EVERY sample point
            for pt in tile_pts:
                cv2.circle(base_img, tuple(pt), max(2, int(1.2*SCALE)), path_color, -1)
                
            # Draw a larger circle at the start node on this tile
            cv2.circle(base_img, tuple(tile_pts[0]), max(4, int(2.5*SCALE)), (255, 255, 255), -1)
            cv2.circle(base_img, tuple(tile_pts[0]), max(3, int(2.0*SCALE)), path_color, -1)
            
            # 4. Label the sensor with Anti-Overlap & Boundary Logic
            text = s_name.replace("_", " ")
            font = cv2.FONT_HERSHEY_SIMPLEX
            font_scale = 0.6
            thickness = 2
            (tw, th), baseline = cv2.getTextSize(text, font, font_scale, thickness)
            
            img_h, img_w = base_img.shape[:2]
            
            # Initial preferred position
            x, y = int(tile_pts[0][0]) + 15, int(tile_pts[0][1]) + 5
            
            # Hard clamp initially
            x = max(8, min(x, img_w - tw - 8))
            y = max(th + 8, min(y, img_h - 8))
            
            # Collision avoidance loop
            for _ in range(50): # Max 50 shifts
                collision = False
                
                # Current bounding box limits
                curr_l, curr_r = x - 4, x + tw + 4
                curr_t, curr_b = y - th - 4, y + 4
                
                for (ox, oy, otw, oth) in occupied_boxes:
                    ol, or_ = ox - 4, ox + otw + 4
                    ot, ob = oy - oth - 4, oy + 4
                    # Intersect check
                    if not (curr_r < ol or curr_l > or_ or curr_b < ot or curr_t > ob):
                        collision = True
                        break
                        
                if not collision:
                    break
                    
                y += th + 10 # Shift down
                
                # Check bottom boundary
                if y + 4 > img_h:
                    y = th + 8 # Wrap to top
                    x -= (tw + 15) # Shift left by a full label width
                    
                # Hard clamp again so it can never be invalid
                x = max(8, min(x, img_w - tw - 8))
                y = max(th + 8, min(y, img_h - 8))
                
            occupied_boxes.append((x, y, tw, th))
            
            # Draw white background box
            cv2.rectangle(base_img, (x - 4, y - th - 4), (x + tw + 4, y + 4), (255, 255, 255), -1)
            # Draw thick distinct colored border matching the path!
            cv2.rectangle(base_img, (x - 4, y - th - 4), (x + tw + 4, y + 4), path_color, 2)
            
            # Draw text
            cv2.putText(base_img, text, (x, y), font, font_scale, (0, 0, 0), thickness, cv2.LINE_AA)
            
        # Combine base image with the semi-transparent overlay
        res_img = cv2.addWeighted(overlay, 0.3, base_img, 0.7, 0)
        
        out_path = os.path.join(dbg_dir, f"tile_overview_{tx}_{ty}.png")
        cv2.imwrite(out_path, res_img)

def collect_traffic_speeds_tiles(config: dict, api_key: str, return_labels: bool = False) -> tuple:
    '''
    Collect per-sensor traffic speeds via tile image colour analysis.

    Returns:
        speeds          : {sensor_name: speed_kmh (float) or label (str) or None}
        success_calls   : int  sensors with a valid colour match
        attempted_calls : int  total sensors
    '''
    sensors = config["sensors"]
    zoom    = int(config.get("tile_zoom_level", 13))
    radius  = int(config.get("tile_sample_radius", 7))

    tile_map = build_sensor_tile_map(config)
    unique_tiles = set()
    for pts in tile_map.values():
        for tx, ty, px, py in pts:
            unique_tiles.add((tx, ty))

    print(f"  Fetching {len(unique_tiles)} tile(s) at zoom {zoom}...")

    try:
        session = get_or_create_session(api_key)
    except RuntimeError as exc:
        print(f"    [!] Tile API session error: {exc}")
        return {s["name"]: None for s in sensors}, 0, len(sensors)

    tile_cache = fetch_all_tiles(unique_tiles, zoom, session, api_key)

    speeds: dict        = {}
    success_calls: int  = 0
    attempted_calls     = len(sensors)

    # Pre-process ALL fetched tiles with Computer Vision
    save_dbg = config.get("save_debug_images", True)
    import time
    dbg_dir = os.path.join(config.get("output_dir", "data"), "sensor_debug")
    if save_dbg:
        os.makedirs(dbg_dir, exist_ok=True)
        
    processed_tiles = {}
    for (tx, ty), img in tile_cache.items():
        if img is not None:
            prefix = os.path.join(dbg_dir, f"tile_{tx}_{ty}_{int(time.time())}") if save_dbg else ""
            masks = process_tile_cv(img, save_debug_images=save_dbg, debug_prefix=prefix)
            processed_tiles[(tx, ty)] = masks
            
            # Save original snapshot if needed
            if config.get("save_snapshots", False):
                snap_dir = os.path.join(config.get("output_dir", "data"), "snapshots")
                os.makedirs(snap_dir, exist_ok=True)
                from datetime import datetime
                # Need to use correct timezone but for patching simplicity let's use built-in format
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                snap_path = os.path.join(snap_dir, f"tile_{zoom}_{tx}_{ty}_{timestamp}.png")
                Image.fromarray(img).save(snap_path)

    if save_dbg:
        draw_sensor_overviews(tile_cache, tile_map, config, dbg_dir)
        
    for sensor in sensors:
        name = sensor["name"]
        pts = tile_map.get(name, [])

        if not pts:
            speeds[name] = None
            continue

        pt_mcis = []
        pt_labels = []
        for tx, ty, px, py in pts:
            masks = processed_tiles.get((tx, ty))
            if masks is None:
                continue

            # Use CV classifier with geometric distance
            label, mci_val = classify_pixel_cv(masks, px, py, radius)
            if mci_val is not None:
                pt_mcis.append(mci_val)
                pt_labels.append(label)

        if pt_mcis:
            if return_labels:
                from collections import Counter
                dominant_label = Counter(pt_labels).most_common(1)[0][0]
                speeds[name] = dominant_label
                success_calls += 1
                print(f"    - {name:20}: {dominant_label} [{len(pt_mcis)}/{len(pts)} pts]")
            else:
                avg_mci = round(sum(pt_mcis) / len(pt_mcis), 2)
                speeds[name] = avg_mci
                success_calls += 1
                print(f"    - {name:20}: MCI {avg_mci:4.2f} [{len(pt_mcis)}/{len(pts)} pts]")
        else:
            speeds[name] = None
            print(f"    - {name:20}: [FAILED] Zero matches (Tile blank)")

    return speeds, success_calls, attempted_calls
