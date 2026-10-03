"""Run the actual inline timeline layout helper against compact event fixtures."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest


HTML = Path(__file__).resolve().parents[1] / 'src/trace_viewer/index.html'


@unittest.skipUnless(shutil.which('node'), 'Node is needed to execute browser layout logic')
class TimelineLayoutTests(unittest.TestCase):
    def test_all_lanes_fixed_even_without_events(self):
        expected = ['User','Command','File','MCP','Subagents','Context','Plan','Search','Other Tools','Tool Results','Artifacts','Agent']
        for events in ([], [{'category':'ASSISTANT'}], [{'category':'AGENT'}]):
            self.assertEqual([r['label'] for r in self.layout(events)['rows']], expected)
        self.assertNotIn('No recent activity', HTML.read_text())

    def test_subagents_stays_visible_without_becoming_main_agent(self):
        for items in ([], [{'category':'ASSISTANT','reply_phase':'Final'}]):
            rows = self.layout(items)['rows']
            subagents = [r for r in rows if r['label']=='Subagents']
            self.assertEqual(len(subagents), 1)
            self.assertEqual(subagents[0]['category'], 'AGENT')
            if items:
                self.assertEqual(rows[-1]['label'], 'Agent')
                self.assertEqual(rows[-1]['category'], 'ASSISTANT')

    def interaction(self, body):
        source = HTML.read_text()
        setup = '''
let follow=true, ledgerFollow=true, selected=null, pendingDetails=false;
const nodes={inspector:{hidden:true},follow:{setAttribute(){}},search:{value:''}};
const $=id=>nodes[id];
const ledger={scrollTop:0,offsetTop:0,clientHeight:100,scrollHeight:1000,addEventListener:(name,fn)=>handlers[name]=fn};
const handlers={};
const document={querySelectorAll:()=>[],getElementById:()=>({offsetTop:200})};
function inspect(){} function draw(){}
const visibleEvents=[],ROW_HEIGHT=30,viewport={addEventListener(){}};
function renderLedger(){} function saveView(){} function fetchDetail(){}
'''
        functions = source[source.index('function setFollow'):source.index('function showById')]
        # Exercise real handlers with a minimal DOM, not a duplicate state model.
        functions = functions[:functions.index('function inspect')] + functions[functions.index('function showEvent'):]
        handlers = source[source.index("ledger.addEventListener('wheel'"):source.index("$('zoom-in').onclick")]
        setup = setup.replace("search:{value:''}", "search:{value:''},pause:{}")
        result = subprocess.run(['node','-e',setup+functions+handlers+body],check=True,capture_output=True,text=True)
        return json.loads(result.stdout)

    def test_inspection_keeps_timeline_follow_state(self):
        state = self.interaction("showEvent({id:'old'}); handlers.scroll(); console.log(JSON.stringify({follow,selected,hidden:nodes.inspector.hidden}));")
        self.assertEqual(state, dict(follow=True,selected='old',hidden=False))
        state = self.interaction("follow=false; showEvent({id:'old'}); console.log(JSON.stringify({follow}));")
        self.assertFalse(state['follow'])

    def test_follow_button_keeps_open_details(self):
        state = self.interaction("showEvent({id:'old'}); follow=false; nodes.follow.onclick(); console.log(JSON.stringify({follow,selected,hidden:nodes.inspector.hidden}));")
        self.assertEqual(state, dict(follow=True,selected='old',hidden=False))

    def test_ledger_browsing_does_not_disable_timeline_follow(self):
        state = self.interaction("handlers.wheel({deltaY:-10}); handlers.touchstart(); handlers.keydown({key:'Home'}); handlers.scroll(); console.log(JSON.stringify({follow,ledgerFollow}));")
        self.assertEqual(state, dict(follow=True,ledgerFollow=False))
        source = HTML.read_text()
        self.assertIn("ledgerFollow&&$('inspector').hidden", source)
        self.assertIn('viewport.scrollLeft=follow?viewport.scrollWidth:oldLeft', source)

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
        self.assertEqual(self.layout(events)['rows'][-3]['label'], 'Tool Results')

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
        events = [{'category': c} for c in ['MCP', 'FILE', 'USER', 'COMMAND', 'ASSISTANT', 'AGENT', 'CONTEXT', 'TOOL', 'PLAN', 'SEARCH', 'OUTPUT', 'ARTIFACT']]
        layout = self.layout(events)
        self.assertEqual([r['category'] for r in layout['rows']],
                         ['USER', 'COMMAND', 'FILE', 'MCP', 'AGENT', 'CONTEXT', 'PLAN', 'SEARCH', 'TOOL', 'OUTPUT', 'ARTIFACT', 'ASSISTANT'])
        self.assertEqual(layout, self.layout(list(reversed(events))))
        self.assertEqual(len({r['top'] for r in layout['rows']}), 12)
        self.assertEqual([r['label'] for r in layout['rows']][-2:], ['Artifacts', 'Agent'])
        self.assertEqual(layout['rows'][4]['label'], 'Subagents')

    def test_turns_span_all_fixed_rows(self):
        layout = self.layout([{'category': 'USER'}, {'category': 'TURN', 'lane': 'boundary'}, {'category': 'COMMAND', 'state': 'failed'}])
        self.assertEqual(len(layout['rows']), 12)
        self.assertGreater(layout['boundaryHeight'], layout['rows'][-1]['top'] + 8)
        self.assertGreater(layout['axisTop'], layout['boundaryHeight'])
        self.assertGreater(layout['height'], layout['axisTop'] + 10)

    def test_future_tool_category_falls_back_without_an_error_lane(self):
        layout = self.layout([{'category': 'FUTURE_TOOL'}, {'category': 'COMMAND', 'state': 'error'}, {'category': 'TOOL'}])
        self.assertEqual(layout, self.layout([]))

    def test_empty_and_live_growth(self):
        empty = self.layout([])
        one = self.layout([{'category': 'ASSISTANT'}])
        two = self.layout([{'category': 'ASSISTANT'}, {'category': 'MCP'}])
        self.assertEqual(len(empty['rows']), 12)
        self.assertEqual(empty, one)
        self.assertEqual(one, two)
        self.assertEqual(one['rows'][-1]['category'], two['rows'][-1]['category'])

    def test_wrappers_fold_results_but_preserve_errors_and_inspection(self):
        source = HTML.read_text()
        helper = source.split('// TIMELINE_LAYOUT_BEGIN', 1)[1].split('// TIMELINE_LAYOUT_END', 1)[0]
        fixture = [dict(id='a', lane='tools', category='TOOL', title='exec', is_wrapper=True,
                        time='2026-10-03T00:00:00Z', output_time='2026-10-03T00:00:01Z', output='result', state='completed')]
        def run(items, show=False):
            result = subprocess.run(['node','-e',helper+'\nconsole.log(JSON.stringify(timelineItems('+json.dumps(items)+','+json.dumps(show)+')));'],check=True,capture_output=True,text=True)
            return json.loads(result.stdout)
        self.assertEqual(len(run(fixture)), 1)
        self.assertEqual(run(fixture)[0]['output'], 'result')
        self.assertEqual(len(run(fixture, True)), 2)
        fixture[0]['state']='failed'
        self.assertEqual(len(run(fixture)), 2)
        fixture[0]['state']='returned'
        self.assertEqual(len(run(fixture)), 2)
        fixture[0]['output']='Script completed\nNested tool failed'
        self.assertEqual(len(run(fixture)), 2)

    def test_artifact_projection_does_not_duplicate_agent_or_calls(self):
        source = HTML.read_text()
        helper = source.split('// TIMELINE_LAYOUT_BEGIN', 1)[1].split('// TIMELINE_LAYOUT_END', 1)[0]
        fixture = [dict(id='reply',lane='activity',category='ASSISTANT',reply_phase='Final',
                        time='2026-10-03T00:00:00Z',artifacts=[dict(id='f',name='report.html',path='report.html',size=7,evidence='Exists; creation not verified')])]
        result = subprocess.run(['node','-e',helper+'\nconsole.log(JSON.stringify(timelineItems('+json.dumps(fixture)+')));'],check=True,capture_output=True,text=True)
        events = json.loads(result.stdout)
        self.assertEqual([e['category'] for e in events], ['ASSISTANT','ARTIFACT'])
        self.assertEqual([r['label'] for r in self.layout(events)['rows']][-2:], ['Artifacts','Agent'])
        self.assertEqual(events[-1]['source_event_id'], 'reply')

    def test_mobile_inspector_uses_content_relative_position(self):
        source = HTML.read_text()
        self.assertNotIn('top:151px', source)
        self.assertIn('.content{position:relative;', source)
        self.assertIn('.mark.boundary{width:1px!important;padding:0;border:0;', source)


@unittest.skipUnless(shutil.which('node'), 'Node is needed for frontend execution')
class HistoryFrontendTests(unittest.TestCase):
    """Execute the shipped script with a small DOM and deterministic HTTP queue."""

    def run_frontend(self, body, saved=None):
        script = HTML.read_text().split('<script>', 1)[1].split('</script>', 1)[0]
        # Leave the real polling function intact, but drive its clock from the test.
        script = script.rsplit('poll();', 1)[0]
        harness = r'''
const assert=require('node:assert/strict');
const storage=new Map(),requests=[],responses=[],timers=[];
class Element {
 constructor(tag='div'){this.tagName=tag;this.children=[];this.style={setProperty(){}};this.dataset={};this.attrs={};this.hidden=false;this.value='';this.scrollTop=0;this.scrollLeft=0;this.clientHeight=300;this.clientWidth=800;this.offsetTop=0;this.handlers={};this.classes=new Set();this.classList={add:x=>this.classes.add(x),remove:x=>this.classes.delete(x),toggle:(x,v)=>v?this.classes.add(x):this.classes.delete(x)};}
 set textContent(v){this.text=v;this.children=[]} get textContent(){return this.text||''}
 get firstChild(){return this.children[0]||null}
 get nextSibling(){const a=this.parent?.children||[];return a[a.indexOf(this)+1]||null}
 get scrollHeight(){return this.children.reduce((n,c)=>n+(c.className==='virtual-spacer'?parseFloat(c.style.height)||0:30),0)}
 get scrollWidth(){return parseFloat(this.children[0]?.style.width)||800}
 get offsetWidth(){return parseFloat(this.style.width)||800}
 append(...items){for(const n of items)this.insertBefore(n,null)}
 insertBefore(n,c){if(n.parent)n.parent.removeChild(n);const i=c?this.children.indexOf(c):this.children.length;this.children.splice(i,0,n);n.parent=this}
 removeChild(n){this.children.splice(this.children.indexOf(n),1);n.parent=null}
 replaceChildren(...items){for(const n of this.children)n.parent=null;this.children=[];this.append(...items)}
 setAttribute(k,v){this.attrs[k]=v} addEventListener(k,fn){this.handlers[k]=fn}
}
const nodes=new Map();
const document={hidden:false,getElementById(id){if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id)},createElement:t=>new Element(t),createTextNode:t=>{const n=new Element();n.textContent=t;return n},querySelectorAll(){return [...nodes.values()].flatMap(n=>n.children).filter(n=>n.dataset.event)},addEventListener(){}};
document.getElementById('inspector').hidden=true;document.getElementById('scale').value='events';
document.getElementById('timeline-scroll').append(document.getElementById('track'));
const window={addEventListener(){}},location={pathname:'/t/chat-a',hash:'#auth-secret'},history={replaceState(){}};
const sessionStorage={getItem(){return null},setItem(){}};
const localStorage={getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v)};
function setTimeout(fn,delay){timers.push({fn,delay});return timers.length}function clearTimeout(){}
async function fetch(url,options){requests.push({url,options});const next=responses.shift();if(!next)throw Error('Unexpected request '+url);return typeof next==='function'?next():{ok:!next.status,status:next.status||200,json:async()=>next}}
function event(n,extra={}){return {id:'e'+n,time:new Date(1700000000000+n*1000).toISOString(),lane:'activity',category:'ASSISTANT',title:'Event '+n,detail:'summary',state:'completed',...extra}}
function page(items,extra={}){return {events:items,version:1,thread_id:'chat-a',has_earlier:true,next_cursor:'opaque/older?cursor',indexing:false,...extra}}
'''
        harness += '\nstorage.set("trace-view:/t/chat-a",'+json.dumps(json.dumps(saved or {}))+');\n'
        result = subprocess.run(['node', '-e', harness+script+'\n(async()=>{'+body+'})().catch(e=>{console.error(e);process.exitCode=1});'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_merge_older_and_live_and_reopen_cursor_without_content(self):
        self.run_frontend('''
responses.push(page([event(10),event(11)]));await poll();
responses.push(page([event(8),event(9)]));await loadEarlier();
assert.deepEqual(canonicalEvents.map(e=>e.id),['e8','e9','e10','e11']);
responses.push(page([event(11,{detail:'changed'}),event(12)],{version:2}));await poll();
assert.equal(canonicalEvents.length,5);assert.equal(canonicalEvents[3].detail,'changed');
showEvent(events.find(e=>e.id==='e8'));saveView();
const saved=JSON.parse(storage.get(stateKey));assert.equal(saved.cursor,'opaque/older?cursor');
assert.equal(saved.selected,'e8');assert.equal(saved.follow,true);
assert.deepEqual(Object.keys(saved).sort(),['anchor','canonical_anchor','cursor','details','follow','ledgerFollow','offset','scrollLeft','scrollTop','selected'].sort());
assert(!storage.get(stateKey).includes('auth-secret'));assert(!storage.get(stateKey).includes('summary'));
''')

    def test_restore_older_page_then_default_latest_poll(self):
        self.run_frontend('''
responses.push(page([event(8),event(9)]));await poll();
assert.equal(requests[0].url,'/api/threads/chat-a/events?before=older%2F%3F');
assert.equal(selected,'e8');assert.equal(follow,false);assert.equal($('inspector').hidden,false);
assert.equal(viewport.scrollLeft,51);
responses.push(page([event(10)],{version:3}));await poll();
assert.equal(requests[1].url,'/api/threads/chat-a/events');assert.equal(canonicalEvents.length,3);
assert.equal(selected,'e8');assert.equal(follow,false);
''', saved=dict(cursor='older/?', selected='e8', details=True, follow=False, ledgerFollow=False, scrollLeft=51))

    def test_window_preserves_selection_and_slides_for_explicit_older_page(self):
        self.run_frontend('''
canonicalEvents=Array.from({length:1200},(_,i)=>event(i));selected='e0';historyHeld=true;
assert(mergePage([event(1201),event(1199,{detail:'updated'})],'latest'));
assert.equal(canonicalEvents.length,1200);assert.equal(canonicalEvents[0].id,'e0');
assert.equal(canonicalEvents.find(e=>e.id==='e1199').detail,'updated');assert(windowFull);
assert(canonicalEvents.some(e=>e.id==='e1201'));
assert.equal(mergePage([event(-1)],'older','c'),true);assert.equal(canonicalEvents.length,1200);
assert(canonicalEvents.some(e=>e.id==='e-1'));assert(canonicalEvents.some(e=>e.id==='e0'));
selected=null;historyHeld=false;ledgerFollow=true;mergePage([event(1201)],'latest');
assert.equal(canonicalEvents.length,1200);assert.equal(canonicalEvents.at(-1).id,'e1201');
''')

    def test_virtual_rows_spacers_search_and_timeline_limit(self):
        self.run_frontend('''
canonicalEvents=Array.from({length:1200},(_,i)=>event(i,{lane:'tools',category:'COMMAND',output:'done',output_time:event(i).time}));
ledgerFollow=false;refreshEvents();ledger.scrollTop=15000;renderLedger();
assert.equal(events.length,2400);assert(rowCache.size<=26);assert(ledger.children.length<=28);
assert.equal(ledger.scrollHeight,2400*30);assert.equal(ledger.children[0].style.height,(500-8)*30+'px');
assert.equal(track.children.filter(n=>n.dataset.event).length,1200);
$('search').value='Event 999';draw();assert.equal(visibleEvents.length,2);assert.equal(rowCache.size,2);
''')

    def test_detail_uses_canonical_id_and_survives_summary_redraw(self):
        self.run_frontend('''
canonicalEvents=[event(1,{id:'call/a?b',lane:'tools',category:'COMMAND',output:'short',output_time:event(2).time,detail_available:true})];refreshEvents();
const result=events.find(e=>e.category==='OUTPUT');selected=result.id;
responses.push({event:{...canonicalEvents[0],input:'full input',output:'FULL RESULT'}});await fetchDetail(result);
assert.equal(requests[0].url,'/api/threads/chat-a/events?detail=call%2Fa%3Fb');
assert.equal($('inspect-body').textContent,'RESULT PREVIEW\\nFULL RESULT');
$('inspector').hidden=false;draw();assert.equal($('inspect-body').textContent,'RESULT PREVIEW\\nFULL RESULT');
assert.equal(canonicalEvents[0].output,'short');
''')

    def test_stale_detail_does_not_replace_new_selection(self):
        self.run_frontend('''
let resolve;responses.push(()=>new Promise(r=>resolve=r));selected='e1';
const pending=fetchDetail(event(1,{detail_available:true}));
selected='e2';responses.push({event:event(2,{detail:'full two'})});await fetchDetail(event(2,{detail_available:true}));
resolve({ok:true,json:async()=>({event:event(1,{detail:'stale one'})})});await pending;
assert.equal(detailId,'e2');assert.equal($('inspect-body').textContent,'full two');
''')

    def test_hidden_and_reconnect_preserve_visible_history(self):
        self.run_frontend('''
responses.push(page([event(1)]));await poll();const previous=ledger.children;
document.hidden=true;await poll();assert.equal(requests.length,1);
document.hidden=false;responses.push({status:401});await poll();
assert.equal($('status').textContent,'Reconnect required');assert.equal(canonicalEvents[0].id,'e1');
assert.equal(ledger.children,previous);await poll();assert.equal(requests.length,2);
''')

    def test_legacy_server_and_latest_reset(self):
        self.run_frontend('''
responses.push({events:[event(1)],version:1,thread_id:'legacy'});await poll();assert.equal($('earlier').hidden,true);
selected='e1';historyHeld=true;windowFull=true;follow=false;
responses.push(page([event(20)],{version:20}));await latest();
assert.deepEqual(canonicalEvents.map(e=>e.id),['e20']);assert.equal(selected,null);
assert(follow);assert(ledgerFollow);assert(!historyHeld);assert(!windowFull);
assert.equal(JSON.parse(storage.get(stateKey)).cursor,null);
''')

    def test_indexing_and_page_error_do_not_discard_events(self):
        self.run_frontend('''
responses.push(page([event(1)],{indexing:true}));await poll();assert.equal($('history-note').textContent,'Indexing history…');
assert($('earlier').disabled);await loadEarlier();assert.equal(requests.length,1);
responses.push(page([event(1)],{indexing:false}));await poll();assert(!$('earlier').disabled);
const cursor=nextCursor;responses.push({status:500});await loadEarlier();
assert.equal(nextCursor,cursor);assert.equal(canonicalEvents.length,1);assert(!loadingPage);
''')

    def test_incomplete_older_page_never_advances_cursor(self):
        self.run_frontend('''
responses.push(page([event(10)]));await poll();const cursor=nextCursor;
responses.push(page([event(9)],{indexing:true,next_cursor:'incomplete'}));await loadEarlier();
assert.equal(nextCursor,cursor);assert.deepEqual(canonicalEvents.map(e=>e.id),['e10']);assert($('earlier').disabled);
responses.push(page([event(10)],{indexing:false,version:1}));await poll();assert(!$('earlier').disabled);
''')

    def test_latest_wins_over_inflight_default_poll(self):
        self.run_frontend('''
responses.push(page([event(10)]));await poll();
let resolve;responses.push(()=>new Promise(r=>resolve=r));const pending=poll();
responses.push(page([event(20)],{version:20}));await latest();
resolve({ok:true,json:async()=>page([event(11)],{version:11})});await pending;
assert.deepEqual(canonicalEvents.map(e=>e.id),['e20']);assert.equal(version,20);
''')

    def test_load_earlier_saves_its_page_without_requiring_selection(self):
        self.run_frontend('''
responses.push(page([event(10)]));await poll();
responses.push(page([event(8),event(9)]));await loadEarlier();
assert.equal(JSON.parse(storage.get(stateKey)).cursor,'opaque/older?cursor');
assert.equal(ledger.scrollTop,0);assert.equal(follow,true);
''')

    def test_history_position_wins_over_an_open_recent_inspector(self):
        self.run_frontend('''
responses.push(page([event(10)]));await poll();selected='e10';
responses.push(page([event(8),event(9)]));await loadEarlier();
assert.equal(JSON.parse(storage.get(stateKey)).cursor,'opaque/older?cursor');
assert.equal(selected,'e10');assert.equal(follow,true);
''')

    def test_detail_401_keeps_list_and_summary(self):
        self.run_frontend('''
canonicalEvents=[event(1,{detail_available:true})];refreshEvents();selected='e1';inspect(canonicalEvents[0]);
responses.push({status:401});await fetchDetail(canonicalEvents[0]);
assert.equal($('status').textContent,'Reconnect required');assert.equal($('inspect-body').textContent,'summary');
assert.equal(canonicalEvents.length,1);assert(rowCache.has('e1'));
$('pause').onclick();assert.equal($('status').textContent,'Reconnect required');
''')

    def test_restore_waits_for_index_without_losing_saved_cursor(self):
        self.run_frontend('''
responses.push(page([],{indexing:true}));responses.push(page([event(10)],{indexing:true}));await poll();
assert(restoring);assert.equal(canonicalEvents[0].id,'e10');assert($('earlier').disabled);
assert.equal(JSON.parse(storage.get(stateKey)).cursor,'saved-before');
responses.push(page([event(8)],{indexing:false}));await poll();
assert(!restoring);assert.equal(selected,'e8');assert.equal($('inspector').hidden,false);
assert.equal(requests[2].url,'/api/threads/chat-a/events?before=saved-before');
''', saved=dict(cursor='saved-before', selected='e8', details=True, ledgerFollow=False))

    def test_latest_cancels_pending_restore(self):
        self.run_frontend('''
responses.push(page([event(10)]));await latest();assert(!restoring);
responses.push(page([event(11)],{version:2}));await poll();
assert(requests.every(r=>!r.url.includes('before=')));assert.equal(selected,null);
''', saved=dict(cursor='saved-before', selected='e8'))

    def test_full_window_keeps_anchor_selection_and_new_tail(self):
        self.run_frontend('''
responses.push(page(Array.from({length:1200},(_,i)=>event(i))));await poll();
showEvent(events[0]);ledger.scrollTop=300;renderLedger();const anchor=ledgerAnchor().id;
responses.push(page([event(1200)],{version:2}));await poll();
assert(follow);assert.equal(canonicalEvents.length,1200);
assert(canonicalEvents.some(e=>e.id==='e0'));assert(canonicalEvents.some(e=>e.id===anchor));
assert.equal(canonicalEvents.at(-1).id,'e1200');assert.equal(ledgerAnchor().id,anchor);
''')

    def test_segment_warnings_visible_on_every_page_path(self):
        self.run_frontend('''
const warnings=['A registered segment is unavailable'];
responses.push(page([event(1)],{warnings}));await poll();
assert($('warnings').textContent.includes(warnings[0]));assert(!$('warnings').hidden);
assert.notEqual($('status').textContent,'Live');
responses.push(page([event(0)],{warnings:['Earlier segment is unavailable']}));await loadEarlier();
assert($('warnings').textContent.includes('Earlier segment is unavailable'));
responses.push(page([event(1)],{version:1,warnings:['Another segment is unavailable']}));await poll();
assert($('warnings').textContent.includes('Another segment is unavailable'));
$('pause').onclick();$('pause').onclick();assert.notEqual($('status').textContent,'Live');
responses.push(page([event(2)],{warnings:['Latest segment is unavailable']}));await latest();
assert($('warnings').textContent.includes('Latest segment is unavailable'));assert(!$('warnings').hidden);
''')

    def test_same_id_detail_response_cannot_overwrite_new_summary(self):
        self.run_frontend('''
const call=event(1,{lane:'tools',category:'COMMAND',state:'running',detail_available:true});
responses.push(page([call]));await poll();selected='e1';$('inspector').hidden=false;
let resolve;responses.push(()=>new Promise(r=>resolve=r));const pending=fetchDetail(events[0]);
const done={...call,state:'completed',output:'finished',output_time:event(2).time};
responses.push(page([done],{version:2}));responses.push({event:{...done,output:'FULL finished'}});await poll();
resolve({ok:true,json:async()=>({event:{...call,input:'stale command'}})});await pending;
await Promise.resolve();await Promise.resolve();
assert($('inspect-meta').textContent.includes('completed'));
assert($('inspect-body').textContent.includes('finished'));
assert(!$('inspect-body').textContent.includes('stale command'));
''')

    def test_cached_details_refresh_when_summary_changes(self):
        self.run_frontend('''
const call=event(1,{lane:'tools',category:'COMMAND',state:'running',detail_available:true});
responses.push(page([call]));await poll();selected='e1';$('inspector').hidden=false;
responses.push({event:{...call,input:'full command'}});await fetchDetail(events[0]);
const done={...call,state:'completed',output:'finished'};
responses.push(page([done],{version:2}));responses.push({event:{...done,output:'FULL finished'}});await poll();
await Promise.resolve();await Promise.resolve();
assert($('inspect-meta').textContent.includes('completed'));
assert($('inspect-body').textContent.includes('finished'));
''')

    def test_saved_latest_anchor_fetches_stable_page_when_missing(self):
        self.run_frontend('''
responses.push(page([event(0),event(1)]));await poll();
assert.equal(requests[0].url,'/api/threads/chat-a/events?anchor=e1');
assert(canonicalEvents.some(e=>e.id==='e1'));assert(!restoring);
''', saved=dict(anchor='e1',cursor=None,ledgerFollow=False))

    def test_cross_page_selected_details_restore_when_event_arrives(self):
        self.run_frontend('''
responses.push(page([event(8),event(9)]));await poll();
assert.equal(JSON.parse(storage.get(stateKey)).details,true);
responses.push(page([event(10)],{version:2}));await poll();
assert.equal(selected,'e10');assert(!$('inspector').hidden);
assert.equal($('inspect-title').textContent,'Event 10');
''', saved=dict(cursor='old-page',anchor='e8',selected='e10',details=True,ledgerFollow=False))

    def test_index_fallback_cannot_evict_restored_page(self):
        self.run_frontend('''
for(let p=0;p<4;p++){
 responses.push(page(Array.from({length:300},(_,i)=>event(p*300+i)),{indexing:true,version:p+1}));await poll();
}
assert.equal(canonicalEvents.length,1200);assert(restoring);
responses.push(page(Array.from({length:300},(_,i)=>event(i-300))));await poll();
assert(!restoring);assert(canonicalEvents.some(e=>e.id==='e-300'));
assert.equal(ledgerAnchor().id,'e-300');assert(!$('inspector').hidden);
''', saved=dict(cursor='old-page',anchor='e-300',selected='e-300',details=True,ledgerFollow=False))

    def test_derived_rows_save_canonical_anchor(self):
        self.run_frontend('''
const call=event(1,{id:'call/a?b',lane:'tools',category:'COMMAND',output:'done',output_time:event(2).time});
responses.push(page([call]));await poll();ledgerFollow=false;ledger.clientHeight=30;
ledger.scrollTop=30;renderLedger();saveView();
let saved=JSON.parse(storage.get(stateKey));assert.equal(saved.anchor,'output:call/a?b');assert.equal(saved.canonical_anchor,'call/a?b');
canonicalEvents=[event(3,{artifacts:[{id:'artifact:part',name:'a',path:'a',size:1,evidence:'exists'}]})];
refreshEvents();ledger.scrollTop=30;renderLedger();saveView();saved=JSON.parse(storage.get(stateKey));
assert.equal(saved.anchor,'artifact:e3:artifact:part');assert.equal(saved.canonical_anchor,'e3');
''')

    def test_canonical_anchor_precedes_legacy_cursor_and_encodes_id(self):
        self.run_frontend('''
responses.push(page([event(1,{id:'call/a?b',lane:'tools',category:'COMMAND',output:'done',output_time:event(2).time})]));await poll();
assert.equal(requests[0].url,'/api/threads/chat-a/events?anchor=call%2Fa%3Fb');assert(!restoring);
''', saved=dict(cursor='legacy',anchor='output:call/a?b',canonical_anchor='call/a?b',ledgerFollow=False))

    def test_missing_anchor_uses_warned_latest_without_restore_loop(self):
        self.run_frontend('''
responses.push(page([event(20)],{warnings:['Saved event is unavailable; showing latest history']}));await poll();
assert(!restoring);assert.equal(canonicalEvents[0].id,'e20');
assert($('warnings').textContent.includes('Saved event is unavailable'));
responses.push(page([event(21)],{version:2}));await poll();
assert.equal(requests[1].url,'/api/threads/chat-a/events');
assert($('warnings').textContent.includes('Saved event is unavailable'));
''', saved=dict(anchor='e1',canonical_anchor='e1',ledgerFollow=False))

    def test_close_cancels_pending_cross_page_inspector(self):
        self.run_frontend('''
responses.push(page([event(8)]));await poll();$('close').onclick();
responses.push(page([event(10)],{version:2}));await poll();
assert($('inspector').hidden);assert.equal(JSON.parse(storage.get(stateKey)).details,false);
''', saved=dict(anchor='e8',selected='e10',details=True,ledgerFollow=False))

    def test_latest_cancels_inflight_anchor_restore(self):
        self.run_frontend('''
let resolve;responses.push(()=>new Promise(r=>resolve=r));const pending=poll();
responses.push(page([event(20)],{version:20}));await latest();
resolve({ok:true,json:async()=>page([event(1)],{warnings:['obsolete warning']})});await pending;
assert(!restoring);assert.equal(selected,null);assert.deepEqual(canonicalEvents.map(e=>e.id),['e20']);
assert(!$('warnings').textContent.includes('obsolete warning'));
''', saved=dict(anchor='e1',selected='e1',details=True))

    def test_long_follow_keeps_pins_and_bounded_caches(self):
        self.run_frontend('''
responses.push(page(Array.from({length:1200},(_,i)=>event(i))));await poll();
showEvent(events[0]);ledger.scrollTop=300;renderLedger();const anchor=ledgerAnchor().id;
for(let p=0;p<20;p++){
 responses.push(page(Array.from({length:100},(_,i)=>event(1200+p*100+i)),{version:p+2}));await poll();
 assert.equal(canonicalEvents.length,1200);assert(eventPages.size<=1200);
 assert(rowCache.size<=26);assert(markCache.size<=1200);
 assert(canonicalEvents.some(e=>e.id===selected));assert.equal(ledgerAnchor().id,anchor);
}
assert(follow);assert.equal(canonicalEvents.at(-1).id,'e3199');
''')


if __name__ == '__main__':
    unittest.main()
