"""Local-only Qwen2.5-1.5B-Instruct Q4_K_M deployment control.

Scope: start / stop / status / integrity check / chat / deployment smoke check.
Python standard library only -- no third-party packages required.

Measurement honesty rules enforced here:
  * "first_content_fragment_ms" is wall clock from writing the HTTP request to
    the arrival of the first *non-empty* content delta. It therefore includes
    prompt processing, which is what a user actually feels. The framework's own
    prompt_ms is stored separately and never re-labelled as first-token latency.
  * Decode speed is only ever reported as predicted_n / predicted_ms, i.e. the
    decode window. Wall-clock totals include prefill and are labelled wall time.
  * GPU memory is reported as whole-card usage plus a before/after delta, with
    an explicit caveat: other GPU programs run here, so neither number is the
    exclusive cost of this model.
"""
import argparse
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / 'configs' / 'deployment.json'
SOURCE = ROOT / 'configs' / 'model_source.json'
STATE = ROOT / 'logs' / 'server_state.json'
LOG_DIR = ROOT / 'logs'
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
SYSTEM = '你是一个友好、准确的中文助手。根据用户要求回答。'

CREATE_DETACHED = 0x00000008
CREATE_NO_WINDOW = 0x08000000
# nvidia-smi / netstat / taskkill emit GBK text under this machine's Chinese
# system locale, so a strict utf-8 decode raises. Replace instead of dying:
# a garbled diagnostic line must never abort a check or mislabel a result.
TEXT = {'text': True, 'encoding': 'utf-8', 'errors': 'replace'}


# ---------------------------------------------------------------- utilities

def save(path, value):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def atomic_write_json(file_path, data, inject_io_error=False):
    """Atomically write JSON data using temp file + replace on Windows."""
    p = Path(file_path).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = p.parent / f"{p.name}.tmp_{os.getpid()}_{time.time_ns()}"
    content = json.dumps(data, ensure_ascii=False)
    with tmp_path.open('w', encoding='utf-8') as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    if inject_io_error:
        import errno
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise OSError(errno.EIO, "[Fault Injection] Controlled disk I/O error during atomic handshake publication (replace action failed)")
    os.replace(tmp_path, p)


def _get_modelprofiles():
    try:
        from hermes_lite import modelprofiles
        return modelprofiles
    except ImportError:
        lite_path = ROOT / 'src' / 'hermes-lite'
        if str(lite_path) not in sys.path:
            sys.path.insert(0, str(lite_path))
        from hermes_lite import modelprofiles
        return modelprofiles


def config(override_path=None, explicit_profile_id=None):
    """Resolve the config this command should act on.

    Round 44 -- unified resolution:
    * `--config PATH`: explicit file path. Must exist and be valid JSON.
      If missing, raises FileNotFoundError.
    * `--profile ID`: explicit profile ID. Must exist and be usable in registry.
      If missing or unusable, raises ProfileError.
    * No explicit override: calls modelprofiles.resolve_startup_target(), which
      unifies startup order:
        registry default -> last verified success -> any usable profile ->
        deployment file / registry error fallback.
    """
    if override_path:
        cfg_path = Path(override_path)
        if not cfg_path.is_file():
            raise FileNotFoundError(f"指定的配置文件不存在：{cfg_path}")
        return json.loads(cfg_path.read_text(encoding='utf-8-sig'))

    mp = _get_modelprofiles()
    res = mp.resolve_startup_target(
        explicit_profile_id=explicit_profile_id,
        explicit_config_path=override_path,
    )
    if res.get('fallback_reason'):
        print(f"INFO: [配置选择回退] {res['fallback_reason']}", file=sys.stderr)
    return res['effective_config']


def profile_model_overrides(explicit_profile_id=None):
    """Model-level overrides from the unified target resolution."""
    mp = _get_modelprofiles()
    res = mp.resolve_startup_target(explicit_profile_id=explicit_profile_id)
    prof = res.get('profile')
    if prof:
        return mp.profile_model_overrides(prof)
    return {}



def save_config(c):
    """Persist DEPLOYMENT-level facts, keeping the deployment's own model fields.

    The classic launcher may move the port when the preferred one is busy, and
    that change belongs in `deployment.json`. Model-level fields that came from a
    PROFILE must not be copied in: that would turn a temporary profile selection
    into a permanent deployment change. They are also not deleted -- the file
    keeps whatever it already said, so it remains a usable fallback if the
    registry is ever removed.
    """
    from_profile = profile_model_overrides()
    payload = dict(c)
    if from_profile:
        try:
            original = json.loads(CONFIG.read_text(encoding='utf-8-sig'))
        except Exception:  # noqa: BLE001
            original = {}
        for key in from_profile:
            if key in original:
                payload[key] = original[key]
            else:
                payload.pop(key, None)
    CONFIG.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                      encoding='utf-8')



def load_state():
    """utf-8-sig tolerates a BOM: an editor or PowerShell Set-Content may add one,
    and a state file that cannot be read would block both status and stop."""
    return json.loads(STATE.read_text(encoding='utf-8-sig'))


def now_iso():
    return time.strftime('%Y-%m-%dT%H:%M:%S%z')


def run(argv, timeout=20):
    return subprocess.run(argv, capture_output=True, timeout=timeout,
                          creationflags=CREATE_NO_WINDOW, **TEXT)


def gpu():
    """Whole-card GPU counters."""
    try:
        r = run(['nvidia-smi',
                 '--query-gpu=name,memory.total,memory.used,memory.free,utilization.gpu,driver_version',
                 '--format=csv,noheader,nounits'], timeout=15)
        if r.returncode or not r.stdout:
            return {'error': (r.stderr or '').strip() or f'nvidia-smi exit {r.returncode}'}
        row = [x.strip() for x in r.stdout.strip().splitlines()[0].split(',')]
        return {'name': row[0], 'total_MiB': int(row[1]), 'used_MiB': int(row[2]),
                'free_MiB': int(row[3]), 'util_pct': int(row[4]), 'driver': row[5],
                'at': now_iso()}
    except Exception as exc:  # diagnostics must never abort a run
        return {'error': repr(exc)}


def gpu_processes():
    """PIDs with a CUDA context. Per-process memory reads [N/A] on this driver,
    so this is a presence list, not a per-process cost table."""
    try:
        r = run(['nvidia-smi', '--query-compute-apps=pid,process_name,used_memory',
                 '--format=csv,noheader,nounits'], timeout=15)
        rows = []
        for line in (r.stdout or '').splitlines():
            parts = [x.strip() for x in line.split(',')]
            if len(parts) >= 3 and parts[0]:
                rows.append({'pid': parts[0], 'name': parts[1], 'used_MiB': parts[2]})
        return rows
    except Exception as exc:  # noqa: BLE001
        return [{'error': repr(exc)}]


