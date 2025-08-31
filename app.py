import os
import json
import threading
import datetime as dt
import time
from typing import Optional, Tuple, Dict, Any, List
from queue import SimpleQueue

from flask import Flask, request, jsonify, Response, render_template, stream_with_context
from sqlalchemy import create_engine, text
from sqlalchemy.pool import QueuePool
import requests
import speedtest  # from speedtest-cli

# -------------------- Config --------------------
DB_PATH = os.environ.get("DB_PATH", "/data/netspeed.sqlite")
PORT = int(os.environ.get("PORT", "8080"))
# Default to every 30 minutes if no schedule is set
DEFAULT_SCHEDULE_MINUTES = int(
    os.environ.get("DEFAULT_SCHEDULE_MINUTES", "30")
)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
ALERT_THRESHOLD_PCT = float(os.environ.get("ALERT_THRESHOLD_PCT", "5"))  # % change trigger
PAGE_URL = os.environ.get("PAGE_URL", "").strip()  # e.g. "http://192.168.0.198:8080"
WEB_URL = os.environ.get("WEB_URL", "http://localhost:8080").strip()  # Web interface URL

app = Flask(__name__)

# -------------------- Database --------------------
engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
    poolclass=QueuePool,
    pool_size=5,
    max_overflow=5,
)

def init_db():
    with engine.begin() as con:
        con.execute(text("""
        CREATE TABLE IF NOT EXISTS results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc TEXT NOT NULL,
            kind TEXT NOT NULL,         -- 'wan_down','wan_up','wan_ping','lan_down','lan_up','ping'
            mbps REAL,
            bytes INTEGER,
            seconds REAL,
            ping_ms REAL,
            jitter_ms REAL,
            server TEXT,
            notes TEXT
        )
        """))
        
        # Create schedule configuration table
        con.execute(text("""
        CREATE TABLE IF NOT EXISTS schedule_config (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            enabled BOOLEAN NOT NULL DEFAULT 1,
            interval_minutes INTEGER NOT NULL DEFAULT 30,
            last_run TEXT,
            next_run TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """))
        
        # Insert default schedule if none exists
        con.execute(text("""
        INSERT OR IGNORE INTO schedule_config 
        (enabled, interval_minutes, created_at, updated_at) 
        VALUES (1, :interval, datetime('now'), datetime('now'))
        """), {"interval": DEFAULT_SCHEDULE_MINUTES})
init_db()

def db():
    return engine.begin()

# -------------------- SSE Pub/Sub --------------------
subscribers: List[SimpleQueue] = []

def sse_subscribe() -> SimpleQueue:
    q = SimpleQueue()
    subscribers.append(q)
    return q

def sse_unsubscribe(q: SimpleQueue):
    try:
        subscribers.remove(q)
    except ValueError:
        pass

def sse_publish(event: str, data: Dict[str, Any]):
    payload = f"event: {event}\n" + "data: " + json.dumps(data) + "\n\n"
    for q in list(subscribers):
        try:
            q.put(payload)
        except Exception:
            pass

@app.get("/events")
def sse_events():
    @stream_with_context
    def gen():
        q = sse_subscribe()
        try:
            q.put("event: hello\ndata: {}\n\n")
            while True:
                yield q.get()
        finally:
            sse_unsubscribe(q)
    headers = {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
    }
    return Response(gen(), headers=headers)

# -------------------- Telegram --------------------
def tg_enabled() -> bool:
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)

# Add web reference to all Telegram messages
def tg_send(text: str, include_web_ref: bool = True) -> None:
    if not tg_enabled():
        print(f"Telegram not enabled, skipping message: {text[:50]}...")
        return
    
    # Add web reference to all messages
    if include_web_ref:
        web_url = os.environ.get("WEB_URL", "http://localhost:8080")
        text = f"{text}\n\n🌐 View details: {web_url}"
    
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
        if TELEGRAM_TOPIC_ID:
            payload["message_thread_id"] = TELEGRAM_TOPIC_ID
        
        print(f"Sending Telegram message to {TELEGRAM_CHAT_ID}" + 
              (f" (topic {TELEGRAM_TOPIC_ID})" if TELEGRAM_TOPIC_ID else "") + 
              f": {text[:50]}...")
        
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 200:
            result = response.json()
            if result.get("ok"):
                print(f"Telegram message sent successfully")
            else:
                print(f"Telegram API error: {result}")
        else:
            print(f"Telegram HTTP error: {response.status_code} - {response.text}")
    except Exception as e:
        print(f"Telegram send failed: {e}")

