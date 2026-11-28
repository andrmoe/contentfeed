# Content Feed

Turns a plain list of links and feeds (RSS, Atom, YouTube channels, subreddits, blogs…) into a browsable card feed (title, description, preview image,
inline YouTube embeds), served to your local network. Python 3, with the packages in
`requirements.txt` (`icalendar` and `recurring-ical-events`, for calendars) installed with pip.

## Run

Install the requirements into a virtual environment in the checkout once (on Debian/Ubuntu,
`apt install python3-venv` first), and again whenever `requirements.txt` changes:

    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt

Then:

    .venv/bin/python server.py --port 8090         # development; keeps data in ./data
    .venv/bin/python server.py --port 8090 --data-dir /some/other/dir

The default port is 80, so the service is reachable at plain `http://<lan-ip>`. Ports below
1024 need root or `CAP_NET_BIND_SERVICE`; the systemd service grants just that permission, so it
doesn't run as root. For development, pick a port of 1024 or above as shown, and open the
printed address from any device on the network.

## Individual posts

Each card's **Post & related** link opens `/post?url=…`, showing the same card and controls,
a feed for every known series it belongs to, and a feed of similar posts. Opening this page
does not mark the post viewed; opening its source or playing it does.

Series use imported YouTube playlist order, Jellyfin/NRK episode order, or the existing
numbered-title detection. Overlapping playlists each get their own feed. Similar posts come
from the same subscription and use your current similarity settings, with the strongest
matches first and the selected post excluded. **Show more related posts** expands the feeds.
Cards here show scores before the main feed's adjustments for similarity to posts above them.

## Tests

Run the regression tests and Python syntax checks:

    .venv/bin/python -m unittest -v
    .venv/bin/python -m py_compile *.py

For the 50-subscription, 10,000-item ranking benchmark and measured tradeoffs, see
[PERFORMANCE.md](PERFORMANCE.md).

## Your data

Everything personal is kept in the data directory, never in the code. Where that is depends on
how you run the feed:

- as the systemd service (see Run as a service): `/var/lib/contentfeed`
- with `.venv/bin/python server.py`: `data/` next to `server.py` (git-ignored), or the folder given with `--data-dir`

It holds:

- `links.txt` and `feeds.txt`: your links and subscriptions (created with instructions on first run)
- `state.json`: for each link or post, when it was first seen, when you last opened it, and the tag
  you gave it (see Tags). Older versions kept these in `added.json`, `viewed.json`, `skipped.json`
  and `colors.json`; they're moved into `state.json` at startup and the old files kept as `.bak`.
  Older versions also recorded skips (the old white dot); at startup, an untagged item whose last
  skip came after you last opened it is tagged "maybe later" instead.
- `settings.json`: your modes and their score settings from the feed's score panel, if you changed them
- `jellyfin.json`: logins for Jellyfin servers you subscribe to (see Jellyfin), if any
- `caldav.json`: logins for private CalDAV calendars you subscribe to, if any
- `cache.json` and `feeds.json`: fetched page details and subscription posts

To back up or move your feed, copy this directory.

## Run as a service

`deploy/contentfeed.service` runs the feed with systemd, for example in its own LXC container or
VM. The code is a git checkout at `/opt/contentfeed` owned by root, which the service can only
read. The data lives in `/var/lib/contentfeed`, which systemd creates and which only the
`contentfeed` user can read. As root in the container (Debian/Ubuntu shown):

    apt install python3 python3-venv git
    useradd --system --no-create-home --home-dir /var/lib/contentfeed --shell /usr/sbin/nologin contentfeed
    git clone https://github.com/andrmoe/contentfeed.git /opt/contentfeed
    python3 -m venv /opt/contentfeed/.venv
    /opt/contentfeed/.venv/bin/pip install -r /opt/contentfeed/requirements.txt
    cp /opt/contentfeed/deploy/contentfeed.service /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable --now contentfeed

The container needs an address on your LAN (e.g. a bridged network) for other devices to reach
port 80. Logs: `journalctl -u contentfeed -f`; they include the addresses the server fetches.
The displayed app version is `VERSION`'s major.minor followed by the number of commits, with the
latest commit's short hash next to it, when the code is a git checkout (a full clone, as above);
otherwise it's `VERSION` alone.

