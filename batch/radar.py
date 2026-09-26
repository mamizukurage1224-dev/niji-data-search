# -*- coding: utf-8 -*-
"""
他枠出演レーダーのデータを作るバッチ。

  python batch/radar.py

入力
  ライバーマスター：環境変数 MASTER_CSV_URL（スプレッドシートを「ウェブに公開」した CSV の URL）。
                    未設定なら ./livers_master.csv
  前回の状態：OUT_DIR/state.json（無ければ初回として過去 BACKFILL_DAYS 日分を取る）
             画面に出す期間（PAST_SHOW_DAYS 日）のうち取っていない古い分は、毎回 OLDER_MAX_PAGES ずつさかのぼって取る
出力（OUT_DIR、既定は ./out）
  radar/index.json          … 最終更新時刻と、ライバーごとの件数・次の出演
  radar/{channel_id}.json   … ライバー別の他枠出演と自枠（それぞれ これから／直近 RECENT_DAYS 日の過去）
  radar/archive/{channel_id}.json … それより前（PAST_SHOW_DAYS 日まで）の過去
  radar/today.json          … 対象のライバー全員の、配信中と今日・明日（日本時間）の配信予定（画面の「にじさんじ全体」）
  state.json                … 次回の差分取得に使う状態
  ./channel_check.md        … マスターに無いチャンネル（新人など）や、活動を終えたらしいチャンネルを見つけたときだけ書く。
                              公開せず、ワークフローが Issue にして知らせる（確かめるのは1日1回）
  （カレンダー（ICS）の出力は 2026-09-25 にやめた。以前の ics/ が残っていれば消す）

取得に失敗したら何も書き換えずに終了コード1で終わる（前回のデータを出し続ける）。
データは Holodex API から取得している（Powered by Holodex。ライセンスと免責は batch/holodex.py を参照）。
"""
import csv
import io
import json
import os
import re
import shutil
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
OLDER_MAX_PAGES = 20      # 取っていない古い分を、1回にさかのぼるページ上限（50件×20。Holodex への負荷を抑えて少しずつ）
COLLAB_PER_RUN = 8        # 他事務所の枠を補うライバー数（1回あたり、順番に回す）
HEAVY_EVERY_MINUTES = 25  # 他事務所の枠の補いと古い分のさかのぼりは、前回からこれだけたったときだけ行う
                          # （実行は15分おき。これからの配信と新しい過去分は毎回、重い取得は30分に1回にして Holodex への負荷を抑える）
KEEP_DAYS = 190           # 状態に残す日数（画面に出すのは PAST_SHOW_DAYS まで。余分に持つと state.json が膨らむだけなので少しの余裕だけ）
PAST_SHOW_DAYS = 180      # JSONに載せる過去の日数
MISSING_CHECK_HOURS = 24  # 非公開・削除になった動画（Holodex の status=missing）を確かめる間隔
MISSING_MAX_PAGES = 10    # 1回に確かめる上限（50件×10。新しいほうから）
RECENT_DAYS = 35          # ライバー別JSONに入れる直近の過去の日数。それより前は archive/ に分ける（画面が3か月・6か月のときだけ読む）
STALE_UPCOMING_HOURS = 12 # 予定時刻を過ぎても配信にならない予定を捨てるまでの時間
ALWAYS_ON_HOURS = 24      # 開始からこの時間を過ぎても配信中のものは常時配信とみなして捨てる
CHANNEL_CHECK_HOURS = 24  # Holodex のチャンネル一覧とマスターを比べる間隔
CHANNEL_REPORT = os.environ.get("CHANNEL_REPORT", "channel_check.md")

EXCLUDED_TOPICS = {"membersonly"}   # メン限は一覧の対象外（二次創作ガイドライン 第1条4項）
# Holodex がメン限と分類しなかった回も、題名で除く（二重の網。公開の枠の題名にこの言葉があっても、厳しい側に倒して除く）
MEMBERS_TITLE = re.compile(r"メン限|メンバー(?:シップ)?(?:様)?限定|members?[\s'’-]*only", re.IGNORECASE)