# -------------------- Stats / Averages --------------------
def _window(now: dt.datetime, days: int, offset_days: int = 0) -> Tuple[str, str]:
    end = now - dt.timedelta(days=offset_days)
    start = end - dt.timedelta(days=days)
    return start.isoformat(timespec="seconds") + "Z", end.isoformat(timespec="seconds") + "Z"

def _avg_for_kind(kind: str, start_iso: str, end_iso: str) -> Optional[float]:
    with db() as con:
        row = con.execute(
            text("""SELECT AVG(mbps) AS avg_mbps
                    FROM results
                    WHERE kind=:k AND mbps IS NOT NULL
                      AND ts_utc >= :start AND ts_utc < :end"""),
            dict(k=kind, start=start_iso, end=end_iso)
        ).mappings().first()
    return float(row["avg_mbps"]) if row and row["avg_mbps"] is not None else None

def _pct_change(old: Optional[float], new: Optional[float]) -> Optional[float]:
    if old is None or new is None or old == 0:
        return None
    return (new - old) / old * 100.0

def compute_3day_and_alerts(now: Optional[dt.datetime] = None) -> Dict[str, Dict[str, Optional[float]]]:
    if now is None:
        now = dt.datetime.now(dt.timezone.utc)
    last_start, last_end = _window(now, days=3, offset_days=0)
    prev_start, prev_end = _window(now, days=3, offset_days=3)
    kinds = ["wan_down", "wan_up", "lan_down", "lan_up"]
    res: Dict[str, Dict[str, Optional[float]]] = {}
    for k in kinds:
        last = _avg_for_kind(k, last_start, last_end)
        prev = _avg_for_kind(k, prev_start, prev_end)
        change = _pct_change(prev, last)
        res[k] = {"last": last, "prev": prev, "change_pct": change}
    return res

def format_alerts(stats: Dict[str, Dict[str, Optional[float]]], threshold_pct: float) -> str:
    lines = []
    labels = {"wan_down": "WAN ↓", "wan_up": "WAN ↑", "lan_down": "LAN ↓", "lan_up": "LAN ↑"}
    for k, v in stats.items():
        last, prev, chg = v["last"], v["prev"], v["change_pct"]
        if chg is None or abs(chg) < threshold_pct:
            continue
        trend = "UP" if chg > 0 else "DOWN"
        lines.append(f"{labels[k]} {trend} {abs(chg):.1f}%  (3-day {last:.2f} vs prev {prev:.2f} Mbps)")
    return "\n".join(lines)

def format_three_day_avgs(stats: Dict[str, Dict[str, Optional[float]]]) -> str:
    def fmt(v): return "n/a" if v is None else f"{v:.2f} Mbps"
    return (
        "3-day averages:\n"
        f"• LAN ↓ {fmt(stats['lan_down']['last'])}  • LAN ↑ {fmt(stats['lan_up']['last'])}\n"
        f"• WAN ↓ {fmt(stats['wan_down']['last'])}  • WAN ↑ {fmt(stats['wan_up']['last'])}"
    )

