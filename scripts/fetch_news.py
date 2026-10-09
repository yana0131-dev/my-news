#!/usr/bin/env python3
"""topics.json の各テーマからニュースを集めて news.json を書き出す。

標準ライブラリのみで動作(pip install 不要)。
使い方: python scripts/fetch_news.py [出力先 news.json のパス]
"""
from __future__ import annotations

import json
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOPICS_FILE = ROOT / "topics.json"
USER_AGENT = "Mozilla/5.0 (compatible; my-news-bot/1.0)"
TIMEOUT = 20

ATOM = "{http://www.w3.org/2005/Atom}"


def build_url(topic: dict, settings: dict) -> str:
    lang = settings.get("language", "ja")
    country = settings.get("country", "JP")
    tail = f"hl={lang}&gl={country}&ceid={country}:{lang}"
    kind = topic["type"]
    if kind == "google_topic":
        return f"https://news.google.com/rss/headlines/section/topic/{topic['topic']}?{tail}"
    if kind == "search":
        # when:1d などの期間指定は付けず、古い記事は後段で max_age_days により除外する
        q = urllib.parse.quote(topic["query"])
        return f"https://news.google.com/rss/search?q={q}&{tail}"
    if kind == "feed":
        return topic["url"]
    raise ValueError(f"unknown topic type: {kind}")


def http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
        return res.read()


def clean_text(s: str | None) -> str:
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    s = unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    try:
        dt = parsedate_to_datetime(s)  # RSS (RFC 822)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))  # Atom (ISO 8601)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_feed(data: bytes) -> list[dict]:
    root = ET.fromstring(data)
    items: list[dict] = []

    # RSS 2.0
    for it in root.iter("item"):
        title = clean_text(it.findtext("title"))
        link = (it.findtext("link") or "").strip()
        src_el = it.find("source")
        source = clean_text(src_el.text) if src_el is not None else ""
        # Googleニュースの題名は「見出し - 媒体名」形式。媒体名を分離する
        if source and title.endswith(f" - {source}"):
            title = title[: -len(f" - {source}")].rstrip()
        items.append({
            "title": title,
            "link": link,
            "source": source,
            "published": parse_date(it.findtext("pubDate")),
        })

    # Atom
    for en in root.iter(f"{ATOM}entry"):
        link = ""
        for l in en.findall(f"{ATOM}link"):
            if l.get("rel", "alternate") == "alternate":
                link = l.get("href", "")
                break
        items.append({
            "title": clean_text(en.findtext(f"{ATOM}title")),
            "link": link.strip(),
            "source": "",
            "published": parse_date(en.findtext(f"{ATOM}updated") or en.findtext(f"{ATOM}published")),
        })
    return items


def fetch_topic(topic: dict, settings: dict) -> tuple[str, list[dict], str | None]:
    try:
        items = parse_feed(http_get(build_url(topic, settings)))
        return topic["id"], items, None
    except Exception as e:  # 1テーマの失敗で全体を止めない
        return topic["id"], [], f"{type(e).__name__}: {e}"


def main() -> int:
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "news.json"
    cfg = json.loads(TOPICS_FILE.read_text(encoding="utf-8"))
    settings = cfg.get("settings", {})
    topics = cfg["topics"]
    limit = int(settings.get("max_items_per_topic", 20))
    cutoff = datetime.now(timezone.utc) - timedelta(days=int(settings.get("max_age_days", 3)))

    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(lambda t: fetch_topic(t, settings), topics))

    articles: dict[str, dict] = {}  # link -> 記事(複数テーマにまたがる記事は topics に追記)
    errors: dict[str, str] = {}
    for topic_id, items, err in results:
        if err:
            errors[topic_id] = err
            print(f"[WARN] {topic_id}: {err}", file=sys.stderr)
            continue
        kept = 0
        for it in sorted(items, key=lambda x: x["published"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True):
            if kept >= limit:
                break
            if not it["title"] or not it["link"]:
                continue
            if it["published"] and it["published"] < cutoff:
                continue
            key = it["link"]
            if key in articles:
                if topic_id not in articles[key]["topics"]:
                    articles[key]["topics"].append(topic_id)
            else:
                articles[key] = {
                    "title": it["title"],
                    "link": it["link"],
                    "source": it["source"],
                    "published": it["published"].isoformat() if it["published"] else None,
                    "topics": [topic_id],
                }
            kept += 1

    ordered = sorted(articles.values(), key=lambda a: a["published"] or "", reverse=True)

    # 全テーマが失敗したときは、古い news.json を空で上書きしないよう異常終了にする
    if not ordered and len(errors) == len(topics):
        print("[ERROR] 全テーマの取得に失敗しました", file=sys.stderr)
        return 1

    payload = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "topics": [
            {"id": t["id"], "name": t["name"], "default": bool(t.get("default", False))}
            for t in topics
        ],
        "errors": errors,
        "articles": ordered,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {out_path}: {len(ordered)} articles, {len(errors)} topic errors")
    return 0


if __name__ == "__main__":
    sys.exit(main())
