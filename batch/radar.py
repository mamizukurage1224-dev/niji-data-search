# -*- coding: utf-8 -*-
"""
他枠出演レーダーのデータを作るバッチ。

  python batch/radar.py

入力
  ライバーマスター：環境変数 MASTER_CSV_URL（スプレッドシートを「ウェブに公開」した CSV の URL）。
                    未設定なら ./livers_master.csv
  前回の状態：OUT_DIR/state.json（無ければ初回として過去 BACKFILL_DAYS 日分を取る）
出力（OUT_DIR、既定は ./out）
  radar/index.json          … 最終更新時刻と、ライバーごとの件数・次の出演
  radar/{channel_id}.json   … ライバー別の出演一覧（これから／過去）
  ics/{channel_id}.ics      … ライバー別のカレンダー（これから＋直近の過去）
  state.json                … 次回の差分取得に使う状態

取得に失敗したら何も書き換えずに終了コード1で終わる（前回のデータを出し続ける）。
データは Holodex API から取得している（Powered by Holodex。ライセンスと免責は batch/holodex.py を参照）。
"""
import csv
import io
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

import holodex

ORG = "Nijisanji"
OUT_DIR = os.environ.get("OUT_DIR", "out")
MASTER_CSV_URL = os.environ.get("MASTER_CSV_URL", "")
MASTER_LOCAL = "livers_master.csv"

UPCOMING_HOURS = 168      # これからの配信は1週間先まで
BACKFILL_DAYS = 30        # 初回に取る過去の日数
PAST_OVERLAP_HOURS = 6    # 差分取得の重なり（取りこぼし防止）
PAST_MAX_PAGES = 40       # 1回の過去分取得のページ上限（50件×40）
COLLAB_PER_RUN = 8        # 他事務所の枠を補うライバー数（1回あたり、順番に回す）
KEEP_DAYS = 365           # 状態に残す日数
PAST_SHOW_DAYS = 180      # JSONに載せる過去の日数
ICS_PAST_DAYS = 14        # ICSに載せる過去の日数
STALE_UPCOMING_HOURS = 12 # 予定時刻を過ぎても配信にならない予定を捨てるまでの時間
DEFAULT_MINUTES = 60      # 長さが分からない配信のICS上の長さ

EXCLUDED_TOPICS = {"membersonly"}   # メン限は一覧の対象外（二次創作ガイドライン 第1条4項）


def now_utc():
    return datetime.now(timezone.utc)


def parse_time(text):
    if not text:
        return None
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- マスター ----------

