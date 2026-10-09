#!/usr/bin/env python3
"""topics.json の各テーマからニュースを集めて news.json を書き出す。

標準ライブラリのみで動作(pip install 不要)。
使い方: python scripts/fetch_news.py [出力先 news.json のパス]

記事の概要は次の順で集める(どれも取れなければ概要なしで表示される)。
  1. フィード(RSS)に概要が付いていれば、それを使う
  2. Googleニュースのフィードは「同じニュースの他社の見出し」を関連報道として保存する
  3. 記事ページ側が公開している概要(og:description など)を取りに行く
     ※ 失敗しても全体は止まらない。一度取れた概要は次回以降も使い回す
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
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

MAX_SUMMARY = 220        # 概要の最大文字数
ENRICH_MAX_PER_RUN = 40  # 1回の更新で記事ページを見に行く最大件数
ENRICH_BUDGET_SEC = 120  # 概要取得に使う時間の上限
ENRICH_MAX_TRIES = 3     # 同じ記事で概要取得を試す最大回数
ENRICH_BREAKER = 8       # 連続でこの回数失敗したら、その回の取得を打ち切る


def build_url(topic: dict, settings: dict) -> str:
    lang = settings.get("language", "ja")
    country = settings.get("country", "JP")
    tail = f"hl={lang}&gl={country}&ceid={country}:{lang}"
    kind = topic["type"]
    if kind == "google_topic":
        return f"https://news.google.com/rss/headlines/section/topic/{topic['topic']}?{tail}"
    if kind == "search":
        q = urllib.parse.quote(topic["query"])
        return f"https://news.google.com/rss/search?q={q}&{tail}"
    if kind in ("feed", "pdf_index"):
        return topic["url"]
    raise ValueError(f"unknown topic type: {kind}")


def http_open(url: str, data: bytes | None = None, headers: dict | None = None,
              max_bytes: int | None = None) -> tuple[str, bytes]:
    """(最終的なURL, 本文) を返す。リダイレクトは自動で追う。"""
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
        body = res.read(max_bytes) if max_bytes else res.read()
        return res.geturl(), body


def http_get(url: str) -> bytes:
    return http_open(url)[1]


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


def trim(text: str, limit: int = MAX_SUMMARY) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# ── フィードの解析 ───────────────────────────────────────────────

_LI_RE = re.compile(
    r'<li[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>(?:\s|&nbsp;|\xa0)*(?:<font[^>]*>(.*?)</font>)?',
    re.S | re.I,
)


def parse_description(html: str, title: str) -> tuple[str, list[dict]]:
    """フィードの description から (概要, 関連報道) を取り出す。

    - <li> を含む(Googleニュースの「同じニュースの他社報道」)→ 関連報道だけ返す
    - それ以外 → リンクと媒体名を除いた本文を概要とする(見出しの繰り返しは捨てる)
    """
    if not html:
        return "", []
    if re.search(r"<li", html, re.I):
        related = []
        for href, t, s in _LI_RE.findall(html):
            rt, rs = clean_text(t), clean_text(s)
            if rs and rt.endswith(f" - {rs}"):
                rt = rt[: -len(rs) - 3].rstrip()
            if rt and rt != title:
                related.append({"title": rt, "source": rs, "link": unescape(href)})
        return "", related[:5]
    stripped = re.sub(r"<a\b.*?</a>|<font\b.*?</font>", " ", html, flags=re.S | re.I)
    text = clean_text(stripped)
    if len(text) < 20 or text == title:
        return "", []
    return trim(text), []


def _local(tag) -> str:
    """名前空間つきのタグ名 '{...}item' から 'item' だけを取り出す。"""
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _child(el, *names):
    for c in el:
        if _local(c.tag) in names:
            return c
    return None


def _child_text(el, *names) -> str:
    c = _child(el, *names)
    return (c.text or "") if c is not None else ""


def parse_feed(data: bytes) -> list[dict]:
    """RSS 2.0 / RSS 1.0(RDF) / Atom のどれでも読めるようにしてある。"""
    root = ET.fromstring(data)
    items: list[dict] = []

    for el in root.iter():
        kind = _local(el.tag)

        if kind == "item":  # RSS 2.0 と RSS 1.0(日本の媒体に多い)
            title = clean_text(_child_text(el, "title"))
            link = _child_text(el, "link").strip()
            src_el = _child(el, "source")
            source = clean_text(src_el.text) if src_el is not None else ""
            # Googleニュースの題名は「見出し - 媒体名」形式。媒体名を分離する
            if source and title.endswith(f" - {source}"):
                title = title[: -len(f" - {source}")].rstrip()
            summary, related = parse_description(_child_text(el, "description"), title)
            items.append({
                "title": title,
                "link": link,
                "source": source,
                "published": parse_date(_child_text(el, "pubDate", "date")),  # date は dc:date(RSS 1.0)
                "summary": summary,
                "related": related,
            })

        elif kind == "entry":  # Atom
            link = ""
            for l in el:
                if _local(l.tag) == "link" and l.get("rel", "alternate") == "alternate":
                    link = l.get("href", "")
                    break
            title = clean_text(_child_text(el, "title"))
            text = clean_text(_child_text(el, "summary") or _child_text(el, "content"))
            items.append({
                "title": title,
                "link": link.strip(),
                "source": "",
                "published": parse_date(_child_text(el, "updated") or _child_text(el, "published")),
                "summary": trim(text) if len(text) >= 20 and text != title else "",
                "related": [],
            })
    return items


def parse_pdf_index(html: str, base_url: str, name: str) -> list[dict]:
    """PDFへのリンク一覧ページ(放送ジャーナル等)から、日付つきの号を拾う。
    PDFのファイル名が YYMMDD.pdf の形であることを日付の手がかりにする。"""
    items, seen = [], set()
    for m in re.finditer(r'<a\b[^>]*?href=["\']([^"\']*?(\d{6})\.pdf)["\'][^>]*>(.*?)</a>', html, re.S | re.I):
        href, ymd, inner = m.group(1), m.group(2), clean_text(m.group(3))
        link = urllib.parse.urljoin(base_url, unescape(href))
        if link in seen:
            continue
        try:
            dt = datetime(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:]), 8, 0, tzinfo=timezone(timedelta(hours=9)))
        except ValueError:
            continue
        seen.add(link)
        no = re.search(r"第\s*\d+\s*号", inner)
        title = f"{name} {dt.month}月{dt.day}日号" + (f"({no.group(0).replace(' ', '')})" if no else "")
        items.append({
            "title": title, "link": link, "source": name,
            "published": dt.astimezone(timezone.utc),
            "summary": f"{name}のPDF版です。タップして開くと、この日の号を読めます。",
            "related": [],
        })
    return items


def fetch_topic(topic: dict, settings: dict) -> tuple[str, list[dict], str | None]:
    try:
        url = build_url(topic, settings)
        if topic["type"] == "pdf_index":
            items = parse_pdf_index(http_get(url).decode("utf-8", "replace"), url, topic.get("source") or topic["name"])
        else:
            items = parse_feed(http_get(url))
        # 媒体名が付いていないフィード(業界紙など)は、設定の source か、サイト名(ドメイン)で補う
        default_src = topic.get("source") or (urllib.parse.urlparse(url).hostname or "" if topic["type"] == "feed" else "")
        keywords = [k.lower() for k in topic.get("keywords", [])]
        excludes = [k.lower() for k in topic.get("exclude", [])]
        kept = []
        for it in items:
            if not it["source"] and default_src:
                it["source"] = default_src
            # 芸能・番組などの話題は除く(exclude のどれかを含む記事)
            if excludes and any(k in f"{it['title']} {it['summary']}".lower() for k in excludes):
                continue
            # 総合的なフィードから、関心のある話題だけを残す(keywords のどれかを含む記事)
            if keywords and not any(k in f"{it['title']} {it['summary']}".lower() for k in keywords):
                continue
            kept.append(it)
        return topic["id"], kept, None
    except Exception as e:  # 1テーマの失敗で全体を止めない
        return topic["id"], [], f"{type(e).__name__}: {e}"


# ── 記事ページから概要を取る ───────────────────────────────────────

def google_article_id(url: str) -> str | None:
    p = urllib.parse.urlparse(url)
    parts = [x for x in p.path.split("/") if x]
    if p.hostname == "news.google.com" and len(parts) >= 2 and parts[-2] in ("articles", "read"):
        return parts[-1]
    return None


def resolve_google(url: str) -> tuple[str, bytes | None]:
    """Googleニュースの中継URLから、配信元の記事URLを求める。(URL, 取得済みの本文 or None)

    Google側の仕組みに依存するため、変更されると取れなくなる(その場合は例外)。
    """
    aid = google_article_id(url)
    if not aid:
        return url, None
    candidates = [url, f"https://news.google.com/articles/{aid}?hl=ja&gl=JP&ceid=JP:ja"]
    for page in candidates:
        final, body = http_open(page, max_bytes=400_000)
        if (urllib.parse.urlparse(final).hostname or "") != "news.google.com":
            return final, body  # そのまま配信元へ転送された
        html = body.decode("utf-8", errors="replace")
        sg = re.search(r'data-n-a-sg="([^"]+)"', html)
        ts = re.search(r'data-n-a-ts="([^"]+)"', html)
        if not (sg and ts):
            continue
        inner = (
            '["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,null,0,1],'
            f'"X","X",1,[1,1,1],1,1,null,0,0,null,0],"{aid}",{ts.group(1)},"{sg.group(1)}"]'
        )
        form = "f.req=" + urllib.parse.quote(json.dumps([[["Fbv4je", inner]]]))
        _, res = http_open(
            "https://news.google.com/_/DotsSplashUi/data/batchexecute",
            data=form.encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
        )
        parsed = json.loads(res.decode("utf-8", errors="replace").split("\n\n")[1])[:-2]
        real = json.loads(parsed[0][2])[1]
        if isinstance(real, str) and real.startswith("http"):
            return real, None
    raise RuntimeError("could not resolve google news url")


def extract_description(body: bytes) -> str:
    """HTML の og:description / description から概要を取り出す。"""
    head = body[:300_000]
    m = re.search(rb'charset=["\']?([\w-]+)', head, re.I)
    enc = m.group(1).decode("ascii", "ignore") if m else "utf-8"
    try:
        html = head.decode(enc, errors="replace")
    except LookupError:
        html = head.decode("utf-8", errors="replace")
    found: dict[str, str] = {}
    for tag in re.findall(r"<meta\b[^>]*>", html, re.I):
        k = re.search(r'(?:property|name)\s*=\s*["\']([^"\']+)["\']', tag, re.I)
        v = re.search(r'content\s*=\s*"([^"]*)"|content\s*=\s*\'([^\']*)\'', tag, re.I)
        if k and v:
            found.setdefault(k.group(1).lower(), clean_text(v.group(1) or v.group(2)))
    for key in ("og:description", "description", "twitter:description"):
        text = found.get(key, "")
        if len(text) >= 20:
            return trim(text)
    return ""


def load_previous() -> dict[str, dict]:
    """前回公開した news.json を読み、概要を使い回す(GitHub Actions 上でだけ動く)。"""
    url = os.environ.get("PREVIOUS_NEWS_URL", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not url and "/" in repo:
        owner, name = repo.split("/", 1)
        url = f"https://{owner.lower()}.github.io/{name}/news.json"
    if not url:
        return {}
    try:
        data = json.loads(http_open(f"{url}?t={int(time.time())}")[1])
        return {a["link"]: a for a in data.get("articles", []) if a.get("link")}
    except Exception as e:
        print(f"[INFO] 前回の news.json は読めませんでした: {type(e).__name__}", file=sys.stderr)
        return {}


def enrich(articles: list[dict], previous: dict[str, dict]) -> tuple[int, int]:
    """概要のない記事に、前回分の再利用 → 記事ページの取得 の順で概要を付ける。(再利用数, 新規取得数)"""
    reused = 0
    todo: list[dict] = []
    for a in articles:  # 新しい順
        if a.get("summary"):
            continue
        prev = previous.get(a["link"]) or {}
        if prev.get("summary"):
            a["summary"] = prev["summary"]
            reused += 1
            continue
        a["tries"] = int(prev.get("tries", 0))
        if a["tries"] < ENRICH_MAX_TRIES and len(todo) < ENRICH_MAX_PER_RUN:
            todo.append(a)

    deadline = time.monotonic() + ENRICH_BUDGET_SEC
    state = {"fails": 0}

    def work(a: dict) -> tuple[dict, str | None, bool]:
        if time.monotonic() > deadline or state["fails"] >= ENRICH_BREAKER:
            return a, None, False  # 打ち切り(試行回数には数えない)
        try:
            real, body = resolve_google(a["link"])
            if body is None:
                _, body = http_open(real, max_bytes=400_000)
            desc = extract_description(body)
            state["fails"] = 0
            return a, desc, True
        except Exception:
            state["fails"] += 1
            return a, None, True

    got = 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        for a, desc, tried in ex.map(work, todo):
            if tried:
                a["tries"] = a.get("tries", 0) + 1
            if desc:
                a["summary"] = desc
                got += 1
    return reused, got


# ── メイン ───────────────────────────────────────────────

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
        tcfg = next((t for t in topics if t["id"] == topic_id), {})
        # テーマごとに件数・保存日数を変えられる(更新が週刊などの業界紙は日数を長めにする)
        t_limit = int(tcfg.get("max_items", limit))
        t_cutoff = datetime.now(timezone.utc) - timedelta(days=int(tcfg.get("max_age_days", settings.get("max_age_days", 3))))
        kept = 0
        for it in sorted(items, key=lambda x: x["published"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True):
            if kept >= t_limit:
                break
            if not it["title"] or not it["link"]:
                continue
            if it["published"] and it["published"] < t_cutoff:
                continue
            key = it["link"]
            if key in articles:
                a = articles[key]
                if topic_id not in a["topics"]:
                    a["topics"].append(topic_id)
                if not a.get("summary") and it["summary"]:
                    a["summary"] = it["summary"]
                if not a.get("related") and it["related"]:
                    a["related"] = it["related"]
            else:
                a = {
                    "title": it["title"],
                    "link": it["link"],
                    "source": it["source"],
                    "published": it["published"].isoformat() if it["published"] else None,
                    "topics": [topic_id],
                }
                if it["summary"]:
                    a["summary"] = it["summary"]
                if it["related"]:
                    a["related"] = it["related"]
                articles[key] = a
            kept += 1

    ordered = sorted(articles.values(), key=lambda a: a["published"] or "", reverse=True)

    # 全テーマが失敗したときは、古い news.json を空で上書きしないよう異常終了にする
    if not ordered and len(errors) == len(topics):
        print("[ERROR] 全テーマの取得に失敗しました", file=sys.stderr)
        return 1

    if os.environ.get("NEWS_NO_ENRICH") != "1":
        reused, got = enrich(ordered, load_previous())
        print(f"summaries: reused {reused}, newly fetched {got}")
    for a in ordered:  # 概要が付いた記事には試行回数の記録は不要
        if a.get("summary"):
            a.pop("tries", None)
        elif not a.get("tries"):
            a.pop("tries", None)

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
    with_summary = sum(1 for a in ordered if a.get("summary"))
    print(f"wrote {out_path}: {len(ordered)} articles ({with_summary} with summary), {len(errors)} topic errors")
    return 0


if __name__ == "__main__":
    sys.exit(main())