def excluded(v):
    """一覧に載せない回：メン限（Holodex の分類か題名）と、YouTube から消えた回。"""
    return v["topic_id"] in EXCLUDED_TOPICS or v["status"] == "missing" or bool(MEMBERS_TITLE.search(v.get("title") or ""))

# 画面に出すブランチと、画面での分け方（jp：にじさんじ、en：NIJISANJI EN）。
# 旧KR・旧ID出身の現役ライバーはいまは にじさんじ所属なので jp に入れる。VirtuaReal は YouTube の配信がほぼ無いので出さない
BRANCH_GROUPS = {"本家": "jp", "旧KR": "jp", "旧ID": "jp", "EN": "en"}

# 配信・動画・ショートの見分け（Holodex には区別が無いので推定。仮の規則）
SHORT_SECONDS = 120          # これ以下の長さの投稿はショートとみなす（1〜2分の告知や切り抜きも入る）
SHORT_TAGGED_SECONDS = 180   # 題名にハッシュタグ（#）があれば、ここまでをショートとみなす（YouTube のショートは3分まで）
LEGACY_LIVE_SECONDS = 600    # 開始時刻を取っていない古いデータは、これより長ければ配信とみなす
STATE_SCHEMA = 2             # 2：過去分も live_info（開始時刻）付きで取る。古い状態なら過去 BACKFILL_DAYS 日を取り直す


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
        if r.get("branch") in BRANCH_GROUPS and r.get("channel_type") == "liver" and r.get("inactive") != "TRUE"
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
    """画面の検索と並び順に使う読み・グループ（jp／en）と、画面の色（ライバーカラー。#RRGGBB でなければ空）。"""
    def color(text):
        text = (text or "").strip().upper()
        return text if re.fullmatch(r"#[0-9A-F]{6}", text) else ""
    return {r["channel_id"]: {"kana": r.get("kana") or "", "alias": r.get("kana_alias") or "",
                              "color": color(r.get("color")), "group": BRANCH_GROUPS.get(r.get("branch"), "")}
            for r in master}


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
        "has_live_info": True,   # 開始時刻（live_info）付きで取った。無い古いデータは kind_of で長さから推定する
    }


