import os
import json
import subprocess
import threading
import datetime as dt
from typing import Optional, Tuple, Dict

from flask import Flask, request, jsonify, Response, render_template
from sqlalchemy import create_engine, text
from sqlalchemy.pool import QueuePool
import requests

# -------------------- Config --------------------
DB_PATH = os.environ.get("DB_PATH", "/data/netspeed.sqlite")
PORT = int(os.environ.get("PORT", "8080"))
CRON = os.environ.get("WAN_SCHEDULE_CRON", "*/30 * * * *").strip()  # "off" to disable

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
ALERT_THRESHOLD_PCT = float(os.environ.get("ALERT_THRESHOLD_PCT", "5"))  # % change trigger

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
init_db()

def db():
    return engine.begin()

# -------------------- Telegram --------------------
def tg_enabled() -> bool:
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)

def tg_send(text: str) -> None:
    """Fire-and-forget Telegram message."""
    if not tg_enabled():
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
    except Exception:
        # Keep the app running even if Telegram fails
        pass

# -------------------- Stats / Averages --------------------
def _window(now: dt.datetime, days: int, offset_days: int = 0) -> Tuple[str, str]:
    """Return (start_iso, end_iso) UTC for [now-days-offset, now-offset)."""
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
    """
    Compute last 3 days and previous 3 days averages and their % changes,
    for WAN down/up and LAN down/up. Returns dict ready to format in notifications.
    """
    if now is None:
        now = dt.datetime.utcnow()
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
    """
    Build human-readable lines for any metric whose |change| >= threshold.
    """
    lines = []
    labels = {
        "wan_down": "WAN ↓",
        "wan_up":   "WAN ↑",
        "lan_down": "LAN ↓",
        "lan_up":   "LAN ↑",
    }
    for k, v in stats.items():
        last, prev, chg = v["last"], v["prev"], v["change_pct"]
        if chg is None or abs(chg) < threshold_pct:
            continue
        trend = "UP" if chg > 0 else "DOWN"
        lines.append(f"{labels[k]} {trend} {abs(chg):.1f}%  (3-day avg {last:.2f} vs prev {prev:.2f} Mbps)")
    return "\n".join(lines)

# -------------------- WAN Speed Test --------------------
def run_wan_speedtest():
    """
    Executes speedtest-cli and stores results (down/up/ping) in DB.
    Sends Telegram notification with results and potential alerts.
    """
    try:
        out = subprocess.check_output(
            ["speedtest-cli", "--json"],
            stderr=subprocess.STDOUT,
            timeout=180
        )
        data = json.loads(out.decode("utf-8", "ignore"))

        def norm_to_mbps(val):
            if val is None:
                return None
            try:
                v = float(val)
            except Exception:
                return None
            # If it's very large, assume bits/s and convert to Mbps
            return v / 1e6 if v > 200000 else v

        down_mbps = norm_to_mbps(data.get("download"))
        up_mbps   = norm_to_mbps(data.get("upload"))
        ping_ms   = float(data.get("ping", 0)) if data.get("ping") is not None else None

        server_desc = ""
        if isinstance(data.get("server"), dict):
            s = data["server"]
            server_desc = f'{s.get("sponsor","")} ({s.get("name","")}, {s.get("country","")}) id={s.get("id","")}'

        now = dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"
        with db() as con:
            con.execute(text("""INSERT INTO results
                (ts_utc,kind,mbps,bytes,seconds,ping_ms,jitter_ms,server,notes)
                VALUES (:ts,'wan_down',:mbps,NULL,NULL,:ping,NULL,:srv,NULL)"""),
                dict(ts=now, mbps=down_mbps, ping=ping_ms, srv=server_desc))
            con.execute(text("""INSERT INTO results
                (ts_utc,kind,mbps,bytes,seconds,ping_ms,jitter_ms,server,notes)
                VALUES (:ts,'wan_up',:mbps,NULL,NULL,:ping,NULL,:srv,NULL)"""),
                dict(ts=now, mbps=up_mbps, ping=ping_ms, srv=server_desc))
            con.execute(text("""INSERT INTO results
                (ts_utc,kind,mbps,bytes,seconds,ping_ms,jitter_ms,server,notes)
                VALUES (:ts,'wan_ping',NULL,NULL,NULL,:ping,NULL,:srv,NULL)"""),
                dict(ts=now, ping=ping_ms, srv=server_desc))

        # Build Telegram message with alerts
        stats = compute_3day_and_alerts()
        alert_lines = format_alerts(stats, ALERT_THRESHOLD_PCT)
        msg = (
            f"🌐 WAN speed test\n"
            f"↓ {down_mbps:.2f} Mbps  ↑ {up_mbps:.2f} Mbps  ping {ping_ms:.1f} ms\n"
            f"Server: {server_desc or 'n/a'}"
        )
        if alert_lines:
            msg += "\n\n⚠️ Averages moved:\n" + alert_lines
        tg_send(msg)

        return dict(ok=True, down_mbps=down_mbps, up_mbps=up_mbps, ping_ms=ping_ms, server=server_desc)
    except subprocess.CalledProcessError as e:
        err = e.output.decode("utf-8", "ignore")
        tg_send(f"❌ WAN speed test failed\n{err}") if tg_enabled() else None
        return dict(ok=False, error=err)
    except Exception as e:
        tg_send(f"❌ WAN speed test error: {e}") if tg_enabled() else None
        return dict(ok=False, error=str(e))

