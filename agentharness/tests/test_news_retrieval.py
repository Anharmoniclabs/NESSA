import json
import unittest
from datetime import datetime, timezone, timedelta
from email.utils import format_datetime
from unittest.mock import patch
from xml.sax.saxutils import escape

from agentharness import online
from agentharness.workspace import ToolError


def feed(items):
    return '<rss><channel>' + ''.join('<item>'+''.join(f'<{k}>{escape(v)}</{k}>' for k,v in row.items())+'</item>' for row in items)+'</channel></rss>'


class NewsRetrieval(unittest.TestCase):
    def test_generic_homepages_are_rejected_for_ai_query(self):
        rss=feed([{'title':'Latest News Headlines', 'link':'https://example.com/', 'description':'Breaking news worldwide'}])
        with patch.object(online,'fetch',return_value=(rss,'text/xml')):
            with self.assertRaises(ToolError): online.web_search('latest news in AI')

    def test_news_uses_topic_dates_articles_and_direct_urls(self):
        now=datetime.now(timezone.utc)
        rows=[{'title':'New AI model released', 'link':'https://www.bing.com/news/apiclick.aspx?url=https%3A%2F%2Fexample.com%2Fai-model',
               'description':'An artificial intelligence model was released.', 'pubDate':format_datetime(now)},
              {'title':'AI old announcement', 'link':'https://example.com/old', 'description':'Artificial intelligence', 'pubDate':format_datetime(now-timedelta(days=30))},
              {'title':'Sports results', 'link':'https://example.com/sport', 'description':'Football', 'pubDate':format_datetime(now)}]
        def get(url):
            if '/news/search?' in url: return feed(rows),'text/xml'
            return '<html><meta name="description" content="A new artificial intelligence model was released today."></html>','text/html'
        with patch.object(online,'fetch',side_effect=get) as request:
            result=json.loads(online.web_search('latest news in AI'))
        self.assertEqual(result['kind'],'news')
        self.assertEqual(len(result['results']),1)
        row=result['results'][0]
        self.assertEqual(row['url'],'https://example.com/ai-model')
        self.assertIn('published',row)
        self.assertEqual(row['page_status'],'fetched')
        self.assertIn('artificial',request.call_args_list[0].args[0])

    def test_failed_article_fetch_is_explicit(self):
        rss=feed([{'title':'AI model release','link':'https://example.com/release',
                  'description':'Artificial intelligence release','pubDate':format_datetime(datetime.now(timezone.utc))}])
        with patch.object(online,'fetch',side_effect=[(rss,'text/xml'),ToolError('HTTP 403')]):
            result=json.loads(online.web_search('latest AI news'))
        self.assertEqual(result['results'][0]['page_status'],'unavailable')
        self.assertIn('403',result['results'][0]['page_error'])

    def test_general_search_keeps_topic(self):
        rss=feed([{'title':'Official definition','link':'https://example.com/official','description':'Dictionary entry'}])
        with patch.object(online,'fetch',return_value=(rss,'text/xml')):
            with self.assertRaises(ToolError): online.web_search('official Python pathlib documentation')

    def test_related_coverage_is_grouped(self):
        rows=[{'title':title,'link':'https://example.com/'+str(i),'description':'Artificial intelligence announcement',
               'pubDate':format_datetime(datetime.now(timezone.utc))} for i,title in enumerate([
                   'OpenAI announces new artificial intelligence model',
                   'OpenAI announces its new AI model',
                   'Google launches artificial intelligence research lab'])]
        with patch.object(online,'fetch',side_effect=lambda url: (feed(rows),'text/xml') if '/news/search?' in url else
                          ('<meta name="description" content="Artificial intelligence announcement">','text/html')):
            result=json.loads(online.news_search('AI news'))
        self.assertEqual(len(result['results']),2)
        self.assertTrue(any(r.get('related_reports') for r in result['results']))
