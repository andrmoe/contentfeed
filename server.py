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
import collections
import functools
import gzip
import hashlib
import json
import re
import subprocess
import threading
import tempfile
import urllib.error
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import feeds
import caldav
import citations
import icalendar_parser
import jellyfin
import nrk
import pages
import scholar
from content_length import page_minutes, youtube_minutes

import ranking
import series

ROOT = Path(__file__).resolve().parent  # code and pages; the server never writes here


def git(*args: str) -> str:
    """A git command's output for the checkout the code runs from, or "" without git or a checkout.

    The service user doesn't own the checkout (see deploy/), and git refuses to read a repository
    owned by someone else unless it is listed in safe.directory, so it is trusted here explicitly.
    """
    try:
        return subprocess.run(["git", "-c", f"safe.directory={ROOT}", "-C", str(ROOT), *args],
                              capture_output=True, text=True, timeout=5, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


# The version is VERSION's major.minor plus the number of commits, so each merged change counts up
# without anyone editing a file that every pull request would then conflict on. The code runs from
# a git checkout (see deploy/); without one, the version is VERSION alone.
COMMIT = git("rev-parse", "--short", "HEAD")  # shown next to the version in the page footer
VERSION = ".".join(filter(None, [(ROOT / "VERSION").read_text(encoding="ascii").strip(), git("rev-list", "--count", "HEAD")]))


def version_label() -> str:
    """The version shown in the page footer."""
    return VERSION + (f" ({COMMIT})" if COMMIT else "")


DEFAULT_DATA_DIR = ROOT / "data"
# Data files; set_data_dir() points these into the data directory at startup.
DATA_FILES = {
    "LINKS_FILE": "links.txt", "FEEDS_FILE": "feeds.txt", "CACHE_FILE": "cache.json", "FEED_CACHE_FILE": "feeds.json",
    "STATE_FILE": "state.json", "SETTINGS_FILE": "settings.json", "JELLYFIN_FILE": "jellyfin.json",
    "CALDAV_FILE": "caldav.json",
}
LINKS_FILE = FEEDS_FILE = CACHE_FILE = FEED_CACHE_FILE = STATE_FILE = SETTINGS_FILE = JELLYFIN_FILE = CALDAV_FILE = Path()
# Before state.json, each of its fields had a file of its own, {url: value}.
OLD_STATE_FILES = {"added": "added.json", "viewed": "viewed.json", "skipped": "skipped.json", "color": "colors.json"}
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
# A private CalDAV calendar needs its login in caldav.json; credentials don't go in this list.
# A series on NRK TV is its address, such as https://tv.nrk.no/serie/skam.
# An author on arXiv is their author page (https://arxiv.org/a/lecun_y_1) or an author search on arXiv,
# and an author on Google Scholar is their profile (https://scholar.google.com/citations?user=…).
# The papers citing a paper are its address on arXiv (https://arxiv.org/abs/1706.03762) or on Google
# Scholar: a paper on a profile, its "Cited by" list (…/scholar?cites=…) or its versions (…/scholar?cluster=…).
#
# https://www.youtube.com/@veritasium science video
# https://www.reddit.com/r/python programming
# https://xkcd.com comics
""",
}
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".js": "text/javascript; charset=utf-8"}
PAGES = {  # request path -> file
    "/": "index.html", "/subscription": "index.html", "/subscription/next-up": "index.html", "/post": "index.html",
    "/subscriptions": "subscriptions.html",
    **{"/" + name: name for name in ("style.css", "common.js", "feed.css", "feed.js", "settings.js",
                                     "subscriptions.css", "subscriptions.js")},
}
MAX_BYTES = 1_000_000
MAX_ITEMS_SHOWN = 300  # per page of the feed; "Show more" asks for the next page
MAX_POSTS_KEPT = 5000  # per subscription, newest first
OLDER_RETRY = 6 * 3600  # seconds before trying again to load a channel's older videos after a failure

lock = threading.Lock()
cache: dict[str, dict] = {}
# What you did with each item, saved together in state.json (see save_state):
added: dict[str, float] = {}  # url -> unix time the server first saw the link
viewed: dict[str, float] = {}  # url -> unix time you last opened it
colors: dict[str, str] = {}  # url -> the tag you gave it (see ranking.TAGS); most items have none
# Modes, each with its own settings, saved in settings.json. With no mode on, the feed is empty.
def starting_modes() -> list[dict]:
    """The modes before you change any: Normal, with the default settings, on in free time."""
    return [{"name": "Normal", "when": {"busy": False, "not_busy": False, "free_time": True},
             "terms": dict(ranking.DEFAULT_TERMS)}]


modes: list[dict] = starting_modes()  # [{"name", "when": {situation: bool}, "terms": the mode's own}], highest priority first
mode_choice: dict = {}  # {"name": mode}: the mode you chose in the panel; empty to follow the calendar
subscriptions: dict[str, dict] = {}  # feeds.txt url -> resolved feed, its posts and fetch status
pending: set[str] = set()
feed_interval = 30 * 60  # seconds between checks of each subscription; set by --feed-interval
executor = ThreadPoolExecutor(max_workers=8)


def set_data_dir(path: Path):
    """Use path for all data files, creating it and the starter lists if they don't exist yet."""
    path.mkdir(parents=True, exist_ok=True)
    globals().update({name: path / file for name, file in DATA_FILES.items()})
    for name, text in TEMPLATES.items():
        if not (path / name).exists():
            (path / name).write_text(text, encoding="utf-8")


_entries_read: dict[Path, tuple[tuple, dict[str, list[str]]]] = {}  # path -> (file stamp, entries)


def read_entries(path: Path | None = None) -> dict[str, list[str]]:
    """url -> tags, in file order (links.txt by default). A line is a URL followed by optional tags.
    The lists are read on almost every request, so they're parsed again only when the file changes."""
    path = path or LINKS_FILE
    try:
        info = path.stat()
    except FileNotFoundError:
        return {}
    stamp = (info.st_mtime_ns, info.st_size, info.st_ino)
    known = _entries_read.get(path)
    if known is None or known[0] != stamp:
        entries: dict[str, list[str]] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            url, *tags = line.split() or [""]
            if url.startswith("#") or urlparse(url).scheme not in ("http", "https") or url in entries:
                continue
            entries[url] = clean_tags(tags)
        known = _entries_read[path] = (stamp, entries)
    return {url: list(tags) for url, tags in known[1].items()}


def clean_tags(words: list[str]) -> list[str]:
    """Tags are lowercase words without a leading '#'; commas also separate them."""
    tags = []
    for word in " ".join(words).replace(",", " ").split():
        tag = word.lstrip("#").lower()
        if tag and tag not in tags:
            tags.append(tag)
    return tags


def fetch_metadata(url: str) -> dict:
    item = {"url": url, "domain": urlparse(url).netloc.removeprefix("www."), "fetched": time.time(), "length_version": 1}
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
            elif ctype.startswith("audio/"):
                item["audio"] = final_url
            elif "html" in ctype:
                charset = resp.headers.get_content_charset() or "utf-8"
                parser = feeds.MetaParser()
                html = resp.read(feeds.MAX_WATCH_PAGE_BYTES if yt else MAX_BYTES).decode(charset, errors="replace")
                parser.feed(html)
                item["estimated_minutes"] = (youtube_minutes(html) if yt else None) or page_minutes(
                    parser, video=bool(yt) or item["domain"] == "tv.nrk.no")
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


# Bumped when saved data changes the ranked feed. Fetch status alone doesn't affect ranking.
state_version = 0


def save_json(path: Path, obj: dict, *, affects_feed: bool = True):
    global state_version
    with lock:
        data = json.dumps(obj, indent=1)
        if affects_feed:
            state_version += 1
    tmp = path.with_suffix(".tmp")
    tmp.write_text(data, encoding="utf-8")
    tmp.replace(path)


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(*, affects_feed: bool = True):
    """Save what you did with each item to state.json, as
    {"version": 1, "items": {url: {"added": time, "viewed": time, "color": tag}}},
    each field only when it has a value."""
    with lock:
        items: dict[str, dict] = {}
        for field, values in (("added", added), ("viewed", viewed), ("color", colors)):
            for url, value in values.items():
                items.setdefault(url, {})[field] = value
    save_json(STATE_FILE, {"version": 1, "items": items}, affects_feed=affects_feed)


def load_state():
    """Read state.json, or the files it replaced, which are kept as .bak once it's written."""
    skipped: dict[str, float] = {}
    maps = {"added": added, "viewed": viewed, "skipped": skipped, "color": colors}
    if STATE_FILE.exists():
        for url, fields in load_json(STATE_FILE).get("items", {}).items():
            for field, value in fields.items():
                if field in maps:
                    maps[field][url] = value
        if skipped:
            tag_skipped(skipped)
            save_state(affects_feed=False)
        return
    old = {field: STATE_FILE.parent / name for field, name in OLD_STATE_FILES.items()}
    for field, path in old.items():
        maps[field].update(load_json(path))
    tag_skipped(skipped)
    if any(path.exists() for path in old.values()):
        save_state(affects_feed=False)
        for path in old.values():
            if path.exists():
                path.replace(path.with_name(path.name + ".bak"))
        print(f"Moved added.json, viewed.json, skipped.json and colors.json into {STATE_FILE}.")


def tag_skipped(skipped: dict[str, float]):
    """Skipping (the old white dot) counted as viewing an item. Tags no longer do, so an untagged item
    whose last skip came after you last opened it is tagged white ("maybe later") instead."""
    for url, at in skipped.items():
        if url not in colors and at > viewed.get(url, 0):
            colors[url] = "white"


def migrate_caldav_url_credentials() -> int:
    """Move legacy credentials out of feeds.txt and cached subscription keys into caldav.json."""
    if not FEEDS_FILE.exists():
        return 0
    lines = FEEDS_FILE.read_text(encoding="utf-8").splitlines()
    migrated, rewritten, replacements = 0, [], {}
    logins, problem = caldav.read_logins(CALDAV_FILE)
    for line in lines:
        parts = line.split()
        if not parts or parts[0].startswith("#"):
            rewritten.append(line)
            continue
        clean_url, login = caldav.extract_url_login(parts[0])
        if login is None:
            rewritten.append(line)
            continue
        if problem:
            raise RuntimeError(f"Can't migrate saved CalDAV credentials: {problem}")
        logins.setdefault(clean_url, login)
        replacements[parts[0]] = clean_url
        rewritten.append(" ".join([clean_url, *parts[1:]]))
        migrated += 1
    if not migrated:
        return 0

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=CALDAV_FILE.parent, delete=False) as f:
        json.dump(logins, f, indent=2)
        credential_temp = Path(f.name)
    credential_temp.chmod(0o600)
    credential_temp.replace(CALDAV_FILE)
    FEEDS_FILE.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
    with lock:
        for old_url, clean_url in replacements.items():
            if old_url in subscriptions:
                subscriptions.setdefault(clean_url, subscriptions[old_url])
                subscriptions.pop(old_url, None)
    save_json(FEED_CACHE_FILE, subscriptions, affects_feed=False)
    return migrated


