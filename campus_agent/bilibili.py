"""用 B 站公开搜索查找课程视频。失败时仍返回搜索页链接。"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

SEARCH_API = "https://api.bilibili.com/x/web-interface/search/type"
_TAG = re.compile(r"<[^>]+>")


def search_url(keyword: str) -> str:
    return "https://search.bilibili.com/all?keyword=" + urllib.parse.quote(keyword.strip())


def videos_from_payload(payload: dict, limit: int = 5) -> list[dict]:
    data = payload.get("data") if isinstance(payload, dict) else None
    rows = data.get("result") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    videos = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = _TAG.sub("", str(row.get("title") or "")).strip()
        bvid = str(row.get("bvid") or "").strip()
        link = str(row.get("arcurl") or "").strip()
        if link.startswith("//"):
            link = "https:" + link
        if not link and bvid:
            link = f"https://www.bilibili.com/video/{bvid}"
        if not title or "bilibili.com" not in link:
            continue
        videos.append(
            {
                "title": title,
                "author": str(row.get("author") or "").strip(),
                "play": int(row.get("play") or 0),
                "url": link,
            }
        )
        if len(videos) >= limit:
            break
    return videos


def search_bilibili(keyword: str, limit: int = 5) -> list[dict]:
    text = keyword.strip()
    if not text:
        return []
    query = urllib.parse.urlencode(
        {"search_type": "video", "keyword": text, "page": "1", "order": "totalrank"}
    )
    request = urllib.request.Request(
        f"{SEARCH_API}?{query}",
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://search.bilibili.com/",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        payload = json.loads(response.read().decode("utf-8", "replace"))
    if not isinstance(payload, dict) or payload.get("code") not in {0, None}:
        raise ValueError("B 站搜索没有返回课程")
    return videos_from_payload(payload, limit)
