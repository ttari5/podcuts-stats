"""Stats-site readings while the PC is off - runs on GitHub, not here.

The review window reads every post's views hourly and pushes the public stats
site, but only while the PC is on. This script is copied into the stats repo
(with a GitHub Actions workflow, see statsite.WORKFLOW) and GitHub runs it
every hour for free. When the PC hasn't pushed for a while, it reads:

- YouTube: views, likes and comments from the YouTube Data API (secret
  YT_API_KEY - a plain API key, public numbers only);
- Instagram: views from each brand's own login (secret IG_TOKEN_<BRAND>,
  e.g. IG_TOKEN_PODCUTS, IG_TOKEN_CHESS_CLIPS), likes and comments too;

and adds a reading to each post in data.json, then commits it. TikTok isn't
read: its login has to be renewed every day, and a renewal from here could
invalidate the PC's own. When the PC is back, it takes these readings into
its tracker before its next push (statsite.take_cloud_readings), so nothing
it pushes erases them.

Standard library only - it runs on GitHub's Python with nothing installed.
No secret is ever printed or written to the repo.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

PC_QUIET_MINUTES = 70     # the PC pushed more recently than this: it's on, leave it be
# GitHub drops many scheduled runs (2 of ~11 hourly ones ran on 2026-09-29), so
# the job is asked every 15 minutes and reads when its last reading is this old.
CLOUD_EVERY_MINUTES = 50
IG_API = "https://graph.instagram.com/v25.0"
YT_API = "https://www.googleapis.com/youtube/v3/videos"
YT_ID = re.compile(r"(?:youtube\.com/(?:shorts/|watch\?(?:.*&)?v=)|youtu\.be/)([\w-]{11})")
IG_CODE = re.compile(r"instagram\.com/(?:[\w.]+/)?(?:reels?|p|tv)/([A-Za-z0-9_-]+)")


def _get(url: str, timeout: float = 30) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "podcuts-stats"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _parse(t: str) -> datetime:
    return datetime.fromisoformat(t.replace("Z", "+00:00"))


def _groups(data: dict) -> list[tuple[str, dict]]:
    """(brand slug, the dict holding its posts) for every brand in data.json."""
    out = [("podcuts", data)]
    for slug, one in (data.get("by_brand") or {}).items():
        out.append((slug, one))
    return out


def secret_name(slug: str) -> str:
    return "IG_TOKEN_" + re.sub(r"[^A-Z0-9]+", "_", slug.upper()).strip("_")


def youtube(ids: list[str], key: str, get=_get) -> dict[str, dict]:
    out = {}
    for i in range(0, len(ids), 50):
        q = urllib.parse.urlencode({"part": "statistics", "id": ",".join(ids[i:i + 50]), "key": key})
        for item in get(f"{YT_API}?{q}").get("items", []):
            s = item.get("statistics") or {}
            out[item["id"]] = {"views": int(s.get("viewCount", 0)), "likes": int(s.get("likeCount", 0)),
                               "comments": int(s.get("commentCount", 0))}
    return out


def instagram(codes: set[str], token: str, get=_get) -> dict[str, dict]:
    """{shortcode: numbers} for this account's Reels among `codes`."""
    out = {}
    url = f"{IG_API}/me/media?" + urllib.parse.urlencode(
        {"fields": "id,permalink,like_count,comments_count", "limit": 100, "access_token": token})
    pages = 0
    while url and pages < 10 and len(out) < len(codes):
        page = get(url)
        pages += 1
        for m in page.get("data", []):
            code = (IG_CODE.search(m.get("permalink") or "") or [None, None])[1]
            if code not in codes:
                continue
            q = urllib.parse.urlencode({"metric": "views", "access_token": token})
            try:
                ins = get(f"{IG_API}/{m['id']}/insights?{q}")
                views = next((v["values"][0]["value"] for v in ins.get("data", []) if v.get("name") == "views"), None)
            except (urllib.error.URLError, KeyError, IndexError, ValueError):
                views = None
            out[code] = {"views": views, "likes": m.get("like_count"), "comments": m.get("comments_count")}
        url = (page.get("paging") or {}).get("next")
    return out