# -------------------- WAN Speed Test (live stages via python API) --------------------
def run_wan_speedtest():
    """
    Uses the python speedtest API so we can emit stage-by-stage SSE updates.
    Stores results and returns a dict.
    """
    try:
        sse_publish("wan_progress", {"stage": "init"})

        s = speedtest.Speedtest(secure=True)
        sse_publish("wan_progress", {"stage": "finding_servers", "message": "Discovering speed test servers..."})
        s.get_servers()

        sse_publish("wan_progress", {"stage": "selecting_best", "message": "Selecting best server..."})
        best = s.get_best_server()
        server_desc = f'{best.get("sponsor","")} ({best.get("name","")}, {best.get("country","")}) id={best.get("id","")}'
        
        sse_publish("wan_progress", {
            "stage": "server_selected", 
            "message": f"Selected server: {best.get('sponsor', 'Unknown')} in {best.get('country', 'Unknown')}",
            "server": server_desc
        })

        # Download test with live progress updates
        sse_publish("wan_progress", {"stage": "download", "message": "Testing download speed...", "progress": 0})
        
        # Start download in a separate thread to monitor progress
        download_result = {"done": False, "speed": 0, "bytes": 0}
        download_thread = threading.Thread(target=lambda: _run_download_with_progress(s, download_result))
        download_thread.start()
        
        # Monitor download progress
        start_time = time.time()
        while not download_result["done"] and (time.time() - start_time) < 60:  # 60 second timeout
            if download_result["bytes"] > 0:
                elapsed = time.time() - start_time
                current_speed = (download_result["bytes"] * 8) / (elapsed * 1e6)  # Mbps
                sse_publish("wan_progress", {
                    "stage": "download", 
                    "message": f"Downloading... {current_speed:.2f} Mbps",
                    "progress": min(75, 50 + (elapsed / 30) * 25),  # Progress from 50% to 75%
                    "current_speed": current_speed,
                    "bytes_downloaded": download_result["bytes"]
                })
            time.sleep(0.5)  # Update every 500ms
        
        download_thread.join(timeout=5)
        down_bps = download_result.get("speed", 0)
        down_mbps = float(down_bps) / 1e6 if down_bps else None
        
        sse_publish("wan_progress", {
            "stage": "download_complete", 
            "message": f"Download complete: {down_mbps:.2f} Mbps" if down_mbps else "Download failed",
            "speed": down_mbps,
            "progress": 75
        })

        # Upload test with live progress updates
        sse_publish("wan_progress", {"stage": "upload", "message": "Testing upload speed...", "progress": 75})
        
        # Start upload in a separate thread to monitor progress
        upload_result = {"done": False, "speed": 0, "bytes": 0}
        upload_thread = threading.Thread(target=lambda: _run_upload_with_progress(s, upload_result))
        upload_thread.start()
        
        # Monitor upload progress
        start_time = time.time()
        while not upload_result["done"] and (time.time() - start_time) < 60:  # 60 second timeout
            if upload_result["bytes"] > 0:
                elapsed = time.time() - start_time
                current_speed = (upload_result["bytes"] * 8) / (elapsed * 1e6)  # Mbps
                sse_publish("wan_progress", {
                    "stage": "upload", 
                    "message": f"Uploading... {current_speed:.2f} Mbps",
                    "progress": min(95, 75 + (elapsed / 30) * 20),  # Progress from 75% to 95%
                    "current_speed": current_speed,
                    "bytes_uploaded": upload_result["bytes"]
                })
            time.sleep(0.5)  # Update every 500ms
        
        upload_thread.join(timeout=5)
        up_bps = upload_result.get("speed", 0)
        up_mbps = float(up_bps) / 1e6 if up_bps else None
        
        sse_publish("wan_progress", {
            "stage": "upload_complete", 
            "message": f"Upload complete: {up_mbps:.2f} Mbps" if up_mbps else "Upload failed",
            "speed": up_mbps,
            "progress": 95
        })

        # Get ping results
        ping_ms = float(s.results.ping) if s.results.ping is not None else None
        # Note: jitter is not always available in speedtest-cli results
        jitter_ms = None
        try:
            if hasattr(s.results, 'jitter') and s.results.jitter is not None:
                jitter_ms = float(s.results.jitter)
        except (AttributeError, ValueError):
            jitter_ms = None

        # Final results
        sse_publish("wan_progress", {
            "stage": "processing", 
            "message": "Processing results...",
            "progress": 98,
            "down_mbps": down_mbps,
            "up_mbps": up_mbps,
            "ping_ms": ping_ms
        })

        now = dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"
        with db() as con:
            con.execute(text("""INSERT INTO results (ts_utc,kind,mbps,bytes,seconds,ping_ms,jitter_ms,server,notes)
                                 VALUES (:ts,'wan_down',:mbps,NULL,NULL,:ping,:jitter,:srv,NULL)"""),
                        dict(ts=now, mbps=down_mbps, ping=ping_ms, jitter=jitter_ms, srv=server_desc))
            con.execute(text("""INSERT INTO results (ts_utc,kind,mbps,bytes,seconds,ping_ms,jitter_ms,server,notes)
                                 VALUES (:ts,'wan_up',:mbps,NULL,NULL,:ping,:jitter,:srv,NULL)"""),
                        dict(ts=now, mbps=up_mbps, ping=ping_ms, jitter=jitter_ms, srv=server_desc))
            con.execute(text("""INSERT INTO results (ts_utc,kind,mbps,bytes,seconds,ping_ms,jitter_ms,server,notes)
                                 VALUES (:ts,'wan_ping',NULL,NULL,NULL,:ping,:jitter,:srv,NULL)"""),
                        dict(ts=now, ping=ping_ms, jitter=jitter_ms, srv=server_desc))

        sse_publish("wan_done", {
            "down_mbps": down_mbps, 
            "up_mbps": up_mbps, 
            "ping_ms": ping_ms, 
            "jitter_ms": jitter_ms,
            "server": server_desc,
            "message": f"Test complete! ↓{down_mbps:.2f} ↑{up_mbps:.2f} Mbps, ping {ping_ms:.1f}ms"
        })

        # Telegram: 3-day averages + alerts + link
        stats = compute_3day_and_alerts()
        alert_lines = format_alerts(stats, ALERT_THRESHOLD_PCT)
        avg_txt = format_three_day_avgs(stats)
        link = f"\n\nOpen: {PAGE_URL}" if PAGE_URL else ""
        msg = "🌐 WAN test completed\n" + avg_txt
        if alert_lines:
            msg += "\n\n⚠️ Averages moved:\n" + alert_lines
        msg += link
        tg_send(msg)

        return dict(ok=True, down_mbps=down_mbps, up_mbps=up_mbps, ping_ms=ping_ms, jitter_ms=jitter_ms, server=server_desc)
    except Exception as e:
        sse_publish("wan_error", {"error": str(e), "message": f"Test failed: {str(e)}"})
        return dict(ok=False, error=str(e))

