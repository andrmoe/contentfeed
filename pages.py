"""Subscriptions to pages without a feed, such as a company's blog or news page: its posts, read from the page.

The posts are the page's links to pages under it: on https://example.com/blog, links to
https://example.com/blog/some-post, at the depth most of them are (so not /blog/team/someone). If it has fewer than MIN_POSTS of those, they are the links to the
folder on the site it links to most, such as /post/some-post on a /research page, or else to the folder
on another site it links to most, such as arxiv.org/abs/… on a page listing papers. Links in the page's
<nav>, <header> and <footer>, to tags, categories and authors, and to social media, don't count.
A page whose links are all added by JavaScript has none of these; then the posts are the pages under it
in the site's sitemap.

A page listing audio files, such as an archive's table of recordings, has them as its posts when it links to
more of them than to any folder of pages: each one plays in its card, titled and dated from the text around
its link (its table row or list item), since such links are usually just "Download".

Each new post's own page is read once, for its title, description, picture, video and date, the way a link in
links.txt is. Where several posts' pages have the same title, such as a series' name, they keep the title
from the list instead, and a description several share, such as the site's, is left out. A post whose
page has a <video> plays in its card. The date is the first of: the one in the page's metadata, the one
by its link in the list, and the first date written in the post. A post with none is dated by when the
feed first saw it.
"""

import re
import time
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from content_length import page_minutes

from feeds import MAX_FEED_BYTES, FeedError, MetaParser, html_to_text, is_audio_url, local, open_url, parse_date

MIN_POSTS = 2
MAX_POSTS = 200  # read from the page; some list a whole series at once, oldest first
SKIPPED_TAGS = {"nav", "header", "footer"}
# Paths to lists rather than posts, such as /blog/category/news or /blog/page/2
LIST_PARTS = {"tag", "tags", "category", "categories", "topic", "topics", "author", "authors", "page", "search", "feed"}
NOT_PAGES = re.compile(r"\.(pdf|png|jpe?g|gif|svg|webp|mp4|zip|xml|json|css|js)$", re.I)
SOCIAL = {"x.com", "twitter.com", "linkedin.com", "facebook.com", "instagram.com", "youtube.com", "github.com",
          "bsky.app", "threads.net", "tiktok.com", "discord.gg", "t.me", "forms.gle", "docs.google.com"}
