"""Bounded tokenizer-only growth benchmark. No model/compactor inference."""
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch

sys.path.insert(0,str(Path.home()/".hermes/hermes-agent"))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from rolling_context.common import StateStore,TokenCounter,dumps
from rolling_context.engine import RollingContextEngine

class BlockedWorker:
    def __init__(self,*args,**kwargs):pass
    def start(self):pass
    def is_alive(self):return True


def main():
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    if (args.state_dir/"memory.sqlite3").exists():
        parser.error("Use a fresh benchmark state directory to preserve earlier results")
    counter=TokenCounter("http://127.0.0.1:8084")
    engine=RollingContextEngine(settings={"state_dir":str(args.state_dir),"profile":"large",
        "tail_tokens":24000,"minimum_tail_tokens":18000,"target_tokens":35000,
        "overhead_reserve":4500},counter=counter)
    engine.on_session_start("tokenizer-growth-benchmark")
    head={"role":"system","content":("Keep source evidence exact and preserve the current task. "*400)}
    body=[];total=0;results=[]
    with patch("rolling_context.engine.spawn_context_thread",side_effect=BlockedWorker):
        for target in (30000,80000,150000,300000):
            while total < target:
                index=len(body)
                message={"role":"user" if index%2==0 else "assistant",
                    "content":f"Old completed record {index}; artifact logs/task_{index}.txt.\n"+("Observed historical build output, resolved; source remains archived.\n"*100)}
                total+=counter.weights([message])[0];body.append(message)
            current={"role":"user","content":"Continue the unrelated current task; keep this request exact."}
            messages=[head,*body,current]
            first=time.monotonic();selected=engine.select_context(messages,conversation_messages=messages,incoming_message=current)
            first_ms=(time.monotonic()-first)*1000
            second=time.monotonic();again=engine.select_context(messages,conversation_messages=messages,incoming_message=current)
            warm_ms=(time.monotonic()-second)*1000
            assert selected==again and selected[-1]==current
            events=[json.loads(line) for line in (args.state_dir/"telemetry.jsonl").read_text().splitlines()]
            event=next(e for e in reversed(events) if e["event"]=="selection")
            assert event["active_tokens"]<40000
            results.append({"requested_history":target,"raw_history_tokens":event["raw_history_tokens"],
                "active_tokens":event["active_tokens"],"message_tokens":event["message_tokens"],
                "target_tokens":event["target_tokens"],"resident_pages":event["resident_pages"],
                "context_generation":event["context_generation"],"first_boundary_ms":round(first_ms,2),
                "warm_boundary_ms":round(warm_ms,2),"queued_jobs":event["compaction_queue"]})
            print(json.dumps(results[-1]),flush=True)
    with engine.store.connect() as db:
        assert db.execute("SELECT count(*) FROM summaries").fetchone()[0]==0
        assert db.execute("SELECT count(*) FROM pages").fetchone()[0]==len(body)+1
    args.output.write_text(json.dumps({"tokenizer":"installed Qwen3.8 on :8084","inference_requests":0,
        "semantic_compactor":"blocked for entire run","overhead_reserve":4500,"results":results},indent=2)+"\n")

if __name__=="__main__":main()