def http_json(path, method='GET', body=None, timeout=20, base=None):
    if base is None:
        base = config()['base_url'].removesuffix('/v1')
    else:
        base = base.removesuffix('/v1')
    data, headers = None, {}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    with HTTP.open(req, timeout=timeout) as r:
        return json.load(r)


def port_free(host, port):
    """True only when the port can really be exclusively bound.

    SO_REUSEADDR must NOT be set. On Windows SO_REUSEADDR lets a socket bind to
    a port another process is already listening on (on Unix it only affects
    TIME_WAIT state), so a REUSEADDR probe reports a busy port as free;
    llama-server then cannot bind and retries silently until the holder exits.
    That is exactly what made one start take 139 s instead of ~2 s. The netstat
    lookup is kept as a second opinion.
    """
    if listener_pid(port) is not None:
        return False
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def listener_pid(port):
    """PID owning the listening TCP socket on `port`, so stop can cross-check
    that the socket really belongs to the process it is about to kill."""
    if port is None:
        return None
    r = run(['netstat', '-ano', '-p', 'TCP'], timeout=20)
    for line in (r.stdout or '').splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[3].upper() == 'LISTENING' and parts[1].endswith(f':{port}'):
            try:
                return int(parts[4])
            except ValueError:
                continue
    return None


# ------------------------------------------------------- process identity

def process_identity(pid):
    if not pid:
        return None
    import ctypes
    from ctypes import wintypes as w
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    k.OpenProcess.restype = w.HANDLE
    k.CloseHandle.argtypes = [w.HANDLE]
    k.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
    k.GetProcessTimes.argtypes = [w.HANDLE] + [ctypes.POINTER(ctypes.c_ulonglong)] * 4
    handle = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        buf = ctypes.create_unicode_buffer(32768)
        n = w.DWORD(len(buf))
        if not k.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(n)):
            return None
        times = [ctypes.c_ulonglong() for _ in range(4)]
        if not k.GetProcessTimes(handle, *[ctypes.byref(x) for x in times]):
            return None
        return {'exe': buf.value, 'creation_time': times[0].value}
    finally:
        k.CloseHandle(handle)


def terminate_process_with_identity(pid, expected_ident, timeout_s=3.0):
    """Safely terminate a process only if its identity matches expected_ident.
    First requests graceful exit; if still running, re-verifies identity and force-kills.
    Never terminates if identity is missing or mismatched.
    """
    if not pid or not expected_ident:
        return False, "Missing pid or expected_ident; refusing to terminate unverified process."
    curr = process_identity(pid)
    if curr is None:
        return True, "Process is already stopped."
    if curr != expected_ident:
        return False, f"Process identity mismatch for PID {pid} (expected {expected_ident}, got {curr}); refusing to terminate."

    # 1. Graceful exit request (taskkill without /F)
    try:
        run(['taskkill', '/PID', str(pid)], timeout=10)
    except Exception:
        pass

    began = time.perf_counter()
    while time.perf_counter() - began < min(1.5, timeout_s):
        if process_identity(pid) is None:
            return True, "Process terminated gracefully."
        time.sleep(0.1)

    # 2. Force termination after re-verifying identity
    recheck = process_identity(pid)
    if recheck and recheck == expected_ident:
        run(['taskkill', '/PID', str(pid), '/F'], timeout=10)
        while time.perf_counter() - began < timeout_s:
            if process_identity(pid) is None:
                return True, "Process force-terminated."
            time.sleep(0.1)
        if process_identity(pid) is None:
            return True, "Process terminated."
        return False, "Process failed to terminate within timeout."
    elif recheck is None:
        return True, "Process terminated."
    else:
        return False, f"Process identity changed before force kill; refusing to terminate {recheck}."


# ------------------------------------------------------------ model check

def model_file_of(c):
    """Absolute path of the model this config names.

    `configs/deployment.json` stores a project-relative path; a model profile
    stores an absolute one. `ROOT / absolute` yields the absolute path, so both
    forms work, but a relative path is explicitly anchored to the project root
    instead of the current working directory.
    """
    raw = c['model']
    p = Path(raw)
    return p.resolve() if p.is_absolute() else (ROOT / raw).resolve()


def _same_model_target(old_state, c):
    """Is the already-running server the SAME model target as this config?

    Compared: model file, alias, context size. Anything else (batch size, GPU
    layers, ...) cannot be read back from a live server, so it is not compared
    here; the desktop's own post-launch verification covers the window.
    """
    try:
        old_model = old_state.get('model')
        if not old_model:
            return False
        if model_file_of({'model': old_model}) != model_file_of(c):
            return False
        if (old_state.get('alias') or '') != (c.get('alias') or ''):
            return False
        old_ctx = (old_state.get('config') or {}).get('context_size')
        if old_ctx is not None and int(old_ctx) != int(c.get('context_size')):
            return False
        return True
    except Exception:  # noqa: BLE001
        return False


def check_model(raise_on_fail=True):
    import hashlib
    c = config()
    f = model_file_of(c)
    if not f.exists():
        raise RuntimeError(f'Model is not installed: {f}')

    # Round 43: `configs/model_source.json` pins the ORIGINAL Qwen download
    # (size + sha256 + provenance). A different model file has no such pin, so a
    # full-file hash against the wrong expected digest would report a perfectly
    # good file as corrupt. Only the pinned model is verified against the pin;
    # anything else is checked for the facts that do not need provenance.
    meta = None
    pinned = False
    try:
        meta = json.loads(SOURCE.read_text(encoding='utf-8-sig'))
        exp_size = int(meta['file']['size'])
        exp_sha = meta['file']['lfs']['sha256'].lower()
        pinned = f.stat().st_size == exp_size
    except Exception:  # noqa: BLE001
        exp_size, exp_sha, meta = None, None, None

    h = hashlib.sha256()
    with f.open('rb') as stream:
        header = stream.read(24)
        stream.seek(0)
        if pinned:
            for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b''):
                h.update(chunk)
    size = f.stat().st_size
    result = {
        'path': str(f), 'bytes': size,
        'sha256': h.hexdigest() if pinned else None,
        'expected_bytes': exp_size, 'expected_sha256': exp_sha,
        'pinned_to_source': pinned,
        'gguf_magic': header[:4].decode('latin-1'),
        'gguf_version': int.from_bytes(header[4:8], 'little'),
        'gguf_tensor_count': int.from_bytes(header[8:16], 'little'),
        'source': ({'repo': meta['repo'], 'revision': meta['revision'],
                    'file': meta['file']['rfilename'],
                    'blobId': meta['file'].get('blobId')} if (meta and pinned) else None),
        'checked_at': now_iso(),
    }
    result['magic_ok'] = result['gguf_magic'] == 'GGUF'
    if pinned:
        result['size_ok'] = size == exp_size
        result['sha_ok'] = result['sha256'] == exp_sha
        result['pass'] = result['size_ok'] and result['sha_ok'] and result['magic_ok']
    else:
        # Unpinned model: the honest check is "a readable GGUF file of non-zero
        # size". It is NOT a provenance claim, and the report says so.
        result['size_ok'] = size > 0
        result['sha_ok'] = None
        result['pass'] = result['size_ok'] and result['magic_ok']
        result['verification_scope'] = ('not-pinned: only file presence and GGUF '
                                        'header were checked, no provenance')
    save(LOG_DIR / 'model_integrity.json', result)
    if not result['pass'] and raise_on_fail:
        raise RuntimeError('Model integrity check failed; refusing to load.')
    return result



