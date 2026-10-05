"""Browser evidence: parsing real adapter output, capture/replay verdicts, agent wiring."""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agentharness import browser, report
from agentharness.agent import Agent, AgentConfig
from agentharness.tests.test_agent import Base, ScriptedClient

# Verbatim output captured from chrome-devtools-mcp (2026-10-05) for a page with a console error,
# a warning, a missing image, a failed fetch and a missing favicon.
PAGES = '## Pages\n1: about:blank\n2: probe (http://127.0.0.1:8771/) [selected]'
CONSOLE = '''## Console messages
Showing 1-4 of 4 (Page 1 of 1).
msgid=1 [error] Failed to load resource: the server responded with a status of 404 (File not found) (0 args)
msgid=2 [error] boom: widget failed (1 args)
msgid=3 [warn] slow render (1 args)
msgid=4 [error] Failed to load resource: the server responded with a status of 404 (File not found) (0 args) [2 times]
msgid=5 [log] hello (1 args)'''
NETWORK = '''## Network requests
Showing 1-4 of 4 (Page 1 of 4).
reqid=1 GET http://127.0.0.1:8771/ [200]
reqid=2 GET http://127.0.0.1:8771/nope.png [404]
reqid=3 GET http://127.0.0.1:8771/api/missing [404]
reqid=4 GET http://127.0.0.1:8771/blocked.js [net::ERR_FAILED]'''
CLEAN = {'new_page': PAGES, 'list_console_messages': '## Console messages\n<no console messages found>',
         'list_network_requests': '## Network requests\nreqid=1 GET http://127.0.0.1:8771/ [200]',
         'close_page': 'closed'}


class FakeAdapter:
    def __init__(self, outputs):
        self.outputs, self.calls = dict(outputs), []

    def __call__(self, tool, args):
        self.calls.append((tool, args))
        return self.outputs[tool]


BROKEN = {**CLEAN, 'list_console_messages': CONSOLE, 'list_network_requests': NETWORK}


class Parsing(unittest.TestCase):
    def test_selected_page_and_title(self):
        self.assertEqual(browser.parse_selected_page(PAGES), (2, 'probe'))
        with self.assertRaises(browser.BrowserError):
            browser.parse_selected_page('## Pages\n1: about:blank')

    def test_console_keeps_errors_and_warnings_collapses_repeats_and_drops_noise(self):
        self.assertEqual(browser.parse_console(CONSOLE), [
            'error: Failed to load resource: the server responded with a status of 404 (File not found)',
            'error: boom: widget failed', 'warn: slow render'])

    def test_network_keeps_http_errors_and_transport_failures_only(self):
        self.assertEqual(browser.parse_network(NETWORK), [
            'GET http://127.0.0.1:8771/nope.png 404', 'GET http://127.0.0.1:8771/api/missing 404',
            'GET http://127.0.0.1:8771/blocked.js net::ERR_FAILED'])


