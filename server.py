#!/usr/bin/env python3
"""Content feed: turns a plain list of links and subscriptions into a browsable feed, served on the LAN.

All personal data lives in the data directory (--data-dir, default ./data), never in the code:
links.txt holds single links and feeds.txt holds subscriptions (RSS/Atom feeds, or pages that have
one, such as YouTube channels, or blogs without a feed). Both have one URL per line, optionally followed by tags; lines
starting with "#" are comments. They are created with instructions on first run.

For each link the server fetches the page once and extracts title, description, image and site
name from OpenGraph / HTML metadata (cached in cache.json). Subscriptions are re-checked on a timer
and their posts are kept in feeds.json, including posts that have since left the feed. For YouTube
channels and playlists, older videos than the feed lists are loaded once (see feeds.older_videos).
"""

import argparse
import functools
import gzip
import hashlib
import json
import re
import threading
import urllib.error
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import feeds
import jellyfin
import nrk
import pages
import ranking

ROOT = Path(__file__).resolve().parent  # code and pages; the server never writes here
DEFAULT_DATA_DIR = ROOT / "data"
# Data files; set_data_dir() points these into the data directory at startup.
LINKS_FILE = FEEDS_FILE = CACHE_FILE = ADDED_FILE = FEED_CACHE_FILE = VIEWED_FILE = SKIPPED_FILE = COLORS_FILE = SETTINGS_FILE = JELLYFIN_FILE = Path()
TEMPLATES = {
    "links.txt": """\
# One link per line, optionally followed by tags: https://example.com music longread
# Lines starting with # are ignored.
# Newest links go at the bottom; they show up first in the feed.
""",
    "feeds.txt": """\
# Subscriptions: one per line, optionally followed by tags, like links.txt.
# A line can be an RSS/Atom feed, or a page that has one: a YouTube channel (@handle, /channel/…)
# or playlist, a subreddit, a blog, a Mastodon profile, and so on. A blog or news page without a feed
# works too: its posts are read from the page. Lines starting with # are ignored.
# A Jellyfin server, or a library or series on one, needs its login in jellyfin.json; see the README.
# A series on NRK TV is its address, such as https://tv.nrk.no/serie/skam.
#
# https://www.youtube.com/@veritasium science video
# https://www.reddit.com/r/python programming
# https://xkcd.com comics
""",
}
PAGES = {  # request path -> (file, content type)
    "/": ("index.html", "text/html; charset=utf-8"),
    "/subscriptions": ("subscriptions.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/common.js": ("common.js", "text/javascript; charset=utf-8"),
}
MAX_BYTES = 1_000_000
MAX_ITEMS_SHOWN = 300  # per page of the feed; "Show more" asks for the next page
MAX_POSTS_KEPT = 5000  # per subscription, newest first
OLDER_RETRY = 6 * 3600  # seconds before trying again to load a channel's older videos after a failure

lock = threading.Lock()
cache: dict[str, dict] = {}
added: dict[str, float] = {}  # url -> unix time the server first saw the link
viewed: dict[str, float] = {}  # url -> unix time you last opened it
skipped: dict[str, float] = {}  # url -> unix time you last skipped it (the white dot)
colors: dict[str, str] = {}  # url -> color you gave it; items without one are white
subscriptions: dict[str, dict] = {}  # feeds.txt url -> resolved feed, its posts and fetch status
pending: set[str] = set()
feed_interval = 30 * 60  # seconds between checks of each subscription; set by --feed-interval
executor = ThreadPoolExecutor(max_workers=8)


def set_data_dir(path: Path):
    """Use path for all data files, creating it and the starter lists if they don't exist yet."""
    global LINKS_FILE, FEEDS_FILE, CACHE_FILE, ADDED_FILE, FEED_CACHE_FILE, VIEWED_FILE, SKIPPED_FILE, COLORS_FILE, SETTINGS_FILE, JELLYFIN_FILE
    path.mkdir(parents=True, exist_ok=True)
    LINKS_FILE, FEEDS_FILE = path / "links.txt", path / "feeds.txt"
    CACHE_FILE, ADDED_FILE, FEED_CACHE_FILE = path / "cache.json", path / "added.json", path / "feeds.json"
    VIEWED_FILE, SKIPPED_FILE, COLORS_FILE = path / "viewed.json", path / "skipped.json", path / "colors.json"
    SETTINGS_FILE, JELLYFIN_FILE = path / "settings.json", path / "jellyfin.json"
    for name, text in TEMPLATES.items():
        if not (path / name).exists():
            (path / name).write_text(text, encoding="utf-8")


def read_entries(path: Path | None = None) -> dict[str, list[str]]:
    """url -> tags, in file order (links.txt by default). A line is a URL followed by optional tags."""
    path = path or LINKS_FILE
    if not path.exists():
        return {}
    entries: dict[str, list[str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        url, *tags = line.split() or [""]
        if url.startswith("#") or urlparse(url).scheme not in ("http", "https") or url in entries:
            continue
        entries[url] = clean_tags(tags)
    return entries


def clean_tags(words: list[str]) -> list[str]:
    """Tags are lowercase words without a leading '#'; commas also separate them."""
    tags = []
    for word in " ".join(words).replace(",", " ").split():
        tag = word.lstrip("#").lower()
        if tag and tag not in tags:
            tags.append(tag)
    return tags


def read_links() -> list[str]:
    return list(read_entries())


def fetch_metadata(url: str) -> dict:
    item = {"url": url, "domain": urlparse(url).netloc.removeprefix("www."), "fetched": time.time()}
    yt = feeds.youtube_id(url)
    if yt:
        item["youtube"] = yt
        item["image"] = f"https://i.ytimg.com/vi/{yt}/hqdefault.jpg"
    try:
        with feeds.open_url(url, "text/html,*/*") as resp:
            ctype = resp.headers.get_content_type()
            final_url = resp.geturl()
            if ctype.startswith("image/"):
                item["image"] = final_url
            elif "html" in ctype:
                charset = resp.headers.get_content_charset() or "utf-8"
                parser = feeds.MetaParser()
                parser.feed(resp.read(MAX_BYTES).decode(charset, errors="replace"))
                m = parser.meta
                item["title"] = m.get("og:title") or m.get("twitter:title") or parser.title.strip()
                item["description"] = (
                    m.get("og:description") or m.get("twitter:description") or m.get("description") or ""
                )
                img = m.get("og:image") or m.get("og:image:url") or m.get("twitter:image")
                if img and "image" not in item:
                    item["image"] = urljoin(final_url, img)
                item["site"] = m.get("og:site_name") or ""
                item["icon"] = urljoin(final_url, parser.icon or "/favicon.ico")
    except Exception as e:  # network errors, bad encodings, etc. — still show the link
        item["error"] = str(e)[:200]
    item["title"] = (item.get("title") or url)[:300]
    item["description"] = (item.get("description") or "")[:500]
    return item


# Bumped whenever anything is saved: every change to links, posts, views, colors or settings is, so
# the ranked feed is only worked out again when this (or something else it depends on) changes.
state_version = 0


def save_json(path: Path, obj: dict):
    global state_version
    with lock:
        data = json.dumps(obj, indent=1)
        state_version += 1
    tmp = path.with_suffix(".tmp")
    tmp.write_text(data, encoding="utf-8")
    tmp.replace(path)


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def refresh(url: str):
    try:
        item = fetch_metadata(url)
        with lock:
            cache[url] = item
        save_json(CACHE_FILE, cache)
    finally:
        with lock:
            pending.discard(url)


def schedule(url: str, force: bool = False):
    with lock:
        if url in pending or (url in cache and not force):
            return
        pending.add(url)
    executor.submit(refresh, url)


def post_key(post: dict) -> str:
    return post.get("youtube") or post["url"]  # a YouTube short has two urls: /shorts/ID and /watch?v=ID


def post_date(post: dict) -> float:
    return post["published"] or post["first_seen"]


def merge_posts(old: list[dict], new: list[dict], now: float) -> list[dict]:
    """Old and new posts together, newest first. New posts replace old copies but keep first_seen."""
    first_seen = {post_key(p): p.get("first_seen", now) for p in old}
    merged = {post_key(p): p for p in old}
    for p in new:
        merged[post_key(p)] = {**p, "first_seen": first_seen.get(post_key(p), now)}
    posts = sorted(merged.values(), key=post_date, reverse=True)
    return posts[:MAX_POSTS_KEPT]


def refresh_subscription(url: str, force: bool = False):
    """Check one subscription and merge its posts, keeping posts that have left the feed."""
    try:
        with lock:
            old = dict(subscriptions.get(url, {}))
        try:
            logins, problem = jellyfin.read_logins(JELLYFIN_FILE)
            login = jellyfin.login_for(url, logins)
            if nrk.series_id(url):
                fresh = nrk.fetch(url, {p["url"] for p in old.get("items", [])})
            elif login is not None:
                fresh = jellyfin.fetch(url, login)
            elif old.get("page"):  # a page without a feed, read before
                fresh = pages.fetch(url, {p["url"] for p in old.get("items", [])})
            else:
                try:
                    feed_url = old.get("feed_url") or feeds.discover(url)
                    fresh = feeds.fetch(feed_url, *(() if force else (old.get("etag"), old.get("modified"))))
                except feeds.FeedError as e:
                    if jellyfin.is_jellyfin(url):
                        raise feeds.FeedError(jellyfin.missing_login(url, logins, problem, JELLYFIN_FILE)) from e
                    if not isinstance(e, feeds.NoFeed):
                        raise
                    fresh = pages.fetch(url)  # no feed, so read the posts from the page
        except Exception as e:  # keep the old posts; show the error in the subscriptions list
            with lock:
                subscriptions[url] = {**old, "error": str(e)[:600], "fetched": time.time()}
            save_json(FEED_CACHE_FILE, subscriptions)
            return
        now = time.time()
        if fresh is None:  # not modified since last check
            entry = {**old, "fetched": now}
        else:
            kept = old.get("items", [])
            if fresh.get("complete"):  # the source lists everything it has, so posts missing from it are gone
                current = {post_key(p) for p in fresh["items"]}
                kept = [p for p in kept if post_key(p) in current]
            entry = {**old, **fresh, "items": merge_posts(kept, fresh["items"], now)}
        entry.pop("error", None)
        with lock:
            subscriptions[url] = entry
        save_json(FEED_CACHE_FILE, subscriptions)
    finally:
        with lock:
            pending.discard("feed:" + url)


def schedule_subscription(url: str, force: bool = False):
    key = "feed:" + url
    with lock:
        last = subscriptions.get(url, {}).get("fetched", 0)
        if key in pending or (not force and time.time() - last < feed_interval):
            return
        pending.add(key)
    executor.submit(refresh_subscription, url, force)


def load_older(url: str):
    """Add the older videos of a YouTube subscription, beyond the ones its feed lists."""
    try:
        with lock:
            feed_url = subscriptions.get(url, {}).get("feed_url")
        try:
            older = feeds.older_videos(feed_url)
        except feeds.FeedError as e:
            with lock:
                if url in subscriptions:
                    subscriptions[url]["older_error"] = {"error": str(e)[:300], "time": time.time()}
            save_json(FEED_CACHE_FILE, subscriptions)
            return
        now = time.time()
        with lock:
            s = subscriptions.get(url)
            if s is None or s.get("feed_url") != feed_url:  # unsubscribed meanwhile
                return
            known = {post_key(p) for p in s.get("items", [])}
            # Posts already known came from the feed, with exact dates, so they win over these.
            s["items"] = merge_posts(s.get("items", []), [p for p in older if post_key(p) not in known], now)
            s["older_loaded"] = now
            s.pop("older_error", None)
        save_json(FEED_CACHE_FILE, subscriptions)
    finally:
        with lock:
            pending.discard("older:" + url)


def schedule_older(url: str):
    """Load a YouTube subscription's older videos once, after its feed has been read."""
    key = "older:" + url
    with lock:
        s = subscriptions.get(url, {})
        failed = s.get("older_error", {}).get("time", 0)
        if (key in pending or s.get("older_loaded") or not feeds.youtube_playlist_id(s.get("feed_url") or "")
                or time.time() - failed < OLDER_RETRY):
            return
        pending.add(key)
    executor.submit(load_older, url)


def subscribe(line: str) -> str | None:
    """Add a feeds.txt line (a URL optionally followed by tags). Returns an error message, or None."""
    url, *tags = line.split() or [""]
    if urlparse(url).scheme not in ("http", "https"):
        return "That isn't a web address."
    if url in read_entries(FEEDS_FILE):
        return "Already subscribed."
    with lock:
        pending.add("feed:" + url)
    refresh_subscription(url, force=True)
    with lock:
        error = subscriptions.get(url, {}).get("error")
        if error:
            subscriptions.pop(url, None)
    if error:
        save_json(FEED_CACHE_FILE, subscriptions)
        return error
    append_line(FEEDS_FILE, url, clean_tags(tags))
    schedule_older(url)
    return None


def subscription_status() -> list[dict]:
    status = []
    with lock:
        for url, tags in read_entries(FEEDS_FILE).items():
            s = subscriptions.get(url, {})
            dates = [it["published"] or it["first_seen"] for it in s.get("items", [])]
            status.append({
                "url": url,
                "tags": tags,
                "title": s.get("title") or url,
                "site": s.get("site"),
                "icon": s.get("icon"),
                "feed_url": s.get("feed_url"),
                "page": bool(s.get("page")),
                "posts": len(s.get("items", [])),
                "latest": max(dates, default=None),
                "fetched": s.get("fetched"),
                "error": s.get("error"),
                "older": "loading" if "older:" + url in pending else "loaded" if s.get("older_loaded") else None,
                "older_error": s.get("older_error", {}).get("error"),
                "checking": "feed:" + url in pending,
            })
    return status


def replace_line(path: Path, url: str, new_line: str | None) -> bool:
    """Replace (or with None, delete) the line for url, leaving comments and other lines as they are."""
    with lock:
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        for i, line in enumerate(lines):
            if (line.split() or [""])[0] == url:
                if new_line is None:
                    del lines[i]
                else:
                    lines[i] = new_line
                path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                return True
    return False


def set_subscription_tags(url: str, tags: list[str]) -> bool:
    return replace_line(FEEDS_FILE, url, " ".join([url, *clean_tags(tags)]))


def unsubscribe(url: str) -> bool:
    """Remove the subscription, with its posts and when you opened, skipped or colored them, so subscribing
    again starts afresh. Posts that are also in links.txt or another subscription keep theirs."""
    if not replace_line(FEEDS_FILE, url, None):
        return False
    elsewhere = known_urls()  # feeds.txt no longer has url
    with lock:
        posts = {p["url"] for p in subscriptions.pop(url, {}).get("items", [])} - elsewhere
        for post in posts:
            viewed.pop(post, None)
            skipped.pop(post, None)
            colors.pop(post, None)
    save_json(FEED_CACHE_FILE, subscriptions)
    save_json(VIEWED_FILE, viewed)
    save_json(SKIPPED_FILE, skipped)
    save_json(COLORS_FILE, colors)
    return True


def check_subscription(url: str) -> bool:
    """Check one subscription now and wait for the result."""
    if url not in read_entries(FEEDS_FILE):
        return False
    with lock:
        pending.add("feed:" + url)
    refresh_subscription(url, force=True)
    return True


def poll_subscriptions():
    while True:
        for url in read_entries(FEEDS_FILE):
            schedule_subscription(url)
            schedule_older(url)
        time.sleep(60)


# Colors you can give an item; ranking.py decides what they do. They have no proper names yet.
COLORS = ("green", "white", "red")


# Skipping an item (the white dot) counts as viewing it, except for next up: what comes after a post you
# skipped isn't next up, and nor is a post you skipped. Whichever you did last counts.
def seen_at(url: str) -> float | None:
    """When you last opened or skipped url. Call with lock held."""
    return max(viewed.get(url, 0), skipped.get(url, 0)) or None


def opened_at(url: str) -> float | None:
    """When you last opened url, unless you skipped it after that. Call with lock held."""
    return viewed[url] if url in viewed and viewed[url] > skipped.get(url, 0) else None


def view_state(url: str) -> dict:
    """The item's color, when you last opened or skipped it, and whether that was a skip. Call with lock held."""
    last = seen_at(url)
    return {"color": colors.get(url, "white"), "last_viewed": last,
            "skipped": last is not None and opened_at(url) is None}


def known_urls() -> set[str]:
    urls = set(read_entries())
    subs = read_entries(FEEDS_FILE)
    with lock:
        for sub_url in subs:
            urls.update(post["url"] for post in subscriptions.get(sub_url, {}).get("items", []))
    return urls


def set_viewed(url: str) -> bool:
    """Record that you opened url now. Only for items in the feed."""
    if url not in known_urls():
        return False
    with lock:
        viewed[url] = time.time()
        approx = next((p["youtube"] for s in subscriptions.values() for p in s.get("items", [])
                       if p["url"] == url and p.get("date_approx")), None)
    save_json(VIEWED_FILE, viewed)
    if approx:
        schedule_exact_date(approx)
    return True


def exact_date(video: str):
    """Give an older YouTube video, whose date is only known as "3 years ago", its exact date."""
    try:
        date = feeds.video_date(video)
        if date is None:  # keep the approximate date; opening the video again tries again
            return
        with lock:
            for s in subscriptions.values():
                posts = [p for p in s.get("items", []) if p.get("youtube") == video and p.get("date_approx")]
                for p in posts:
                    p["published"] = date
                    del p["date_approx"]
                if posts:
                    s["items"].sort(key=post_date, reverse=True)
        save_json(FEED_CACHE_FILE, subscriptions)
    finally:
        with lock:
            pending.discard("date:" + video)


def schedule_exact_date(video: str):
    key = "date:" + video
    with lock:
        if key in pending:
            return
        pending.add(key)
    executor.submit(exact_date, video)


def set_skipped(url: str) -> bool:
    """Record that you skipped url now. Only for items in the feed."""
    if url not in known_urls():
        return False
    with lock:
        skipped[url] = time.time()
    save_json(SKIPPED_FILE, skipped)
    return True


def set_color(url: str, color: str) -> bool:
    if color not in COLORS or url not in known_urls():
        return False
    with lock:
        if color == "white":
            colors.pop(url, None)
        else:
            colors[url] = color
    save_json(COLORS_FILE, colors)
    return True


def mark_older_red(url: str, age: float) -> int | None:
    """Mark the subscription's posts published more than age seconds ago red, except ones you made green.
    Posts without a date are left alone. Returns how many turned red, or None if it isn't subscribed."""
    if url not in read_entries(FEEDS_FILE):
        return None
    cutoff = time.time() - age
    with lock:
        posts = [p["url"] for p in subscriptions.get(url, {}).get("items", [])
                 if p["published"] and p["published"] < cutoff and p["url"] not in colors]
        for post in posts:
            colors[post] = "red"
    save_json(COLORS_FILE, colors)
    return len(posts)


def settings() -> dict:
    return {"weights": ranking.WEIGHTS, "defaults": ranking.DEFAULT_WEIGHTS}


def save_settings(weights) -> str | None:
    """Set the score ranker's weights from the feed's score panel. Returns an error message, or None."""
    try:
        checked = ranking.check_weights(weights if isinstance(weights, dict) else {})
    except ValueError as e:
        return str(e)
    with lock:
        ranking.WEIGHTS.update(checked)
    save_json(SETTINGS_FILE, {"weights": checked})
    return None


def load_settings():
    weights = load_json(SETTINGS_FILE).get("weights")
    if not isinstance(weights, dict):
        return
    # Settings saved by another version may lack newer weights, which keep their defaults, or have
    # ones that no longer exist, which are dropped.
    weights = {k: v for k, v in weights.items() if k in ranking.DEFAULT_WEIGHTS}
    try:
        ranking.WEIGHTS.update(ranking.check_weights({**ranking.DEFAULT_WEIGHTS, **weights}))
    except ValueError as e:
        print(f"Ignoring {SETTINGS_FILE}: {e}")


def series_state(subs) -> tuple[dict[str, str], dict[str, str]]:
    """Call with lock held. Returns:
    - next up, as {its url: the opened post's title}: for each subscription, the post after the one
      you opened most recently: the next part of its series, or else the next published. Skipped posts
      don't count as opened here, and a post you skipped isn't next up. A subscription
      whose posts belong to series of their own, such as a Jellyfin server's TV shows, has a next post
      in each series, in episode order.
    - later parts of a series none of whose earlier parts you've opened, as {url: the first part's title}.
    Series are found from titles (see ranking.title_series), or for Jellyfin and NRK TV from episode order.
    Posts marked no_series, such as NRK TV's news, have no series."""
    next_up, unstarted = {}, {}
    for sub_url in subs:
        groups = {}
        for p in subscriptions.get(sub_url, {}).get("items", []):  # newest first
            groups.setdefault(p.get("series"), []).append(p)
        for key, posts in groups.items():
            if key is None:
                prev = {} if posts[0].get("no_series") else ranking.title_series(posts)
            else:
                posts.sort(key=lambda p: p.get("episode", []), reverse=True)  # last episode first; stable otherwise
                prev = {p["url"]: posts[i + 1] for i, p in enumerate(posts[:-1])}
            opened = [(t, i) for i, p in enumerate(posts) if (t := opened_at(p["url"]))]
            if opened:
                i = max(opened)[1]
                after = [p for p in posts if prev.get(p["url"]) is posts[i]]
                nxt = after[0] if after else posts[i - 1] if i > 0 else None
                if nxt and not view_state(nxt["url"])["skipped"]:
                    next_up.setdefault(nxt["url"], posts[i]["title"] or posts[i]["url"])
            # For each part: (the series' first part, whether it or anything before it was opened)
            state = {}
            for p in posts:
                chain = [p]
                while chain[-1]["url"] in prev and prev[chain[-1]["url"]]["url"] not in state:
                    chain.append(prev[chain[-1]["url"]])
                for q in reversed(chain):
                    before = prev.get(q["url"])
                    first, any_opened = state[before["url"]] if before else (q, False)
                    state[q["url"]] = (first, any_opened or seen_at(q["url"]) is not None)
                    if before and not any_opened:
                        unstarted.setdefault(q["url"], first["title"] or first["url"])
    return next_up, unstarted


def last_opened(subs) -> dict[str, float]:
    """For each subscription you've opened or skipped a post of, when you last did. Call with lock held."""
    out = {}
    for sub_url in subs:
        times = [t for p in subscriptions.get(sub_url, {}).get("items", []) if (t := seen_at(p["url"]))]
        if times:
            out[sub_url] = max(times)
    return out


@functools.lru_cache(maxsize=200_000)
def domain(url: str) -> str:
    return urlparse(url).netloc.removeprefix("www.")


# Recently ranked feeds, so checking for changes or moving a slider back doesn't rank everything again.
# Each is kept for a minute at most, as points change with the time since things were published or opened.
RANKED_TTL = 60
ranked_cache: dict[tuple, tuple[float, list[dict]]] = {}


def feed(ranker: str, limit: int = MAX_ITEMS_SHOWN, weights: dict | None = None) -> dict:
    entries = read_entries()
    subs = read_entries(FEEDS_FILE)
    for url in entries:
        schedule(url)
    for url in subs:
        schedule_subscription(url)
    now = time.time()
    with lock:
        key = (ranker, json.dumps(weights or ranking.WEIGHTS, sort_keys=True), json.dumps([entries, subs]),
               state_version, len(pending))
        hit = ranked_cache.get(key)
        if hit and now - hit[0] < RANKED_TTL:
            return {"items": hit[1][:limit], "total": len(hit[1]), "pending": len(pending), "ranker": ranker}
        new = [url for url in entries if url not in added]
        added.update({url: now for url in new})
        items = [
            {**cache.get(url, {"url": url, "loading": True}), "kind": "link", "tags": tags,
             "added": added[url], "date": added[url], "position": i, "sub": None}
            for i, (url, tags) in enumerate(entries.items())
        ]
        seen = set(entries)
        next_after, series_first = series_state(subs)
        sub_opened = last_opened(subs)
        for sub_url, tags in subs.items():
            s = subscriptions.get(sub_url, {})
            for post in s.get("items", []):
                if post["url"] in seen:
                    continue
                seen.add(post["url"])
                items.append({
                    **post, "kind": "subscription", "tags": tags, "feed": s["title"], "icon": s.get("icon"),
                    "domain": domain(post["url"]),
                    "added": post["first_seen"], "date": post["published"] or post["first_seen"], "position": -1,
                    "sub_last_viewed": sub_opened.get(sub_url), "sub": sub_url,
                })
        for item in items:
            item.update(view_state(item["url"]), next_after=next_after.get(item["url"]),
                        series_first=series_first.get(item["url"]))
        n_pending = len(pending)
    if new:
        save_json(ADDED_FILE, added)
    ranked = ranking.rank(ranker, items, now, weights)
    with lock:
        for k in [k for k, (t, _) in ranked_cache.items() if now - t >= RANKED_TTL or k[3] != state_version]:
            del ranked_cache[k]
        if len(ranked_cache) >= 20:  # such as many slider positions tried
            ranked_cache.pop(next(iter(ranked_cache)))
        ranked_cache[key] = (now, ranked)
    return {
        "items": ranked[:limit],
        "total": len(ranked),
        "pending": n_pending,
        "ranker": ranker,
    }


def append_line(path: Path, url: str, tags: list[str]):
    with lock:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        if text and not text.endswith("\n"):
            text += "\n"
        path.write_text(text + " ".join([url, *tags]) + "\n", encoding="utf-8")


def add_link(line: str) -> bool:
    """Append a links.txt line: a URL optionally followed by tags."""
    url, *tags = line.split() or [""]
    if urlparse(url).scheme not in ("http", "https") or url in read_links():
        return False
    append_line(LINKS_FILE, url, clean_tags(tags))
    schedule(url)
    return True


# Playing Jellyfin videos in the feed: the browser asks the feed server, which relays from Jellyfin with its
# own login. Only videos of posts in the feed are relayed, and only the parts a player needs.
RELAY = re.compile(r"^/jellyfin/([0-9a-f]{12})(/videos/([0-9a-f-]{32,36})/(?:stream|master\.m3u8|main\.m3u8|hls1/\w+/-?\d+\.(?:mp4|ts)))$",
                   re.I)


def relay_key(server: str) -> str:
    return hashlib.sha256(server.encode()).hexdigest()[:12]


def jellyfin_post(url: str) -> tuple[str, dict] | None:
    """(server, login) for a Jellyfin post in the feed, or None."""
    with lock:
        known = any(p["url"] == url and p.get("jellyfin") for s in subscriptions.values() for p in s.get("items", []))
    login = jellyfin.login_for(url, jellyfin.read_logins(JELLYFIN_FILE)[0]) if known else None
    return (jellyfin.server_of(url), login) if login is not None else None


def jellyfin_relay_target(key: str, item: str) -> tuple[str, dict] | None:
    """(server, login) for a relay request, if item is a post in the feed on the server with that key."""
    item = jellyfin.normal_id(item)
    with lock:
        urls = [p["url"] for s in subscriptions.values() for p in s.get("items", [])
                if p.get("jellyfin") and jellyfin.normal_id(jellyfin.item_id(p["url"]) or "") == item]
    for url in urls:
        target = jellyfin_post(url)
        if target and relay_key(target[0]) == key.lower():
            return target
    return None


def jellyfin_play(url: str) -> dict:
    """Where the browser can play a Jellyfin post: {"src": relay address, "hls": bool}, or {"error": …}."""
    target = jellyfin_post(url)
    if target is None:
        return {"error": "not a Jellyfin post in the feed, or its server has no login in jellyfin.json"}
    server, login = target
    try:
        path, hls = jellyfin.playback(url, login)
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code} from Jellyfin"}
    except (OSError, ValueError, KeyError, feeds.FeedError) as e:
        return {"error": str(e)[:300]}
    return {"src": f"/jellyfin/{relay_key(server)}{path}", "hls": hls}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _feed_json(self, obj):
        """Send the feed compressed, and with an ETag, so the page's checks for changes get a short
        "not modified" when nothing has changed."""
        body = json.dumps(obj).encode()
        tag = '"' + hashlib.sha1(body).hexdigest() + '"'
        if self.headers.get("If-None-Match") == tag:
            self.send_response(304)
            self.send_header("ETag", tag)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        if "gzip" in self.headers.get("Accept-Encoding", ""):
            body = gzip.compress(body, compresslevel=5)
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", tag)
        self.send_header("Cache-Control", "no-cache")  # always asks, but can be answered with "not modified"
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path in PAGES:
            name, ctype = PAGES[path]
            self._send(200, (ROOT / name).read_bytes(), ctype)
        elif path == "/settings":  # the old Settings page is now the feed's score panel
            self.send_response(302)
            self.send_header("Location", "/#settings")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif path == "/api/feed":
            query = parse_qs(parsed.query)
            name = query.get("ranker", [self.server.ranker])[0]
            if name not in ranking.RANKERS:
                return self._json({"error": f"unknown ranker; available: {sorted(ranking.RANKERS)}"}, 400)
            limit = query.get("limit", [""])[0]
            weights = None
            if "weights" in query:  # unsaved weights, to preview them while you move a slider
                try:
                    weights = ranking.check_weights(json.loads(query["weights"][0]))
                except (ValueError, TypeError) as e:
                    return self._json({"error": f"weights: {e}"}, 400)
            self._feed_json(feed(name, max(int(limit), 1) if limit.isdigit() else MAX_ITEMS_SHOWN, weights))
        elif path == "/api/subscriptions":
            self._json({"subscriptions": subscription_status(), "interval_minutes": feed_interval / 60})
        elif path == "/api/settings":
            self._json(settings())
        elif path == "/api/jellyfin/play":
            result = jellyfin_play(parse_qs(parsed.query).get("url", [""])[0])
            self._json(result, 400 if "error" in result else 200)
        elif RELAY.match(path):
            self._relay(RELAY.match(path), parsed.query)
        else:
            self._send(404, b"Not found", "text/plain")

    def _relay(self, m, query: str):
        """Pass on a Jellyfin video, playlist or segment, with byte ranges so the player can seek."""
        target = jellyfin_relay_target(m.group(1), m.group(3))
        if target is None:
            return self._send(404, b"Not found", "text/plain")
        server, login = target
        headers = {"Range": self.headers["Range"]} if self.headers.get("Range") else {}
        try:
            r = jellyfin.stream(server, login, m.group(2) + (f"?{jellyfin.without_token(query)}" if query else ""), headers)
        except urllib.error.HTTPError as e:
            return self._send(e.code, f"HTTP {e.code} from Jellyfin".encode(), "text/plain")
        except (OSError, feeds.FeedError) as e:
            return self._send(502, f"couldn't reach Jellyfin: {e}".encode(), "text/plain")
        with r:
            ctype = r.headers.get("Content-Type", "application/octet-stream")
            if m.group(2).endswith(".m3u8"):  # playlists name the next files with the token in them
                return self._send(200, jellyfin.without_token(r.read(jellyfin.MAX_BYTES).decode()).encode(), ctype)
            self.send_response(r.status)
            for name in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                if r.headers.get(name):
                    self.send_header(name, r.headers[name])
            self.end_headers()
            try:
                while chunk := r.read(256 * 1024):
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):  # the player stopped or seeked elsewhere
                pass

    def do_POST(self):
        path = urlparse(self.path).path
        length = min(int(self.headers.get("Content-Length") or 0), 10_000)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._json({"error": "invalid json"}, 400)
        if path == "/api/links":
            ok = add_link(str(body.get("url", "")))
            self._json({"ok": ok}, 200 if ok else 400)
        elif path == "/api/subscriptions":
            error = subscribe(str(body.get("url", "")))
            self._json({"ok": not error, "error": error}, 400 if error else 200)
        elif path in ("/api/subscriptions/tags", "/api/subscriptions/remove", "/api/subscriptions/check"):
            url = str(body.get("url", ""))
            if path.endswith("/tags"):
                tags = body.get("tags", [])
                ok = set_subscription_tags(url, [str(t) for t in tags] if isinstance(tags, list) else [str(tags)])
            elif path.endswith("/remove"):
                ok = unsubscribe(url)
            else:
                ok = check_subscription(url)
            self._json({"ok": ok, "subscriptions": subscription_status()}, 200 if ok else 404)
        elif path == "/api/subscriptions/red":
            try:
                age = float(body.get("age"))
            except (TypeError, ValueError):
                age = -1
            if not 0 <= age < float("inf"):
                return self._json({"error": "age must be a number of seconds"}, 400)
            marked = mark_older_red(str(body.get("url", "")), age)
            self._json({"ok": marked is not None, "marked": marked, "subscriptions": subscription_status()},
                       200 if marked is not None else 404)
        elif path == "/api/viewed":
            ok = set_viewed(str(body.get("url", "")))
            self._json({"ok": ok}, 200 if ok else 404)
        elif path == "/api/skipped":
            ok = set_skipped(str(body.get("url", "")))
            self._json({"ok": ok}, 200 if ok else 404)
        elif path == "/api/color":
            ok = set_color(str(body.get("url", "")), str(body.get("color", "")))
            self._json({"ok": ok}, 200 if ok else 404)
        elif path == "/api/settings":
            error = save_settings(body.get("weights"))
            self._json({"ok": not error, "error": error, **settings()}, 400 if error else 200)
        elif path == "/api/refresh":
            for u in read_links():
                schedule(u, force=True)
            for u in read_entries(FEEDS_FILE):
                schedule_subscription(u, force=True)
            self._json({"ok": True})
        else:
            self._json({"error": "not found"}, 404)

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}")


