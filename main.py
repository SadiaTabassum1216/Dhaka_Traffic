import os
import json
import time
from datetime import datetime, timezone, timedelta
tz_dhaka = timezone(timedelta(hours=6))
import exporter
import argparse
import traceback
import sys
import tile_collector

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Pricing constants (Google Maps Platform, 2026-03-23)
_TILE_API_PRICE_PER_1000 = 2.0   # Maps Tile API
_MONTHLY_FREE_CREDIT     = 200.0 # $200/month platform credit

def load_config():
    with open("config.json", "r", encoding="utf-8-sig") as f:
        return json.load(f)

def check_trial_mode(config):
    if not config.get("trial_mode", {}).get("enabled", False):
        return True
    start_file = os.path.join(config.get("output_dir", "data"), "trial_start.txt")
    duration = config["trial_mode"].get("duration_hours", 12)
    now = datetime.now(tz_dhaka)
    if not os.path.exists(start_file):
        os.makedirs(os.path.dirname(start_file), exist_ok=True)
        with open(start_file, "w") as f:
            f.write(now.isoformat())
        print(f"Trial mode started at {now.isoformat()}. Duration: {duration}h")
        return True
    with open(start_file, "r") as f:
        try:
            start_time_val = datetime.fromisoformat(f.read().strip())
        except Exception:
            with open(start_file, "w") as f:
                f.write(now.isoformat())
            return True
    elapsed = (now - start_time_val).total_seconds() / 3600
    if elapsed >= duration:
        print(f"Trial mode duration ({duration}h) reached. Stopping.")
        return False
    print(f"Trial mode active. Elapsed: {elapsed:.2f}h / {duration}h")
    return True

def estimate_monthly_cost(config, interval_minutes):
    """
    Return (units_per_iter, monthly_units, gross_cost, net_cost)
    Net cost accounts for the $200/month free credit.
    """
    iterations_per_month = (24 * 60 * 30) / interval_minutes
    units_per_iter = tile_collector.estimate_tile_calls_per_iteration(config)
    monthly_units  = round(units_per_iter * iterations_per_month)
    gross = (monthly_units / 1000) * _TILE_API_PRICE_PER_1000
    net   = max(0.0, gross - _MONTHLY_FREE_CREDIT)
    return units_per_iter, monthly_units, gross, net

def print_startup_banner(config, interval_minutes):
    sensors = config.get("sensors", [])
    units_per_iter, monthly_units, gross, net = estimate_monthly_cost(config, interval_minutes)

    print("=" * 60)
    print("  Dhaka Traffic Speed Collector")
    print(f"  Backend      : Maps Tile API (image colour extraction)")
    print(f"  Interval     : {interval_minutes} min | Sensors: {len(sensors)}")
    print(f"  Tiles/iter   : {units_per_iter}")
    print(f"  Monthly tiles: {monthly_units:,}")
    # print(f"  Gross cost   : ~${gross:,.2f}/month")
    # print(f"  Net cost     : ~${net:,.2f}/month  (after $200 credit)")
    print(f"  Pacing       : {os.environ.get('TILE_FETCH_DELAY', '1.0')}s per tile")
    print("=" * 60)

def run_iteration(timestamp, config, api_key):
    mcis, success, attempted = tile_collector.collect_traffic_speeds_tiles(config, api_key)
    exporter.export_traffic_mcis(timestamp, mcis, config)

    failed = attempted - success
    print(
        f"  Summary      : {success}/{attempted} sensors classified"
        + (f", {failed} no colour match" if failed else "")
    )
    if attempted > 0 and success == 0:
        print("    [!] No traffic colours detected.")
        print("    [!] Ensure 'Map Tiles API' is enabled in Google Cloud Console.")
    return success > 0

def main():
    parser = argparse.ArgumentParser(description="Dhaka Traffic Speed Collector")
    parser.add_argument("--interval",   type=int,   help="Interval in minutes")
    parser.add_argument("--duration",   type=float, help="Duration in hours")
    parser.add_argument("--iterations", type=int,   help="Number of iterations (overrides duration)")
    parser.add_argument("--mode",       choices=["test", "full"], help="Collection mode: test (12h) or full (2 months/1440h)")
    args = parser.parse_args()

    config = load_config()
    
    # Mode-based duration logic
    if args.mode == "test":
        duration = 12.0
    elif args.mode == "full":
        duration = 1440.0 # 2 months (60 days)
    else:
        duration = args.duration if args.duration else config.get("run_duration_hours", 24)

    if not check_trial_mode(config):
        return

    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "") or config.get("google_api_key", "")
    if not api_key or api_key == "YOUR_API_KEY_HERE":
        print("=" * 60)
        print("  ERROR: Google Maps API key not configured!")
        print("  Add it to .env (GOOGLE_MAPS_API_KEY) or config.json.")
        print("=" * 60)
        sys.exit(1)

    interval = args.interval if args.interval else config.get("interval_minutes", 10)

    os.makedirs(config["output_dir"], exist_ok=True)
    exporter.create_adjacency_matrix(config)
    print_startup_banner(config, interval)
    print(f"  Mode         : {args.mode if args.mode else 'Custom'}")
    print(f"  Runtime      : {duration}h")

    start_time      = time.time()
    duration_seconds = duration * 3600
    iteration       = 0

    try:
        while True:
            iteration += 1
            if args.iterations and iteration > args.iterations:
                break
            if not args.iterations and (time.time() - start_time) >= duration_seconds:
                break

            iter_start = time.time()
            timestamp  = datetime.now(tz_dhaka).strftime("%Y%m%d_%H%M%S")
            print(f"\n--- Iteration {iteration} | {datetime.now(tz_dhaka).strftime('%H:%M:%S')} ---")

            run_iteration(timestamp, config, api_key)

            if args.iterations and iteration >= args.iterations:
                break

            elapsed = time.time() - iter_start
            wait    = max(0, interval * 60 - elapsed)
            print(f"  Next in {wait / 60:.1f} min...")
            time.sleep(wait)

    except KeyboardInterrupt:
        print("\nStopped by user.")
    except Exception as e:
        print(f"Fatal error: {e}")
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