MONTH_NAMES = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
MONTH = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
DATES = [  # (pattern, order of year, month and day in it)
    (re.compile(r"\b(\d{4})[-/](\d{2})[-/](\d{2})\b"), "ymd"),
    (re.compile(rf"\b{MONTH}\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", re.I), "mdy"),
    (re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+{MONTH},?\s+(\d{{4}})\b", re.I), "dmy"),
]
# Metadata that gives a page's date, most telling first
DATE_META = ("article:published_time", "datepublished", "citation_publication_date", "citation_online_date",
             "citation_date", "dc.date", "date")
# What separates a page's title from the site's name, as in "Some post | Example"
TITLE_SUFFIX = re.compile(r"\s+[|–—·•\\-]\s+[^|–—·•\\]{1,60}$")
# Link text that isn't a title
GENERIC = {"read more", "learn more", "continue reading", "read", "more", "read the post", "read article", "view", "see more"}
# What holds a link and the text that goes with it, such as an audio file's row in a table
ROWS = {"tr", "li", "article"}
CELLS = {"td", "th", "p", "div", "span", "h1", "h2", "h3", "h4", "h5", "h6", "dt", "dd"}
# A cell that is just a date, as archives write them: 1924/06/26, 1924-06, or 1920
CELL_DATE = re.compile(r"(\d{4})(?:[-/.](\d{1,2}))?(?:[-/.](\d{1,2}))?")
# A length written in brackets, as in "(4 min.)", "(13 min 42 sec)", "(Total program 36 min)" or "(25 secs)",
# also where the closing bracket is missing at the end
LENGTH = re.compile(r"\([^()]*?\b(?:(\d+)\s*min(?:ute)?s?\.?\s*)?(?:(\d+)\s*sec(?:ond)?s?\.?)?\s*(?:\)|$)", re.I)
SHORT_TITLE = 120
WORKERS = 6  # posts' pages read at the same time
MAX_SITEMAPS = 5  # read from a sitemap index


class Links(HTMLParser):
    """The page's links outside <nav>, <header> and <footer>, in order, with each one's text, headings,
    picture and <time datetime>, and the table row or list item it's in, as its cells' texts. An <audio>
    file counts as a link too."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[dict] = []
        self.skipping = 0
        self.open: dict | None = None
        self.heading = 0
        self.rows: list[dict] = []  # the rows the parser is in, innermost last
        self.in_audio = False

    def link(self, href: str) -> dict:
        row = self.rows[-1] if self.rows else None
        return {"href": href, "text": [], "headings": [], "image": None, "time": None, "row": row}

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag in ROWS:
            self.rows.append({"cells": [""]})
        elif (tag in CELLS or tag in ("a", "br")) and self.rows:
            self.rows[-1]["cells"].append("")
        if tag == "audio":
            self.in_audio = True
        if tag in SKIPPED_TAGS:
            self.skipping += 1
        elif tag == "a" and a.get("href") and not self.skipping:
            self.open = self.link(a["href"])
            self.links.append(self.open)
            self.heading = 0
        elif tag in ("audio", "source") and self.in_audio and a.get("src") and not self.skipping:
            self.links.append(self.link(a["src"]))
        elif self.open:
            if tag == "img" and not self.open["image"]:
                self.open["image"] = a.get("src") or (a.get("srcset") or "").split(" ")[0] or None
            elif tag == "time" and a.get("datetime"):
                self.open["time"] = a["datetime"]
            elif re.fullmatch(r"h[1-6]", tag):
                self.heading += 1
                self.open["headings"].append("")
            if tag in ("p", "div", "br", "span", "li", "time") or re.fullmatch(r"h[1-6]", tag):
                self.open["text"].append(" ")

    def handle_endtag(self, tag):
        if tag in ROWS and self.rows:
            self.rows.pop()
        elif (tag in CELLS or tag == "a") and self.rows:
            self.rows[-1]["cells"].append("")
        if tag == "audio":
            self.in_audio = False
        if tag in SKIPPED_TAGS:
            self.skipping = max(0, self.skipping - 1)
        elif tag == "a":
            self.open = None
        elif re.fullmatch(r"h[1-6]", tag):
            self.heading = max(0, self.heading - 1)
            if self.open:
                self.open["text"].append(" ")

    def handle_data(self, data):
        if self.rows and not self.skipping and not self.in_audio:  # not "Your browser can't play this"
            self.rows[-1]["cells"][-1] += data
        if self.open:
            self.open["text"].append(data)
            if self.heading and self.open["headings"]:
                self.open["headings"][-1] += data


def site(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def folder(url: str) -> str:
    """The site and folder of a page, such as example.com/blog for https://www.example.com/blog/post."""
    return site(url) + urlparse(url).path.rstrip("/").rsplit("/", 1)[0]


def is_post(url: str, page_url: str) -> bool:
    p = urlparse(url)
    parts = [part for part in p.path.lower().split("/") if part]
    return (p.scheme in ("http", "https") and bool(parts) and not NOT_PAGES.search(p.path)
            and not LIST_PARTS & set(parts) and site(url) not in SOCIAL
            and (site(url), p.path.rstrip("/")) != (site(page_url), urlparse(page_url).path.rstrip("/")))


def depth(url: str) -> int:
    return len([part for part in urlparse(url).path.split("/") if part])


def under(url: str, page_url: str) -> bool:
    """Whether url is a page under page_url, such as example.com/blog/post under example.com/blog."""
    path = urlparse(page_url).path.rstrip("/") + "/"
    return site(url) == site(page_url) and path != "/" and urlparse(url).path.startswith(path)


def post_links(links: list[dict], page_url: str) -> dict[str, list[dict]]:
    """The links to posts, as {post url: every link to it}, in the order they're on the page."""
    found: dict[str, list[dict]] = {}
    audio: dict[str, list[dict]] = {}
    for link in links:
        url = urljoin(page_url, link["href"]).split("#")[0]
        if urlparse(url).scheme in ("http", "https") and is_audio_url(url):
            audio.setdefault(url, []).append(link)
        elif is_post(url, page_url):
            found.setdefault(url, []).append(link)
    posts = {u: ls for u, ls in found.items() if under(u, page_url)}
    if len(posts) >= MIN_POSTS:
        # Posts are all as deep as each other, such as /research/some-post, unlike /research/team/alignment
        depths = [depth(u) for u in posts]
        usual = max(set(depths), key=depths.count)
        return {u: ls for u, ls in posts.items() if depth(u) == usual}
    folders = Counter(folder(u) for u in found if folder(u) != site(u))  # not at the top, like /about
    if len(audio) >= MIN_POSTS and len(audio) >= max(folders.values(), default=0):
        return audio
    for same_site in (True, False):
        folders: dict[str, int] = {}
        for u in found:
            if (site(u) == site(page_url)) == same_site and folder(u) != site(u):  # not at the top, like /about
                folders[folder(u)] = folders.get(folder(u), 0) + 1
        if folders and max(folders.values()) >= MIN_POSTS:
            top = max(folders, key=folders.get)
            return {u: ls for u, ls in found.items() if folder(u) == top}
    return {}


def sitemap_posts(page_url: str) -> dict[str, dict]:
    """The pages under page_url in the site's sitemap, as {url: {"lastmod": date or None}}."""
    root = urljoin(page_url, "/")
    try:
        with open_url(urljoin(root, "/robots.txt"), "text/plain") as r:
            robots = r.read(MAX_FEED_BYTES).decode("utf-8", errors="replace")
        todo = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", robots)
    except OSError:
        todo = []
    todo, done, posts = todo or [urljoin(root, "/sitemap.xml")], 0, {}
    while todo and done < MAX_SITEMAPS:
        done += 1
        try:
            with open_url(todo.pop(0), "application/xml, text/xml") as r:
                tree = ET.fromstring(r.read(MAX_FEED_BYTES))
        except (OSError, ET.ParseError):
            continue
        for entry in tree:
            loc = next((c.text.strip() for c in entry if local(c.tag) == "loc" and c.text), None)
            lastmod = next((c.text.strip() for c in entry if local(c.tag) == "lastmod" and c.text), None)
            if not loc:
                continue
            if local(entry.tag) == "sitemap":  # a sitemap index lists more sitemaps
                todo.append(loc)
            elif under(loc, page_url) and is_post(loc, page_url):
                posts[loc] = {"lastmod": lastmod}
    return posts


def text_date(text: str) -> float | None:
    """The first date written in the text, such as "March 5, 2026", "5 Mar 2026" or "2026-03-05"."""
    found = []
    for pattern, order in DATES:
        for m in pattern.finditer(text or ""):
            parts = dict(zip(order, m.groups()))
            month = parts["m"] if parts["m"].isdigit() else MONTH_NAMES.index(parts["m"][:3].lower()) + 1
            try:
                day = datetime(int(parts["y"]), int(month), int(parts["d"]), 12, tzinfo=timezone.utc)
            except ValueError:  # such as 2026-13-45
                continue
            found.append((m.start(), day.timestamp()))
            break  # later matches of this pattern come after this one
    return min(found)[1] if found else None


def json_ld_date(blocks: list[str]) -> str | None:
    for block in blocks:
        m = re.search(r'"datePublished"\s*:\s*"([^"]+)"', block)
        if m:
            return m.group(1)
    return None


def page_date(value: str | None) -> float | None:
    """A date from a page's metadata: ISO 8601 (2026-03-05T10:00:00Z), RFC 822, or written out."""
    if not value:
        return None
    return parse_date(value.replace("Z", "+00:00")) or text_date(value)


def cell_date(text: str) -> float | None:
    """The date in a cell that holds only a date, such as 1924/06/26 or 1920 (as its first day)."""
    m = CELL_DATE.fullmatch(text.strip())
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2) or 1), int(m.group(3) or 1), tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def audio_card(url: str, links: list[dict], page_url: str) -> dict:
    """An audio file's post, from the row its link is in: titled with the row's longest text, with all of
    that text as the description when the title had to be shortened, dated by a cell that is a date and
    with a length written as "(4 min.)"."""
    link_texts = {" ".join("".join(l["text"]).split()) for l in links}
    cells = [" ".join(c.split()) for l in links if l["row"] for c in l["row"]["cells"]]
    cells = [c for c in cells if c and c not in link_texts]
    text = max(cells, key=len, default="") or max(link_texts, key=len, default="")
    if text.lower().strip(" .…→›»") in GENERIC | {"download", "download audio file", "listen", "play", "audio"}:
        text = ""
    title = text
    if len(title) > SHORT_TITLE:
        end = title.find(". ", 20, SHORT_TITLE)
        title = title[:end + 1] if end > 0 else title[:SHORT_TITLE].rsplit(" ", 1)[0] + "…"
    name = urlparse(url).path.rstrip("/")
    name = next((p for p in reversed(name.split("/")) if is_audio_url("/" + p)), name.rsplit("/", 1)[-1])
    length = next((m for m in LENGTH.finditer(text) if m.group(1) or m.group(2)), None)
    return {
        "url": url,
        "title": title or name,
        "description": text if title != text else "",
        "image": None,
        "audio": url,
        "published": next((d for d in map(cell_date, cells) if d is not None), None) or text_date(" ".join(cells)),
        "estimated_minutes": int(length.group(1) or 0) + int(length.group(2) or 0) / 60 if length else None,
    }


