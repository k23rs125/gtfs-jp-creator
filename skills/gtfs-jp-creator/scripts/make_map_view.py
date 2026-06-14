#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_map_view.py  ―  stops.txt（と任意で shapes.txt / trips）から
検証用の停留所マップ(1枚のHTML)を生成する。

設計方針（引き継ぎドキュメントに準拠）:
- 決定的処理のみ。推測しない。決まらない/おかしい点は「正しく失敗」させ、
  地図に無理に置かず、注記で人に確認を促す。
- 生成HTMLは1ファイル完結・データ埋め込み(外部送信なし)。インストール不要。
- 座標の妥当性チェック:
    (1) 日本の範囲外 → 値が壊れている/緯度経度の取り違えの疑い（地図に置かない）
    (2) 想定bbox外（任意指定）→ P11の同名誤マッチ等で別の場所に飛んだ疑い（橙で強調）
- shapes(走行ルート線)は任意。--shapes を付けたときだけ描く。
  --trips（trips_with_shapes.txt）を併せて渡すと、便名でルートを選べる。

使い方:
    python make_map_view.py stops.txt
    python make_map_view.py stops.txt --shapes shapes.txt --trips trips_with_shapes.txt \
        --title "インガット号（B日程）" --bbox 130.34,33.18,130.52,33.32 --out map_view.html