def fetch(state, targets):
    """Holodex から取る。1つでも失敗したら HolodexError をそのまま上げる。"""
    now = now_utc()
    # live_info：過去の動画にも開始時刻を付けてもらい、配信と投稿動画を見分ける（/live には最初から付く）
    common = {"type": "stream", "include": "mentions,live_info"}

    print("1/3 これからの配信を取得中…", flush=True)
    live = holodex.get_all("/live", {**common, "org": ORG, "max_upcoming_hours": UPCOMING_HOURS}, max_pages=10)

    # 状態が古い形式なら、過去 BACKFILL_DAYS 日を開始時刻付きで取り直す（上限で止まったら次の回に続きから）
    last = parse_time(state.get("last_past_fetch")) if state.get("schema", 1) >= STATE_SCHEMA else None
    since = (last - timedelta(hours=PAST_OVERLAP_HOURS)) if last else (now - timedelta(days=BACKFILL_DAYS))
    print(f"2/3 {iso(since)[:16]} 以降の過去の配信を取得中（最大{PAST_MAX_PAGES * holodex.PAGE}本）…", flush=True)
    past = holodex.get_all("/videos", {**common, "org": ORG, "status": "past", "from": iso(since),
                                       "sort": "available_at", "order": "asc"}, max_pages=PAST_MAX_PAGES)

    # 画面に出す期間（PAST_SHOW_DAYS）のうち、まだ取っていない古い分を、新しいほうから少しずつさかのぼって取る
    past_from = parse_time(state.get("past_from")) if last else since
    if past_from is None:   # past_from を持つ前の状態：いちばん古い過去分から続ける
        past_from = min((parse_time(v["available_at"]) for v in (state.get("videos") or {}).values()
                         if v["src"] == "org" and v["status"] == "past" and v.get("available_at")), default=since)
    goal = now - timedelta(days=PAST_SHOW_DAYS)
    heavy_at = parse_time(state.get("heavy_at"))
    heavy = not heavy_at or now - heavy_at >= timedelta(minutes=HEAVY_EVERY_MINUTES)
    older = []
    if heavy and past_from > goal:
        print(f"   {iso(goal)[:10]}〜{iso(past_from)[:10]} の古い分をさかのぼって取得中（最大{OLDER_MAX_PAGES * holodex.PAGE}本）…",
              flush=True)
        older = holodex.get_all("/videos", {**common, "org": ORG, "status": "past", "from": iso(goal),
                                            "to": iso(past_from + timedelta(hours=1)),
                                            "sort": "available_at", "order": "desc"}, max_pages=OLDER_MAX_PAGES)
        # 上限で止まったら、取れたいちばん古いところから次の回に続ける
        past_from = (parse_time(older[-1].get("available_at")) or goal
                     if len(older) >= OLDER_MAX_PAGES * holodex.PAGE else goal)

    # 非公開・削除になった動画は、Holodex が status=missing にする。画面に出す期間のぶんを1日1回まとめて取り、一覧から外す
    # （1本ずつ確かめるより呼び出しがずっと少ない）
    missing_at = parse_time(state.get("missing_checked_at"))
    check_missing = heavy and (not missing_at or now - missing_at >= timedelta(hours=MISSING_CHECK_HOURS))
    missing = []
    if check_missing:
        print("   非公開・削除になった動画を確かめています…", flush=True)
        missing = holodex.get_all("/videos", {"type": "stream", "org": ORG, "status": "missing", "from": iso(goal),
                                              "sort": "available_at", "order": "desc"}, max_pages=MISSING_MAX_PAGES)

    # 他事務所の枠への出演は、ライバー別の collabs を少しずつ順番に取って補う
    ids = sorted(targets)
    cursor = state.get("collab_cursor", 0) % max(len(ids), 1)
    turn = [ids[(cursor + i) % len(ids)] for i in range(min(COLLAB_PER_RUN, len(ids)))] if heavy else []
    print(f"3/3 他事務所の枠への出演を {len(turn)} 人分取得中…" if heavy
          else "3/3 他事務所の枠の補いと古い分のさかのぼりは、今回は休み（前回から間がないため）", flush=True)
    collabs = []
    for cid in turn:
        collabs.extend(holodex.as_list(holodex.get(f"/channels/{cid}/collabs",
                                                   {"include": "mentions,live_info", "limit": 25})))

    # 上限で打ち切ったときは、取れたところまでを記録して次の回に続きを取る
    truncated = len(past) >= PAST_MAX_PAGES * holodex.PAGE
    past_until = (past[-1].get("available_at") or iso(now)) if truncated else iso(now)

    return {
        "live": [slim(v, "org") for v in live],
        "past": [slim(v, "org") for v in past],
        "older": [slim(v, "org") for v in older],
        "collabs": [slim(v, "collab") for v in collabs],
        "fetched_at": iso(now),
        "past_until": past_until,
        "past_from": iso(past_from),
        "collab_cursor": (cursor + len(turn)) % max(len(ids), 1),
        "heavy_at": iso(now) if heavy else state.get("heavy_at"),
        "missing": [v["id"] for v in missing if v.get("id")],
        "missing_checked_at": iso(now) if check_missing else state.get("missing_checked_at"),
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

    for v in got["collabs"] + got.get("older", []) + got["past"] + got["live"]:
        if v["type"] != "stream":
            continue
        if v["src"] == "collab" and v["id"] in videos and videos[v["id"]]["src"] == "org":
            v["src"] = "org"
        videos[v["id"]] = v

    # 非公開・削除になった動画は missing にして、一覧に載せない（KEEP_DAYS を過ぎれば状態からも消える）
    for vid in got.get("missing", []):
        if vid in videos:
            videos[vid]["status"] = "missing"

    for vid, v in list(videos.items()):
        start = parse_time(v["start_scheduled"] or v["available_at"])
        started = parse_time(v["start_actual"]) or start
        # 他事務所の枠の予定は /live で確かめられないので、時刻を過ぎて残っていたら捨てる。
        # collabs から来る、ずっと先の待機所（1年以上先の枠など）も載せない
        if v["status"] == "upcoming" and start and (
                start < now - timedelta(hours=STALE_UPCOMING_HOURS)
                or start > now + timedelta(hours=UPCOMING_HOURS)):
            del videos[vid]
        # 常時配信（ユニットの「〇〇 Station」など）はずっと /live に居て消えないので、
        # 開始から時間がたっても配信中のものは捨てる（毎回 /live から入り直し、ここでまた消える）
        elif v["status"] == "live" and started and started < now - timedelta(hours=ALWAYS_ON_HOURS):
            del videos[vid]
        elif start and start < now - timedelta(days=KEEP_DAYS):
            del videos[vid]

    return {
        "videos": videos,
        "last_past_fetch": got["past_until"],
        "past_from": got.get("past_from"),   # にじさんじ全体の過去分を、ここから後はすべて取ってある
        "updated_at": got["fetched_at"],
        "collab_cursor": got["collab_cursor"],
        "heavy_at": got.get("heavy_at"),   # 他事務所の枠の補いと古い分のさかのぼりを最後に行った時刻
        "missing_checked_at": got.get("missing_checked_at"),   # 非公開・削除になった動画を最後に確かめた時刻
        "schema": STATE_SCHEMA,
    }


# ---------- 出力 ----------

def kind_of(v):
    """配信（live）・動画（video）・ショート（short）を見分ける。
    開始時刻があるものは配信（プレミア公開や、#shorts 付きの縦型の生配信も開始時刻を持つので、ここでは配信に入る）。
    投稿動画は、Holodex の分類（topic_id）が shorts ならショート、ほかの分類があればショートにしない（1〜2分の告知 PV など）。
    分類が無いときだけ、長さと題名で推定する（2026-09-26：約2万本で、長さだけの推定は 24本のショートを動画に、
    128本の告知 PV などをショートにしていた）"""
    duration = v.get("duration") or 0
    topic = v.get("topic_id")
    if v["status"] in ("upcoming", "live") or v.get("start_actual"):
        return "live"
    if topic == "shorts":
        return "short"
    if not topic and (0 < duration <= SHORT_SECONDS or (
            duration <= SHORT_TAGGED_SECONDS and re.search(r"[#＃]\S", v.get("title") or ""))):
        return "short"
    if not v.get("has_live_info") and duration > LEGACY_LIVE_SECONDS:
        return "live"
    return "video"


def entry(v, owner, names):
    """画面に載せる1件。owner_id は枠の主（サブチャンネルならメインのチャンネル）。"""
    return {
        "id": v["id"],
        "title": v["title"],
        "host_id": v["channel_id"],
        "owner_id": owner.get(v["channel_id"], v["channel_id"]),
        "host_name": names.get(v["channel_id"]) or v["channel_name"],
        "status": v["status"],
        "kind": kind_of(v),
        "start": v["start_actual"] or v["start_scheduled"] or v["available_at"],
        "duration": v["duration"],
        "url": f"https://www.youtube.com/watch?v={v['id']}",
    }


def appearances(videos, targets, owner, names):
    """ライバーごとの他枠出演。自分（とサブチャンネル）の枠は除く。"""
    result = {cid: [] for cid in targets}
    for v in videos.values():
        if excluded(v):
            continue
        host = owner.get(v["channel_id"], v["channel_id"])
        for cid in v["mentions"]:
            if cid in result and cid != host:
                result[cid].append(entry(v, owner, names))
    return result


def own_streams(videos, targets, owner, names):
    """ライバーごとの自枠（本人とサブチャンネルの枠）。"""
    result = {cid: [] for cid in targets}
    for v in videos.values():
        if excluded(v):
            continue
        host = owner.get(v["channel_id"], v["channel_id"])
        if host in result:
            result[host].append(entry(v, owner, names))
    return result


def split(items, show_from):
    """これから（配信中を含む、古い順）と、show_from 以降の過去（新しい順）に分ける。"""
    items = sorted(items, key=lambda a: a["start"] or "")
    upcoming = [a for a in items if a["status"] in ("upcoming", "live")]
    past = [a for a in items if a["status"] == "past" and a["start"] and parse_time(a["start"]) >= show_from]
    past.reverse()
    return upcoming, past


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


JST = timezone(timedelta(hours=9))


def today_items(videos, targets, owner, names, generated):
    """対象のライバー全員の、配信中と今日・明日（日本時間）の配信予定。cast はその枠に出る対象のライバー（枠の主は除く）。"""
    until = datetime.combine(generated.astimezone(JST).date() + timedelta(days=2), datetime.min.time(), JST)
    items = []
    for v in videos.values():
        if excluded(v) or v["status"] not in ("upcoming", "live"):
            continue
        host = owner.get(v["channel_id"], v["channel_id"])
        start = parse_time(v["start_scheduled"] or v["available_at"])
        if host not in targets or (v["status"] == "upcoming" and start and start >= until):
            continue
        items.append({**entry(v, owner, names),
                      "cast": [m for m in dict.fromkeys(v["mentions"]) if m in targets and m != host]})
    return sorted(items, key=lambda a: a["start"] or "")


def write_outputs(state, targets, owner, names, meta=None):
    meta = meta or {}
    generated = parse_time(state["updated_at"])
    by_liver = appearances(state["videos"], targets, owner, names)
    own_by_liver = own_streams(state["videos"], targets, owner, names)
    show_from = generated - timedelta(days=PAST_SHOW_DAYS)
    recent_from = generated - timedelta(days=RECENT_DAYS)
    recent = lambda xs: [a for a in xs if parse_time(a["start"]) >= recent_from]
    older = lambda xs: [a for a in xs if parse_time(a["start"]) < recent_from]
    # 対象から外れたライバーのファイルを残さないよう、毎回作り直す（前回分は gh-pages から戻してある）
    shutil.rmtree(os.path.join(OUT_DIR, "radar"), ignore_errors=True)
    index = []
    for cid, items in by_liver.items():
        upcoming, past = split(items, show_from)
        own_upcoming, own_past = split(own_by_liver[cid], show_from)
        # upcoming・past は他枠出演、own_upcoming・own_past は自枠。
        # 過去は直近だけをここに入れ、それより前は archive/ に分ける（普段の読み込みを軽くする）
        write_json(os.path.join(OUT_DIR, "radar", f"{cid}.json"), {
            "channel_id": cid, "name": targets[cid], "updated_at": state["updated_at"],
            "recent_from": iso(recent_from),
            "upcoming": upcoming, "past": recent(past), "own_upcoming": own_upcoming, "own_past": recent(own_past),
        })
        write_json(os.path.join(OUT_DIR, "radar", "archive", f"{cid}.json"), {
            "channel_id": cid, "updated_at": state["updated_at"], "from": iso(show_from), "to": iso(recent_from),
            "past": older(past), "own_past": older(own_past),
        })
        index.append({"channel_id": cid, "name": targets[cid],
                      **meta.get(cid, {"kana": "", "alias": "", "color": "", "group": ""}),
                      "upcoming": len(upcoming), "past": len(past),
                      "own_upcoming": len(own_upcoming), "own_past": len(own_past),
                      "next": upcoming[0]["start"] if upcoming else None})
    write_json(os.path.join(OUT_DIR, "radar", "today.json"), {
        "updated_at": state["updated_at"],
        "items": today_items(state["videos"], targets, owner, names, generated),
    })
    write_json(os.path.join(OUT_DIR, "radar", "index.json"), {
        "updated_at": state["updated_at"],
        "source": "Holodex API (https://holodex.net/)",
        "livers": index,
    })
    # カレンダー（ICS）の出力はやめた。前回までの出力（gh-pages から戻したもの）が残っていれば消す
    shutil.rmtree(os.path.join(OUT_DIR, "ics"), ignore_errors=True)


# ---------- マスターの見直しの知らせ ----------

def check_channels(state, master, now):
    """1日1回、Holodex のにじさんじのチャンネル一覧とマスターを比べる。確かめる時期でなければ None。"""
    last = parse_time(state.get("channels_checked_at"))
    if last and now - last < timedelta(hours=CHANNEL_CHECK_HOURS):
        return None
    channels = holodex.get_all("/channels", {"org": ORG, "type": "vtuber"}, max_pages=20)
    known = {r["channel_id"]: r for r in master}
    return {
        "checked_at": iso(now),
        # マスターに無い：新人のデビュー、新しいユニットや公式のチャンネルなど
        "new": [c for c in channels if c.get("id") and c["id"] not in known],
        # Holodex では活動終了なのに、マスターでは現役：卒業・契約解除など
        "ended": [c for c in channels if c.get("inactive") and c.get("id") in known
                  and known[c["id"]].get("inactive") != "TRUE"],
    }


def write_channel_report(check):
    def table(rows):
        cell = lambda t: (t or "").replace("|", "／")
        return "\n".join(["| 名前 | 英語名 | グループ | チャンネル |", "|---|---|---|---|"] + [
            f"| {cell(c.get('name'))} | {cell(c.get('english_name'))} | {cell(c.get('group'))} | "
            f"[{c['id']}](https://www.youtube.com/channel/{c['id']}) |" for c in rows])
    parts = [f"Holodex のにじさんじのチャンネル一覧と、ライバー一覧（livers_master.csv）を比べた結果です（{check['checked_at'][:10]}）。"
             "ライバー一覧を直すと、サイトに反映されます。"]
    if check["new"]:
        parts += ["## ライバー一覧に無いチャンネル（新人・新しいユニットなど）", table(check["new"])]
    if check["ended"]:
        parts += ["## 活動を終えたらしいチャンネル（Holodex では活動終了、ライバー一覧では現役）", table(check["ended"])]
    repo = "https://github.com/mamizukurage1224-dev/niji-data-search"
    parts += ["## すること", "\n".join([
        f"- **新人**：[ライバー一覧を直す]({repo}/actions/workflows/master.yml) を開き、「Run workflow」→「新人を追加する」を選んで、"
        "チャンネルID（上の表からコピー）・グループ・表示名・読みを入れて実行します。"
        "ライバーカラーは分かれば入れます（あとからでも入れられます）。15分以内にサイトに出ます。",
        "- **卒業・活動終了**：同じ画面で「卒業・活動終了にする」を選び、チャンネルIDを入れて実行します。",
        "- **ライバー本人ではないチャンネル**（ユニット・公式・切り抜きなど）：何もしなくてかまいません。",
    ]), f"終わったら、この Issue を閉じてください。詳しくは [運用ガイド]({repo}/blob/main/OPERATIONS.md) を見てください。"]
    with open(CHANNEL_REPORT, "w", encoding="utf-8") as f:
        f.write("\n\n".join(parts) + "\n")


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
    # マスターの見直しが要るチャンネルを1日1回確かめる。失敗してもデータの更新は続け、次の回にまた試す
    new_state["channels_checked_at"] = state.get("channels_checked_at")
    new_state["channels_reported"] = state.get("channels_reported", [])
    try:
        check = check_channels(state, master, parse_time(got["fetched_at"]))
    except holodex.HolodexError as e:
        print(f"チャンネル一覧を取れませんでした（次の回にまた確かめます）：{e}", file=sys.stderr)
        check = None
    if check:
        ids = sorted(c["id"] for c in check["new"] + check["ended"])
        if ids and ids != new_state["channels_reported"]:   # 前回知らせたのと同じ顔ぶれなら、くり返し知らせない
            write_channel_report(check)
            print(f"マスターの見直しが要るチャンネルがあります：{len(check['new'])} 件が未登録、{len(check['ended'])} 件が活動終了")
        new_state["channels_checked_at"] = check["checked_at"]
        new_state["channels_reported"] = ids
    write_outputs(new_state, targets, owner, names, build_meta(master))
    write_json(os.path.join(OUT_DIR, "state.json"), new_state)
    total = sum(1 for v in new_state["videos"].values())
    print(f"対象 {len(targets)} 人 / 取得 これから{len(got['live'])}・過去{len(got['past'])}・"
          f"さかのぼり{len(got['older'])}・collabs{len(got['collabs'])} / 保持 {total} 本 / "
          f"過去分は {new_state['past_from'][:10]} から / 出力先 {OUT_DIR}")
    if new_state["last_past_fetch"] != new_state["updated_at"]:
        print("過去の配信は上限で止めました。もう一度実行すると続きから取ります。")


if __name__ == "__main__":
    main()
