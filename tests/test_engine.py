import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

HERMES = Path.home()/".hermes/hermes-agent"
sys.path.insert(0,str(HERMES))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from rolling_context.common import StateStore, digest, dumps, wire_message
from rolling_context.engine import RollingContextEngine, safe_boundaries, SUMMARY_SECTIONS


class Counter:
    def text(self,text):
        return len(text)
    def weights(self,messages):
        return [len(message.get("content") or "") for message in messages]
    def messages(self,messages):
        return sum(self.weights(messages))


class Deferred:
    def __init__(self,target,**kwargs):
        self.target=target
        self.alive=False
    def start(self):
        self.alive=True
    def is_alive(self):
        return self.alive
    def run(self):
        self.target()
        self.alive=False


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.settings={"state_dir":str(self.root),"selection_policy":"circulator","semantic_policy":"prefix",
                       "segmentation_policy":"tool_safe","compact_source_aliases":False,"tail_tokens":60,"target_tokens":2000,
                       "chunk_min":60,"chunk_target":80,"chunk_max":100,
                       "overhead_reserve":10,"warm_budget_tokens":4096,
                       "recall_budget_tokens":50,"mode":"circulator"}
        self.engine=RollingContextEngine(settings=self.settings,counter=Counter())
        self.engine.on_session_start("session-a")
        self.messages=[{"role":"system","content":"Keep system instructions unchanged."}]+[
            {"role":"user" if i%2==0 else "assistant","content":f"cold_{i}".ljust(32,"x")} for i in range(4)
        ]+[{"role":"user","content":"current_goal".ljust(32,"q")},
           {"role":"assistant","content":"latest_tail".ljust(32,"z")}]
        self.spawn=patch("rolling_context.engine.spawn_context_thread",side_effect=lambda target,**kw:Deferred(target))
        self.spawn.start()
        self.verify=patch("rolling_context.engine.verify_compactor",return_value=None)
        self.verify.start()
    def tearDown(self):
        self.spawn.stop()
        self.verify.stop()
        self.temp.cleanup()
    def select(self,messages=None):
        messages=messages or self.messages
        return self.engine.select_context(messages,conversation_messages=messages,
                                          incoming_message=next(m for m in reversed(messages) if m["role"]=="user"))
    def result(self,payload):
        first=json.loads(payload["messages"][1]["content"])["transcript_chunk"][0]
        obj={section:[] for section in SUMMARY_SECTIONS}
        obj["task_state"]=[{"text":"fixture recorded; preserve exact paths","sources":[first["source_id"]]}]
        return {"choices":[{"finish_reason":"stop","message":{"content":dumps(obj)}}],
                "usage":{"prompt_tokens":90,"completion_tokens":40},"timings":{"prompt_ms":10}}
    def finish(self,result=None):
        worker=self.engine._worker
        self.engine._latest_plan=None
        with patch("rolling_context.engine.http_json",return_value=[]),patch("rolling_context.engine.stream_chat",side_effect=result or (lambda url,payload,timeout:self.result(payload))):
            worker.run()
        return worker

    def test_cold_eviction_does_not_wait_for_semantic_summary_or_mutate_transcript(self):
        original=copy.deepcopy(self.messages)
        selected=self.select()
        self.assertEqual(self.messages,original)
        self.assertEqual(selected[0],original[0])
        self.assertEqual(selected[-2:],original[-2:])
        self.assertLess(len(selected),len(original))
        self.assertTrue(self.engine._worker.is_alive())
        with self.engine.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM summaries").fetchone()[0],0)
            self.assertEqual(db.execute("SELECT count(*) FROM pages").fetchone()[0],6)
        for message in original[1:]:
            self.assertEqual(self.engine.store.record("session-a",digest(wire_message(message))),wire_message(message))

    def test_summary_is_independent_and_only_enters_next_request_boundary(self):
        first=self.select()
        frozen=copy.deepcopy(first)
        calls=[]
        def capture(url,payload,timeout):
            calls.append(payload)
            return self.result(payload)
        self.finish(capture)
        self.assertEqual(first,frozen)
        second=self.select()
        self.assertIn("IMMUTABLE BLOCK",dumps(second))
        self.assertNotIn("previous_memory",calls[0]["messages"][1]["content"])
        self.assertEqual(second[-2:],self.messages[-2:])
        with self.engine.store.connect() as db:
            row=db.execute("SELECT text FROM summaries").fetchone()[0]
            self.assertEqual(db.execute("SELECT status FROM jobs ORDER BY id LIMIT 1").fetchone()[0],"ready")
            self.assertGreater(db.execute("SELECT count(*) FROM page_representations WHERE kind='COMPACT'").fetchone()[0],0)
        again=self.select()
        with self.engine.store.connect() as db:
            self.assertEqual(row,db.execute("SELECT text FROM summaries").fetchone()[0])
        self.assertEqual(second,again)

    def test_restart_reuses_valid_blocks_but_edited_source_invalidates_them(self):
        self.select();self.finish()
        e=RollingContextEngine(settings=self.settings,counter=Counter());e.on_session_start("session-a")
        output=e.select_context(self.messages,conversation_messages=self.messages)
        self.assertIn("IMMUTABLE BLOCK",dumps(output))
        changed=copy.deepcopy(self.messages);changed[1]["content"]="edited fact".ljust(32,"x")
        output=e.select_context(changed,conversation_messages=changed)
        self.assertNotIn("IMMUTABLE BLOCK",dumps(output))

    def test_queued_worker_captures_original_session_across_reset(self):
        self.select();worker=self.engine._worker
        self.engine.on_session_reset();self.engine.on_session_start("session-b")
        self.engine._latest_plan=None
        with patch("rolling_context.engine.http_json",return_value=[]),patch("rolling_context.engine.stream_chat",side_effect=lambda u,p,timeout:self.result(p)):
            worker.run()
        with self.engine.store.connect() as db:
            self.assertEqual(db.execute("SELECT session FROM summaries").fetchone()[0],"session-a")
        self.assertEqual(self.engine.store.search("session-b","cold_0"),[])

    def test_truncated_or_invalid_provenance_never_installs_a_summary(self):
        self.select()
        def truncated(url,payload,timeout):
            result=self.result(payload);result["choices"][0]["finish_reason"]="length";return result
        self.finish(truncated)
        with self.engine.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM summaries").fetchone()[0],0)
            self.assertEqual(db.execute("SELECT status FROM jobs").fetchone()[0],"failed")
        self.assertEqual(self.select()[-2:],self.messages[-2:])

    def test_tool_boundaries_never_separate_a_call_from_its_results(self):
        body=[{"role":"user","content":"task"},
              {"role":"assistant","content":"","tool_calls":[{"id":"one"},{"id":"two"}]},
              {"role":"tool","content":"result1","tool_call_id":"one"},
              {"role":"tool","content":"result2","tool_call_id":"two"},
              {"role":"assistant","content":"finished"},{"role":"user","content":"next"}]
        self.assertNotIn(2,safe_boundaries(body));self.assertNotIn(3,safe_boundaries(body))
        self.assertIn(4,safe_boundaries(body))

    def test_current_user_instruction_is_exact_even_when_outside_recent_tail(self):
        messages=copy.deepcopy(self.messages)
        messages[5]={"role":"assistant","content":"new_assistant".ljust(32,"n")}
        selected=self.select(messages)
        latest_user=next(m for m in reversed(messages) if m["role"]=="user")
        self.assertIn(latest_user,selected)
        self.assertEqual(selected[-2:],messages[-2:])

    def test_lexical_recall_returns_archived_raw_page_and_is_session_scoped(self):
        self.select()
        cold_id=digest(wire_message(self.messages[1]))
        hits=self.engine.pages.recall("session-a","cold_0",{cold_id},100)
        self.assertEqual(hits[0]["source_id"],cold_id)
        self.assertEqual(hits[0]["message"],wire_message(self.messages[1]))
        self.assertEqual(self.engine.pages.recall("session-b","cold_0",{cold_id},100),[])
        restored=json.loads(self.engine.handle_tool_call("rolling_history_read",{"source_id":cold_id,"max_chars":15}))
        self.assertEqual(restored["next_offset"],15)

    def test_context_generation_is_stable_on_retry_and_advances_on_new_content(self):
        first=self.select();self.select()
        with self.engine.store.connect() as db:
            before=db.execute("SELECT count(*) FROM context_generations").fetchone()[0]
        changed=copy.deepcopy(self.messages);changed[-1]["content"]+="changed"
        self.select(changed)
        with self.engine.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM context_generations").fetchone()[0],before+1)

    def test_pinned_project_memory_is_stable_until_session_restart(self):
        path=self.root/"large-project-memory.json"
        path.write_text('{"constraint":"Preserve immutable anchors"}')
        self.engine.on_session_start("session-a")
        first=self.select()
        path.write_text('{"constraint":"A new operator fact"}')
        self.assertEqual(first,self.select())
        self.engine.on_session_start("session-a")
        self.assertNotEqual(first,self.select())

    def test_growth_does_not_wait_for_a_permanently_blocked_compactor(self):
        self.settings.update(tail_tokens=2400,minimum_tail_tokens=1800,target_tokens=4000,
                             chunk_min=800,chunk_target=1000,chunk_max=1200,
                             overhead_reserve=100,recall_budget_tokens=200)
        engine=RollingContextEngine(settings=self.settings,counter=Counter())
        engine.on_session_start("growing")
        active=[]
        for size in (30000,80000,150000,300000):
            messages=[{"role":"system","content":"keep pinned"}]+[
                {"role":"user" if i%2==0 else "assistant","content":f"record {i}:".ljust(1000,"x")} for i in range(size//1000)]
            selected=engine.select_context(messages,conversation_messages=messages)
            active.append(engine.counter.messages(selected))
            self.assertEqual(selected[-1],messages[-1])
            self.assertLess(active[-1],4000)
            self.assertTrue(engine._worker.is_alive())
            # Simulate a CPU inference that remains running throughout all later
            # turns, rather than merely a queued job.
            with engine.store.connect() as db:
                db.execute("UPDATE jobs SET status='running' WHERE status='queued'")
        self.assertLess(max(active)-min(active),100)
        events=[json.loads(line) for line in (self.root/"telemetry.jsonl").read_text().splitlines() if json.loads(line)["event"]=="selection"]
        event=events[-1]
        for key in ("active_tokens","target_tokens","raw_history_tokens","resident_pages","recalled_pages",
                    "evicted_pages","compact_pages","raw_pages","compaction_queue","compaction_in_progress",
                    "last_compaction_duration","context_generation"):
            self.assertIn(key,event)
        self.assertEqual(event["raw_history_tokens"],300000)
        self.assertEqual(event["compaction_in_progress"],1)
        self.assertGreater(event["evicted_pages"],0)
        with engine.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM summaries").fetchone()[0],0)
            self.assertEqual(db.execute("SELECT count(*) FROM pages WHERE session='growing'").fetchone()[0],300)
            ledger=json.loads(db.execute("SELECT pages FROM context_generations ORDER BY id DESC LIMIT 1").fetchone()[0])
        self.assertTrue(ledger)
        self.assertTrue(all("page_id" in p and "representation" in p for p in ledger))

    def test_recall_selects_facts_compact_excerpt_and_raw_without_destroying_source(self):
        message={"role":"assistant","content":"decision: use src/widget.py port 8084\n"+"padding "*1000}
        h=digest(wire_message(message));store=self.engine.store
        store.snapshot("session-a","wire",[message]);self.engine.pages.ingest("session-a",[message],[8000])
        obj={section:[] for section in SUMMARY_SECTIONS}
        obj["decisions"]=[{"text":"use widget on 8084","sources":[h]}]
        self.engine.pages.attach_compact("session-a",{h},9,obj)
        facts=self.engine.pages.recall("session-a","what path src/widget.py port 8084",{h},1500)
        self.assertEqual(facts[0]["representation"],"FACTS")
        self.assertEqual(self.engine.pages.recall("session-a","8084",{h},1500)[0]["source_id"],h)
        compact=self.engine.pages.recall("session-a","widget decision",{h},1500)
        self.assertEqual(compact[0]["representation"],"COMPACT")
        excerpt=self.engine.pages.recall("session-a","exact widget error",{h},1500)
        self.assertEqual(excerpt[0]["representation"],"RAW_EXCERPT")
        self.assertIn(excerpt[0]["message"]["content"],message["content"])
        raw=self.engine.pages.recall("session-a","full raw widget",{h},9000)
        self.assertEqual(raw[0]["representation"],"RAW")
        self.assertEqual(store.record("session-a",h),message)

    def test_stale_large_tool_output_can_shed_before_a_summary_exists(self):
        e=RollingContextEngine(settings={**self.settings,"target_tokens":2000},counter=Counter())
        e.on_session_start("tools")
        messages=[self.messages[0],{"role":"user","content":"old task"},
                  {"role":"assistant","content":"","tool_calls":[{"id":"old"}]},
                  {"role":"tool","tool_call_id":"old","content":"build passed src/file.py\n"+"log\n"*4000},
                  {"role":"assistant","content":"completed"},
                  {"role":"user","content":"new current task"},
                  {"role":"assistant","content":"","tool_calls":[{"id":"new"}]},
                  {"role":"tool","tool_call_id":"new","content":"keep current output exactly"}]
        original=copy.deepcopy(messages)
        selected=e.select_context(messages,conversation_messages=messages)
        self.assertLess(e.counter.messages(selected),2000)
        self.assertEqual(selected[-1],messages[-1]);self.assertIn(messages[-3],selected)
        self.assertEqual(messages,original)
        self.assertEqual(e.store.record("tools",digest(wire_message(messages[3]))),messages[3])



if __name__=="__main__":unittest.main()
