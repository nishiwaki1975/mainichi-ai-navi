#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
まいにちAIナビ: データ取得スクリプト (GitHub Actions / ローカル共通)

- 天気: 気象庁公式JSON (東京都 130000) を取得し、傘・洗濯・服装アドバイスをルール生成。
  予報JSONは発表時刻により timeSeries の並びや数が変わるため、配列の位置ではなく
  「エリアコードと項目名」で系列を探す方式にしている。
- ニュース: 政府広報オンライン RSS (RSS1.0) を取得し、5件を掲載。
  政府広報オンラインのコンテンツは公共データ利用規約 (PDL1.0) の対象で、
  出典明示により利用できる。NHK・消費者庁・内閣府などの RSS は規約で
  再配信が禁止されているため、公開サイトでは使用しない。
- お出かけ/お得: 掲載中アイテムの出典URLの到達確認のみ行う
  (内容の自動書き換えはしない。差し替えは確認後に portal.json を編集)。
  一部でも確認できない場合は stale=true にし「未確認」と分かる表示にする。
- 取得に失敗したセクションは前回データを保持し、stale=true を立てる。

使い方:
  python3 scripts/fetch_update.py --section all|weather|news|check-links|auto
                                   [--schedule "0 7 * * *"]
  --auto の対象は、--schedule で渡された「予定されていた cron 式」を優先して決める
  (Actions の実行が遅れても、予定どおりのカテゴリが更新される)。
  --schedule が空 (手動実行) の場合は全カテゴリを対象にする。
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
JMA_OVERVIEW_URL = "https://www.jma.go.jp/bosai/forecast/data/overview_forecast/130000.json"
NEWS_RSS = "https://www.gov-online.go.jp/rss/index.rdf"
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "portal.json"
WEEKDAY = ["月", "火", "水", "木", "金", "土", "日"]

# 予定 cron 式 → 更新対象 (ワークフローの schedule と一致させること)
SCHEDULE_MAP = {
    "0 7 * * *": ["weather", "news", "outings"],
    "0 8 * * *": ["deals"],
    "0 12 * * *": ["weather", "news"],
    "0 18 * * *": ["weather", "news"],
}

CODE_TEXT = {
    "100": "晴れ", "101": "晴れ 時々 くもり", "102": "晴れ 一時 雨", "103": "晴れ 時々 雨",
    "110": "晴れ 後 時々 くもり", "111": "晴れ 後 くもり", "112": "晴れ 後 一時 雨",
    "200": "くもり", "201": "くもり 時々 晴れ", "202": "くもり 一時 雨", "203": "くもり 時々 雨",
    "210": "くもり 後 時々 晴れ", "211": "くもり 後 晴れ", "212": "くもり 後 一時 雨",
    "300": "雨", "301": "雨 時々 晴れ", "302": "雨 時々 止む", "311": "雨 後 晴れ",
    "313": "雨 後 くもり", "400": "雪", "401": "雪 時々 晴れ",
}


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
    if temp_max is None:
        clothing = "気温に合わせて調整しやすい服装がおすすめです。"
    elif temp_max >= 28:
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


def find_series(block, area_code, key):
    """timeSeries の位置に依存せず、エリアコードと項目名で系列を探す。"""
    for series in block.get("timeSeries", []):
        for a in series.get("areas", []):
            if a.get("area", {}).get("code") == area_code and key in a:
                return series.get("timeDefines", []), a
    return [], None


def by_date(time_defines, values):
    return {t[:10]: v for t, v in zip(time_defines, values)}


