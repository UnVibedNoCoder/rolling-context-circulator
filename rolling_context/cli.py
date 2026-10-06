"""Prepare and launch an isolated Hermes context-engine prototype."""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
import tempfile
import urllib.error
import urllib.request

from .common import compactor_process_facts, option_value
from .defaults import VALIDATED_POLICY
from .provenance import profile_provenance


PROFILES = {
    "fast": {"port": 8082, "relay_port": 8092, "model": "qwen38-27b-atx", "context_length": 65536},
    "large": {"port": 8084, "relay_port": 8094, "model": "qwen38-27b-atx-73k", "context_length": 73728},
}
DEFAULT_STATE = Path.home() / ".local/state/local-ai-control-centre/rolling-context"
SOURCE_ROOT = Path(__file__).resolve().parent.parent
MARKER = ".rolling-context-profile.json"


class PrototypeError(RuntimeError):
    pass


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def profile_home(args):
    # Hermes trusts an explicit HERMES_HOME whose immediate parent is 'profiles'.
    return args.state_dir.expanduser().resolve() / "profiles" / args.profile


def private_directory(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def private_write(path, content):
    private_directory(path.parent)
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    path.chmod(0o600)


def yaml_module():
    try:
        import yaml
    except ModuleNotFoundError as exc:
        if exc.name != "yaml":
            raise
        raise PrototypeError(
            f"PyYAML is unavailable in {sys.executable}. Run ~/.local/bin/hermes-circulator "
            "directly; its launcher uses /usr/bin/python3."
        ) from exc
    return yaml


def read_yaml(path):
    yaml = yaml_module()
    try:
        result = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise PrototypeError(f"Cannot read configuration {path}: {type(exc).__name__}") from exc
    if not isinstance(result, dict):
        raise PrototypeError(f"Configuration {path} must contain a mapping.")
    return result


def section(config, key):
    value = config.get(key)
    if not isinstance(value, dict):
        value = {}
        config[key] = value
    return value


def generated_config(source, args):
    config = copy.deepcopy(source)
    spec = PROFILES[args.profile]
    relay = f"http://127.0.0.1:{spec['relay_port']}/v1"
    main = f"http://127.0.0.1:{spec['port']}"
    config["model"] = {
        "default": spec["model"], "provider": "custom", "base_url": relay,
        "api_mode": "chat_completions", "context_length": spec["context_length"],
    }
    # This installed CLI does not read model.max_tokens. A matching custom-provider
    # extra_body is merged into the real request; the relay enforces the cap too.
    config["custom_providers"] = [{
        "name": f"Rolling context {args.profile.upper()}", "base_url": relay,
        "model": spec["model"], "api_mode": "chat_completions",
        "context_length": spec["context_length"], "extra_body": {"max_tokens": 8192},
    }]
    config.pop("providers", None)
    config["fallback_model"] = []
    compression = section(config, "compression")
    compression.update(enabled=False, micro_compact=False, proactive_prune_tokens=0,
                       idle_compact_after_seconds=0)
    context = section(config, "context")
    context["engine"] = "local_rolling"
    context["local_rolling"] = {
        "state_dir": str(args.state_dir.expanduser().resolve()), "profile": args.profile,
        "main_url": main, "compactor_url": "http://127.0.0.1:8083",
        "compactor_model": "qwen35-4b-compactor", "compactor_context_length": 65536,
        "mode": "circulator", "ram_cache_bytes": 64 * 1024 * 1024,
        "overhead_reserve": 8192,
        **copy.deepcopy(VALIDATED_POLICY),
    }
    if args.profile == "large":
        context["local_rolling"]["compactor_mode"] = "librarian"
        section(config, "agent")["reasoning_effort"] = "medium"
    auxiliary = section(config, "auxiliary")
    auxiliary["compression"] = {
        "provider": "custom", "model": "qwen35-4b-compactor",
        "base_url": "http://127.0.0.1:8083/v1", "context_length": 65536,
        "timeout": 600, "reasoning_effort": "none",
        "extra_body": {"max_tokens": VALIDATED_POLICY["summary_max_tokens"], "chat_template_kwargs": {"enable_thinking": False}},
    }
    # Independent profile directories hold no copied sessions, cron jobs or gateway
    # credentials. Keep tool/display preferences while restricting auxiliary routes.
    config["plugins"] = {"enabled": [], "disabled": [], "clone_timeout_seconds": 300}
    config["mcp_servers"] = {}
    return config


def prepare(args):
    yaml = yaml_module()
    home = profile_home(args)
    config_path = home / "config.yaml"
    marker_path = home / MARKER
    if config_path.exists() and not args.refresh:
        if not marker_path.exists():
            raise PrototypeError(f"Refusing to reuse unmarked profile {home}.")
        print(f"Profile already prepared: {home}\nUse prepare --refresh to refresh its copied configuration.")
        return
    source_home = args.source_home.expanduser().resolve()
    source = read_yaml(source_home / "config.yaml")
    private_directory(args.state_dir.expanduser().resolve())
    private_directory(home)
    if config_path.exists():
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = home / f"config.yaml.before-refresh-{stamp}"
        private_write(backup, config_path.read_text(encoding="utf-8"))
    content = yaml.safe_dump(generated_config(source, args), sort_keys=False, allow_unicode=True)
    private_write(config_path, content)
    plugin = home / "plugins/local_rolling"
    shim = ("import sys\n" + f"sys.path.insert(0, {str(SOURCE_ROOT)!r})\n"
            + "from rolling_context.engine import RollingContextEngine as _RollingContextEngine\n"
            + "class RollingContextEngine(_RollingContextEngine):\n    pass\n")
    private_write(plugin / "__init__.py", shim)
    private_write(plugin / "plugin.yaml", "name: local_rolling\nversion: 0.2.0\ndescription: Continuous local Context Circulator\n")
    # A personality copy is independent and contains no live database handles.
    soul = source_home / "SOUL.md"
    if soul.is_file() and (args.refresh or not (home / "SOUL.md").exists()):
        private_write(home / "SOUL.md", soul.read_text(encoding="utf-8"))
    if args.copy_secrets:
        source_env = source_home / ".env"
        if source_env.is_file():
            private_write(home / ".env", source_env.read_text(encoding="utf-8"))
    private_write(marker_path, json.dumps({
        "prototype": "local-rolling-context", "profile": args.profile, "created_at": utc_now(),
        "source_home": str(source_home), "source_root": str(SOURCE_ROOT),
        "config_sha256": hashlib.sha256(content.encode()).hexdigest(), "enabled": True,
    }, indent=2) + "\n")
    # Only dependency environments are shared; profile config, credentials,
    # histories and tools' state remain separate. Hermes resolves PM state at the
    # parent of profiles/, so link there rather than copying a second environment.
    for name in ("installs", "tools", "runtime"):
        source_runtime = source_home / name
        destination = args.state_dir.expanduser().resolve() / name
        if source_runtime.exists() and not destination.exists():
            destination.symlink_to(source_runtime, target_is_directory=True)
    print(f"Prepared {args.profile.upper()} profile: {home}\nGlobal Hermes configuration and sessions were not changed.")


def hermes_launcher(args):
    launcher = str(args.hermes.expanduser())
    if not Path(launcher).is_file():
        found = shutil.which(launcher)
        if not found:
            raise PrototypeError(f"Hermes launcher not found: {launcher}")
        launcher = found
    return launcher


def runtime_info(args):
    launcher = hermes_launcher(args)
    result = subprocess.run([launcher, "--print-runtime-command"], capture_output=True,
                            text=True, timeout=30, check=False)
    try:
        command = json.loads(result.stdout)
        if not isinstance(command, list) or not command or not Path(command[0]).is_file():
            raise ValueError()
    except (ValueError, TypeError):
        raise PrototypeError("Hermes lacks the supported --print-runtime-command launcher. Upgrade or supply its installed launcher.")
    # Launcher bootstrap explicitly inserts the checkout. Extract that declared path
    # rather than guessing a venv or running its application entrypoint.
    import ast
    try:
        bootstrap = command[command.index("-c") + 1]
        tree = ast.parse(bootstrap)
        root = next(node.args[1].value for node in ast.walk(tree)
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "insert" and len(node.args) == 2
                    and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str))
    except (ValueError, StopIteration, SyntaxError, AttributeError):
        raise PrototypeError("Unable to resolve Hermes source from its runtime launcher.")
    return launcher, command[0], Path(root)