To update:

    git -C /opt/contentfeed pull
    /opt/contentfeed/.venv/bin/pip install -r /opt/contentfeed/requirements.txt
    systemctl restart contentfeed

If the service file changed, copy it to `/etc/systemd/system/` again and run
`systemctl daemon-reload` before restarting.

To edit your lists by hand, edit `/var/lib/contentfeed/links.txt` or `feeds.txt` as root; the
running server picks up changes within about 30 seconds.

If the service fails to start with `status=226/NAMESPACE`, the container doesn't allow systemd's
sandboxing; comment out the sandboxing block in the unit file.

## Links and subscriptions

In the data directory, `links.txt` holds single links and `feeds.txt` holds subscriptions. In both, each line is a URL
optionally followed by space-separated tags (`https://example.com music longread`), and lines
starting with `#` are comments.

A subscription can be an RSS or Atom feed, a CalDAV calendar collection or shared `.ics` link, or a page that has one: a YouTube channel
(`youtube.com/@handle`, `/channel/…`) or playlist, a subreddit, a blog, a Mastodon profile and so on.
A podcast's feed, or a page listing audio files, plays its episodes in the cards (see Podcasts and audio).
It can also be a blog or news page without a feed (see Pages without a feed), a series on NRK TV
(see NRK TV), an author on arXiv or Google Scholar (see Authors), the papers citing a paper (see Citations)
or a Jellyfin server (see Jellyfin).
Incomplete tasks appear as cards; events don't — they take place when scheduled, not whenever the
feed happens to show them, so they only turn modes on (see Modes), such as a mode with a post limit
of 0 during busy events to stop watching for meetings. The score panel
controls task urgency: its points ramp up over the days before a deadline (30 by default). An overdue
task keeps the full deadline points, its card says how long overdue it is, and it stays in the feed
until it's completed or cancelled in the calendar. Tasks also get the
similar-posts-above penalty against other tasks in the same calendar, and the fixed Task term
adds points to every task (default 0). The Task priority term gives a task points by its
calendar priority: all of them at priority 1 (highest), down to a ninth of them at 9 (lowest),
and none without a priority (default 0). Paste each calendar's own URL into the Subscriptions page
to add it; repeat for each calendar you want. A parent collection URL isn't a substitute for an
individual calendar URL. For private calendars,
put the login in `caldav.json` in the data directory and keep credentials out of the URL. A login mapped
to a parent collection URL applies to calendars beneath that path. Use a dedicated account with
read-only access if the server supports it; ContentFeed only sends calendar-reading requests.

