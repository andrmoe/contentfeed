# Content Feed

Turns a plain list of links and feeds (RSS, Atom, YouTube channels, subreddits, blogs…) into a browsable card feed (title, description, preview image,
inline YouTube embeds), served to your local network. Python 3 standard library only.

## Run

    python3 server.py --port 8090         # development; keeps data in ./data
    python3 server.py --port 8090 --data-dir /some/other/dir

The default port is 80, so the service is reachable at plain `http://<lan-ip>`. Ports below
1024 need root or `CAP_NET_BIND_SERVICE`; the systemd service grants just that permission, so it
doesn't run as root. For development, pick a port of 1024 or above as shown, and open the
printed address from any device on the network.

## Your data

Everything personal is kept in the data directory, never in the code. Where that is depends on
how you run the feed:

- as the systemd service (see Run as a service): `/var/lib/contentfeed`
- with `python3 server.py`: `data/` next to `server.py` (git-ignored), or the folder given with `--data-dir`

It holds:

- `links.txt` and `feeds.txt`: your links and subscriptions (created with instructions on first run)
- `added.json`: when each link or post was first seen
- `viewed.json`: when you last opened each item
- `skipped.json`: when you last skipped each item (the white dot)
- `colors.json`: the items you marked green or red
- `settings.json`: your score settings from the feed's score panel, if you changed them
- `jellyfin.json`: logins for Jellyfin servers you subscribe to (see Jellyfin), if any
- `cache.json` and `feeds.json`: fetched page details and subscription posts

To back up or move your feed, copy this directory.

## Run as a service

`deploy/contentfeed.service` runs the feed with systemd, for example in its own LXC container or
VM. The code is a git checkout at `/opt/contentfeed` owned by root, which the service can only
read. The data lives in `/var/lib/contentfeed`, which systemd creates and which only the
`contentfeed` user can read. As root in the container (Debian/Ubuntu shown):

    apt install python3 git
    useradd --system --no-create-home --home-dir /var/lib/contentfeed --shell /usr/sbin/nologin contentfeed
    git clone https://github.com/andrmoe/contentfeed.git /opt/contentfeed
    cp /opt/contentfeed/deploy/contentfeed.service /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable --now contentfeed

The container needs an address on your LAN (e.g. a bridged network) for other devices to reach
port 80. Logs: `journalctl -u contentfeed -f`; they include the addresses the server fetches.

To update: `git -C /opt/contentfeed pull && systemctl restart contentfeed`. If the service
file changed, copy it to `/etc/systemd/system/` again and run `systemctl daemon-reload` first.

To edit your lists by hand, edit `/var/lib/contentfeed/links.txt` or `feeds.txt` as root; the
running server picks up changes within about 30 seconds.

If the service fails to start with `status=226/NAMESPACE`, the container doesn't allow systemd's
sandboxing; comment out the sandboxing block in the unit file.

## Links and subscriptions

In the data directory, `links.txt` holds single links and `feeds.txt` holds subscriptions. In both, each line is a URL
optionally followed by space-separated tags (`https://example.com music longread`), and lines
starting with `#` are comments.

A subscription can be an RSS or Atom feed, or a page that has one: a YouTube channel
(`youtube.com/@handle`, `/channel/…`) or playlist, a subreddit, a blog, a Mastodon profile and so on.
It can also be a blog or news page without a feed (see Pages without a feed), a series on NRK TV
(see NRK TV) or a Jellyfin server (see Jellyfin).
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
with its post count, latest post, last check and any errors. From there you can subscribe, edit
tags, check a feed now and unsubscribe. Every change is written straight to `feeds.txt`, keeping
your comments in it. Unsubscribing also forgets which of its posts you opened or colored (unless a post is
also in `links.txt` or another subscription), so subscribing again starts afresh. Removing a
subscription by editing `feeds.txt` keeps that history.

To catch up on a subscription without its back catalog, "Mark posts older than" marks every post
published before that age red, so they sink to the bottom of the feed. Months count as 30 days and years
as 365. Posts you made green stay green, and posts without a date are left alone. It can't be undone in
one step: each post goes back to white from its dot in the feed.

You can also add links and subscriptions (with tags) from the box at the top of the feed; they
are appended to the files. To remove a link, edit `links.txt`. Changes to either file show up
within about 30 seconds.

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
links.txt is, and the site's name is taken off the end of its title. Its date is the first of: the one
in the page's metadata, the one by its link in the list, and the first date written in the post. A post
with none is dated by when the feed first saw it, so the first check puts such posts at the top.

Only the first page of the list is read (up to 50 posts), not those behind "Load more" or page 2, but
posts stay in the feed once seen. The Subscriptions page says "No feed; posts are read from the page" for
these. This is guesswork that suits most blogs, but it can pick the wrong links, such as team pages on a
page whose posts are added by JavaScript; check what a new subscription shows. If the site has a
Substack or a newsletter with a feed, subscribing to that is more reliable.

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

## Viewed items

