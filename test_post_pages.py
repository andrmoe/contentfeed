import contextlib
import copy
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import patch

import ranking
import server


class PostPageTests(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        for name in ('LINKS_FILE', 'FEEDS_FILE', 'ADDED_FILE', 'VIEWED_FILE', 'COLORS_FILE', 'SKIPPED_FILE'):
            stack.enter_context(patch.object(server, name, root / name))
        for name in ('subscriptions', 'cache', 'added', 'viewed', 'skipped', 'colors', 'ranked_cache'):
            stack.enter_context(patch.object(server, name, {}))
        stack.enter_context(patch.object(server, 'pending', set()))
        stack.enter_context(patch.object(server, 'state_version', 0))
        stack.enter_context(patch.object(server, 'schedule'))
        stack.enter_context(patch.object(server, 'schedule_subscription'))
        stack.enter_context(patch.object(ranking, 'WEIGHTS', dict(ranking.DEFAULT_WEIGHTS)))
        self.sub = 'https://example.com/feed'
        server.FEEDS_FILE.write_text(self.sub)
        self.posts = [dict(url=f'https://example.com/{i}', title=f'Part {i}', first_seen=100,
                           published=i * 86400, estimated_minutes=10) for i in (3, 2, 1)]
        server.subscriptions[self.sub] = {'title': 'Example', 'items': self.posts}
        self.url = self.posts[1]['url']

    def page(self, url=None, limit=300, **kwargs):
        return server.ranked_feed('score', limit, post_url=url or self.url, **kwargs)

    def urls(self, items):
        return [p['url'] for p in items]

    def test_numbered_series_order_and_similarity(self):
        page = self.page()
        self.assertEqual(page['item']['url'], self.url)
        self.assertEqual(self.urls(page['series'][0]['items']), self.urls(self.posts[::-1]))
        self.assertEqual(page['series'][0]['total'], 3)
        self.assertNotIn(self.url, self.urls(page['similar']['items']))
        self.assertEqual(page['similar']['total'], 2)
        for p in page['similar']['items']:
            self.assertEqual(p['related_percent'], ranking.similarity(page['item'], p, ranking.WEIGHTS)[0])
        self.assertEqual(server.viewed, {})  # Browsing the page does not open its source.

    def test_overlapping_playlists_keep_imported_order(self):
        for p in self.posts:
            p['youtube'] = p['url'].rsplit('/', 1)[-1]
        server.subscriptions[self.sub]['youtube_playlists'] = [
            dict(id='PLone', videos=['2', '1', '3', 'missing']),
            dict(id='PLtwo', videos=['3', '2']),
        ]
        page = self.page()
        self.assertEqual(len(page['series']), 2)
        self.assertEqual(self.urls(page['series'][0]['items']), [self.url, self.posts[2]['url'], self.posts[0]['url']])
        self.assertEqual(self.urls(page['series'][1]['items']), [self.posts[0]['url'], self.url])
        self.assertIn('PLone', page['series'][0]['url'])

    def test_explicit_episode_series_and_no_series(self):
        for i, p in enumerate(self.posts):
            p.update(series='show', episode=[1, 3 - i])
        page = self.page()
        self.assertEqual(self.urls(page['series'][0]['items']), self.urls(self.posts[::-1]))
        self.posts[1]['no_series'] = True
        server.ranked_cache.clear()
        self.assertEqual(self.page()['series'], [])

    def test_nonseries_youtube_posts_do_not_use_numbered_titles(self):
        for p in self.posts:
            p['youtube'] = p['url']
        self.assertEqual(self.page()['series'], [])

    def test_similar_posts_use_current_weights_and_exclude_other_subscriptions(self):
        other_sub = 'https://other.example/feed'
        server.FEEDS_FILE.write_text(self.sub + '\n' + other_sub)
        server.subscriptions[other_sub] = {'title': 'Other', 'items': [dict(self.posts[0], url='https://other.example/post')]}
        self.posts[0]['estimated_minutes'] = 20
        weights = dict(ranking.WEIGHTS, similar_base_percent=0, similar_publish_weight=0, similar_length_weight=1)
        page = self.page(weights=weights)
        self.assertEqual(self.urls(page['similar']['items']), [self.posts[2]['url'], self.posts[0]['url']])
        self.assertEqual([p['related_percent'] for p in page['similar']['items']], [100, 50])
        weights['similar_length_weight'] = 0
        self.assertEqual(self.page(weights=weights)['similar'], {'items': [], 'total': 0})

    def test_limits_do_not_hide_target_or_memberships(self):
        page = self.page(url=self.posts[2]['url'], limit=1)
        self.assertEqual(page['item']['url'], self.posts[2]['url'])
        self.assertEqual(len(page['series'][0]['items']), 1)
        self.assertEqual(page['series'][0]['total'], 3)
        self.assertEqual(len(page['similar']['items']), 1)
        self.assertEqual(page['similar']['total'], 2)
        self.assertEqual(len(self.page(limit=3)['series'][0]['items']), 3)

    def test_scores_and_view_state_do_not_change_cached_main_feed(self):
        before = copy.deepcopy(server.feed('score'))
        page = self.page()
        self.assertEqual(page['item']['score'], ranking.WEIGHTS['series_penalty'])
        self.assertEqual(server.feed('score'), before)
        server.set_viewed(self.url)
        page = self.page()
        self.assertIsNotNone(page['item']['last_viewed'])
        for group in page['series']:
            own = next(p for p in group['items'] if p['url'] == self.url)
            self.assertEqual(own['last_viewed'], page['item']['last_viewed'])
            self.assertEqual(own['score'], page['item']['score'])

    def test_saved_link_has_card_and_empty_related_feeds(self):
        link = 'https://example.com/saved?x=1&y=2#fragment'
        server.LINKS_FILE.write_text(link)
        server.cache[link] = dict(url=link, title='Saved')
        page = self.page(url=link)
        self.assertEqual(page['item']['title'], 'Saved')
        self.assertEqual(page['series'], [])
        self.assertEqual(page['similar']['items'], [])

    def test_enrichment_happens_only_when_requested(self):
        with patch.object(server, 'enrich_posts') as enrich:
            self.page()
            enrich.assert_not_called()
            self.page(enrich=True)
            self.assertEqual(enrich.call_args.args[0][0]['url'], self.url)
            self.assertEqual(enrich.call_args.args[1], ranking.WEIGHTS['enrich_count'])

    def test_http_routes_validation_and_etag(self):
        httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        httpd.ranker = 'score'
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        base = f'http://127.0.0.1:{httpd.server_port}'
        query = '/api/post?' + urllib.parse.urlencode({'url': self.url, 'limit': 1})
        with urllib.request.urlopen(base + query) as response:
            tag = response.headers['ETag']
            page = json.load(response)
        self.assertEqual(page['item']['url'], self.url)
        self.assertEqual(len(page['series'][0]['items']), 1)
        request = urllib.request.Request(base + query, headers={'If-None-Match': tag})
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        self.assertEqual(error.exception.code, 304)
        for path, code in [('/api/post', 400), ('/api/post?url=missing', 404),
                           ('/api/post?url=x&ranker=missing', 400), ('/api/post?url=x&weights={}', 400)]:
            with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(base + path)
            self.assertEqual(error.exception.code, code)
        with urllib.request.urlopen(base + '/post?' + urllib.parse.urlencode({'url': self.url})) as response:
            self.assertEqual(response.status, 200)
            self.assertIn('Post & related', response.read().decode())
