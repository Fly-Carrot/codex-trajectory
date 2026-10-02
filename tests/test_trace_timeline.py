"""Run the actual inline timeline layout helper against compact event fixtures."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest


HTML = Path(__file__).resolve().parents[1] / 'src/trace_viewer/index.html'


@unittest.skipUnless(shutil.which('node'), 'Node is needed to execute browser layout logic')
class TimelineLayoutTests(unittest.TestCase):
    def test_output_projection_is_linked_and_does_not_inflate_operations(self):
        source = HTML.read_text()
        helper = source.split('// TIMELINE_LAYOUT_BEGIN', 1)[1].split('// TIMELINE_LAYOUT_END', 1)[0]
        fixture = [dict(id='c1', lane='tools', category='COMMAND', title='echo hello',
                        time='2026-10-03T01:00:00Z', output_time='2026-10-03T01:00:02Z', output='hello'),
                   dict(id='c2', lane='tools', category='TOOL', title='pending', time='2026-10-03T01:00:01Z')]
        result = subprocess.run(['node','-e', helper+'\nconsole.log(JSON.stringify(timelineItems('+json.dumps(fixture)+')));'],
                                capture_output=True, text=True, check=True)
        events = json.loads(result.stdout)
        self.assertEqual(len(events), 3)
        self.assertEqual(sum(e['lane']=='tools' for e in events), 2)
        self.assertEqual(events[-1]['category'], 'OUTPUT')
        self.assertEqual(events[-1]['source_event_id'], 'c1')
        self.assertEqual(events[-1]['time'], fixture[0]['output_time'])
        self.assertEqual(self.layout(events)['rows'][-1]['label'], 'Output')

    def layout(self, events):
        source = HTML.read_text()
        self.assertIn('// TIMELINE_LAYOUT_BEGIN', source)
        helper = source.split('// TIMELINE_LAYOUT_BEGIN', 1)[1].split('// TIMELINE_LAYOUT_END', 1)[0]
        result = subprocess.run(
            ['node', '-e', helper + '\nconsole.log(JSON.stringify(timelineLayout(' + json.dumps(events) + ')));'],
            check=True, capture_output=True, text=True,
        )
        return json.loads(result.stdout)

    def test_categories_have_distinct_rows_and_stable_order(self):
        events = [{'category': c} for c in ['MCP', 'FILE', 'USER', 'COMMAND', 'ASSISTANT', 'AGENT', 'CONTEXT', 'TOOL', 'PLAN', 'SEARCH']]
        layout = self.layout(events)
        self.assertEqual([r['category'] for r in layout['rows']],
                         ['USER', 'ASSISTANT', 'COMMAND', 'FILE', 'MCP', 'AGENT', 'CONTEXT', 'PLAN', 'SEARCH', 'TOOL'])
        self.assertEqual(layout, self.layout(list(reversed(events))))
        self.assertEqual(len({r['top'] for r in layout['rows']}), 10)

    def test_empty_categories_hidden_and_turns_span_rows(self):
        layout = self.layout([{'category': 'USER'}, {'category': 'TURN', 'lane': 'boundary'}, {'category': 'COMMAND', 'state': 'failed'}])
        self.assertEqual([r['category'] for r in layout['rows']], ['USER', 'COMMAND'])
        self.assertGreater(layout['boundaryHeight'], layout['rows'][-1]['top'] + 8)
        self.assertGreater(layout['axisTop'], layout['boundaryHeight'])
        self.assertGreater(layout['height'], layout['axisTop'] + 10)

    def test_future_tool_category_falls_back_without_an_error_lane(self):
        layout = self.layout([{'category': 'FUTURE_TOOL'}, {'category': 'COMMAND', 'state': 'error'}, {'category': 'TOOL'}])
        self.assertEqual([r['label'] for r in layout['rows']], ['Command', 'Other Tools'])

    def test_empty_and_live_growth(self):
        empty = self.layout([])
        one = self.layout([{'category': 'ASSISTANT'}])
        two = self.layout([{'category': 'ASSISTANT'}, {'category': 'MCP'}])
        self.assertEqual(empty['rows'], [])
        self.assertLess(empty['height'], one['height'])
        self.assertLess(one['height'], two['height'])
        self.assertEqual(one['rows'][0], two['rows'][0])

    def test_mobile_inspector_uses_content_relative_position(self):
        source = HTML.read_text()
        self.assertNotIn('top:151px', source)
        self.assertIn('.content{position:relative;', source)
        self.assertIn('.mark.boundary{width:1px!important;padding:0;border:0;', source)


if __name__ == '__main__':
    unittest.main()
