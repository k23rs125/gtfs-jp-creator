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
import argparse, json, os, re, sys

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--extract", required=True)
    ap.add_argument("--decisions", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--timetable-review", default=None,
                    help="修正済み時刻CSVのフォルダ。指定時は構造化の前に extract の時刻を反映")
    a = ap.parse_args()

    ex = json.load(open(a.extract, encoding="utf-8"))
    dec = json.load(open(a.decisions, encoding="utf-8"))
    # 時刻修正CSV(export_timetable_review)があれば、stop_times を作る前に反映する。
    if a.timetable_review:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "skills", "gtfs-jp-creator", "scripts"))
        from apply_timetable_review import apply_reviews
        ch, warn = apply_reviews(ex, a.timetable_review)
        print(f"[時刻修正] 反映 {len(ch)} セル" + (f" / 警告 {len(warn)}件" if warn else ""))
        for w in warn:
            print("  [警告]", w)
    blocks = ex["blocks"]

    exclude_reserve = dec.get("exclude_reserve", True)
    # 座標方式は無番号セル＝案内文ノイズなので除外。Excel等は全停留所が無番号なので除外しない。
    exclude_unnumbered = dec.get("exclude_unnumbered", True)
    stop_key = dec.get("stop_key", "name")
    bdir = {int(k): v for k, v in dec.get("block_direction", {}).items()}
    # 行き/帰りで停留所を分けるか（既定ON）。多くの停留所は反対車線にあり方向ごとに座標が
    # 異なるため、方向(direction_id)込みでキー化して別停留所にする。循環・片方向のみの
    # 停留所は方向が1つなので結果的に1つのまま。stop_desc に方面（行先ベース）を入れる。
    split_dir = dec.get("split_by_direction", True)
    bhead = dec.get("block_headsign", {})

    def cell_excluded(c):
        if exclude_reserve and c.get("reserve"):
            return True
        if exclude_unnumbered and c.get("num") is None:   # 無番号ノイズ（案内文混入）
            return True
        return False

    def base_key(c):
        nm = (c.get("name") or "").strip()
        return nm if stop_key == "name" else (int(c["num"]) if c.get("num") is not None else None)

    def key_of(c, did):
        bk = base_key(c)
        return (bk, did) if split_dir else bk

    # 方面（行先ベース）を direction ごとに決める → stop_desc に入れる。
    def _houmen(h):
        h = (h or "").strip()
        if not h:
            return ""
        h = re.sub(r"(行き|行|方面|方向)$", "", h).strip()
        return (h + "方面") if h else ""

    def _dest_of_cells(_cs):
        """便(セル列)の行先。循環(起点に戻る)なら起点手前を行先とする。"""
        _names = [(c.get("name") or "").strip() for c in _cs]
        if len(_names) < 1:
            return ""
        if len(_names) >= 3 and _names[-1] == _names[0]:   # 循環は起点手前を行先に
            return _names[-2]
        return _names[-1]

    def _dest_of(_b):
        """そのブロックの行先(最初の有効便の最終停留所)。stop_desc の既定に使う。"""
        for _t in _b["trips"]:
            _cs = [c for c in _t["cells"] if not cell_excluded(c)]
            if len(_cs) >= 2:
                return _dest_of_cells(_cs)
        return ""

    def _block_houmen(bi):
        """ブロック bi の「○○方面」テキスト。stop_desc・行先の既定に使う。
        方向IDだけのグローバルにすると、別路線（先に処理した路線）の行先を流用して
        しまう（例: 循環路線に「佐屋方面」）。必ずそのブロック自身の終点から作る。"""
        _h = bhead.get(str(bi)) or bhead.get(int(bi))
        if not _h or re.search(r"(回り|循環)", str(_h)):   # 右回り/左回り→行先ベースの方面
            _h = _dest_of(blocks[int(bi)])
        _did = bdir.get(int(bi), 0)
        return _houmen(_h) or ("行き方面" if _did == 0 else "帰り方面")

    # 停留所レジストリ（split時は (base, did) をキー＝方向ごとに別停留所）
    reg = {}   # key -> (name, num_for_sort, did)
    reg_block = {}   # key -> 代表ブロック（方面=stop_desc をそのブロックの終点から出す）
    for _bi, b in enumerate(blocks):
        did = bdir.get(_bi, 0)
        for t in b["trips"]:
            for c in t["cells"]:
                if cell_excluded(c):
                    continue
                nm = (c.get("name") or "").strip()
                num = c.get("num")
                num = int(num) if num is not None else None
                key = key_of(c, did)
                if key not in reg:
                    reg[key] = (nm, num if num is not None else 99999, did)
                    reg_block[key] = _bi
    # S採番（代表num昇順→方向→名称）
    ordered = sorted(reg, key=lambda k: (reg[k][1], reg[k][2], reg[k][0]))
    sid_of = {k: f"S{i:03d}" for i, k in enumerate(ordered, 1)}
    stops = [{"stop_id": sid_of[k], "stop_name": reg[k][0], "stop_lat": None, "stop_lon": None,
              "stop_desc": (_block_houmen(reg_block[k]) if split_dir else "")}
             for k in ordered]

    routes, trips, stop_times = [], [], []
    svc = dec.get("service", {})
    sid = svc.get("service_id", "SVC")
    # 複数ダイヤ対応: block_service でブロックごとに service_id を割り当てられる
    # （平日/土日 で時刻が違う＝別ブロックのときに別カレンダーへ）。無指定は既定 sid。
    block_service = {int(k): v for k, v in dec.get("block_service", {}).items()}
    # 便ごとのサービス割当（block_service より優先）。季節ダイヤ（相らんど第2の夏季/冬季を
    # 別サービスにする等）で、同じブロック内の便を別カレンダーへ振り分けるのに使う。
    # trip_service: [{"block":bi,"trip":ti,"service":"平日（夏季）"}, ...]
    trip_svc = {}
    for _ts in dec.get("trip_service", []) or []:
        trip_svc[(int(_ts["block"]), int(_ts["trip"]))] = _ts["service"]
    used_services = set()

    # 乗降制約（降車専用/乗車専用）。範囲は推測せず spec で明示する（route_id/direction_id/
    # block で限定可）。降車専用=乗車不可(pickup_type=1)、乗車専用=降車不可(drop_off_type=1)。
    boarding = dec.get("boarding") or []   # [{type, route_id?, direction_id?, block?, stops:[name]}]

    def _board_codes(rid_, did_, bi_, name_):
        pu = do = 0
        for rule in boarding:
            if rule.get("route_id") and rule["route_id"] != rid_:
                continue
            if rule.get("direction_id") is not None and int(rule["direction_id"]) != int(did_):
                continue
            if rule.get("block") is not None and int(rule["block"]) != int(bi_):
                continue
            if name_ in (rule.get("stops") or []):
                tp = rule.get("type")
                if tp == "drop_off_only":
                    pu = 1
                elif tp == "pickup_only":
                    do = 1
        return pu, do

    trip_by_biti = {}   # (block_index, trip_index) -> trip dict。block_id 割当に使う
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
                # 無ければこのブロック自身の終点から「○○方面」。
                # dir_head（方向IDだけのグローバル）を先に使うと、先に処理した別路線の行先を
                # 流用してしまう（例: 循環路線 R02 に山らいず R01 の「佐屋方面」が付く）。
                head = bhead.get(str(bi)) or bhead.get(int(bi))
                if not head or re.search(r"(回り|循環)", str(head)):   # 右回り/左回り→○○方面
                    # この便自身の終点から方面を作る（区間便も正確に）。無ければブロック既定。
                    head = (_houmen(_dest_of_cells(cells)) or _block_houmen(bi)
                            or (cells[-1].get("name") or "").strip())
                svc_id = trip_svc.get((int(bi), ti)) or block_service.get(int(bi), sid)
                used_services.add(svc_id)
                trips.append({"trip_id": trip_id, "route_id": rid, "service_id": svc_id,
                              "direction_id": did, "trip_headsign": head, "shape_id": None})
                trip_by_biti[(int(bi), ti)] = trips[-1]
                _prev_min, _dayoff = None, 0   # 便内の日跨ぎ検出（夜→翌朝は24時超で出力）
                for seq, c in enumerate(cells, 1):
                    hhmmss = c["time"] if c["time"].count(":") == 2 else c["time"] + ":00"
                    _pp = hhmmss.split(":")
                    _h, _m = int(_pp[0]), int(_pp[1]); _s = _pp[2].zfill(2) if len(_pp) > 2 else "00"
                    _cur = _h * 60 + _m
                    # 日跨ぎ＝翌日(+24h)。翌日扱いにすると前の時刻から自然に続く(3時間以内で増える)
                    # ときだけ +24h する。待機時間(0:11 等の注記)は翌日にしても差が大きく、誤変換しない。
                    if _prev_min is not None and _cur + _dayoff * 1440 < _prev_min \
                            and 0 <= (_cur + (_dayoff + 1) * 1440) - _prev_min <= 180:
                        _dayoff += 1
                    _prev_min = _cur + _dayoff * 1440
                    hhmmss = f"{_h + _dayoff * 24:02d}:{_m:02d}:{_s}"
                    strec = {"trip_id": trip_id, "stop_id": sid_of[key_of(c, did)],
                             "stop_sequence": seq, "arrival_time": hhmmss, "departure_time": hhmmss}
                    pu, do = _board_codes(rid, did, bi, (c.get("name") or "").strip())
                    if pu:   # 降車専用（乗車不可）。0は付けず generate の要予約=2既定を壊さない
                        strec["pickup_type"] = pu
                    if do:   # 乗車専用（降車不可）
                        strec["drop_off_type"] = do
                    stop_times.append(strec)

    # 車両運用（ブロック）: through-running など、同一車両が続けて走る便に共通の block_id を振る。
    # block_links: [[{"block":bi,"trip":ti}, ...], ...]  各グループが1つの車両ブロック（例: 山らいず
    # 15便→相らんど第1 10便）。指定された便にだけ付与し、他は空（＝公式と同じ運用の表し方）。
    for gi, group in enumerate(dec.get("block_links", []) or [], 1):
        block_id = f"B{gi:03d}"
        for ref in group:
            key = (int(ref["block"]), int(ref["trip"]))
            if key in trip_by_biti:
                trip_by_biti[key]["block_id"] = block_id

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

    # 運賃: fare_matrix(区間制) > route_fares(路線別) > fares(全路線区分別) > fare_price(単一)。
    # fare の agency_id は本体 agency と一致させる（AGENCY_TBD固定をやめ desync を防ぐ）。
    fares = dec.get("fares")
    fare_matrix = dec.get("fare_matrix")   # [{from, to, price}] 停留所名で指定（区間運賃）
    route_fares = dec.get("route_fares")   # {route_id: [{category, price}]} 路線別運賃
    fa, fr = [], []
    if fare_matrix:
        # 区間運賃: 各停留所をゾーン(=stop_id)にし、出発→到着ごとに運賃を fare_rules で持つ。
        name_to_sid = {reg[k][0]: sid_of[k] for k in ordered}
        seen_price, zoned = {}, set()
        for m in fare_matrix:
            fz = name_to_sid.get(str(m.get("from") or "").strip())
            tz = name_to_sid.get(str(m.get("to") or "").strip())
            pr = m.get("price")
            if not fz or not tz or pr in (None, ""):
                continue
            pr = int(pr)
            fid = seen_price.get(pr)
            if fid is None:
                fid = f"Z{pr}"
                seen_price[pr] = fid
                fa.append({"fare_id": fid, "price": pr, "currency_type": "JPY",
                           "payment_method": 0, "transfers": 0, "agency_id": agency_id})
            fr.append({"fare_id": fid, "route_id": None, "origin_id": fz,
                       "destination_id": tz, "contains_id": None})
            zoned.add(fz); zoned.add(tz)
        for s in stops:   # zone_id = stop_id（区間運賃で使う停留所のみ）
            if s["stop_id"] in zoned:
                s["zone_id"] = s["stop_id"]
    elif route_fares:
        # 路線別運賃: 路線ごとに運賃を持つ（古賀バスのように路線で値段が違う場合）。
        # fare_id は 区分+金額 で一意化（同額同区分は共有）。fare_rules は route_id 付き。
        seen = set()
        for rid, lst in route_fares.items():
            for f in (lst or []):
                cat = str(f.get("category") or "").strip()
                pr = f.get("price")
                if not cat or pr in (None, ""):
                    continue
                pr = int(pr)
                fid = f"{cat}_{pr}"
                if fid not in seen:
                    seen.add(fid)
                    fa.append({"fare_id": fid, "price": pr, "currency_type": "JPY",
                               "payment_method": 0, "transfers": 0, "agency_id": agency_id})
                fr.append({"fare_id": fid, "route_id": rid, "origin_id": None,
                           "destination_id": None, "contains_id": None})
    elif fares:
        seen_fid = set()
        for f in fares:
            cat = str(f.get("category") or "").strip()
            pr = f.get("price")
            if not cat or pr in (None, "") or cat in seen_fid:
                continue
            seen_fid.add(cat)
            # payment_method: 0=車内で支払う(後払い) / 1=乗車前に支払う(前払い)。指定なければ 0。
            pm = 1 if int(f.get("payment_method") or 0) == 1 else 0
            # fare_id に区分名を使う（GTFSは非ASCII可。運賃表で区分が見えるように）
            fa.append({"fare_id": cat, "price": int(pr), "currency_type": "JPY",
                       "payment_method": pm, "transfers": 0, "agency_id": agency_id})
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
