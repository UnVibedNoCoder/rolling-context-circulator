import copy
import tempfile
import unittest

from rolling_context.common import StateStore, generation_for_request, transport_hash, wire_hash


class TransportTests(unittest.TestCase):
    def test_transport_alias_matches_send_normalization_without_mutating_raw(self):
        original=[{'role':'user','content':'  objective  '},
                  {'role':'assistant','content':None,'reasoning_content':'private',
                   'tool_calls':[{'id':'one','function':{'name':'test','arguments':'{ "b": 2, "a": 1 }'}}]}]
        raw=copy.deepcopy(original)
        wire=[{'role':'user','content':'objective'},
              {'role':'assistant','content':'','tool_calls':[{'id':'one','function':{'name':'test','arguments':'{"a":1,"b":2}'}}]}]
        self.assertEqual(transport_hash(original),transport_hash(wire))
        self.assertEqual(original,raw)
        with tempfile.TemporaryDirectory() as td:
            store=StateStore(td)
            generation=store.generation('s',wire_hash(original),[],0,transport_alias=transport_hash(original))
            result=generation_for_request(td,wire_hash(wire),transport_hash(wire))
            self.assertEqual(result['context_generation'],generation)
            self.assertEqual(result['generation_link_basis'],'canonical_transport_hash')

    def test_different_facts_and_ambiguous_sessions_do_not_get_false_links(self):
        old=[{'role':'user','content':'port 8084'}];new=[{'role':'user','content':'port 8082'}]
        self.assertNotEqual(transport_hash(old),transport_hash(new))
        with tempfile.TemporaryDirectory() as td:
            store=StateStore(td)
            for session in ['a','b']:store.generation(session,wire_hash(old),[],0,transport_alias=transport_hash(old))
            self.assertTrue(generation_for_request(td,wire_hash(old),transport_hash(old))['generation_link_ambiguous'])
            self.assertIsNone(generation_for_request(td,wire_hash(new),transport_hash(new)))
