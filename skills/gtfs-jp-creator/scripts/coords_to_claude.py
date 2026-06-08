#!/usr/bin/env python3
"""
coords_to_claude.py
===================
extract_timetable_coords.py の座標抽出JSONを、Markdownを経由せず
中間JSON(claude.json スキーマ)に「決定的に」変換する。

要予約バス停の扱い(先生方針 2026-06):
- 要予約(reserve=True)のデマンド型バス停は、公式オープンデータの慣行に倣い
  既定で GTFS から除外する(stops/stop_times の双方から落とす)。
  デマンド停留所を本線 trip に含めると時刻が逆行し Validator が ERROR を出すため。
- --keep-reserve を付けると従来どおり含める(pickup_type/drop_off_type=2 を付与)。
"""
import argparse, json, re, sys
from pathlib import Path

def main():
    ap=argparse.ArgumentParser(description="座標抽出JSON→中間JSON(claude.json)へ決定的変換")
    ap.add_argument("input")
    ap.add_argument("-o","--output",required=True)
    ap.add_argument("--route-id",default="R01")
    ap.add_argument("--route-name",default=None,help="route_long_name(未指定なら要確認)")
    ap.add_argument("--service-id",default="SERVICE_1")
    ap.add_argument("--meta-json",default=None,help="agency等PDF外情報を補うJSON")
    ap.add_argument("--keep-reserve",action="store_true",
                    help="要予約バス停を含める(既定は除外)。含める場合 pickup/drop_off=2 を付与")
    args=ap.parse_args()

    ext=json.load(open(args.input,encoding="utf-8"))
    blocks=ext.get("blocks",[])
    needs=list(ext.get("needs_confirmation",[]))
    keep_reserve=args.keep_reserve

    # --- 要予約番号を収集(除外判定用) ---
    reserve_nums=set()
    for b in blocks:
        for s in b["stops"]:
            if s.get("reserve") and s["num"] is not None:
                reserve_nums.add(s["num"])
    excluded_reserve=set()  # 実際に除外した停留所名

    # --- 停留所マスタ(番号→名前)。全ブロックで統合。番号なしは除外(uncertain) ---
    master={}
    name_set_noname=[]
    for b in blocks:
        for s in b["stops"]:
            if s["num"] is None:
                name_set_noname.append(s["name"]); continue
            if (not keep_reserve) and s.get("reserve"):
                excluded_reserve.add(s["name"]); continue   # 要予約は除外
            master.setdefault(s["num"], s["name"])
    nums=sorted(master)
    num2sid={n:f"S{i+1:03d}" for i,n in enumerate(nums)}
    stops=[{"stop_id":num2sid[n],"stop_name":master[n],"stop_lat":None,"stop_lon":None} for n in nums]

    # --- trips / stop_times を決定的に生成 ---
    trips=[]; stop_times=[]
    for direction,b in enumerate(blocks):
        for k,t in enumerate(b["trips"],1):
            tid=f"{args.route_id}_{direction}_{k:02d}"
            cells=t["cells"]
            # headsign: 末尾セル(要予約除外時は除外後の末尾)
            usable=[c for c in cells if c["num"] is not None and (keep_reserve or not c.get("reserve"))]
            head=usable[-1]["name"] if usable else ""
            trips.append({"trip_id":tid,"route_id":args.route_id,"service_id":args.service_id,
                          "direction_id":direction,"trip_headsign":head,"shape_id":None})
            seq=0
            for c in cells:
                if c["num"] is None:
                    continue
                if (not keep_reserve) and c.get("reserve"):
                    continue   # 要予約セルは stop_times から除外
                seq+=1
                tt=c["time"]
                hh,mm,ss=(tt.split(":")+["00","00"])[:3]
                tt=f"{int(hh):02d}:{mm}:{ss}"
                pd=2 if c.get("reserve") else 0
                stop_times.append({"trip_id":tid,"stop_id":num2sid[c["num"]],
                                   "stop_sequence":seq,"arrival_time":tt,"departure_time":tt,
                                   "pickup_type":pd,"drop_off_type":pd})

    meta_ext=json.load(open(args.meta_json,encoding="utf-8")) if args.meta_json else {}
    agency=meta_ext.get("agency",{"agency_id":None,"agency_name":None,"agency_url":None,"agency_phone":None})
    reserve_note=(f"要予約バス停 {len(excluded_reserve)}件を除外(先生方針・公式データ慣行)。"
                  if not keep_reserve else
                  "要予約バス停を含める(--keep-reserve)。pickup_type/drop_off_type=2 を付与。")
    out={
        "agency":agency,
        "agency_jp":meta_ext.get("agency_jp",{"agency_official_name":None,"agency_zip_number":None,
            "agency_address":None,"agency_president_pos":None,"agency_president_name":None}),
        "office_jp":meta_ext.get("office_jp",[]),
        "routes":[{"route_id":args.route_id,"route_short_name":"",
            "route_long_name":args.route_name,"route_type":3,"route_color":None,
            "route_origin_stop":(stops[0]["stop_name"] if stops else None),
            "route_destination_stop":None,"route_via_stop":None}],
        "stops":stops,"trips":trips,"stop_times":stop_times,
        "calendar":meta_ext.get("calendar",
            [{"service_id":args.service_id,"monday":0,"tuesday":0,"wednesday":0,
              "thursday":0,"friday":0,"saturday":0,"sunday":0,"start_date":None,"end_date":None}]),
        "calendar_dates":[],
        "fare_attributes":meta_ext.get("fare_attributes",[]),
        "fare_rules":meta_ext.get("fare_rules",[]),
        "_meta":{
            "source":ext.get("source",""),
            "extraction_notes":"extract_timetable_coords.py の座標抽出JSONから coords_to_claude.py で決定的に変換(Markdown非経由・LLM再解釈なし)。番号→stop_id は番号昇順でS連番採番。",
            "warnings":[
                "便名(trip_id)は時刻列の並び順で機械割り当て(route_dir_連番)。PDFの正式便名とは対応しない。",
                "direction_id はブロック検出順に 0,1,... を割り当て。原典で方向の意味を確認のこと。",
                f"route_long_name={args.route_name!r}, service_id={args.service_id!r} は引数指定値。未確定なら要確認。",
                reserve_note,
                "番号が取れない停留所候補は stop_times に含めていない(needs_confirmationのuncertain_stop参照)。",
                "停留所の緯度経度は未補完(Step3.5 P11/Nominatimで後付与)。",
                "agency等PDF外情報は --meta-json 指定が無い項目は空(要入力)。"
            ],
            "needs_confirmation":needs,
            "excluded_reserve_stops":sorted(excluded_reserve),
            "user_overrides":{}
        }
    }
    Path(args.output).write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding="utf-8")

    sids={s["stop_id"] for s in stops}; tids={t["trip_id"] for t in trips}
    bad=[st for st in stop_times if st["stop_id"] not in sids or st["trip_id"] not in tids]
    print(f"[OK] {args.output}",file=sys.stderr)
    print(f"  停留所{len(stops)} 便{len(trips)} stop_times{len(stop_times)} 参照整合エラー{len(bad)}",file=sys.stderr)
    print(f"  要確認 needs_confirmation {len(needs)}件 / 番号なし停留所 {len(set(name_set_noname))}件",file=sys.stderr)
    if not keep_reserve:
        print(f"  要予約バス停を除外: {len(excluded_reserve)}件 {sorted(excluded_reserve)}",file=sys.stderr)
    else:
        print(f"  要予約バス停を含める(--keep-reserve)",file=sys.stderr)

if __name__=="__main__":
    main()