def parse_weather(raw, today_date):
    """気象庁予報JSON (2ブロック構成) を表示用データに変換する。"""
    block, weekly = raw[0], raw[1]
    today_iso = today_date.isoformat()
    tomorrow_iso = (today_date + timedelta(days=1)).isoformat()

    # 今日・明日の天気文: 日付をキーに引く (先頭要素は発表時刻の日になることがあるため位置で取らない)
    w_def, w_area = find_series(block, "130010", "weathers")
    if w_area is None:
        raise RuntimeError("天気の系列が見つかりませんでした")
    weather_by_date = by_date(w_def, w_area["weathers"])

    # 時間帯別降水確率: 時刻をキーに引く (発表時刻により先頭の時刻が変わっても対応)
    p_def, p_area = find_series(block, "130010", "pops")
    pop_by_time = {t: v for t, v in zip(p_def, p_area["pops"])} if p_area else {}
    slots = []
    for hour, label in [(0, "0〜6時"), (6, "6〜12時"), (12, "12〜18時"), (18, "18〜24時")]:
        key = next((t for t in pop_by_time if t.startswith(f"{today_iso}T{hour:02d}:")), None)
        if key is not None and pop_by_time[key] != "":
            slots.append({"label": label, "pop": int(pop_by_time[key])})

    # 今日の気温 (地点予報): timeDefines の日付が「今日」の値だけを使う。
    # 発表時刻によっては翌日分の値が混ざることがあるため、配列全体の min/max は取らない。
    t_def, t_area = find_series(block, "44132", "temps")
    temp_min = temp_max = None
    if t_area is not None:
        vals = [int(v) for t, v in zip(t_def, t_area["temps"])
                if t[:10] == today_iso and v != ""]
        if vals:
            temp_min, temp_max = min(vals), max(vals)

    # 週間予報: 天気コード・降水確率は地方エリア、気温は地点エリアから日付キーで引く
    rw_def, rw_area = find_series(weekly, "130010", "weatherCodes")
    pw_def, pw_area = find_series(weekly, "44132", "tempsMin")
    codes = by_date(rw_def, rw_area["weatherCodes"]) if rw_area else {}
    wpops = by_date(rw_def, rw_area["pops"]) if rw_area and "pops" in rw_area else {}
    tmins = by_date(pw_def, pw_area["tempsMin"]) if pw_area else {}
    tmaxs = by_date(pw_def, pw_area["tempsMax"]) if pw_area else {}

    def day_data(iso):
        return {
            "weather_text": weather_by_date.get(iso) or CODE_TEXT.get(codes.get(iso, ""), ""),
            "temp_min": int(tmins[iso]) if tmins.get(iso) else None,
            "temp_max": int(tmaxs[iso]) if tmaxs.get(iso) else None,
            "pop_max": int(wpops[iso]) if wpops.get(iso) else None,
        }

    pop_max_today = max([s["pop"] for s in slots], default=None)
    if pop_max_today is None:
        pop_max_today = day_data(today_iso)["pop_max"] or 0
    if temp_min is None or temp_max is None:
        d0 = day_data(today_iso)
        temp_min = temp_min if temp_min is not None else d0["temp_min"]
        temp_max = temp_max if temp_max is not None else d0["temp_max"]

    today_text = weather_by_date.get(today_iso, "")
    u, l, c = advice(today_text, pop_max_today, temp_max)
    today = {
        "date_label": date_label(today_iso),
        "weather_text": today_text.replace("　", " "),
        "temp_min": temp_min, "temp_max": temp_max,
        "pops": slots,
        "umbrella": u, "laundry": l, "clothing": c,
    }
    tmr = day_data(tomorrow_iso)
    if weather_by_date.get(tomorrow_iso):
        tmr["weather_text"] = weather_by_date[tomorrow_iso]
    u2, l2, c2 = advice(tmr["weather_text"], tmr["pop_max"] or 0, tmr["temp_max"])
    tomorrow = {
        "date_label": date_label(tomorrow_iso),
        "weather_text": tmr["weather_text"].replace("　", " "),
        "temp_min": tmr["temp_min"], "temp_max": tmr["temp_max"],
        "pop_max": tmr["pop_max"],
        "umbrella": u2, "laundry": l2, "clothing": c2,
    }
    weekend = []
    d = today_date
    while d.weekday() != 5:  # 直近の土曜日
        d += timedelta(days=1)
    for dd in [d, d + timedelta(days=1)]:
        iso = dd.isoformat()
        if iso in codes or iso in tmaxs:
            wd = day_data(iso)
            weekend.append({
                "date_label": date_label(iso),
                "weather_text": wd["weather_text"].replace("　", " "),
                "temp_min": wd["temp_min"], "temp_max": wd["temp_max"],
                "pop_max": wd["pop_max"],
            })
    return block.get("reportDatetime", ""), today, tomorrow, weekend


def fetch_overview(old_overview):
    """気象概況 (天気概況文) を取得する。失敗時は前回の文を保持する。"""
    try:
        ov = json.loads(fetch(JMA_OVERVIEW_URL))
        text = re.sub(r"\s+", " ", ov.get("text", "")).strip()
        if not text:
            return old_overview
        if len(text) > 240:
            cut = text.rfind("。", 0, 240)
            text = text[: cut + 1] if cut > 60 else text[:240]
        return text
    except Exception:
        return old_overview


def update_weather(portal):
    raw = json.loads(fetch(JMA_URL))
    report_dt, today, tomorrow, weekend = parse_weather(raw, now_jst().date())
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
        "report_datetime": report_dt.replace("T", " ")[:16],
        "overview": fetch_overview(portal["weather"].get("overview", "")),
        "today": today,
        "tomorrow": tomorrow,
        "weekend": weekend,
        "weekend_advice": weekend_advice,
    }
    return "天気を更新しました"