def refresh(data: dict, secrets: dict, *, now: datetime | None = None, get=_get,
            force: bool = False) -> dict:
    """Add a reading to every post it can read. Returns a summary; `data` is
    changed in place (and gains `cloud` with what happened)."""
    now = now or datetime.now(timezone.utc)
    stamp = now.replace(microsecond=0).isoformat()
    summary = {"at": stamp, "read": 0, "errors": [], "skipped": None}
    published = data.get("published_at")
    if not force and published and now - _parse(published) < timedelta(minutes=PC_QUIET_MINUTES):
        summary["skipped"] = "the PC is on and pushing its own readings"
        return summary
    last = data.get("cloud") or {}
    if not force and last.get("read") and last.get("at") and now - _parse(last["at"]) < timedelta(minutes=CLOUD_EVERY_MINUTES):
        summary["skipped"] = "read less than an hour ago"
        return summary
    groups = _groups(data)
    readings: dict[int, dict] = {}
    key = secrets.get("YT_API_KEY")
    yt_posts = [(p, m.group(1)) for _, g in groups for p in g.get("posts", [])
                if p.get("platform") == "youtube" and (m := YT_ID.search(p.get("url") or ""))]
    if key and yt_posts:
        try:
            got = youtube(sorted({i for _, i in yt_posts}), key, get)
            for p, i in yt_posts:
                if i in got:
                    readings[id(p)] = got[i]
        except (urllib.error.URLError, ValueError, KeyError) as exc:
            summary["errors"].append(f"YouTube: {type(exc).__name__}")
    elif yt_posts:
        summary["errors"].append("YouTube: no YT_API_KEY secret")
    for slug, g in groups:
        ig = [(p, m.group(1)) for p in g.get("posts", [])
              if p.get("platform") == "instagram" and (m := IG_CODE.search(p.get("url") or ""))]
        if not ig:
            continue
        token = secrets.get(secret_name(slug))
        if not token:
            summary["errors"].append(f"Instagram ({slug}): no {secret_name(slug)} secret")
            continue
        if now.hour == 3:
            # Once a day: renewing keeps the token going past its 60 days (the
            # same token, good for another 60). A failure here is only noted.
            try:
                get(f"https://graph.instagram.com/refresh_access_token?"
                    + urllib.parse.urlencode({"grant_type": "ig_refresh_token", "access_token": token}))
            except (urllib.error.URLError, ValueError):
                summary["errors"].append(f"Instagram ({slug}): couldn't renew the token")
        try:
            got = instagram({c for _, c in ig}, token, get)
            for p, c in ig:
                if c in got and got[c].get("views") is not None:
                    readings[id(p)] = got[c]
        except (urllib.error.URLError, ValueError, KeyError) as exc:
            summary["errors"].append(f"Instagram ({slug}): {type(exc).__name__}")
    for _, g in groups:
        touched = False
        for p in g.get("posts", []):
            r = readings.get(id(p))
            if not r or r.get("views") is None:
                continue
            p.update({k: r[k] for k in ("views", "likes", "comments") if r.get(k) is not None})
            p.setdefault("series", []).append([stamp, r["views"]])
            summary["read"] += 1
            touched = True
        if touched:
            g["checked_at"] = stamp
    data["cloud"] = summary
    return summary


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "data.json"
    # The workflow names each secret it passes (statsite.workflow).
    secrets = {k: v for k, v in os.environ.items() if (k == "YT_API_KEY" or k.startswith("IG_TOKEN_")) and v}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    summary = refresh(data, secrets, force=os.environ.get("CLOUD_FORCE") == "1")
    print(json.dumps({k: v for k, v in summary.items()}))   # counts and error names only
    if summary["skipped"] or not summary["read"]:
        # Nothing new: still note the run (and any problem) for the PC to show.
        if summary["skipped"]:
            return 0
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