# ------------------------------------------------------------ log parsing

PATTERNS = [
    ('architecture', r'general\.architecture'),
    ('offload', r'offload'),
    ('tensors', r'load_tensors:'),
    ('ctx', r'llama_context:'),
    ('kv', r'llama_kv_cache'),
    ('model', r'llama_model_loader:'),
    ('device', r'(GGML_CUDA|ggml_cuda_init|Device 0:|load_backend: loaded)'),
    ('listening', r'(server is listening|starting the main loop)'),
    ('graph', r'graph nodes|graph splits'),
]


def scan_log():
    """Extract the load-time facts llama.cpp itself reports, so offload status
    and the model / KV / compute buffer split come from the runtime rather than
    from assumptions."""
    lines = []
    for path in (LOG_DIR / 'server.stderr.log', LOG_DIR / 'server.stdout.log'):
        if path.exists():
            text = path.read_text(encoding='utf-8', errors='replace')
            lines.extend(ln for ln in text.splitlines()
                         if any(re.search(p, ln) for _, p in PATTERNS))
    summary = {}
    offload = None
    for ln in lines:
        hit = re.search(r'offloaded (\d+)/(\d+) layers to GPU', ln)
        if hit:
            offload = hit
            break
    summary['layers_offloaded'] = f'{offload.group(1)}/{offload.group(2)}' if offload else None
    summary['all_layers_on_gpu'] = bool(offload and offload.group(1) == offload.group(2))
    summary['offload_lines'] = [ln.strip() for ln in lines if 'offload' in ln]
    summary['buffer_lines'] = [ln.strip() for ln in lines
                               if 'buffer size' in ln or 'graph splits' in ln
                               or 'compute buffer' in ln or 'KV self size' in ln]
    summary['context_lines'] = [ln.strip() for ln in lines
                                if re.search(r'n_ctx|n_batch|n_ubatch|n_seq_max|flash_attn|kv_unified', ln)]
    summary['device_lines'] = [ln.strip() for ln in lines if re.search(PATTERNS[6][1], ln)]
    summary['raw_line_count'] = len(lines)
    return summary


# ------------------------------------------------------------------ start

def pick_port(c):
    host = c['host']
    preferred = int(c['port'])
    if port_free(host, preferred):
        return preferred, False
    for cand in range(preferred + 1, preferred + 41):
        if port_free(host, cand):
            return cand, True
    raise RuntimeError(f'No free port on {host} in {preferred}..{preferred + 40}')


