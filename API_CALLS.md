# API Calls Documentation

This document describes the external API calls made by the Dhaka Traffic Congestion Collector.

---

## Active Backend: Google Maps Tile API

The collector fetches map tile images and extracts traffic colour at each sensor's pixel location using pixel-based classification.


### Technical Specification: Zoom 13
Dhaka is ~20km North-to-South. At **Zoom 13**, map tiles provide **~17m/pixel** resolution. This specific resolution ensures:
1.  **Precision**: We sample the color of the *road* segment, avoiding interference from buildings.
2.  **Breadth**: A single tile covers ~4.5km, allowing the 77 sensors to be covered by only 10 tiles.
3.  **Stability**: Higher zoom levels (14+) would require ~4x more tiles, likely exceeding free-tier limits.

### Snapshot Storage
If `"save_snapshots": true` is set in `config.json`, the raw fetched tiles are saved to `data/snapshots/`. These tiles are used for provenance auditing and generating the annotated debug maps.

---

## Technical Workflow

```
1 iteration  →  10 unique tile fetches  →  Continuous path sampling  →  77 MCI values
```

All 77 sensors share tiles. Each unique tile is fetched **once** per iteration regardless of how many sensors fall inside it.

### Step 1 — Create a Session

A session token is required to fetch tiles. This token caches the visual styling (Roadmap + Traffic Layer).

```text
POST https://tile.googleapis.com/v1/createSession?key=YOUR_API_KEY
Content-Type: application/json
```

**Request body:**
```json
{
  "mapType": "roadmap",
  "language": "en-US",
  "region": "BD",
  "layerTypes": ["layerTraffic"]
}
```

**Response Metadata:**
- `session`: Authentication token for subsequent GET requests. (Cached in `data/.tile_session.json`).
- `expiry`: Unix timestamp. The system auto-refreshes before expiry.

### Step 2 — Fetch a 2D Tile

```text
GET https://tile.googleapis.com/v1/2dtiles/{z}/{x}/{y}?session=SESSION&key=API_KEY
```

| Parameter | Value (Current) | Description |
|---|---|---|
| `{z}` | `13` | Zoom level |
| `{x}` | Dynamic | Tile column index |
| `{y}` | Dynamic | Tile row index |

Returns a **256×256 PNG image** of the map segment including road traffic colour overlay.

---

## Computer Vision Logic

### Step 3 — Coordinate Conversion (Mercator)
Each sensor's continuous GPS path is converted to a series of tile addresses $(x, y)$ and pixel coordinates $(px, py)$ using the standard Web Mercator projection, spaced at 25-meter intervals.

### Step 4 — HSV Color Masking and Aggregation
Instead of single-point neighborhood sampling, the system converts the entire fetched tile from RGB to HSV space.

| Label | Map Color | Mean Congestion Index (MCI) |
|:---|:---|:---|
| `free_flow` | Green | 1.0 |
| `moderate` | Yellow / Orange | 2.0 |
| `congested` | Red | 3.0 |
| `standstill` | Dark Red | 4.0 |

**Classification**: Binary masks are generated for each traffic state using precise HSV boundaries. For each sensor, all 25m interval pixels falling along its geographic path are evaluated against these masks. The discrete pixel MCI values (1.0 - 4.0) are then mathematically averaged to compute the final continuous Mean Segment MCI.

---

## Error Handling

| Condition | Behaviour |
|---|---|
| Session Expired | Automatic refresh; tile fetch retried once. |
| HTTP Error (4xx/5xx) | Sensor(s) on that specific tile are logged as `None`. |
| Zero Colour Match | Collector outputs `None` (indicates map text or building pixel was dominant). |
