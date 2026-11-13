"""Feed ranking.

Rankers are deliberately simple and explainable: they never learn from how the feed is used.
A ranker looks at one item (with the time now and the score settings) and returns its reasons, each a (label, points) pair, such as
("tagged music", 2). An item's score is the sum of its points, the feed is sorted by score
(highest first; on ties, unopened first, then newest), and every card shows its reasons on the page.

Each item has its page metadata (url, title, description, domain, ...) plus these inputs:

    kind         "link" (from links.txt) or "subscription" (a post from a feed in feeds.txt)
    date         unix time: when a link was added, or when a post was published
    date_approx  True for older YouTube videos, whose date is only known as "3 years ago"
    added        unix time the server first saw the link or post
    tags         words written after the URL on its line in links.txt or feeds.txt, lowercased
    feed         the subscription's title (posts only)
    position     index in links.txt (0 = first line); -1 for posts
    last_viewed  unix time you last opened or skipped it (the white dot), or None
    skipped      True if the last of those was a skip
    color        "green", "white" (the default) or "red", as you marked it on the page
    next_after   for a post: the title of the post you opened (not skipped) most recently in its subscription,
                 if this post comes right after that one ("what's next"): the next part of its series
                 (see title_series) or else the next published; otherwise None
    series_first for a later part of a series of which you haven't opened anything before it: the
                 first part's title; otherwise None
    sub          for a post: its subscription's address; None for links
    sub_last_viewed  for a post: unix time you last opened or skipped any post in its subscription,
                 or None if you've done neither

rank() adds one more, "similar": the item you opened recently that this one is most like (see
similarity), or None.

Nothing is hidden: items you've opened or marked red are scored differently, not removed.

A ranker registered with spread=True penalizes similarity to posts above it (see rank).

Register a ranker with @ranker("name") and select it with `server.py --ranker name`, or try it
without restarting via /api/feed?ranker=name.

Keep points stable between page updates, for example by using whole hours rather than seconds
for age, so the page doesn't redraw every time it checks for changes.
"""

import bisect
import functools
import heapq
import math
import re
import threading
import unicodedata
from typing import Callable, Iterator
from urllib.parse import urlparse

Reasons = list[tuple[str, float]]
Ranker = Callable[[dict, float, dict], Reasons]
RANKERS: dict[str, Ranker] = {}
DEFAULT = "score"

# Settings for the "score" ranker, in points. These are the defaults; you can change them in the feed's score panel, and server.py keeps your
# values in settings.json.
DEFAULT_WEIGHTS = {
    "next_up_bonus": 30,  # the post after the one you opened most recently in a subscription
    "seen_bonus": -30,  # items you've opened or skipped; below 0 puts them under unopened ones
    "new_sub_bonus": 0,  # for posts from a subscription you've never opened anything from
    "similar_penalty": -40,  # added at 100% like something you opened recently (see similarity):
    "similar_hours": 24,  # penalty fades linearly to zero this many hours after opening
    "similar_base_percent": 50,  # same-subscription baseline; other similarity fills the remainder
    "similar_publish_weight": 1,  # relative weight of publication-time similarity
    "similar_title_weight": 0,  # relative weight of meaningful title-word overlap
    "similar_length_weight": 0,  # relative weight of duration similarity; 0 keeps the old behavior
    "similar_days": 30,  # posts from the same subscription are alike if published within this many days
    "series_penalty": -30,  # added to a later part of a series when you haven't opened anything before it
    "above_similarity_penalty": -15,  # added per 100% similarity to each post above it
    "green_bonus_points": 0,
    "youtube_shorts_bonus": 0,
    "video_bonus": 0,  # for each media type (see media_type); below 0 is a penalty
    "image_bonus": 0,
    "article_bonus": 0,
    "enrich_count": 5,  # highest-ranked incomplete posts to enrich on page load
    "length_per_minute": 0,  # signed points per minute away from the target length
    "length_target_minutes": 0,  # target runtime or reading time; 0 preserves the original linear score
    "age_per_year": 0,  # signed points per fractional 365-day year since publication (or addition for links)
}
WEIGHTS = dict(DEFAULT_WEIGHTS)
# Every scoring coefficient is signed; windows, similarity parameters and counts are not.
SIGNED = {
    "next_up_bonus", "seen_bonus", "new_sub_bonus", "series_penalty", "similar_penalty",
    "above_similarity_penalty", "green_bonus_points", "video_bonus", "image_bonus",
    "article_bonus", "youtube_shorts_bonus", "length_per_minute", "age_per_year",
}
LIMIT = 10_000_000
RED = 10_000_000  # red items sink below everything else


