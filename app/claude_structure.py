# -*- coding: utf-8 -*-
"""Step2(構造化)を Claude API で行う。抽出結果(blocks/trips) → decision-spec(JSON)。
LLMは「路線・方向・循環・除外の判断」だけを返す（転記・座標・メタ情報は扱わない）。
decision-spec は apply_decisions.py がそのまま展開できる形。"""
import json

SYSTEM = """あなたは GTFS-JP 生成の「構造化(Step2)」だけを担う。バス時刻表の抽出結果(blocks/trips)を見て、
路線・方向・循環・除外の判断を decision-spec(JSON) で返す。停留所名や時刻の転記、座標は扱わない。
出力は JSON のみ（前後に文章を付けない）。スキーマ:
{
 "routes": [{"route_id":"R01","route_long_name":"<路線名>","blocks":[0,1],"circular":false}],
 "block_direction": {"0":0, "1":1},
 "block_headsign": {"0":"<行先/方向名>", "1":"<行先/方向名>"},
 "exclude_reserve": true,
 "exclude_unnumbered": <Excel由来(停留所番号なし)なら false、PDF座標方式なら true>,
 "stop_key": "name"
}
判断指針:
- 同一路線の往復は 1 つの route に blocks をまとめ、direction を 0/1 で分ける。
- 循環は circular=true。headsign は方向名(例 左回り/右回り)にし、最終停留所名にはしない
  (循環は始終点が同じため)。
- direction_hint(「○○行き」等)があれば、その block の headsign に使う。
- 往復で direction_hint が無ければ、各 block の最終停留所名を headsign の候補にする。
- route_long_name は路線名が分かればそれ、不明なら代表的な行先から簡潔に付ける。"""


def summarize_extract(extract: dict) -> str:
    """プロンプト用に抽出結果を要約(全セルは渡さず、構造が分かる最小限)。"""
    lines = []
    for b in extract.get("blocks", []):
        bi = b.get("block_index")
        dh = b.get("direction_hint")
        trips = b.get("trips", [])
        cells0 = trips[0].get("cells", []) if trips else []
        names = [c.get("name", "") for c in cells0]
        has_num = any(c.get("num") is not None for c in cells0)
        has_reserve = any(c.get("reserve") for t in trips for c in t.get("cells", []))
        lines.append(f"block {bi}: direction_hint={dh!r}, 便数={len(trips)}, 停留所数={len(names)}, "
                     f"番号あり={has_num}, 要予約セルあり={has_reserve}")
        lines.append("  停留所順: " + " → ".join(names))
    return "\n".join(lines)


def structure(extract: dict, api_key: str, model: str = "claude-sonnet-4-6") -> dict:
    """Claude API で decision-spec を得る。"""
    from anthropic import Anthropic
    client = Anthropic(api_key=api_key)
    summary = summarize_extract(extract)
    msg = client.messages.create(
        model=model, max_tokens=1500, system=SYSTEM,
        messages=[{"role": "user",
                   "content": f"抽出結果:\n{summary}\n\nこの時刻表の decision-spec を JSON で返してください。"}],
    )
    text = "".join(getattr(b, "text", "") for b in msg.content if getattr(b, "type", "") == "text")
    a, z = text.find("{"), text.rfind("}")
    if a < 0 or z < 0:
        raise ValueError(f"Claude応答からJSONを抽出できませんでした:\n{text[:400]}")
    return json.loads(text[a:z + 1])