def clean_summary(desc, limit=140):
    text = re.sub(r"\s+", " ", desc or "").strip()
    text = text.split("…")[0]
    if len(text) > limit:
        cut = text.rfind("。", 0, limit)
        text = text[: cut + 1] if cut > 40 else text[:limit]
    return text


def update_news(portal):
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
        title = it.findtext("rss:title", namespaces=ns) or ""
        parsed.append({
            "title": title,
            "summary": clean_summary(it.findtext("rss:description", namespaces=ns), limit=110),
            "url": it.findtext("rss:link", namespaces=ns) or "",
            "published_jst": pub_label,
            "_press": "記者会見" in title,
        })
    if not parsed:
        raise RuntimeError("政府広報オンラインRSSに記事がありませんでした")
    parsed.sort(key=lambda x: x["_press"])  # 定例記者会見ばかりにならないよう、それ以外を優先
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
    total = len(portal[section]["items"])
    if total == 0:
        # 掲載期間の終了などで0件なのは正常な状態。取得失敗と区別する
        portal[section]["fetched_at_jst"] = fmt(now_jst())
        portal[section]["stale"] = False
        portal[section]["note"] = "現在掲載中の情報はありません"
        return True, f"{section}は掲載中の情報が0件です（正常）"
    ok = 0
    for item in portal[section]["items"]:
        try:
            req = urllib.request.Request(item["url"], headers=UA, method="GET")
            with urllib.request.urlopen(req, timeout=20) as r:
                if r.status < 400:
                    ok += 1
        except Exception:
            pass
    date_str = now_jst().strftime("%Y年%m月%d日")
    if total > 0 and ok == total:
        portal[section]["fetched_at_jst"] = fmt(now_jst())
        portal[section]["stale"] = False
        portal[section]["note"] = f"出典ページの公開を{date_str}に確認（{ok}/{total}件到達）"
        return True, f"{section}の出典リンクを確認しました（{ok}/{total}件到達）"
    # 一部でも確認できない場合は「確認済み」と表示せず、前回情報を未確認として残す
    portal[section]["stale"] = True
    portal[section]["note"] = (f"{date_str}の確認で、出典ページ {ok}/{total} 件しか到達できませんでした。"
                                "確認できていない情報が含まれます。前回の情報を表示しています。")
    return False, f"{section}の出典リンクを一部確認できませんでした（{ok}/{total}件到達）"


def run(sections):
    portal = json.loads(DATA.read_text(encoding="utf-8"))
    results = []
    for sec in sections:
        try:
            if sec == "weather":
                msg = update_weather(portal)
                results.append((sec, True, msg))
            elif sec == "news":
                msg = update_news(portal)
                results.append((sec, True, msg))
            elif sec in ("outings", "deals"):
                ok, msg = check_links(portal, sec)
                results.append((sec, ok, msg))
        except Exception as e:  # 失敗時は前回データを保持し stale を立てる
            if sec in portal:
                portal[sec]["stale"] = True
            results.append((sec, False, f"取得に失敗したため前回の情報を保持します: {e}"))
    DATA.write_text(json.dumps(portal, ensure_ascii=False, indent=2), encoding="utf-8")
    for sec, ok, msg in results:
        print(f"[{'OK' if ok else 'FAIL'}] {sec}: {msg}")
    return 0


def sections_for_auto(schedule_expr):
    """予定されていた cron 式から更新対象を決める。実行時刻には依存しない。"""
    expr = (schedule_expr or "").strip()
    if expr:
        if expr in SCHEDULE_MAP:
            return SCHEDULE_MAP[expr]
        # 未知の cron 式の場合は、予定に近い処理として時刻ベースにフォールバック
        print(f"警告: 未知のスケジュール式 '{expr}' のため、実行時刻ベースで判定します", file=sys.stderr)
    else:
        # 手動実行 (workflow_dispatch) では schedule が渡らないため全カテゴリを対象にする
        return ["weather", "news", "outings", "deals"]
    h = now_jst().hour
    if h == 8:
        return ["deals"]
    if h == 7:
        return ["weather", "news", "outings"]
    return ["weather", "news"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--section", default="all",
                    choices=["all", "weather", "news", "check-links", "auto"])
    ap.add_argument("--schedule", default="",
                    help="トリガーとなった cron 式 (GitHub Actions の github.event.schedule)")
    args = ap.parse_args()
    if args.section == "all":
        sections = ["weather", "news", "outings", "deals"]
    elif args.section == "check-links":
        sections = ["outings", "deals"]
    elif args.section == "auto":
        sections = sections_for_auto(args.schedule)
        print(f"更新対象 ({args.schedule or '手動実行'}): {', '.join(sections)}")
    else:
        sections = [args.section]
    sys.exit(run(sections))


if __name__ == "__main__":
    main()
