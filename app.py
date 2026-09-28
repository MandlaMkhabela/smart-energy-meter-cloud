import os
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template_string
import paho.mqtt.client as mqtt

try:
    import psycopg2
    import psycopg2.extras
except Exception:
    psycopg2 = None

BROKER = os.getenv("MQTT_BROKER", "broker.hivemq.com")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
TOPIC = os.getenv("MQTT_TOPIC", "wits/group55/mandla-main001/data")
DATABASE_URL = os.getenv("DATABASE_URL")

SQLITE_PATH = Path(__file__).resolve().parent / "meter_readings.db"

app = Flask(__name__)

HTML = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Smart Energy Meter Dashboard</title>
<style>
body{font-family:Arial,sans-serif;margin:0;background:#f4f5f7;color:#1f2937}
header{background:#111827;color:white;padding:20px}
main{max-width:1000px;margin:24px auto;padding:0 16px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px}
.card{background:white;border-radius:12px;padding:18px;box-shadow:0 2px 10px rgba(0,0,0,.08)}
.label{font-size:12px;color:#6b7280;text-transform:uppercase}
.value{font-size:30px;font-weight:700;margin-top:8px}
.status{font-size:28px;font-weight:700;text-align:center}
.normal{color:#15803d}.alarm{color:#b91c1c}
table{width:100%;border-collapse:collapse;margin-top:12px}
th,td{padding:8px;border-bottom:1px solid #e5e7eb;text-align:left;font-size:14px}
.badge{display:inline-block;padding:4px 8px;border-radius:999px;background:#e5e7eb;font-size:12px}
</style>
</head>
<body>
<header>
  <h1>Smart Energy Meter — Remote Monitoring</h1>
  <div>Wits Investigation Project · Cloud Dashboard</div>
</header>
<main>
<div class="grid">
<div class="card"><div class="label">Upstream current</div><div class="value"><span id="u">--</span> A</div></div>
<div class="card"><div class="label">Main meter current</div><div class="value"><span id="m">--</span> A</div></div>
<div class="card"><div class="label">Difference</div><div class="value"><span id="d">--</span> A</div></div>
<div class="card"><div class="label">Last update</div><div class="value" style="font-size:18px"><span id="t">--</span></div></div>
</div>

<div class="card" style="margin-top:16px">
<div class="label">System status</div>
<div id="s" class="status">WAITING FOR DATA</div>
</div>

<div class="card" style="margin-top:16px">
<div class="label">Connection</div>
<div style="margin-top:8px">
<span class="badge">MQTT cloud broker</span>
<span class="badge">Cloud database</span>
<span class="badge">Internet-accessible dashboard</span>
</div>
</div>

<div class="card" style="margin-top:16px">
<div class="label">Recent readings</div>
<table>
<thead><tr><th>Time</th><th>Upstream</th><th>Main</th><th>Difference</th><th>Status</th></tr></thead>
<tbody id="hist"></tbody>
</table>
</div>
</main>

<script>
function f(v){return Number(v).toFixed(3)}
async function refresh(){
  try{
    const r=await fetch('/api/latest', {cache:'no-store'}); 
    const x=await r.json();

    if(x.status!=='NO_DATA'){
      u.textContent=f(x.upstream_current_A);
      m.textContent=f(x.main_current_A);
      d.textContent=f(x.difference_A);
      t.textContent=new Date(x.timestamp_utc).toLocaleTimeString();
      s.textContent=x.status;
      s.className='status '+(x.status==='NORMAL'?'normal':'alarm');
    }

    const h=await fetch('/api/history', {cache:'no-store'}); 
    const rows=await h.json();
    hist.innerHTML='';
    rows.slice(-15).reverse().forEach(x=>{
      const tr=document.createElement('tr');
      tr.innerHTML=`<td>${new Date(x.timestamp_utc).toLocaleTimeString()}</td>
      <td>${f(x.upstream_current_A)} A</td>
      <td>${f(x.main_current_A)} A</td>
      <td>${f(x.difference_A)} A</td>
      <td>${x.status}</td>`;
      hist.appendChild(tr);
    });
  }catch(e){
    s.textContent='BACKEND UNREACHABLE';
    s.className='status alarm';
  }
}
refresh(); setInterval(refresh,2000);
</script>
</body>
</html>
"""

def using_postgres():
    return bool(DATABASE_URL and psycopg2)

def pg_connect():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    if using_postgres():
        with pg_connect() as con:
            with con.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS readings(
                        id BIGSERIAL PRIMARY KEY,
                        timestamp_utc TIMESTAMPTZ NOT NULL,
                        meter_id TEXT NOT NULL,
                        upstream_current_A DOUBLE PRECISION NOT NULL,
                        main_current_A DOUBLE PRECISION NOT NULL,
                        difference_A DOUBLE PRECISION NOT NULL,
                        status TEXT NOT NULL,
                        source TEXT
                    )
                """)
        print("Database ready: PostgreSQL")
    else:
        with sqlite3.connect(SQLITE_PATH) as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS readings(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp_utc TEXT NOT NULL,
                    meter_id TEXT NOT NULL,
                    upstream_current_A REAL NOT NULL,
                    main_current_A REAL NOT NULL,
                    difference_A REAL NOT NULL,
                    status TEXT NOT NULL,
                    source TEXT
                )
            """)
            con.commit()
        print("Database ready: SQLite fallback")

def save_reading(data):
    ts = datetime.now(timezone.utc)
    row = (
        ts,
        str(data.get("meter_id", "main001")),
        float(data["upstream_current_A"]),
        float(data["main_current_A"]),
        float(data["difference_A"]),
        str(data["status"]),
        str(data.get("source", "mqtt")),
    )

    if using_postgres():
        with pg_connect() as con:
            with con.cursor() as cur:
                cur.execute("""
                    INSERT INTO readings
                    (timestamp_utc,meter_id,upstream_current_A,main_current_A,difference_A,status,source)
                    VALUES(%s,%s,%s,%s,%s,%s,%s)
                """, row)
    else:
        with sqlite3.connect(SQLITE_PATH) as con:
            con.execute("""
                INSERT INTO readings
                (timestamp_utc,meter_id,upstream_current_A,main_current_A,difference_A,status,source)
                VALUES(?,?,?,?,?,?,?)
            """, (ts.isoformat(),) + row[1:])
            con.commit()

def serialize_row(row):
    if row is None:
        return None
    d = dict(row)
    ts = d.get("timestamp_utc")
    if hasattr(ts, "isoformat"):
        d["timestamp_utc"] = ts.isoformat()
    return d

def latest_row():
    if using_postgres():
        with pg_connect() as con:
            with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM readings ORDER BY id DESC LIMIT 1")
                return serialize_row(cur.fetchone())
    with sqlite3.connect(SQLITE_PATH) as con:
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM readings ORDER BY id DESC LIMIT 1").fetchone()
        return serialize_row(row)

def history_rows(limit=100):
    if using_postgres():
        with pg_connect() as con:
            with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT * FROM readings ORDER BY id DESC LIMIT %s",
                    (limit,)
                )
                rows = [serialize_row(r) for r in cur.fetchall()]
                return list(reversed(rows))
    with sqlite3.connect(SQLITE_PATH) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT * FROM readings ORDER BY id DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [serialize_row(r) for r in reversed(rows)]

def on_connect(client, userdata, flags, reason_code, properties=None):
    print("MQTT connected:", reason_code)
    client.subscribe(TOPIC)
    print("Subscribed to:", TOPIC)

def on_message(client, userdata, msg):
    try:
        data = json.loads(msg.payload.decode("utf-8"))
        save_reading(data)
        print("Stored:", data)
    except Exception as e:
        print("Bad MQTT message:", e)

def mqtt_loop():
    while True:
        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
            client.on_connect = on_connect
            client.on_message = on_message
            print(f"Connecting MQTT: {BROKER}:{MQTT_PORT}")
            client.connect(BROKER, MQTT_PORT, 60)
            client.loop_forever()
        except Exception as e:
            print("MQTT connection error:", e)
            time.sleep(5)

@app.get("/")
def home():
    return render_template_string(HTML)

@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "database": "postgresql" if using_postgres() else "sqlite",
        "mqtt_broker": BROKER,
        "mqtt_topic": TOPIC,
    })

@app.get("/api/latest")
def latest():
    row = latest_row()
    return jsonify({"status": "NO_DATA"} if row is None else row)

@app.get("/api/history")
def history():
    return jsonify(history_rows(100))

# Gunicorn imports this module instead of running __main__, so initialize here.
init_db()
threading.Thread(target=mqtt_loop, daemon=True, name="mqtt-subscriber").start()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
