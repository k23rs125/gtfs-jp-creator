# -*- coding: utf-8 -*-
"""Step2 決定スペック（LLMの判断）＋ 抽出JSON → 構造化中間JSON（決定的）。
LLMは「判断」だけを decision spec として出力し，本スクリプトが cells を決定的に展開する。
これにより Step2 を「LLMの判断品質」で比較できる（転記はLLM非依存）。

decision spec（LLM出力）の例:
{
  "routes": [
    {"route_id":"R01","route_long_name":"北野線（循環）","blocks":[0],"circular":true},
    {"route_id":"R02","route_long_name":"弓削線","blocks":[1,2]}
  ],
  "block_direction": {"0":0, "1":0, "2":1},
  "exclude_reserve": true,
  "stop_key": "name",
  "service": {"service_id":"B_TTS","mon":0,"tue":1,"wed":0,"thu":1,"fri":0,"sat":1,"sun":0,
              "start_date":"20240601","end_date":"20270331"},
  "fare_price": 200,
  "block_headsign": {"0":"左回り","1":"右回り"},     # 循環/往復の方向名（任意）
  "agency": {"agency_id":"8000020402044","agency_name":"直方市",
             "agency_url":"https://...","agency_phone":"0949-..."},   # 任意。無ければ未定プレースホルダ
  "agency_jp": {"agency_official_name":"直方市","agency_zip_number":"8228501",
                "agency_address":"...","agency_president_pos":null,"agency_president_name":null},
  "calendar_dates": []                              # 任意。祝日・年末年始の運休/運行
}

agency を spec に載せると agency.txt/agency_jp.txt に反映され、fare_attributes.agency_id も
本体 agency に一致させる（AGENCY_TBD 固定をやめ desync を防ぐ）。手書き構造化スクリプト不要。
"""
import argparse, json

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--extract", required=True)
    ap.add_argument("--decisions", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    ex = json.load(open(a.extract, encoding="utf-8"))
    dec = json.load(open(a.decisions, encoding="utf-8"))
    blocks = ex["blocks"]

    exclude_reserve = dec.get("exclude_reserve", True)
    # 座標方式は無番号セル＝案内文ノイズなので除外。Excel等は全停留所が無番号なので除外しない。
    exclude_unnumbered = dec.get("exclude_unnumbered", True)
    stop_key = dec.get("stop_key", "name")
    bdir = {int(k): v for k, v in dec.get("block_direction", {}).items()}

    def cell_excluded(c):
        if exclude_reserve and c.get("reserve"):
            return True
        if exclude_unnumbered and c.get("num") is None:   # 無番号ノイズ（案内文混入）
            return True
        return False

    # 停留所レジストリ
    reg = {}   # key -> (name, num_for_sort)
    for b in blocks:
        for t in b["trips"]:
            for c in t["cells"]:
                if cell_excluded(c):
                    continue
                nm = (c.get("name") or "").strip()
                num = c.get("num")
                num = int(num) if num is not None else None
                key = nm if stop_key == "name" else num
                if key not in reg:
                    reg[key] = (nm, num if num is not None else 99999)
    # S採番（代表num昇順→名称。無番号は登場順を保つため大きな番号扱い）
    ordered = sorted(reg, key=lambda k: (reg[k][1], reg[k][0]))
    sid_of = {k: f"S{i:03d}" for i, k in enumerate(ordered, 1)}
    stops = [{"stop_id": sid_of[k], "stop_name": reg[k][0], "stop_lat": None, "stop_lon": None}
             for k in ordered]

    def key_of(c):
        nm = (c.get("name") or "").strip()
        return nm if stop_key == "name" else int(c["num"])

    routes, trips, stop_times = [], [], []
    svc = dec.get("service", {})
    sid = svc.get("service_id", "SVC")
    # 複数ダイヤ対応: block_service でブロックごとに service_id を割り当てられる
    # （平日/土日 で時刻が違う＝別ブロックのときに別カレンダーへ）。無指定は既定 sid。
    block_service = {int(k): v for k, v in dec.get("block_service", {}).items()}
    used_services = set()
    for r in dec["routes"]:
        rid = r["route_id"]
        routes.append({"route_id": rid, "route_short_name": "",
                       "route_long_name": r.get("route_long_name", rid),
                       "route_type": 3, "route_color": None,
                       "route_origin_stop": None, "route_destination_stop": None, "route_via_stop": None})
        bhead = dec.get("block_headsign", {})   # 例: {"0":"左回り","1":"右回り"}（循環の方向名）
        for bi in r["blocks"]:
            did = bdir.get(int(bi), 0)
            for ti, t in enumerate(blocks[int(bi)]["trips"], 1):
                cells = [c for c in t["cells"] if not cell_excluded(c)]
                if len(cells) < 2:
                    continue
                trip_id = f"{rid}_{did}_{bi}_{ti}"
                # 行先: 決定スペックの block_headsign（循環は「左回り/右回り」等）優先、
                # 無ければ最終停留所名。
                head = bhead.get(str(bi)) or bhead.get(int(bi)) or (cells[-1].get("name") or "").strip()
                svc_id = block_service.get(int(bi), sid)
                used_services.add(svc_id)
                trips.append({"trip_id": trip_id, "route_id": rid, "service_id": svc_id,
                              "direction_id": did, "trip_headsign": head, "shape_id": None})
                for seq, c in enumerate(cells, 1):
                    hhmmss = c["time"] if c["time"].count(":") == 2 else c["time"] + ":00"
                    p = hhmmss.split(":"); p[0] = p[0].zfill(2); hhmmss = ":".join(p)
                    stop_times.append({"trip_id": trip_id, "stop_id": sid_of[key_of(c)],
                                       "stop_sequence": seq, "arrival_time": hhmmss, "departure_time": hhmmss})

    # カレンダー: services(複数ダイヤ) があれば各々を、無ければ単一 service を出す。
    # 既定の開始/終了は単一 service の値を流用。実際に便が参照する service だけを残す。
    s0, e0 = svc.get("start_date", "20240101"), svc.get("end_date", "20271231")

    def _cal(d):
        return {"service_id": d.get("service_id", sid),
                "monday": d.get("mon", 0), "tuesday": d.get("tue", 0), "wednesday": d.get("wed", 0),
                "thursday": d.get("thu", 0), "friday": d.get("fri", 0),
                "saturday": d.get("sat", 0), "sunday": d.get("sun", 0),
                "start_date": d.get("start_date", s0), "end_date": d.get("end_date", e0)}

    src_services = dec.get("services") or [svc if svc else {"service_id": sid}]
    calendar = [_cal(s) for s in src_services
                if (not used_services) or s.get("service_id", sid) in used_services]
    # 事業者: decision-spec に agency / agency_jp があれば採用（無ければ従来の未定プレースホルダ）。
    # これにより事業者情報を spec に載せられ、手書き構造化スクリプトが不要になる。
    agency = dec.get("agency") or {"agency_id": "AGENCY_TBD", "agency_name": "未定（自治体が記入）",
                                   "agency_url": None, "agency_phone": None}
    agency_id = agency.get("agency_id") or "AGENCY_TBD"
    agency_jp = dec.get("agency_jp") or {"agency_official_name": None, "agency_zip_number": None,
                                         "agency_address": None, "agency_president_pos": None,
                                         "agency_president_name": None}

    # 運賃: fares(区分別: 大人/小児/障がい者…)優先、無ければ fare_price(単一=大人相当)。
    # fare の agency_id は本体 agency と一致させる（AGENCY_TBD固定をやめ desync を防ぐ）。
    fares = dec.get("fares")
    fa, fr = [], []
    if fares:
        for f in fares:
            cat = str(f.get("category") or "").strip()
            pr = f.get("price")
            if not cat or pr in (None, ""):
                continue
            # fare_id に区分名を使う（GTFSは非ASCII可。運賃表で区分が見えるように）
            fa.append({"fare_id": cat, "price": int(pr), "currency_type": "JPY",
                       "payment_method": 0, "transfers": 0, "agency_id": agency_id})
            fr += [{"fare_id": cat, "route_id": r["route_id"], "origin_id": None,
                    "destination_id": None, "contains_id": None} for r in routes]
    else:
        price = dec.get("fare_price")
        if price:
            fa = [{"fare_id": "F", "price": price, "currency_type": "JPY", "payment_method": 0,
                   "transfers": 0, "agency_id": agency_id}]
            fr = [{"fare_id": "F", "route_id": r["route_id"], "origin_id": None,
                   "destination_id": None, "contains_id": None} for r in routes]

    # feed_info: 有効期間(service期間)と事業者を反映する（利用者入力が calendar 止まりで
    # feed_info に載らない問題の解消）。明示指定があればそれを優先。
    feed_info = dict(dec.get("feed_info") or {})
    feed_info.setdefault("feed_start_date", s0)
    feed_info.setdefault("feed_end_date", e0)
    if agency.get("agency_name"):
        feed_info.setdefault("feed_publisher_name", agency["agency_name"])
    if agency.get("agency_url"):
        feed_info.setdefault("feed_publisher_url", agency["agency_url"])
        feed_info.setdefault("feed_contact_url", agency["agency_url"])

    out = {"agency": agency,
           "agency_jp": agency_jp, "feed_info": feed_info,
           "office_jp": dec.get("office_jp", []), "routes": routes, "stops": stops,
           "trips": trips, "stop_times": stop_times,
           "calendar": calendar, "calendar_dates": dec.get("calendar_dates", []),
           "fare_attributes": fa, "fare_rules": fr,
           "_meta": {"source": "decision-spec applier", "step2_by": dec.get("_llm", "unknown")}}
    json.dump(out, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"applied: routes={len(routes)} trips={len(trips)} stops={len(stops)} stop_times={len(stop_times)}")

if __name__ == "__main__":
    main()
