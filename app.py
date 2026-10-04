import encodings.idna
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
<meta name="theme-color" content="#07111f">
<title>26G55 Smart Energy Meter</title>

<style>
:root{
  --bg:#101214;
  --panel:#191c1f;
  --panel2:#202428;
  --line:#343a40;
  --text:#f3f4f6;
  --muted:#9da3aa;
  --cyan:#4cc9b0;
  --blue:#e9ecef;
  --green:#38d996;
  --yellow:#f6b94a;
  --red:#ff6677;
  --shadow:0 18px 55px rgba(0,0,0,.28);
}

*{box-sizing:border-box}
html,body{margin:0;min-height:100%;font-family:Inter,Segoe UI,Arial,sans-serif;background:
radial-gradient(circle at 15% 0%,rgba(76,201,176,.08),transparent 30%),
radial-gradient(circle at 85% 15%,rgba(246,185,74,.06),transparent 28%),
var(--bg);color:var(--text)}

body:before{
  content:"";
  position:fixed;inset:0;pointer-events:none;
  background-image:linear-gradient(rgba(255,255,255,.015) 1px,transparent 1px),
  linear-gradient(90deg,rgba(255,255,255,.015) 1px,transparent 1px);
  background-size:36px 36px;
}

body.normal-state{
  background:
  radial-gradient(circle at 15% 0%,rgba(56,217,150,.08),transparent 30%),
  radial-gradient(circle at 85% 15%,rgba(76,201,176,.05),transparent 28%),
  var(--bg);
}