def _run_download_with_progress(s, result):
    """Run download test and update result dict"""
    try:
        down_bps = s.download()
        result["speed"] = down_bps
        result["bytes"] = down_bps if down_bps else 0
        result["done"] = True
    except Exception as e:
        result["error"] = str(e)
        result["done"] = True

def _run_upload_with_progress(s, result):
    """Run upload test and update result dict"""
    try:
        up_bps = s.upload(pre_allocate=False)
        result["speed"] = up_bps
        result["bytes"] = up_bps if up_bps else 0
        result["done"] = True
    except Exception as e:
        result["error"] = str(e)
        result["done"] = True

# -------------------- LAN Helpers --------------------
def gen_bytes(total, chunk=1024 * 1024):
    blk = b"\0" * chunk
    left = total
    while left > 0:
        n = chunk if left >= chunk else left
        yield blk if n == chunk else b"\0" * n
        left -= n

def _store_lan(kind: str, mbps: Optional[float], bytes_: Optional[int], seconds: Optional[float]):
    now = dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    with db() as con:
        con.execute(text("""INSERT INTO results
            (ts_utc,kind,mbps,bytes,seconds,ping_ms,jitter_ms,server,notes)
            VALUES (:ts,:k,:mbps,:b,:s,NULL,NULL,NULL,NULL)"""),
            dict(ts=now, k=kind, mbps=mbps, b=bytes_, s=seconds))
    sse_publish("history_updated", {"kind": kind})

# -------------------- UI --------------------
@app.get("/")
def index():
    # For now, just render with default values to avoid any initialization issues
    return render_template("index.html", schedule_on=True, cron="Every 30 minutes")

# -------------------- LAN Endpoints --------------------
@app.get("/ping")
def http_ping():
    return Response("pong", mimetype="text/plain")

@app.get("/download")
def http_download():
    size = int(request.args.get("size", "104857600"))  # default 100MB
    return Response(gen_bytes(size), mimetype="application/octet-stream")

@app.post("/upload")
def http_upload():
    data = request.get_data(cache=False, as_text=False)
    return jsonify(ok=True, bytes=len(data))

# -------------------- API --------------------
@app.get("/api/results")
def api_results():
    limit = int(request.args.get("limit", "500"))
    with db() as con:
        rows = con.execute(
            text("SELECT * FROM results ORDER BY id DESC LIMIT :n"),
            dict(n=limit)
        ).mappings().all()
    return jsonify(items=[dict(r) for r in rows])

@app.get("/api/averages")
def api_averages():
    stats = compute_3day_and_alerts()
    return jsonify(stats)

@app.post("/api/report")
def api_report():
    payload = request.get_json(force=True, silent=True) or {}
    kind = payload.get("kind", "lan")
    mbps = payload.get("mbps")
    bytes_ = payload.get("bytes")
    seconds = payload.get("seconds")

    if kind in ("lan_down", "lan_up"):
        _store_lan(kind, mbps, bytes_, seconds)
    else:
        now = dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"
        with db() as con:
            con.execute(text("""INSERT INTO results
                (ts_utc,kind,mbps,bytes,seconds,ping_ms,jitter_ms,server,notes)
                VALUES (:ts,:k,:mbps,:b,:s,:ping,:jit,:srv,:n)"""),
                dict(ts=now, k=kind, mbps=mbps, b=bytes_, s=seconds, ping=payload.get("ping_ms"),
                     jit=payload.get("jitter_ms"), srv=payload.get("server"), n=payload.get("notes")))
        sse_publish("history_updated", {"kind": kind})

    return jsonify(ok=True)

