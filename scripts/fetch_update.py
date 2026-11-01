#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
まいにちAIナビ: データ取得スクリプト (GitHub Actions / ローカル共通)

- 天気: 気象庁公式JSON (東京都 130000) を取得し、傘・洗濯・服装アドバイスをルール生成
- ニュース: 政府広報オンライン RSS (RSS1.0) を取得し、5件を掲載。
  政府広報オンラインのコンテンツは公共データ利用規約 (PDL1.0) の対象で、
  出典明示により利用できる。NHK・消費者庁・内閣府などの RSS は規約で
  再配信が禁止されているため、公開サイトでは使用しない。
  要約はフィードの description を整形したもの (AI生成ではない)
- お出かけ/お得: 掲載中アイテムの出典URLの到達確認のみ行い、確認日時を更新する
  (内容の自動書き換えはしない。内容の差し替えは人間/Museの確認後に portal.json を編集)
- 取得に失敗したセクションは前回データを保持し、stale=true を立てる

使い方:
  python3 scripts/fetch_update.py --section all|weather|news|check-links|auto
  --auto は現在の日本時間に応じて更新対象を決める
    7時: 天気+ニュース+お出かけ確認 / 12時,18時: 天気+ニュース / 8時: お得確認
"""
import argparse
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))
UA = {"User-Agent": "Mozilla/5.0 (mainichi-ai-navi; data fetcher)"}
JMA_URL = "https://www.jma.go.jp/bosai/forecast/data/forecast/130000.json"
NEWS_RSS = "https://www.gov-online.go.jp/rss/index.rdf"
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "portal.json"
WEEKDAY = ["月", "火", "水", "木", "金", "土", "日"]


def now_jst():
    return datetime.now(JST)


def fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M")


def date_label(iso_date):
    d = datetime.strptime(iso_date, "%Y-%m-%d").date()
    return f"{d.month}月{d.day}日（{WEEKDAY[d.weekday()]}）"


def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def advice(weather_text, pop_max, temp_max):
    rainy = pop_max >= 50 or "雨" in weather_text
    umbrella = "傘が必要です" if pop_max >= 50 else ("折りたたみ傘があると安心です" if pop_max >= 30 else "傘はほぼ不要です")
    if rainy:
        laundry = "外干しはおすすめしません。部屋干しが安心です"
    elif pop_max >= 30:
        laundry = "午前中の早めの時間に干すのがおすすめです"
    else:
        laundry = "洗濯物がよく乾きそうです"
    if temp_max >= 28:
        clothing = "半袖で過ごせそうです。日差し対策も忘れずに。"
    elif temp_max >= 22:
        clothing = "長袖シャツ1枚で過ごしやすい気温です。"
    elif temp_max >= 16:
        clothing = "長袖シャツに、朝晩用に薄手の羽織りものがあると安心です。"
    elif temp_max >= 11:
        clothing = "セーターや厚手の上着がおすすめです。"
    else:
        clothing = "コートなど冬物の防寒着で、しっかり暖かくしてください。"
    return umbrella, laundry, clothing


def update_weather(portal):
    raw = json.loads(fetch(JMA_URL))
    block, weekly = raw[0], raw[1]
    today_iso = now_jst().date().isoformat()
    tomorrow_iso = (now_jst().date() + timedelta(days=1)).isoformat()

    def area(series_idx, code):
        for a in block["timeSeries"][series_idx]["areas"]:
            if a["area"]["code"] == code:
                return a
        return None

    weathers = area(0, "130010")
    pops_series = block["timeSeries"][1]
    pops_tokyo = [a for a in pops_series["areas"] if a["area"]["code"] == "130010"][0]["pops"]
    # pops: [前日18時, 0-6, 6-12, 12-18, 18-24]
    today_pops = [
        {"label": "0〜6時", "pop": int(pops_tokyo[1])},
        {"label": "6〜12時", "pop": int(pops_tokyo[2])},
        {"label": "12〜18時", "pop": int(pops_tokyo[3])},
        {"label": "18〜24時", "pop": int(pops_tokyo[4])},
    ]
    temps_today = [a for a in block["timeSeries"][2]["areas"] if a["area"]["code"] == "44132"][0]["temps"]
    # 週間予報: timeSeries[0]=地方(天気コード・降水確率), timeSeries[1]=地点(気温)
    w_regional = [a for a in weekly["timeSeries"][0]["areas"] if a["area"]["code"] == "130010"][0]
    w_point = [a for a in weekly["timeSeries"][1]["areas"] if a["area"]["code"] == "44132"][0]
    w_dates = [t[:10] for t in weekly["timeSeries"][0]["timeDefines"]]
    code_text = {
        "100": "晴れ", "101": "晴れ 時々 くもり", "102": "晴れ 一時 雨", "103": "晴れ 時々 雨",
        "110": "晴れ 後 時々 くもり", "111": "晴れ 後 くもり", "112": "晴れ 後 一時 雨",
        "200": "くもり", "201": "くもり 時々 晴れ", "202": "くもり 一時 雨", "203": "くもり 時々 雨",
        "210": "くもり 後 時々 晴れ", "211": "くもり 後 晴れ", "212": "くもり 後 一時 雨",
        "300": "雨", "301": "雨 時々 晴れ", "302": "雨 時々 止む", "311": "雨 後 晴れ",
        "313": "雨 後 くもり", "400": "雪", "401": "雪 時々 晴れ",
    }

    def weekly_day(iso):
        i = w_dates.index(iso)
        code = w_regional["weatherCodes"][i]
        pops = w_regional["pops"][i]
        return {
            "date_label": date_label(iso),
            "weather_text": code_text.get(code, ""),
            "temp_min": int(w_point["tempsMin"][i]) if w_point["tempsMin"][i] else None,
            "temp_max": int(w_point["tempsMax"][i]) if w_point["tempsMax"][i] else None,
            "pop_max": int(pops) if pops else None,
        }

    dates = [t[:10] for t in block["timeSeries"][0]["timeDefines"]]
    ti = dates.index(today_iso)
    today_text = weathers["weathers"][ti]
    pop_max_today = max(p["pop"] for p in today_pops)
    u, l, c = advice(today_text, pop_max_today, int(temps_today[1]))
    today = {
        "date_label": date_label(today_iso),
        "weather_text": today_text.replace("　", " "),
        "temp_min": int(temps_today[0]),
        "temp_max": int(temps_today[1]),
        "pops": today_pops,
        "umbrella": u, "laundry": l, "clothing": c,
    }
    tmr = weekly_day(tomorrow_iso)
    tmr_text = ""
    if tomorrow_iso in dates:
        tmr_text = weathers["weathers"][dates.index(tomorrow_iso)]
    u2, l2, c2 = advice(tmr_text or tmr["weather_text"], tmr["pop_max"] or 0, tmr["temp_max"] or 20)
    tomorrow = {
        "date_label": date_label(tomorrow_iso),
        "weather_text": (tmr_text or tmr["weather_text"]).replace("　", " "),
        "temp_min": tmr["temp_min"], "temp_max": tmr["temp_max"],
        "pop_max": tmr["pop_max"],
        "umbrella": u2, "laundry": l2, "clothing": c2,
    }
    # 直近の土日
    weekend = []
    d = now_jst().date()
    while d.weekday() != 5:  # 土曜
        d += timedelta(days=1)
    for dd in [d, d + timedelta(days=1)]:
        if dd.isoformat() in w_dates:
            day = weekly_day(dd.isoformat())
            weekend.append({k: day[k] for k in ("date_label", "weather_text", "temp_min", "temp_max", "pop_max")})
    if len(weekend) == 2 and weekend[0]["temp_max"] is not None and weekend[1]["temp_min"] is not None:
        weekend_advice = (f"土曜は最高{weekend[0]['temp_max']}℃の予報です。"
                          f"日曜の朝は{weekend[1]['temp_min']}℃まで冷え込むので、暖かい服装で。")
    else:
        weekend_advice = portal["weather"].get("weekend_advice", "")
    portal["weather"] = {
        "fetched_at_jst": fmt(now_jst()),
        "stale": False,
        "source_name": "気象庁",
        "source_url": JMA_URL,
        "report_datetime": block["reportDatetime"].replace("T", " ")[:16],
        "overview": portal["weather"].get("overview", ""),
        "today": today,
        "tomorrow": tomorrow,
        "weekend": weekend,
        "weekend_advice": weekend_advice,
    }
    return "天気を更新しました"


def clean_summary(desc, limit=140):
    text = re.sub(r"\s+", " ", desc or "").strip()
    text = text.split("…")[0]
    # 文の途中で切らないよう、limit以内で最後の「。」までを使う
    if len(text) > limit:
        cut = text.rfind("。", 0, limit)
        text = text[: cut + 1] if cut > 40 else text[:limit]
    return text


def update_news(portal):
    # 政府広報オンライン RSS1.0 (RDF): item 要素に dc:date を持つ
    root = ET.fromstring(fetch(NEWS_RSS))
    ns = {"rss": "http://purl.org/rss/1.0/", "dc": "http://purl.org/dc/elements/1.1/"}
    parsed = []
    for it in root.findall("rss:item", ns):
        date_raw = it.findtext("dc:date", namespaces=ns) or ""
        try:
            pub_dt = datetime.fromisoformat(date_raw).astimezone(JST)
            pub_label = fmt(pub_dt)
        except Exception:
            pub_label = date_raw
        parsed.append({
            "title": it.findtext("rss:title", namespaces=ns) or "",
            "summary": clean_summary(it.findtext("rss:description", namespaces=ns), limit=110),
            "url": it.findtext("rss:link", namespaces=ns) or "",
            "published_jst": pub_label,
            "_press": "記者会見" in (it.findtext("rss:title", namespaces=ns) or ""),
        })
    if not parsed:
        raise RuntimeError("政府広報オンラインRSSに記事がありませんでした")
    # 定例記者会見の動画速報ばかりにならないよう、それ以外を優先して5件選ぶ
    parsed.sort(key=lambda x: x["_press"])  # stable sort: 非記者会見が先に来る
    items = [{k: v for k, v in p.items() if k != "_press"} for p in parsed[:5]]
    portal["news"] = {
        "fetched_at_jst": fmt(now_jst()),
        "stale": False,
        "source_name": "政府広報オンライン",
        "source_url": NEWS_RSS,
        "items": items,
    }
    return "ニュースを更新しました"


def check_links(portal, section):
    # 終了日 (end_date) を過ぎたアイテムは取り除く
    today = now_jst().date().isoformat()
    portal[section]["items"] = [
        i for i in portal[section]["items"]
        if not i.get("end_date") or i["end_date"] >= today
    ]
    ok = 0
    for item in portal[section]["items"]:
        try:
            req = urllib.request.Request(item["url"], headers=UA, method="GET")
            with urllib.request.urlopen(req, timeout=20) as r:
                if r.status < 400:
                    ok += 1
        except Exception:
            pass
    portal[section]["fetched_at_jst"] = fmt(now_jst())
    portal[section]["stale"] = False
    portal[section]["note"] = f"出典ページの公開を{now_jst().strftime('%Y年%m月%d日')}に確認（{ok}/{len(portal[section]['items'])}件到達）"
    return f"{section}の出典リンクを確認しました（{ok}件到達）"


def run(sections):
    portal = json.loads(DATA.read_text(encoding="utf-8"))
    results = []
    for sec in sections:
        try:
            if sec == "weather":
                msg = update_weather(portal)
            elif sec == "news":
                msg = update_news(portal)
            elif sec in ("outings", "deals"):
                msg = check_links(portal, sec)
            results.append((sec, True, msg))
        except Exception as e:  # 失敗時は前回データを保持し stale を立てる
            key = {"weather": "weather", "news": "news"}.get(sec, sec)
            if key in portal:
                portal[key]["stale"] = True
            results.append((sec, False, f"取得に失敗したため前回の情報を保持します: {e}"))
    DATA.write_text(json.dumps(portal, ensure_ascii=False, indent=2), encoding="utf-8")
    for sec, ok, msg in results:
        print(f"[{'OK' if ok else 'FAIL'}] {sec}: {msg}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--section", default="all",
                    choices=["all", "weather", "news", "check-links", "auto"])
    args = ap.parse_args()
    if args.section == "all":
        sections = ["weather", "news", "outings", "deals"]
    elif args.section == "check-links":
        sections = ["outings", "deals"]
    elif args.section == "auto":
        h = now_jst().hour
        if h == 8:
            sections = ["deals"]
        elif h == 7:
            sections = ["weather", "news", "outings"]
        else:
            sections = ["weather", "news"]
    else:
        sections = [args.section]
    sys.exit(run(sections))


if __name__ == "__main__":
    main()