CalDAV calendars (such as Radicale's) stay in step with the feed: every time the page checks for changes, the
server asks the calendar for its version, a small request that changes whenever anything in the
calendar does, and reads the calendar again only if it has changed. So a change in your calendar
reaches the feed within seconds. If the calendar server doesn't answer within 2 seconds, the feed
keeps using the events it read last, says so in the status line, and tries again every 30 seconds.
A server that reports no version is read again at most once a minute. Shared `.ics` links are
checked on the subscription timer like feeds.

For example, `caldav.json` can map the Radicale user collection to a login used by selected child calendars:

    {"https://radicale.example/andreas/": {"username": "contentfeed", "password": "…"}}

Keep this file readable only by the service account (for example, `chown contentfeed: /var/lib/contentfeed/caldav.json && chmod 600 /var/lib/contentfeed/caldav.json`). ContentFeed refuses to use it when group or other users have access. Use HTTPS unless the connection stays on a trusted private network. The password stays server-side; it is not included in `feeds.txt` or subscription status. Authenticated redirects are followed only when they keep the same origin. Older credential-bearing calendar URLs are moved into this file at startup.
The server finds the feed, also where a page doesn't link to it: it tries the usual addresses, such as
`/feed`, `/rss.xml` and `/index.xml`, under the page and at the top of the site. It also tries them when a
site turns away the server but not its feed (OpenAI's news page, say). The server checks it every 30 minutes (`--feed-interval MINUTES`), and adds
its posts to the feed.

Posts stay in the feed after they drop out of the subscription's feed (up to 5000 per subscription).
Feeds only list recent posts (a YouTube channel's feed has its latest 15 videos), so for YouTube
channels and playlists the server also loads every older video once, right after subscribing. It
reads them from YouTube's playlist page, which needs no API key but gives upload dates only as
"3 years ago", so these videos show approximate dates ("about 3 years ago"). This uses YouTube's own
page data rather than an official API, so it can break when YouTube changes its pages; the
Subscriptions page then shows the error and the server tries again every 6 hours.

The feed shows 300 items at a time; "Show more" at the bottom loads the next 300. Newest come first,
so older videos are near the end.

The Subscriptions page (`/subscriptions`, linked at the top of the feed) lists every subscription
with its post count, latest post, last check and any errors. Clicking a subscription opens its page
(`/subscription?url=...`), with its stats, a view of every "next up" post in it (or choose the Next up view),
and all its posts. From there you can subscribe, edit
tags, check a feed now and unsubscribe. Every change is written straight to `feeds.txt`, keeping
your comments in it. Unsubscribing also forgets which of its posts you opened or tagged (unless a post is
also in `links.txt` or another subscription), so subscribing again starts afresh. Removing a
subscription by editing `feeds.txt` keeps that history.

To catch up on a subscription without its back catalog, "Tag posts older than" tags every post
published before that age "don't show again" (red), so they get its points (−1000 by default). Months
count as 30 days and years as 365. Posts you've already tagged keep their tag, and posts without a
date are left alone. It can't be undone in one step: click the red dot on a post in the feed to remove
its tag.

You can also add links and subscriptions (with tags) from the box at the top of the Subscriptions
page; they are appended to the files. To remove a link, edit `links.txt`. Changes to either file
show up within about 30 seconds.

Page details for links are fetched once; subscription posts are kept between checks. "Refresh all"
re-fetches links and checks every subscription now.

## Pages without a feed

Many company and research blogs have no RSS or Atom feed. Subscribe to the page that lists their posts,
such as `https://www.anthropic.com/news`, and the feed reads the posts from the page instead:

- The posts are the page's links to pages under it, such as `/news/some-post`, at the depth most of them
  are (so not `/research/team/alignment` on a page of `/research/…` posts). Links in the page's navigation,
  header and footer don't count, and nor do tags, categories, authors and social media.
- If there are fewer than two of those, they are the links to the folder the page links to most, first on
  its own site (such as `/post/…` on Eleos AI's `/research`), then on others (such as `arxiv.org/abs/…` on a
  page listing papers).
- A page that adds its links with JavaScript has none of these. Then the posts are the pages under it in
  the site's sitemap, if it has one.

Each new post's own page is read once, for its title, description and picture, the way a link in
links.txt is, and the site's name is taken off the end of its title. Where several posts' pages have the
same title, such as each episode's page being titled with the series' name, they keep the title from the
list instead, and a description several share, such as the site's, is left out. A post whose page has a
video file (an HTML `<video>`) plays in its card like a YouTube video, and counts as a video for its media
type. Its date is the first of: the one
in the page's metadata, the one by its link in the list, and the first date written in the post. A post
with none is dated by when the feed first saw it, so the first check puts such posts at the top.

Only the first page of the list is read (up to 200 posts), not those behind "Load more" or page 2, but
posts stay in the feed once seen. The Subscriptions page says "No feed; posts are read from the page" for
these. This is guesswork that suits most blogs, but it can pick the wrong links, such as team pages on a
page whose posts are added by JavaScript; check what a new subscription shows. If the site has a
Substack or a newsletter with a feed, subscribing to that is more reliable.

## Podcasts and audio

Subscribe to a podcast by its RSS feed. Each episode's card has an audio player, under the episode's
picture or the podcast's cover, and plays the file from the podcast's own server; pressing play counts as
opening the episode. The card links to the episode's page, or to the audio file for an episode without one.
Its length is the feed's `itunes:duration`.

A page that lists audio files without a feed, such as an archive's table of recordings
(`https://www.fdrlibrary.org/utterancesfdr`), works too, when it links to more audio files (`.mp3`, `.m4a`,
`.ogg`, `.opus`, `.wav`, `.flac` and so on, also with an id after the name, as in `/talk.mp3/f5f4…`) than to
any folder of pages, and has fewer than two posts under its own address. Every audio file on the page
becomes a post, with no limit of 200, and none is downloaded until you play it. Such links usually just say
"Download", so each post is described by the table row or list item its link is in: its longest text is the
title, cut at the first full stop when it's long, with the whole text as the description. A cell holding only
a date (`1924/06/26`, `1924-06` or `1920`, as its first day) dates the post, or else the first date written
in the row, and a length in brackets, such as "(4 min.)" or "(13 min 42 sec)", is its length.

A link in `links.txt` straight to an audio file gets a player too. The Audio setting gives all of these
points of their own (see Ranking).

## Jellyfin

You can subscribe to the movies and TV episodes on a [Jellyfin](https://jellyfin.org) server. First
add a login for the server to `jellyfin.json` in the data directory (`/var/lib/contentfeed` for the
service; create the file if it isn't there).
The key is the server's address, the same as in the browser up to `/web`:

    {"https://jellyfin.example": {"username": "me", "password": "…"}}

The address must match the one you subscribe to, including `http://` or `https://` and the port
(for example `http://jellyfin.home.arpa:8096`). With the systemd service the file is
`/var/lib/contentfeed/jellyfin.json`; if you create it as root, make it readable only by the service
with `chown contentfeed: /var/lib/contentfeed/jellyfin.json && chmod 600 /var/lib/contentfeed/jellyfin.json`.
If the feed can't use the file, subscribing says why.

The feed sees what that user sees. A Jellyfin user just for the feed, without admin rights and with only
the libraries you want, limits what the password stored here can do. Then subscribe to:

- the server's address, for every movie and episode on it, or
- a page on it, copied from the browser (`https://jellyfin.example/web/#/details?id=…`): a library,
  series, season or collection, for what's in it.

The server is checked like any other subscription, and the feed picks up changes to `jellyfin.json`
at the next check. Movies and episodes are dated by when they were added to the server ("added 2 days
ago"), so new additions come first, and ones removed from the server leave the feed.

Click a card's picture to play it right there; the title opens it in Jellyfin's web app instead.
The video comes through the feed server, which logs in for you, so your browser needs no Jellyfin
login and never sees the password or token. Jellyfin converts files the browser can't play as they are
(such as MKV or HEVC) to HLS, which Chromium-based browsers like Vivaldi play, but Firefox doesn't.
The feed server only passes on videos of posts in the feed, but anyone who can open the feed can play
them. Items without a picture have no play button, so the card just opens them in Jellyfin, and a video
that can't play in the card (for example if Jellyfin fails to convert it) shows a link to open it in
Jellyfin instead.

"Next in its subscription" goes by series: open an episode and the
next one in season and episode order gets the bonus. Movies don't get it. Whether you've watched
something in Jellyfin doesn't count as opened here; only opening it from the feed does.

## NRK TV

You can subscribe to a series on [NRK TV](https://tv.nrk.no): copy its address from the browser, such
as `https://tv.nrk.no/serie/skam`. An address of one of its seasons or episodes subscribes to the whole
series. The feed gets the episodes you can watch now, from NRK's public catalogue API (`psapi.nrk.no`),
which needs no login. It isn't documented, so it can break when NRK changes it; the Subscriptions page
then shows the error.

A series with seasons, such as a drama, is a series in the feed too: open an episode and the next
one in season and episode order is next up, and later episodes are held back until you've opened an
earlier one. Episodes that are no longer available leave the feed. Other series, such as the news or
talk shows, list their episodes by date; the feed reads up to the 1000 newest, and next up is the
episode published after the one you opened. Their titles say the date ("5. oktober"), even where NRK
says "I dag" or "Fredag". Episodes are dated by when they came out on NRK TV.

Clicking a card opens the episode on tv.nrk.no; it doesn't play in the card. Many programs can only
be watched in Norway.

## Authors

To follow a researcher's papers, subscribe to one of these:

- **An arXiv author page**, such as `https://arxiv.org/a/lecun_y_1`: the papers the author has claimed
  on arXiv, from the page's own feed. Authors get such a page once they claim their papers, so it may
  be missing or behind on recent ones.
- **An arXiv author search**: search arXiv for the author's name with "Authors" chosen, as "LeCun, Yann",
  and subscribe to the results' address (`https://arxiv.org/search/?query=LeCun%2C+Yann&searchtype=author…`).
  The feed asks arXiv's API for the 50 newest papers by anyone with that name, so a common name brings
  other people's papers too.
- **A Google Scholar profile**, such as `https://scholar.google.com/citations?user=JicYPdAAAAAJ`: the papers
  on the profile, including ones not on arXiv. Google Scholar has no feed or API, so the server reads the
  newest 100 from the profile page, which can break when Google changes it. It gives only each paper's year,
  so papers show approximate dates: the middle of their year at first, and the day they appeared on
  the profile for recent papers found later. Google Scholar turns away clients that ask often, so a profile is
  checked at most every 6 hours (Check now and Refresh all check it anyway), and each paper's own page is never
  read. If Google asks for a CAPTCHA instead, the Subscriptions page shows the error and later checks try again.
  Cards link to the paper's page on Google Scholar.

arXiv papers link to their abstract page, without a version number, so a new version of a paper is
the same post.

## Citations

To get the new papers that cite a paper, subscribe to the paper's link:

- **On arXiv**: its abstract, PDF or HTML page, such as `https://arxiv.org/abs/1706.03762` (any version).
- **On Google Scholar**: the paper on an author's profile (the link of a card from a profile you follow, or
  `scholar.google.com/citations?view_op=view_citation&…`), its "Cited by" list (`scholar.google.com/scholar?cites=…`)
  or its versions (`scholar.google.com/scholar?cluster=…`).

The citing papers come from [OpenAlex](https://openalex.org), a free, open catalog of scholarly papers and what
they cite, which needs no account: the 50 newest by publication date, leaving out papers dated in the future
(such as a journal's next issue). An arXiv paper is found on OpenAlex by its arXiv DOI. A Google Scholar paper
is found by its title and year, read once from Google Scholar, or not at all for a paper on a profile you follow;
if Google asks for a CAPTCHA, the Subscriptions page shows the error and later checks try again. Once found, the
paper is remembered and Google Scholar isn't asked again. A citation subscription is checked at most every 6 hours
(Check now and Refresh all check it anyway). Without a key, OpenAlex gives each network address a small free
daily budget, plenty for a few subscriptions; for many, get a free key at openalex.org and start the server
with it in the `OPENALEX_API_KEY` environment variable.

A citing paper links to its arXiv abstract when it is on arXiv, so a paper by an author you follow on arXiv
that cites a paper you follow is one card. Cards of papers say why they are in the feed, under the source line:
**✍ Paper by** the author, for an author on arXiv or Google Scholar, and **❝ Cites** “the cited paper”, for a
citation subscription, with one label for each subscription the paper is in. Searching the feed finds the
author's name and the cited paper's title too.

## Viewed items

Opening an item (clicking its link, middle-clicking it, or playing its video) records when you
last viewed it. Scrolling past doesn't count. Nothing is hidden: an item you've opened scores lower (see Ranking). The card says when you
viewed it.

The feed's order changes only when you load the page, change a setting or the mode, or
search, and then the page shows the top of the feed; "Show more" adds the next posts below the ones
shown. Otherwise nothing moves while you're watching: the page checks the server in the background
and updates each card's score, tag and viewed time (and the status line) in place.
New posts and new scores take effect in the order the next time you reload. Playing a video stops
the one playing in another card.

## Tags

Each card has three dots in its bottom corner, each showing the points it adds:

- **green, "show again soon"**: +10 by default
- **white, "maybe later"**: −50 by default
- **red, "don't show again"**: −1000 by default

A tag only adds its points to the item's score; change them in the score panel (see Ranking). Tagging
an item doesn't count as opening it, so it neither gets the "seen" points nor makes the post after it
next up. Items start untagged; click the dot of an item's tag again to remove the tag.

## Modes

The feed's settings belong to modes, such as Meeting, on during busy events with a post limit of 0
for an empty page, or one with points for tasks. At first there's one, Normal, with the default settings, on in free time. It's
an ordinary mode: change, move or remove it like any other. The Mode list
at the top of the settings panel shows Auto and your modes; **New mode…** adds one, starting as a
copy of the settings shown (or the defaults), and you change it like any other. Picking a mode in the
list uses it until you pick Auto again.

With Auto (the default), the calendar turns modes on, and the list marks the one that's on now.
Each mode has three toggles under **With Auto, on during**: **Busy events**, **Free (not busy)
events** and **Free time, with no event** (no event of either kind taking place). A mode is on
while any of its ticked situations lasts; all-day events, such as birthdays, don't count, so free
time goes on during them. A new mode has none ticked, so it's only on when you pick it. When
several modes could be on, such as two ticking the same situation, or one for busy events and one
for free events while both kinds take place, the one highest in the list wins; move modes up and
down with the ↑ and ↓ buttons.

Each mode's **Lead time** (under **Mode changes**, 0 by default) turns it on that long before
an event it's on for starts, so you're ready when it does: a Meeting mode with a lead time of 30
minutes turns on at 09:30 for a 10:00 meeting; with a post limit of 0, the feed empties then. An
event's lead time is that of the highest mode on during events of its kind, busy or free, and free
time ends when it begins. Free time itself doesn't use a lead time.

While the calendar is set to change the mode (see when below), a post whose estimated length would run
past the change gets the mode's **Past the mode change** points (under **Mode changes**) for each
minute it would (−20 per minute by default, rounded up to whole minutes), so a post running 1 minute
over drops a little and a two-hour film sinks to the bottom. With a Work mode on from 09:00, at 08:30
a 45-minute video gets −300. Nothing is left out. Like the rest of the order, this is worked out
when the feed is shown, not as time passes. The post you're viewing (the one you opened last) is
exempt, so reloading while you watch doesn't push it down. Posts without an estimated length and
tasks get no points, and neither does anything while you've picked a mode, as it doesn't change. To
leave posts out altogether, use the post limit (see Ranking).

When no mode is on, the feed is empty and says so, as does the status line ("No mode on"), and the
panel shows no settings until you pick a mode. A subscription's page and a
post's page still list posts then, ranked with the default settings.

The panel always shows and changes the settings of the mode in effect, and the status line names
it. Following the calendar, the status line says when the mode next changes and to what ("Meeting
mode for “Standup” until 17:00, then Work", "Normal mode in free time until 09:00, then Work", or "No
mode on until 09:00"), looking a week ahead; the page checks again just after then. Click the mode
there to see the event that turned it on below: its title, link, calendar, times and description. In
free time, it's the next event, which ends it. A change is when the mode in effect becomes another, not just when
an event starts or ends: back-to-back work blocks count as one, and a meeting starting during a
work block ends Work then if a mode for busy events is higher in the list. When the mode changes,
such as when a work block starts, the feed is shown again from the top, any video or other media
playing is stopped, and a short alert sounds, on every page; changing the mode yourself in the panel
does neither. Remove deletes the mode
shown and its settings. Modes are saved in `settings.json`.
Settings saved before Normal was a mode become the Normal mode, at the bottom of the list, on in
free time; each mode keeps the lead time it had; the old global Fetch details setting is ignored; modes saved with the earlier title patterns are on during free events, unless the
pattern was empty; and a mode's Past the stop time points, from before they were for the mode change,
become its Past the mode change points.

## Ranking

Feed order comes from a ranker in `ranking.py`. Rankers never learn from how you use the
feed; they only use what you wrote (tags, which file or subscription an item came from, the site,
the tags you gave items), the numbers in titles, dates (when a post was published, or when a link was added) and when you
last opened an item. A ranker returns reasons with points, such as
`("tagged music", 2)`; the score is their sum. Click "Score" on any card to see its reasons.

All scoring terms add their configured signed points; negative values subtract points. Similarity weights, time windows and counts remain nonnegative. The default, `score`, adds up these points. With the default settings:

- +30 for **next up**: the post after the one you opened most recently in its subscription. That's
  the next part of its series (see below) or, if it has none, the post published next. Jellyfin
  series go in episode order. Tag a subscription `latest` in feeds.txt to turn next up off for it,
  such as a news feed.
- −30 for anything you've opened, including next up
- 0 if nothing from its subscription has been opened yet, such as one you just subscribed to
- −30 for a later part of a series when you haven't opened anything before it (see below)
- 0 (off) × the **sum** over everything you opened recently of similarity × the fraction of the 24-hour window remaining since opening; each match contributes on its own, not just the strongest (see below)
- 0 for its media type: video (YouTube, Jellyfin, NRK TV, pages with a video file), audio (podcast episodes and
  other audio files, see Podcasts and audio), image (a link straight to a picture) or article (anything else). Each has its own setting, which may be negative, to favor or hold back that type.
- 0 per **minute away from the target content length** by default. Content length adds `weight × |estimated minutes − target minutes|`. Set a negative weight to favor content near the target, or a positive weight to favor content farther away. The target defaults to 0 minutes, preserving existing scores; both the weight and target are configurable in Settings. Runtime metadata is used where available; text is estimated at 200 words per minute (at least one minute). Feed text may be only an excerpt, and page estimates can include surrounding text. Unknown lengths have no adjustment. A post whose length or exact publication time is missing stays out of the main feed until its details have been fetched; the server fetches them in the background as soon as it sees the post, and the status line says how many posts are waiting ("3 posts waiting for details"). If the details turn out to be unavailable, the post enters without them. Posts whose details can't be fetched (tasks, Jellyfin, Google Scholar, audio) enter at once, and a subscription's page lists every post. Fetches wait in a queue per site and run one request at a time per site, so a site with many posts to fetch doesn't hold up others. When the site loads, the posts it shows that are still incomplete are fetched again if they weren't in the last six hours. YouTube updates fetch runtime and exact publication time together. Unavailable data stays unknown and recent attempts are skipped for six hours.
- 0 (off) for a **middle author** paper: one in the feed for an author you follow (see Authors) who is
  neither its first nor its last author, when it has at least 4 authors (a second setting). In many fields the
  first author did most of the work and the last led it, so −1000 drops papers the author only helped on, like
  the red tag. Where several authors you follow wrote it, each must be in the middle. Names are matched by surname and
  first initial, ignoring case and accents. arXiv lists every author; Google Scholar's profile page shows only
  the first few, then "...", so any shown author but the first is in the middle, and an author it leaves out
  isn't counted. Neither is a paper from before this setting existed, until its feed is checked again, nor a
  field that lists authors alphabetically, where the place says nothing.
- +10 if tagged "show again soon" (green), −50 if "maybe later" (white), −1000 if "don't show again" (red)
- 0 (off) × the **sum of similarities to posts above it**. At −15, a 100% match contributes −15,
  a 50% match contributes −7.5, and two 50% matches contribute −15 together. It uses the same
  publish-time and content-length weights as recent-view similarity, without a viewing-time fade.
  Different subscriptions do not match. “Similar posts above” replaces the fixed repeat penalty;
  existing saved repeat amounts are carried over to the new setting.

- −20 per minute a post would run past the next mode change, while the calendar is set to change it (see Modes)

Equal scores are ordered unopened first, then newest first, so with nothing else going on the
feed is newest first.

The **Post limit** setting shows only that many of the highest-scoring posts, with no "Show more":
1 gives one post at a time (tag or open it, reload, and the next one is there), and 0 an empty feed.
An empty box means no limit, which is the default (stored as the highest value, 10 000 000). A
subscription's page isn't limited.

For YouTube, subscribing imports the channel’s public playlists in the background alongside older
videos. Existing channels are backfilled automatically. Each playlist defines a series in its saved
video order; a video can belong to several series. The uploads playlist is excluded for channels.
Direct playlist subscriptions use that playlist’s order. Videos outside playlists have no series,
and YouTube titles are never used to infer one. Playlist membership is saved with the subscription
and imported once; failures retry automatically.

For other feeds, a post follows another from the same subscription when their titles have the same numbers, except
one that's 1 higher: "Making a CPU, part 2: the ALU" follows "Making a CPU, part 1", and "S01E05"
follows "S01E04". Only the numbers are compared, not the words, and only between posts at most 10
apart in the subscription; numbers of 1000 or more, such as years, don't count. Posts that follow
each other make a series, whose first part is the one that follows nothing. So "part 3" is held back
until you've opened part 1 or 2, and after you open part 2, part 3 is next up. Jellyfin episodes
and NRK TV episodes are in series by episode order instead, and NRK TV's news and other dated programs have no series. Subscriptions tagged `latest` have no series. Because words aren't compared, unrelated posts are
sometimes taken for a series, such as "Q&A #2" after "Chapter 1"; the card says which post it
thinks starts the series.

How alike two items are, from 0 to 100%:

- **Subscription base:** 50% by default for posts from the same subscription. The weighted
  components below fill the remaining share: `50% + 50% × measured similarity`. A measured
  60% match therefore becomes 80%. The flat **Subscription base similarity** setting accepts
  0–100%; 0 restores the component-only calculation, and 100 makes all same-subscription posts
  fully alike. Cards show Subscription alongside the other weighted contributions.
- **Series base:** 75% by default for posts in the same series of a subscription (see above for
  what makes a series), used instead of the subscription base when it's higher. A measured 60%
  match between two episodes of a show becomes `75% + 25% × 60% = 90%`. Set it at or below the
  subscription base to ignore series.

- **Publish time:** 100% for posts published together, falling evenly to 0% at the configured
  window (30 days by default). A week apart is 77%, two weeks 53%.
- **Content length:** shorter minutes divided by longer minutes. Equal lengths are 100%;
  10 and 20 minutes are 50%.
- The two similarities add according to their relative weights, then divide by the sum of
  the available weights. A 50% publish-time match and a 70% length match with equal weights
  give 60% measured similarity, or 80% overall with the default base (rounded once to whole
  percent). Scaling both weights together leaves similarity unchanged.
- Settings exposes both weights and the publish-time window. Publish time defaults to
  weight 1 and content length to weight 0. The Content length distance-from-target scoring is
  separate from similarity.
- If either length is unknown or invalid, length is left out along with its weight; with no
  available component, distinct same-subscription posts keep the subscription base.
- Only posts from the same subscription match. Tags don't affect similarity. When length is
  enabled, posts can still gain similarity beyond the subscription base outside the publish-time window.
- Every item is 100% like itself, even without a known length. Set both bases and both weights to 0 to disable the
  similarity penalties entirely, including for opened items.

The similarity penalty fades linearly to zero over 24 hours, updated in whole hours. Every
recently opened match adds its own similarity × remaining fraction, so the penalty is the sum
over all of them: two 50% matches opened 12 hours ago together cost the same as one 100% match
done. With Like something opened at −40, after you
watch a video, its channel's next one (published a week later) starts at next up 30 − 35.2;
after 12 hours, that penalty is only 17.6. These examples use the default 50% subscription base
and publish-time-only measured similarity.
Something you've opened then starts at −70 (−30 for opened and −40 for being like itself),
reaches −50 after 12 hours, and −30 after 24 hours. Both similarity penalties are 0, so off, by
default.

The feed is built from the top down, choosing the highest current score after accounting for
similarity to all posts already placed. Less-similar posts can overtake closer matches from the
same subscription. All preceding matches add together; this penalty does not depend on whether
you opened those posts. The cumulative adjustment is rounded to cents, not each match separately.
Cards show the number of matches and the total similarity, such as “similarity to 2 posts above,
100% total −15”.

Ranking is computed only as far as the requested page needs, then resumed from cached state for
“Show more”.

Each card's "Score" shows its points, such as
`88% like “Egyptian Fractions”, opened 2 hours ago (same subscription, published 7 days apart), 91.67% penalty remaining −32.27`
or `in a series starting “Making a CPU, part 1”, nothing before it opened −30`.

Change the numbers in the score panel: click "Settings" at the top of the feed. It opens
beside the feed on a wide screen, or along the bottom on a narrow one, and stays open across reloads
until you close it. The settings are in groups that fold away (Feed; Opened and next up; Tags; Kind,
length and age; Mode changes; Tasks; Similarity), and each group's heading says how many of its settings
you've changed from the defaults. The browser remembers which groups you left open. Each setting has
a box for an exact number and a slider; the sliders go in steps of 1, 1.5, 2, 3, 5 and 7 times a power
of ten, so they cover both small and large numbers. Leave the post limit's box empty for no limit.
Age counts fractional 365-day years since publication (or addition for links), updated in whole
hours, and future dates count as age 0. The feed reorders, showing its top, as you move a slider,
and the setting is saved when you let go (or when you press Enter in the box). Settings that add no points when the page loads (those at 0, and the post limit when there is
none) are hidden to keep the panel short, along with the similarity measures while both similarity
penalties are, and so is a group with nothing left to show. Bring a setting back with the "Add a
setting" list below the groups, which lists the hidden ones under their groups' names: picking one
opens its group and puts the cursor in its box. The defaults are at the top of `ranking.py`. The other ranker,
`chronological`, has no rules: everything scores 0 and the newest item is first. To add another ranker, register a
function with `@ranker("name")`, then select it with `.venv/bin/python server.py --ranker name`, or try
it without a restart at `/api/feed?ranker=name`.

YouTube Shorts has a separate signed points setting in the score panel (default 0).
It applies to YouTube /shorts/ links in addition to Video points; negative values penalize them.