def load_master():
    if MASTER_CSV_URL:
        req = urllib.request.Request(MASTER_CSV_URL, headers={"User-Agent": holodex.USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as res:
            text = res.read().decode("utf-8-sig")
    else:
        with open(MASTER_LOCAL, encoding="utf-8-sig") as f:
            text = f.read()
    return list(csv.DictReader(io.StringIO(text)))


def build_index(master):
    """対象ライバーと、チャンネル → 本人（メイン）の対応を作る。"""
    targets = {
        r["channel_id"]: (r.get("display_name") or r.get("name_holodex") or r["channel_id"])
        for r in master
        if r.get("branch") == "本家" and r.get("channel_type") == "liver" and r.get("inactive") != "TRUE"
    }
    main_by_name = {r["display_name"]: r["channel_id"]
                    for r in master if r.get("channel_type") == "liver" and r.get("display_name")}
    owner = {}   # サブチャンネル → メインのチャンネル
    names = {}   # チャンネル → 表示名
    for r in master:
        cid = r["channel_id"]
        names[cid] = r.get("display_name") or r.get("name_holodex") or cid
        owner[cid] = main_by_name.get(r.get("display_name"), cid) if r.get("channel_type") == "sub" else cid
    return targets, owner, names


def build_meta(master):
    """画面の検索と五十音順に使う読み。"""
    return {r["channel_id"]: {"kana": r.get("kana") or "", "alias": r.get("kana_alias") or ""} for r in master}


# ---------- 取得 ----------

def slim(v, src):
    """保存用に必要な項目だけ残す。"""
    ch = v.get("channel") or {}
    return {
        "id": v["id"],
        "title": v.get("title") or "",
        "type": v.get("type"),
        "topic_id": v.get("topic_id"),
        "status": v.get("status"),
        "channel_id": ch.get("id") or v.get("channel_id"),
        "channel_name": ch.get("name") or "",
        "start_scheduled": v.get("start_scheduled"),
        "start_actual": v.get("start_actual"),
        "available_at": v.get("available_at"),
        "duration": v.get("duration") or 0,
        "mentions": [m["id"] for m in (v.get("mentions") or []) if m.get("id")],
        "src": src,
    }


def fetch(state, targets):
    """Holodex から取る。1つでも失敗したら HolodexError をそのまま上げる。"""
    now = now_utc()
    common = {"type": "stream", "include": "mentions"}

    print("1/3 これからの配信を取得中…", flush=True)
    live = holodex.get_all("/live", {**common, "org": ORG, "max_upcoming_hours": UPCOMING_HOURS}, max_pages=10)

    last = parse_time(state.get("last_past_fetch"))
    since = (last - timedelta(hours=PAST_OVERLAP_HOURS)) if last else (now - timedelta(days=BACKFILL_DAYS))
    print(f"2/3 {iso(since)[:16]} 以降の過去の配信を取得中（最大{PAST_MAX_PAGES * holodex.PAGE}本）…", flush=True)
    past = holodex.get_all("/videos", {**common, "org": ORG, "status": "past", "from": iso(since),
                                       "sort": "available_at", "order": "asc"}, max_pages=PAST_MAX_PAGES)

    # 他事務所の枠への出演は、ライバー別の collabs を少しずつ順番に取って補う
    ids = sorted(targets)
    cursor = state.get("collab_cursor", 0) % max(len(ids), 1)
    turn = [ids[(cursor + i) % len(ids)] for i in range(min(COLLAB_PER_RUN, len(ids)))]
    print(f"3/3 他事務所の枠への出演を {len(turn)} 人分取得中…", flush=True)
    collabs = []
    for cid in turn:
        collabs.extend(holodex.as_list(holodex.get(f"/channels/{cid}/collabs",
                                                   {"include": "mentions", "limit": 25})))

    # 上限で打ち切ったときは、取れたところまでを記録して次の回に続きを取る
    truncated = len(past) >= PAST_MAX_PAGES * holodex.PAGE
    past_until = (past[-1].get("available_at") or iso(now)) if truncated else iso(now)

    return {
        "live": [slim(v, "org") for v in live],
        "past": [slim(v, "org") for v in past],
        "collabs": [slim(v, "collab") for v in collabs],
        "fetched_at": iso(now),
        "past_until": past_until,
        "collab_cursor": (cursor + len(turn)) % max(len(ids), 1),
    }


# ---------- 状態の更新 ----------

def merge(state, got):
    videos = dict(state.get("videos") or {})
    now = parse_time(got["fetched_at"])
    live_ids = {v["id"] for v in got["live"]}

    # 以前「これから／配信中」だった、にじさんじの枠のうち、今回 /live に無いものは
    # 中止や枠の立て直しなので消す（配信済みなら直後の過去分取得で入り直す）
    for vid, v in list(videos.items()):
        if v["status"] in ("upcoming", "live") and v["src"] == "org" and vid not in live_ids:
            del videos[vid]

    for v in got["collabs"] + got["past"] + got["live"]:
        if v["type"] != "stream":
            continue
        if v["src"] == "collab" and v["id"] in videos and videos[v["id"]]["src"] == "org":
            v["src"] = "org"
        videos[v["id"]] = v

    for vid, v in list(videos.items()):
        start = parse_time(v["start_scheduled"] or v["available_at"])
        # 他事務所の枠の予定は /live で確かめられないので、時刻を過ぎて残っていたら捨てる。
        # collabs から来る、ずっと先の待機所（1年以上先の枠など）も載せない
        if v["status"] == "upcoming" and start and (
                start < now - timedelta(hours=STALE_UPCOMING_HOURS)
                or start > now + timedelta(hours=UPCOMING_HOURS)):
            del videos[vid]
        elif start and start < now - timedelta(days=KEEP_DAYS):
            del videos[vid]

    return {
        "videos": videos,
        "last_past_fetch": got["past_until"],
        "updated_at": got["fetched_at"],
        "collab_cursor": got["collab_cursor"],
    }


# ---------- 出力 ----------

def appearances(videos, targets, owner, names):
    """ライバーごとの他枠出演。自分（とサブチャンネル）の枠は除く。"""
    result = {cid: [] for cid in targets}
    for v in videos.values():
        if v["topic_id"] in EXCLUDED_TOPICS or v["status"] == "missing":
            continue
        host = owner.get(v["channel_id"], v["channel_id"])
        for cid in v["mentions"]:
            if cid in result and cid != host:
                result[cid].append({
                    "id": v["id"],
                    "title": v["title"],
                    "host_id": v["channel_id"],
                    "host_name": names.get(v["channel_id"]) or v["channel_name"],
                    "status": v["status"],
                    "start": v["start_actual"] or v["start_scheduled"] or v["available_at"],
                    "duration": v["duration"],
                    "url": f"https://www.youtube.com/watch?v={v['id']}",
                })
    return result


def ics_escape(text):
    return (text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n"))


def ics_fold(line):
    """75オクテットごとに折り返す（RFC 5545）。マルチバイト文字の途中では切らない。"""
    out, cur, size = [], "", 0
    for ch in line:
        n = len(ch.encode("utf-8"))
        limit = 75 if not out else 74   # 2行目以降は先頭の空白1文字分を引く
        if size + n > limit:
            out.append(cur)
            cur, size = "", 0
        cur += ch
        size += n
    out.append(cur)
    return "\r\n ".join(out)


def to_ics(name, items, generated):
    stamp = generated.strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//niji-oshikatsu-tools//radar//JA",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{ics_escape(name)} 他枠出演（非公式）",
        "X-WR-TIMEZONE:Asia/Tokyo",
    ]
    for a in items:
        start = parse_time(a["start"])
        if not start:
            continue
        minutes = round(a["duration"] / 60) if a["duration"] else DEFAULT_MINUTES
        end = start + timedelta(minutes=max(minutes, 1))
        lines += [
            "BEGIN:VEVENT",
            f"UID:{a['id']}@niji-oshikatsu-tools",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{start.strftime('%Y%m%dT%H%M%SZ')}",
            f"DTEND:{end.strftime('%Y%m%dT%H%M%SZ')}",
            f"SUMMARY:{ics_escape('[他枠] ' + a['host_name'] + '：' + a['title'])}",
            f"URL:{a['url']}",
            "DESCRIPTION:" + ics_escape(
                f"{a['url']}\n予定は変わることがあります。最新はサイトで確認してください。\n"
                "非公式ファンツール / Powered by Holodex"),
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(ics_fold(line) for line in lines) + "\r\n"


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.replace(tmp, path)


def write_outputs(state, targets, owner, names, meta=None):
    meta = meta or {}
    generated = parse_time(state["updated_at"])
    by_liver = appearances(state["videos"], targets, owner, names)
    show_from = generated - timedelta(days=PAST_SHOW_DAYS)
    ics_from = generated - timedelta(days=ICS_PAST_DAYS)
    index = []
    for cid, items in by_liver.items():
        items.sort(key=lambda a: a["start"] or "")
        upcoming = [a for a in items if a["status"] in ("upcoming", "live")]
        past = [a for a in items if a["status"] == "past" and a["start"] and parse_time(a["start"]) >= show_from]
        past.reverse()
        write_json(os.path.join(OUT_DIR, "radar", f"{cid}.json"), {
            "channel_id": cid, "name": targets[cid], "updated_at": state["updated_at"],
            "upcoming": upcoming, "past": past,
        })
        in_ics = upcoming + [a for a in past if parse_time(a["start"]) >= ics_from]
        write_text(os.path.join(OUT_DIR, "ics", f"{cid}.ics"), to_ics(targets[cid], in_ics, generated))
        index.append({"channel_id": cid, "name": targets[cid], **meta.get(cid, {"kana": "", "alias": ""}),
                      "upcoming": len(upcoming),
                      "past": len(past), "next": upcoming[0]["start"] if upcoming else None})
    write_json(os.path.join(OUT_DIR, "radar", "index.json"), {
        "updated_at": state["updated_at"],
        "source": "Holodex API (https://holodex.net/)",
        "livers": index,
    })


# ---------- 実行 ----------

def load_state():
    path = os.path.join(OUT_DIR, "state.json")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main():
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    master = load_master()
    targets, owner, names = build_index(master)
    if not targets:
        sys.exit("対象のライバーがいません（マスターの branch・channel_type・inactive を確認してください）")
    state = load_state()
    try:
        got = fetch(state, targets)
    except holodex.HolodexError as e:
        print(f"Holodex からの取得に失敗しました。前回のデータをそのまま残します：{e}", file=sys.stderr)
        sys.exit(1)
    new_state = merge(state, got)
    write_outputs(new_state, targets, owner, names, build_meta(master))
    write_json(os.path.join(OUT_DIR, "state.json"), new_state)
    total = sum(1 for v in new_state["videos"].values())
    print(f"対象 {len(targets)} 人 / 取得 これから{len(got['live'])}・過去{len(got['past'])}・"
          f"collabs{len(got['collabs'])} / 保持 {total} 本 / 出力先 {OUT_DIR}")
    if new_state["last_past_fetch"] != new_state["updated_at"]:
        print("過去の配信は上限で止めました。もう一度実行すると続きから取ります。")


if __name__ == "__main__":
    main()
