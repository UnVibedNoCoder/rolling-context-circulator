"""Read literal shipped tool schemas without launching Hermes or importing plugins."""
import ast
from pathlib import Path

def schemas():
    root=Path('/path/to/hermes-agent/tools')
    values={'DEFAULT_READ_LIMIT':2000,'DEFAULT_SEARCH_LIMIT':100,'MAX_SEARCH_LIMIT':1000}
    result={}
    def literal(node):
        if isinstance(node,ast.Name):return values[node.id]
        if isinstance(node,ast.Constant):return node.value
        if isinstance(node,(ast.List,ast.Tuple)):return [literal(x) for x in node.elts]
        if isinstance(node,ast.Dict):return {literal(k):literal(v) for k,v in zip(node.keys,node.values)}
        if isinstance(node,ast.BinOp) and isinstance(node.op,ast.Add):return literal(node.left)+literal(node.right)
        raise ValueError('Nonliteral schema')
    preferred=['file_tools.py','terminal_tool.py','todo_tool.py','skills_tool.py','memory_tool.py','session_search_tool.py','web_tools.py','browser_tool.py','code_execution_tool.py']
    for p in [root/name for name in preferred]+sorted(root.glob('*.py')):
        if not p.exists():continue
        for node in ast.parse(p.read_text()).body:
            if not isinstance(node,ast.Assign):continue
            try:value=literal(node.value)
            except (ValueError,KeyError,TypeError):continue
            for target in node.targets:
                if isinstance(target,ast.Name):values[target.id]=value
            if isinstance(value,dict) and isinstance(value.get('name'),str) and isinstance(value.get('parameters'),dict):
                name=value['name']
                if any(word in name for word in ['send','delegate','spawn','delete','publish','tweet','cron','calendar','email','notify']):continue
                result.setdefault(name,{'type':'function','function':value})
    return list(result.values())