@app.post("/api/run-wan")
def api_run_wan():
    # Run in background so UI stays responsive
    def job():
        res = run_wan_speedtest()
        if not res.get("ok") and tg_enabled():
            tg_send(f"❌ WAN speed test failed\n{res.get('error','unknown error')}")
        sse_publish("history_updated", {"kind": "wan"})
    threading.Thread(target=job, daemon=True).start()
    return jsonify(ok=True, started=True)

@app.post("/api/run-all")
def api_run_all():
    """
    Client posts LAN DL/UL results; we store them, then run WAN in a thread.
    ONE Telegram is sent with 3-day averages (+changes) and a link.
    """
    payload = request.get_json(force=True, silent=True) or {}
    lan_down = payload.get("lan_down") or {}
    lan_up   = payload.get("lan_up") or {}

    if "mbps" in lan_down:
        _store_lan("lan_down", lan_down.get("mbps"), lan_down.get("bytes"), lan_down.get("seconds"))
    if "mbps" in lan_up:
        _store_lan("lan_up", lan_up.get("mbps"), lan_up.get("bytes"), lan_up.get("seconds"))

    def job():
        # Run WAN (emits SSE and stores results)
        run_wan_speedtest()
        # Send consolidated Telegram with averages
        stats = compute_3day_and_alerts()
        avg_txt = format_three_day_avgs(stats)
        alert_lines = format_alerts(stats, ALERT_THRESHOLD_PCT)
        link = f"\n\nOpen: {PAGE_URL}" if PAGE_URL else ""
        msg = "📊 NetSpeed – ALL tests\n" + avg_txt
        if alert_lines:
            msg += "\n\n⚠️ Averages moved:\n" + alert_lines
        msg += link
        tg_send(msg)
        sse_publish("history_updated", {"kind": "all"})

    threading.Thread(target=job, daemon=True).start()
    # Respond immediately; WAN result will arrive via SSE 'wan_done'
    return jsonify(ok=True, started=True)

# -------------------- WAN Status API --------------------
@app.get("/api/wan-status")
def api_wan_status():
    """Get current WAN test status and recent results with percentage changes"""
    try:
        with db() as con:
            # Get latest WAN results
            rows = con.execute(
                text("""SELECT kind, mbps, ping_ms, jitter_ms, server, ts_utc 
                        FROM results 
                        WHERE kind IN ('wan_down', 'wan_up', 'wan_ping')
                        ORDER BY ts_utc DESC 
                        LIMIT 30""")
            ).mappings().all()
        
        # Group by timestamp
        results = {}
        for row in rows:
            ts = row["ts_utc"]
            if ts not in results:
                results[ts] = {"ts_utc": ts, "server": row["server"]}
            results[ts][row["kind"]] = {
                "mbps": row["mbps"],
                "ping_ms": row["ping_ms"],
                "jitter_ms": row["jitter_ms"]
            }
        
        # Calculate 3-day averages for comparison
        now = dt.datetime.now(dt.timezone.utc)
        last_start, last_end = _window(now, days=3, offset_days=0)
        
        # Get averages for comparison
        avg_down = _avg_for_kind("wan_down", last_start, last_end)
        avg_up = _avg_for_kind("wan_up", last_start, last_end)
        avg_ping = _avg_for_kind("wan_ping", last_start, last_end)
        
        # Add percentage changes to recent tests
        recent_tests = list(results.values())[:5]  # Last 5 tests
        for test in recent_tests:
            if "wan_down" in test and test["wan_down"]["mbps"] and avg_down:
                change = _pct_change(avg_down, test["wan_down"]["mbps"])
                test["wan_down"]["change_pct"] = change
            
            if "wan_up" in test and test["wan_up"]["mbps"] and avg_up:
                change = _pct_change(avg_up, test["wan_up"]["mbps"])
                test["wan_up"]["change_pct"] = change
            
            if "wan_ping" in test and test["wan_ping"]["ping_ms"] and avg_ping:
                change = _pct_change(avg_ping, test["wan_ping"]["ping_ms"])
                test["wan_ping"]["change_pct"] = change
        
        return jsonify({
            "ok": True,
            "recent_tests": recent_tests,
            "averages": {
                "down": avg_down,
                "up": avg_up,
                "ping": avg_ping
            },
            "last_update": dt.datetime.now(dt.timezone.utc).isoformat() + "Z"
        })
    except Exception as e:
        return jsonify(ok=False, error=str(e))

