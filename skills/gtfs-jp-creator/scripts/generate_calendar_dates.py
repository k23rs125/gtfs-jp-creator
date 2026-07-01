#!/usr/bin/env python3
"""
generate_calendar_dates.py
==========================
運休日を calendar_dates.txt に展開する (exception_type=2)。

設計方針 (正しく失敗する):
  - 有効期間(start_date/end_date)は PDF 外情報。利用者が指定する。
  - 祝日は内閣府公開CSV(syukujitsu.csv, Shift_JIS)を一次データとして使う。
    ライブラリによる祝日「計算」はせず、公式データをそのまま展開する。
  - お盆・年末年始は既定では展開しない。PDFに運休記載がある場合のみ、利用者が
    --obon / --nenmatsu で範囲を明示指定したときに展開する(市ごとに異なるため推測しない)。
  - 日曜運休は calendar.txt の sunday=0 で表現されるため、ここでは扱わない
    (二重計上を避ける)。祝日が日曜と重なっても calendar_dates には祝日として
    1行だけ出す(重複排除)。

入力:
  --calendar     : calendar.txt (対象 service_id と有効期間の確認に使用)
  --service-id   : 運休日を付与する service_id (カンマ区切りで複数可)
  --syukujitsu   : 内閣府祝日CSV (任意。無ければ祝日は付加しない=要確認)
  --start / --end: 有効期間 YYYYMMDD (任意。未指定なら calendar.txt から読む)
  --obon         : お盆の運休範囲 "MM-DD:MM-DD" (既定: 無効。PDF記載時のみ指定)
  --nenmatsu     : 年末年始 "MM-DD:MM-DD" (既定: 無効。PDF記載時のみ指定)
出力:
  --output       : calendar_dates.txt (既存があれば追記マージ・重複排除)

Usage:
  python generate_calendar_dates.py --calendar gtfs/calendar.txt \\
      --service-id "月・水・土,火・木・金" --syukujitsu syukujitsu.csv \\
      -o gtfs/calendar_dates.txt
"""
import argparse, csv, io, sys
from datetime import date, timedelta
from pathlib import Path


def read_calendar(path):
    rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
    return rows


def parse_ymd(s):
    s = str(s).strip()
    return date(int(s[:4]), int(s[4:6]), int(s[6:8]))