def start(handshake_file=None, managed=False, switch_port=False, port_override=None,
          parent_pid=None, token=None, config_override=None, profile_override=None,
          parent_auth_event=None,
          test_delay_spawn=0.0, test_fail_handshake=False, test_corrupt_handshake=False,
          test_mismatch_token=False, test_identity_mode=None, test_exit_before_handshake=False,
          test_child_spawned_event=None, test_pause_before_handshake_event=None,
          test_diag_file=None):
    c = config(config_override, explicit_profile_id=profile_override)
    port = int(port_override) if port_override is not None else int(c['port'])
    host = c['host']
    launch_token = token or f"launch_{os.getpid()}_{time.time_ns()}"

    if STATE.exists():
        try:
            old = load_state()
            ident = process_identity(old.get('pid', 0)) if old.get('pid') else None
            if ident and ident == old.get('identity'):
                try:
                    health = http_json('/health', timeout=3, base=f"http://{host}:{port}")
                except (OSError, ValueError, urllib.error.URLError):
                    health = None
                # Round 43: a "reuse" is only honest when the server that is
                # already listening was started for the SAME model file and the
                # SAME alias. Otherwise a model switch would hand back a
                # successful handshake for the OLD model, and the app would have
                # to catch that afterwards. Better to refuse the reuse here.
                same_model = _same_model_target(old, c)
                if health and health.get('status') == 'ok' and same_model:
                    res_event = {
                        'event': 'reused',
                        'already_running': True,
                        'pid': old['pid'],
                        'identity': ident,
                        'port': old.get('port', port),
                        'base_url': old.get('base_url'),
                        'health': health,
                        'token': launch_token
                    }
                    if handshake_file:
                        atomic_write_json(handshake_file, res_event)
                    print(f"@@HANDSHAKE@@:{json.dumps(res_event, ensure_ascii=False)}", flush=True)
                    print(json.dumps(res_event, ensure_ascii=False, indent=2))
                    return
        except Exception:
            pass

    moved = False
    if not port_free(host, port):
        if not switch_port:
            raise RuntimeError(f"Port {port} on {host} is occupied; refusing to switch port or alter deployment configuration.")
        port, moved = pick_port(c)
        print(f'port {c["port"]} is occupied; switching to {port}')
        c['port'] = port
        c['base_url'] = f'http://{c["host"]}:{port}/v1'
        save_config(c)

    integrity = {'sha256': 'test_mock_integrity'} if c.get('skip_integrity') else check_model()
    before = gpu()
    exe = ROOT / c['runtime']
    base_py = getattr(sys, '_base_executable', sys.executable)
    base_cmd = [base_py, str(exe)] if str(exe).endswith('.py') else [str(exe)]
    argv = base_cmd + ['-m', str(model_file_of(c)),
            '--host', host, '--port', str(port), '--alias', c['alias'],
            '-c', str(c['context_size']),
            '-ngl', str(c['gpu_layers_requested']),
            '-np', str(c['parallel']),
            '-b', str(c['batch_size']), '-ub', str(c['ubatch_size']),
            '-t', '6', '-tb', '6',
            '--jinja', '--offline', '--metrics', '--slots']
    if c.get('flash_attention'):
        argv.append('-fa')
    if c.get('delay_ready'):
        argv.extend(['--delay-ready', str(c['delay_ready'])])
    if c.get('exit_early'):
        argv.extend(['--exit-early', str(c['exit_early'])])

    if handshake_file:
        try:
            atomic_write_json(
                handshake_file,
                {'event': 'spawning', 'launcher_pid': os.getpid(), 'token': launch_token}
            )
        except Exception:
            pass

    began = time.perf_counter()
    out_path, err_path = LOG_DIR / 'server.stdout.log', LOG_DIR / 'server.stderr.log'
    proc = None
    actual_pid = None
    actual_ident = None

    stop_monitor = threading.Event()
    parent_aborted = threading.Event()

    import ctypes
    k32 = ctypes.windll.kernel32
    SYNCHRONIZE = 0x00100000
    WAIT_OBJECT_0 = 0x00000000
    WAIT_TIMEOUT = 0x00000102

    h_parent = None
    expected_parent_ident = None
    if parent_pid:
        expected_parent_ident = process_identity(parent_pid)
        if expected_parent_ident is None:
            parent_aborted.set()
        else:
            h_parent = k32.OpenProcess(SYNCHRONIZE, False, parent_pid)
            if not h_parent:
                parent_aborted.set()

    def parent_monitor():
        nonlocal h_parent
        while not stop_monitor.is_set():
            if parent_aborted.is_set():
                break
            if h_parent:
                res = k32.WaitForSingleObject(h_parent, 200)
                if res == WAIT_OBJECT_0:
                    parent_aborted.set()
                    break
                elif res != WAIT_TIMEOUT:
                    parent_aborted.set()
                    break
                cur_p_ident = process_identity(parent_pid)
                if cur_p_ident != expected_parent_ident:
                    parent_aborted.set()
                    break
            else:
                if parent_pid and process_identity(parent_pid) is None:
                    parent_aborted.set()
                    break
                time.sleep(0.2)

    if managed:
        t_mon = threading.Thread(target=parent_monitor, daemon=True)
        t_mon.start()

    try:
        if parent_auth_event:
            open_began = time.perf_counter()
            h_auth = None
            while time.perf_counter() - open_began < 5.0:
                if parent_aborted.is_set():
                    raise RuntimeError("Parent process exited or aborted before authorizing model launch.")
                h_auth = k32.OpenEventW(SYNCHRONIZE, False, parent_auth_event)
                if h_auth:
                    break
                time.sleep(0.05)
            if not h_auth:
                raise RuntimeError(f"Failed to open parent auth event '{parent_auth_event}'.")
            try:
                auth_began = time.perf_counter()
                authorized = False
                while time.perf_counter() - auth_began < 15.0:
                    if parent_aborted.is_set():
                        raise RuntimeError("Parent process exited or aborted before authorizing model launch.")
                    res = k32.WaitForSingleObject(h_auth, 100)
                    if res == WAIT_OBJECT_0:
                        authorized = True
                        break
                if not authorized:
                    raise RuntimeError("Timed out waiting for parent authorization / Job Object assignment.")
            finally:
                k32.CloseHandle(h_auth)

        if test_delay_spawn > 0:
            time.sleep(test_delay_spawn)

        with out_path.open('wb') as out, err_path.open('wb') as err:
            proc = subprocess.Popen(argv, cwd=str(exe.parent), stdin=subprocess.DEVNULL,
                                    stdout=out, stderr=err,
                                    creationflags=CREATE_DETACHED | CREATE_NO_WINDOW)
        actual_pid = proc.pid
        actual_ident = process_identity(actual_pid)

        if test_diag_file:
            try:
                atomic_write_json(test_diag_file, {
                    'pid': actual_pid,
                    'identity': actual_ident,
                    'launcher_pid': os.getpid(),
                    'launcher_identity': process_identity(os.getpid())
                })
            except Exception:
                pass

        if test_child_spawned_event:
            EVENT_MODIFY_STATE = 0x0002
            h_spawned = k32.OpenEventW(EVENT_MODIFY_STATE, False, test_child_spawned_event)
            if h_spawned:
                k32.SetEvent(h_spawned)
                k32.CloseHandle(h_spawned)

        if test_pause_before_handshake_event:
            h_pause = k32.OpenEventW(SYNCHRONIZE, False, test_pause_before_handshake_event)
            if h_pause:
                try:
                    pause_began = time.perf_counter()
                    while time.perf_counter() - pause_began < 30.0:
                        if parent_aborted.is_set():
                            raise RuntimeError("Parent process aborted while paused before handshake; cleaning up.")
                        res = k32.WaitForSingleObject(h_pause, 100)
                        if res == WAIT_OBJECT_0:
                            break
                finally:
                    k32.CloseHandle(h_pause)

        if test_exit_before_handshake:
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except Exception:
                pass
            # Update actual_ident to None since process is dead
            actual_ident = None

        msg_ident = actual_ident
        if test_identity_mode == 'none':
            msg_ident = None
        elif test_identity_mode == 'missing_fields':
            msg_ident = {'exe': actual_ident.get('exe', 'dummy.exe')} if actual_ident else {'exe': 'dummy.exe'}
        elif test_identity_mode == 'mismatched':
            msg_ident = {'exe': 'C:\\fake_nonexistent_process.exe', 'creation_time': 123456789}

        msg_token = "mismatched_token_999" if test_mismatch_token else launch_token

        spawned_event = {
            'event': 'spawned',
            'launcher_pid': os.getpid(),
            'pid': actual_pid,
            'identity': msg_ident,
            'port': port,
            'base_url': f"http://{host}:{port}/v1",
            'token': msg_token,
            'started_at': now_iso()
        }

        if test_corrupt_handshake:
            if handshake_file:
                Path(handshake_file).write_text("{corrupted_handshake_json", encoding='utf-8')
            print("@@HANDSHAKE@@:{corrupted_handshake_json", flush=True)
        else:
            if handshake_file:
                atomic_write_json(handshake_file, spawned_event, inject_io_error=test_fail_handshake)
            print(f"@@HANDSHAKE@@:{json.dumps(spawned_event, ensure_ascii=False)}", flush=True)

        state = {'pid': actual_pid, 'identity': actual_ident, 'token': launch_token,
                 'launcher_pid': os.getpid(), 'argv': argv,
                 'cwd': str(exe.parent), 'base_url': f"http://{host}:{port}/v1", 'port': port,
                 'health_url': f'http://{host}:{port}',
                 'port_moved_from_preferred': moved, 'started_at': now_iso(),
                 'integrity_sha256': integrity['sha256'], 'model': c['model'],
                 'alias': c['alias'], 'config': c, 'gpu_before_load': before,
                 'status': 'starting'}
        save(STATE, state)

        while time.perf_counter() - began < 180:
            if parent_aborted.is_set():
                raise RuntimeError("Parent process aborted during startup; cleaning up.")

            if proc.poll() is not None:
                state.update(status='exited', returncode=proc.returncode)
                save(STATE, state)
                tail = err_path.read_text(encoding='utf-8', errors='replace')[-4000:]
                raise RuntimeError(f'Server exited with code {proc.returncode}\n{tail}')

            try:
                health = http_json('/health', timeout=3, base=f"http://{host}:{port}")
                if health.get('status') == 'ok':
                    state['ready_seconds'] = round(time.perf_counter() - began, 3)
                    state['health'] = health
                    state['models'] = http_json('/v1/models', timeout=10, base=f"http://{host}:{port}")
                    try:
                        props = http_json('/props', timeout=10, base=f"http://{host}:{port}")
                        tpl = props.pop('chat_template', None)
                        state['props'] = props
                        if tpl:
                            (LOG_DIR / 'chat_template.jinja').write_text(tpl, encoding='utf-8')
                            state['chat_template_path'] = str(LOG_DIR / 'chat_template.jinja')
                    except (OSError, ValueError, urllib.error.URLError):
                        state['props'] = None
                    state['gpu_after_load'] = gpu()
                    state['log_scan'] = scan_log()
                    state['listener_pid'] = listener_pid(port)
                    state['status'] = 'ready'
                    save(STATE, state)
                    ready_event = {
                        'event': 'ready',
                        'pid': actual_pid,
                        'identity': msg_ident,
                        'port': port,
                        'base_url': f"http://{host}:{port}/v1",
                        'ready_seconds': state['ready_seconds'],
                        'status': 'ready',
                        'listener_pid': state['listener_pid'],
                        'token': msg_token
                    }
                    if test_corrupt_handshake:
                        if handshake_file:
                            Path(handshake_file).write_text("{corrupted_ready_json", encoding='utf-8')
                        print("@@READY@@:{corrupted_ready_json", flush=True)
                    else:
                        if handshake_file:
                            try:
                                atomic_write_json(handshake_file, ready_event)
                            except Exception:
                                pass
                        print(f"@@READY@@:{json.dumps(ready_event, ensure_ascii=False)}", flush=True)
                    print(json.dumps({k: state[k] for k in
                                      ('pid', 'port', 'base_url', 'ready_seconds',
                                       'status', 'listener_pid')},
                                     ensure_ascii=False, indent=2))
                    print(json.dumps({k: state['log_scan'][k] for k in
                                      ('layers_offloaded', 'all_layers_on_gpu')},
                                     ensure_ascii=False, indent=2))
                    for ln in state['log_scan']['offload_lines'][:6]:
                        print('  ' + ln)
                    after = state.get('gpu_after_load', {}).get('used_MiB')
                    if isinstance(after, int) and isinstance(before.get('used_MiB'), int):
                        print(f"  whole-card VRAM: {before['used_MiB']} MiB before -> {after} MiB "
                              f"after load (delta {after - before['used_MiB']} MiB; other GPU "
                              f"programs are running, so this is not solely this model)")

                    if not managed:
                        stop_monitor.set()
                        if h_parent:
                            try:
                                k32.CloseHandle(h_parent)
                            except Exception:
                                pass
                        return

                    # Managed mode: stay running supervising parent_pid until parent aborts or stop requested
                    print(f"@@SUPERVISOR_RUNNING@@:{json.dumps({'launcher_pid': os.getpid(), 'actual_pid': actual_pid})}", flush=True)
                    try:
                        while not parent_aborted.is_set() and not stop_monitor.is_set():
                            if proc.poll() is not None:
                                break
                            time.sleep(0.2)
                    except KeyboardInterrupt:
                        pass
                    finally:
                        stop_monitor.set()
                        if h_parent:
                            try:
                                k32.CloseHandle(h_parent)
                            except Exception:
                                pass
                            h_parent = None
                        if actual_pid and actual_ident:
                            terminate_process_with_identity(actual_pid, actual_ident, timeout_s=3.0)
                    return
            except (OSError, ValueError, urllib.error.URLError):
                pass
            time.sleep(0.3)

        raise RuntimeError('Server readiness timed out. See logs/server.stderr.log')
    except Exception:
        stop_monitor.set()
        if h_parent:
            try:
                k32.CloseHandle(h_parent)
            except Exception:
                pass
            h_parent = None
        # Creator cleanup responsibility: if startup failed or aborted, terminate the child server we spawned!
        if actual_pid and actual_ident:
            try:
                terminate_process_with_identity(actual_pid, actual_ident, timeout_s=3.0)
            except Exception:
                pass
        raise


