#!/usr/bin/env python3
"""Serve fictional demo data through the real UI. Never reads Codex logs."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from src.trace_viewer.server import RolloutReader, ViewerServer


def main():
    with tempfile.TemporaryDirectory(prefix='codex-trajectory-demo-') as temp:
        log = Path(temp)/'demo.jsonl'
        start = datetime(2026, 10, 3, 9, 0, tzinfo=timezone.utc)
        rows = [{'type':'session_meta', 'payload':{'id':'demo-session'}}]
        def stamp(i): return (start+timedelta(seconds=i)).isoformat()
        def message(i, role, text):
            rows.append({'type':'response_item', 'timestamp':stamp(i), 'payload':{
                'type':'message', 'role':role, 'phase':'commentary', 'content':text}})
        def item(i, kind, ident, **fields):
            rows.append({'type':'event_msg', 'timestamp':stamp(i+1), 'payload':{
                'type':'item_completed', 'thread_id':'demo-session',
                'started_at_ms':int((start+timedelta(seconds=i)).timestamp()*1000),
                'completed_at_ms':int((start+timedelta(seconds=i+1)).timestamp()*1000),
                'item':{'type':kind, 'id':ident, 'status':'completed', **fields}}})
        rows.append({'type':'event_msg','timestamp':stamp(0),'payload':{'type':'task_started','turn_id':'demo-turn'}})
        message(1, 'user', 'Add a compact result inspector and check the mobile layout.')
        message(2, 'assistant', 'I will inspect the existing layout, make a focused patch, and test the result.')
        item(3,'Plan','p1',text='Inspect the layout; add the inspector; run tests; review on mobile.')
        item(4,'CommandExecution','c1',command='rg -n "inspector" src/viewer.html',stdout='src/viewer.html:84: selected event panel',exit_code=0)
        item(6,'CollabAgentToolCall','a1',tool='spawn_agent',prompt='Review keyboard navigation. Read-only scope.',receiver_thread_ids=['demo-reviewer'])
        item(8,'McpToolCall','m1',server='browser',tool='inspect',arguments={'target':'local preview'},result={'content':[{'type':'text','text':'Timeline visible. No horizontal page overflow.'}]})
        item(10,'FileChange','f1',changes={'src/viewer.html':{'type':'update'}})
        message(12,'assistant','The inspector now follows the available content area instead of a fixed offset.')
        item(13,'CommandExecution','c2',command='python3 -m unittest discover -s tests',stdout='Ran 59 tests. OK.',exit_code=0)
        item(15,'McpToolCall','m2',server='browser',tool='check_mobile',result={'content':[{'type':'text','text':'390px viewport: labels readable; inspector does not overlap the timeline.'}]})
        item(17,'ContextCompaction','ctx')
        rows.append({'type':'response_item','timestamp':stamp(18),'payload':{'type':'function_call','name':'capture_preview','arguments':'Save the reviewed local preview','call_id':'t1'}})
        rows.append({'type':'response_item','timestamp':stamp(19),'payload':{'type':'function_call_output','call_id':'t1','output':'Preview saved. Synthetic demonstration data only.'}})
        item(20,'CollabAgentToolCall','a2',tool='wait_agent',agents_states={'demo-reviewer':'Review complete: keyboard navigation verified.'})
        message(22,'assistant','Ready for review. Tests pass, keyboard controls work, and mobile layout is clean.')
        log.write_text('\n'.join(json.dumps(r) for r in rows)+'\n')
        server=ViewerServer(RolloutReader(log,'demo-session'))
        print(server.origin+'/#'+server.token, flush=True)
        try: server.serve_forever()
        except KeyboardInterrupt: pass
        finally: server.server_close()


if __name__ == '__main__':
    main()
