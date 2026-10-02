# Dhaka Traffic Collector — Simplified Deployment Guide

Follow these **3 clean phases** to get your system running on a free cloud server. 

---

## Phase 1: Create the Folder & Upload Files
*Run these commands once.*

1.  **Connect** to your VM via SSH.
2.  **Navigate to the project folder**:
    ```bash
    mkdir -p ~/Dhaka_Traffic
    cd ~/Dhaka_Traffic
    ```
3.  **Clear Old Files (Optional)**:
    If you already had a previous version on the server, clear it first to ensure you have a "fresh start" with the latest code:
    ```bash
    rm -rf ~/Dhaka_Traffic/*
    ```
4.  **Upload the Code**: 
    -   `main.py`, `tile_collector.py`, `config.json`, `exporter.py`, `requirements.txt`

> [!TIP]
> **If files were uploaded to the wrong folder** (e.g. your home directory instead of `~/Dhaka_Traffic`), run this to move them all at once:
> ```bash
> mv config.json main.py exporter.py requirements.txt tile_collector.py ~/Dhaka_Traffic/
> ```

---

## Phase 2: Prepare Python Runtime
*Run these commands once.*

1.  **Install system dependencies**:
    ```bash
    sudo apt update
    sudo apt install -y python3-pip python3-venv tmux libgl1
    ```
2.  **Create and Activate Virtual Environment**:
    ```bash
    python3 -m venv .venv
    source .venv/bin/activate
    ```
3.  **Install Project Packages**:
    ```bash
    # (Must be inside the .venv for this to work)
    pip install -r requirements.txt
    ```

---

## Phase 3: Launch & Detach (The "Permanent" Run)
*Follow this specific sequence to start the 24/7 collection.*

1.  **Start a fresh "Permanent Screen" (tmux)**:
    ```bash
    tmux new -s traffic_collector
    ```
2.  **Activate Python inside the tmux window** (Crucial Step):
    ```bash
    source .venv/bin/activate
    ```
3.  **Start the Collector**:
    ```bash
    # Option A: 12-hour Test Run
    python3 main.py --mode test

    # Option B: 2-month Full Production
    python3 main.py --mode full
    ```

> [!TIP]
> **Self-Healing & Reliability**: The system now has "Infinite Adaptive Retry" for `429 Too Many Requests` errors. If Google rate-limits your server, the system will automatically pause and retry with exponential backoff. You don't need to restart it.
4.  **Detach and Go Home**:
    Once you see **"Iteration 1"**, press **`Ctrl + B`**, let go, and then press **`D`**.
    *The terminal will say `[detached (from session traffic_collector)]`. You can now safely close the browser window.*

## How to Download Your Data 📊

Once you have collected data for a few days, you will want to download it to your local computer for analysis.

### 1. Download Single Files (e.g., CSV)
1.  Click the **Download File** button in the top right corner of your SSH browser window.
2.  Provide the **Absolute Path** to the file:
    -   `/home/[your-username]/Dhaka_Traffic/data/traffic_speed_202604.csv`

### 2. Download Entire Folders (e.g., Debug Snapshots)
To download multiple files at once, it is best to compress the folder first:
1.  **Navigate to the project and compress the folder**:
    ```bash
    cd ~/Dhaka_Traffic
    # To compress just the data folder:
    tar -czvf project_backup.tar.gz data/
    
    # Or, to compress the entire project (excluding the heavy virtual environment):
    # tar --exclude='.venv' --exclude='__pycache__' -czvf project_backup.tar.gz .
    ```
2.  **Download it**: 
    Use the **Download File** button and provide this path:
    -   `/home/[your-username]/Dhaka_Traffic/project_backup.tar.gz`

---

## How to Check Your Results 📉

If you want to see a **Visual Heatmap** of your current progress on the server:

1.  **Run the visualizer**:
    ```bash
    python3 scripts/visualize_traffic.py
    ```
2.  **Download the result**:
    Use the **Download File** button and provide this path:
    -   `/home/[your-username]/Dhaka_Traffic/data/traffic_speed_202604_heatmap.png`

---

## Maintenance & Recovery

### How to Check Status Later
Log back in via SSH and run:
```bash
tmux attach -t traffic_collector
```
*You will see the latest logs exactly where you left off.*

### How to Stop & Kill the Program
1.  **Re-attach**: `tmux attach -t traffic_collector`
2.  **Stop**: Press **`Ctrl + C`**.
3.  **Kill Session**: Type **`exit`** or run **`tmux kill-session -t traffic_collector`**.