# -------------------- LAN Helpers --------------------
def gen_bytes(total, chunk=1024 * 1024):
    blk = b"\0" * chunk
    left = total
    while left > 0:
        n = chunk if left >= chunk else left
        yield blk if n == chunk else b"\0" * n
        left -= n

def _maybe_notify_lan(kind: str, mbps: Optional[float]):
    """
    Send a concise LAN message each time a LAN test is recorded (optional).
    Also include 3-day average change alerts if threshold exceeded.
    """
    if mbps is None:
        return
    # small, immediate notification
    arrow = "↓" if "down" in kind else "↑"
    msg = f"🖧 LAN {arrow} test: {mbps:.2f} Mbps"
    # include alert status if present
    stats = compute_3day_and_alerts()
    alert_lines = format_alerts(stats, ALERT_THRESHOLD_PCT)
    if alert_lines:
        msg += "\n\n⚠️ Averages moved:\n" + alert_lines
    tg_send(msg)

# -------------------- UI --------------------
@app.get("/")
def index():
    return render_template(
        "index.html",
        schedule_on=(CRON.lower() != "off"),
        cron=CRON
    )

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
    limit = int(request.args.get("limit", "100"))
    with db() as con:
        rows = con.execute(
            text("SELECT * FROM results ORDER BY id DESC LIMIT :n"),
            dict(n=limit)
        ).mappings().all()
    return jsonify(items=[dict(r) for r in rows])

@app.post("/api/report")
def api_report():
    """
    Browser reports LAN results or ping.
    We store them; if kind is lan_down/lan_up, we may notify + include avg deltas.
    """
    payload = request.get_json(force=True, silent=True) or {}
    now = dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    with db() as con:
        con.execute(text("""INSERT INTO results
            (ts_utc,kind,mbps,bytes,seconds,ping_ms,jitter_ms,server,notes)
            VALUES (:ts,:k,:mbps,:b,:s,:ping,:jit,:srv,:n)"""),
            dict(
                ts=now,
                k=payload.get("kind", "lan"),
                mbps=payload.get("mbps"),
                b=payload.get("bytes"),
                s=payload.get("seconds"),
                ping=payload.get("ping_ms"),
                jit=payload.get("jitter_ms"),
                srv=payload.get("server"),
                n=payload.get("notes"),
            )
        )

    k = payload.get("kind", "")
    if k in ("lan_down", "lan_up"):
        try:
            _maybe_notify_lan(k, payload.get("mbps"))
        except Exception:
            pass

    return jsonify(ok=True)

@app.post("/api/run-wan")
def api_run_wan():
    return jsonify(run_wan_speedtest())

# -------------------- Scheduler --------------------
def _parse_cron(expr: str):
    if not expr or expr.lower() == "off":
        return None
    parts = expr.split()
    if len(parts) != 5:
        return None
    return parts[0], parts[1]  # minute, hour

def _cron_match(field: str, value: int) -> bool:
    if field == "*":
        return True
    if field.startswith("*/"):
        try:
            step = int(field[2:])
            return value % step == 0
        except Exception:
            return False
    try:
        return int(field) == value
    except Exception:
        return False

def scheduler_loop():
    pat = _parse_cron(CRON)
    if not pat:
        return
    minute_pat, hour_pat = pat
    while True:
        try:
            now = dt.datetime.utcnow()
            if _cron_match(hour_pat, now.hour) and _cron_match(minute_pat, now.minute):
                run_wan_speedtest()
                sleep_s = 65
            else:
                sleep_s = 5
        except Exception:
            sleep_s = 10
        import time
        time.sleep(sleep_s)

def maybe_start_scheduler():
    if _parse_cron(CRON):
        t = threading.Thread(target=scheduler_loop, daemon=True)
        t.start()

maybe_start_scheduler()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True)