def load_syukujitsu(path):
    """内閣府CSV(Shift_JIS, 'YYYY/M/D,名称')を {date: 名称} で返す。"""
    holidays = {}
    if not path:
        return holidays
    # 内閣府CSVはShift_JIS。BOMやエンコーディング差に強く読む。
    raw = Path(path).read_bytes()
    for enc in ("cp932", "utf-8-sig", "utf-8"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise SystemExit("祝日CSVの文字コードを判定できません(cp932想定)")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    for r in rows:
        if len(r) < 1 or not r[0]:
            continue
        cell = r[0].strip()
        # ヘッダ行(日付でない)はスキップ
        parts = cell.replace("-", "/").split("/")
        if len(parts) != 3 or not parts[0].isdigit():
            continue
        try:
            d = date(int(parts[0]), int(parts[1]), int(parts[2]))
        except ValueError:
            continue
        name = r[1].strip() if len(r) > 1 else "祝日"
        holidays[d] = name
    return holidays


def daterange(start, end):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def span_dates(start, end, mmdd_range):
    """期間内で 'MM-DD:MM-DD' に該当する日付集合(年をまたぐ年末年始にも対応)。"""
    if not mmdd_range:
        return set()
    a, b = mmdd_range.split(":")
    am, ad = map(int, a.split("-"))
    bm, bd = map(int, b.split("-"))
    out = set()
    for d in daterange(start, end):
        # 同年内範囲 (例 08-13..08-15)
        if (am, ad) <= (bm, bd):
            if (am, ad) <= (d.month, d.day) <= (bm, bd):
                out.add(d)
        else:
            # 年またぎ (例 12-29..01-03)
            if (d.month, d.day) >= (am, ad) or (d.month, d.day) <= (bm, bd):
                out.add(d)
    return out


def main():
    ap = argparse.ArgumentParser(description="運休日を calendar_dates.txt に展開(exception_type=2)")
    ap.add_argument("--calendar", required=True)
    ap.add_argument("--service-id", required=True, help="カンマ区切りで複数可")
    ap.add_argument("--syukujitsu", default=None, help="内閣府祝日CSV(任意)")
    ap.add_argument("--start", default=None, help="YYYYMMDD(未指定ならcalendarから)")
    ap.add_argument("--end", default=None)
    ap.add_argument("--obon", default="", help="お盆運休 \"MM-DD:MM-DD\"。PDFに記載があれば指定(例 08-13:08-15)。未指定なら展開しない")
    ap.add_argument("--nenmatsu", default="", help="年末年始運休 \"MM-DD:MM-DD\"。PDFに記載があれば指定(例 12-29:01-03)。未指定なら展開しない")
    ap.add_argument("-o", "--output", required=True)
    args = ap.parse_args()

    cal = read_calendar(args.calendar)
    sids = [s.strip() for s in args.service_id.split(",") if s.strip()]

    # 有効期間: 引数優先、無ければ calendar.txt から
    if args.start and args.end:
        start, end = parse_ymd(args.start), parse_ymd(args.end)
    else:
        starts = [parse_ymd(r["start_date"]) for r in cal if r.get("start_date")]
        ends = [parse_ymd(r["end_date"]) for r in cal if r.get("end_date")]
        if not starts or not ends:
            raise SystemExit("有効期間が calendar.txt にありません。--start/--end を指定してください。")
        start, end = min(starts), max(ends)

    holidays = load_syukujitsu(args.syukujitsu)
    holi_in = {d for d in holidays if start <= d <= end}
    obon = span_dates(start, end, args.obon)
    nenmatsu = span_dates(start, end, args.nenmatsu)
    closed = sorted(holi_in | obon | nenmatsu)

    # 既存 calendar_dates があれば読み込み、重複排除でマージ
    existing = []
    seen = set()
    outp = Path(args.output)
    if outp.exists():
        for r in csv.DictReader(open(outp, encoding="utf-8-sig")):
            key = (r["service_id"], r["date"], r["exception_type"])
            if key not in seen:
                seen.add(key); existing.append(r)

    rows = list(existing)
    added = 0
    for sid in sids:
        for d in closed:
            ymd = f"{d.year}{d.month:02d}{d.day:02d}"
            key = (sid, ymd, "2")
            if key in seen:
                continue
            seen.add(key)
            rows.append({"service_id": sid, "date": ymd, "exception_type": "2"})
            added += 1

    rows.sort(key=lambda r: (r["service_id"], r["date"]))
    # 既存行が comment 列を持つ場合があるので保持（無ければ空）。extrasaction=ignore で安全側。
    with open(outp, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["service_id", "date", "exception_type", "comment"],
                           extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)

    print(f"[OK] {outp}", file=sys.stderr)
    print(f"  有効期間: {start} 〜 {end}", file=sys.stderr)
    print(f"  祝日(CSV内・期間内): {len(holi_in)}日 / お盆: {len(obon)}日 / 年末年始: {len(nenmatsu)}日", file=sys.stderr)
    print(f"  運休日(重複排除後): {len(closed)}日 × service {len(sids)}種 = 追加{added}行", file=sys.stderr)
    if not args.syukujitsu and not args.obon and not args.nenmatsu:
        print("  [警告] 運休条件が何も指定されていません。calendar_dates は変更されません。", file=sys.stderr)
        print("         運休日は市・事業者ごとに異なります。PDFの記載を確認し、必要な条件のみ", file=sys.stderr)
        print("         (--syukujitsu / --obon / --nenmatsu)を明示指定してください(推測しません)。", file=sys.stderr)
    elif not args.syukujitsu:
        print("  [注意] 祝日CSV(--syukujitsu)未指定のため祝日は展開していません。", file=sys.stderr)

if __name__ == "__main__":
    main()
