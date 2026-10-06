"""Start fixed benchmark servers; stop only processes whose saved identity still matches."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT/'work/astra-servers'
BIN = '/path/to/llama-server'
MODELS = Path('/path/to/models')


def command(profile):
    cpu = profile == 'compactor'
    large = profile == 'large'
    cmd = [BIN, '--model', str(MODELS/('compactor/Qwen3.5-4B-Q4_K_M.gguf' if cpu else 'test-a/Qwen3.8-27B-ATX-4-XS.gguf')),
           '--alias', {'fast':'qwen38-27b-atx','large':'qwen38-27b-atx-73k','compactor':'qwen35-4b-compactor'}[profile],
           '--host','127.0.0.1','--port',str({'fast':8082,'large':8084,'compactor':8083}[profile]),
           '--ctx-size',str(73728 if large else 65536),'--threads',str(24 if cpu else 16),
           '--threads-batch',str(32 if cpu else 16),'--n-gpu-layers',str(0 if cpu else 999)]
    if not cpu:
        cmd += ['--split-mode','layer','--tensor-split','1.10,0.90' if large else '1.05,0.95','--flash-attn','on']
    cmd += ['--cache-type-k','q4_0' if large else 'q8_0','--cache-type-v','q4_0']
    if cpu or large:
        cmd += ['--batch-size',str(2048 if cpu else 1024),'--ubatch-size',str(512 if cpu else 128)]
    cmd += ['--parallel','1','--jinja','--temp','0.1' if cpu else '0.2','--reasoning','off' if cpu else 'on']
    if not cpu:
        cmd += ['--reasoning-effort','high','--reasoning-budget','8192','--spec-type','draft-mtp',
                '--spec-draft-n-max','2' if large else '3','--spec-draft-p-min','0']
    return cmd


def identity(pid):
    proc = Path('/proc')/str(pid)
    return {'start_ticks': (proc/'stat').read_text().split(') ',1)[1].split()[19],
            'cmdline': (proc/'cmdline').read_bytes().decode().rstrip('\0').split('\0'),
            'exe': str((proc/'exe').resolve(strict=True))}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['start','stop']);p.add_argument('profile',choices=['fast','large','compactor'])
    args=p.parse_args();STATE.mkdir(parents=True,exist_ok=True)
    path=STATE/f'{args.profile}.json'
    if args.action=='stop':
        record=json.loads(path.read_text());pid=record['pid']
        try:
            current=identity(pid)
        except (FileNotFoundError,ProcessLookupError):
            print('Owned process already exited');return
        if current != record['identity']:
            raise RuntimeError('PID identity changed; refusing to signal it')
        os.kill(pid,signal.SIGTERM)
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            try:
                current=identity(pid)
            except (FileNotFoundError,ProcessLookupError):
                print('Terminated owned',args.profile,pid);return
            if current != record['identity']:
                raise RuntimeError('PID identity changed while waiting; no further signal sent')
            if (Path('/proc')/str(pid)/'stat').read_text().split(') ',1)[1].split()[0]=='Z':
                print('Owned process exited',args.profile,pid);return
            time.sleep(.25)
        raise RuntimeError('Owned server did not exit within 30 seconds; no escalation sent')
    ports=[8083] if args.profile=='compactor' else [8082,8084]
    for port in ports:
        with socket.socket() as sock:
            if sock.connect_ex(('127.0.0.1',port))==0:
                raise RuntimeError(f'Port {port} occupied; existing server left untouched')
    env=os.environ.copy();env['CUDA_VISIBLE_DEVICES']='' if args.profile=='compactor' else '0,1'
    # FAST inherits the normal configuration, without the LARGE-only graph override.
    env.pop('GGML_CUDA_DISABLE_GRAPHS',None)
    if args.profile=='large':env['GGML_CUDA_DISABLE_GRAPHS']='1'
    cmd=command(args.profile)
    if path.exists():
        previous=path.with_name(f'{args.profile}-previous-{time.time_ns()}.json')
        previous.write_bytes(path.read_bytes())
    log=STATE/f'{args.profile}-{time.time_ns()}.log'
    with log.open('wb') as handle:
        child=subprocess.Popen(cmd,env=env,stdout=handle,stderr=subprocess.STDOUT,start_new_session=True)
    time.sleep(.2)
    record={'pid':child.pid,'identity':identity(child.pid),'command':cmd,'log':str(log),
            'environment':{k:env.get(k) for k in ['CUDA_VISIBLE_DEVICES','GGML_CUDA_DISABLE_GRAPHS']},
            'started':time.time(),'binary_sha256':hashlib.sha256(Path(BIN).read_bytes()).hexdigest()}
    path.write_text(json.dumps(record,indent=2)+'\n')
    log.with_suffix('.json').write_text(json.dumps(record,indent=2)+'\n')
    print(json.dumps(record,indent=2))
    deadline=time.monotonic()+120
    port={'fast':8082,'large':8084,'compactor':8083}[args.profile]
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while time.monotonic()<deadline:
        if child.poll() is not None:raise RuntimeError('Owned server exited during startup; inspect saved log')
        try:
            with opener.open(f'http://127.0.0.1:{port}/health',timeout=2) as response:
                if json.load(response).get('status')=='ok':
                    print('Healthy',args.profile,port);return
        except (OSError,ValueError):pass
        time.sleep(1)
    raise RuntimeError('Owned server startup health timeout; inspect saved log')


if __name__=='__main__':main()
