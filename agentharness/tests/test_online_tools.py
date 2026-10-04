import json
import unittest
from unittest.mock import patch

from agentharness import online
from agentharness.tools import build_tools
from agentharness.workspace import ToolError


class OnlineTools(unittest.TestCase):
    def test_tools_are_read_only_and_not_cached(self):
        tools = build_tools()
        for name in ('web_search', 'web_fetch', 'weather'):
            self.assertEqual(tools[name].kind, 'read')
            self.assertFalse(tools[name].cacheable)

    def test_weather_has_source_time_and_units(self):
        replies = [json.dumps({'results': [{'name': 'New York', 'latitude': 40.7,
            'longitude': -74, 'country': 'United States'}]}), json.dumps({
            'timezone': 'America/New_York', 'current': {'time': '2026-10-04T13:00',
            'temperature_2m': 20, 'weather_code': 51}, 'current_units': {'temperature_2m': '°C'},
            'daily': {'time': ['2026-10-04'], 'temperature_2m_max': [22]}})]
        with patch.object(online, 'fetch', side_effect=[(x, 'application/json') for x in replies]):
            result = online.weather('NYC')
        self.assertIn('New York', result)
        self.assertIn('2026-10-04', result)
        self.assertIn('°C', result)
        self.assertIn('open-meteo.com', result)
        self.assertIn('Light drizzle', result)

    def test_search_returns_links_and_snippets(self):
        rss = '<rss><channel><item><title>Example</title><link>https://example.com/doc</link><description>Evidence</description></item></channel></rss>'
        with patch.object(online, 'fetch', return_value=(rss, 'application/rss+xml')):
            result = online.web_search('example')
        self.assertIn('https://example.com/doc', result)
        self.assertIn('Evidence', result)

    def test_bad_or_private_url_rejected(self):
        for url in ['file:///etc/passwd', 'http://127.0.0.1/', 'https://user:pass@example.com']:
            with self.subTest(url=url), self.assertRaises(ToolError):
                online.validate_url(url)

    def test_empty_search_does_not_claim_success(self):
        with patch.object(online, 'fetch', return_value=('<rss><channel/></rss>', 'text/xml')):
            with self.assertRaises(ToolError): online.web_search('example')