# Background jobs. Each job has a pending key, its prefix and argument, such as "feed:<url>": while
# it's queued or running the key is in pending, so it isn't started twice, and the page can say how
# many fetches are under way.
def job(prefix: str):
    """Make fn a job whose pending key is prefix + its first argument, released however it ends."""
    def wrap(fn):
        @functools.wraps(fn)
        def run(arg, *args, **kwargs):
            try:
                return fn(arg, *args, **kwargs)
            finally:
                with lock:
                    pending.discard(prefix + arg)
        run.key = lambda arg: prefix + arg
        return run
    return wrap


def submit_once(work, arg, *args, unless=None, site: str | None = None) -> bool:
    """Queue a job unless it's already pending or unless() (called with lock held) is true. With a
    site, the job waits in that site's queue (see site_queues) instead of the shared executor."""
    key = work.key(arg)
    with lock:
        if key in pending or (unless is not None and unless()):
            return False
        pending.add(key)
        if site is not None:
            queue = site_queues.get(site)
            start = queue is None
            if start:
                queue = site_queues[site] = collections.deque()
            queue.append((work, arg, args))
    if site is None:
        executor.submit(work, arg, *args)
    elif start:
        threading.Thread(target=work_through, args=(site,), daemon=True).start()
    return True


# Fetches of a post's details wait in a queue per site, each worked through by a thread of its own
# while it has jobs. outbound paces requests to a site one at a time anyway, so a site with hundreds
# of posts to fetch, such as YouTube, only delays its own fetches, not other sites' or the executor's.
site_queues: dict[str, collections.deque] = {}


def work_through(site: str):
    while True:
        with lock:
            queue = site_queues[site]
            if not queue:
                del site_queues[site]
                return
            work, arg, args = queue.popleft()
        try:
            work(arg, *args)
        except Exception as e:  # like the executor, carry on with the next job
            print(f"Fetching details from {site} failed: {e}")


def run_now(work, arg, *args):
    """Run a job here and wait for it, marking it pending meanwhile."""
    with lock:
        pending.add(work.key(arg))
    return work(arg, *args)


@job("")
def refresh(url: str):
    item = fetch_metadata(url)
    with lock:
        cache[url] = item
    save_json(CACHE_FILE, cache)


def schedule(url: str, force: bool = False):
    submit_once(refresh, url, unless=lambda: url in cache and not force, site=urlparse(url).netloc.lower())


def post_key(post: dict) -> str:
    return post.get("youtube") or post["url"]  # a YouTube short has two urls: /shorts/ID and /watch?v=ID


def post_date(post: dict) -> float:
    return post["published"] or post["first_seen"]


def merge_posts(old: list[dict], new: list[dict], now: float) -> list[dict]:
    """Old and new posts together, newest first. New posts replace old copies but keep first_seen."""
    first_seen = {post_key(p): p.get("first_seen", now) for p in old}
    merged = {post_key(p): p for p in old}
    for p in new:
        if not p.get("estimated_minutes") and merged.get(post_key(p), {}).get("estimated_minutes"):
            p = {**p, "estimated_minutes": merged[post_key(p)]["estimated_minutes"]}
        if merged.get(post_key(p), {}).get("video_metadata_checked"):
            p = {**p, "video_metadata_checked": merged[post_key(p)]["video_metadata_checked"]}
        merged[post_key(p)] = {**p, "first_seen": first_seen.get(post_key(p), now)}
    posts = sorted(merged.values(), key=post_date, reverse=True)
    return posts[:MAX_POSTS_KEPT]


# Where a subscription's posts come from, saved as its "source" once found. Subscriptions saved before
# sources were recorded get theirs from what was saved about them (see known_source).
SOURCES = ("nrk", "citations", "scholar", "jellyfin", "caldav", "feed", "page")


def known_source(url: str, old: dict) -> str | None:
    """The subscription's source, if it has been found before."""
    if old.get("source") in SOURCES:
        return old["source"]
    if nrk.series_id(url):
        return "nrk"
    if citations.is_paper(url):
        return "citations"
    if scholar.user_id(url):
        return "scholar"
    if any(p.get("jellyfin") for p in old.get("items", [])):
        return "jellyfin"
    if old.get("page"):
        return "page"
    if old.get("feed_url"):
        return "feed"
    if old.get("events") or any(p.get("calendar_kind") for p in old.get("items", [])):
        return "caldav"
    return None


def fetch_calendar(url: str) -> dict:
    """A CalDAV calendar or .ics file, with its login from caldav.json. A missing login is a FeedError."""
    logins, problem = caldav.read_logins(CALDAV_FILE)
    try:
        return caldav.fetch(url, caldav.login_for(url, logins))
    except caldav.CredentialsInURL as e:
        raise feeds.FeedError(str(e)) from e
    except caldav.CalDAVError as e:
        if "HTTP 401" in str(e):
            detail = problem or f"add a username and password for this calendar to {CALDAV_FILE}"
            raise feeds.FeedError(f"CalDAV requires a login: {detail}") from e
        raise


def jellyfin_login(url: str) -> dict:
    logins, problem = jellyfin.read_logins(JELLYFIN_FILE)
    login = jellyfin.login_for(url, logins)
    if login is None:
        raise feeds.FeedError(jellyfin.missing_login(url, logins, problem, JELLYFIN_FILE))
    return login


def known_paper_titles() -> dict[str, str]:
    """url -> title of the posts from Google Scholar profiles, so a paper on one needn't be read again."""
    with lock:
        return {p["url"]: p["title"] for s in subscriptions.values() if s.get("source") == "scholar"
                for p in s.get("items", [])}


def fetch_source(source: str, url: str, old: dict, force: bool) -> tuple[str, dict | None]:
    """(source, posts) from a known source; posts are None when a feed is unchanged since last time."""
    known = {p["url"] for p in old.get("items", [])}
    if source == "nrk":
        return source, nrk.fetch(url, known)
    if source == "scholar":
        if not force and time.time() - old.get("scholar_checked", 0) < scholar.CHECK_INTERVAL:
            return source, None  # Google Scholar turns away clients that ask often
        return source, scholar.fetch(url, {p["url"]: p.get("published") for p in old.get("items", [])})
    if source == "citations":
        if not force and time.time() - old.get("citations_checked", 0) < citations.CHECK_INTERVAL:
            return source, None  # citing papers come slowly, and OpenAlex has a small free daily budget
        return source, citations.fetch(url, old.get("cited"), None if old.get("cited") else known_paper_titles())
    if source == "jellyfin":
        return source, jellyfin.fetch(url, jellyfin_login(url))
    if source == "page":
        return source, pages.fetch(url, known)
    if source == "caldav":
        return source, fetch_calendar(url)
    try:
        return source, feeds.fetch(old["feed_url"], *(() if force else (old.get("etag"), old.get("modified"))))
    except feeds.NoFeed:
        return "page", pages.fetch(url)  # the feed is gone, so read the posts from the page


def detect_source(url: str) -> tuple[str, dict]:
    """(source, posts) for a new subscription, trying each kind of source in turn."""
    if nrk.series_id(url):
        return "nrk", nrk.fetch(url)
    if citations.is_paper(url):
        return "citations", citations.fetch(url, None, known_paper_titles())
    if scholar.user_id(url):
        return "scholar", scholar.fetch(url)
    logins, problem = jellyfin.read_logins(JELLYFIN_FILE)
    login = jellyfin.login_for(url, logins)
    if login is not None:
        return "jellyfin", jellyfin.fetch(url, login)
    try:
        try:
            return "caldav", fetch_calendar(url)
        except caldav.CalDAVError:
            pass  # not a calendar
        return "feed", feeds.fetch(feeds.discover(url))
    except feeds.FeedError as e:
        if jellyfin.is_jellyfin(url):
            raise feeds.FeedError(jellyfin.missing_login(url, logins, problem, JELLYFIN_FILE)) from e
        if not isinstance(e, feeds.NoFeed):
            raise
        return "page", pages.fetch(url)  # no feed, so read the posts from the page


