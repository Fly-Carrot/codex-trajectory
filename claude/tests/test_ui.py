"""Run the page's own inline JavaScript helpers against SYNTHETIC events."""
import json
from pathlib import Path
import subprocess
import unittest

import support

HTML = Path(__file__).resolve().parents[1] / 'src/claude_trajectory/index.html'
NODE = support.node()


def script():
    return HTML.read_text()


@unittest.skipUnless(NODE, 'Node is needed to execute browser logic (set TRAJECTORY_NODE)')
class UILogicTests(unittest.TestCase):
    def run_js(self, body):
        result = subprocess.run([NODE, '-e', body], check=True, capture_output=True, text=True, timeout=30)
        return json.loads(result.stdout)

    def layout(self):
        source = script()
        return source[source.index('// TIMELINE_LAYOUT_BEGIN'):source.index('// TIMELINE_LAYOUT_END')]

    def test_deleted_session_status_label(self):
        source = script()
        start = source.index("let logState='available'")
        status = source[start:source.index('\n', source.index('function liveStatus', start))]
        body = 'let warningMessages=new Set();' + status + ''';const out=[];
logState='missing';out.push(liveStatus());releaseReason='delete';out.push(liveStatus());
logState='available';out.push(liveStatus());console.log(JSON.stringify(out))'''
        self.assertEqual(self.run_js(body), ['Transcript missing - index only', 'Deleted in Claude Code - index only', 'Live'])

    def test_fixed_lane_order(self):
        result = self.run_js(self.layout() + 'console.log(JSON.stringify(timelineLayout([]).rows.map(r=>r.label)))')
        self.assertEqual(result, ['User', 'Command', 'File', 'MCP', 'Subagents', 'Context', 'Plan', 'Search',
                                  'Other Tools', 'Tool Results', 'Artifacts', 'Agent'])

    def test_result_and_completion_projections_and_subagent_lane(self):
        events = [
            {'id': 'call:a', 'time': '2026-10-01T09:00:01Z', 'lane': 'tools', 'category': 'AGENT', 'title': 'Dispatch Explore',
             'output': 'launched', 'output_time': '2026-10-01T09:00:02Z', 'notification': 'Background task completed',
             'completion_time': '2026-10-01T09:00:09Z', 'state': 'completed'},
            {'id': 'call:s', 'time': '2026-10-01T09:00:05Z', 'lane': 'subagent', 'category': 'AGENT', 'title': 'Explore · ls',
             'output': 'ok', 'output_time': '2026-10-01T09:00:06Z', 'state': 'returned'},
            {'id': 'text:m:0', 'time': '2026-10-01T09:00:10Z', 'lane': 'activity', 'category': 'ASSISTANT', 'title': 'Agent reply',
             'artifacts': [{'id': 'r', 'name': 'report.md', 'path': 'report.md', 'size': 3, 'evidence': 'x'}]}]
        body = (self.layout() + 'const items=timelineItems(' + json.dumps(events) + ');'
                'console.log(JSON.stringify(items.map(e=>[e.id,timelineCategory(e)])))')
        self.assertEqual(self.run_js(body), [
            ['call:a', 'AGENT'], ['output:call:a', 'OUTPUT'], ['call:s', 'AGENT'], ['completion:call:a', 'OUTPUT'],
            ['text:m:0', 'ASSISTANT'], ['artifact:text:m:0:r', 'ARTIFACT']])

    def test_opening_details_does_not_touch_timeline_follow(self):
        source = script()
        for name in ('function showEvent(', 'async function fetchDetail(', 'function inspectSelected(', 'function inspect('):
            start = source.index(name)
            body = source[start:source.index('\n}', start)]
            self.assertNotIn('setFollow', body, name)
            self.assertNotIn('follow=', body.replace('ledgerFollow=', ''), name)

    def recovery(self, body):
        source = script()
        helper = source[source.index('function retryDelay()'):source.index('reconnectButton.onclick=')]
        setup = """
let failures=0,reconnect=false,paused=false,timer=null,calls=0;
let selected='old-event',follow=false,events=[{id:'old-event'}];
const recoveryURL='claude-trajectory://reconnect/session-a';
const location={href:'http://127.0.0.1:1234/s/session-a#expired'};
const document={hidden:false};
const nodes={recovery:{hidden:true},'recovery-message':{textContent:''}};
const $=id=>nodes[id];
function poll(){calls++}
"""
        return self.run_js(setup + helper + body)

    def test_reconnect_keeps_reading_state_and_backoff_is_bounded(self):
        result = self.recovery("reconnectPanel();const delays=[0,1,2,3,20].map(n=>{failures=n;return retryDelay()});"
                               "console.log(JSON.stringify({calls,selected,follow,delays}))")
        self.assertEqual(result, {'calls': 1, 'selected': 'old-event', 'follow': False,
                                  'delays': [1500, 3000, 6000, 12000, 30000]})

    def test_expired_link_uses_own_protocol_only_on_click(self):
        result = self.recovery("reconnect=true;reconnectPanel();console.log(JSON.stringify({href:location.href,calls}))")
        self.assertEqual(result, {'href': 'claude-trajectory://reconnect/session-a', 'calls': 0})
        source = script()
        self.assertNotIn('codex-trajectory://', source)
        self.assertNotIn('is_wrapper', source)
        self.assertEqual(source.count('location.href=recoveryURL'), 1)

    def test_failure_messages_are_distinct(self):
        source = script()
        report = source[source.index('function reportError('):source.index('async function requestEvents(')]
        request = source[source.index('async function requestEvents('):source.index('function mergePage(')]
        body = ("let reconnect=false,token='t',failure='';const nodes={status:{textContent:'',classList:{add(){}}},"
                "recovery:{hidden:true},'recovery-message':{textContent:''}};const $=id=>nodes[id];function historyControls(){}"
                + report + "const out={};for(const [r,f] of [[true,'auth'],[false,'offline'],[false,'unavailable']]){reconnect=r;failure=f;"
                "reportError(Error('x'));out[f]=nodes['recovery-message'].textContent}console.log(JSON.stringify(out))")
        messages = self.run_js(body)
        self.assertEqual(len(set(messages.values())), 3)
        self.assertIn('no longer valid', messages['auth'])
        self.assertIn('not reachable', messages['offline'])
        self.assertIn('No other session', messages['unavailable'])
        self.assertIn("failure='offline'", request)
        self.assertIn("r.status===401", request)

    def test_page_never_renders_log_text_as_html(self):
        source = script()
        self.assertNotIn('innerHTML', source)
        self.assertNotIn('insertAdjacentHTML', source)
        self.assertNotIn('eval(', source)
        self.assertIn("redirect:'error'", source)

    def test_auth_bootstrap_keeps_fragment_and_no_query_token(self):
        source = script()
        boot = source[source.index('const threadPath='):source.index('const endpoint=')]
        body = ("const vm=require('node:vm');const location=new URL('http://127.0.0.1:9/s/sess-1#tok');"
                "location.reload=()=>{};const store={};const ctx={location,window:{addEventListener(){}},"
                "sessionStorage:{getItem:k=>store[k]||null,setItem:(k,v)=>{store[k]=v}},history:{replaceState(){}}};"
                "vm.createContext(ctx);vm.runInContext(" + json.dumps(boot + ';globalThis.out={token,path:threadPath[1]}') + ",ctx);"
                "console.log(JSON.stringify(ctx.out))")
        self.assertEqual(self.run_js(body), {'token': 'tok', 'path': 'sess-1'})


if __name__ == '__main__':
    unittest.main()