def card(url: str, links: list[dict], page_url: str) -> dict:
    """What the list says about a post, from its links: their title, picture and date."""
    if is_audio_url(url):
        return audio_card(url, links, page_url)
    texts = [" ".join("".join(l["text"]).split()) for l in links]
    headings = [" ".join(h.split()) for l in links for h in l["headings"]]
    headings = [h for h in headings if h.lower().strip(" .…→›»") not in GENERIC]
    image = next((l["image"] for l in links if l["image"]), None)
    when = next((l["time"] for l in links if l["time"]), None)
    return {
        "url": url,
        "title": next((h for h in headings if h), None) or max(texts, key=len, default="") or url,
        "image": urljoin(page_url, image) if image else None,
        "published": page_date(when) or next((d for d in map(text_date, texts) if d), None),
    }


def read_post(post: dict) -> dict:
    """The post with its own page's title, description, picture and date, where it has them. An audio
    file has no page, so it keeps what the list said."""
    if post.get("audio"):
        return {"youtube": None, **post}
    item = {"url": post["url"], "title": post["title"][:300], "list_title": post["title"][:300], "description": "",
            "image": post["image"], "youtube": None, "published": post["published"]}
    try:
        with open_url(post["url"], "text/html") as r:
            if "html" not in r.headers.get_content_type():
                return item
            parser = MetaParser()
            parser.feed(r.read(MAX_FEED_BYTES).decode(r.headers.get_content_charset() or "utf-8", errors="replace"))
    except (OSError, ValueError):  # keep what the list said
        return item
    item["estimated_minutes"] = page_minutes(parser)
    m = parser.meta
    title = m.get("og:title") or m.get("twitter:title") or m.get("citation_title") or parser.title.strip()
    image = m.get("og:image") or m.get("og:image:url") or m.get("twitter:image")
    description = html_to_text(m.get("og:description") or m.get("twitter:description") or m.get("description") or "")[0]
    meta_date = next((m[k] for k in DATE_META if m.get(k)), None) or json_ld_date(parser.json_ld) or parser.time
    item.update(
        title=html_to_text(title)[0][:300] or item["title"],
        description=description[:500],
        image=urljoin(post["url"], image) if image else item["image"],
        published=(page_date(meta_date) or item["published"] or text_date(" ".join(parser.text))
                   or text_date(description)),
        site_name=m.get("og:site_name"),
    )
    if parser.video and not parser.video.startswith("blob:"):
        item["video"] = urljoin(post["url"], parser.video)
    return item


