import os
import json
import subprocess
import threading
import datetime as dt
from typing import Optional, Tuple, Dict, Any, List
from queue import SimpleQueue
import speedtest  # comes with speedtest-cli

from flask import Flask, request, jsonify, Response, render_template, stream_with_context
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
            # Initial hello lets the client know we're connected
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

def tg_send(text: str) -> None:
    if not tg_enabled():
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
    except Exception:
        pass

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
    lines = []
    labels = {"wan_down": "WAN ↓", "wan_up": "WAN ↑", "lan_down": "LAN ↓", "lan_up": "LAN ↑"}
    for k, v in stats.items():
        last, prev, chg = v["last"], v["prev"], v["change_pct"]
        if chg is None or abs(chg) < threshold_pct:
            continue
        trend = "UP" if chg > 0 else "DOWN"
        lines.append(f"{labels[k]} {trend} {abs(chg):.1f}%  (3-day {last:.2f} vs prev {prev:.2f} Mbps)")
    return "\n".join(lines)

# -------------------- WAN Speed Test --------------------
def _speedtest_norm_to_mbps(val):
    if val is None:
        return None
    try:
        v = float(val)
    except Exception:
        return None
    return v / 1e6 if v > 200000 else v

def run_wan_speedtest():
    """
    Uses the python speedtest API so we can emit stage-by-stage SSE updates.
    Stores results and returns a dict.
    """
    try:
        sse_publish("wan_progress", {"stage": "init"})

        s = speedtest.Speedtest(secure=True)
        sse_publish("wan_progress", {"stage": "finding_servers"})
        s.get_servers()

        sse_publish("wan_progress", {"stage": "selecting_best"})
        best = s.get_best_server()  # dict with server props
        server_desc = f'{best.get("sponsor","")} ({best.get("name","")}, {best.get("country","")}) id={best.get("id","")}'

        sse_publish("wan_progress", {"stage": "download"})
        down_bps = s.download()  # bits per second (float)

        sse_publish("wan_progress", {"stage": "upload"})
        up_bps = s.upload(pre_allocate=False)  # bits per second

        ping_ms = float(s.results.ping) if s.results.ping is not None else None
        down_mbps = float(down_bps) / 1e6 if down_bps else None
        up_mbps   = float(up_bps)   / 1e6 if up_bps else None

        now = dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"
        with db() as con:
            con.execute(text("""INSERT INTO results (ts_utc,kind,mbps,bytes,seconds,ping_ms,jitter_ms,server,notes)
                                VALUES (:ts,'wan_down',:mbps,NULL,NULL,:ping,NULL,:srv,NULL)"""),
                        dict(ts=now, mbps=down_mbps, ping=ping_ms, srv=server_desc))
            con.execute(text("""INSERT INTO results (ts_utc,kind,mbps,bytes,seconds,ping_ms,jitter_ms,server,notes)
                                VALUES (:ts,'wan_up',:mbps,NULL,NULL,:ping,NULL,:srv,NULL)"""),
                        dict(ts=now, mbps=up_mbps, ping=ping_ms, srv=server_desc))
            con.execute(text("""INSERT INTO results (ts_utc,kind,mbps,bytes,seconds,ping_ms,jitter_ms,server,notes)
                                VALUES (:ts,'wan_ping',NULL,NULL,NULL,:ping,NULL,:srv,NULL)"""),
                        dict(ts=now, ping=ping_ms, srv=server_desc))

        # final event
        sse_publish("wan_done", {"down_mbps": down_mbps, "up_mbps": up_mbps, "ping_ms": ping_ms, "server": server_desc})

        return dict(ok=True, down_mbps=down_mbps, up_mbps=up_mbps, ping_ms=ping_ms, server=server_desc)
    except Exception as e:
        sse_publish("wan_error", {"error": str(e)})
        return dict(ok=False, error=str(e))

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
    return render_template("index.html", schedule_on=(CRON.lower() != "off"), cron=CRON)

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
    """
    Browser reports LAN results or ping.
    """
    payload = request.get_json(force=True, silent=True) or {}
    kind = payload.get("kind", "lan")
    mbps = payload.get("mbps")
    bytes_ = payload.get("bytes")
    seconds = payload.get("seconds")

    # Store
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
    # run on a thread so SSE can show progress and we don't block worker
    res_holder = {}
    def job():
        res = run_wan_speedtest()
        res_holder["res"] = res
        if res.get("ok"):
            # Notify Telegram for WAN only (single-message combo handled by /api/run-all)
            stats = compute_3day_and_alerts()
            alert_lines = format_alerts(stats, ALERT_THRESHOLD_PCT)
            msg = (
                f"🌐 WAN speed test\n"
                f"↓ {res['down_mbps']:.2f} Mbps  ↑ {res['up_mbps']:.2f} Mbps  ping {res['ping_ms']:.1f} ms\n"
                f"Server: {res.get('server') or 'n/a'}"
            )
            if alert_lines:
                msg += "\n\n⚠️ Averages moved:\n" + alert_lines
            tg_send(msg)
        else:
            if tg_enabled():
                tg_send(f"❌ WAN speed test failed\n{res.get('error','unknown error')}")
        sse_publish("history_updated", {"kind": "wan"})
    threading.Thread(target=job, daemon=True).start()
    return jsonify(ok=True, started=True)

@app.post("/api/run-all")
def api_run_all():
    """
    Client sends LAN DL/UL results, server runs WAN, then sends ONE Telegram message.
    Body: {
      "lan_down": {"mbps":..., "bytes":..., "seconds":...},
      "lan_up":   {"mbps":..., "bytes":..., "seconds":...}
    }
    """
    payload = request.get_json(force=True, silent=True) or {}
    lan_down = payload.get("lan_down") or {}
    lan_up   = payload.get("lan_up") or {}

    # Store LAN results (if present)
    if "mbps" in lan_down:
        _store_lan("lan_down", lan_down.get("mbps"), lan_down.get("bytes"), lan_down.get("seconds"))
    if "mbps" in lan_up:
        _store_lan("lan_up", lan_up.get("mbps"), lan_up.get("bytes"), lan_up.get("seconds"))

    # Run WAN test right now (sync inside this request)
    res = run_wan_speedtest()

    # Compose ONE Telegram message with LAN+WAN and alert deltas
    if res.get("ok"):
        stats = compute_3day_and_alerts()
        alert_lines = format_alerts(stats, ALERT_THRESHOLD_PCT)
        parts = []
        if "mbps" in lan_down:
            parts.append(f"LAN ↓ {lan_down['mbps']:.2f} Mbps")
        if "mbps" in lan_up:
            parts.append(f"LAN ↑ {lan_up['mbps']:.2f} Mbps")
        parts.append(f"WAN ↓ {res['down_mbps']:.2f} Mbps")
        parts.append(f"WAN ↑ {res['up_mbps']:.2f} Mbps")
        parts.append(f"ping {res['ping_ms']:.1f} ms")
        msg = "📊 NetSpeed (All tests)\n" + "  • " + "\n  • ".join(parts) + f"\nServer: {res.get('server') or 'n/a'}"
        if alert_lines:
            msg += "\n\n⚠️ Averages moved:\n" + alert_lines
        tg_send(msg)
    else:
        if tg_enabled():
            tg_send(f"❌ Run-all: WAN part failed\n{res.get('error','unknown error')}")

    sse_publish("history_updated", {"kind": "all"})
    return jsonify(res)

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