def isolated_env(args):
    env = os.environ.copy()
    env["HERMES_HOME"] = str(profile_home(args))
    env.pop("HERMES_PROFILE", None)
    env.pop("PYTHONHOME", None)
    return env


def check_plugin(args):
    launcher, python, source = runtime_info(args)
    engine_path = source / "agent/context_engine.py"
    import ast
    tree = ast.parse(engine_path.read_text(encoding="utf-8"))
    methods = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    required = {"select_context", "on_turn_complete", "on_session_start", "update_model"}
    if not required <= methods:
        raise PrototypeError("Installed Hermes lacks the ContextEngine request-selection API required by this prototype.")
    if not (SOURCE_ROOT / "rolling_context/engine.py").is_file():
        raise PrototypeError("Prototype engine.py is missing.")
    home, marker = require_profile(args)
    config = read_yaml(home / 'config.yaml')
    report = profile_provenance(home, marker, config)
    require_sources(report)
    # Hermes discovery still executes the real shim, but the probe engine gets
    # a temporary store. doctor must not open or migrate production history.
    with tempfile.TemporaryDirectory(prefix='rolling-plugin-check-') as temporary:
        # Some Hermes imports initialise profile files even before load_config.
        # Mirror discovery in a temporary profile and resolve its shim symlink
        # back to the selected original, sharing only dependency environments.
        probe_root = Path(temporary)
        probe_home = probe_root/'profiles'/args.profile
        probe_home.mkdir(parents=True)
        probe_config = copy.deepcopy(config)
        probe_config['context']['local_rolling']['state_dir'] = str(probe_root/'memory')
        (probe_home/'config.yaml').write_text(yaml_module().safe_dump(probe_config))
        (probe_home/'plugins').mkdir()
        (probe_home/'plugins/local_rolling').symlink_to(home/'plugins/local_rolling', target_is_directory=True)
        for name in ('installs','tools','runtime'):
            dependency = args.state_dir.expanduser().resolve()/name
            if dependency.exists():
                (probe_root/name).symlink_to(dependency.resolve(), target_is_directory=True)
        code = f"""import sys, json, inspect, copy
sys.path.insert(0, {str(source)!r})
import hermes_bootstrap
import hermes_cli.config as config_module
config = json.loads({json.dumps(config)!r})
probe_config = copy.deepcopy(config)
probe_config['context']['local_rolling']['state_dir'] = {temporary!r}
config_module.load_config = lambda *a, **kw: probe_config
from plugins.context_engine import load_context_engine, find_engine_dir
engine = load_context_engine('local_rolling')
assert engine is not None and engine.name == 'local_rolling', 'Context engine did not load'
names = {{s['function']['name'] for s in engine.get_tool_schemas()}}
assert {{'rolling_file_snapshot','rolling_snapshot_read','rolling_raw_read'}} <= names, 'Native file tools missing'
base = next(c for c in type(engine).__mro__ if c.__module__ == 'rolling_context.engine')
from rolling_context.provenance import source_provenance
settings = engine.settings.copy()
settings['state_dir'] = config['context']['local_rolling'].get('state_dir', '~/.local/state/local-ai-control-centre/rolling-context')
report = source_provenance(settings, config['context']['local_rolling'])
report['engine_defining_file'] = str(__import__('pathlib').Path(inspect.getfile(base)).resolve())
report['discovered_profile_shim'] = str((find_engine_dir('local_rolling')/'__init__.py').resolve())
print(json.dumps(report))
"""
        env = isolated_env(args)
        env['HERMES_HOME'] = str(probe_home)
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        env['HERMES_DISABLE_LAZY_INSTALLS'] = '1'
        result = subprocess.run([python, '-I', '-B', '-c', code], env=env,
                                capture_output=True, text=True, timeout=45)
    if result.returncode:
        # Keep this read-only and avoid traceback/config disclosures.
        report['provenance_errors'].append('Context engine failed real Hermes discovery in temporary state; no production state was opened')
        require_sources(report)
    try:
        verified = json.loads(result.stdout.splitlines()[-1])
    except (ValueError, IndexError):
        raise PrototypeError('Hermes discovery did not return source provenance')
    for field in ('engine_defining_file', 'relay_source_file', 'source_fingerprint', 'policy_fingerprint'):
        if verified.get(field) != report[field]:
            report['provenance_errors'].append(f'Runtime {field} mismatch: {verified.get(field)}; expected {report[field]}')
    if verified.get('discovered_profile_shim') != str((home/'plugins/local_rolling/__init__.py').resolve()):
        report['provenance_errors'].append('Hermes discovered a different shim (possible bundled plugin collision)')
    report.update(runtime_engine_defining_file=verified.get('engine_defining_file'),
                  discovered_profile_shim=verified.get('discovered_profile_shim'))
    require_sources(report)
    args._provenance = report
    return launcher, python, source