"""

import argparse
import csv
import html
import json
import sys
from collections import defaultdict, Counter

JP_LAT_MIN, JP_LAT_MAX = 24.0, 46.0
JP_LON_MIN, JP_LON_MAX = 122.0, 154.0


def classify(lat_s, lon_s, bbox):
    lat_s = (lat_s or "").strip()
    lon_s = (lon_s or "").strip()
    if lat_s == "" or lon_s == "":
        return "no_coord", None, None
    try:
        lat = float(lat_s); lon = float(lon_s)
    except ValueError:
        return "bad_value", None, None
    if not (JP_LAT_MIN <= lat <= JP_LAT_MAX and JP_LON_MIN <= lon <= JP_LON_MAX):
        return "out_of_japan", lat, lon
    if bbox is not None:
        lon_min, lat_min, lon_max, lat_max = bbox
        if not (lat_min <= lat <= lat_max and lon_min <= lon <= lon_max):
            return "out_of_bbox", lat, lon
    return "ok", lat, lon


def read_stops(path, bbox):
    stops = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        required = {"stop_id", "stop_name", "stop_lat", "stop_lon"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            sys.exit("ERROR: stops.txt に必要な列がありません: " + ", ".join(sorted(missing)))
        for row in reader:
            status, lat, lon = classify(row.get("stop_lat"), row.get("stop_lon"), bbox)
            stops.append({"id": row.get("stop_id", "").strip(),
                          "name": row.get("stop_name", "").strip(),
                          "lat": lat, "lon": lon, "status": status})
    return stops


def read_shapes(path):
    """shapes.txt を shape_id ごとの [[lat,lon],...]（sequence順）にまとめる。"""
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        required = {"shape_id", "shape_pt_lat", "shape_pt_lon", "shape_pt_sequence"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            sys.exit("ERROR: shapes.txt に必要な列がありません: " + ", ".join(sorted(missing)))
        rows = list(reader)
    grouped = defaultdict(list)
    bad = 0
    for r in rows:
        try:
            seq = int(r["shape_pt_sequence"])
            lat = round(float(r["shape_pt_lat"]), 6)
            lon = round(float(r["shape_pt_lon"]), 6)
        except (ValueError, KeyError):
            bad += 1
            continue
        grouped[r["shape_id"].strip()].append((seq, lat, lon))
    shapes = {}
    for sid, pts in grouped.items():
        pts.sort(key=lambda x: x[0])           # sequenceで並べる（決定的）
        shapes[sid] = [[lat, lon] for _, lat, lon in pts]
    return shapes, bad


def read_trips(path):
    """trips(_with_shapes).txt を読む。shape_idを持つ便だけ採用。"""
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if "shape_id" not in (reader.fieldnames or []):
            sys.exit("ERROR: trips に shape_id 列がありません（trips_with_shapes.txt を渡してください）")
        trips = []
        for r in reader:
            sid = (r.get("shape_id") or "").strip()
            if not sid:
                continue
            trips.append({"trip_id": (r.get("trip_id") or "").strip(),
                          "headsign": (r.get("trip_headsign") or "").strip(),
                          "direction_id": (r.get("direction_id") or "").strip(),
                          "shape_id": sid})
    return trips


def read_stop_times(path):
    """stop_times.txt を 便ごとの [{seq, stop_id, time}] (停車順) にまとめる。
       time は departure_time を優先、無ければ arrival_time。"""
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        req = {"trip_id", "stop_id", "stop_sequence"}
        missing = req - set(reader.fieldnames or [])
        if missing:
            sys.exit("ERROR: stop_times.txt に必要な列がありません: " + ", ".join(sorted(missing)))
        by_trip = defaultdict(list)
        for r in reader:
            try:
                seq = int(r["stop_sequence"])
            except (ValueError, KeyError):
                continue
            t = (r.get("departure_time") or r.get("arrival_time") or "").strip()
            by_trip[r["trip_id"].strip()].append({
                "seq": seq, "stop_id": r["stop_id"].strip(), "time": t})
    for tid in by_trip:
        by_trip[tid].sort(key=lambda x: x["seq"])
    return dict(by_trip)


def parse_bbox(s):
    if s is None:
        return None
    try:
        parts = [float(x) for x in s.split(",")]
        assert len(parts) == 4
    except (ValueError, AssertionError):
        sys.exit("ERROR: --bbox は 'lon_min,lat_min,lon_max,lat_max' 形式で指定してください")
    return tuple(parts)


TEMPLATE = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>停留所マップ ― __TITLE__</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css"/>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<style>
  html, body { margin: 0; padding: 0; height: 100%; font-family: "Meiryo", system-ui, sans-serif; }
  #wrap { display: flex; flex-direction: column; height: 100%; }
  #bar { background: #1E4A70; color: #fff; padding: 10px 16px;
         display: flex; align-items: baseline; gap: 14px; flex-wrap: wrap; }
  #bar h1 { font-size: 16px; margin: 0; font-weight: 700; }
  #bar .sub { font-size: 12px; color: #BBD0E2; }
  #legend { display: flex; gap: 16px; flex-wrap: wrap; align-items: center;
            background: #16395A; color: #DCE8F2; font-size: 12px; padding: 6px 16px; }
  #legend .item { display: flex; align-items: center; gap: 6px; }
  #legend .dot { width: 12px; height: 12px; border-radius: 50%; display: inline-block; border: 2px solid; }
  .dot-ok { background: #5BA8C9; border-color: #1C7293; }
  .dot-bbox { background: #F0B354; border-color: #B5791A; }
  #ctrl { background: #16395A; color: #DCE8F2; font-size: 12px; padding: 6px 16px;
          display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  #ctrl select { font-size: 12px; padding: 3px 6px; max-width: 70vw; }
  #note { background: #FBEED9; color: #7A4B12; font-size: 12px;
          padding: 6px 16px; border-bottom: 1px solid #E2C58A; white-space: pre-line; }
  #map { flex: 1; }
  .stop-label { font-size: 13px; font-weight: 700; color: #1E4A70; }
  .stop-id { font-size: 11px; color: #6b7785; }
  .stop-warn { font-size: 11px; color: #B5791A; font-weight: 700; }
  .stop-time { font-size: 12px; color: #145A32; font-weight: 700; }
  .seq-badge { display: inline-block; min-width: 16px; padding: 0 4px; border-radius: 9px;
               background: #1C7293; color: #fff; font-size: 11px; font-weight: 700; text-align: center; }
  .seq-marker .seq-num { width: 20px; height: 20px; line-height: 20px; border-radius: 50%;
               background: #1C7293; color: #fff; font-size: 11px; font-weight: 700; text-align: center;
               border: 2px solid #fff; box-shadow: 0 0 2px rgba(0,0,0,0.4); }
  #trip-note { background: #E7F0F7; color: #1E4A70; font-size: 12px;
               padding: 6px 16px; border-bottom: 1px solid #B9D2E6; display: none; }
</style>
</head>
<body>
<div id="wrap">
  <div id="bar">
    <h1>停留所マップ</h1>
    <span class="sub">__TITLE__／停留所をクリックすると名前が出ます</span>
  </div>
  <div id="legend">
    <span class="item"><span class="dot dot-ok"></span>正常（想定範囲内）</span>
    <span class="item"><span class="dot dot-bbox"></span>想定範囲外（要確認）</span>
    <span class="item" id="bbox-info"></span>
  </div>
  <div id="ctrl" style="display:none">
    <label for="shape-select">ルート表示：</label>
    <select id="shape-select"></select>
    <span id="ctrl-info"></span>
  </div>
  <div id="trip-note"></div>
  <div id="note"></div>
  <div id="map"></div>
</div>
<script>
const STOPS  = __DATA__;
const BBOX   = __BBOX__;     // [lon_min,lat_min,lon_max,lat_max] または null
const SHAPES = __SHAPES__;   // {shape_id:[[lat,lon],...], ...}（無ければ {}）
const TRIPS  = __TRIPS__;    // [{trip_id,headsign,direction_id,shape_id},...]（無ければ []）
const STOP_TIMES = __STOP_TIMES__; // {trip_id:[{seq,stop_id,time},...], ...}（無ければ {}）

const STYLE = {
  ok:          { radius: 6, color: "#1C7293", weight: 2, fillColor: "#5BA8C9", fillOpacity: 0.9 },
  out_of_bbox: { radius: 7, color: "#B5791A", weight: 3, fillColor: "#F0B354", fillOpacity: 0.95 }
};
const WARN_TEXT = {
  out_of_bbox:  "⚠ 想定範囲外に配置されています（要確認）",
  out_of_japan: "⚠ 日本の範囲外の座標（値の誤り・緯度経度取り違えの疑い）",
  bad_value:    "⚠ 座標の値が不正",
  no_coord:     "座標未補完（P11未マッチ等）"
};

const placed = STOPS.filter(s => s.status === "ok" || s.status === "out_of_bbox");
const issues = STOPS.filter(s => s.status !== "ok");

const order = ["out_of_bbox", "out_of_japan", "bad_value", "no_coord"];
const lines = [];
for (const st of order) {
  const items = issues.filter(s => s.status === st);
  if (items.length === 0) continue;
  const names = items.map(s => s.name + "(" + s.id + ")").join("、");
  lines.push(WARN_TEXT[st] + "：" + names + "（計" + items.length + "件）");
}
const note = document.getElementById("note");
if (lines.length > 0) {
  note.textContent = lines.join("\n");
} else {
  note.textContent = "検出された座標の異常はありません（全" + STOPS.length + "件）。";
  note.style.background = "#E6F2E6"; note.style.color = "#2C5E2C"; note.style.borderColor = "#B9D9B9";
}

const bboxInfo = document.getElementById("bbox-info");
bboxInfo.textContent = BBOX ? "想定範囲 bbox: " + BBOX.join(", ")
                            : "（bbox未指定：範囲外判定は行いません）";

// 地図
const map = L.map("map");
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",
  { maxZoom: 19, attribution: "&copy; OpenStreetMap contributors" }).addTo(map);

// ルート線は停留所点の下のレイヤに置く（点が隠れないように）
map.createPane("shapesPane");
map.getPane("shapesPane").style.zIndex = 350;   // overlayPane(400)より下
const shapeLayer = L.layerGroup().addTo(map);

function drawShape(coords, style) {
  if (!coords || coords.length < 2) return;
  L.polyline(coords, Object.assign({ pane: "shapesPane" }, style)).addTo(shapeLayer);
}
const ALL_STYLE   = { color: "#2C6E9B", weight: 3, opacity: 0.55 };
const FAINT_STYLE = { color: "#9AA7B2", weight: 2, opacity: 0.30 };
const HILITE_STYLE= { color: "#C0392B", weight: 5, opacity: 0.95 };

function redraw(sel) {
  shapeLayer.clearLayers();
  if (sel === "__none__") { resetStops(); setTripNote(null); return; }
  if (sel === "__all__") {
    for (const sid in SHAPES) drawShape(SHAPES[sid], ALL_STYLE);
    resetStops(); setTripNote(null);
    return;
  }
  // 特定の便/shapeを選択：背景に全ルートを薄く、選択分を濃く
  let targetShape = sel;
  let isTrip = false;
  if (TRIPS.length > 0) {
    const t = TRIPS.find(x => x.trip_id === sel);
    if (t) { targetShape = t.shape_id; isTrip = true; }
  }
  for (const sid in SHAPES) if (sid !== targetShape) drawShape(SHAPES[sid], FAINT_STYLE);
  drawShape(SHAPES[targetShape], HILITE_STYLE);
  // 便が選ばれていて stop_times があれば、停車停留所を番号順に強調
  if (isTrip && STOP_TIMES[sel]) {
    const noCoord = highlightTripStops(sel);
    setTripNote(sel, noCoord);
  } else {
    resetStops(); setTripNote(null);
  }
}

// 便選択時、座標未補完の停車停留所を注記で知らせる
function setTripNote(tripId, noCoordServed) {
  const tn = document.getElementById("trip-note");
  if (!tripId || !noCoordServed || noCoordServed.length === 0) { tn.style.display = "none"; tn.textContent = ""; return; }
  const names = noCoordServed.map(x => x.s.name + "(" + x.s.id + ")・順" + x.seq).join("、");
  tn.style.display = "block";
  tn.textContent = "この便は座標未補完の停留所に停車します（地図に表示できません）：" + names;
}

// プルダウン設定は停留所マーカー定義の後で行う（初期redrawがマーカーを参照するため）


// 停留所の点（marker[stop_id] で後から番号・濃淡を付け替えられるよう保持）
const markerById = {};
const stopById = {};
STOPS.forEach(s => { stopById[s.id] = s; });
const bounds = [];
placed.forEach(s => {
  const style = STYLE[s.status] || STYLE.ok;
  const m = L.circleMarker([s.lat, s.lon], style).addTo(map);
  m.bindPopup(buildStopPopup(s, null, null));
  markerById[s.id] = m;
  bounds.push([s.lat, s.lon]);
});

// 番号ラベル（停車順）用のレイヤ。便選択時だけ表示する。
const numberLayer = L.layerGroup().addTo(map);

function buildStopPopup(s, seq, time) {
  let p = '';
  if (seq !== null) p += '<span class="seq-badge">' + seq + '</span> ';
  p += '<span class="stop-label">' + s.name + '</span><br><span class="stop-id">' + s.id + '</span>';
  if (time) p += '<br><span class="stop-time">発車 ' + time + '</span>';
  if (s.status !== "ok") p += '<br><span class="stop-warn">' + WARN_TEXT[s.status] + '</span>';
  return p;
}

// 便選択に応じて停留所の強調を更新する
const DIM_STYLE = { radius: 4, color: "#B7C0C8", weight: 1, fillColor: "#D6DCE1", fillOpacity: 0.7 };
const SERVED_STYLE = { radius: 8, color: "#1C7293", weight: 3, fillColor: "#2E9BCB", fillOpacity: 0.95 };

function resetStops() {
  numberLayer.clearLayers();
  placed.forEach(s => {
    const style = STYLE[s.status] || STYLE.ok;
    markerById[s.id].setStyle(style).setRadius(style.radius);
    markerById[s.id].setPopupContent(buildStopPopup(s, null, null));
  });
}

function highlightTripStops(tripId) {
  numberLayer.clearLayers();
  const seqList = STOP_TIMES[tripId] || [];
  const servedIds = new Set(seqList.map(x => x.stop_id));
  // まず全部を薄く
  placed.forEach(s => {
    if (!servedIds.has(s.id)) {
      markerById[s.id].setStyle(DIM_STYLE).setRadius(DIM_STYLE.radius);
      markerById[s.id].setPopupContent(buildStopPopup(s, null, null));
    }
  });
  // 停車停留所を濃く＋番号＋時刻。座標が無い停留所は注記に回す。
  const noCoordServed = [];
  seqList.forEach(item => {
    const s = stopById[item.stop_id];
    if (!s) return;
    if (s.lat === null || s.lon === null) { noCoordServed.push({s:s, seq:item.seq, time:item.time}); return; }
    const m = markerById[s.id];
    if (!m) return;
    m.setStyle(SERVED_STYLE).setRadius(SERVED_STYLE.radius);
    m.setPopupContent(buildStopPopup(s, item.seq, item.time));
    // 番号ラベル
    const icon = L.divIcon({ className: "seq-marker", html: '<div class="seq-num">' + item.seq + '</div>', iconSize: [20,20], iconAnchor: [10,10] });
    L.marker([s.lat, s.lon], { icon: icon, interactive: false, pane: "markerPane" }).addTo(numberLayer);
  });
  return noCoordServed;
}

if (bounds.length > 0) map.fitBounds(bounds, { padding: [30, 30] });
else map.setView([36.0, 138.0], 5);

// プルダウンの組み立て（shapesがある時だけ表示）。マーカー定義後に実行。
const shapeIds = Object.keys(SHAPES);
if (shapeIds.length > 0) {
  document.getElementById("ctrl").style.display = "flex";
  const sel = document.getElementById("shape-select");
  const add = (val, text) => { const o = document.createElement("option"); o.value = val; o.textContent = text; sel.appendChild(o); };
  add("__all__", "全ルートを薄く重ねる");
  add("__none__", "ルート非表示（点だけ）");
  if (TRIPS.length > 0) {
    TRIPS.forEach(t => add(t.trip_id,
      t.trip_id + "／" + (t.headsign || "(行先なし)") + "（方向" + t.direction_id + "）"));
    const extra = (Object.keys(STOP_TIMES).length > 0) ? "／便を選ぶと停車停留所を番号順に強調" : "";
    document.getElementById("ctrl-info").textContent = "便数 " + TRIPS.length + " ／ ルート " + shapeIds.length + "本" + extra;
  } else {
    shapeIds.forEach(sid => add(sid, sid));
    document.getElementById("ctrl-info").textContent = "ルート " + shapeIds.length + "本（trips未指定のためshape_idで表示）";
  }
  sel.value = "__all__";
  sel.addEventListener("change", e => redraw(e.target.value));
  redraw("__all__");
}
</script>
</body>
</html>
"""