def check_weights(weights: dict) -> dict:
    """The weights as numbers, or ValueError if one is missing, unknown, out of range or not a number."""
    if set(weights) != set(DEFAULT_WEIGHTS):
        raise ValueError(f"expected exactly these settings: {', '.join(DEFAULT_WEIGHTS)}")
    out = {}
    for key, value in weights.items():
        low = -LIMIT if key in SIGNED else 0
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= LIMIT:
            raise ValueError(f"{key} must be a number from {low} to {LIMIT}")
        if key == "similar_base_percent" and value > 100:
            raise ValueError("similar_base_percent must be from 0 to 100")
        if key == "enrich_count" and (not float(value).is_integer() or value > 100):
            raise ValueError("enrich_count must be a whole number from 0 to 100")
        out[key] = int(value) if float(value).is_integer() else value
    return out


def hours_label(hours: int) -> str:
    """Such as "5 hours", "3 days 4 hours", "12 days", "5 months" or "3 years"."""
    days, hours = divmod(hours, 24)
    if days >= 730:
        return f"{days // 365} years"
    if days >= 60:
        return f"{days // 30} months"
    if days >= 7:
        hours = 0
    parts = [f"{n} {unit}{'s' if n != 1 else ''}" for n, unit in ((days, "day"), (hours, "hour")) if n]
    return " ".join(parts) or "0 hours"


def short(title: str | None) -> str:
    title = title if isinstance(title, str) and title else "Untitled"
    return title if len(title) <= 60 else title[:59] + "…"


def points(x: float) -> float:
    return int(x) if float(x).is_integer() else round(x, 2)