def get_json(url, timeout=4):
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=timeout) as response:
        return json.load(response)


def check_servers(args):
    spec = PROFILES[args.profile]
    reports = []
    for label, port, alias, context in [
        (args.profile.upper(), spec["port"], spec["model"], spec["context_length"]),
        ("COMPACTOR", 8083, "qwen35-4b-compactor", 65536),
    ]:
        base = f"http://127.0.0.1:{port}"
        try:
            health = get_json(base + "/health")
            props = get_json(base + "/props")
            models = get_json(base + "/v1/models")
        except (OSError, ValueError) as exc:
            if label == "COMPACTOR":
                reports.append({"server":label,"port":port,"available":False,"reason":type(exc).__name__})
                continue
            raise PrototypeError(f"{label} server unavailable on :{port}: {type(exc).__name__}. Start it with ai-stack; this prototype never starts/stops llama.") from exc
        if health.get("status") != "ok":
            if label == "COMPACTOR":
                reports.append({"server":label,"port":port,"available":False,"reason":"unhealthy"})
                continue
            raise PrototypeError(f"{label} server on :{port} is not healthy.")
        model = next((item for item in models.get("data", []) if item.get("id") == alias), None)
        n_ctx = props.get("default_generation_settings", {}).get("n_ctx")
        if n_ctx is None and model:
            n_ctx = model.get("meta", {}).get("n_ctx")
        if model is None or n_ctx != context:
            if label == "COMPACTOR":
                reports.append({"server":label,"port":port,"available":False,"reason":"alias/context mismatch"})
                continue
            raise PrototypeError(f"{label} requires alias {alias} and context {context}; server reports context {n_ctx}.")
        reports.append({"server": label, "port": port, "model": alias, "context_length": n_ctx})
    if reports[-1].get("available") is False:
        return reports
    facts = compactor_process_facts()
    if len(facts) != 1 or facts[0]["gpu_layers"] != "0" or facts[0]["reasoning"] != "off":
        reports[-1].update(available=False,reason="CPU-only reasoning-off process verification failed")
        return reports
    reports[-1].update(facts[0])
    return reports