# ------------------------------------------------------------------- stop

def stop(expected_pid=None, expected_ident=None):
    if not STATE.exists():
        print('No managed server state; nothing to stop.')
        return
    s = load_state()
    pid = s.get('pid')
    if expected_pid is not None and pid != expected_pid:
        print(f"Recorded PID {pid} does not match expected PID {expected_pid}; refusing to stop.")
        return

    ident = process_identity(pid) if pid else None
    if ident is None:
        print('Managed server is already stopped.')
        s.update(status='stopped', stopped_at=now_iso())
        save(STATE, s)
        return

    target_ident = expected_ident or s.get('identity')
    if not target_ident:
        raise RuntimeError("Missing recorded identity; refusing to terminate unverified process by PID.")

    if ident != target_ident:
        raise RuntimeError('Process identity mismatch (pid likely reused); refusing to stop.\n'
                           f'  recorded: {target_ident}\n  observed: {ident}')
    expected_exe = str(ROOT / config()['runtime'])
    if Path(ident['exe']).as_posix().lower() != Path(expected_exe).as_posix().lower():
        raise RuntimeError(f'PID {pid} now runs {ident["exe"]}; refusing to stop.')

    owner = listener_pid(s.get('port'))
    if owner is not None and owner != pid:
        raise RuntimeError(f'Port {s.get("port")} is held by pid {owner}, not managed pid {pid}; '
                           'refusing to stop.')

    ok, msg = terminate_process_with_identity(pid, target_ident, timeout_s=5.0)
    if not ok:
        raise RuntimeError(msg)

    s.update(status='stopped', stopped_at=now_iso(), gpu_after_stop=gpu())
    save(STATE, s)
    print(f'Managed local server stopped ({msg}).')