class CaptureAndReplay(unittest.TestCase):
    def test_capture_records_problems_and_closes_the_tab(self):
        adapter = FakeAdapter(BROKEN)
        evidence = browser.capture(adapter, 'http://127.0.0.1:8771/', 'before')
        self.assertEqual(evidence.title, 'probe')
        self.assertEqual(len(evidence.console), 3)
        self.assertEqual(len(evidence.failed_requests), 3)
        self.assertEqual(adapter.calls[-1], ('close_page', {'pageId': 2}))
        self.assertEqual([c[0] for c in adapter.calls][:3], ['new_page', 'list_console_messages', 'list_network_requests'])

    def test_tab_is_closed_even_when_reading_fails(self):
        adapter = FakeAdapter({**CLEAN, 'list_console_messages': 'ERROR: MCP server crashed'})
        with self.assertRaisesRegex(browser.BrowserError, 'reading console failed'):
            browser.capture(adapter, 'http://localhost:3000/', 'x')
        self.assertEqual(adapter.calls[-1][0], 'close_page')

    def test_only_local_pages_and_safe_labels_are_accepted(self):
        adapter = FakeAdapter(CLEAN)
        for url in ('https://example.com/', 'http://127.0.0.1.evil.com/', 'file:///etc/passwd'):
            with self.assertRaisesRegex(browser.BrowserError, 'local'):
                browser.capture(adapter, url, 'ok')
        with self.assertRaisesRegex(browser.BrowserError, 'label'):
            browser.capture(adapter, 'http://localhost/', '../escape')
        self.assertEqual(adapter.calls, [], 'a rejected request must not touch the browser')

    def test_replay_verdicts(self):
        before = browser.capture(FakeAdapter(BROKEN), 'http://127.0.0.1:8771/', 'before')
        fixed = browser.capture(FakeAdapter(CLEAN), 'http://127.0.0.1:8771/', 'fixed')
        self.assertEqual(browser.compare(before, fixed).verdict, 'fixed')
        partial = browser.capture(FakeAdapter({**CLEAN, 'list_console_messages': CONSOLE}),
                                  'http://127.0.0.1:8771/', 'partial')
        self.assertEqual(browser.compare(before, partial).verdict, 'improved')
        self.assertEqual(browser.compare(before, before).verdict, 'unchanged')
        worse = browser.capture(FakeAdapter({**BROKEN, 'list_console_messages':
                                             CONSOLE + '\nmsgid=9 [error] new crash (0 args)'}),
                                'http://127.0.0.1:8771/', 'worse')
        replay = browser.compare(before, worse)
        self.assertEqual(replay.verdict, 'regressed')
        self.assertEqual(replay.introduced, ['error: new crash'])
        self.assertEqual(browser.compare(fixed, fixed).verdict, 'no_problems_seen')

    def test_saved_captures_round_trip_and_unknown_labels_are_errors(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        evidence = browser.capture(FakeAdapter(BROKEN), 'http://127.0.0.1:8771/', 'before')
        browser.save(tmp, evidence)
        self.assertEqual(browser.load(tmp, 'before'), evidence)
        with self.assertRaisesRegex(browser.BrowserError, 'no saved capture'):
            browser.load(tmp, 'missing')


class FakeBus:
    def __init__(self, outputs, tools=browser.REQUIRED_TOOLS):
        self.discovered = {browser.PREFIX + t: ('chrome-devtools', {'name': t}) for t in tools}
        self.outputs = outputs

    def tools(self):
        return {}

    def summary(self):
        return 'chrome-devtools'

    def call(self, name, args):
        return self.outputs[name.removeprefix(browser.PREFIX)]


class AgentWiring(Base):
    def agent_with(self, bus, steps):
        agent = Agent(ScriptedClient(steps), self.ws, config=AgentConfig(require_approval=False, plan_first=False))
        if browser.available(bus):
            agent.mcp = bus
            agent.tools.update(browser.tools())
        return agent

    def test_tools_require_every_adapter_tool(self):
        self.assertTrue(browser.available(FakeBus(CLEAN)))
        self.assertFalse(browser.available(FakeBus(CLEAN, tools=('new_page',))))
        self.assertFalse(browser.available(None))

    def test_capture_then_replay_through_the_agent_loop_saves_evidence(self):
        bus = FakeBus(BROKEN)
        steps = [[('browser_capture', {'url': 'http://127.0.0.1:8771/', 'label': 'before'})],
                 lambda m: (setattr(bus, 'outputs', CLEAN), [('browser_replay', {
                     'url': 'http://127.0.0.1:8771/', 'label': 'after', 'baseline': 'before'})])[1],
                 [('respond', {'message': 'verified in browser'})]]
        agent = self.agent_with(bus, steps)
        result = agent.run('fix the widget')
        self.assertEqual(result.status, 'answered')
        evidence = Path(result.evidence_dir) / 'browser'
        self.assertEqual({p.stem for p in evidence.glob('*.json')}, {'before', 'after'})
        self.assertIn('fixed', json.dumps(agent.messages))

    def test_report_lists_browser_captures(self):
        bus = FakeBus(BROKEN)
        agent = self.agent_with(bus, [[('browser_capture', {'url': 'http://localhost:3000/', 'label': 'before'})],
                                      [('respond', {'message': 'ok'})]])
        result = agent.run('look at the page')
        page = report.render(Path(result.evidence_dir))
        self.assertIn('Browser captures', page)
        self.assertIn('before', page)


if __name__ == '__main__':
    unittest.main()