def build_html(stops, title, bbox, shapes, trips, stop_times):
    data = [{"id": s["id"], "name": s["name"], "lat": s["lat"], "lon": s["lon"], "status": s["status"]} for s in stops]
    out = TEMPLATE
    out = out.replace("__TITLE__", html.escape(title))
    out = out.replace("__DATA__", json.dumps(data, ensure_ascii=False))
    out = out.replace("__BBOX__", json.dumps(list(bbox)) if bbox is not None else "null")
    out = out.replace("__SHAPES__", json.dumps(shapes, ensure_ascii=False))
    out = out.replace("__TRIPS__", json.dumps(trips, ensure_ascii=False))
    out = out.replace("__STOP_TIMES__", json.dumps(stop_times, ensure_ascii=False))
    return out


def main():
    ap = argparse.ArgumentParser(description="stops.txt（+任意でshapes/trips）から検証用の停留所マップHTMLを生成")
    ap.add_argument("stops", help="stops.txt のパス")
    ap.add_argument("--shapes", default=None, help="shapes.txt のパス（指定するとルート線を描く）")
    ap.add_argument("--trips", default=None, help="trips_with_shapes.txt のパス（便名でルートを選べる）")
    ap.add_argument("--stop-times", dest="stop_times", default=None,
                    help="stop_times.txt のパス（便を選ぶと停車停留所を番号順に強調・時刻表示）")
    ap.add_argument("--out", default="map_view.html", help="出力HTMLパス（既定: map_view.html）")
    ap.add_argument("--title", default="停留所マップ", help="バーに出すサブタイトル")
    ap.add_argument("--bbox", default=None, help="想定範囲 'lon_min,lat_min,lon_max,lat_max'。指定すると範囲外を橙で強調")
    args = ap.parse_args()

    bbox = parse_bbox(args.bbox)
    stops = read_stops(args.stops, bbox)

    shapes, bad_shape_pts = ({}, 0)
    if args.shapes:
        shapes, bad_shape_pts = read_shapes(args.shapes)

    trips = []
    if args.trips:
        if not args.shapes:
            sys.exit("ERROR: --trips を使うには --shapes も必要です")
        trips = read_trips(args.trips)
        # tripsが参照するshapeがshapesに無い場合は除外して警告
        valid = set(shapes)
        dropped = [t for t in trips if t["shape_id"] not in valid]
        trips = [t for t in trips if t["shape_id"] in valid]
        if dropped:
            print("WARN: shapes.txtに無いshape_idを参照する便を%d件除外しました: %s"
                  % (len(dropped), ", ".join(t["trip_id"] for t in dropped)), file=sys.stderr)

    stop_times = {}
    if args.stop_times:
        stop_times = read_stop_times(args.stop_times)
        # tripsを指定している場合、tripsに無い便のstop_timesは載せない（HTML肥大化防止）
        if trips:
            trip_ids = {t["trip_id"] for t in trips}
            stop_times = {k: v for k, v in stop_times.items() if k in trip_ids}

    out_html = build_html(stops, args.title, bbox, shapes, trips, stop_times)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(out_html)

    counts = Counter(s["status"] for s in stops)
    print("生成しました: " + args.out, file=sys.stderr)
    print("  停留所 計%d件" % len(stops), file=sys.stderr)
    for st in ["ok", "out_of_bbox", "out_of_japan", "bad_value", "no_coord"]:
        if counts.get(st):
            print("    %-12s %d件" % (st, counts[st]), file=sys.stderr)
    if shapes:
        total_pts = sum(len(v) for v in shapes.values())
        print("  ルート %d本（計%d点）" % (len(shapes), total_pts), file=sys.stderr)
        if bad_shape_pts:
            print("    （数値解釈できず除外した点 %d）" % bad_shape_pts, file=sys.stderr)
    if trips:
        print("  便 %d件（ルート選択に使用）" % len(trips), file=sys.stderr)
    if stop_times:
        total_st = sum(len(v) for v in stop_times.values())
        print("  stop_times %d便（計%d停車・便選択で停留所強調）" % (len(stop_times), total_st), file=sys.stderr)


if __name__ == "__main__":
    main()