def without_shared(items: list[dict]):
    """Posts whose pages share a title, such as each episode's page being titled with the series' name,
    get the title the list gives them instead. A description they share, such as the site's, is dropped."""
    titles = Counter(it["title"] for it in items)
    descriptions = Counter(it["description"] for it in items)
    for it in items:
        list_title = it.pop("list_title")
        if titles[it["title"]] > 1 and list_title != it["url"]:
            it["title"] = list_title
        if descriptions[it["description"]] > 1:
            it["description"] = ""


def without_site_name(items: list[dict]):
    """Remove the site's name from the end of titles, as in "Some post | Example": the og:site_name, or
    an ending that all the posts' titles share."""
    endings = [TITLE_SUFFIX.search(it["title"]) for it in items]
    shared = {e.group() for e in endings if e}
    shared = shared.pop() if len(items) >= MIN_POSTS and len(shared) == 1 and all(endings) else None
    for it, ending in zip(items, endings):
        name = it.pop("site_name", None)
        if ending and (ending.group() == shared or (name and ending.group().split(None, 1)[-1].strip() == name.strip())):
            it["title"] = it["title"][:ending.start()]


def fetch(url: str, known: set[str] = frozenset()) -> dict:
    """The posts on a page without a feed, like feeds.fetch(). Only posts not in known (urls the feed
    already has) are read from their own pages; the others are left out, and the feed keeps them."""
    try:
        with open_url(url, "text/html") as r:
            final_url = r.geturl()
            charset = r.headers.get_content_charset() or "utf-8"
            text = r.read(MAX_FEED_BYTES).decode(charset, errors="replace")
    except OSError as e:
        raise FeedError(f"couldn't fetch {url}: {e}") from e
    meta, links = MetaParser(), Links()
    meta.feed(text)
    links.feed(text)
    found = post_links(links.links, final_url)
    if found:
        cards = [card(u, ls, final_url) for u, ls in found.items()]
    else:
        cards = [{"url": u, "title": u, "image": None, "published": page_date(s["lastmod"])}
                 for u, s in sitemap_posts(final_url).items()]
        cards.sort(key=lambda c: c["published"] or 0, reverse=True)
    if not cards:
        raise FeedError(f"no RSS or Atom feed found at {url}, and no posts on the page or under it in the "
                        "site's sitemap. The page may add its links with JavaScript, which the feed doesn't run.")
    # Audio files aren't read from their own pages, so all of them are kept.
    new, pages = [], 0
    for c in cards:
        pages += not c.get("audio")
        if (c.get("audio") or pages <= MAX_POSTS) and c["url"] not in known:
            new.append(c)
    with ThreadPoolExecutor(WORKERS) as pool:
        items = list(pool.map(read_post, new))
    read = [it for it in items if not it.get("audio")]
    without_shared(read)
    without_site_name(read)
    title = html_to_text(meta.title.strip() or meta.meta.get("og:site_name") or "")[0]
    return {
        "title": title or site(final_url),
        "site": final_url,
        "icon": urljoin(final_url, meta.icon or "/favicon.ico"),
        "items": items,
        "fetched": time.time(),
        "feed_url": final_url,
        "page": True,
    }