def require_profile(args):
    home = profile_home(args)
    marker = home / MARKER
    if not marker.is_file() or not (home / "config.yaml").is_file():
        raise PrototypeError(f"Profile is not prepared. Run prepare --profile {args.profile} first.")
    try:
        data = json.loads(marker.read_text())
    except (OSError, ValueError):
        raise PrototypeError(f"Invalid prototype profile marker at {marker}.")
    if data.get("prototype") != "local-rolling-context" or data.get("profile") != args.profile:
        raise PrototypeError(f"Profile marker does not match {args.profile}.")
    return home, data


def require_sources(report):
    if report['provenance_errors']:
        report['provenance_status'] = 'error'
        raise PrototypeError('Source provenance check failed: ' + json.dumps(report, sort_keys=True))


def doctor(args):
    home, marker = require_profile(args)
    config = read_yaml(home / "config.yaml")
    if config.get("context", {}).get("engine") != "local_rolling" or not marker.get("enabled", True):
        raise PrototypeError("Prototype profile is disabled. Run enable to reactivate it.")
    if config.get("compression", {}).get("enabled") is not False:
        raise PrototypeError("Isolated profile must set compression.enabled: false to preserve canonical history.")
    report = profile_provenance(home, marker, config)
    require_sources(report)
    launcher, python, source = check_plugin(args)
    output = {"profile": args.profile, "hermes_home": str(home),
              "hermes_launcher": launcher, "hermes_python": python,
              "hermes_source": str(source), "context_engine": "local_rolling", **args._provenance}
    try:
        output['servers'] = check_servers(args)
    except PrototypeError as error:
        output.update(servers=[], server_check_error=str(error))
        print(json.dumps(output, indent=2))
        raise
    print(json.dumps(output, indent=2))