# Enhanced cron management - Working with existing environment-based cron
@app.get("/api/cron/status")
def api_cron_status():
    """Get current cron status and configuration"""
    try:
        # Get current cron schedule from environment
        current_cron = os.environ.get("WAN_SCHEDULE_CRON", "*/30 * * * *")
        
        # Parse and format the cron schedule for display
        if current_cron.lower() == "off":
            enabled = False
            schedule_desc = "Disabled"
        else:
            enabled = True
            # Parse cron expression for user-friendly description
            parts = current_cron.split()
            if len(parts) >= 5:
                minute, hour, day, month, weekday = parts[:5]
                
                # Create human-readable description
                if minute.startswith("*/"):
                    interval = minute[2:]
                    schedule_desc = f"Every {interval} minutes"
                elif minute == "0" and hour == "*":
                    schedule_desc = "Every hour"
                elif minute == "0" and hour == "0":
                    schedule_desc = "Daily at midnight"
                elif minute == "0" and hour == "0,12":
                    schedule_desc = "Twice daily (midnight & noon)"
                else:
                    schedule_desc = f"Custom: {current_cron}"
            else:
                schedule_desc = f"Custom: {current_cron}"
        
        return jsonify({
            "ok": True,
            "enabled": enabled,
            "schedule": schedule_desc,
            "cron_expression": current_cron,
            "environment_based": True,
            "last_check": dt.datetime.now(dt.timezone.utc).isoformat() + "Z"
        })
    except Exception as e:
        return jsonify(ok=False, error=str(e))

@app.post("/api/cron/update")
def api_cron_update():
    """Update cron schedule - Note: This updates the display only, actual cron is managed via environment"""
    try:
        data = request.get_json()
        frequency = data.get("frequency", "hourly")
        
        # Define cron schedules that match common patterns
        schedules = {
            "hourly": "0 * * * *",           # Every hour at minute 0
            "every_30min": "*/30 * * * *",   # Every 30 minutes
            "every_15min": "*/15 * * * *",   # Every 15 minutes
            "every_10min": "*/10 * * * *",   # Every 10 minutes
            "every_5min": "*/5 * * * *",     # Every 5 minutes
            "daily": "0 0 * * *",            # Daily at midnight
            "twice_daily": "0 0,12 * * *",   # Twice daily at midnight and noon
            "custom": data.get("custom_schedule", "0 * * * *")
        }
        
        cron_schedule = schedules.get(frequency, "0 * * * *")
        
        # Note: In a real deployment, you would update environment variables
        # For now, we'll just return the suggested schedule
        message = f"🕐 Cron schedule suggestion: {frequency}\n\n"
        message += f"To apply this schedule, update your environment variable:\n"
        message += f"WAN_SCHEDULE_CRON=\"{cron_schedule}\"\n\n"
        message += f"Or restart your container with:\n"
        message += f"-e WAN_SCHEDULE_CRON=\"{cron_schedule}\""
        
        # Send Telegram notification with instructions
        tg_send(message)
        
        return jsonify({
            "ok": True,
            "message": f"Cron schedule suggestion: {frequency}",
            "schedule": cron_schedule,
            "note": "Schedule is managed via WAN_SCHEDULE_CRON environment variable",
            "instructions": message
        })
    except Exception as e:
        return jsonify(ok=False, error=str(e))

@app.get("/api/schedule/status")
def api_schedule_status():
    """Get current schedule status"""
    try:
        config = get_schedule_config()
        
        # Calculate next run time
        next_run = None
        if config["enabled"] and config["last_run"]:
            try:
                last_run = dt.datetime.fromisoformat(config["last_run"].replace('Z', '+00:00'))
                next_run = last_run + dt.timedelta(minutes=config["interval_minutes"])
                # If next_run is in the past, calculate from now
                if next_run < dt.datetime.now(dt.timezone.utc):
                    next_run = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=config["interval_minutes"])
            except Exception:
                next_run = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=config["interval_minutes"])
        
        return jsonify({
            "ok": True,
            "enabled": config["enabled"],
            "interval_minutes": config["interval_minutes"],
            "last_run": config["last_run"],
            "next_run": next_run.isoformat() if next_run else None,
            "status": "✅ Enabled" if config["enabled"] else "❌ Disabled",
            "description": f"Every {config['interval_minutes']} minutes" if config["enabled"] else "Disabled"
        })
    except Exception as e:
        return jsonify(ok=False, error=str(e))