# ----------------------------------------------------------------- status

def status():
    out = {'config': config(), 'gpu': gpu()}
    if STATE.exists():
        s = load_state()
        ident = process_identity(s.get('pid', 0)) if s.get('pid') else None
        out['process'] = {'pid': s.get('pid'), 'alive': ident is not None,
                          'identity_matches': ident == s.get('identity'),
                          'port': s.get('port'), 'started_at': s.get('started_at'),
                          'recorded_status': s.get('status'),
                          'ready_seconds': s.get('ready_seconds')}
        if s.get('log_scan'):
            out['load_report'] = {k: s['log_scan'].get(k) for k in
                                  ('layers_offloaded', 'all_layers_on_gpu')}
        # Only report CUDA processes belonging to this project, so the output is
        # about this deployment rather than every unrelated app on the desktop.
        mine = str(ROOT).lower()
        out['this_project_gpu_processes'] = [
            p for p in gpu_processes() if str(p.get('name', '')).lower().startswith(mine)]
    try:
        out['health'] = http_json('/health', timeout=5)
        out['models'] = http_json('/v1/models', timeout=5)
    except Exception as exc:  # noqa: BLE001
        out['health'] = None
        out['error'] = repr(exc)
    print(json.dumps(out, ensure_ascii=False, indent=2))


# -------------------------------------------------------------- inference

def stream_chat(messages, max_tokens=256, sampler=None, timeout=600, on_content=None):
    """One streaming chat call, returning everything needed to judge quality and
    timing, including every raw SSE chunk for later re-examination.

    sampler:
      None      -> the frozen greedy eval defaults (see configs/sampling_eval.json)
      'server'  -> send *no* sampler fields, so llama-server applies its own
                   defaults; the only way to test true default sampling
      dict      -> explicit override, e.g. configs/sampling_chat.json["params"]

    on_content: called with each non-empty delta string the moment it arrives.
    It is purely a display hook. The reply is still built from the internal
    accumulator, so printing never depends on and never duplicates the returned
    `reply`. Two distinct timings are recorded:
      first_content_fragment_ms  - wire arrival of the first content delta
      first_render_ms            - when the display callback last returned
    i.e. interface latency vs what the user actually saw painted.
    """
    c = config()
    body = {'model': c['alias'], 'messages': messages, 'stream': True,
            'stream_options': {'include_usage': True},
            'temperature': 0, 'top_k': 1, 'top_p': 1, 'repeat_penalty': 1.0,
            'seed': 0, 'max_tokens': max_tokens, 'cache_prompt': False}
    if sampler == 'server':
        body = {'model': c['alias'], 'messages': messages, 'stream': True,
                'stream_options': {'include_usage': True},
                'max_tokens': max_tokens, 'cache_prompt': False}
    elif isinstance(sampler, dict):
        body.update(sampler)
        if max_tokens is not None and 'max_tokens' not in sampler:
            body['max_tokens'] = max_tokens
    req = urllib.request.Request(c['base_url'] + '/chat/completions',
                                 data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
                                 headers={'Content-Type': 'application/json'})
    began = time.perf_counter()
    first_content = first_event = first_render = None
    chunks, parts = [], []
    with HTTP.open(req, timeout=timeout) as r:
        for raw in r:
            if not raw.startswith(b'data: '):
                continue
            payload = raw[6:].strip()
            if payload == b'[DONE]':
                break
            chunk = json.loads(payload)
            if first_event is None:
                first_event = time.perf_counter()
            chunks.append(chunk)
            for choice in chunk.get('choices', []):
                piece = choice.get('delta', {}).get('content') or ''
                if piece:
                    if first_content is None:
                        first_content = time.perf_counter()
                    parts.append(piece)
                    # Emit immediately so a caller can render as it arrives.
                    # `parts` stays the authoritative accumulator: the callback
                    # is for display only and its return value is ignored, so a
                    # failing callback can never corrupt the recorded reply.
                    if on_content is not None:
                        try:
                            on_content(piece)
                            # Only the FIRST paint counts as first-render time;
                            # overwriting it on every fragment would silently
                            # record the last one instead.
                            if first_render is None:
                                first_render = time.perf_counter()
                        except Exception:  # noqa: BLE001 - display must not abort the request
                            pass
    ended = time.perf_counter()
    usage = next((x['usage'] for x in reversed(chunks) if x.get('usage')), None)
    timings = next((x['timings'] for x in reversed(chunks) if x.get('timings')), None)
    reasons = [y.get('finish_reason') for x in chunks
               for y in x.get('choices', []) if y.get('finish_reason')]
    return {'request': body,
            'messages': [{'role': m['role'], 'content': m['content']} for m in messages],
            'reply': ''.join(parts),
            'first_content_fragment_ms': round((first_content - began) * 1000, 2) if first_content else None,
            'first_sse_event_ms': round((first_event - began) * 1000, 2) if first_event else None,
            'first_render_ms': round((first_render - began) * 1000, 2) if first_render else None,
            'wall_seconds': round(ended - began, 3),
            'usage': usage, 'timings': timings,
            'finish_reason': reasons[-1] if reasons else None,
            'n_chunks': len(chunks), 'chunks': chunks}


def derive(rec):
    """Copy or divide only reported numbers. No metric is invented here."""
    t = rec.get('timings') or {}
    out = {'predicted_n': t.get('predicted_n'), 'prompt_n': t.get('prompt_n'),
           'prompt_ms': t.get('prompt_ms'), 'predicted_ms': t.get('predicted_ms'),
           'predicted_per_second_reported': t.get('predicted_per_second'),
           'prompt_per_second_reported': t.get('prompt_per_second'),
           'first_content_fragment_ms': rec.get('first_content_fragment_ms'),
           'wall_seconds': rec.get('wall_seconds'),
           'finish_reason': rec.get('finish_reason'),
           'stop_type': stop_type(rec)}
    if t.get('predicted_n') and t.get('predicted_ms'):
        out['decode_tokens_per_second'] = round(t['predicted_n'] / (t['predicted_ms'] / 1000.0), 2)
        out['decode_tokens_per_second_basis'] = 'predicted_n / predicted_ms (decode window only)'
    return out


def stop_type(rec):
    """Map the reported stop cause to a plain cause name. `limit` means the
    output cap was reached, which is a truncation risk, not a natural ending."""
    fr = rec.get('finish_reason')
    return {None: 'unknown', 'stop': 'eos', 'length': 'limit'}.get(fr, str(fr))


# ------------------------------------------------------------------ verify