def hours_since(now: float, then: float) -> int:
    return max(0, int((now - then) // 3600))  # whole hours, so points don't change every second


def valid_minutes(value) -> bool:
    """Only positive, finite numeric estimates can contribute to length similarity."""
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0)


TITLE_STOP_WORDS = frozenset("a an the and or but of for to in on at by with from is are be as it its this that these those how what why when where who".split())


@functools.lru_cache(maxsize=50_000)
def title_tokens(title: str | None) -> frozenset[str]:
    """Unique Unicode words; keep numbers, omit common English function words."""
    if not isinstance(title, str):
        return frozenset()
    normalized = unicodedata.normalize("NFKC", title).casefold()
    return frozenset(re.findall(r"[^\W_]+", normalized)) - TITLE_STOP_WORDS


def title_fraction(first: frozenset[str], second: frozenset[str]) -> float:
    """Sørensen–Dice overlap; empty titles are missing data, not a match."""
    return 2 * len(first & second) / (len(first) + len(second)) if first and second else 0


class SimilarityMetric:
    """Numeric similarity shared by recent views and feed spreading."""
    def __init__(self, days: float, publish_weight: float, length_weight: float, title_weight: float = 0, base_percent: float = 0):
        self.base_percent = base_percent
        self.days = days
        self.date_scale = 100 / (days * 86400) if days else 0
        self.publish_weight = publish_weight
        self.length_weight = length_weight
        self.title_weight = title_weight
        total = publish_weight + length_weight
        self.enabled = bool(base_percent or total + title_weight)
        full = total + title_weight
        self.full_publish_share = publish_weight / full if full else 0
        self.full_length_share = length_weight / full if full else 0
        self.full_title_share = title_weight / full if full else 0
        no_length = publish_weight + title_weight
        self.title_publish_share = publish_weight / no_length if no_length else 0
        self.title_share = title_weight / no_length if no_length else 0
        self.publish_share = publish_weight / total if total else 0
        self.length_share = length_weight / total if total else 0

    def published(self, first_date: float, second_date: float) -> float:
        return max(0.0, 1 - abs(first_date - second_date) / 3600 / 24 / self.days) if self.days else 0.0

    def percent(self, first_date: float, first_minutes: float | None,
                second_date: float, second_minutes: float | None,
                first_title: frozenset[str] | None = None, second_title: frozenset[str] | None = None) -> int:
        """Lengths are sanitized; titles are pre-tokenized once per item."""
        published = max(0.0, 100 - abs(first_date - second_date) * self.date_scale) if self.publish_weight and self.days else 0
        has_length = self.length_weight and first_minutes is not None and second_minutes is not None
        if self.title_weight and first_title and second_title:
            title = title_fraction(first_title, second_title)
            if has_length:
                length = min(first_minutes, second_minutes) / max(first_minutes, second_minutes)
                percent = (self.full_publish_share * published + self.full_length_share * 100 * length
                           + self.full_title_share * 100 * title)
            else:
                percent = self.title_publish_share * published + self.title_share * 100 * title
        elif has_length:
            length = min(first_minutes, second_minutes) / max(first_minutes, second_minutes)
            percent = self.publish_share * published + self.length_share * 100 * length
        else:
            percent = published if self.publish_weight else 0
        percent = self.base_percent + (1 - self.base_percent / 100) * percent
        rounded = round(percent)
        # Only half-percent boundaries need extra precision stabilization.
        if abs(percent - rounded) > 0.499999999999:
            return round(round(percent, 12))
        return rounded


@functools.lru_cache(maxsize=128)
def similarity_metric(days: float, publish_weight: float, length_weight: float, title_weight: float = 0, base_percent: float = 0) -> SimilarityMetric:
    return SimilarityMetric(days, publish_weight, length_weight, title_weight, base_percent)


# Only posts from the same subscription match; an item always matches itself.
# Missing lengths and empty titles are omitted from the normalized weighted average.
def similarity(item: dict, other: dict, weights: dict) -> tuple[int, str]:
    """Whole-percent similarity, plus the components that explain it."""
    if item["url"] == other["url"]:
        return 100, "itself"
    if not item.get("sub") or item.get("sub") != other.get("sub"):
        return 0, "different subscriptions"

    metric = similarity_metric(weights["similar_days"], weights["similar_publish_weight"],
                               weights["similar_length_weight"], weights["similar_title_weight"], weights["similar_base_percent"])
    first, second = item.get("estimated_minutes"), other.get("estimated_minutes")
    first = first if valid_minutes(first) else None
    second = second if valid_minutes(second) else None
    titles = (title_tokens(item.get("title")), title_tokens(other.get("title"))) if metric.title_weight else (None, None)
    percent = metric.percent(item["date"], first, other["date"], second, *titles)
    return percent, similarity_reason(item, other, metric, first, second)


@functools.lru_cache(maxsize=4096)
def title_word_spans(title: str) -> tuple:
    """Map ranking tokens back to the original title, including Unicode expansions."""
    normalized = unicodedata.normalize("NFKC", title).casefold()
    lengths = [len(unicodedata.normalize("NFKC", title[:i]).casefold()) for i in range(len(title) + 1)]
    spans = []
    for match in re.finditer(r"[^\W_]+", normalized):
        start = bisect.bisect_right(lengths, match.start()) - 1
        end = bisect.bisect_left(lengths, match.end())
        end = bisect.bisect_right(lengths, lengths[end]) - 1
        # Keep combining marks with their original base character when normalization reorders them.
        while start > 0 and unicodedata.category(title[start]).startswith("M"):
            start -= 1
        while end < len(title) and unicodedata.category(title[end]).startswith("M"):
            end += 1
        spans.append((match.group(), start, end))
    return tuple(spans)


def highlighted_title_parts(title: str, shared: set | frozenset) -> list[dict]:
    ranges = []
    for word, start, end in title_word_spans(title):
        if word not in shared:
            continue
        if ranges and start <= ranges[-1][1]:
            ranges[-1][1] = max(end, ranges[-1][1])
        else:
            ranges.append([start, end])
    parts = []
    at = 0
    for start, end in ranges:
        if start > at:
            parts.append({"text": title[at:start], "shared": False})
        parts.append({"text": title[start:end], "shared": True})
        at = end
    if at < len(title):
        parts.append({"text": title[at:], "shared": False})
    return parts


def similarity_breakdown(item: dict, other: dict, weights: dict) -> dict:
    """Numeric inputs for a displayed match; uses the ranking metric unchanged."""
    metric = similarity_metric(weights["similar_days"], weights["similar_publish_weight"],
                               weights["similar_length_weight"], weights["similar_title_weight"], weights["similar_base_percent"])
    first_minutes, second_minutes = item.get("estimated_minutes"), other.get("estimated_minutes")
    first_minutes = first_minutes if valid_minutes(first_minutes) else None
    second_minutes = second_minutes if valid_minutes(second_minutes) else None
    first_title, second_title = title_tokens(item.get("title")), title_tokens(other.get("title"))
    if item["url"] == other["url"]:
        percent = 100
    elif item.get("sub") and item.get("sub") == other.get("sub"):
        percent = metric.percent(item["date"], first_minutes, other["date"], second_minutes, first_title, second_title)
    else:
        percent = 0
    components = []
    gap_days = abs(item["date"] - other["date"]) / 86400
    if weights["similar_publish_weight"]:
        value = max(0, 1 - gap_days / weights["similar_days"]) if weights["similar_days"] else 0
        components.append({"label": "Published", "percent": points(100 * value),
                           "weight": weights["similar_publish_weight"],
                           "input": f"{points(gap_days)} / {weights['similar_days']} days apart"})
    if weights["similar_length_weight"]:
        first, second = first_minutes, second_minutes
        known = first is not None and second is not None
        components.append({"label": "Length", "percent": points(100 * min(first, second) / max(first, second)) if known else None,
                           "weight": weights["similar_length_weight"],
                           "input": f"{points(min(first, second))} ÷ {points(max(first, second))} min" if known else "unknown · omitted"})
    if weights["similar_title_weight"]:
        first, second = first_title, second_title
        known = bool(first and second)
        components.append({"label": "Title", "percent": points(100 * title_fraction(first, second)) if known else None,
                           "weight": weights["similar_title_weight"],
                           "shared_words": sorted(first & second),
                           "input": f"2 × {len(first & second)} shared ÷ ({len(first)} + {len(second)}) words" if known else "unknown · omitted"})
    title = other.get("title") or "Untitled"
    shared = (first_title & second_title) if weights["similar_title_weight"] and weights["similar_base_percent"] < 100 else frozenset()
    return {"title": title, "url": other["url"], "percent": percent,
            "title_parts": highlighted_title_parts(title, shared) if shared else [{"text": title, "shared": False}],
            "base_percent": weights["similar_base_percent"],
            "self": item["url"] == other["url"], "components": components}


def explain_similarities(visible: list[dict], source: list[dict], weights: dict) -> list[dict]:
    """Enrich only displayed cards, keeping pair details out of the ranking cache."""
    by_url = {item["url"]: item for item in source}
    groups = {}
    explained = []
    for item in visible:
        out = {**item, "score_weights": dict(weights)}
        recent = item.get("similar")
        if recent:
            other = by_url.get(recent.get("url"))
            if other:
                out["recent_match"] = similarity_breakdown(item, other, weights)
        if any(r["label"].startswith("similarity to ") for r in item["reasons"]):
            matches = []
            for other in groups.get(item.get("sub"), []):
                match = similarity_breakdown(item, other, weights)
                if match["percent"]:
                    matches.append(match)
            out["above_matches"] = matches
        if item.get("sub"):
            groups.setdefault(item["sub"], []).append(item)
        explained.append(out)
    return explained


def similarity_reason(item: dict, other: dict, metric: SimilarityMetric, first, second) -> str:
    """Build text only for a selected match, not every candidate comparison."""
    gap = abs(item["date"] - other["date"]) / 3600
    apart = f"published {hours_label(int(gap))} apart" if gap >= 1 else "published within the hour"
    details = ["same subscription", apart]
    if metric.base_percent:
        details.append(f"{points(metric.base_percent)}% base")
    if metric.length_weight:
        if first is not None and second is not None:
            published = metric.published(item["date"], other["date"])
            length = min(first, second) / max(first, second)
            details.append(f"publish time {points(100 * published)}% × {points(metric.publish_weight)}, "
                           f"length {points(100 * length)}% × {points(metric.length_weight)} "
                           f"({points(first)} and {points(second)} minutes)" + (", normalized" if not metric.title_weight else ""))
        else:
            details.append("length unknown; using other enabled components" if metric.title_weight else "length unknown; using publish time only")
    if metric.title_weight:
        if metric.publish_weight and not (metric.length_weight and first is not None and second is not None):
            details.append(f"publish time {points(100 * metric.published(item['date'], other['date']))}% × {points(metric.publish_weight)}")
        first_title, second_title = title_tokens(item.get("title")), title_tokens(other.get("title"))
        if first_title and second_title:
            details.append(f"title {points(100 * title_fraction(first_title, second_title))}% × {points(metric.title_weight)}")
        else:
            details.append("title unknown; using other enabled components")
        details.append("normalized")
    return ", ".join(details)


def similar_to_recent(items: list[dict], now: float, weights: dict, title_words: dict | None = None):
    """Set each item's "similar" to the strongest match after linear recency decay, as
    {title, hours (since opened), percent, strength, why}, or None if all strengths are zero.
    Strength is similarity percent times the fraction of similar_hours remaining."""
    for it in items:
        it["similar"] = None
    window = weights["similar_hours"]
    if (not weights["similar_penalty"] or not window
            or not (weights["similar_base_percent"] or weights["similar_publish_weight"] or weights["similar_length_weight"] or weights["similar_title_weight"])):
        return
    metric = similarity_metric(weights["similar_days"], weights["similar_publish_weight"],
                               weights["similar_length_weight"], weights["similar_title_weight"], weights["similar_base_percent"])
    # Only posts from the same subscription can match. Links can only match themselves.
    # Compute recency once per viewed item, rather than once per comparison.
    title_words = title_words if title_words is not None else (
        {it["url"]: title_tokens(it.get("title")) for it in items} if metric.title_weight else {})
    recent_by_sub: dict[str, list[tuple]] = {}
    recent_by_url = {}
    for other in items:
        if other["last_viewed"] is None:
            continue
        hours = hours_since(now, other["last_viewed"])
        if hours >= window:
            continue
        minutes = other.get("estimated_minutes")
        entry = (other, hours, 1 - hours / window, minutes if valid_minutes(minutes) else None, title_words.get(other["url"]))
        recent_by_url[other["url"]] = entry
        if other.get("sub"):
            recent_by_sub.setdefault(other["sub"], []).append(entry)
    for it in items:
        best = None
        minutes = it.get("estimated_minutes")
        first = minutes if valid_minutes(minutes) else None
        candidates = recent_by_sub.get(it.get("sub"), [])
        own = recent_by_url.get(it["url"])
        if not it.get("sub"):
            candidates = [own] if own else []
        first_title = title_words.get(it["url"])
        for other, hours, remaining, second, second_title in candidates:
            itself = it["url"] == other["url"]
            percent = 100 if itself else metric.percent(it["date"], first, other["date"], second, first_title, second_title)
            strength = percent * remaining
            if strength and (not best or strength > best["strength"] or (strength == best["strength"] and itself)):
                # Delay explanation strings until the strongest match is known.
                best = {"title": other["title"], "hours": hours, "percent": percent, "strength": strength,
                        "skipped": other.get("skipped", False), "other": other, "minutes": second}
        if best:
            other, second = best.pop("other"), best.pop("minutes")
            best["url"] = other["url"]
            best["why"] = "itself" if it["url"] == other["url"] else similarity_reason(it, other, metric, first, second)
        it["similar"] = best


# Series: a post follows another from the same subscription when their titles have the same numbers
# except one, which is 1 higher, such as "Making a CPU, part 2" after "Making a CPU, part 1", or
# "S01E05" after "S01E04". Only the numbers are compared, not the words around them, and only between
# posts at most SERIES_NEAR apart in the subscription. Numbers of 1000 or more, such as years, don't count.
SERIES_NEAR = 10


def title_numbers(title: str) -> tuple[int, ...]:
    return tuple(n for n in map(int, re.findall(r"\d+", title)) if n < 1000)


def follows(before: tuple[int, ...], after: tuple[int, ...]) -> bool:
    """Whether numbers after come right after before: all the same but one, which is 1 higher."""
    return len(before) == len(after) and sum(a != b for a, b in zip(before, after)) == 1 and sum(after) == sum(before) + 1


def title_series(posts: list[dict]) -> dict[str, dict]:
    """For a subscription's posts, in feed order: {a post's url: the post it follows}, the nearest one if several."""
    numbers = [title_numbers(p["title"] or "") for p in posts]
    out = {}
    for i, p in enumerate(posts):
        if not numbers[i]:
            continue
        near = sorted(range(max(0, i - SERIES_NEAR), min(len(posts), i + SERIES_NEAR + 1)), key=lambda j: abs(j - i))
        for j in near:
            if follows(numbers[j], numbers[i]):
                out[p["url"]] = posts[j]
                break
    return out


def media_type(item: dict) -> str:
    """"video" for YouTube, Jellyfin, NRK TV and pages with a video file, "image" for a link straight to a picture,
    otherwise "article"."""
    if item.get("youtube") or item.get("jellyfin") or item.get("video"):
        return "video"
    domain = item.get("domain") or urlparse(item["url"]).netloc
    if domain.lower().removeprefix("www.") == "tv.nrk.no":
        return "video"
    if item.get("image") and item.get("image") == item["url"]:
        return "image"
    return "article"


def ranker(name: str, spread: bool = False):
    """Register a ranker. With spread, the feed is built from the top down and each item gains
    above_similarity_penalty times its summed similarity to posts above it (see rank)."""
    def register(fn: Ranker) -> Ranker:
        fn.spread = spread
        RANKERS[name] = fn
        return fn

    return register


@ranker("chronological")
def chronological(item: dict, now: float, weights: dict) -> Reasons:
    """No rules: every item scores 0, so the feed is simply newest first."""
    return []


@ranker("score", spread=True)
def score(item: dict, now: float, weights: dict) -> Reasons:
    """Points for next up, opened before, a new subscription, a later part of a series, being like
    something opened recently, its media type, and green. rank() then penalizes similarity to posts above it."""
    w = weights
    reasons = []
    if item.get("next_after") and "latest" not in item["tags"]:
        if w["next_up_bonus"]:
            reasons.append((f"next up after “{short(item['next_after'])}”", w["next_up_bonus"]))
    if item["last_viewed"] is not None and w["seen_bonus"]:
        away = hours_since(now, item["last_viewed"])
        verb = "skipped" if item.get("skipped") else "opened"
        reasons.append((f"{verb} {hours_label(away)} ago" if away else f"{verb} in the last hour", w["seen_bonus"]))
    if item["kind"] == "subscription" and item.get("sub_last_viewed") is None and w["new_sub_bonus"]:
        reasons.append((f"nothing opened from “{short(item['feed'])}” yet", w["new_sub_bonus"]))
    if item.get("series_first") and "latest" not in item["tags"] and w["series_penalty"]:
        reasons.append((f"in a series starting “{short(item['series_first'])}”, nothing before it opened", w["series_penalty"]))
    like = item.get("similar")
    if like and w["similar_penalty"]:
        verb = "skipped" if like.get("skipped") else "opened"
        when = f"{verb} {hours_label(like['hours'])} ago" if like["hours"] else f"{verb} in the last hour"
        if like["why"] == "itself":
            label = f"100% like itself, {when}"
        else:
            label = f"{like['percent']}% like “{short(like['title'])}”, {when} ({like['why']})"
        label += f", {points(100 * (1 - like['hours'] / w['similar_hours']))}% penalty remaining"
        reasons.append((label, points(w["similar_penalty"] * like["strength"] / 100)))
    kind = media_type(item)
    if w[f"{kind}_bonus"]:
        reasons.append((kind, w[f"{kind}_bonus"]))
    short_url = urlparse(item["url"])
    if (short_url.hostname or "").lower().removeprefix("www.").removeprefix("m.") == "youtube.com" and re.fullmatch(r"/shorts/[\w-]+/?", short_url.path) and w["youtube_shorts_bonus"]:
        reasons.append(("YouTube Shorts", w["youtube_shorts_bonus"]))
    minutes = item.get("estimated_minutes")
    if w["length_per_minute"] and valid_minutes(minutes):
        distance = abs(minutes - w["length_target_minutes"])
        target = points(w["length_target_minutes"])
        reasons.append((f"estimated {points(minutes)} minutes, {points(distance)} minutes from target {target}",
                        points(distance * w["length_per_minute"])))
    if w["age_per_year"]:
        hours = hours_since(now, item["date"])
        reasons.append((f"age {hours_label(hours)}", points(hours / (24 * 365) * w["age_per_year"])))
    if item["color"] == "green" and w["green_bonus_points"]:
        reasons.append(("green", w["green_bonus_points"]))
    elif item["color"] == "red":
        reasons.append(("red", -RED))
    return reasons


class RankedItems:
    """A resumable exact ranking. Concurrent requests can safely extend its cached prefix."""
    def __init__(self, source: list[dict], iterator: Iterator[dict]):
        self.source = source
        self.iterator = iterator
        self.items = []
        self.total = len(source)
        self.lock = threading.Lock()

    def read(self, limit: int, extra_count: int = 0, match_key=None) -> tuple[list[dict], list[dict]]:
        """Extend to the page size and, optionally, the highest-ranked eligible matches.

        Count eligible source keys first so a page with no missing metadata never consumes
        the whole ranking just to discover there is nothing to enrich.
        """
        with self.lock:
            matched = set()
            target = 0
            if extra_count and match_key:
                possible = {key for it in self.source if (key := match_key(it)) is not None}
                target = min(extra_count, len(possible))
                matched = {key for it in self.items if (key := match_key(it)) is not None}
            while len(self.items) < min(limit, self.total) or len(matched) < target:
                it = next(self.iterator)
                self.items.append(it)
                if target and (key := match_key(it)) is not None:
                    matched.add(key)
            return self.items[:limit], list(self.items) if extra_count else []


def rank(name: str, items: list[dict], now: float, weights: dict | None = None,
         *, lazy: bool = False) -> list[dict] | RankedItems:
    """Return items sorted by score, each with "score" and "reasons" filled in. Weights other than the
    saved ones are for previewing them, such as while you move a slider.

    For a spread ranker, repeatedly choose the item with the highest current score, after
    adding above_similarity_penalty times its summed similarity to all items already
    placed above it. Similarity is the same normalized measure used for recent views, without
    viewing-time decay. Penalties are cumulative; different subscriptions don't match.
    """
    fn = RANKERS[name]
    weights = weights or WEIGHTS
    items = [dict(it) for it in items]
    title_words = {it["url"]: title_tokens(it.get("title")) for it in items} if weights["similar_title_weight"] else {}
    similar_to_recent(items, now, weights, title_words)
    scored = []
    for item in items:
        reasons = [{"label": label, "points": points} for label, points in fn(item, now, weights)]
        scored.append({**item, "score": points(sum(r["points"] for r in reasons)), "reasons": reasons})
    # On equal scores: things you haven't opened first, then newest first
    order = lambda it: (it["score"], it["last_viewed"] is None, it["date"], it["position"])
    penalty = weights.get("above_similarity_penalty", 0) if fn.spread else 0
    metric = similarity_metric(weights["similar_days"], weights["similar_publish_weight"],
                               weights["similar_length_weight"], weights["similar_title_weight"], weights["similar_base_percent"])
    iterator = (iter(sorted(scored, key=order, reverse=True)) if not penalty or not metric.enabled
                else spread_by_similarity(scored, penalty, metric, title_words))
    return RankedItems(scored, iterator) if lazy else list(iterator)


def spread_by_similarity(scored: list[dict], penalty: float, metric: SimilarityMetric, title_words: dict | None = None) -> Iterator[dict]:
    """Exact greedy ordering with subscription-local heap updates.

    Other subscriptions cannot affect a group's scores. Keep only each group's current best
    item in the global heap. After placing it, update the remaining items in that group and
    heapify once in C, rather than repeatedly refreshing stale entries. Any remaining item can
    overtake its former leader. Memory is linear; no pair matrix or per-pair reasons.
    """
    dates = [it["date"] for it in scored]
    minutes = [it.get("estimated_minutes") for it in scored]
    minutes = [value if valid_minutes(value) else None for value in minutes]
    title_words = title_words or {}
    titles = [title_words.get(it["url"]) for it in scored]
    base_cents = [round(it["score"] * 100) for it in scored]
    totals = [0] * len(scored)  # sums of whole percentages, avoiding accumulated rounding
    matches = [0] * len(scored)
    groups = {}
    for i, it in enumerate(scored):
        key = ("sub", it["sub"]) if it.get("sub") else ("url", it["url"])
        groups.setdefault(key, []).append(i)
    queues = []
    for indices in groups.values():
        queue = [(-base_cents[i], scored[i]["last_viewed"] is not None,
                  -dates[i], -scored[i]["position"], i) for i in indices]
        heapq.heapify(queue)
        queues.append(queue)
    heap = [(*queue[0], group) for group, queue in enumerate(queues)]
    heapq.heapify(heap)
    window = metric.days * 86400
    for_date_only = not (metric.base_percent or metric.length_weight or metric.title_weight)
    percent_for = metric.percent
    while heap:
        *_, i, group = heapq.heappop(heap)
        queue = queues[group]
        heapq.heappop(queue)
        it = scored[i]
        if totals[i]:
            # Round only the cumulative adjustment to cents, never each pair.
            cents = round(penalty * totals[i])
            label = f"similarity to {matches[i]} post{'s' if matches[i] != 1 else ''} above, {totals[i]}% total"
            it["reasons"].append({"label": label, "points": points(cents / 100)})
            it["score"] = points((base_cents[i] + cents) / 100)
        yield it
        if not queue:
            continue
        changed = False
        first_date, first_minutes = dates[i], minutes[i]
        for position, previous in enumerate(queue):
            j = previous[-1]
            if for_date_only and abs(first_date - dates[j]) > window:
                continue
            percent = percent_for(first_date, first_minutes, dates[j], minutes[j], titles[i], titles[j])
            if percent:
                totals[j] += percent
                matches[j] += 1
                queue[position] = (-(base_cents[j] + round(penalty * totals[j])), *previous[1:])
                changed = True
        if changed:
            heapq.heapify(queue)
        heapq.heappush(heap, (*queue[0], group))
