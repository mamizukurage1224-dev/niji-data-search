# -*- coding: utf-8 -*-
"""
ライバー一覧（livers_master.csv）を直す小さな道具。GitHub の Actions「ライバー一覧を直す」の画面から使う
（運用に詳しくなくても、欄を埋めて実行するだけで直せるように）。手元でも動く。

  python batch/master_tool.py add    --channel-id UC… --kana よみ [--name 表示名] [--alias 名だけの読み] [--color #RRGGBB] [--group jp|en]
  python batch/master_tool.py retire --channel-id UC…
  python batch/master_tool.py edit   --channel-id UC… [--name …] [--kana …] [--alias …] [--color …]
  python batch/master_tool.py --from-env   （Actions から。ACTION・CHANNEL_ID・NAME・KANA・ALIAS・COLOR・GROUP を読む）

add は、HOLODEX_API_KEY があれば Holodex からチャンネルの名前などを取って埋め、にじさんじのチャンネルかも確かめる。
直した内容は commit_message.txt（コミットの説明）と、Actions の実行結果の画面（GITHUB_STEP_SUMMARY）に書く。
"""
import argparse
import csv
import os
import re
import sys

import holodex

PATH = os.environ.get("MASTER_PATH", "livers_master.csv")
MESSAGE_FILE = os.environ.get("COMMIT_MESSAGE_FILE", "commit_message.txt")
GROUP_BRANCH = {"jp": "本家", "en": "EN"}
GROUP_NAMES = {"にじさんじ": "jp", "NIJISANJI EN": "en", "jp": "jp", "en": "en"}
ACTIONS = {"新人を追加する": "add", "卒業・活動終了にする": "retire", "表示名・読み・色を直す": "edit",
           "add": "add", "retire": "retire", "edit": "edit"}
ID_RE = re.compile(r"^UC[\w-]{22}$")
COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
KANA_RE = re.compile(r"^[ぁ-ゟァ-ヺー・\s]+$")


class Mistake(Exception):
    """入力の誤り。利用者に分かる言葉で知らせる。"""