def eval_sampler_for(profile_params, max_tokens):
    """Eval sampler kwargs with the per-call output cap.

    The profile stores a default max_tokens, but every historical eval call used
    its own cap (320 / 200 / 64 / 96 ...). Those caps are part of the recorded
    protocol, so the per-call value wins and the profile default is dropped.
    """
    params = {k: v for k, v in profile_params.items() if k != 'max_tokens'}
    params['max_tokens'] = max_tokens
    return params


def verify():
    """Deployment smoke check: five functional probes plus timing and VRAM
    instrumentation. Not an Agent benchmark and not a quantisation comparison.

    This uses the frozen greedy eval profile from configs/sampling_eval.json,
    which is exactly the profile the already-published measurements used.
    Changing the chat profile cannot alter these numbers.
    """
    eval_sampler, eval_meta = load_sampling(EVAL_SAMPLING)
    integrity = check_model()
    state = load_state() if STATE.exists() else {}
    idle = gpu()
    samples, halt = [], threading.Event()
    began = time.perf_counter()

    def monitor():
        while not halt.is_set():
            if len(samples) < 4000:
                snap = gpu()
                snap['elapsed_s'] = round(time.perf_counter() - began, 2)
                samples.append(snap)
            halt.wait(0.5)

    mon = threading.Thread(target=monitor, daemon=True)
    mon.start()
    records = {'eval_sampling': {'params_sent': eval_sampler, 'profile': eval_meta}}
    warm = None
    try:
        records['health'] = http_json('/health')
        records['models'] = http_json('/v1/models')
        props = http_json('/props') or {}
        props.pop('chat_template', None)
        records['props'] = props

        # Short warm-up: its timings are recorded but never used as the measured
        # latency figures below.
        warm = stream_chat([{'role': 'user', 'content': '只回答两个字：你好'}], 8,
                           sampler=eval_sampler_for(eval_sampler, 8))
        warm['derived'] = derive(warm)
        records['warmup'] = warm
        time.sleep(1.0)

        cases = [
            ('c1_explain',
             [('system', SYSTEM), ('user', '用两句话解释什么是“本地离线模型”。')], 320),
            ('c2_rewrite',
             [('system', SYSTEM),
              ('user', '把下面这句话改写得更礼貌，保持原意，不要添加任何新信息：'
                       '今天下班前把表格发给我。')], 320),
            ('c3_json',
             [('system', SYSTEM),
              ('user', '请只输出一个 JSON 对象，不要解释文字，也不要用代码块标记。'
                       '把姓名“小林”和城市“杭州”分别放进 name 和 city 字段。')], 200),
        ]
        for cid, turns, limit in cases:
            rec = stream_chat([{'role': r, 'content': c} for r, c in turns], limit,
                              sampler=eval_sampler_for(eval_sampler, limit))
            rec['derived'] = derive(rec)
            rec['gpu_at_end'] = gpu()
            records[cid] = rec

        msgs = [{'role': 'system', 'content': SYSTEM},
                {'role': 'user',
                 'content': '请记住：我叫小林，在杭州做产品经理。只回复“记住了”三个字。'}]
        t1 = stream_chat(msgs, 64, sampler=eval_sampler_for(eval_sampler, 64))
        t1['derived'] = derive(t1)
        records['c4_memory_turn1'] = t1
        msgs = msgs + [{'role': 'assistant', 'content': t1['reply']},
                       {'role': 'user', 'content': '我叫什么名字？在哪个城市工作？'}]
        t2 = stream_chat(msgs, 96, sampler=eval_sampler_for(eval_sampler, 96))
        t2['derived'] = derive(t2)
        records['c4_memory_turn2'] = t2
    finally:
        halt.set()
        mon.join(15)
        used = [x['used_MiB'] for x in samples if isinstance(x.get('used_MiB'), int)]
        result = {
            'scope': ('Official Q4_K_M artifact deployment smoke check. Not an Agent '
                      'evaluation and not a formal F16/Q8/Q4 comparison.'),
            'when': now_iso(),
            'config': config(),
            'integrity': integrity,
            'runtime_version_probe': runtime_version(),
            'server_state': state,
            'gpu_idle_before_checks': idle,
            'gpu_processes_at_check_start': gpu_processes(),
            'records': records,
            'gpu_samples': samples,
            'gpu_peak_whole_card_MiB': max(used) if used else None,
            'gpu_min_whole_card_MiB': min(used) if used else None,
            'gpu_sample_count': len(samples),
            'gpu_delta_vs_idle_MiB': (max(used) - idle['used_MiB'])
                                     if used and isinstance(idle.get('used_MiB'), int) else None,
            'gpu_caveat': ('Whole-card counters include every other GPU program on this '
                           'machine, and the before/after delta also moves with unrelated '
                           'process activity. Neither is the exclusive cost of this model.'),
            'context_caveat': ('context_size 4096 is this round\'s configured capacity; these '
                               'short probes are not a long-context validation.'),
        }
        save(LOG_DIR / 'deployment_checks.json', result)
        keys = ('first_content_fragment_ms', 'wall_seconds', 'prompt_n',
                'predicted_n', 'decode_tokens_per_second', 'finish_reason', 'stop_type')
        brief = {k: {'reply': v['reply'][:500], **{kk: v['derived'].get(kk) for kk in keys}}
                 for k, v in records.items() if k.startswith(('c1', 'c2', 'c3', 'c4'))}
        brief['warmup_not_measured'] = {kk: warm['derived'].get(kk) for kk in keys} if warm else None
        print(json.dumps(brief, ensure_ascii=False, indent=2))
        print(json.dumps({'idle_whole_card_MiB': idle.get('used_MiB'),
                          'peak_whole_card_MiB': result['gpu_peak_whole_card_MiB'],
                          'delta_MiB': result['gpu_delta_vs_idle_MiB'],
                          'samples': result['gpu_sample_count']}, ensure_ascii=False))


def runtime_version():
    exe = ROOT / config()['runtime']
    r = run([str(exe), '--version'], timeout=120)
    text = (r.stdout or '') + (r.stderr or '')
    keep = r'version:|built with|compute capability|GGML_CUDA|CUDA devices'
    return [ln.strip() for ln in text.splitlines() if re.search(keep, ln)]


# -------------------------------------------------------------------- chat

CHAT_SAMPLING = ROOT / 'configs' / 'sampling_chat.json'
EVAL_SAMPLING = ROOT / 'configs' / 'sampling_eval.json'


def load_sampling(path):
    """Read a sampling profile and return (params, provenance dict).

    Chat and evaluation use separate files on purpose: the evaluation numbers
    already published were produced under the greedy profile, so editing the
    chat profile must not be able to move them.
    """
    data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    return data['params'], {k: data.get(k) for k in
                            ('profile', 'description', 'provenance', 'frozen',
                             'frozen_reason', 'notes')}