@app.post("/api/schedule/update")
def api_schedule_update():
    """Update schedule configuration"""
    try:
        data = request.get_json()
        enabled = data.get("enabled", True)
        interval_minutes = data.get("interval_minutes", 30)
        
        # Validate interval
        if interval_minutes < 1 or interval_minutes > 1440:  # 1 minute to 24 hours
            return jsonify(ok=False, error="Interval must be between 1 and 1440 minutes")
        
        # Update configuration
        success = update_schedule_config(enabled, interval_minutes)
        
        if success:
            # Restart scheduler with new configuration
            stop_scheduler()
            start_scheduler()
            
            message = f"🕐 Schedule updated: {'✅ Enabled' if enabled else '❌ Disabled'}"
            if enabled:
                message += f" - Every {interval_minutes} minutes"
            
            tg_send(message)
            
            return jsonify({
                "ok": True,
                "message": "Schedule updated successfully",
                "enabled": enabled,
                "interval_minutes": interval_minutes
            })
        else:
            return jsonify(ok=False, error="Failed to update schedule")
            
    except Exception as e:
        return jsonify(ok=False, error=str(e))

@app.post("/api/schedule/disable")
def api_schedule_disable():
    """Disable automated testing"""
    try:
        success = update_schedule_config(False, 30)  # interval doesn't matter when disabled
        
        if success:
            # Restart scheduler with new configuration
            stop_scheduler()
            start_scheduler()
            
            tg_send("🕐 Automated WAN testing disabled")
            
            return jsonify({
                "ok": True,
                "message": "Automated testing disabled successfully"
            })
        else:
            return jsonify(ok=False, error="Failed to disable schedule")
            
    except Exception as e:
        return jsonify(ok=False, error=str(e))

# -------------------- Test Modal Endpoint --------------------
@app.get("/api/test-modal")
def api_test_modal():
    """Test endpoint to verify modal functionality"""
    return jsonify({
        "ok": True,
        "message": "Modal test endpoint working",
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat() + "Z"
    })

# -------------------- Python-Based Scheduler --------------------
_sched_lock = threading.Lock()
_sched_running = False  # overlap guard
_scheduler_thread = None
_scheduler_stop_event = threading.Event()

def get_schedule_config() -> Dict[str, Any]:
    """Get current schedule configuration from database"""
    try:
        with db() as con:
            result = con.execute(text("""
                SELECT enabled, interval_minutes, last_run, next_run 
                FROM schedule_config 
                ORDER BY id DESC 
                LIMIT 1
            """)).fetchone()
            
            if result:
                return {
                    "enabled": bool(result[0]),
                    "interval_minutes": result[1],
                    "last_run": result[2],
                    "next_run": result[3]
                }
            else:
                # Return default if no config found
                return {
                    "enabled": True,
                    "interval_minutes": DEFAULT_SCHEDULE_MINUTES,
                    "last_run": None,
                    "next_run": None
                }
    except Exception as e:
        print(f"Error getting schedule config: {e}")
        return {
            "enabled": True,
            "interval_minutes": DEFAULT_SCHEDULE_MINUTES,
            "last_run": None,
            "next_run": None
        }

def update_schedule_config(enabled: bool, interval_minutes: int) -> bool:
    """Update schedule configuration in database"""
    try:
        with db() as con:
            # Get the most recent config ID
            result = con.execute(text("""
                SELECT id FROM schedule_config ORDER BY id DESC LIMIT 1
            """)).fetchone()
            
            if not result:
                print("No schedule config found to update")
                return False
            
            config_id = result[0]
            print(f"Updating schedule config ID {config_id}")
            
            # Update the configuration
            con.execute(text("""
                UPDATE schedule_config 
                SET enabled = :enabled, interval_minutes = :interval, updated_at = datetime('now')
                WHERE id = :id
            """), {"enabled": enabled, "interval": interval_minutes, "id": config_id})
            
            # Update next_run time
            if enabled:
                next_run = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=interval_minutes)
                con.execute(text("""
                    UPDATE schedule_config 
                    SET next_run = :next_run 
                    WHERE id = :id
                """), {"next_run": next_run.isoformat(), "id": config_id})
            
            print(f"Schedule config updated successfully: enabled={enabled}, interval={interval_minutes}")
            return True
    except Exception as e:
        print(f"Error updating schedule config: {e}")
        return False