body.alarm-state{
  background:
  radial-gradient(circle at 18% 0%,rgba(255,102,119,.16),transparent 32%),
  radial-gradient(circle at 85% 10%,rgba(255,102,119,.10),transparent 28%),
  linear-gradient(180deg,#1a1014 0%, #101214 100%);
  animation:alarmBgPulse 1.4s ease-in-out infinite;
}

@keyframes alarmBgPulse{
  0%,100%{box-shadow:inset 0 0 0 0 rgba(255,102,119,0)}
  50%{box-shadow:inset 0 0 180px 0 rgba(255,102,119,.05)}
}

.shell{max-width:1240px;margin:auto;padding:22px}

.topbar{
  display:flex;align-items:center;justify-content:space-between;gap:16px;
  margin-bottom:18px
}
.brand{display:flex;align-items:center;gap:13px}
.logo{
  width:48px;height:48px;border-radius:14px;
  display:grid;place-items:center;font-weight:900;font-size:19px;
  background:linear-gradient(145deg,#e7e5df,#b7bcc2);
  color:#111315;box-shadow:0 0 24px rgba(255,255,255,.10)
}
.brand h1{font-size:22px;margin:0;letter-spacing:.2px}
.brand p{margin:4px 0 0;color:var(--muted);font-size:13px}

.top-actions{display:flex;align-items:center;gap:10px;flex-wrap:wrap;justify-content:flex-end}
.view-switch{
  display:flex;align-items:center;padding:3px;border:1px solid var(--line);
  background:rgba(28,31,34,.92);border-radius:12px
}
.view-btn{
  border:0;background:transparent;color:var(--muted);font:inherit;font-size:12px;font-weight:800;
  padding:7px 11px;border-radius:9px;cursor:pointer;transition:.18s ease
}
.view-btn:hover{color:var(--text)}
.view-btn.active{background:#e9ecef;color:#121416;box-shadow:0 4px 14px rgba(0,0,0,.18)}
.live-pill{
  display:flex;align-items:center;gap:9px;padding:9px 13px;border:1px solid var(--line);
  background:rgba(28,31,34,.92);border-radius:999px;color:#d7dadd;font-size:13px
}
.dot{width:9px;height:9px;border-radius:50%;background:var(--green);box-shadow:0 0 14px var(--green);animation:pulse 1.4s infinite}
@keyframes pulse{50%{transform:scale(.72);opacity:.55}}

.status-hero{
  position:relative;overflow:hidden;border:1px solid var(--line);
  background:linear-gradient(135deg,rgba(31,34,37,.98),rgba(20,22,24,.96));
  border-radius:22px;padding:22px;box-shadow:var(--shadow);margin-bottom:16px;
}
.status-hero:after{
  content:"";position:absolute;width:230px;height:230px;border-radius:50%;
  right:-85px;top:-115px;background:rgba(246,185,74,.07);filter:blur(2px)
}
.hero-row{display:flex;justify-content:space-between;align-items:center;gap:18px;position:relative;z-index:1}
.eyebrow{font-size:11px;letter-spacing:1.6px;text-transform:uppercase;color:var(--muted);font-weight:700}
.hero-status{font-size:34px;font-weight:900;margin-top:7px;letter-spacing:.3px}
.hero-status.normal-text{color:#88f0b8}
.hero-status.alarm-text{color:#ff7d8c;text-shadow:0 0 24px rgba(255,102,119,.22)}
.hero-sub{color:var(--muted);margin-top:7px;font-size:14px}
.status-badge{
  padding:13px 17px;border-radius:14px;font-weight:800;min-width:168px;text-align:center;
  border:1px solid rgba(255,255,255,.08)
}
.status-normal{
  color:#d8ffe9;
  background:rgba(56,217,150,.18);
  border:1px solid rgba(56,217,150,.35);
  box-shadow:0 0 28px rgba(56,217,150,.10), inset 0 0 22px rgba(56,217,150,.04)
}
.status-alarm{
  color:#ffe3e7;
  background:rgba(255,93,115,.22);
  border:1px solid rgba(255,93,115,.42);
  box-shadow:0 0 38px rgba(255,93,115,.18), inset 0 0 24px rgba(255,93,115,.08);
  animation:alarmBadgePulse 1s ease-in-out infinite;
}
@keyframes alarmBadgePulse{
  0%,100%{transform:scale(1)}
  50%{transform:scale(1.03)}
}

.grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:14px}
.metric{
  background:linear-gradient(160deg,rgba(31,34,37,.97),rgba(22,24,26,.97));
  border:1px solid var(--line);border-radius:18px;padding:17px;box-shadow:var(--shadow);
  position:relative;overflow:hidden
}
.metric:before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--cyan)}
.metric:nth-child(2):before{background:#e9ecef}
.metric:nth-child(3):before{background:var(--yellow)}
.metric:nth-child(4):before{background:var(--green)}
.metric-label{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:1px}
.metric-value{font-size:31px;font-weight:900;margin-top:10px;white-space:nowrap}
.metric-unit{font-size:15px;color:var(--muted);font-weight:600;margin-left:3px}
.metric-note{font-size:12px;color:var(--muted);margin-top:8px}

.two-col{display:grid;grid-template-columns:1.6fr .9fr;gap:14px;margin-bottom:14px}
.card{
  background:linear-gradient(160deg,rgba(31,34,37,.97),rgba(22,24,26,.97));
  border:1px solid var(--line);border-radius:18px;padding:18px;box-shadow:var(--shadow)
}
.card-head{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:12px}
.card-title{font-weight:800;font-size:15px}
.card-sub{font-size:12px;color:var(--muted)}
.legend{display:flex;gap:13px;flex-wrap:wrap;font-size:12px;color:var(--muted)}
.legend span{display:flex;align-items:center;gap:6px}
.swatch{width:9px;height:9px;border-radius:999px;display:inline-block}
.chart-wrap{height:260px;width:100%;position:relative}
svg{width:100%;height:100%;overflow:visible}

.kpi-stack{display:grid;gap:11px}
.kpi{
  border:1px solid var(--line);border-radius:14px;padding:14px;
  background:rgba(255,255,255,.018)
}
.kpi-top{display:flex;justify-content:space-between;gap:10px;align-items:center}
.kpi-name{color:var(--muted);font-size:12px}
.kpi-val{font-size:22px;font-weight:850;margin-top:5px}
.chips{display:flex;gap:7px;flex-wrap:wrap;margin-top:12px}
.chip{font-size:11px;padding:6px 9px;border-radius:999px;background:#24282c;border:1px solid #3a4046;color:#d6d9dc}

.table-card{padding:0;overflow:hidden}
.table-head{padding:18px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center}
.table-wrap{overflow:auto;max-height:430px}
table{width:100%;border-collapse:collapse;min-width:720px}
th,td{padding:12px 16px;border-bottom:1px solid rgba(35,54,77,.72);text-align:left;font-size:13px}
th{position:sticky;top:0;background:#202326;color:#b4b8bd;font-size:11px;text-transform:uppercase;letter-spacing:.8px;z-index:2}
tr:hover td{background:rgba(255,255,255,.02)}
.badge{display:inline-block;padding:5px 9px;border-radius:999px;font-size:11px;font-weight:800}
.badge-normal{color:#aef6d2;background:rgba(44,227,143,.10);border:1px solid rgba(44,227,143,.22)}
.badge-alarm{color:#ffd3da;background:rgba(255,93,115,.12);border:1px solid rgba(255,93,115,.25)}
.alarm-row td{background:rgba(255,93,115,.035)}

.footer{display:flex;justify-content:space-between;gap:14px;flex-wrap:wrap;color:var(--muted);font-size:11px;padding:17px 2px 4px}
.offline .dot{background:var(--red);box-shadow:0 0 14px var(--red)}
.offline #liveText{color:#ffc5cd}

@media(max-width:900px){
  .grid4{grid-template-columns:repeat(2,1fr)}
  .two-col{grid-template-columns:1fr}
}
@media(max-width:580px){
  .shell{padding:14px}
  .topbar{align-items:flex-start}
  .brand h1{font-size:18px}
  .logo{width:42px;height:42px}
  .live-pill{font-size:11px;padding:8px 10px}
  .grid4{grid-template-columns:1fr 1fr;gap:10px}
  .metric{padding:14px}
  .metric-value{font-size:24px}
  .hero-row{align-items:flex-start;flex-direction:column}
  .hero-status{font-size:27px}
  .status-badge{min-width:0;width:100%}
  .chart-wrap{height:210px}
}
</style>
</head>

<body class="normal-state">
<div class="shell">

  <div class="topbar">
    <div class="brand">
      <div class="logo">26</div>
      <div>
        <h1>26G55 Smart Energy Meter</h1>
        <p>Wits EIE Investigation Project · Cloud Remote Monitoring</p>
      </div>
    </div>
    <div class="top-actions">
      <div class="view-switch" aria-label="Dashboard measurement view">
        <button class="view-btn active" id="currentViewBtn" type="button">Current</button>
        <button class="view-btn" id="powerViewBtn" type="button">Power</button>
      </div>
      <div class="live-pill" id="connectionPill">
        <span class="dot"></span>
        <span id="liveText">LIVE</span>
      </div>
    </div>
  </div>

  <section class="status-hero" id="heroPanel">
    <div class="hero-row">
      <div>
        <div class="eyebrow">System condition</div>
        <div class="hero-status" id="heroStatus">WAITING FOR DATA</div>
        <div class="hero-sub" id="heroSub">Waiting for the first meter packet…</div>
      </div>
      <div class="status-badge status-normal" id="statusBadge">MONITORING</div>
    </div>
  </section>

  <section class="grid4">
    <div class="metric">
      <div class="metric-label" id="upstreamLabel">Upstream current</div>
      <div class="metric-value"><span id="u">--</span><span class="metric-unit" id="upstreamUnit">A</span></div>
      <div class="metric-note">Reference-side measurement</div>
    </div>
    <div class="metric">
      <div class="metric-label" id="mainLabel">Main meter current</div>
      <div class="metric-value"><span id="m">--</span><span class="metric-unit" id="mainUnit">A</span></div>
      <div class="metric-note">Customer meter measurement</div>
    </div>
    <div class="metric">
      <div class="metric-label" id="differenceLabel">Current difference</div>
      <div class="metric-value"><span id="d">--</span><span class="metric-unit" id="differenceUnit">A</span></div>
      <div class="metric-note" id="differenceNote">Mismatch unavailable</div>
    </div>
    <div class="metric">
      <div class="metric-label">Last update</div>
      <div class="metric-value" style="font-size:22px"><span id="t">--:--:--</span></div>
      <div class="metric-note" id="age">No data yet</div>
    </div>
  </section>

  <section class="two-col">
    <div class="card">
      <div class="card-head">
        <div>
          <div class="card-title" id="trendTitle">Live current trend</div>
          <div class="card-sub">Most recent meter readings</div>
        </div>
        <div class="legend">
          <span><i class="swatch" style="background:#4cc9b0"></i>Upstream</span>
          <span><i class="swatch" style="background:#e9ecef"></i>Main</span>
          <span><i class="swatch" style="background:#f6b94a"></i>Difference</span>
        </div>
      </div>
      <div class="chart-wrap">
        <svg id="chart" viewBox="0 0 900 260" preserveAspectRatio="none"></svg>
      </div>
    </div>

    <div class="card">
      <div class="card-head">
        <div>
          <div class="card-title">Monitoring summary</div>
          <div class="card-sub">Recent cloud data</div>
        </div>
      </div>
      <div class="kpi-stack">
        <div class="kpi">
          <div class="kpi-top"><span class="kpi-name">Possible bypass events</span><span>⚠</span></div>
          <div class="kpi-val" id="bypassCount">0</div>
        </div>
        <div class="kpi">
          <div class="kpi-top"><span class="kpi-name">Latest mismatch</span><span id="mismatchSymbol">ΔI</span></div>
          <div class="kpi-val"><span id="mismatchPct">--</span><span class="metric-unit">%</span></div>
        </div>
        <div class="kpi">
          <div class="kpi-top"><span class="kpi-name">Potential unmetered energy</span><span>∫ΔPdt</span></div>
          <div class="kpi-val"><span id="bypassEnergy">--</span><span class="metric-unit"> Wh</span></div>
          <div class="metric-note">Validated possible-bypass periods in this MCU session</div>
        </div>
        <div class="kpi">
          <div class="kpi-top"><span class="kpi-name">Meter ID</span><span>▣</span></div>
          <div class="kpi-val" id="meterId" style="font-size:18px">--</div>
        </div>
      </div>
      <div class="chips">
        <span class="chip">MQTT cloud broker</span>
        <span class="chip">PostgreSQL</span>
        <span class="chip">HTTPS dashboard</span>
      </div>
    </div>
  </section>

  <section class="card table-card">
    <div class="table-head">
      <div>
        <div class="card-title">Recent readings</div>
        <div class="card-sub">Newest measurements and bypass decisions</div>
      </div>
      <div class="card-sub" id="rowCount">0 records</div>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Time</th>
            <th id="histUpstreamHead">Upstream</th>
            <th id="histMainHead">Main</th>
            <th id="histDifferenceHead">Difference</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody id="hist"></tbody>
      </table>
    </div>
  </section>

  <div class="footer">
    <span>26G55 · Electrical Engineering Investigation Project</span>
    <span id="sourceText">Source: waiting for data</span>
  </div>

</div>

<script>
let lastReceivedAt = null;
let latestHistory = [];
let latestReading = null;
let viewMode = "current";

function num(v){
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
}
function finiteOrNull(v){
  if(v === null || v === undefined || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}
function fCurrent(v){ return num(v).toFixed(3); }
function fPower(v){
  const n=finiteOrNull(v);
  return n === null ? "--" : n.toFixed(1);
}
function fEnergy(v){
  const n=finiteOrNull(v);
  return n === null ? "--" : n.toFixed(3);
}
function pctDiff(u,m){
  const uu=finiteOrNull(u), mm=finiteOrNull(m);
  if(uu === null || mm === null) return null;
  const base = Math.max(Math.abs(uu),0.001);
  return Math.abs(uu-mm)/base*100;
}
function localTime(ts){
  const d = new Date(ts);
  return Number.isNaN(d.getTime()) ? "--:--:--" : d.toLocaleTimeString();
}
function esc(s){
  return String(s ?? "").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#039;"}[c]));
}

function measurementFields(row){
  if(viewMode === "power"){
    return {
      upstream: finiteOrNull(row?.upstream_power_W),
      main: finiteOrNull(row?.main_power_W),
      difference: finiteOrNull(row?.power_difference_W),
      unit: "W",
      decimals: 1
    };
  }
  return {
    upstream: finiteOrNull(row?.upstream_current_A),
    main: finiteOrNull(row?.main_current_A),
    difference: finiteOrNull(row?.difference_A),
    unit: "A",
    decimals: 3
  };
}

function formatMeasurement(v){
  const n=finiteOrNull(v);
  if(n === null) return "--";
  return viewMode === "power" ? n.toFixed(1) : n.toFixed(3);
}

function setStatus(status, currentDifference, bypassEnergyWh){
  const alarm = status !== "NORMAL";
  document.body.classList.toggle("alarm-state", alarm);
  document.body.classList.toggle("normal-state", !alarm);

  heroStatus.textContent = alarm ? "POSSIBLE BYPASS DETECTED" : "SYSTEM NORMAL";
  heroStatus.className = "hero-status " + (alarm ? "alarm-text" : "normal-text");

  statusBadge.textContent = alarm ? "ALARM" : "NORMAL";
  statusBadge.className = "status-badge " + (alarm ? "status-alarm" : "status-normal");

  const heroPanel = document.getElementById("heroPanel");
  heroPanel.style.borderColor = alarm ? "rgba(255,93,115,.45)" : "rgba(56,217,150,.25)";
  heroPanel.style.boxShadow = alarm
    ? "0 0 0 1px rgba(255,93,115,.12), 0 18px 55px rgba(0,0,0,.28), 0 0 45px rgba(255,93,115,.14)"
    : "0 0 0 1px rgba(56,217,150,.08), 0 18px 55px rgba(0,0,0,.28), 0 0 32px rgba(56,217,150,.08)";

  const diff=finiteOrNull(currentDifference);
  if(alarm){
    const e=finiteOrNull(bypassEnergyWh);
    const energyText = e === null ? "" : ` Potential unmetered energy recorded during validated bypass periods: ${fEnergy(e)} Wh.`;
    heroSub.textContent = diff === null
      ? "The MCU has reported a possible bypass condition. Immediate investigation is recommended." + energyText
      : `Current mismatch of ${fCurrent(diff)} A has exceeded the prototype threshold. Immediate investigation is recommended.${energyText}`;
  }else{
    heroSub.textContent = "Upstream and main-meter currents are within the expected range.";
  }
}

function drawChart(rows){
  const svg = document.getElementById("chart");
  let data = rows.slice(-30);
  svg.innerHTML = "";

  if(viewMode === "power"){
    data = data.filter(x =>
      finiteOrNull(x.upstream_power_W) !== null &&
      finiteOrNull(x.main_power_W) !== null &&
      finiteOrNull(x.power_difference_W) !== null
    );
  }

  if(data.length < 2){
    const msg = viewMode === "power"
      ? "Waiting for power data…"
      : "Waiting for trend data…";
    svg.innerHTML = `<text x="50%" y="50%" text-anchor="middle" fill="#91a4b7" font-size="14">${msg}</text>`;
    return;
  }

  const W=900,H=260,padL=46,padR=12,padT=14,padB=28;
  const us=data.map(x=>measurementFields(x).upstream);
  const ms=data.map(x=>measurementFields(x).main);
  const ds=data.map(x=>measurementFields(x).difference);
  const all=[...us,...ms,...ds].filter(v=>v!==null);
  let max=Math.max(...all,1);
  let min=Math.min(...all,0);
  const minimumSpan = viewMode === "power" ? 20 : .2;
  if(max-min < minimumSpan){
    const bump = viewMode === "power" ? 10 : .1;
    max += bump; min -= bump;
  }

  const x=i=>padL+(i/(data.length-1))*(W-padL-padR);
  const y=v=>padT+(max-v)/(max-min)*(H-padT-padB);

  for(let i=0;i<5;i++){
    const yy=padT+i*(H-padT-padB)/4;
    const value=max-i*(max-min)/4;
    const label=viewMode === "power" ? value.toFixed(0) : value.toFixed(1);
    svg.insertAdjacentHTML("beforeend",
      `<line x1="${padL}" x2="${W-padR}" y1="${yy}" y2="${yy}" stroke="#34383d" stroke-width="1"/>
       <text x="${padL-8}" y="${yy+4}" text-anchor="end" fill="#8e949a" font-size="11">${label}</text>`);
  }

  function path(vals,color){
    const d=vals.map((v,i)=>(i?"L":"M")+x(i).toFixed(1)+","+y(v).toFixed(1)).join(" ");
    svg.insertAdjacentHTML("beforeend",
      `<path d="${d}" fill="none" stroke="${color}" stroke-width="2.4" vector-effect="non-scaling-stroke" stroke-linejoin="round" stroke-linecap="round"/>`);
  }

  path(us,"#4cc9b0");
  path(ms,"#e9ecef");
  path(ds,"#f6b94a");

  const first = localTime(data[0].timestamp_utc);
  const last = localTime(data[data.length-1].timestamp_utc);
  svg.insertAdjacentHTML("beforeend",
    `<text x="${padL}" y="${H-6}" fill="#8e949a" font-size="11">${first}</text>
     <text x="${W-padR}" y="${H-6}" text-anchor="end" fill="#8e949a" font-size="11">${last}</text>`);
}

function updateViewLabels(){
  const power = viewMode === "power";

  currentViewBtn.classList.toggle("active", !power);
  powerViewBtn.classList.toggle("active", power);

  upstreamLabel.textContent = power ? "Upstream power" : "Upstream current";
  mainLabel.textContent = power ? "Main meter power" : "Main meter current";
  differenceLabel.textContent = power ? "Power difference" : "Current difference";
  upstreamUnit.textContent = power ? "W" : "A";
  mainUnit.textContent = power ? "W" : "A";
  differenceUnit.textContent = power ? "W" : "A";
  trendTitle.textContent = power ? "Live power trend" : "Live current trend";
  mismatchSymbol.textContent = power ? "ΔP" : "ΔI";
  histUpstreamHead.textContent = "Upstream";
  histMainHead.textContent = "Main";
  histDifferenceHead.textContent = "Difference";
}

function renderData(){
  updateViewLabels();

  if(latestReading && latestReading.status !== "NO_DATA"){
    const vals=measurementFields(latestReading);
    u.textContent=formatMeasurement(vals.upstream);
    m.textContent=formatMeasurement(vals.main);
    d.textContent=formatMeasurement(vals.difference);
    t.textContent=localTime(latestReading.timestamp_utc);

    const mismatch=pctDiff(vals.upstream,vals.main);
    mismatchPct.textContent=mismatch === null ? "--" : mismatch.toFixed(1);
    differenceNote.textContent=mismatch === null
      ? (viewMode === "power" ? "Power data unavailable" : "Mismatch unavailable")
      : `${mismatch.toFixed(1)}% of upstream reading`;

    meterId.textContent=latestReading.meter_id || "--";
    bypassEnergy.textContent=fEnergy(latestReading.possible_bypass_energy_Wh);
    sourceText.textContent="Source: " + (latestReading.source || "mqtt");
    setStatus(latestReading.status, latestReading.difference_A, latestReading.possible_bypass_energy_Wh);
    lastReceivedAt = new Date(latestReading.timestamp_utc).getTime();
  }

  const recent = latestHistory.slice(-20).reverse();
  hist.innerHTML="";
  let bypass=0;

  latestHistory.forEach(x=>{ if(x.status !== "NORMAL") bypass++; });
  bypassCount.textContent=bypass;
  rowCount.textContent=`${recent.length} records`;

  recent.forEach(x=>{
    const alarm=x.status!=="NORMAL";
    const vals=measurementFields(x);
    const tr=document.createElement("tr");
    if(alarm) tr.className="alarm-row";
    tr.innerHTML=`
      <td>${esc(localTime(x.timestamp_utc))}</td>
      <td>${formatMeasurement(vals.upstream)} ${vals.unit}</td>
      <td>${formatMeasurement(vals.main)} ${vals.unit}</td>
      <td>${formatMeasurement(vals.difference)} ${vals.unit}</td>
      <td><span class="badge ${alarm?"badge-alarm":"badge-normal"}">${esc(x.status)}</span></td>`;
    hist.appendChild(tr);
  });

  drawChart(latestHistory);
}

function updateAge(){
  const pill=document.getElementById("connectionPill");
  if(!lastReceivedAt){
    age.textContent="No data yet";
    return;
  }
  const sec=Math.floor((Date.now()-lastReceivedAt)/1000);
  age.textContent = sec < 5 ? "Updated just now" : `Updated ${sec}s ago`;

  if(sec > 20){
    pill.classList.add("offline");
    liveText.textContent="STALE DATA";
  }else{
    pill.classList.remove("offline");
    liveText.textContent="LIVE";
  }
}

async function refresh(){
  try{
    const [lr,hr] = await Promise.all([
      fetch("/api/latest",{cache:"no-store"}),
      fetch("/api/history",{cache:"no-store"})
    ]);

    if(!lr.ok || !hr.ok) throw new Error("API error");

    latestReading = await lr.json();
    latestHistory = await hr.json();
    renderData();

  }catch(e){
    connectionPill.classList.add("offline");
    liveText.textContent="BACKEND ERROR";
    heroStatus.textContent="CONNECTION PROBLEM";
    heroSub.textContent="The browser could not retrieve the latest cloud data.";
  }
}

currentViewBtn.addEventListener("click",()=>{
  viewMode="current";
  renderData();
});
powerViewBtn.addEventListener("click",()=>{
  viewMode="power";
  renderData();
});

refresh();
setInterval(refresh,2000);
setInterval(updateAge,1000);
</script>
</body>
</html>
"""

def using_postgres():
    return bool(DATABASE_URL and psycopg2)

def pg_connect():
    return psycopg2.connect(DATABASE_URL)

def ensure_sqlite_column(con, name, sql_type):
    existing = {row[1] for row in con.execute("PRAGMA table_info(readings)").fetchall()}
    if name not in existing:
        con.execute(f'ALTER TABLE readings ADD COLUMN "{name}" {sql_type}')


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
                        "upstream_power_W" DOUBLE PRECISION,
                        "main_power_W" DOUBLE PRECISION,
                        "power_difference_W" DOUBLE PRECISION,
                        "voltage_V" DOUBLE PRECISION,
                        power_factor DOUBLE PRECISION,
                        "possible_bypass_energy_Wh" DOUBLE PRECISION,
                        status TEXT NOT NULL,
                        source TEXT
                    )
                """)
                # Existing Render databases already contain the current-only table.
                # Add the new optional fields without deleting historical readings.
                cur.execute('ALTER TABLE readings ADD COLUMN IF NOT EXISTS "upstream_power_W" DOUBLE PRECISION')
                cur.execute('ALTER TABLE readings ADD COLUMN IF NOT EXISTS "main_power_W" DOUBLE PRECISION')
                cur.execute('ALTER TABLE readings ADD COLUMN IF NOT EXISTS "power_difference_W" DOUBLE PRECISION')
                cur.execute('ALTER TABLE readings ADD COLUMN IF NOT EXISTS "voltage_V" DOUBLE PRECISION')
                cur.execute('ALTER TABLE readings ADD COLUMN IF NOT EXISTS power_factor DOUBLE PRECISION')
                cur.execute('ALTER TABLE readings ADD COLUMN IF NOT EXISTS "possible_bypass_energy_Wh" DOUBLE PRECISION')
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
                    upstream_power_W REAL,
                    main_power_W REAL,
                    power_difference_W REAL,
                    voltage_V REAL,
                    power_factor REAL,
                    possible_bypass_energy_Wh REAL,
                    status TEXT NOT NULL,
                    source TEXT
                )
            """)
            ensure_sqlite_column(con, "upstream_power_W", "REAL")
            ensure_sqlite_column(con, "main_power_W", "REAL")
            ensure_sqlite_column(con, "power_difference_W", "REAL")
            ensure_sqlite_column(con, "voltage_V", "REAL")
            ensure_sqlite_column(con, "power_factor", "REAL")
            ensure_sqlite_column(con, "possible_bypass_energy_Wh", "REAL")
            con.commit()
        print("Database ready: SQLite fallback")


def optional_float(data, key):
    value = data.get(key)
    if value is None or value == "":
        return None
    return float(value)


def save_reading(data):
    ts = datetime.now(timezone.utc)
    row = (
        ts,
        str(data.get("meter_id", "main001")),
        float(data["upstream_current_A"]),
        float(data["main_current_A"]),
        float(data["difference_A"]),
        optional_float(data, "upstream_power_W"),
        optional_float(data, "main_power_W"),
        optional_float(data, "power_difference_W"),
        optional_float(data, "voltage_V"),
        optional_float(data, "power_factor"),
        optional_float(data, "possible_bypass_energy_Wh"),
        str(data["status"]),
        str(data.get("source", "mqtt")),
    )

    if using_postgres():
        with pg_connect() as con:
            with con.cursor() as cur:
                cur.execute("""
                    INSERT INTO readings
                    (timestamp_utc,meter_id,upstream_current_A,main_current_A,difference_A,
                     "upstream_power_W","main_power_W","power_difference_W","voltage_V",power_factor,
                     "possible_bypass_energy_Wh",status,source)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, row)
    else:
        with sqlite3.connect(SQLITE_PATH) as con:
            con.execute("""
                INSERT INTO readings
                (timestamp_utc,meter_id,upstream_current_A,main_current_A,difference_A,
                 upstream_power_W,main_power_W,power_difference_W,voltage_V,power_factor,possible_bypass_energy_Wh,status,source)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (ts.isoformat(),) + row[1:])
            con.commit()


def serialize_row(row):
    if row is None:
        return None

    d = dict(row)

    # PostgreSQL lowercases unquoted legacy current column names.
    if "upstream_current_a" in d:
        d["upstream_current_A"] = d.pop("upstream_current_a")
    if "main_current_a" in d:
        d["main_current_A"] = d.pop("main_current_a")
    if "difference_a" in d:
        d["difference_A"] = d.pop("difference_a")

    # New power/voltage columns are quoted in PostgreSQL so their API names
    # remain exactly the same. These fallbacks also tolerate an unquoted DB.
    if "upstream_power_w" in d:
        d["upstream_power_W"] = d.pop("upstream_power_w")
    if "main_power_w" in d:
        d["main_power_W"] = d.pop("main_power_w")
    if "power_difference_w" in d:
        d["power_difference_W"] = d.pop("power_difference_w")
    if "voltage_v" in d:
        d["voltage_V"] = d.pop("voltage_v")
    if "possible_bypass_energy_wh" in d:
        d["possible_bypass_energy_Wh"] = d.pop("possible_bypass_energy_wh")

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
                cur.execute("SELECT * FROM readings ORDER BY id DESC LIMIT %s",(limit,))
                rows=[serialize_row(r) for r in cur.fetchall()]
                return list(reversed(rows))

    with sqlite3.connect(SQLITE_PATH) as con:
        con.row_factory=sqlite3.Row
        rows=con.execute("SELECT * FROM readings ORDER BY id DESC LIMIT ?",(limit,)).fetchall()
        return [serialize_row(r) for r in reversed(rows)]

def on_connect(client, userdata, flags, reason_code, properties=None):
    print("MQTT connected:", reason_code)
    client.subscribe(TOPIC)
    print("Subscribed to:", TOPIC)

def on_message(client, userdata, msg):
    try:
        data=json.loads(msg.payload.decode("utf-8"))
        save_reading(data)
        print("Stored:", data)
    except Exception as e:
        print("Bad MQTT message:", e)

def mqtt_loop():
    while True:
        try:
            client=mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
            client.on_connect=on_connect
            client.on_message=on_message
            print(f"Connecting MQTT: {BROKER}:{MQTT_PORT}")
            client.connect(BROKER,MQTT_PORT,60)
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
    row=latest_row()
    return jsonify({"status":"NO_DATA"} if row is None else row)

@app.get("/api/history")
def history():
    return jsonify(history_rows(100))

init_db()
threading.Thread(target=mqtt_loop,daemon=True,name="mqtt-subscriber").start()

if __name__=="__main__":
    port=int(os.getenv("PORT","5000"))
    app.run(host="0.0.0.0",port=port,debug=False)
