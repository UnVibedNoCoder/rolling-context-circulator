"""Immutable message pages, simultaneous representations and lexical recall."""
from collections import OrderedDict
import copy
import json
import math
import re
import time

from .common import digest, dumps, wire_message, persistent_message


# Current disk captures are original observations; archive reads/searches are echoes.
ARCHIVE_TOOLS = frozenset(('rolling_history_search', 'rolling_history_read',
                          'rolling_snapshot_read', 'rolling_raw_read'))


def terms(text):
    return list(dict.fromkeys(re.findall(r"[A-Za-z_][A-Za-z0-9_.:/-]{2,80}", text)))[:32]


def identifiers(text):
    return list(dict.fromkeys(re.findall(
        r"(?:~?/|[A-Za-z0-9_.-]+/)[A-Za-z0-9_./-]+|\b[0-9a-f]{7,64}\b|\b\d+(?:\.\d+)?\b|\b[A-Za-z_]\w*(?:\.\w+)+\b", text)))[:80]


def facts_for(message):
    """Literal observations, never inferred decisions or execution success."""
    text = persistent_message(message).get("content") or ""
    lines = [line[:350] for line in text.splitlines() if re.search(
        r"\b(?:decision|decided|must|constraint|passed|failed|error|exit code|completed|TODO)\b", line, re.I)][:8]
    return {"identifiers": identifiers(text), "observed_lines": lines,
            "role": message.get("role"), "chars": len(text),
            "tool_call_id": message.get("tool_call_id")}