def load():
    with open(PATH, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return reader.fieldnames, list(reader)


def save(fields, rows):
    with open(PATH, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def hiragana(text):
    """カタカナをひらがなにする（にじさんじの読みはひらがなで持つ）。"""
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in text)


def check(opts):
    cid = (opts.channel_id or "").strip()
    if not ID_RE.match(cid):
        raise Mistake(f"チャンネルID「{cid}」の形が違います。UC から始まる24文字です（Issue の表からそのままコピーしてください）。")
    opts.channel_id = cid
    for key in ("name", "kana", "alias", "color"):
        setattr(opts, key, (getattr(opts, key) or "").strip())
    if opts.color and not COLOR_RE.match(opts.color):
        raise Mistake(f"ライバーカラー「{opts.color}」の形が違います。# から始まる6桁（例：#A58CDC）で入れてください。空でもかまいません。")
    # 名前だけの読みは、漢字の別名（「凉舞」など）も入れられるよう、形は問わない
    if opts.kana and not KANA_RE.match(opts.kana):
        raise Mistake(f"読み「{opts.kana}」は、ひらがなかカタカナで入れてください。")


def holodex_channel(cid):
    """Holodex のチャンネル情報（キーが無ければ空）。にじさんじ以外なら誤りにする。"""
    if not (os.environ.get("HOLODEX_API_KEY") or "").strip():
        return {}
    info = holodex.get(f"/channels/{cid}") or {}
    if info and info.get("org") and info["org"] != "Nijisanji":
        raise Mistake(f"チャンネルID「{cid}」は、にじさんじのチャンネルではないようです（Holodex では {info['org']}）。")
    return info


def apply(opts):
    """一覧を直し、何をしたかの説明を返す。"""
    fields, rows = load()
    row = next((r for r in rows if r["channel_id"] == opts.channel_id), None)
    group = GROUP_NAMES.get(opts.group or "jp", "jp")

    if opts.action == "add":
        info = holodex_channel(opts.channel_id)
        name = opts.name or info.get("name") or ""
        if not name:
            raise Mistake("表示名を入れてください（例：星導ショウ）。")
        if group == "jp" and not opts.kana:
            raise Mistake("にじさんじのライバーは、読み（ひらがな。例：ほしるべしょう）を入れてください。検索と五十音順に使います。")
        kana = hiragana(opts.kana) if group == "jp" else opts.kana
        alias = hiragana(opts.alias) if group == "jp" else opts.alias
        if row is None:
            row = {f: "" for f in fields}
            row.update({"channel_id": opts.channel_id, "comments_crawled_at": ""})
            rows.append(row)
            done = "追加"
        else:
            done = "追加（すでにあったので上書き）"
        row.update({
            "name_holodex": info.get("name") or row.get("name_holodex") or name,
            "english_name": info.get("english_name") or row.get("english_name") or "",
            "group": info.get("group") or row.get("group") or "",
            "twitter": info.get("twitter") or row.get("twitter") or "",
            "branch": GROUP_BRANCH[group], "channel_type": "liver", "inactive": "FALSE",
            "display_name": name, "kana": kana, "kana_alias": alias,
        })
        if opts.color:
            row["color"] = opts.color.upper()
        message = f"ライバー一覧：{name} を{done}"

    elif row is None:
        raise Mistake(f"チャンネルID「{opts.channel_id}」は、ライバー一覧にありません。新しい人なら「新人を追加する」を選んでください。")

    elif opts.action == "retire":
        row["inactive"] = "TRUE"
        message = f"ライバー一覧：{row['display_name'] or row['name_holodex']} を卒業・活動終了に"

    else:   # edit：入れた欄だけ直す
        changes = []
        if opts.name:
            row["display_name"] = opts.name
            changes.append("表示名")
        kana_group = "en" if row.get("branch") == "EN" else "jp"
        if opts.kana:
            row["kana"] = hiragana(opts.kana) if kana_group == "jp" else opts.kana
            changes.append("読み")
        if opts.alias:
            row["kana_alias"] = hiragana(opts.alias) if kana_group == "jp" else opts.alias
            changes.append("名前だけの読み")
        if opts.color:
            row["color"] = opts.color.upper()
            changes.append("ライバーカラー")
        if not changes:
            raise Mistake("直す欄（表示名・読み・名前だけの読み・ライバーカラー）を1つ以上入れてください。")
        message = f"ライバー一覧：{row['display_name'] or row['name_holodex']} の{'・'.join(changes)}を直す"

    save(fields, rows)
    return message


def report(text, ok):
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(("✅ " if ok else "❌ ") + text + "\n")


def parse(argv):
    if argv and argv[0] == "--from-env":
        env = os.environ.get
        return argparse.Namespace(action=ACTIONS.get(env("ACTION", ""), env("ACTION", "")), channel_id=env("CHANNEL_ID"),
                                  name=env("NAME"), kana=env("KANA"), alias=env("ALIAS"), color=env("COLOR"),
                                  group=env("GROUP"))
    p = argparse.ArgumentParser(description="ライバー一覧（livers_master.csv）を直す")
    p.add_argument("action", choices=["add", "retire", "edit"])
    p.add_argument("--channel-id", required=True)
    for key in ("name", "kana", "alias", "color"):
        p.add_argument(f"--{key}")
    p.add_argument("--group", choices=["jp", "en"], default="jp")
    return p.parse_args(argv)


def main(argv=None):
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    opts = parse(sys.argv[1:] if argv is None else argv)
    try:
        if opts.action not in ("add", "retire", "edit"):
            raise Mistake("「すること」を選んでください。")
        check(opts)
        message = apply(opts)
    except Mistake as e:
        report(f"直せませんでした：{e}", ok=False)
        sys.exit(1)
    except holodex.HolodexError as e:
        report(f"Holodex に問い合わせられませんでした。少し時間をおいて、もう一度実行してください（{e}）。", ok=False)
        sys.exit(1)
    with open(MESSAGE_FILE, "w", encoding="utf-8") as f:
        f.write(message + "\n")
    report(message + "。次の更新（15分以内）からサイトに出ます。", ok=True)


if __name__ == "__main__":
    main()