def reject_route_overrides(argv):
    forbidden = {"-p", "--profile", "-m", "--model", "--provider", "--base-url", "--api-key", "--ignore-user-config", "--safe-mode"}
    for arg in argv:
        if arg.split("=", 1)[0] in forbidden or (arg.startswith("-p") and not arg.startswith("--") and len(arg) > 2):
            raise PrototypeError(f"Hermes route override {arg.split('=', 1)[0]} is outside this bounded prototype. Select --profile fast or large on this wrapper.")
    if argv and argv[0] == "chat":
        return argv[1:]
    if argv and not argv[0].startswith("-"):
        raise PrototypeError("run launches Hermes chat only; other Hermes subcommands should use the normal hermes launcher.")
    return argv


def run(args):
    home, marker = require_profile(args)
    if not marker.get("enabled", True):
        raise PrototypeError("Prototype is disabled; use ordinary hermes or run enable.")
    config = read_yaml(home / "config.yaml")
    if config.get("context", {}).get("engine") != "local_rolling" or config.get("compression", {}).get("enabled") is not False:
        raise PrototypeError("Prototype config changed: context.engine must be local_rolling and compression.enabled false.")
    hermes_args = args.hermes_args
    if hermes_args[:1] == ["--"]:
        hermes_args = hermes_args[1:]
    hermes_args = reject_route_overrides(hermes_args)
    report = profile_provenance(home, marker, config)
    require_sources(report)
    launcher, _, _ = check_plugin(args)
    report = args._provenance
    print('Rolling Context provenance: ' + json.dumps(report, sort_keys=True), flush=True)
    check_servers(args)
    port = PROFILES[args.profile]["relay_port"]
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            raise PrototypeError(f"Port :{port} is already occupied. Existing process was left untouched; close its owning prototype first.")
    state_dir = args.state_dir.expanduser().resolve()
    private_directory(state_dir / "logs")
    logfile = state_dir / "logs" / f"relay-{args.profile}.log"
    logfd = os.open(logfile, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    env = isolated_env(args)
    env['ROLLING_CONTEXT_LAUNCH_PROVENANCE'] = json.dumps(report, sort_keys=True)
    relay = None
    hermes = None
    previous_handlers = {}
    shutdown_signal = None

    def forward_termination(sig, frame):
        nonlocal shutdown_signal
        shutdown_signal = sig
        if hermes is not None and hermes.poll() is None:
            try:
                hermes.send_signal(sig)
            except ProcessLookupError:
                pass

    def check_shutdown():
        if shutdown_signal is not None:
            raise SystemExit(128 + shutdown_signal)

    try:
        # Register before spawning children: a terminal closing during relay
        # readiness must also reach the owned-process cleanup below.
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous_handlers[sig] = signal.signal(sig, forward_termination)
        check_shutdown()
        relay_env = env.copy()
        relay_env["PYTHONPATH"] = str(SOURCE_ROOT)
        with os.fdopen(logfd, "ab", buffering=0) as relay_log:
            relay = subprocess.Popen([
                sys.executable, "-m", "rolling_context.relay", "--profile", args.profile,
                "--state-dir", str(state_dir), "--port", str(port),
            ], env=relay_env, cwd=str(SOURCE_ROOT), stdout=relay_log, stderr=relay_log,
               start_new_session=True)
        started = time.monotonic()
        while time.monotonic() - started < 12:
            check_shutdown()
            if relay.poll() is not None:
                raise PrototypeError(f"Relay failed to start; inspect {logfile}.")
            try:
                health = get_json(f"http://127.0.0.1:{port}/rolling-context/health", timeout=1)
                if health.get("profile") == args.profile and health.get("upstream_port") == PROFILES[args.profile]["port"]:
                    if any(health.get(k) != report[k] for k in ('relay_source_file','source_fingerprint','policy_fingerprint')):
                        raise PrototypeError('Relay source/policy provenance mismatch; owned relay will be stopped')
                    break
            except (OSError, ValueError):
                pass
            time.sleep(0.1)
        else:
            raise PrototypeError(f"Relay readiness check failed; inspect {logfile}.")
        check_shutdown()
        print(f"Rolling context {args.profile.upper()}: {home}\nMemory and telemetry: {state_dir}", flush=True)
        hermes = subprocess.Popen([launcher, "chat", *hermes_args], env=env)
        # A signal can arrive while Popen is returning, before its result is
        # assigned. Forward any pending shutdown once the child is owned.
        if shutdown_signal is not None:
            forward_termination(shutdown_signal, None)

        # The owner exits on interruption even when Hermes handles SIGINT as
        # a generation cancel and keeps its UI alive. Only our direct children
        # are signalled/reaped; an occupied foreign listener never gets here.
        while True:
            check_shutdown()
            try:
                result = hermes.wait(timeout=0.2)
                # A forwarded signal can make wait return before the next loop
                # check. The owner signal determines the wrapper's exit status.
                check_shutdown()
                return result
            except subprocess.TimeoutExpired:
                pass
    finally:
        try:
            try:
                stop_owned_child(hermes)
            finally:
                stop_owned_child(relay)
        finally:
            # Keep handlers active throughout cleanup: a second Ctrl+C must not
            # escape the finally block and strand the owned relay.
            for sig, handler in previous_handlers.items():
                signal.signal(sig, handler)


def stop_owned_child(child):
    """A Popen direct-child handle is the cleanup authority, never a port/PID scan."""
    if child is None:
        return
    if child.poll() is None:
        try:
            child.terminate()
        except ProcessLookupError:
            pass
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
    else:
        child.wait()  # Reap exited children too, including failed startup.



def status(args):
    home, marker = require_profile(args)
    config = read_yaml(home / "config.yaml")
    spec = PROFILES[args.profile]
    report = {"profile": args.profile, "enabled": marker.get("enabled", True),
              "hermes_home": str(home), "state_dir": str(args.state_dir.expanduser().resolve()),
              "context_engine": config.get("context", {}).get("engine"),
              "main_port": spec["port"], "relay_port": spec["relay_port"]}
    try:
        report["relay"] = get_json(f"http://127.0.0.1:{spec['relay_port']}/rolling-context/health", timeout=1)
    except (OSError, ValueError):
        report["relay"] = "not running"
    telemetry = args.state_dir.expanduser().resolve()/"telemetry.jsonl"
    if telemetry.exists():
        with telemetry.open("rb") as handle:
            handle.seek(max(0,telemetry.stat().st_size-2*1024*1024))
            for line in handle.read().splitlines():
                try:
                    item=json.loads(line)
                    if item.get("profile")==args.profile and item.get("event")=="selection":
                        report["last_context"]=item
                except (ValueError,UnicodeDecodeError):
                    continue
    print(json.dumps(report, indent=2))


def set_enabled(args, enabled):
    home, marker = require_profile(args)
    marker["enabled"] = enabled
    marker["updated_at"] = utc_now()
    private_write(home / MARKER, json.dumps(marker, indent=2) + "\n")
    print(f"Prototype profile {args.profile} {'enabled' if enabled else 'disabled'} for future wrapper launches.\nExisting sessions are unchanged; exit them normally. Use ordinary hermes to return to the original configuration.")


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    subparsers = root.add_subparsers(dest="command", required=True)
    for command in ("prepare", "doctor", "run", "status", "disable", "enable"):
        item = subparsers.add_parser(command)
        item.add_argument("--profile", choices=PROFILES, default="large")
        item.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
        item.add_argument("--hermes", type=Path, default=Path.home() / ".local/bin/hermes")
        if command == "prepare":
            item.add_argument("--source-home", type=Path, default=Path.home() / ".hermes")
            item.add_argument("--refresh", action="store_true", help="Back up and refresh the isolated copied config.")
            item.add_argument("--copy-secrets", action="store_true", help="Explicitly copy the source .env privately for external tools; never copy auth/session databases.")
        if command == "run":
            item.add_argument("hermes_args", nargs=argparse.REMAINDER)
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        handlers = {"prepare": prepare, "doctor": doctor, "run": run, "status": status,
                    "disable": lambda a: set_enabled(a, False), "enable": lambda a: set_enabled(a, True)}
        return handlers[args.command](args) or 0
    except (PrototypeError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"hermes-rolling: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