def chat(session_log=None):
    """Interactive Chinese chat.

    Two things this function deliberately gets right:
      1. It uses the *chat* sampling profile from configs/sampling_chat.json,
         not the frozen greedy eval profile. Earlier versions silently inherited
         greedy decoding, which contradicted the report's own advice.
      2. It renders each content fragment as it arrives through on_content, so
         the text appears progressively instead of in one dump at the end. The
         full reply is still accumulated inside stream_chat, so nothing is
         printed twice and the recorded reply stays authoritative.
    """
    http_json('/health', timeout=5)
    sampler, sampling_meta = load_sampling(CHAT_SAMPLING)
    messages = [{'role': 'system', 'content': SYSTEM}]
    turns = []
    print('已连接本地 Qwen2.5-1.5B-Instruct (Q4_K_M)。/exit 退出，/clear 清空上下文。')
    print(f"采样：chat 配置 {json.dumps(sampler, ensure_ascii=False)}"
          f"（来源 {sampling_meta.get('provenance', {}).get('basis', 'n/a')}，"
          f"is_tuned={sampling_meta.get('provenance', {}).get('is_tuned')}）")

    def render(piece):
        sys.stdout.write(piece)
        sys.stdout.flush()

    while True:
        try:
            text = input('\n你：').strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if text in ('/exit', '/quit'):
            break
        if text == '/clear':
            messages = messages[:1]
            turns.append({'event': 'clear', 'at': now_iso()})
            print('上下文已清空。')
            continue
        if not text:
            continue
        messages.append({'role': 'user', 'content': text})
        print('Qwen：', end='', flush=True)
        try:
            rec = stream_chat(messages, sampler=sampler, timeout=900, on_content=render)
        except Exception:
            messages.pop()
            raise
        print()  # the reply was already painted by render(); just end the line
        d = derive(rec)
        if d.get('decode_tokens_per_second'):
            print(f"  [{d['predicted_n']} tok · 接口首片 {d['first_content_fragment_ms']} ms · "
                  f"首帧显示 {rec.get('first_render_ms')} ms · "
                  f"{d['decode_tokens_per_second']} tok/s(解码窗口) · 停止={d['stop_type']}]")
        rec.pop('chunks', None)  # keep the session log compact; raw SSE stays out of chat logs
        turns.append({'event': 'turn', 'user': text, 'reply': rec['reply'],
                      'derived': d, 'request': rec['request'],
                      'first_render_ms': rec.get('first_render_ms'),
                      'at': now_iso()})
        messages.append({'role': 'assistant', 'content': rec['reply']})

    if session_log:
        save(session_log, {'when': now_iso(), 'profile': sampling_meta,
                           'sampler_sent': sampler,
                           'system_prompt': SYSTEM, 'events': turns})
        print(f'本次会话记录已写入 {session_log}')


# -------------------------------------------------------------------- main

COMMANDS = ['start', 'stop', 'status', 'verify-model', 'verify', 'chat', 'gpu']


def main():
    p = argparse.ArgumentParser(description='Local Qwen2.5-1.5B-Instruct Q4_K_M control.')
    p.add_argument('command', choices=COMMANDS)
    p.add_argument('--handshake-file', type=str, default=None, help='File to write atomic launch event')
    p.add_argument('--managed', action='store_true', help='Clean up child if parent aborts or pipe closes')
    p.add_argument('--no-switch-port', action='store_true', help='Do not auto-switch port if occupied')
    p.add_argument('--port', type=int, default=None, help='Override port')
    p.add_argument('--parent-pid', type=int, default=None, help='Parent process PID to monitor')
    p.add_argument('--expected-pid', type=int, default=None, help='Expected PID for stop verification')
    p.add_argument('--token', type=str, default=None, help='Launch token for handshake verification')
    p.add_argument('--config', type=str, default=None, help='Config file path override')
    p.add_argument('--profile', type=str, default=None, help='Explicit model profile id')
    p.add_argument('--parent-auth-event', type=str, default=None, help='Win32 named event to wait for before spawning child')
    p.add_argument('--test-delay-spawn', type=float, default=0.0, help='Testing hook: delay before writing handshake')
    p.add_argument('--test-fail-handshake', action='store_true', help='Testing hook: simulate handshake write failure')
    p.add_argument('--test-corrupt-handshake', action='store_true', help='Testing hook: simulate corrupted handshake file')
    p.add_argument('--test-mismatch-token', action='store_true', help='Testing hook: simulate mismatched token in handshake')
    p.add_argument('--test-identity-mode', type=str, default=None, choices=['none', 'missing_fields', 'mismatched'], help='Testing hook: simulate faulty identity')
    p.add_argument('--test-exit-before-handshake', action='store_true', help='Testing hook: terminate child before handshake')
    p.add_argument('--test-child-spawned-event', type=str, default=None, help='Testing hook: Win32 event signaled when child spawned')
    p.add_argument('--test-pause-before-handshake-event', type=str, default=None, help='Testing hook: Win32 event to pause before publishing handshake')
    p.add_argument('--test-diag-file', type=str, default=None, help='Testing hook: file to write child PID/identity without handshake')
    a = p.parse_args()
    if a.command == 'verify-model':
        print(json.dumps(check_model(), ensure_ascii=False, indent=2))
    elif a.command == 'gpu':
        print(json.dumps(gpu(), ensure_ascii=False, indent=2))
    elif a.command == 'start':
        start(handshake_file=a.handshake_file, managed=a.managed,
              switch_port=not a.no_switch_port, port_override=a.port,
              parent_pid=a.parent_pid, token=a.token, config_override=a.config,
              profile_override=a.profile,
              parent_auth_event=a.parent_auth_event,
              test_delay_spawn=a.test_delay_spawn,
              test_fail_handshake=a.test_fail_handshake,
              test_corrupt_handshake=a.test_corrupt_handshake,
              test_mismatch_token=a.test_mismatch_token,
              test_identity_mode=a.test_identity_mode,
              test_exit_before_handshake=a.test_exit_before_handshake,
              test_child_spawned_event=a.test_child_spawned_event,
              test_pause_before_handshake_event=a.test_pause_before_handshake_event,
              test_diag_file=a.test_diag_file)
    elif a.command == 'stop':
        stop(expected_pid=a.expected_pid)
    else:
        globals()[a.command]()


if __name__ == '__main__':
    for attr in ('stdout', 'stderr'):
        stream = getattr(sys, attr)
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f'ERROR: {exc}', file=sys.stderr)
        sys.exit(1)