Opening an item (clicking its link, middle-clicking it, or playing its video) records when you
last viewed it. Scrolling past doesn't count. Nothing is hidden: an item you've opened scores lower (see Ranking). The card says when you
viewed it.

Cards you open or color stay where they are until you reload the page, so nothing moves away
while you're looking at it. Playing a video stops the one playing in another card.

## Colors

Each card has three dots in its bottom corner: green, white and red. They don't have proper names
yet; for now they change an item's score:

- **green**: +10 by default; you can change it in the score panel (see Ranking)
- **white**: the default, no change. Clicking white also skips the item: it counts as viewed (the card
  says "Skipped"), except that it doesn't make anything next up. Neither the item nor the post after it
  gets the next-up bonus, so you can pass over an episode without the feed pushing the following one.
- **red**: puts it at the bottom, below everything else

## Ranking

Feed order comes from a ranker in `ranking.py`. Rankers never learn from how you use the
feed; they only use what you wrote (tags, which file or subscription an item came from, the site,
the colors you picked), the numbers in titles, dates (when a post was published, or when a link was added) and when you
last opened an item. A ranker returns reasons with points, such as
`("tagged music", 2)`; the score is their sum. Click "Score" on any card to see its reasons.

The default, `score`, adds up these points. With the default settings:

- +30 for **next up**: the post after the one you opened (not skipped) most recently in its subscription. That's
  the next part of its series (see below) or, if it has none, the post published next. Jellyfin
  series go in episode order. Tag a subscription `latest` in feeds.txt to turn next up off for it,
  such as a news feed.
- −30 for anything you've opened or skipped, other than next up
- +20 if nothing from its subscription has been opened yet, such as one you just subscribed to
- −30 for a later part of a series when you haven't opened anything before it (see below)
- −40 × how alike it is to the most similar thing you opened in the last 24 hours (see below)
- 0 for its media type: video (YouTube, Jellyfin, NRK TV), image (a link straight to a picture) or
  article (anything else). Each has its own setting, which may be negative, to favor or hold back that type.
- +10 if green; red items go to the bottom
- −15 for each post from its subscription higher up in the feed

Equal scores are ordered unopened first, then newest first, so with nothing else going on the
feed is newest first.

A post follows another from the same subscription when their titles have the same numbers, except
one that's 1 higher: "Making a CPU, part 2: the ALU" follows "Making a CPU, part 1", and "S01E05"
follows "S01E04". Only the numbers are compared, not the words, and only between posts at most 10
apart in the subscription; numbers of 1000 or more, such as years, don't count. Posts that follow
each other make a series, whose first part is the one that follows nothing. So "part 3" is held back
until you've opened part 1 or 2, and after you open part 2, part 3 is next up. Jellyfin episodes
and NRK TV episodes are in series by episode order instead, and NRK TV's news and other dated programs have no series. Subscriptions tagged `latest` have no series. Because words aren't compared, unrelated posts are
sometimes taken for a series, such as "Q&A #2" after "Chapter 1"; the card says which post it
thinks starts the series.

How alike two items are, from 0 to 100%:

- Two posts from the same subscription are 100% alike if published at the same time, falling evenly
  to 0% when published 30 days or more apart: a week apart is 77%, two weeks 53%.
- Items from different subscriptions (or links) are 30% alike for each tag they share, other than
  `latest`, such as two subscriptions both tagged `math`.
- An item is 100% like itself.

So after you watch a video, its channel's next one (published a week later: next up 30 − 30.8)
waits a day instead of coming straight back, while the channel's old videos aren't affected.
Something you've opened is at −70 (−30 for opened and −40 for being like itself) for 24 hours,
then −30. The feed is built from the top down, taking the repeat penalty into account as it goes,
so the second post from a subscription loses 15, the third 30, and two in a row are rare.

Each card's "Score" shows its points, such as
`77% like “Egyptian Fractions”, opened 2 hours ago (same subscription, published 7 days apart) −30.8`
or `in a series starting “Making a CPU, part 1”, nothing before it opened −30`.

Change the numbers in the score panel: click "Score settings" at the top of the feed. It opens
beside the feed on a wide screen, or along the bottom on a narrow one, and stays open across reloads
until you close it. There are twelve settings, each with a slider and a box for an exact number:
Next up, Seen, New subscription, Series, Repeat penalty, Video, Image and Article (these four may be
negative), Green, Similar
(which also has its number of hours), Same subscription (days) and Shared tag (percent). The
sliders go in steps of 1, 1.5, 2, 3, 5 and 7 times a power of ten, so they cover both small and large
numbers. The feed reorders as
you move a slider, and the setting is saved when you let go (or when you press Enter in the box). The defaults are at the top of `ranking.py`. The other ranker,
`chronological`, has no rules: everything scores 0 and the newest item is first. To add another ranker, register a
function with `@ranker("name")`, then select it with `python3 server.py --ranker name`, or try
it without a restart at `/api/feed?ranker=name`.
