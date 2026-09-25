# -*- coding: utf-8 -*-
"""
Holodex API v2 の呼び出し（読み取りのみ）。

Powered by Holodex (https://holodex.net/)
このコードは Holodex API を利用しています。利用は Holodex API License
(https://docs.holodex.net/#section/LICENSE) に従い、同ライセンスの免責
（Disclaimer of Warranty：API は現状のまま提供され、データの正確性を含め一切保証されない）が適用されます。

APIキーは環境変数 HOLODEX_API_KEY から読む（1アプリ1キー。共有しない）。
クラウド環境で API credentials を使う場合は、キー無しでもプロキシが付ける。
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://holodex.net/api/v2"
WAIT_SEC = 1.0          # 呼び出し間隔（Holodexへの負荷対策）
USER_AGENT = "niji-oshikatsu-tools/0.1"
PAGE = 50               # Holodex の limit の上限


class HolodexError(Exception):
    pass


def get(path, params=None, retries=3):
    """1回呼ぶ。429（呼びすぎ）と5xxは間隔を空けて再試行し、それでもだめなら HolodexError。"""
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"User-Agent": USER_AGENT}
    key = (os.environ.get("HOLODEX_API_KEY") or "").strip()
    if key:
        headers["X-APIKEY"] = key
    req = urllib.request.Request(url, headers=headers)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                body = res.read().decode("utf-8")
            time.sleep(WAIT_SEC)
            return json.loads(body)
        except urllib.error.HTTPError as e:
            if (e.code == 429 or e.code >= 500) and attempt < retries - 1:
                time.sleep(15 * (attempt + 1))
                continue
            raise HolodexError(f"{path}: HTTP {e.code}") from e
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            if attempt < retries - 1:
                time.sleep(15 * (attempt + 1))
                continue
            raise HolodexError(f"{path}: {e}") from e
    raise HolodexError(f"{path}: 再試行の上限")


def as_list(data):
    """返り値が {total, items} 形式でも配列でも、配列として扱う。"""
    if isinstance(data, dict):
        return data.get("items") or []
    return data or []


def get_all(path, params, max_pages):
    """limit=50 でページをめくって全件取る。max_pages で打ち切る（取りすぎ防止）。"""
    items = []
    for page in range(max_pages):
        batch = as_list(get(path, {**params, "limit": PAGE, "offset": page * PAGE}))
        items.extend(batch)
        if len(batch) < PAGE:
            break
    return items