def _kickoff_wan_async():
    """Start WAN speed test in background thread"""
    
    def job():
        global _sched_running
        try:
            print(f"🕐 Running scheduled WAN speed test at {dt.datetime.now(dt.timezone.utc)}")
            run_wan_speedtest()
            
            # Update last_run time
            try:
                with db() as con:
                    con.execute(text("""
                        UPDATE schedule_config 
                        SET last_run = ?, updated_at = datetime('now')
                        WHERE id = (SELECT id FROM schedule_config ORDER BY id DESC LIMIT 1)
                    """), (dt.datetime.now(dt.timezone.utc).isoformat(),))
            except Exception as e:
                print(f"Error updating last_run: {e}")
                
        finally:
            with _sched_lock:
                _sched_running = False
    
    threading.Thread(target=job, daemon=True).start()

def _kickoff_lan_async():
    """Start LAN speed test in background thread"""
    
    def job():
        try:
            print(f"🕐 Running scheduled LAN speed test at {dt.datetime.now(dt.timezone.utc)}")
            run_lan_speedtest()
        except Exception as e:
            print(f"Error running LAN test: {e}")
    
    threading.Thread(target=job, daemon=True).start()

def _kickoff_comprehensive_async():
    """Start both WAN and LAN speed tests in background thread"""
    
    def job():
        global _sched_running
        try:
            print(f"🕐 Running scheduled comprehensive network test at {dt.datetime.now(dt.timezone.utc)}")
            
            # Run LAN tests first (faster)
            print("📡 Running LAN speed tests...")
            run_lan_speedtest()
            
            # Then run WAN tests
            print("🌐 Running WAN speed tests...")
            run_wan_speedtest()
            
            # Update last_run time
            try:
                with db() as con:
                    con.execute(text("""
                        UPDATE schedule_config 
                        SET last_run = ?, updated_at = datetime('now')
                        WHERE id = (SELECT id FROM schedule_config ORDER BY id DESC LIMIT 1)
                    """), (dt.datetime.now(dt.timezone.utc).isoformat(),))
            except Exception as e:
                print(f"Error updating last_run: {e}")
                
        finally:
            with _sched_lock:
                _sched_running = False
    
    threading.Thread(target=job, daemon=True).start()

def scheduler_loop():
    """Main scheduler loop that runs WAN tests at specified intervals"""
    global _sched_running
    print(f"🚀 Starting Python-based scheduler")
    
    # Run initial test on boot to create baseline
    print("🚀 Running initial WAN speed test on boot to create baseline...")
    try:
        _sched_running = True
        _kickoff_wan_async()
        # Wait a bit for the initial test to complete
        time.sleep(30)
    except Exception as e:
        print(f"Error running initial test: {e}")
        _sched_running = False
    
    while not _scheduler_stop_event.is_set():
        try:
            config = get_schedule_config()
            
            if not config["enabled"]:
                time.sleep(10)  # Check every 10 seconds if disabled
                continue
            
            interval_minutes = config["interval_minutes"]
            print(f"⏰ Scheduler running with {interval_minutes} minute intervals")
            
            # Wait for the specified interval
            time.sleep(interval_minutes * 60)
            
            if _scheduler_stop_event.is_set():
                break
            
            # Check if we should run the test
            start_it = False
            with _sched_lock:
                if not _sched_running:
                    _sched_running = True
                    start_it = True
            
            if start_it:
                _kickoff_wan_async()
                
        except Exception as e:
            print(f"Error in scheduler loop: {e}")
            time.sleep(60)  # Wait a minute before retrying
    
    print("🛑 Scheduler stopped")

def start_scheduler():
    """Start the scheduler thread"""
    global _scheduler_thread
    if _scheduler_thread and _scheduler_thread.is_alive():
        return
    
    _scheduler_stop_event.clear()
    _scheduler_thread = threading.Thread(target=scheduler_loop, daemon=True)
    _scheduler_thread.start()
    print("✅ Scheduler started")

def stop_scheduler():
    """Stop the scheduler thread"""
    global _scheduler_thread
    if _scheduler_thread:
        _scheduler_stop_event.set()
        _scheduler_thread.join(timeout=5)
        _scheduler_thread = None
        print("🛑 Scheduler stopped")

if __name__ == "__main__":
    # Start scheduler after app is ready
    def start_scheduler_delayed():
        time.sleep(2)  # Wait for app to be ready
        start_scheduler()
    
    threading.Thread(target=start_scheduler_delayed, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT, threaded=True)
