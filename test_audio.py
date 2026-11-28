import unittest
from datetime import datetime, timezone
from unittest import mock

import feeds
import pages
import ranking
import server


def ts(*args):
    return datetime(*args, tzinfo=timezone.utc).timestamp()


PODCAST = b"""<?xml version="1.0"?>
<rss xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" version="2.0"><channel>
<title>A podcast</title><link>https://pod.example/</link>
<itunes:image href="https://pod.example/cover.jpg"/>
<item><title>Episode 2</title><link>https://pod.example/2</link>
  <enclosure url="https://cdn.example/ep2.mp3" length="1" type="audio/mpeg"/>
  <itunes:duration>1:02:03</itunes:duration><description>""" + b"word " * 900 + b"""</description>
  <pubDate>Tue, 02 Jan 2024 00:00:00 GMT</pubDate></item>
<item><title>Episode 1</title><guid isPermaLink="false">ep1</guid>
  <enclosure url="https://cdn.example/ep1.m4a" length="1"/><itunes:image href="https://pod.example/ep1.jpg"/>
  <pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate></item>
<item><title>A blog post</title><link>https://pod.example/post</link>
  <enclosure url="https://pod.example/pic.jpg" type="image/jpeg"/></item>
</channel></rss>"""

ARCHIVE = """<html><head><title>Audio Recordings</title></head><body>
<nav><a href="/documents/1/nav.mp3/x">Nav</a></nav>
<a href="/about">About</a> <a href="/visit/hours">Hours</a>
<table><thead><tr><th>Date</th><th>Description</th><th>Audio</th></tr></thead><tbody>
<tr id="r1"><td>1920</td><td><a name="r1"></a> Americanism - Recording made under the heading of "Nation's Forum"
  by Columbia Phonograph Co. Excerpts from his acceptance speech, given at Hyde Park, NY, August 9, 1920. (4 min.)</td>
  <td><audio controls preload="none"><source src="/documents/1/a001.mp3/f5f4-91" type="audio/mp4">Your browser
  does not support the audio element.</audio><br><a href="/documents/1/a001.mp3/f5f4-91">Download Audio File</a></td>
  <td>RLxA-1-P</td></tr>
<tr id="r2"><td>1924/06/26</td><td>New York City - Remarks to the convention (13 min 42 sec)</td>
  <td><a href="/documents/1/a002.mp3/a00f-8d">Download Audio File</a></td><td>73-7</td></tr>
<tr id="r3"><td>1928/06/27</td><td>Houston, TX - "The Happy Warrior" speech (25 secs)</td>
  <td><a href="/documents/1/a003.mp3/557c-ad">Download Audio File</a></td><td>73-8</td></tr>
</tbody></table></body></html>"""


class FakeResponse:
    def __init__(self, url, body, ctype="text/html"):
        self.url, self.body = url, body.encode()
        self.headers = mock.Mock(get_content_charset=lambda: "utf-8", get_content_type=lambda: ctype)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def geturl(self):
        return self.url

    def read(self, n=-1):
        return self.body


class PodcastFeedTests(unittest.TestCase):
    def test_episodes_get_their_audio_cover_and_length(self):
        items = {it["title"]: it for it in feeds.parse(PODCAST, "https://pod.example/feed.xml")["items"]}
        ep2, ep1, post = items["Episode 2"], items["Episode 1"], items["A blog post"]
        self.assertEqual((ep2["url"], ep2["audio"]), ("https://pod.example/2", "https://cdn.example/ep2.mp3"))
        self.assertEqual(ep2["image"], "https://pod.example/cover.jpg")  # the podcast's cover
        self.assertEqual(ep2["estimated_minutes"], 62.05)  # its duration, not its show notes' reading time
        # No page of its own: the episode is its audio file. An untyped enclosure counts by its extension.
        self.assertEqual((ep1["url"], ep1["audio"]), ("https://cdn.example/ep1.m4a", "https://cdn.example/ep1.m4a"))
        self.assertEqual(ep1["image"], "https://pod.example/ep1.jpg")
        self.assertNotIn("audio", post)
        self.assertEqual(post["image"], "https://pod.example/pic.jpg")

    def test_audio_is_its_own_media_type(self):
        self.assertEqual(ranking.media_type({"url": "https://pod.example/2", "audio": "https://cdn.example/a.mp3"}), "audio")
        self.assertEqual(ranking.media_type({"url": "https://pod.example/2"}), "article")
        self.assertEqual(ranking.DEFAULT_TERMS["audio_bonus"], 0)
        self.assertIn("audio_bonus", ranking.SIGNED)

    def test_audio_posts_are_not_fetched_for_details(self):
        self.assertIsNone(server.details_job({"url": "https://pod.example/2", "audio": "https://cdn.example/a.mp3"}, {}))


class AudioPageTests(unittest.TestCase):
    def fetch(self, html=ARCHIVE, known=frozenset()):
        url = "https://archive.example/recordings"
        with mock.patch.object(pages, "open_url", side_effect=lambda u, accept: FakeResponse(url, html)) as opened:
            result = pages.fetch(url, known)
        return result, opened

    def test_audio_files_listed_on_a_page_are_its_posts(self):
        result, opened = self.fetch()
        self.assertEqual(opened.call_count, 1)  # the audio files themselves aren't fetched
        self.assertEqual([it["url"] for it in result["items"]], [
            "https://archive.example/documents/1/a001.mp3/f5f4-91",
            "https://archive.example/documents/1/a002.mp3/a00f-8d",
            "https://archive.example/documents/1/a003.mp3/557c-ad"])
        first, second, third = result["items"]
        self.assertEqual(first["audio"], first["url"])
        # The row's text, not "Download Audio File" or the player's fallback text
        self.assertEqual(first["title"], 'Americanism - Recording made under the heading of "Nation\'s Forum" by Columbia Phonograph Co.')
        self.assertTrue(first["description"].endswith("August 9, 1920. (4 min.)"))
        self.assertEqual(second["title"], "New York City - Remarks to the convention (13 min 42 sec)")
        self.assertEqual(second["description"], "")
        self.assertEqual([it["published"] for it in result["items"]], [ts(1920, 1, 1), ts(1924, 6, 26), ts(1928, 6, 27)])
        self.assertEqual(first["estimated_minutes"], 4)
        self.assertAlmostEqual(second["estimated_minutes"], 13.7)
        self.assertAlmostEqual(third["estimated_minutes"], 25 / 60)

    def test_known_audio_posts_are_left_out(self):
        result, _ = self.fetch(known={"https://archive.example/documents/1/a002.mp3/a00f-8d"})
        self.assertEqual(len(result["items"]), 2)

    def test_posts_under_the_page_win_over_a_few_audio_files(self):
        html = ARCHIVE.replace("<table>", '<a href="/recordings/one">One</a> <a href="/recordings/two">Two</a><table>')
        found = pages.post_links(self.links(html), "https://archive.example/recordings")
        self.assertEqual(list(found), ["https://archive.example/recordings/one", "https://archive.example/recordings/two"])

    def test_a_folder_of_pages_wins_over_fewer_audio_files(self):
        posts = "".join(f'<a href="/post/{i}">Post {i}</a>' for i in range(5))
        found = pages.post_links(self.links(posts + ARCHIVE), "https://archive.example/recordings")
        self.assertEqual(len(found), 5)
        self.assertTrue(all("/post/" in u for u in found))

    @staticmethod
    def links(html):
        parser = pages.Links()
        parser.feed(html)
        return parser.links


if __name__ == "__main__":
    unittest.main()