@job("feed:")
def refresh_subscription(url: str, force: bool = False):
    """Check one subscription and merge its posts, keeping posts that have left the feed."""
    with lock:
        old = dict(subscriptions.get(url, {}))
    try:
        source = known_source(url, old)
        source, fresh = fetch_source(source, url, old, force) if source else detect_source(url)
    except Exception as e:  # keep the old posts; show the error in the subscriptions list
        with lock:
            subscriptions[url] = {**old, "error": str(e)[:600], "fetched": time.time()}
        save_json(FEED_CACHE_FILE, subscriptions, affects_feed=False)
        return
    now = time.time()
    if fresh is None:  # not modified since last check
        entry = {**old, "fetched": now, "source": source}
    else:
        kept = old.get("items", [])
        if fresh.get("complete"):  # the source lists everything it has, so posts missing from it are gone
            current = {post_key(p) for p in fresh["items"]}
            kept = [p for p in kept if post_key(p) in current]
        entry = {**old, **fresh, "items": merge_posts(kept, fresh["items"], now), "fetched": now, "source": source}
    entry.pop("error", None)
    with lock:
        affects_feed = any(old.get(field) != entry.get(field) for field in ("items", "events", "title", "icon"))
        subscriptions[url] = entry
    save_json(FEED_CACHE_FILE, subscriptions, affects_feed=affects_feed)


def schedule_subscription(url: str, force: bool = False):
    submit_once(refresh_subscription, url, force, unless=lambda: not force and
                time.time() - subscriptions.get(url, {}).get("fetched", 0) < feed_interval)


@job("older:")
def load_older(url: str):
    """Import video history and playlists independently, retaining completed work on retry."""
    with lock:
        original = subscriptions.get(url, {})
        feed_url = original.get("feed_url")
        older_loaded = original.get("older_loaded")
        playlists_loaded = original.get("playlists_loaded")
    if not feed_url:
        return
    for kind, loaded, fetch in (("older", older_loaded, feeds.older_videos),
                                ("playlists", playlists_loaded, feeds.channel_playlists)):
        if loaded:
            continue
        try:
            result = fetch(feed_url)
        except feeds.FeedError as e:
            with lock:
                s = subscriptions.get(url)
                if s is None or s.get("feed_url") != feed_url:
                    return
                s["older_error"] = {"error": str(e)[:300], "time": time.time()}
            save_json(FEED_CACHE_FILE, subscriptions, affects_feed=False)
            return
        now = time.time()
        with lock:
            s = subscriptions.get(url)
            if s is None or s.get("feed_url") != feed_url:
                return
            if kind == "older":
                known = {post_key(p) for p in s.get("items", [])}
                durations = {post_key(p): p.get("estimated_minutes") for p in result}
                for post in s.get("items", []):
                    if not post.get("estimated_minutes") and durations.get(post_key(post)):
                        post["estimated_minutes"] = durations[post_key(post)]
                s["items"] = merge_posts(s.get("items", []),
                                         [p for p in result if post_key(p) not in known], now)
            else:
                s["youtube_playlists"] = result
            s[kind + "_loaded"] = now
            s.pop("older_error", None)
        save_json(FEED_CACHE_FILE, subscriptions)


def schedule_older(url: str):
    """Load a YouTube subscription's older videos once, after its feed has been read."""
    def done_or_failed_lately():
        s = subscriptions.get(url, {})
        return ((s.get("older_loaded") and s.get("playlists_loaded")) or not feeds.youtube_playlist_id(s.get("feed_url") or "")
                or time.time() - s.get("older_error", {}).get("time", 0) < OLDER_RETRY)
    submit_once(load_older, url, unless=done_or_failed_lately)


