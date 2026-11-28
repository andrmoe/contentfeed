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
    last_viewed  unix time you last opened it, or None. Tags don't count as opening it.
    color        the tag you gave it on the page, or None: "green" (show again soon), "white" (maybe
                 later) or "red" (don't show again). Each only adds its configured points (see TAGS).
    next_after   for a post: the title of the post you opened most recently in its subscription,
                 if this post comes right after that one ("what's next"): the next part of its series
                 (see series.py) or else the next published; otherwise None
    series_first for a later part of a series of which you haven't opened anything before it: the
                 first part's title; otherwise None. Both are None for subscriptions tagged "latest".
    sub          for a post: its subscription's address; None for links
    sub_last_viewed  for a post: unix time you last opened any post in its subscription, or None
    series_ids   for a post: the ids of the series it's part of within its subscription (see series.py),
                 empty if none
    past_mode_change  whole minutes its estimated length would run past the next mode change (see
                 minutes_past), or None when it fits, its length is unknown or the mode isn't set to change

rank() adds one more, "similar": the item you opened recently that this one is most like, plus
"total" and "matches", the summed strength and count of every recent match (see similarity), or
None. The similar_penalty is based on the sum, not on the strongest match alone.

Nothing is hidden: items you've opened, tagged or that would run past the next mode change are scored
differently, not removed. The one cut is the post limit term: the feed shows at most that many posts
(server.py applies it; 0 gives an empty feed, such as in a mode on during busy events). Calendar
events are not feed items at all — they take place when scheduled — so subscriptions keep them apart
from their posts, and they reach the feed only by turning modes on (see server.active_mode).

A ranker registered with spread=True penalizes similarity to posts above it (see rank).

Register a ranker with @ranker("name") and select it with `server.py --ranker name`, or try it
without restarting via /api/feed?ranker=name.

Keep points stable between page updates, for example by using whole hours rather than seconds
for age, so the page doesn't redraw every time it checks for changes.
"""

import functools
import heapq
import math
import re
import threading
from typing import Callable, Iterator
from urllib.parse import urlparse

import authors

Reasons = list[tuple[str, float]]
Ranker = Callable[[dict, float, dict], Reasons]
RANKERS: dict[str, Ranker] = {}
DEFAULT = "score"

# Terms of the "score" ranker, in points. These are the defaults; you can change them in the feed's score panel, and server.py keeps your
# values in settings.json. Not every term is a weight: bonuses are flat points, and windows, counts and the event lead time are durations.
DEFAULT_TERMS = {
    "next_up_bonus": 30,  # the post after the one you opened most recently in a subscription
    "seen_bonus": -30,  # items you've opened; below 0 puts them under unopened ones
    "new_sub_bonus": 0,  # for posts from a subscription you've never opened anything from
    "similar_penalty": 0,  # added per 100% combined similarity to what you opened recently (see similarity):
    "similar_hours": 24,  # penalty fades linearly to zero this many hours after opening
    "similar_base_percent": 50,  # same-subscription baseline; other similarity fills the remainder
    "similar_series_percent": 75,  # baseline for posts in the same series; never below the subscription's
    "similar_publish_weight": 1,  # relative weight of publication-time similarity
    "similar_length_weight": 0,  # relative weight of duration similarity; 0 keeps the old behavior
    "similar_days": 30,  # posts from the same subscription are alike if published within this many days
    "series_penalty": -30,  # added to a later part of a series when you haven't opened anything before it
    "above_similarity_penalty": 0,  # added per 100% similarity to each post above it
    "event_lead_minutes": 0,  # a mode turns on this many minutes before an event it's on for
    "task_deadline_bonus": 200,  # maximum points for a task due now
    "task_deadline_days": 30,  # the deadline points ramp up from 0 this many days before the deadline
    "task_bonus": 0,  # fixed points added to every task
    "task_priority_bonus": 0,  # points for a priority-1 task, scaled down to 1/9 of them at priority 9
    "green_points": 10,  # the tags on each card (see TAGS)
    "white_points": -50,
    "red_points": -1000,
    "youtube_shorts_bonus": 0,
    "video_bonus": 0,  # for each media type (see media_type); below 0 is a penalty
    "audio_bonus": 0,
    "image_bonus": 0,
    "article_bonus": 0,
    "length_per_minute": 0,  # signed points per minute away from the target length
    "length_target_minutes": 0,  # target runtime or reading time; 0 preserves the original linear score
    "middle_author_points": 0,  # for a paper whose authors you follow are all neither first nor last author
    "middle_author_min_count": 4,  # ...when it has at least this many authors
    "age_per_year": 0,  # signed points per fractional 365-day year since publication (or addition for links)
    "past_mode_change_per_minute": -20,  # signed points per minute a post would run past the next mode change
    "post_limit": 10_000_000,  # most posts the feed shows; the maximum (LIMIT) means no limit, 0 an empty feed
}
TERMS = dict(DEFAULT_TERMS)
# Every scoring coefficient is signed; windows, similarity parameters and counts are not.
SIGNED = {
    "next_up_bonus", "seen_bonus", "new_sub_bonus", "series_penalty", "similar_penalty",
    "above_similarity_penalty", "green_points", "white_points", "red_points", "video_bonus", "image_bonus",
    "audio_bonus", "article_bonus", "youtube_shorts_bonus", "length_per_minute", "age_per_year", "middle_author_points",
    "task_deadline_bonus", "task_bonus", "task_priority_bonus", "past_mode_change_per_minute",
}
LIMIT = 10_000_000
# The tags you can give an item with the dots on its card, and their names. None of them counts as
# opening the item; each only adds its term's points, "<color>_points".
TAGS = {"green": "show again soon", "white": "maybe later", "red": "don't show again"}
# Each term's allowed values, which the score panel also gets, to check what you type.
TERM_LIMITS = {key: {"min": -LIMIT if key in SIGNED else 0,
                     "max": 100 if key in ("similar_base_percent", "similar_series_percent") else LIMIT,
                     "integer": key == "post_limit"}
               for key in DEFAULT_TERMS}


def check_terms(terms: dict) -> dict:
    """The terms as numbers, or ValueError if one is missing, unknown, out of range or not a number."""
    if set(terms) != set(DEFAULT_TERMS):
        raise ValueError(f"expected exactly these settings: {', '.join(DEFAULT_TERMS)}")
    out = {}
    for key, value in terms.items():
        low, high, integer = TERM_LIMITS[key]["min"], TERM_LIMITS[key]["max"], TERM_LIMITS[key]["integer"]
        if (isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high
                or (integer and not float(value).is_integer())):
            raise ValueError(f"{key} must be a {'whole ' if integer else ''}number from {low} to {high}")
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


def minutes_past(item: dict, now: float, at: float | None) -> int | None:
    """How many whole minutes the item would run past a time, such as the next mode change, if started
    now (rounded up, so any overrun counts), or None when it can be finished before then or at is
    None. Tasks aren't watched, and items without an estimated length can't be ruled out, so they
    always fit."""
    if at is None or item.get("calendar_kind"):
        return None
    minutes = item.get("estimated_minutes")
    if not valid_minutes(minutes):
        return None
    over = math.ceil((now + minutes * 60 - at) / 60)
    return over if over > 0 else None


class SimilarityMetric:
    """Numeric similarity shared by recent views and feed spreading: the base percent (the series
    base for posts in the same series, if higher), plus the remainder times the weighted average of
    the components known for both items (publish time and length, each 0–100%)."""
    def __init__(self, days: float, publish_weight: float, length_weight: float,
                 base_percent: float = 0, series_percent: float = 0):
        self.base_percent = base_percent
        self.series_percent = max(base_percent, series_percent)
        self.days = days
        self.date_scale = 100 / (days * 86400) if days else 0
        self.publish_weight = publish_weight
        self.length_weight = length_weight
        self.enabled = bool(self.series_percent or publish_weight + length_weight)

    def base(self, same_series: bool) -> float:
        return self.series_percent if same_series else self.base_percent

    def published(self, first_date: float, second_date: float) -> float:
        return max(0.0, 1 - abs(first_date - second_date) / 3600 / 24 / self.days) if self.days else 0.0

    def percent(self, first_date: float, first_minutes: float | None,
                second_date: float, second_minutes: float | None, same_series: bool = False) -> int:
        """Lengths are sanitized. A length unknown for either item is left out of the average."""
        weighted = total = 0.0
        if self.publish_weight:
            published = max(0.0, 100 - abs(first_date - second_date) * self.date_scale) if self.days else 0.0
            weighted, total = self.publish_weight * published, self.publish_weight
        if self.length_weight and first_minutes is not None and second_minutes is not None:
            weighted += self.length_weight * 100 * min(first_minutes, second_minutes) / max(first_minutes, second_minutes)
            total += self.length_weight
        percent = weighted / total if total else 0
        base = self.series_percent if same_series else self.base_percent
        percent = base + (1 - base / 100) * percent
        rounded = round(percent)
        # Only half-percent boundaries need extra precision stabilization.
        if abs(percent - rounded) > 0.499999999999:
            return round(round(percent, 12))
        return rounded


@functools.lru_cache(maxsize=128)
def similarity_metric(days: float, publish_weight: float, length_weight: float,
                      base_percent: float = 0, series_percent: float = 0) -> SimilarityMetric:
    return SimilarityMetric(days, publish_weight, length_weight, base_percent, series_percent)


def metric_for(terms: dict) -> SimilarityMetric:
    return similarity_metric(terms["similar_days"], terms["similar_publish_weight"], terms["similar_length_weight"],
                             terms["similar_base_percent"], terms["similar_series_percent"])


def series_of(item: dict) -> frozenset:
    return frozenset(item.get("series_ids") or ())


def same_series(item: dict, other: dict) -> bool:
    """Whether two posts of a subscription share a series."""
    return not series_of(item).isdisjoint(series_of(other))


def similarity_enabled(terms: dict) -> bool:
    return bool(terms["similar_base_percent"] or terms["similar_series_percent"] or terms["similar_publish_weight"]
                or terms["similar_length_weight"])


def known_minutes(item: dict) -> float | None:
    minutes = item.get("estimated_minutes")
    return minutes if valid_minutes(minutes) else None


def similarity_percent(item: dict, other: dict, terms: dict, metric: SimilarityMetric) -> int:
    """Whole-percent similarity: 100 to itself, 0 to items of another subscription."""
    if item["url"] == other["url"]:
        return 100
    if not item.get("sub") or item.get("sub") != other.get("sub"):
        return 0
    return metric.percent(item["date"], known_minutes(item), other["date"], known_minutes(other),
                          same_series(item, other))


# Only posts from the same subscription match; an item always matches itself. Posts in the same
# series start from the series base instead of the subscription base, when it's higher.
# Missing lengths are omitted from the normalized weighted average.
def similarity(item: dict, other: dict, terms: dict) -> tuple[int, str]:
    """Whole-percent similarity, plus the components that explain it."""
    if item["url"] == other["url"]:
        return 100, "itself"
    if not item.get("sub") or item.get("sub") != other.get("sub"):
        return 0, "different subscriptions"
    metric = metric_for(terms)
    return (similarity_percent(item, other, terms, metric),
            similarity_reason(item, other, metric, known_minutes(item), known_minutes(other)))


def similarity_breakdown(item: dict, other: dict, terms: dict) -> dict:
    """Numeric inputs for a displayed match; uses the ranking metric unchanged."""
    percent = similarity_percent(item, other, terms, metric_for(terms))
    first_minutes, second_minutes = known_minutes(item), known_minutes(other)
    components = []
    gap_days = abs(item["date"] - other["date"]) / 86400
    if terms["similar_publish_weight"]:
        value = max(0, 1 - gap_days / terms["similar_days"]) if terms["similar_days"] else 0
        components.append({"label": "Published", "percent": points(100 * value),
                           "weight": terms["similar_publish_weight"],
                           "input": f"{points(gap_days)} / {terms['similar_days']} days apart"})
    if terms["similar_length_weight"]:
        first, second = first_minutes, second_minutes
        known = first is not None and second is not None
        components.append({"label": "Length", "percent": points(100 * min(first, second) / max(first, second)) if known else None,
                           "weight": terms["similar_length_weight"],
                           "input": f"{points(min(first, second))} ÷ {points(max(first, second))} min" if known else "unknown · omitted"})
    title = other.get("title") or "Untitled"
    series = same_series(item, other) and terms["similar_series_percent"] > terms["similar_base_percent"]
    base = terms["similar_series_percent"] if series else terms["similar_base_percent"]
    return {"title": title, "url": other["url"], "percent": percent,
            "base_percent": base, "same_series": series,
            "self": item["url"] == other["url"], "components": components}


def similarity_group(item: dict) -> tuple:
    """Items only match others in their group: a calendar's tasks, a subscription's other posts, or
    a link only itself."""
    if item.get("calendar_kind"):
        return ("calendar", item.get("sub") or item["url"])
    return ("sub", item["sub"]) if item.get("sub") else ("url", item["url"])


def explain_similarities(visible: list[dict], source: list[dict], terms: dict, now: float) -> list[dict]:
    """Enrich only displayed cards, keeping pair details out of the ranking cache."""
    by_url = {item["url"]: item for item in source}
    window = terms["similar_hours"]
    recent_by_sub, recent_by_url = recent_view_index(source, now, window) if window else ({}, {})
    groups = {}
    explained = []
    for item in visible:
        out = {**item, "score_terms": dict(terms)}
        recent = item.get("similar")
        if recent:
            other = by_url.get(recent.get("url"))
            if other:
                out["recent_match"] = similarity_breakdown(item, other, terms)
            matches = []
            for other, hours, remaining, _minutes, _series in recent_candidates(item, recent_by_sub, recent_by_url):
                match = similarity_breakdown(item, other, terms)
                if match["percent"]:
                    matches.append((match["percent"] * remaining, match))
            if matches:
                out["recent_matches"] = [match for _, match in sorted(matches, key=lambda pair: pair[0], reverse=True)]
        if any(r["label"].startswith("similarity to ") for r in item["reasons"]):
            matches = []
            for other in groups.get(similarity_group(item), []):
                match = similarity_breakdown(item, other, terms)
                if match["percent"]:
                    matches.append(match)
            out["above_matches"] = matches
        if item.get("sub"):
            groups.setdefault(similarity_group(item), []).append(item)
        explained.append(out)
    return explained


def similarity_reason(item: dict, other: dict, metric: SimilarityMetric, first, second) -> str:
    """Build text only for a selected match, not every candidate comparison."""
    gap = abs(item["date"] - other["date"]) / 3600
    apart = f"published {hours_label(int(gap))} apart" if gap >= 1 else "published within the hour"
    series = same_series(item, other) and metric.series_percent > metric.base_percent
    details = ["same series" if series else "same subscription", apart]
    if metric.base(series):
        details.append(f"{points(metric.base(series))}% base")
    if metric.length_weight:
        if first is not None and second is not None:
            published = metric.published(item["date"], other["date"])
            length = min(first, second) / max(first, second)
            details.append(f"publish time {points(100 * published)}% × {points(metric.publish_weight)}, "
                           f"length {points(100 * length)}% × {points(metric.length_weight)} "
                           f"({points(first)} and {points(second)} minutes), normalized")
        else:
            details.append("length unknown; using publish time only")
    return ", ".join(details)


def recent_view_index(items: list[dict], now: float, window: float) -> tuple[dict, dict]:
    """Recently viewed items inside the window, indexed by subscription and by url, each as
    (other, hours since opened, fraction of the window remaining, minutes, series ids).
    Recency is computed once per viewed item, rather than once per comparison."""
    recent_by_sub: dict[str, list[tuple]] = {}
    recent_by_url = {}
    for other in items:
        if other["last_viewed"] is None:
            continue
        hours = hours_since(now, other["last_viewed"])
        if hours >= window:
            continue
        entry = (other, hours, 1 - hours / window, known_minutes(other), series_of(other))
        recent_by_url[other["url"]] = entry
        if other.get("sub"):
            recent_by_sub.setdefault(other["sub"], []).append(entry)
    return recent_by_sub, recent_by_url


def recent_candidates(it: dict, recent_by_sub: dict, recent_by_url: dict) -> list[tuple]:
    """The indexed items an item can match: those in its similarity group (see similarity_group),
    which for a link is only itself."""
    if not it.get("sub"):
        own = recent_by_url.get(it["url"])
        return [own] if own else []
    group = similarity_group(it)
    return [entry for entry in recent_by_sub.get(it["sub"], []) if similarity_group(entry[0]) == group]


def similar_to_recent(items: list[dict], now: float, terms: dict):
    """Set each item's "similar" to the strongest match after linear recency decay, as
    {title, hours (since opened), percent, strength, why, total, matches}, or None if all
    strengths are zero. Strength is similarity percent times the fraction of similar_hours
    remaining; total and matches sum that strength over every match, and the similar_penalty
    is based on the sum, not on the strongest match alone."""
    for it in items:
        it["similar"] = None
    window = terms["similar_hours"]
    if (not terms["similar_penalty"] or not window
            or not similarity_enabled(terms)):
        return
    metric = metric_for(terms)
    recent_by_sub, recent_by_url = recent_view_index(items, now, window)
    for it in items:
        best = None
        total = 0
        matches = 0
        first = known_minutes(it)
        first_series = series_of(it)
        for other, hours, remaining, second, second_series in recent_candidates(it, recent_by_sub, recent_by_url):
            itself = it["url"] == other["url"]
            percent = 100 if itself else metric.percent(it["date"], first, other["date"], second,
                                                        not first_series.isdisjoint(second_series))
            strength = percent * remaining
            if strength:
                total += strength
                matches += 1
                if not best or strength > best["strength"] or (strength == best["strength"] and itself):
                    # Delay explanation strings until the strongest match is known.
                    best = {"title": other["title"], "hours": hours, "percent": percent, "strength": strength,
                            "other": other, "minutes": second}
        if best:
            other, second = best.pop("other"), best.pop("minutes")
            best["url"] = other["url"]
            best["why"] = "itself" if it["url"] == other["url"] else similarity_reason(it, other, metric, first, second)
            best["total"] = total
            best["matches"] = matches
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
    """"video" for YouTube, Jellyfin, NRK TV and pages with a video file, "audio" for podcast episodes and
    other audio files, "image" for a link straight to a picture, otherwise "article"."""
    if item.get("youtube") or item.get("jellyfin") or item.get("video"):
        return "video"
    if item.get("audio"):
        return "audio"
    domain = item.get("domain") or urlparse(item["url"]).netloc
    if domain.lower().removeprefix("www.") == "tv.nrk.no":
        return "video"
    if item.get("image") and item.get("image") == item["url"]:
        return "image"
    return "article"


def middle_author(item: dict, min_count: float) -> str | None:
    """Why a paper counts as a small contribution by the authors you follow, or None: it's in the feed
    for one or more authors (see server.paper_reason), each of them is in the middle of its author list,
    and the list has at least min_count authors. An author whose place can't be told doesn't count."""
    followed = list(dict.fromkeys(r["name"] for r in item.get("via") or [] if r.get("kind") == "author"))
    places = [(name, authors.middle(item, name)) for name in followed]
    if not places or any(place is None or place[1] < min_count for _, place in places):
        return None
    return "; ".join(f"{name} is author {i} of {n}{'+' if more else ''}" for name, (i, n, more) in places)


def ranker(name: str, spread: bool = False):
    """Register a ranker. With spread, the feed is built from the top down and each item gains
    above_similarity_penalty times its summed similarity to posts above it (see rank)."""
    def register(fn: Ranker) -> Ranker:
        fn.spread = spread
        RANKERS[name] = fn
        return fn

    return register


@ranker("chronological")
def chronological(item: dict, now: float, terms: dict) -> Reasons:
    """No rules: every item scores 0, so the feed is simply newest first."""
    return []


def _similarity_reasons(item: dict, terms: dict) -> Reasons:
    """Explain the recent-view similarity adjustment shared by posts and tasks: one reason whose
    points are similar_penalty times the summed strength ("total") of all matches, not just the
    strongest one."""
    like = item.get("similar")
    if not like or not terms["similar_penalty"]:
        return []
    when = f"opened {hours_label(like['hours'])} ago" if like["hours"] else "opened in the last hour"
    if like["why"] == "itself":
        label = f"100% like itself, {when}"
    else:
        label = f"{like['percent']}% like “{short(like['title'])}”, {when} ({like['why']})"
    if like["matches"] > 1:
        label += f", {like['matches']} matches, {points(like['total'])}% combined"
    label += f", {points(100 * (1 - like['hours'] / terms['similar_hours']))}% penalty remaining"
    return [(label, points(terms["similar_penalty"] * like["total"] / 100))]


def _task_score(item: dict, now: float, terms: dict) -> Reasons:
    """Score calendar tasks under their own rules: deadline urgency, priority, similarity to recently
    opened tasks, the fixed task term and its tag. (Events aren't feed items.)"""
    due = item.get("deadline")
    reasons = []
    if due is not None:
        hours = max(0, int((due - now) // 3600))
        horizon = terms["task_deadline_days"] * 24
        if horizon and hours <= horizon:
            urgency = points(terms["task_deadline_bonus"] * (1 - hours / horizon))
            if urgency and due <= now:
                # Past the deadline the points stay at the full amount until the task is done or cancelled.
                late = hours_label(int((now - due) // 3600))
                reasons.append((f"task overdue by {late}, full deadline points until done", urgency))
            elif urgency:
                reasons.append((f"task deadline in {hours_label(hours)}", urgency))
    priority = item.get("priority")
    if priority and terms["task_priority_bonus"]:
        reasons.append((f"priority {priority}", points(terms["task_priority_bonus"] * (10 - priority) / 9)))
    reasons.extend(_similarity_reasons(item, terms))
    if terms["task_bonus"]:
        reasons.append(("task", terms["task_bonus"]))
    return reasons + _tag_reasons(item, terms)


def _tag_reasons(item: dict, terms: dict) -> Reasons:
    """The points for the item's tag, if it has one."""
    tag = item.get("color")
    if tag in TAGS and terms[f"{tag}_points"]:
        return [(f"tagged “{TAGS[tag]}”", terms[f"{tag}_points"])]
    return []


@ranker("score", spread=True)
def score(item: dict, now: float, terms: dict) -> Reasons:
    """Points for next up, opened before, a new subscription, a later part of a series, being like
    something opened recently, its media type, length, being by an author you follow in the middle of
    the author list, age, running past the next mode change, and its tag. rank() then penalizes similarity to posts above it."""
    w = terms
    reasons = []
    if item.get("calendar_kind"):
        return _task_score(item, now, w)
    if item.get("next_after") and w["next_up_bonus"]:
        reasons.append((f"next up after “{short(item['next_after'])}”", w["next_up_bonus"]))
    if item["last_viewed"] is not None and w["seen_bonus"]:
        away = hours_since(now, item["last_viewed"])
        reasons.append((f"opened {hours_label(away)} ago" if away else "opened in the last hour", w["seen_bonus"]))
    if item["kind"] == "subscription" and item.get("sub_last_viewed") is None and w["new_sub_bonus"]:
        reasons.append((f"nothing opened from “{short(item['feed'])}” yet", w["new_sub_bonus"]))
    if item.get("series_first") and w["series_penalty"]:
        reasons.append((f"in a series starting “{short(item['series_first'])}”, nothing before it opened", w["series_penalty"]))
    reasons.extend(_similarity_reasons(item, w))
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
    if w["middle_author_points"] and (label := middle_author(item, w["middle_author_min_count"])):
        reasons.append((label, w["middle_author_points"]))
    if w["age_per_year"]:
        hours = hours_since(now, item["date"])
        reasons.append((f"age {hours_label(hours)}", points(hours / (24 * 365) * w["age_per_year"])))
    past = item.get("past_mode_change")
    if past and w["past_mode_change_per_minute"]:
        reasons.append((f"runs {past} minute{'s' if past != 1 else ''} past the mode change", points(past * w["past_mode_change_per_minute"])))
    return reasons + _tag_reasons(item, w)


def order(item: dict) -> tuple:
    """Sort key for scored items, highest first with reverse=True: on equal scores, things you haven't
    opened first, then newest first, then earlier lines of links.txt."""
    return item["score"], item["last_viewed"] is None, item["date"], item["position"]


class RankedItems:
    """A resumable exact ranking. Concurrent requests can safely extend its cached prefix."""
    def __init__(self, source: list[dict], iterator: Iterator[dict]):
        self.source = source
        self.iterator = iterator
        self.items = []
        self.total = len(source)
        self.lock = threading.Lock()

    def read(self, limit: int) -> list[dict]:
        """The first limit items, extending the cached prefix as needed."""
        with self.lock:
            while len(self.items) < min(limit, self.total):
                self.items.append(next(self.iterator))
            return self.items[:limit]


def rank(name: str, items: list[dict], now: float, terms: dict | None = None,
         *, lazy: bool = False) -> list[dict] | RankedItems:
    """Return items sorted by score, each with "score" and "reasons" filled in. Weights other than the
    saved ones are for previewing them, such as while you move a slider.

    For a spread ranker, repeatedly choose the item with the highest current score, after
    adding above_similarity_penalty times its summed similarity to all items already
    placed above it. Similarity is the same normalized measure used for recent views, without
    viewing-time decay. Penalties are cumulative; different subscriptions don't match.
    """
    fn = RANKERS[name]
    terms = terms or TERMS
    items = [dict(it) for it in items]
    similar_to_recent(items, now, terms)
    scored = []
    for item in items:
        reasons = [{"label": label, "points": points} for label, points in fn(item, now, terms)]
        scored.append({**item, "score": points(sum(r["points"] for r in reasons)), "reasons": reasons})
    penalty = terms.get("above_similarity_penalty", 0) if fn.spread else 0
    metric = metric_for(terms)
    iterator = (iter(sorted(scored, key=order, reverse=True)) if not penalty or not metric.enabled
                else spread_by_similarity(scored, penalty, metric))
    return RankedItems(scored, iterator) if lazy else list(iterator)


def spread_by_similarity(scored: list[dict], penalty: float, metric: SimilarityMetric) -> Iterator[dict]:
    """Exact greedy ordering with subscription-local heap updates.

    Other subscriptions cannot affect a group's scores. Keep only each group's current best
    item in the global heap. After placing it, update the remaining items in that group and
    heapify once in C, rather than repeatedly refreshing stale entries. Any remaining item can
    overtake its former leader. Memory is linear; no pair matrix or per-pair reasons.
    """
    dates = [it["date"] for it in scored]
    minutes = [known_minutes(it) for it in scored]
    series = [series_of(it) for it in scored]
    base_cents = [round(it["score"] * 100) for it in scored]
    totals = [0] * len(scored)  # sums of whole percentages, avoiding accumulated rounding
    matches = [0] * len(scored)
    groups = {}
    for i, it in enumerate(scored):
        groups.setdefault(similarity_group(it), []).append(i)
    queues = []
    for indices in groups.values():
        queue = [(-base_cents[i], scored[i]["last_viewed"] is not None,
                  -dates[i], -scored[i]["position"], i) for i in indices]
        heapq.heapify(queue)
        queues.append(queue)
    heap = [(*queue[0], group) for group, queue in enumerate(queues)]
    heapq.heapify(heap)
    window = metric.days * 86400
    for_date_only = not (metric.series_percent or metric.length_weight)
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
        # Only check for a shared series when it could change the base.
        first_series = series[i] if metric.series_percent > metric.base_percent else None
        for position, previous in enumerate(queue):
            j = previous[-1]
            if for_date_only and abs(first_date - dates[j]) > window:
                continue
            percent = percent_for(first_date, first_minutes, dates[j], minutes[j],
                                  bool(first_series) and not first_series.isdisjoint(series[j]))
            if percent:
                totals[j] += percent
                matches[j] += 1
                queue[position] = (-(base_cents[j] + round(penalty * totals[j])), *previous[1:])
                changed = True
        if changed:
            heapq.heapify(queue)
        heapq.heappush(heap, (*queue[0], group))
