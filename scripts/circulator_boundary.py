"""Read-only boundary manifests; all output stays under NEW_ROOT."""
import hashlib,json,os,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'work/synthetic-campaign'
PATHS={'old':Path('/path/to/optional-comparison-package'),'test-project':Path('/path/to/test-project'),'hermes':Path('/path/to/hermes-home'),'ai-stack':Path('/path/to/optional-server-manager'),'launch':Path('/path/to/optional-launch-profiles')}
def manifest(root):
    result={}
    files=[root] if root.is_file() or root.is_symlink() else (Path(d)/f for d,ds,fs in os.walk(root,followlinks=False) for f in fs+ [x for x in ds if (Path(d)/x).is_symlink()])
    for p in files:
        key=str(p.relative_to(root)) if p!=root else '.'
        try:
            st=p.lstat()
            if p.is_symlink(): result[key]={'link':os.readlink(p)};continue
            h=hashlib.sha256()
            with p.open('rb') as f:
                for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
            result[key]={'size':st.st_size,'sha256':h.hexdigest(),'mtime_ns':st.st_mtime_ns,'mode':st.st_mode}
        except OSError as e: result[key]={'error':str(e)}
    return result
phase=sys.argv[1]
result={k:manifest(p) for k,p in PATHS.items()}
(OUT/f'boundaries-{phase}.json').write_text(json.dumps(result,indent=2))
print({k:len(v) for k,v in result.items()},flush=True)
if phase=='after':
    before=json.loads((OUT/'boundaries-before.json').read_text()); changes={k:[p for p in set(v)|set(before[k]) if v.get(p)!=before[k].get(p)] for k,v in result.items()}
    (OUT/'boundary-comparison.json').write_text(json.dumps(changes,indent=2));print('Changes:',changes)