def subscribe(line: str) -> str | None:
    """Add a feeds.txt line (a URL optionally followed by tags). Returns an error message, or None."""
    url, *tags = line.split() or [""]
    if urlparse(url).scheme not in ("http", "https"):
        return "That isn't a web address."
    if urlparse(url).username is not None or urlparse(url).password is not None:
        return "Don't put credentials in a subscription URL. For CalDAV, store them in caldav.json."
    if url in read_entries(FEEDS_FILE):
        return "Already subscribed."
    run_now(refresh_subscription, url, True)
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
        subs = read_entries(FEEDS_FILE)
        next_up_map, _ = series_state(subs)
        for url, tags in subs.items():
            s = subscriptions.get(url, {})
            dates = [post_date(it) for it in s.get("items", [])]
            states = [view_state(u) for u in {p["url"] for p in s.get("items", [])}]
            next_up_count = sum(1 for p in s.get("items", []) if p["url"] in next_up_map)
            status.append({
                "url": url,
                "tags": tags,
                "title": s.get("title") or url,
                "site": s.get("site"),
                "icon": s.get("icon"),
                "feed_url": s.get("feed_url"),
                "page": s.get("source") == "page" or bool(s.get("page")),
                "source": s.get("source"),
                "posts": len(s.get("items", [])),
                "opened": sum(state["last_viewed"] is not None for state in states),
                "unseen": sum(state["last_viewed"] is None for state in states),
                "next_up": next_up_count,
                "color_counts": {color: sum(state["color"] == color for state in states) for color in ranking.TAGS},
                "last_viewed": max((state["last_viewed"] for state in states if state["last_viewed"] is not None), default=None),
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
    """Remove the subscription, with its posts and when you opened or tagged them, so subscribing
    again starts afresh. Posts that are also in links.txt or another subscription keep theirs."""
    if not replace_line(FEEDS_FILE, url, None):
        return False
    elsewhere = known_urls()  # feeds.txt no longer has url
    with lock:
        posts = {p["url"] for p in subscriptions.pop(url, {}).get("items", [])} - elsewhere
        for post in posts:
            viewed.pop(post, None)
            colors.pop(post, None)
    save_json(FEED_CACHE_FILE, subscriptions)
    save_state()
    return True


def check_subscription(url: str) -> bool:
    """Check one subscription now and wait for the result."""
    if url not in read_entries(FEEDS_FILE):
        return False
    run_now(refresh_subscription, url, True)
    return True


def poll_subscriptions():
    while True:
        for url in read_entries(FEEDS_FILE):
            schedule_subscription(url)
            schedule_older(url)
        time.sleep(60)


def view_state(url: str) -> dict:
    """The item's tag (None if it has none) and when you last opened it. Tags don't count as opening
    it. Call with lock held."""
    return {"color": colors.get(url), "last_viewed": viewed.get(url)}


def being_viewed(items: list[dict]) -> dict | None:
    """The item you opened most recently: the one you're viewing now, if any. Items carry last_viewed
    from view_state."""
    opened = [it for it in items if it.get("last_viewed")]
    return max(opened, key=lambda it: it["last_viewed"], default=None)


def known_urls() -> set[str]:
    urls = set(read_entries())
    subs = read_entries(FEEDS_FILE)
    with lock:
        for sub_url in subs:
            urls.update(post["url"] for post in subscriptions.get(sub_url, {}).get("items", []))
    return urls


def is_known(url: str) -> bool:
    """Whether url is a link or a subscription's post: an item in the feed."""
    if url in read_entries():
        return True
    subs = read_entries(FEEDS_FILE)
    with lock:
        return any(post["url"] == url for sub_url in subs for post in subscriptions.get(sub_url, {}).get("items", []))


def set_viewed(url: str) -> bool:
    """Record that you opened url now. Only for items in the feed."""
    if not is_known(url):
        return False
    with lock:
        viewed[url] = time.time()
        video = next((p["youtube"] for s in subscriptions.values() for p in s.get("items", [])
                      if p["url"] == url and p.get("youtube")
                      and (p.get("date_approx") or not p.get("estimated_minutes"))), None)
    save_state()
    if video:
        schedule_video_metadata(video)
    return True


@job("video:")
def update_video_metadata(video: str):
    """Enrich every copy of a video with its exact date and runtime in one fetch."""
    metadata = feeds.video_metadata(video)
    now = time.time()
    with lock:
        for subscription in subscriptions.values():
            changed_date = False
            for post in subscription.get("items", []):
                if post.get("youtube") != video:
                    continue
                post["video_metadata_checked"] = now
                if metadata.get("estimated_minutes"):
                    post["estimated_minutes"] = metadata["estimated_minutes"]
                if post.get("date_approx") and metadata.get("published"):
                    post["published"] = metadata["published"]
                    post.pop("date_approx", None)
                    changed_date = True
            if changed_date:
                subscription["items"].sort(key=post_date, reverse=True)
    save_json(FEED_CACHE_FILE, subscriptions)


def schedule_video_metadata(video: str):
    submit_once(update_video_metadata, video, site="www.youtube.com")


def set_color(url: str, color: str) -> bool:
    """Tag an item (see ranking.TAGS), or with "none" remove its tag. A tag only adds its points; it
    doesn't count as opening the item. Only for items in the feed."""
    if (color not in ranking.TAGS and color != "none") or not is_known(url):
        return False
    with lock:
        if color == "none":
            colors.pop(url, None)
        else:
            colors[url] = color
    save_state()
    return True


def mark_older_red(url: str, age: float) -> int | None:
    """Tag the subscription's posts published more than age seconds ago red, except ones you've tagged.
    Posts without a date are left alone. Returns how many turned red, or None if it isn't subscribed."""
    if url not in read_entries(FEEDS_FILE):
        return None
    cutoff = time.time() - age
    with lock:
        posts = [p["url"] for p in subscriptions.get(url, {}).get("items", [])
                 if p["published"] and p["published"] < cutoff and p["url"] not in colors]
        for post in posts:
            colors[post] = "red"
    save_state()
    return len(posts)


def settings() -> dict:
    """The settings panel's data: the modes with their settings, the active mode and how it was chosen
    (see active_mode), and the defaults and limits."""
    with lock:
        mode = active_mode(calendar_events(list(subscriptions)), time.time())
        return {"modes": [mode_data(m) for m in modes],
                "mode": mode, "defaults": ranking.DEFAULT_TERMS, "limits": ranking.TERM_LIMITS}


MODE_NAME_MAX = 40


def find_mode(name: str) -> dict | None:
    """The mode with this name, ignoring case, or None. Call with lock held."""
    return next((m for m in modes if m["name"].casefold() == name.casefold()), None)


def mode_terms(name: str | None) -> dict | None:
    """A copy of a mode's settings, or None if there's no such mode. Call with lock held."""
    mode = find_mode(name) if name else None
    return dict(mode["terms"]) if mode else None


def terms_for(name: str | None) -> dict:
    """The terms in effect in a mode: its own. With no such mode, as when none is on, the defaults.
    Call with lock held."""
    return {**ranking.DEFAULT_TERMS, **(mode_terms(name) or {})}


# The calendar situations a mode can be on in (its toggles, all off for a new mode): during a busy
# event, during a free (not busy) event, and in free time, when no event takes place. All-day events,
# such as birthdays, don't count (see is_timed).
SITUATIONS = ("busy", "not_busy", "free_time")


def mode_data(mode: dict) -> dict:
    """A mode as saved and sent to the panel."""
    return {"name": mode["name"], "when": dict(mode["when"]), "terms": dict(mode["terms"])}


def when_error(when) -> str | None:
    """Why this can't change a mode's toggles ({situation: bool}, some or all), or None if it can."""
    if not isinstance(when, dict) or not when:
        return "expected the situations to turn on or off"
    unknown = [k for k in when if k not in SITUATIONS]
    if unknown:
        return f"there's no situation called {unknown[0]}"
    if not all(isinstance(v, bool) for v in when.values()):
        return "each situation must be on (true) or off (false)"
    return None


def is_timed(event: dict) -> bool:
    """Whether a calendar item is an event at a time of day, with a start and an end: the events that
    turn modes on. All-day events, such as birthdays, and tasks don't."""
    return (event.get("calendar_kind") == "event" and not event.get("all_day")
            and event.get("calendar_start") is not None and event.get("deadline") is not None)


def begins(event: dict) -> float:
    """When an event begins: its start, or earlier by a mode's lead time (see with_leads)."""
    return event.get("begins", event["calendar_start"])


def event_lead(event: dict, preview: tuple[str, dict] | None = None) -> float:
    """An event's lead time, in seconds: that of the first mode in the list on during events of its
    kind, busy or free (not busy), which the event turns on; 0 if no mode is. With preview, (a mode's
    name, terms to use for it instead of its own), such as while you move a slider. Call with lock held."""
    situation = "not_busy" if event.get("free") else "busy"
    mode = next((m for m in modes if m["when"][situation]), None)
    if mode is None:
        return 0
    terms = preview[1] if preview and preview[0] == mode["name"] else mode["terms"]
    return terms.get("event_lead_minutes", ranking.DEFAULT_TERMS["event_lead_minutes"]) * 60


def with_leads(events: list[dict], preview: tuple[str, dict] | None = None) -> list[dict]:
    """The events, with each timed one (see is_timed) beginning its lead time early (see event_lead):
    a copy with "begins", its start minus the lead. The mode it turns on is on from then, and free
    time ends then. Call with lock held."""
    return [dict(e, begins=e["calendar_start"] - event_lead(e, preview)) if is_timed(e) else e for e in events]


def calendar_situations(events: list[dict], now: float) -> dict:
    """What takes place now: {situation: (event title or None, until) or None} for each of SITUATIONS.
    An event takes place from when it begins (see begins, with_leads) until its end. For an event, until
    is its end (of several, the one ending first); for free time, when the next event begins, or None if
    there's none. Only timed events count (see is_timed)."""
    out = {}
    for situation in ("busy", "not_busy"):
        event = situation_event(events, now, situation)
        out[situation] = (event.get("title"), event["deadline"]) if event else None
    if any(begins(e) <= now < e["deadline"] for e in events if is_timed(e)):
        out["free_time"] = None
    else:
        following = situation_event(events, now, "free_time")
        out["free_time"] = (None, begins(following) if following else None)
    return out


def situation_event(events: list[dict], now: float, situation: str) -> dict | None:
    """The event behind a situation at now: for busy or not_busy, the event of that kind taking place
    (of several, the one ending first); for free_time, the next event to begin, which ends it. None if
    there's no such event. Only timed events count (see is_timed)."""
    timed = [e for e in events if is_timed(e)]
    if situation == "free_time":
        later = [e for e in timed if begins(e) > now]
        return min(later, key=begins) if later else None
    free = situation == "not_busy"
    ongoing = [e for e in timed if begins(e) <= now < e["deadline"] and bool(e.get("free")) == free]
    return min(ongoing, key=lambda e: e["deadline"]) if ongoing else None


def mode_trigger(events: list[dict], now: float, situation: str | None) -> dict | None:
    """What turned the calendar's mode on, for the status line to show: the event behind the situation
    (see situation_event) with its calendar's name, title, start, end, description and link; for free
    time, the event that ends it, or None. Call with lock held."""
    event = situation_event(events, now, situation) if situation else None
    if event is None:
        return None
    calendar = next((sub.get("title") or url for url, sub in subscriptions.items()
                     if any(e.get("url") == event.get("url") for e in sub.get("events", []))), None)
    return {**{k: event.get(k) for k in ("url", "title", "description", "calendar_start", "deadline")},
            "calendar": calendar}


def calendar_mode(events: list[dict], now: float) -> tuple[dict | None, str | None, tuple | None]:
    """The mode the calendar turns on at now, ignoring the mode you chose: (the first mode in the list,
    the highest priority, turned on for a situation taking place then, the situation, and (the event's
    title or None, when the situation ends)), or (None, None, None). Call with lock held."""
    now_on = calendar_situations(events, now)
    for mode in modes:
        situation = next((s for s in SITUATIONS if mode["when"][s] and now_on[s]), None)
        if situation:
            return mode, situation, now_on[situation]
    return None, None, None


MODE_LOOKAHEAD = 7 * 86400  # how far ahead to look for the next mode change, in seconds


def next_mode_change(events: list[dict], now: float, name: str | None) -> tuple[float | None, str | None]:
    """When the calendar next turns on a mode other than name (None: no mode), and which (None: none),
    within MODE_LOOKAHEAD; (None, None) if it doesn't. The mode can only change when an event begins
    (see begins) or ends, so those are the times to check. Call with lock held."""
    times = sorted({t for e in events if is_timed(e)
                    for t in (begins(e), e["deadline"]) if now < t <= now + MODE_LOOKAHEAD})
    for t in times:
        mode = calendar_mode(events, t)[0]
        if (mode and mode["name"]) != name:
            return t, mode and mode["name"]
    return None, None


def active_mode(events: list[dict], now: float) -> dict:
    """The mode in effect: {"name", "source", "situation", "event", "trigger", "until", "next"}. The mode
    you chose in the panel, if any ("source": "chosen"); else the mode the calendar turns on (see
    calendar_mode; "calendar", with the situation, the event's title, if any, and the event in full as
    trigger, see mode_trigger); else none ("name": None, "source":
    "none"), and the feed is empty. Following the calendar, until is when the mode next changes and
    next the mode then (see next_mode_change); a chosen mode stays until you choose another. Events begin their lead time early (see with_leads). Call with
    lock held."""
    chosen = mode_choice.get("name")
    if chosen and find_mode(chosen):
        return {"name": chosen, "source": "chosen", "situation": None, "event": None, "trigger": None,
                "until": None, "next": None}
    events = with_leads(events)
    mode, situation, on = calendar_mode(events, now)
    name = mode["name"] if mode else None
    until, following = next_mode_change(events, now, name)
    return {"name": name, "source": "calendar" if mode else "none", "situation": situation,
            "event": on[0] if on else None, "trigger": mode_trigger(events, now, situation),
            "until": until, "next": following}


def write_settings():
    """Save the modes and the mode you chose to settings.json."""
    with lock:
        data = {"version": SETTINGS_VERSION, "modes": [mode_data(m) for m in modes],
                "mode_choice": mode_choice.get("name")}
    save_json(SETTINGS_FILE, data)


def save_settings(terms, mode: str | None = None) -> str | None:
    """Set a mode's terms from the feed's settings panel: all of them, for the mode in effect by default.
    Returns an error message, or None."""
    try:
        checked = ranking.check_terms(terms if isinstance(terms, dict) else {})
    except ValueError as e:
        return str(e)
    with lock:
        if mode is None:
            mode = active_mode(calendar_events(list(subscriptions)), time.time())["name"]
        if mode is None:
            return "no mode is on, so there are no settings to save"
        target = find_mode(mode)
        if target is None:
            return f"there's no mode called {mode}"
        target["terms"].update(checked)
    write_settings()
    return None


def mode_name_error(name) -> str | None:
    """Why a new mode can't have this name, or None if it can. Call with lock held."""
    if not isinstance(name, str) or not name.strip():
        return "a mode needs a name"
    if len(name.strip()) > MODE_NAME_MAX:
        return f"a mode's name can be at most {MODE_NAME_MAX} characters"
    if not name.isprintable():
        return "a mode's name can't have control characters"
    if name.strip().casefold() == "auto" or find_mode(name.strip()):
        return f"there's already a mode called {name.strip()}"
    return None


def add_mode(name, copy_from: str | None = None) -> str | None:
    """Add a mode whose settings start as a copy of another mode's, or the defaults, and switch to it,
    so the panel shows its settings to change. The calendar doesn't turn it on until you turn on a situation (see
    set_mode_when). Returns an error message, or None."""
    with lock:
        error = mode_name_error(name)
        if error:
            return error
        name = name.strip()
        modes.append({"name": name, "when": dict.fromkeys(SITUATIONS, False), "terms": mode_terms(copy_from) or dict(ranking.DEFAULT_TERMS)})
        mode_choice["name"] = name
    write_settings()
    return None


def set_mode_when(name, when) -> str | None:
    """Turn the calendar situations in which a mode is on on or off ({situation: bool}, see SITUATIONS).
    Returns an error, or None."""
    error = when_error(when)
    if error:
        return error
    with lock:
        mode = find_mode(name) if isinstance(name, str) else None
        if mode is None:
            return f"there's no mode called {name}"
        mode["when"].update(when)
    write_settings()
    return None


def move_mode(name, to) -> str | None:
    """Move a mode to position to in the list (0 is the top). When several modes could be on, the
    highest wins (see active_mode). Returns an error, or None."""
    with lock:
        mode = find_mode(name) if isinstance(name, str) else None
        if mode is None:
            return f"there's no mode called {name}"
        if not isinstance(to, int) or isinstance(to, bool) or not 0 <= to < len(modes):
            return f"the position must be a whole number from 0 to {len(modes) - 1}"
        modes.remove(mode)
        modes.insert(to, mode)
    write_settings()
    return None


def remove_mode(name) -> str | None:
    """Remove a mode and its settings. If you had chosen it, the calendar decides the mode again."""
    with lock:
        mode = find_mode(name) if isinstance(name, str) else None
        if mode is None:
            return f"there's no mode called {name}"
        modes.remove(mode)
        if (mode_choice.get("name") or "").casefold() == mode["name"].casefold():
            mode_choice.clear()
    write_settings()
    return None


def choose_mode(name) -> str | None:
    """Use a mode until you choose another, or follow the calendar again with None."""
    with lock:
        if name is None:
            mode_choice.clear()
        elif isinstance(name, str) and find_mode(name):
            mode_choice["name"] = find_mode(name)["name"]
        else:
            return f"there's no mode called {name}"
    write_settings()
    return None


# settings.json's format. Files without a version were saved before it had one; when a term is renamed
# or changes meaning, bump the version and move saved values in migrate_terms.
SETTINGS_VERSION = 6  # 3: the normal settings are the Normal mode's; 4: global settings; 5: past the mode change; 6: no global settings


def migrate_terms(data: dict) -> dict | None:
    """The saved terms in the current format, or None if there are none."""
    terms = data.get("terms")
    if terms is None:
        terms = data.get("weights")  # settings saved before the terms were called terms
    if not isinstance(terms, dict):
        return None
    if data.get("version", 0) < 1:
        # The old repeat setting is the per-100%-similarity amount for posts above.
        if "above_similarity_penalty" not in terms and "repeat_per_post" in terms:
            terms["above_similarity_penalty"] = terms["repeat_per_post"]
        # The oldest calendar field, task_bonus, controlled the deadline ramp; task_bonus now means
        # fixed per-item points, so move a saved ramp setting to its explicit name.
        if "task_bonus" in terms and "task_deadline_bonus" not in terms:
            terms["task_deadline_bonus"] = terms.pop("task_bonus")
    if data.get("version", 0) < 2:
        # Green's points were green_bonus_points, 0 by default. Keep them only if you changed them, so
        # the new tag defaults apply otherwise.
        green = terms.pop("green_bonus_points", 0)
        if green and "green_points" not in terms:
            terms["green_points"] = green
    if data.get("version", 0) < 5:
        # The points for running past the stop time are now for running past the next mode change.
        if "past_stop_per_minute" in terms and "past_mode_change_per_minute" not in terms:
            terms["past_mode_change_per_minute"] = terms.pop("past_stop_per_minute")
    return terms


def complete_terms(terms: dict) -> dict:
    """Saved terms, checked. Settings saved by another version may lack newer terms, which keep their
    defaults, or have ones that no longer exist, which are dropped. ValueError if a value is wrong."""
    terms = {k: v for k, v in terms.items() if k in ranking.DEFAULT_TERMS}
    return ranking.check_terms({**ranking.DEFAULT_TERMS, **terms})


def saved_when(mode: dict) -> dict:
    """A saved mode's toggles (see SITUATIONS). Modes saved with a title pattern instead, which matched
    free events, are on during free events, unless the pattern was empty (never on)."""
    when = mode.get("when")
    if isinstance(when, dict):
        return {s: when.get(s) is True for s in SITUATIONS}
    pattern = mode.get("pattern", True)  # modes saved before patterns matched their name
    return dict(dict.fromkeys(SITUATIONS, False), not_busy=bool(pattern))


def load_settings():
    """Load the modes and the mode you chose from settings.json. Without saved settings, the modes are
    the starting ones (see starting_modes). Settings saved before version 3 had normal settings, used
    whenever no mode was on; they become the Normal mode, at the bottom of the list, on in free time.
    Versions 4 and 5 also saved global settings, which are ignored."""
    data = load_json(SETTINGS_FILE)
    saved_modes = data.get("modes") if isinstance(data.get("modes"), list) else []
    with lock:
        modes.clear()
        for mode in saved_modes:
            if not isinstance(mode, dict) or mode_name_error(mode.get("name")):
                continue
            mode_terms_saved = migrate_terms({"version": data.get("version", 0), "terms": mode.get("terms")})
            try:
                modes.append({"name": mode["name"].strip(), "when": saved_when(mode),
                              "terms": complete_terms(mode_terms_saved or {})})
            except ValueError as e:
                print(f"Ignoring mode {mode['name']} in {SETTINGS_FILE}: {e}")
        if data.get("version", 0) < 3 and not find_mode("Normal"):
            normal = starting_modes()[0]
            terms = migrate_terms(data)
            try:
                normal["terms"] = complete_terms(terms or {})
            except ValueError as e:
                print(f"Ignoring the normal settings in {SETTINGS_FILE}: {e}")
            modes.append(normal)
        mode_choice.clear()
        chosen = data.get("mode_choice")
        if isinstance(chosen, str) and find_mode(chosen):
            mode_choice["name"] = find_mode(chosen)["name"]


def series_state(subs: dict[str, list[str]]) -> tuple[dict[str, str], dict[str, str]]:
    """For the subscriptions (url -> tags), except those tagged "latest". Call with lock held. Returns:
    - next up, as {its url: the opened post's title}: for each subscription, the post after the one
      you opened most recently: the next part of its series, or else the next published. A subscription
      whose posts belong to series of their own, such as a Jellyfin server's TV shows, has a next post
      in each series, in episode order.
    - later parts of a series none of whose earlier parts you've opened, as {url: the first part's title}.
    See series.py for what makes a series."""
    next_up, unstarted = {}, {}
    started = set()
    for sub_url, tags in subs.items():
        if "latest" in tags:  # you only want the latest posts from it, so neither applies
            continue
        for group in series.groups(subscriptions.get(sub_url, {})):
            posts, prev = group["posts"], group["prev"]
            opened = [(t, i) for i, p in enumerate(posts) if (t := viewed.get(p["url"]))]
            if opened:
                i = max(opened)[1]
                after = [p for p in posts if prev.get(p["url"]) is posts[i]]
                nxt = after[0] if after else posts[i - 1] if i > 0 else None
                if nxt:
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
                    state[q["url"]] = (first, any_opened or q["url"] in viewed)
                    if before and any_opened:
                        started.add(q["url"])
                    if before and not any_opened:
                        unstarted.setdefault(q["url"], first["title"] or first["url"])
    return next_up, {url: title for url, title in unstarted.items() if url not in started}


def last_opened(subs) -> dict[str, float]:
    """For each subscription you've opened a post of, when you last did. Call with lock held."""
    out = {}
    for sub_url in subs:
        times = [t for p in subscriptions.get(sub_url, {}).get("items", []) if (t := viewed.get(p["url"]))]
        if times:
            out[sub_url] = max(times)
    return out


@functools.lru_cache(maxsize=200_000)
def domain(url: str) -> str:
    return urlparse(url).netloc.removeprefix("www.")


# Recently ranked feeds, so checking for changes or moving a slider back doesn't rank everything again.
# Each is kept for a minute at most, as points change with the time since things were published or opened.
RANKED_TTL = 60
RANKED_KEPT = 20


@dataclass
class CachedRanking:
    built: float  # unix time
    ranked: ranking.RankedItems
    change: float | None  # the next mode change it was marked for; see ranking_for
    state_version: int
    waiting: list[dict]  # the posts left out until their details are fetched; see ranking_for


ranked_cache: dict[tuple, CachedRanking] = {}


def details_job(post: dict, cached: dict) -> tuple[str, float] | None:
    """For a post whose length or exact date is missing and can be fetched: (the fetch's job key, when
    it was last fetched, or 0 if never). None for a complete post, or one whose details can't be
    fetched: tasks, Jellyfin, Google Scholar, which turns away clients that read many of its pages, and audio,
    whose length its page's text would get wrong."""
    if (post.get("calendar_kind") or post.get("jellyfin") or post.get("scholar") or post.get("audio")
            or (post.get("estimated_minutes") and not post.get("date_approx"))):
        return None
    video = post.get("youtube") if post.get("kind") == "subscription" else None
    metadata = cached.get(post["url"], {})
    checked = post.get("video_metadata_checked", 0) if video else (
        metadata.get("fetched", 0) if metadata.get("length_version") == 1 else 0)
    return ("video:" + video if video else post["url"]), checked


def awaiting_details(post: dict, cached: dict) -> bool:
    """Whether a post's details haven't been fetched yet, which keeps it out of the main feed until they
    have. A post whose details turned out to be unavailable enters the feed without them."""
    job = details_job(post, cached)
    return job is not None and not job[1]


def enrichment_key(post: dict, cached: dict, pending_keys: set, now: float) -> str | None:
    """The job key to fetch an incomplete post's details: never fetched, or not for six hours. None if
    it's complete, can't be fetched, or is being fetched."""
    job = details_job(post, cached)
    if job is None:
        return None
    key, checked = job
    if key in pending_keys or (checked and now - checked < 6 * 3600):
        return None
    return key


def enrich_posts(ranked: list[dict]):
    """Fetch the details of incomplete posts (see enrichment_key), in their sites' queues (see
    site_queues)."""
    with lock:
        cached, pending_keys = dict(cache), set(pending)
    seen = set()
    now = time.time()
    for post in ranked:
        key = enrichment_key(post, cached, pending_keys, now)
        if key is None or key in seen:
            continue
        seen.add(key)
        if key.startswith("video:"):
            schedule_video_metadata(key.removeprefix("video:"))
        else:
            schedule(post["url"], force=True)


ARXIV_AUTHOR_TITLE = re.compile(r"^(.+?)(?:'s articles)? on arXiv$")


def paper_reason(s: dict, sub_url: str) -> dict | None:
    """Why a subscription's papers are in the feed: {"kind": "author", "name": <author>} for an author on
    arXiv or Google Scholar, {"kind": "cites", "name": <cited paper's title>} for the papers citing one."""
    source, title, feed_url = s.get("source"), s.get("title") or sub_url, s.get("feed_url") or ""
    if source == "citations" and s.get("cited"):
        return {"kind": "cites", "name": s["cited"]["title"], "sub": sub_url}
    if source == "scholar":
        return {"kind": "author", "name": title, "sub": sub_url}
    if source == "feed" and (feed_url.startswith("https://arxiv.org/a/") or feed_url.startswith(feeds.ARXIV_API)):
        m = ARXIV_AUTHOR_TITLE.match(title)  # "Yann LeCun's articles on arXiv", "LeCun, Yann on arXiv"
        return {"kind": "author", "name": m.group(1) if m else title, "sub": sub_url}
    return None


def build_items(entries: dict[str, list[str]], subs: dict[str, list[str]], now: float) -> tuple[list[dict], list[str]]:
    """The links and subscriptions' posts as feed items (see ranking.py for their fields), each url once,
    and the links seen for the first time. Call with lock held."""
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
    # A paper can be in several subscriptions, such as by an author you follow and citing a paper you follow;
    # its card says each reason, though it is one post, in the first subscription that has it.
    reasons = {}
    for sub_url in subs:
        s = subscriptions.get(sub_url, {})
        if reason := paper_reason(s, sub_url):
            for post in s.get("items", []):
                if reason not in reasons.setdefault(post["url"], []):
                    reasons[post["url"]].append(reason)
    for sub_url, tags in subs.items():
        s = subscriptions.get(sub_url, {})
        playlists_by_video = {}
        for playlist in s.get("youtube_playlists", []):
            for video in playlist["videos"]:
                playlists_by_video.setdefault(video, []).append(playlist["id"])
        in_series = {} if "latest" in tags else series.memberships(s)
        for post in s.get("items", []):
            if post["url"] in seen:
                continue
            seen.add(post["url"])
            items.append({
                **post, "youtube_playlists": playlists_by_video.get(post.get("youtube"), []),
                "estimated_minutes": post.get("estimated_minutes") or cache.get(post["url"], {}).get("estimated_minutes"),
                "kind": "subscription", "tags": tags, "feed": s["title"], "icon": s.get("icon"),
                "domain": domain(post["url"]),
                "added": post["first_seen"], "date": post_date(post), "position": -1,
                "sub_last_viewed": sub_opened.get(sub_url), "sub": sub_url,
                "series_ids": in_series.get(post["url"], []), "via": reasons.get(post["url"], []),
            })
    for item in items:
        item.update(view_state(item["url"]), next_after=next_after.get(item["url"]),
                    series_first=series_first.get(item["url"]))
    return items, new


def mark_past_mode_change(items: list[dict], change: float | None, now: float, post_url: str | None) -> list[dict]:
    """While the calendar is set to change the mode (change, when it next does; see active_mode), give
    each post past_mode_change: how many minutes it would run past then, for the ranker's points. The
    post whose page is open and the one you're viewing are left unmarked, so reloading while you watch
    doesn't push it down. A mode you chose doesn't change, so its posts aren't marked."""
    if change is None:
        return items
    watching = being_viewed(items)
    for it in items:
        if it is not watching and it["url"] != post_url:
            it["past_mode_change"] = ranking.minutes_past(it, now, change)
    return items


def ranking_for(ranker: str, terms: dict, entries: dict, subs: dict, search: str, post_url: str | None,
                change: float | None, now: float, complete_only: bool = False) -> tuple[ranking.RankedItems, list[dict]]:
    """The ranked feed and the posts left out of it, from the cache when it's recent and was marked for
    the same next mode change (see mark_past_mode_change), which changes with time alone. With
    complete_only, as for the main feed, posts whose details haven't been fetched yet are left out (see
    awaiting_details); they enter once fetching them saves the details, which rebuilds the ranking."""
    with lock:
        key = (ranker, json.dumps(terms, sort_keys=True), json.dumps([entries, subs, search, post_url, complete_only]))
        hit = ranked_cache.get(key)
        if hit and now - hit.built < RANKED_TTL and hit.change == change and hit.state_version == state_version:
            return hit.ranked, hit.waiting
        version = state_version
        items, new = build_items(entries, subs, now)
        cached = dict(cache) if complete_only else {}
    if new:
        save_state(affects_feed=False)
    waiting = [p for p in items if awaiting_details(p, cached)] if complete_only else []
    if waiting:
        left_out = {id(p) for p in waiting}
        items = [p for p in items if id(p) not in left_out]
    if post_url is None and search.strip():
        items = [p for p in items if matches_search(p, search)]
    ranked = ranking.rank(ranker, mark_past_mode_change(items, change, now, post_url), now, terms, lazy=True)
    with lock:
        for k in [k for k, c in ranked_cache.items() if now - c.built >= RANKED_TTL or c.state_version != state_version]:
            del ranked_cache[k]
        if len(ranked_cache) >= RANKED_KEPT:
            ranked_cache.pop(next(iter(ranked_cache)))
        if version == state_version:
            ranked_cache[key] = CachedRanking(now, ranked, change, version, waiting)
    return ranked, waiting


def feed(ranker: str, limit: int = MAX_ITEMS_SHOWN, terms: dict | None = None, *, enrich: bool = False,
         search: str = "", post_url: str | None = None, subscription_url: str | None = None) -> dict:
    """The ranked feed: everything, or a subscription's posts (with its next up), or a post's page
    (see post_details). It uses the active mode's settings (see active_mode); terms other than the saved
    ones preview them, such as while you move a slider."""
    entries = read_entries()
    subs = all_subs = read_entries(FEEDS_FILE)
    if subscription_url is not None:
        if subscription_url not in subs:
            return {"error": "Subscription not found"}
        entries = {}
        subs = {subscription_url: subs[subscription_url]}
    for url in entries:
        schedule(url)
    for url in subs:
        schedule_subscription(url)
    sync_calendars(all_subs)
    now = time.time()
    with lock:
        # Every subscribed calendar's events turn modes on, on every page, not only their own subscription's.
        events = calendar_events(all_subs)
        mode = active_mode(events, now)
        # With no mode on, the main feed is empty; other pages rank with the default settings.
        preview = (mode["name"], terms) if terms and mode["name"] else None
        terms = terms or terms_for(mode["name"])
        if preview and mode["source"] == "calendar":
            # A previewed lead time can move the next mode change, such as by opening a gap between
            # meetings, so the points for running past it follow the preview.
            until, following = next_mode_change(with_leads(events, preview), now, mode["name"])
            mode = {**mode, "until": until, "next": following}
    context = {"mode": mode, "calendars_unreachable": unreachable_calendars(all_subs)}
    main = post_url is None and subscription_url is None
    ranked, waiting = ranking_for(ranker, terms, entries, subs, search, post_url, mode["until"], now, complete_only=main)
    # Posts left out of the main feed are fetched on every request, not only on page load, so they
    # enter it as soon as they can; enrich_posts skips those already queued.
    if waiting:
        enrich_posts(waiting)
    if post_url is not None:
        return {**post_details(ranked.source, post_url, ranker, limit, terms, now, enrich=enrich, search=search), **context}
    # The post limit applies to the main feed, not to a subscription's page, where you browse its posts.
    post_limit = terms["post_limit"] if subscription_url is None and terms["post_limit"] < ranking.LIMIT else None
    if subscription_url is None and mode["name"] is None:
        post_limit = 0
    if post_limit is not None:
        limit = min(limit, post_limit)
    visible = ranked.read(limit)
    next_up = (sorted((p for p in ranked.source if p.get("next_after")), key=ranking.order, reverse=True)
               if subscription_url is not None else [])
    if enrich:
        enrich_posts([*next_up, *visible])
    result = {"items": ranking.explain_similarities(visible, ranked.source, terms, now),
              "total": ranked.total, "post_limit": post_limit, "pending": pending_count(), "ranker": ranker,
              "waiting": len(waiting), **context}
    if subscription_url is not None:
        result["next_up"] = ranking.explain_similarities(next_up, ranked.source, terms, now)
    return result


def pending_count() -> int:
    with lock:
        return len(pending)


def matches_search(post: dict, search: str) -> bool:
    text = " ".join([*(str(post.get(k) or "") for k in ("title", "description", "url", "feed", "site", "tags")),
                     *(reason["name"] for reason in post.get("via") or [])]).casefold()
    return all(word in text for word in search.casefold().split())


def post_details(source: list[dict], url: str, ranker: str, limit: int, terms: dict,
                 now: float, *, enrich: bool = False, search: str = "") -> dict:
    """Related feeds use base scores: their order is series order or similarity, not feed spreading."""
    by_url = {p["url"]: p for p in source}
    target = by_url.get(url)
    if target is None:
        return {"error": "Post not found"}

    def cards(posts):
        scored = []
        for p in posts:
            reasons = [{"label": label, "points": value} for label, value in ranking.RANKERS[ranker](p, now, terms)]
            scored.append({**p, "reasons": reasons, "score": ranking.points(sum(r["points"] for r in reasons))})
        return ranking.explain_similarities(scored, source, terms, now)

    feeds_of_series = []
    with lock:
        memberships = [(sub, dict(subscriptions.get(sub, {}))) for sub in read_entries(FEEDS_FILE)]
    for sub, subscription in memberships:
        for group in series.of_post(subscription, url):
            ordered = list({p["url"]: by_url[p["url"]] for p in group["posts"] if p["url"] in by_url}.values())
            ordered = [p for p in ordered if matches_search(p, search)]
            feeds_of_series.append({"id": sub + ":" + group["id"], "title": group["title"], "url": group["url"],
                                    "items": cards(ordered[:limit]), "total": len(ordered)})
    matches = []
    for p in source:
        if matches_search(p, search) and p["url"] != url and target.get("sub") and p.get("sub") == target["sub"]:
            percent, _ = ranking.similarity(target, p, terms)
            if percent:
                matches.append((percent, p))
    matches.sort(key=lambda pair: (pair[0], pair[1]["date"], pair[1]["url"]), reverse=True)
    similar = cards([p for _, p in matches[:limit]])
    for p, (percent, _) in zip(similar, matches):
        p["related_percent"] = percent
    item = cards([target])[0]
    if enrich:
        enrich_posts([item, *(p for group in feeds_of_series for p in group["items"]), *similar])
    return {"item": item, "series": feeds_of_series, "similar": {"items": similar, "total": len(matches)},
            "pending": pending_count(), "ranker": ranker}


def append_line(path: Path, url: str, tags: list[str]):
    with lock:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        if text and not text.endswith("\n"):
            text += "\n"
        path.write_text(text + " ".join([url, *tags]) + "\n", encoding="utf-8")


def add_link(line: str) -> bool:
    """Append a links.txt line: a URL optionally followed by tags."""
    url, *tags = line.split() or [""]
    if urlparse(url).scheme not in ("http", "https") or url in read_entries():
        return False
    append_line(LINKS_FILE, url, clean_tags(tags))
    schedule(url)
    return True


# Playing Jellyfin videos in the feed: the browser asks the feed server, which relays from Jellyfin with its
# own login. Only videos of posts in the feed are relayed, and only the parts a player needs.
RELAY = re.compile(r"^/jellyfin/([0-9a-f]{12})(/videos/([0-9a-f-]{32,36})/(?:stream|master\.m3u8|main\.m3u8|hls1/\w+/-?\d+\.(?:mp4|ts)))$",
                   re.I)
IMAGE_RELAY = re.compile(r"^/jellyfin/([0-9a-f]{12})/images/([0-9a-f]{32})$", re.I)


def relay_key(server: str) -> str:
    return hashlib.sha256(server.encode()).hexdigest()[:12]


def jellyfin_post(url: str) -> tuple[str, dict] | None:
    """(server, login) for a Jellyfin post in the feed, or None."""
    with lock:
        known = any(p["url"] == url and p.get("jellyfin") for s in subscriptions.values() for p in s.get("items", []))
    login = jellyfin.login_for(url, jellyfin.read_logins(JELLYFIN_FILE)[0]) if known else None
    return (jellyfin.server_of(url), login) if login is not None else None


def jellyfin_posts(key: str, item: str) -> list[tuple[dict, str, dict]]:
    """(post, server, login) for each post in the feed of the Jellyfin item on the server with that relay key."""
    item = jellyfin.normal_id(item)
    with lock:
        posts = [p for s in subscriptions.values() for p in s.get("items", [])
                 if p.get("jellyfin") and jellyfin.normal_id(jellyfin.item_id(p["url"]) or "") == item]
    if not posts:
        return []
    logins = jellyfin.read_logins(JELLYFIN_FILE)[0]
    found = []
    for post in posts:
        server, login = jellyfin.server_of(post["url"]), jellyfin.login_for(post["url"], logins)
        if login is not None and relay_key(server) == key.lower():
            found.append((post, server, login))
    return found


def jellyfin_relay_target(key: str, item: str) -> tuple[str, dict] | None:
    """(server, login) for a relay request, if item is a post in the feed on the server with that key."""
    return next(((server, login) for _, server, login in jellyfin_posts(key, item)), None)


def jellyfin_image_target(key: str, item: str) -> tuple[tuple[str, dict], str] | None:
    """The authenticated Jellyfin image URL for a pictured post in the feed."""
    for post, server, login in jellyfin_posts(key, item):
        # The source address came from Jellyfin metadata; relay only its path and query.
        image = urlparse(post.get("_jellyfin_image", ""))
        if image.scheme in ("http", "https") and image.netloc == urlparse(server).netloc and image.path.startswith("/Items/"):
            return (server, login), image.path + (f"?{image.query}" if image.query else "")
    return None


def migrate_calendar_events() -> bool:
    """Move calendar events saved among a subscription's posts, before they were kept apart, to its events."""
    changed = False
    with lock:
        for subscription in subscriptions.values():
            items = subscription.get("items", [])
            events = [p for p in items if p.get("calendar_kind") == "event"]
            if events:
                subscription["items"] = [p for p in items if p.get("calendar_kind") != "event"]
                subscription["events"] = [*subscription.get("events", []), *events]
                changed = True
    return changed


def calendar_events(subs) -> list[dict]:
    """The subscriptions' calendar events. Call with lock held."""
    return [event for url in subs for event in subscriptions.get(url, {}).get("events", [])]


# CalDAV calendars are kept in step on every feed request, not only on the subscription timer: each
# request asks for the calendar's version (see caldav.version), which changes with every change in it,
# and reads the calendar again only when it has. So events are at most a page update behind the
# calendar. When the server doesn't answer, the events from the last read stay in use, and the feed
# response names the calendar as unreachable. Shared .ics links are read on the timer as before.
CALENDAR_CHECK_GAP = 2  # seconds: requests within this long of a check reuse it
CALENDAR_RETRY = 30  # seconds between checks while a calendar doesn't answer
CALENDAR_UNVERSIONED = 60  # seconds between reads of a calendar whose server reports no version
calendar_checks: dict[str, dict] = {}  # url -> {"lock", "checked" (monotonic time), "unreachable" (unix time or None)}
calendar_checker = ThreadPoolExecutor(max_workers=4)  # apart from executor, so fetches don't delay checks


def checked_calendars(subs) -> list[str]:
    """The CalDAV collections among the subscriptions: those whose last read reported a version (or None)."""
    with lock:
        return [url for url in subs if subscriptions.get(url, {}).get("source") == "caldav"
                and "version" in subscriptions[url]]


def check_calendar(url: str):
    """Read the calendar again if it has changed since it was last read."""
    state = calendar_checks.setdefault(url, {"lock": threading.Lock(), "checked": float("-inf"), "unreachable": None})
    with state["lock"]:  # requests arriving during a check wait for it, and then use it
        gap = CALENDAR_RETRY if state["unreachable"] else CALENDAR_CHECK_GAP
        if time.monotonic() - state["checked"] < gap:
            return
        with lock:
            saved = dict(subscriptions.get(url, {}))
        try:
            current = caldav.version(url, caldav.login_for(url, caldav.read_logins(CALDAV_FILE)[0]))
            if (current != saved.get("version") or saved.get("error")
                    or (current is None and time.time() - saved.get("fetched", 0) >= CALENDAR_UNVERSIONED)):
                run_now(refresh_subscription, url, True)
                with lock:
                    failed = bool(subscriptions.get(url, {}).get("error"))
            else:
                failed = False
        except (caldav.CalDAVError, OSError):
            failed = True
        state["unreachable"] = (state["unreachable"] or time.time()) if failed else None
        state["checked"] = time.monotonic()


def reread_outdated_calendars() -> int:
    """Read again the calendars last read with an older parser, whose saved events and tasks would
    otherwise stay as that parser read them until the calendar changes. Returns how many."""
    with lock:
        outdated = [url for url, s in subscriptions.items() if s.get("source") == "caldav"
                    and s.get("parser") != icalendar_parser.PARSER_VERSION]
    for url in outdated:
        schedule_subscription(url, force=True)
    return len(outdated)


def sync_calendars(subs):
    """Bring every CalDAV calendar among the subscriptions up to date, checking them side by side."""
    for future in [calendar_checker.submit(check_calendar, url) for url in checked_calendars(subs)]:
        future.result()


def unreachable_calendars(subs) -> list[dict]:
    """The calendars whose server didn't answer the last check, with when their events were last read."""
    out = []
    with lock:
        for url in subs:
            state = calendar_checks.get(url)
            if state and state["unreachable"]:
                s = subscriptions.get(url, {})
                out.append({"url": url, "title": s.get("title") or url, "fetched": s.get("fetched")})
    return out


def migrate_jellyfin_images() -> bool:
    """Move cached Jellyfin thumbnails onto the feed origin after upgrading."""
    changed = False
    with lock:
        for subscription in subscriptions.values():
            for post in subscription.get("items", []):
                source = post.get("image")
                if not post.get("jellyfin") or not source or post.get("_jellyfin_image"):
                    continue
                image = urlparse(source)
                server = jellyfin.server_of(post["url"])
                item = jellyfin.item_id(post["url"])
                if (item and image.scheme in ("http", "https")
                        and image.netloc == urlparse(server).netloc and image.path.startswith("/Items/")):
                    post["_jellyfin_image"] = source
                    post["image"] = f"/jellyfin/{relay_key(server)}/images/{jellyfin.normal_id(item)}"
                    changed = True
    return changed


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


def query_limit(query: dict) -> int:
    limit = query.get("limit", [""])[0]
    return max(int(limit), 1) if limit.isdigit() else MAX_ITEMS_SHOWN


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

    def _not_found(self):
        self._send(404, b"Not found", "text/plain")

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
        path, query = parsed.path, parse_qs(parsed.query)
        if path in PAGES:
            return self._page(PAGES[path])
        if path in FEED_APIS:
            return self._feed(path, query)
        if path in GET_APIS:
            return self._json(*GET_APIS[path](query))
        if m := IMAGE_RELAY.fullmatch(path):
            return self._relay_image(m)
        if m := RELAY.fullmatch(path):
            return self._relay(m, parsed.query)
        self._not_found()

    def _page(self, name: str):
        body = (ROOT / name).read_bytes()
        if name.endswith(".html"):
            body = body.replace(b"{{VERSION}}", version_label().encode("ascii"))
        self._send(200, body, CONTENT_TYPES[Path(name).suffix])

    def _feed(self, path: str, query: dict):
        """/api/feed, or with url, /api/post (a post's page) or /api/subscription (one subscription;
        view=next-up for its next up only)."""
        ranker = query.get("ranker", [self.server.ranker])[0]
        if ranker not in ranking.RANKERS:
            return self._json({"error": f"unknown ranker; available: {sorted(ranking.RANKERS)}"}, 400)
        terms = None
        if "terms" in query:  # unsaved terms, to preview them while you move a slider
            try:
                terms = ranking.check_terms(json.loads(query["terms"][0]))
            except (ValueError, TypeError) as e:
                return self._json({"error": f"terms: {e}"}, 400)
        options = {"enrich": query.get("enrich") == ["1"], "search": query.get("q", [""])[0]}
        if path != "/api/feed":
            url = query.get("url", [""])[0]
            if not url:
                return self._json({"error": "url is required"}, 400)
            options["post_url" if path == "/api/post" else "subscription_url"] = url
        result = feed(ranker, query_limit(query), terms, **options)
        if "error" in result:
            return self._json(result, 404)
        if path == "/api/subscription":
            result["subscription"] = next((s for s in subscription_status() if s["url"] == url), None)
            if query.get("view") == ["next-up"]:
                result["items"] = result.get("next_up", [])
                result["total"] = len(result["items"])
        self._feed_json(result)

    def _stream(self, r, headers: tuple[str, ...], extra: dict | None = None):
        """Pass on an upstream response's status, the named headers and its body."""
        self.send_response(r.status)
        for name in headers:
            if r.headers.get(name):
                self.send_header(name, r.headers[name])
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        try:
            while chunk := r.read(256 * 1024):
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):  # the player stopped or seeked elsewhere
            pass

    def _open_jellyfin(self, server: str, login: dict, path: str, headers: dict):
        """The Jellyfin response, or None after sending the error."""
        try:
            return jellyfin.stream(server, login, path, headers)
        except urllib.error.HTTPError as e:
            self._send(e.code, f"HTTP {e.code} from Jellyfin".encode(), "text/plain")
        except (OSError, feeds.FeedError) as e:
            self._send(502, f"couldn't reach Jellyfin: {e}".encode(), "text/plain")
        return None

    def _relay(self, m, query: str):
        """Pass on a Jellyfin video, playlist or segment, with byte ranges so the player can seek."""
        target = jellyfin_relay_target(m.group(1), m.group(3))
        if target is None:
            return self._not_found()
        server, login = target
        headers = {"Range": self.headers["Range"]} if self.headers.get("Range") else {}
        r = self._open_jellyfin(server, login, m.group(2) + (f"?{jellyfin.without_token(query)}" if query else ""), headers)
        if r is None:
            return
        with r:
            if m.group(2).endswith(".m3u8"):  # playlists name the next files with the token in them
                ctype = r.headers.get("Content-Type", "application/octet-stream")
                return self._send(200, jellyfin.without_token(r.read(jellyfin.MAX_BYTES).decode()).encode(), ctype)
            self._stream(r, ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"))

    def _relay_image(self, m):
        """Serve a Jellyfin preview through this origin to avoid browser mixed content blocking."""
        result = jellyfin_image_target(m.group(1), m.group(2))
        if result is None:
            return self._not_found()
        (server, login), path = result
        r = self._open_jellyfin(server, login, path, {})
        if r is None:
            return
        with r:
            self._stream(r, ("Content-Length",), {"Content-Type": r.headers.get("Content-Type", "application/octet-stream"),
                                                  "Cache-Control": "no-store"})

    def do_POST(self):
        length = min(int(self.headers.get("Content-Length") or 0), 10_000)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._json({"error": "invalid json"}, 400)
        action = POST_APIS.get(urlparse(self.path).path)
        if action is None or not isinstance(body, dict):
            return self._json({"error": "not found" if action is None else "expected a json object"},
                              404 if action is None else 400)
        self._json(*action(body))

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}")


# The JSON API: each takes the query (GET) or body (POST) and returns (response, status code).
def ok(done: bool, failed: int = 404, **extra) -> tuple[dict, int]:
    return {"ok": done, **extra}, 200 if done else failed


def with_status(done: bool) -> tuple[dict, int]:
    return ok(done, subscriptions=subscription_status())


def subscribe_api(body):
    error = subscribe(str(body.get("url", "")))
    return ok(not error, 400, error=error)


def subscription_tags_api(body):
    tags = body.get("tags", [])
    return with_status(set_subscription_tags(str(body.get("url", "")), [str(t) for t in tags] if isinstance(tags, list) else [str(tags)]))


def mark_red_api(body):
    try:
        age = float(body.get("age"))
    except (TypeError, ValueError):
        age = -1
    if not 0 <= age < float("inf"):
        return {"error": "age must be a number of seconds"}, 400
    marked = mark_older_red(str(body.get("url", "")), age)
    return ok(marked is not None, marked=marked, subscriptions=subscription_status())


def settings_api(body):
    mode = body.get("mode")
    error = save_settings(body.get("terms"), mode if isinstance(mode, str) else None)
    return {"ok": not error, "error": error, **settings()}, 400 if error else 200


def modes_api(body):
    """{"add": name, "copy_from": mode}, {"remove": name}, {"choose": name, or null to follow the
    calendar}, {"when": {situation: bool}, "mode": name} (see set_mode_when) or {"move": name, "to":
    position} (see move_mode)."""
    if "add" in body:
        copy_from = body.get("copy_from")
        error = add_mode(body["add"], copy_from if isinstance(copy_from, str) else None)
    elif "remove" in body:
        error = remove_mode(body["remove"])
    elif "choose" in body:
        error = choose_mode(body["choose"])
    elif "when" in body:
        error = set_mode_when(body.get("mode"), body["when"])
    elif "move" in body:
        error = move_mode(body["move"], body.get("to"))
    else:
        error = "expected add, remove, choose, when or move"
    return {"ok": not error, "error": error, **settings()}, 400 if error else 200


def refresh_api(body):
    for u in read_entries():
        schedule(u, force=True)
    for u in read_entries(FEEDS_FILE):
        schedule_subscription(u, force=True)
    return {"ok": True}, 200


def jellyfin_play_api(query):
    result = jellyfin_play(query.get("url", [""])[0])
    return result, 400 if "error" in result else 200


FEED_APIS = ("/api/feed", "/api/post", "/api/subscription")
GET_APIS = {
    "/api/subscriptions": lambda query: ({"subscriptions": subscription_status(), "interval_minutes": feed_interval / 60}, 200),
    "/api/settings": lambda query: (settings(), 200),
    "/api/jellyfin/play": jellyfin_play_api,
}
POST_APIS = {
    "/api/links": lambda body: ok(add_link(str(body.get("url", ""))), 400),
    "/api/subscriptions": subscribe_api,
    "/api/subscriptions/tags": subscription_tags_api,
    "/api/subscriptions/remove": lambda body: with_status(unsubscribe(str(body.get("url", "")))),
    "/api/subscriptions/check": lambda body: with_status(check_subscription(str(body.get("url", "")))),
    "/api/subscriptions/red": mark_red_api,
    "/api/viewed": lambda body: ok(set_viewed(str(body.get("url", "")))),
    "/api/color": lambda body: ok(set_color(str(body.get("url", "")), str(body.get("color", "")))),
    "/api/settings": settings_api,
    "/api/modes": modes_api,
    "/api/refresh": refresh_api,
}


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
    ap.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR,
                    help="where links.txt, feeds.txt and the caches are kept (default: ./data)")
    args = ap.parse_args()

    global feed_interval
    feed_interval = max(args.feed_interval, 1) * 60
    set_data_dir(args.data_dir.resolve())
    cache.update(load_json(CACHE_FILE))
    subscriptions.update(load_json(FEED_CACHE_FILE))
    migrated_credentials = migrate_caldav_url_credentials()
    if migrated_credentials:
        print(f"Moved credentials out of {FEEDS_FILE} for {migrated_credentials} CalDAV subscription(s).")
    if migrate_calendar_events() | migrate_jellyfin_images():
        save_json(FEED_CACHE_FILE, subscriptions, affects_feed=False)
    load_state()
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
    if outdated := reread_outdated_calendars():
        print(f"Reading {outdated} calendar(s) again with the current parser.")
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