class PageManager:
    def __init__(self, store, ram_limit=64*1024*1024, counter=None):
        self.store = store
        self.counter = counter
        self.ram_limit = ram_limit
        self.ram_bytes = 0
        self.cache = OrderedDict()
        self.known = set()
        self.recall_cache = OrderedDict()
        self.observed_order = {}
        self.recall_calls = self.recall_cache_hits = self.raw_cache_hits = self.raw_database_reads = 0
        self.recall_seconds = 0.0
        with self.store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS pages (
                    session TEXT NOT NULL, source_id TEXT NOT NULL, tokens INTEGER NOT NULL,
                    state TEXT NOT NULL, last_access REAL NOT NULL, accesses INTEGER NOT NULL,
                    tags TEXT NOT NULL, PRIMARY KEY(session,source_id)
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS page_fts USING fts5(
                    session UNINDEXED, source_id UNINDEXED, text, tokenize='unicode61'
                );
                CREATE TABLE IF NOT EXISTS page_representations (
                    session TEXT NOT NULL, source_id TEXT NOT NULL, kind TEXT NOT NULL,
                    version INTEGER NOT NULL, body TEXT NOT NULL, created REAL NOT NULL,
                    PRIMARY KEY(session,source_id,kind,version)
                );
                CREATE TABLE IF NOT EXISTS page_transitions (
                    id INTEGER PRIMARY KEY, session TEXT NOT NULL, source_id TEXT NOT NULL,
                    created REAL NOT NULL, state TEXT NOT NULL, generation INTEGER
                );
                CREATE TABLE IF NOT EXISTS observed_identifiers (
                    session TEXT NOT NULL, source_id TEXT NOT NULL, kind TEXT NOT NULL,
                    value TEXT NOT NULL, UNIQUE(session,source_id,kind,value)
                );
                CREATE INDEX IF NOT EXISTS identifier_lookup ON observed_identifiers(session,value);
                CREATE TABLE IF NOT EXISTS page_observations (
                    session TEXT NOT NULL, source_id TEXT NOT NULL,
                    position INTEGER NOT NULL, present INTEGER NOT NULL,
                    PRIMARY KEY(session,source_id)
                );
            """)
            self.known = {(r[0], r[1]) for r in db.execute("SELECT session,source_id FROM page_representations WHERE kind='FACTS'")}
            # Existing immutable records stay untouched. Upgrade only derived tags,
            # including calls/results no longer present in the latest transcript.
            db.execute("CREATE TABLE IF NOT EXISTS page_index_versions(name TEXT PRIMARY KEY, version INTEGER)")
            if not db.execute("SELECT 1 FROM page_index_versions WHERE name='archive_tools' AND version=1").fetchone():
                names = ','.join('?' for _ in ARCHIVE_TOOLS)
                db.execute(f"""CREATE TEMP TABLE retrieval_calls AS
                    SELECT p.session,p.source_id,json_extract(c.value,'$.id') AS call_id
                    FROM pages p JOIN records r ON r.hash=p.source_id,
                         json_each(r.body,'$.tool_calls') c
                    WHERE json_extract(c.value,'$.function.name') IN ({names})""", tuple(sorted(ARCHIVE_TOOLS)))
                db.execute("""UPDATE pages SET tags=json_insert(tags,'$[#]','memory_retrieval')
                    WHERE tags NOT LIKE '%memory_retrieval%' AND
                    (EXISTS (SELECT 1 FROM retrieval_calls c WHERE c.session=pages.session AND c.source_id=pages.source_id)
                     OR EXISTS (SELECT 1 FROM records r JOIN retrieval_calls c
                        ON c.session=pages.session AND c.call_id=json_extract(r.body,'$.tool_call_id')
                        WHERE r.hash=pages.source_id AND json_extract(r.body,'$.role')='tool'))""")
                db.execute("INSERT OR REPLACE INTO page_index_versions VALUES ('archive_tools',1)")
            # Migrate residency vocabulary from the earlier prototype.
            db.execute("UPDATE pages SET state='ACTIVE' WHERE state='RESIDENT'")

    def ingest(self, session, messages, weights):
        messages = [persistent_message(m) for m in messages]
        now = time.time()
        source_ids = [digest(wire_message(m)) for m in messages]
        revision_changed = self.observed_order.get(session) != source_ids
        added = set()
        memory_calls={c.get('id') for m in messages for c in m.get('tool_calls') or [] if c.get('function',{}).get('name') in ARCHIVE_TOOLS}
        with self.store.connect() as db:
            for message, count, source_id in zip(messages, weights, source_ids):
                if (session, source_id) not in self.known:
                    text = dumps(wire_message(message))
                    facts = facts_for(message)
                    tags = []
                    if any(c.get('function',{}).get('name') in ARCHIVE_TOOLS for c in message.get('tool_calls') or []) or message.get('role')=='tool' and message.get('tool_call_id') in memory_calls:
                        tags.append('memory_retrieval')
                    if message.get("role") == "tool":
                        tags.append("stale_tool_output")
                    if count >= 2048:
                        tags.append("large_output")
                    if any("/" in value for value in facts["identifiers"]):
                        tags.append("source_paths")
                    if re.search(r"\b(?:passed|failed|build|test|exit code)\b", text, re.I):
                        tags.append("build_test_output")
                    if re.search(r"\b(?:completed|resolved|finished)\b", text, re.I):
                        tags.append("completed_chatter")
                    db.execute("INSERT OR IGNORE INTO pages VALUES (?,?,?,?,?,?,?)",
                               (session,source_id,count,"WARM",now,0,dumps(tags)))
                    exists = db.execute("SELECT 1 FROM page_fts WHERE session=? AND source_id=?",(session,source_id)).fetchone()
                    if not exists:
                        db.execute("INSERT INTO page_fts(session,source_id,text) VALUES (?,?,?)",(session,source_id,text))
                    for kind, value in (("RAW", {"record_hash":source_id}), ("FACTS",facts), ("INDEX",{"terms":terms(text)})):
                        db.execute("INSERT OR IGNORE INTO page_representations VALUES (?,?,?,?,?,?)",
                                   (session,source_id,kind,0,dumps(value),now))
                    db.executemany("INSERT OR IGNORE INTO observed_identifiers VALUES (?,?,?,?)",
                                   [(session,source_id,"literal",value) for value in facts["identifiers"]])
                    added.add((session,source_id))
                self._cache(source_id, message)
            if revision_changed:
                db.execute('UPDATE page_observations SET present=0 WHERE session=?',(session,))
                db.executemany('INSERT INTO page_observations VALUES (?,?,?,1) ON CONFLICT(session,source_id) DO UPDATE SET position=excluded.position,present=1',
                               [(session,h,i) for i,h in enumerate(source_ids)])
        # Publish index/cache metadata only after the durable transaction succeeds.
        self.known.update(added)
        if revision_changed:
            self.observed_order[session] = source_ids
            self.recall_cache.clear()

    def _cache(self, source_id, message):
        if source_id in self.cache:
            self.cache.move_to_end(source_id)
            return
        size = len(dumps(message).encode())
        if size > self.ram_limit:
            return
        while self.cache and self.ram_bytes + size > self.ram_limit:
            _, (_, removed_size) = self.cache.popitem(last=False)
            self.ram_bytes -= removed_size
        self.cache[source_id] = (copy.deepcopy(message),size)
        self.ram_bytes += size

    def attach_compact(self, session, source_ids, summary_id, summary):
        self.recall_cache.clear()
        with self.store.connect() as db:
            for source_id in source_ids:
                relevant = {section:[item for item in items if source_id in item["sources"]]
                            for section,items in summary.items()}
                relevant = {section:items for section,items in relevant.items() if items}
                # No fabricated representation for an omitted source. The full
                # block and its raw chunk remain durable independently.
                if relevant:
                    db.execute("INSERT OR IGNORE INTO page_representations VALUES (?,?,?,?,?,?)",
                               (session,source_id,"COMPACT",summary_id,dumps(relevant),time.time()))

    def _tokens(self, value):
        text = dumps(value)
        return self.counter.text(text) if self.counter else math.ceil(len(text)/3)

    def automatic_ids(self, session, allowed_ids):
        """Session-scoped originals eligible for automatic recall, never echoes."""
        if not allowed_ids:
            return set()
        with self.store.connect() as db:
            return {r[0] for r in db.execute("SELECT source_id FROM pages WHERE session=? AND tags NOT LIKE '%memory_retrieval%' AND source_id IN (SELECT value FROM json_each(?))",
                                            (session, dumps(sorted(allowed_ids))))}

    def recall(self, session, query, allowed_ids, budget_tokens, relevance_first=False):
        started = time.monotonic()
        allowed_ids = self.automatic_ids(session, allowed_ids)
        self.recall_calls += 1
        stop = {"the","and","that","with","this","from","have","please","again","should"}
        keywords = list(dict.fromkeys([word for word in terms(query) if word.lower() not in stop]+identifiers(query)))[:12]
        if not keywords or not allowed_ids or budget_tokens <= 0:
            return []
        key=(session,query,digest(sorted(allowed_ids)),budget_tokens,relevance_first)
        if key in self.recall_cache:
            self.recall_cache_hits += 1
            self.recall_cache.move_to_end(key)
            self.recall_seconds += time.monotonic()-started
            return copy.deepcopy(self.recall_cache[key])
        expression = " OR ".join('"'+word.replace('"','""')+'"*' for word in keywords)
        exact = identifiers(query) + keywords
        placeholders = ",".join("?" for _ in exact)
        eligible = dumps(sorted(allowed_ids))
        with self.store.connect() as db:
            exact_ids = {r[0] for r in db.execute(f"SELECT source_id FROM observed_identifiers WHERE session=? AND value IN ({placeholders}) AND source_id IN (SELECT value FROM json_each(?))",(session,*exact,eligible))}
            rows = db.execute("SELECT f.source_id,bm25(page_fts) AS rank,p.last_access,p.accesses,p.tokens FROM page_fts AS f JOIN pages AS p ON p.session=f.session AND p.source_id=f.source_id WHERE page_fts MATCH ? AND f.session=? AND f.source_id IN (SELECT value FROM json_each(?)) ORDER BY rank LIMIT 100",
                              (expression,session,eligible)).fetchall()
            observations = {r['source_id']:dict(r) for r in db.execute('SELECT source_id,position,present FROM page_observations WHERE session=?',(session,))}
            # Exact identifiers dominate lexical relevance, followed by BM25,
            # access recency and frequency. No model call or embeddings here.
            exact_rows=[]
            for source_id in exact_ids | ({query.strip()} & allowed_ids):
                row=db.execute("SELECT source_id,-1.0 AS rank,last_access,accesses,tokens FROM pages WHERE session=? AND source_id=?",(session,source_id)).fetchone()
                if row:exact_rows.append(row)
            current_requested = bool(re.search(r'\b(?:current|latest|now|corrected|updated|revised)\b',query,re.I))
            def rank(row):
                evidence = observations.get(row['source_id'],{})
                revision = (-evidence.get('present',0),-evidence.get('position',-1))
                lexical = (row['source_id'] not in exact_ids,row['rank'])
                if relevance_first:
                    return (revision[0], *lexical, revision[1])
                return (*revision,*lexical) if current_requested else (revision[0],*lexical,revision[1])
            rows = sorted([*exact_rows,*rows],key=rank)
            result = []
            remaining = budget_tokens
            seen = set()
            raw_requested = bool(re.search(r"\b(?:exact|quote|verbatim|raw|full|error|excerpt)\b", query,re.I))
            facts_requested = bool(re.search(r"\b(?:path|paths|identifier|port|number|facts)\b",query,re.I)) and not raw_requested
            for row in rows:
                source_id = row["source_id"]
                if source_id not in allowed_ids or source_id in seen:
                    continue
                seen.add(source_id)
                representations = {r["kind"]:dict(r) for r in db.execute("SELECT * FROM page_representations WHERE session=? AND source_id=? ORDER BY version",(session,source_id))}
                if source_id in self.cache:
                    self.raw_cache_hits += 1
                    message = self.cache[source_id][0]
                    self.cache.move_to_end(source_id)
                else:
                    self.raw_database_reads += 1
                    record = db.execute("SELECT body FROM records WHERE hash=?",(source_id,)).fetchone()
                    if not record:
                        continue
                    message = json.loads(record[0]);self._cache(source_id,message)
                candidates = []
                if facts_requested and "FACTS" in representations:
                    candidates.append(("FACTS",json.loads(representations["FACTS"]["body"]),0))
                if not raw_requested and "COMPACT" in representations:
                    r=representations["COMPACT"];candidates.append(("COMPACT",json.loads(r["body"]),r["version"]))
                if row["tokens"] <= remaining:
                    candidates.append(("RAW",wire_message(message),0))
                # Matching excerpts retain exact text and character offsets. File
                # dumps and logs can be recalled without bringing every line back.
                text = message.get("content") or ""
                at = next((text.lower().find(k.lower()) for k in keywords if k.lower() in text.lower()),0)
                start=max(0,at-200);end=min(len(text),start+min(6000,max(128,remaining*2)))
                candidates.append(("RAW_EXCERPT",{"role":message.get("role"),"content":text[start:end],
                                                     "start_char":start,"end_char":end,"total_chars":len(text)},0))
                if "FACTS" in representations:
                    candidates.append(("FACTS",json.loads(representations["FACTS"]["body"]),0))
                for kind,value,version in candidates:
                    count = row["tokens"] if kind == "RAW" else self._tokens(value)
                    if kind == "RAW_EXCERPT":
                        while count > remaining and len(value["content"]) > 128:
                            value["content"]=value["content"][:max(128,int(len(value["content"])*remaining/count*.8))]
                            value["end_char"]=value["start_char"]+len(value["content"])
                            count=self._tokens(value)
                    if count <= remaining:
                        item={"page_id":source_id,"source_id":source_id,"representation":kind,"version":version,"tokens":count}
                        evidence = observations.get(source_id,{})
                        item['evidence_position'] = evidence.get('position')
                        item['source_status'] = ('in_current_transcript' if evidence.get('present') else 'historical_revision')
                        item['truth_status'] = 'quoted_observation'
                        item["message" if kind in {"RAW","RAW_EXCERPT"} else "data"]=copy.deepcopy(value)
                        result.append(item);remaining-=count
                        break
                if len(result) >= 8:
                    break
            self.recall_cache[key]=copy.deepcopy(result)
            if len(self.recall_cache)>64:
                self.recall_cache.popitem(last=False)
            self.recall_seconds += time.monotonic()-started
            return result

    def macro_groups(self, session, messages, hashes, weights, max_tokens):
        from .coherent import coherent_boundaries
        cuts = [0]+coherent_boundaries(messages,weights,max_tokens)+[len(messages)]
        groups = []
        with self.store.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS macro_segments(session TEXT,segment_id TEXT,source_ids TEXT,tokens INTEGER,start_position INTEGER,end_position INTEGER,PRIMARY KEY(session,segment_id))")
            for start,end in zip(cuts,cuts[1:]):
                ids = hashes[start:end]
                segment = {'segment_id':digest(ids),'source_ids':ids,'start':start,'end':end,'tokens':sum(weights[start:end])}
                groups.append(segment)
                db.execute("INSERT OR IGNORE INTO macro_segments VALUES (?,?,?,?,?,?)",(session,segment['segment_id'],dumps(ids),segment['tokens'],start,end))
        return groups

    def recall_segments(self, session, query, messages, hashes, weights, allowed_ids, budget_tokens, max_tokens=16000):
        """Fine search seeds, coarse task-cluster prompt units with exact provenance."""
        if budget_tokens <= 0:
            return []
        allowed_ids = self.automatic_ids(session, allowed_ids)
        seeds = self.recall(session,query,allowed_ids,min(budget_tokens,1800),relevance_first=True)
        groups = self.macro_groups(session,messages,hashes,weights,max_tokens)
        lookup = {h:g for g in groups for h in g['source_ids']}
        result=[];seen=set();remaining=budget_tokens
        for seed in seeds:
            group=lookup.get(seed['source_id'])
            if not group or group['segment_id'] in seen or not set(group['source_ids']).intersection(allowed_ids):
                continue
            seen.add(group['segment_id'])
            # One quoted macrosegment, preserving chronological call/result and
            # interpretation. Large dumps may use explicitly labelled excerpts;
            # every exact original remains available through source_id.
            evidence=[]
            for index in range(group['start'],group['end']):
                m=wire_message(messages[index]);h=hashes[index]
                if h not in allowed_ids:continue
                if weights[index]<=900 or h==seed['source_id'] and weights[index]<=remaining//2:
                    evidence.append({'source_id':h,'representation':'RAW','message':m})
                else:
                    text=m.get('content') or ''
                    keys=terms(query)+identifiers(query)
                    at=next((text.lower().find(k.lower()) for k in keys if len(k)>4 and k.lower() in text.lower()),0)
                    start=max(0,at-150);end=min(len(text),start+700)
                    evidence.append({'source_id':h,'representation':'FACTS_EXCERPT','facts':facts_for(m),'message':{**m,'content':text[start:end]},'start_char':start,'end_char':end,'total_chars':len(text)})
            value={'page_id':group['segment_id'],'source_id':seed['source_id'],'source_ids':list(dict.fromkeys(h for h in group['source_ids'] if h in allowed_ids)),'representation':'MACRO','version':0,'boundary':'task_cluster','data':evidence,'source_tokens':group['tokens']}
            count=self._tokens(value)
            if count>remaining:
                # Keep the matched exact record and all structural neighbours as
                # one unit; shrink only labelled large excerpts, never RAW atoms.
                for item in evidence:
                    if item['representation']=='FACTS_EXCERPT':
                        item['message']['content']=item['message']['content'][:128]
                        item['end_char']=item['start_char']+len(item['message']['content'])
                count=self._tokens(value)
            if count>remaining:
                continue
            value['tokens']=count;result.append(value);remaining-=count
            if len(result)>=4:break
        return result

    def state(self, session, source_ids, state, generation=None):
        now = time.time()
        with self.store.connect() as db:
            for source_id in source_ids:
                row = db.execute("SELECT state FROM pages WHERE session=? AND source_id=?",(session,source_id)).fetchone()
                if not row:
                    continue
                if row["state"] != state:
                    db.execute("INSERT INTO page_transitions(session,source_id,created,state,generation) VALUES (?,?,?,?,?)",
                               (session,source_id,now,state,generation))
                db.execute("UPDATE pages SET state=?,last_access=?,accesses=accesses+? WHERE session=? AND source_id=?",
                           (state,now,int(state in {"ACTIVE","RECALL"}),session,source_id))

    def cool_others(self, session, resident_ids, generation):
        with self.store.connect() as db:
            prior = [r[0] for r in db.execute("SELECT source_id FROM pages WHERE session=? AND state IN ('ACTIVE','RECALL')",(session,))]
            cooling = [r[0] for r in db.execute("SELECT source_id FROM pages WHERE session=? AND state='COOLING'",(session,))]
            newly_cold = [r[0] for r in db.execute("SELECT source_id FROM pages WHERE session=? AND state='WARM'",(session,)) if r[0] not in resident_ids]
        self.state(session,[h for h in cooling if h not in resident_ids],"COLD",generation)
        self.state(session,newly_cold,"COLD",generation)
        evicted=[h for h in prior if h not in resident_ids]
        self.state(session,evicted,"COOLING",generation)
        return evicted

    def archive_cold(self, session, source_ids):
        with self.store.connect() as db:
            cold={r[0] for r in db.execute("SELECT source_id FROM pages WHERE session=? AND state NOT IN ('ACTIVE','RECALL')",(session,))}
        self.state(session,cold & set(source_ids),"ARCHIVED")

    def search_ids(self, session, query, limit=5, relevance_first=False):
        words=list(dict.fromkeys(terms(query)+identifiers(query)))
        if not words:
            return []
        expression=" OR ".join('"'+word.replace('"','""')+'"*' for word in words[:12])
        with self.store.connect() as db:
            # An exact source hash can also be pasted into the search tool.
            exact=db.execute("SELECT source_id FROM pages WHERE session=? AND source_id=?",(session,query)).fetchall()
            order = "coalesce(o.present,0) DESC,bm25(page_fts),coalesce(o.position,-1) DESC" if relevance_first else "coalesce(o.present,0) DESC,coalesce(o.position,-1) DESC,bm25(page_fts)"
            rows=db.execute("SELECT f.source_id FROM page_fts AS f LEFT JOIN page_observations AS o ON o.session=f.session AND o.source_id=f.source_id JOIN pages AS p ON p.session=f.session AND p.source_id=f.source_id WHERE page_fts MATCH ? AND f.session=? AND p.tags NOT LIKE '%memory_retrieval%' ORDER BY "+order+" LIMIT ?",(expression,session,limit)).fetchall()
        return list(dict.fromkeys(r[0] for r in [*exact,*rows]))[:limit]

    def status(self, session):
        with self.store.connect() as db:
            counts = dict(db.execute("SELECT state,count(*) FROM pages WHERE session=? GROUP BY state",(session,)).fetchall())
        return {"states":counts,"ram_bytes":self.ram_bytes,"ram_limit":self.ram_limit,
                "recall_calls":self.recall_calls,"recall_cache_hits":self.recall_cache_hits,
                "raw_cache_hits":self.raw_cache_hits,"raw_database_reads":self.raw_database_reads,
                "recall_seconds":self.recall_seconds}
