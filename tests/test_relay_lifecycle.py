"""Real wrapper/relay process lifetimes; temporary ports and state only."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

ROOT=Path(__file__).resolve().parents[1]

HELPER=r'''
import json, os, subprocess, sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, sys.argv[1])
from rolling_context import cli
from rolling_context.provenance import source_provenance
root=Path(sys.argv[2]);port=int(sys.argv[3]);mode=sys.argv[4]
cli.PROFILES['large']['relay_port']=port
cli.require_profile=lambda args:(root/'profile',{'enabled':True})
cli.read_yaml=lambda path:{'context':{'engine':'local_rolling'},'compression':{'enabled':False}}
report=dict(source_provenance({'profile':'large','state_dir':str(root/'state')}),provenance_errors=[])
cli.profile_provenance=lambda *args:report
cli.check_servers=lambda args:[]
cli.isolated_env=lambda args:os.environ.copy()
def check_plugin(args):
    args._provenance=report
    return(str(root/'fake-hermes'),None,None)
cli.check_plugin=check_plugin
actual=subprocess.Popen
def owned(argv,**kwargs):
    relay='rolling_context.relay' in argv
    if not relay and mode=='startup-failure':raise OSError('controlled Hermes startup failure')
    child=actual(argv,**kwargs)
    (root/('relay.pid' if relay else 'hermes.pid')).write_text(str(child.pid))
    return child
cli.subprocess.Popen=owned
args=SimpleNamespace(profile='large',state_dir=root/'state',hermes_args=[],hermes=root/'fake-hermes')
try:
    raise SystemExit(cli.run(args))
except cli.PrototypeError as e:
    print(str(e),file=sys.stderr);raise SystemExit(2)
except OSError as e:
    print(str(e),file=sys.stderr);raise SystemExit(3)
'''


class RelayLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        (self.root/'owner.py').write_text(textwrap.dedent(HELPER))
        self.processes=[]

    def tearDown(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
                try:process.wait(timeout=8)
                except subprocess.TimeoutExpired:process.kill();process.wait()
        # If an assertion fails, target only the recorded fixture relay with
        # this test's exact state-dir argument, never production listeners.
        for state in self.root.glob('run-*'):
            pidfile=state/'relay.pid'
            if pidfile.exists():
                pid=int(pidfile.read_text())
                try:
                    argv=Path('/proc',str(pid),'cmdline').read_bytes().split(b'\0')
                    if os.fsencode(state/'state') in argv and b'rolling_context.relay' in argv:
                        os.kill(pid,signal.SIGTERM)
                except OSError:pass
        self.temp.cleanup()

    def port(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));return sock.getsockname()[1]

    def launch(self, mode='normal', port=None):
        state=self.root/('run-'+str(len(self.processes)));state.mkdir()
        (state/'fake-hermes').write_text('#!'+sys.executable+'\n'+textwrap.dedent('''
            import os, signal, sys, time
            from pathlib import Path
            Path(__file__).with_name('started').touch()
            mode=Path(__file__).with_name('mode').read_text()
            if mode in ('normal','provider-abort'):raise SystemExit(0 if mode=='normal' else 9)
            signal.signal(signal.SIGINT, lambda *args: None)
            while True:time.sleep(.05)
        '''))
        (state/'fake-hermes').chmod(0o755);(state/'mode').write_text(mode)
        port=port or self.port()
        env=os.environ.copy();env.update(PYTHONDONTWRITEBYTECODE='1',HERMES_DISABLE_LAZY_INSTALLS='1')
        process=subprocess.Popen([sys.executable,str(self.root/'owner.py'),str(ROOT),str(state),str(port),mode],
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env,start_new_session=True)
        self.processes.append(process);return process,state,port

    def wait_for(self, path, process):
        end=time.monotonic()+8
        while time.monotonic()<end:
            if path.exists() and (path.name=='started' or path.read_text().strip()):return
            if process.poll() is not None:self.fail(str(process.communicate()))
            time.sleep(.02)
        self.fail('Fixture did not start')

    def finished(self, process, state, port, expected):
        output,error=process.communicate(timeout=10);self.assertEqual(process.returncode,expected,output+error)
        pidfile=state/'relay.pid'
        if pidfile.exists():
            self.assertFalse(Path('/proc',pidfile.read_text().strip()).exists(),'owned relay was not reaped')
        with socket.socket() as probe:
            self.assertNotEqual(probe.connect_ex(('127.0.0.1',port)),0,'owned relay kept listening')
        return output,error

    def test_normal_child_exit_cleans_real_owned_relay(self):
        process,state,port=self.launch();self.finished(process,state,port,0)

    def test_sigint_cleans_relay_even_when_hermes_only_cancels_generation(self):
        process,state,port=self.launch('hang');self.wait_for(state/'started',process)
        process.send_signal(signal.SIGINT);self.finished(process,state,port,130)

    def test_sigterm_cleans_owned_relay(self):
        process,state,port=self.launch('hang');self.wait_for(state/'started',process)
        process.send_signal(signal.SIGTERM);self.finished(process,state,port,143)

    def test_sigint_during_readiness_cleans_owned_relay(self):
        process,state,port=self.launch('hang');self.wait_for(state/'relay.pid',process)
        process.send_signal(signal.SIGINT);self.finished(process,state,port,130)

    def test_failed_hermes_startup_cleans_owned_relay(self):
        process,state,port=self.launch('startup-failure');self.finished(process,state,port,3)

    def test_provider_retry_abort_cleans_owned_relay(self):
        process,state,port=self.launch('provider-abort');self.finished(process,state,port,9)

    def test_foreign_listener_is_never_killed_or_replaced(self):
        foreign=self.root/'foreign.py';foreign.write_text('import socket,sys,time\ns=socket.socket();s.bind(("127.0.0.1",int(sys.argv[1])));s.listen();print("ready",flush=True);time.sleep(30)\n')
        port=self.port();process=subprocess.Popen([sys.executable,str(foreign),str(port)],stdout=subprocess.PIPE,text=True)
        self.processes.append(process);self.assertEqual(process.stdout.readline().strip(),'ready')
        owner,state,_=self.launch('normal',port);output,error=owner.communicate(timeout=8)
        self.assertEqual(owner.returncode,2,output+error);self.assertIn('left untouched',error)
        self.assertIsNone(process.poll());self.assertFalse((state/'relay.pid').exists())
        with socket.socket() as probe:self.assertEqual(probe.connect_ex(('127.0.0.1',port)),0)
        process.terminate();process.wait();process.stdout.close()

    def test_second_run_reuses_same_port_after_first_exit(self):
        first,state,port=self.launch();self.finished(first,state,port,0)
        second,state,port=self.launch(port=port);self.finished(second,state,port,0)
