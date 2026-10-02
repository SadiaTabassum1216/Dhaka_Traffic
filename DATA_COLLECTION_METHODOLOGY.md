# Methodology: High-Precision Dhaka Traffic Congestion Index

## 1. Overview
This document details the technical methodology used to collect a high-resolution, time-series traffic congestion dataset for Dhaka, Bangladesh. The system leverages the **Google Maps Tile API (v1)** to extract traffic density metadata via **Path-Based Computer Vision**. Unlike traditional point-based sensors, this system samples traffic along the entire length of road segments to ensure geographic accuracy and generates a robust **Mean Congestion Index (MCI)**.

## 3. Data Acquisition
The collection engine operates on a continuous, automated cycle.
- **Frequency (Temporal Resolution)**: Data is sampled every 10 minutes.
- **Engine**: Google Maps Tile API (Roadmap type with `layerTraffic` enabled).
- **Sampling Strategy**: 
    - **Path Interpolation**: Each road segment is spatially interpolated every **25.0 meters**.
    - **Search Corridor**: At each sample point, the system scans a **7-pixel radius corridor** (the "capture zone") to identify traffic colors.
- **Reliability**: Implements an **Infinite Adaptive Retry** logic with exponential backoff to handle `429 Too Many Requests` errors, ensuring month-long unattended collection.

## 4. Computer Vision Methodology
The system uses automated color isolation to classify traffic states into a 4-class taxonomy, which maps directly to the continuous **Mean Congestion Index (MCI)** (range 1.0 - 4.0).

### 4.1 HSV Color Extraction
The map tiles are converted from RGB to **HSV (Hue, Saturation, Value)** space. For each traffic state, a specific HSV range is used to create a binary mask, isolating the traffic lines from the map background.

| Traffic State | MCI Class | MCI Value | Map Color |
|:---|:---|:---|:---|
| **Free Flow** | Class 1 | 1.0 | Green |
| **Moderate** | Class 2 | 2.0 | Yellow / Orange |
| **Congested** | Class 3 | 3.0 | Red |
| **Standstill** | Class 4 | 4.0 | Dark Red |

### 4.2 Aggregation
For each sensor path, the discrete pixel MCI values (1.0, 2.0, 3.0, 4.0) from all valid 25m sample points are mathematically averaged to produce a single continuous **Mean Segment MCI**. Points matching the map background (grey/white) are excluded from the calculation.

## 5. Adjacency and Distance Analysis
To support Graph Neural Network (GNN) research, the system generates a static **Adjacency Matrix**.
- **Distance Metric**: **Open Source Routing Machine (OSRM)** driving distance between the start nodes of each segment.
- **Output**: A $81 \times 81$ distance matrix in CSV format.
