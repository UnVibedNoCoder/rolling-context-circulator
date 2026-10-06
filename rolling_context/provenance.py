"""Read-only source attribution and effective policy, without Hermes dependencies."""
import ast
import copy
import hashlib
import json
import os
from pathlib import Path

from .defaults import VALIDATED_POLICY

SOURCE_ROOT = Path(__file__).resolve().parent.parent


def effective_settings(settings):
    return {
        'state_dir': '~/.local/state/local-ai-control-centre/rolling-context',
        'profile': 'large', 'main_url': 'http://127.0.0.1:8084',
        'compactor_url': 'http://127.0.0.1:8083',
        'overhead_reserve': 8192, 'mode': 'circulator',
        'ram_cache_bytes': 64 * 1024 * 1024,
        **copy.deepcopy(VALIDATED_POLICY), **copy.deepcopy(settings),
    }


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


def source_fingerprint(root=SOURCE_ROOT):
    root = Path(root).resolve()
    # Hash the executable package and wrapper. Reports, indexes, and bytecode
    # never affect build identity; relocation alone also leaves it unchanged.
    paths = sorted((root/'rolling_context').rglob('*.py')) + [root/'hermes-circulator']
    return fingerprint({str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in paths})


def source_provenance(settings, explicit_settings=None, root=SOURCE_ROOT):
    root = Path(root).resolve()
    settings = effective_settings(settings)
    physical = 65536 if settings['profile'] == 'fast' else 73728
    policy = {k:v for k,v in settings.items() if k not in {'state_dir','main_url','compactor_url','file_workspace'}}
    policy.update(physical_context=physical, safety_margin_tokens=1024)
    return {'source_root': str(root),
            'engine_defining_file': str(root/'rolling_context/engine.py'),
            'relay_source_file': str(root/'rolling_context/relay.py'),
            'source_fingerprint': source_fingerprint(root),
            'effective_settings': settings,
            'explicit_settings': copy.deepcopy(explicit_settings if explicit_settings is not None else settings),
            'policy_fingerprint': fingerprint(policy),
            'physical_context': physical,
            'generation_reserve_tokens': settings['generation_reserve_tokens'],
            'safety_margin_tokens': 1024}


def profile_provenance(home, marker, config, root=SOURCE_ROOT):
    """Inspect declared shim paths without executing a missing/mismatched shim."""
    root = Path(root).resolve()
    explicit = config.get('context', {}).get('local_rolling', {})
    report = source_provenance(explicit, explicit, root)
    shim = Path(home)/'plugins/local_rolling/__init__.py'
    installed = Path.home()/'.local/bin/hermes-circulator'
    report.update(profile_shim=str(shim), profile_shim_source_root=None,
                  profile_marker_source_root=marker.get('source_root'),
                  wrapper_source_file=str(root/'hermes-circulator'),
                  installed_wrapper_resolved=str(installed.resolve()),
                  installed_wrapper_matches_source=installed.resolve()==root/'hermes-circulator',
                  provenance_errors=[], provenance_warnings=[])
    try:
        tree = ast.parse(shim.read_text(encoding='utf-8'))
        constants = {n.targets[0].id:n.value.value for n in tree.body
                     if isinstance(n, ast.Assign) and len(n.targets)==1 and isinstance(n.targets[0], ast.Name)
                     and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)}
        paths = []
        for n in ast.walk(tree):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and
                isinstance(n.func.value, ast.Attribute) and isinstance(n.func.value.value, ast.Name) and
                n.func.value.value.id=='sys' and n.func.value.attr=='path' and n.func.attr=='insert' and len(n.args)==2):
                value = n.args[1]
                path = value.value if isinstance(value, ast.Constant) else constants.get(value.id) if isinstance(value, ast.Name) else None
                if not isinstance(path, str) or not Path(path).is_absolute():
                    raise ValueError('Shim source path must be a literal absolute path')
                paths.append(str(Path(path).resolve()))
        if len(set(paths)) != 1:
            raise ValueError('Shim must declare one unambiguous source root')
        declared = Path(paths[0])
        report['profile_shim_source_root'] = str(declared)
        if not (declared/'rolling_context/engine.py').is_file():
            report['provenance_errors'].append(f'Shim engine is missing: {declared}/rolling_context/engine.py')
        elif declared != root:
            report['provenance_errors'].append(f'Shim source mismatch: {declared}; wrapper source: {root}')
    except (OSError, SyntaxError, ValueError) as error:
        report['provenance_errors'].append(f'Cannot verify profile shim {shim}: {error}')
    if report['effective_settings']['profile'] != Path(home).name:
        report['provenance_errors'].append('Effective engine profile does not match selected profile')
    marker_root = marker.get('source_root')
    if marker_root and Path(marker_root).resolve() != root:
        report['provenance_warnings'].append(f'Profile creation marker names {marker_root}; shim and runtime must verify current source')
    if not report['installed_wrapper_matches_source']:
        report['provenance_warnings'].append(f'Installed wrapper resolves to {installed.resolve()}; use {root}/hermes-circulator for this source')
    report['provenance_status'] = 'error' if report['provenance_errors'] else 'ok'
    return report


def session_provenance(settings, explicit_settings):
    report = source_provenance(settings, explicit_settings)
    home = os.environ.get('HERMES_HOME')
    report['profile_shim'] = str(Path(home)/'plugins/local_rolling/__init__.py') if home else None
    try:
        launch = json.loads(os.environ.get('ROLLING_CONTEXT_LAUNCH_PROVENANCE', '{}'))
        for key in ('profile_shim_source_root','profile_marker_source_root','wrapper_source_file',
                    'installed_wrapper_resolved','installed_wrapper_matches_source','provenance_warnings'):
            if key in launch:
                report[key] = launch[key]
    except (TypeError, ValueError):
        pass
    return report