def lan_addresses() -> list[str]:
    import socket

    addrs = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # no packets sent; just picks the outbound interface
        addrs.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return sorted(addrs) or ["<this-machine-ip>"]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces)")
    ap.add_argument("--port", type=int, default=80, help="default 80; ports below 1024 need extra permission")
    ap.add_argument("--ranker", default=ranking.DEFAULT, choices=sorted(ranking.RANKERS))
    ap.add_argument("--feed-interval", type=float, default=30, help="minutes between subscription checks")
    # Items used to be hidden after you opened them; now ranking.py scores them down instead.
    for old in ("--hide-viewed", "--hide-green"):
        ap.add_argument(old, help=argparse.SUPPRESS)
    ap.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR,
                    help="where links.txt, feeds.txt and the caches are kept (default: ./data)")
    args = ap.parse_args()

    if args.hide_viewed or args.hide_green:
        print("Note: --hide-viewed and --hide-green no longer do anything; nothing is hidden now. "
              "Viewed and colored items are scored instead (see ranking.py).")
    global feed_interval
    feed_interval = max(args.feed_interval, 1) * 60
    set_data_dir(args.data_dir.resolve())
    cache.update(load_json(CACHE_FILE))
    added.update(load_json(ADDED_FILE))
    subscriptions.update(load_json(FEED_CACHE_FILE))
    viewed.update(load_json(VIEWED_FILE))
    skipped.update(load_json(SKIPPED_FILE))
    colors.update(load_json(COLORS_FILE))
    load_settings()
    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
    except PermissionError:
        raise SystemExit(
            f"Not allowed to use port {args.port}: ports below 1024 need root or CAP_NET_BIND_SERVICE "
            f"(the systemd service grants this). For development, use e.g. --port 8090."
        )
    except OSError as e:
        raise SystemExit(f"Can't use port {args.port}: {e.strerror}. Choose another with --port.")
    server.ranker = args.ranker
    feed(args.ranker)  # warm the caches in the background
    threading.Thread(target=poll_subscriptions, daemon=True).start()

    suffix = "" if args.port == 80 else f":{args.port}"
    print(f"Content feed using data in {LINKS_FILE.parent}, serving on:")
    print(f"  http://localhost{suffix}")
    for ip in lan_addresses():
        print(f"  http://{ip}{suffix}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
