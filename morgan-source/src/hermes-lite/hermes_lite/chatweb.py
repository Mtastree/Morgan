"""Round 16: the chat service (one generation at a time) + its HTTP page.

This is a NEW surface, separate from `web.py`. It deliberately does not import
`core`, `runtime`, `round*`, or any Skill module: the chat product shares the
model endpoint and the loopback HTTP style, nothing else. That separation is what
makes "no tools / no Skill / no Agent guidance" a structural fact rather than a
promise.

## Generation lifecycle

    POST /api/chat/send
        -> user message row (idempotent on client_msg_id)
        -> memory rule pass (0 requests) [-> optional 1 extraction request]
        -> assistant row created as `streaming`, request_id fixed
        -> worker thread streams into that row
        -> row becomes complete | interrupted | error

`stop` marks the in-flight request's stop flag. The worker then finalises the row
as `interrupted` with the partial text kept. Because every write is guarded by
`id + session_id + request_id + status='streaming'`, a late chunk from a stopped
generation can never land in a newer one, and a new session can never inherit it.

## Endpoints

    GET  /                        the chat page
    GET  /static/<file>           local css / js only
    GET  /api/chat/status         model connection, window, counters, store paths
    GET  /api/chat/sessions       session list
    POST /api/chat/sessions       {title?} -> new session
    POST /api/chat/sessions/rename {session_id, title}
    POST /api/chat/sessions/delete {session_id}
    GET  /api/chat/session?id=    one session with its messages
    POST /api/chat/send           {session_id, text, client_msg_id?}
    POST /api/chat/stop           {session_id}
    GET  /api/chat/stream?session_id=&after=N   SSE of the live generation
    GET  /api/chat/memories       memory panel
    POST /api/chat/memories/update {id, content?, enabled?}
    POST /api/chat/memories/delete {id}
    GET  /api/chat/export?session_id=&format=md|json
    GET  /api/chat/requests       the request log (counts + purity flags)
"""
from __future__ import annotations

import concurrent.futures
import json
import mimetypes
import os
import sys
import threading
import time
import traceback
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional

from hermes_lite import (chatcompress, chatengine, chatmem, chatparams,
                         chatsearch, chatsearch_router, chatstore)

# ---------------------------------------------------------------------------
# Web-search admission control (round 39)
# ---------------------------------------------------------------------------
# `ThreadPoolExecutor(max_workers=4)` bounds how many searches RUN. It does not
# bound how many WAIT: `submit()` appends to an unbounded work queue, so a user
# who stops and re-asks repeatedly can pile up searches that the OS has not
# actually finished. Those queued searches would each still fire a network
# request later, which is exactly what "I stopped" must not cause.
#
# So admission is explicit, counted, and released on a real event:
#
#   running  <= 4   same as the executor's worker count; more runners than
#                   workers would just wait inside the pool anyway.
#   queued   <= 1   ONE spare slot. The chat side holds a single model slot, so
#                   at most one search is live at a time; a second slot is
#                   enough for the case where the previous search was abandoned
#                   but its HTTP call has not returned yet. Anything beyond
#                   that is not "an extra search", it is a leak.
#   total    <= 5   = running + queued, the number the requirement asks for.
#
# A ticket is released when, and only when, one of these happens:
#   * the search function actually returned (success, failure or timeout), or
#   * the future was cancelled BEFORE it started, so no request was ever made.
# It is NOT released when the user clicks stop while the request is in flight:
# that search is still on the wire and still occupies capacity.
SEARCH_MAX_RUNNING = 4
SEARCH_MAX_QUEUED = 1
SEARCH_MAX_TOTAL = SEARCH_MAX_RUNNING + SEARCH_MAX_QUEUED


class SearchGate:
    """Counted admission control for background web searches.

    Every state change is taken under one lock and every release is bound to a
    unique ticket, so a retiring task can never release a newer task's capacity
    and a ticket can never be released twice.
    """

    def __init__(self, max_running: int = SEARCH_MAX_RUNNING,
                 max_queued: int = SEARCH_MAX_QUEUED):
        self.max_running = max_running
        self.max_queued = max_queued
        self.max_total = max_running + max_queued
        self._lock = threading.Lock()
        self._next_ticket = 1
        self._live: dict[int, dict] = {}
        self.counters = {
            'accepted': 0,
            'rejected': 0,
            'released_completed': 0,
            'released_cancelled_before_start': 0,
            'double_release_attempts': 0,
            'peak_running': 0,
            'peak_queued': 0,
        }

    # -- queries ----------------------------------------------------------
    def _counts_locked(self) -> tuple[int, int]:
        running = sum(1 for t in self._live.values() if t['started'])
        queued = len(self._live) - running
        return running, queued

    def status(self) -> dict:
        with self._lock:
            running, queued = self._counts_locked()
            return {
                'max_running': self.max_running,
                'max_queued': self.max_queued,
                'max_total': self.max_total,
                'running': running,
                'queued': queued,
                'in_flight': len(self._live),
                'tickets': {str(k): dict(v) for k, v in self._live.items()},
                **self.counters,
            }

    # -- admission --------------------------------------------------------
    def admit(self, label: str = '') -> Optional[dict]:
        """Take a ticket, or return None fast when the total cap is reached.

        The single test is on the TOTAL, because that is the quantity the
        requirement bounds ("running + queued"). A separate queue-only test
        would be wrong: tickets are counted before their thread starts, so at
        admission time every ticket still looks queued and such a test would
        reject the very first search. The queue is bounded as a consequence --
        `running` can never exceed `max_running` worker threads, so
        `queued <= max_total - max_running == max_queued`.
        """
        with self._lock:
            running, queued = self._counts_locked()
            if len(self._live) >= self.max_total:
                self.counters['rejected'] += 1
                return None
            ticket = self._next_ticket
            self._next_ticket += 1
            self._live[ticket] = {'ticket': ticket, 'label': label,
                                  'started': False, 'since': time.time()}
            self.counters['accepted'] += 1
            self.counters['peak_queued'] = max(self.counters['peak_queued'], queued + 1)
            return {'ticket': ticket}

    def mark_running(self, ticket: int) -> None:
        with self._lock:
            t = self._live.get(ticket)
            if t is None:
                return
            t['started'] = True
            t['started_at'] = time.time()
            running, _ = self._counts_locked()
            self.counters['peak_running'] = max(self.counters['peak_running'], running)

    def release(self, ticket: int, reason: str) -> bool:
        """Give the ticket back. Returns True if it was still held by us."""
        with self._lock:
            if ticket not in self._live:
                self.counters['double_release_attempts'] += 1
                return False
            del self._live[ticket]
            key = ('released_completed' if reason == 'completed'
                   else 'released_cancelled_before_start')
            self.counters[key] = self.counters.get(key, 0) + 1
            return True

    def reset(self) -> None:
        """Drop all tickets and counters.

        Only meaningful when nothing is genuinely in flight (a fresh service or
        a test harness). It does NOT cancel anything; it clears accounting.
        """
        with self._lock:
            self._live.clear()
            for k in self.counters:
                self.counters[k] = 0


_SEARCH_GATE = SearchGate()
_SEARCH_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=SEARCH_MAX_RUNNING,
                                                         thread_name_prefix='websearch-bg')
_SEARCH_EXECUTOR_LOCK = threading.Lock()
_SEARCH_EXECUTOR_CLOSED = {'value': False}


def search_pool_status() -> dict:
    """Snapshot used by tests and by the shutdown path."""
    with _SEARCH_EXECUTOR_LOCK:
        closed = _SEARCH_EXECUTOR_CLOSED['value']
    return {'executor_shutdown': closed, 'gate': _SEARCH_GATE.status()}


def shutdown_search_executor(wait: bool = False, timeout_s: float = 2.0,
                             drain: bool = False) -> dict:
    """Tear the search pool down. Safe to call more than once.

    ## What `wait=False` does and does NOT mean (round 40)

    `ThreadPoolExecutor.shutdown(wait=False)` only promises that the CALLER does
    not block. It does not let the interpreter exit while workers are busy:
    `concurrent.futures.thread` registers `_python_exit()` via
    `threading._register_atexit`, which joins every worker thread. So with a
    search still on the wire, the process stays alive until that HTTP call
    returns. Round 39's probe released the blocked task before exiting, which is
    why it could not show this.

    `drain=True` (used by the desktop close path) makes the bounded wait
    explicit: after signalling shutdown we join the workers up to `timeout_s` and
    report whether they actually finished. Callers can then state honestly
    "queued work cancelled, N running searches drained within X s" instead of
    implying instant exit.
    """
    started = time.monotonic()
    with _SEARCH_EXECUTOR_LOCK:
        already = _SEARCH_EXECUTOR_CLOSED['value']
        _SEARCH_EXECUTOR_CLOSED['value'] = True
    before = _SEARCH_GATE.status()
    if not already:
        try:
            _SEARCH_EXECUTOR.shutdown(wait=wait, cancel_futures=True)
        except TypeError:  # pragma: no cover - Python < 3.9
            _SEARCH_EXECUTOR.shutdown(wait=wait)
        except Exception:  # noqa: BLE001
            pass

    drained = None
    waited_s = 0.0
    if drain:
        # Join only the executor's own worker threads; bounded by timeout_s.
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        threads = list(getattr(_SEARCH_EXECUTOR, '_threads', ()) or ())
        for t in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                t.join(timeout=remaining)
            except Exception:  # noqa: BLE001
                pass
        waited_s = round(time.monotonic() - started, 3)
        drained = all(not t.is_alive() for t in threads)

    after = _SEARCH_GATE.status()
    return {'already_closed': already,
            'cancelled_queued': max(0, before['queued'] - after['queued']),
            'still_running': after['running'],
            'drained': drained,
            'waited_s': waited_s,
            'gate': after}

LITE = Path(__file__).resolve().parents[1]
PROJECT = LITE.parents[1]
CHAT_STATIC = Path(__file__).resolve().parent / 'chat_static'


@dataclass
class RuntimeInstance:
    """Clear, authoritative descriptor of the verified active model runtime instance."""
    base_url: str
    model_alias: str
    actual_window: Optional[int]
    owned: bool
    pid: Optional[int] = None
    identity: Optional[str] = None
    instance_seq: int = 1
    status: str = 'ready'  # ready, stopping, loading, restoring, unavailable
    error: Optional[str] = None
    actual_window_source: str = 'service-probe'
    configured_window: Optional[int] = None
    # Round 43: WHICH model configuration produced this instance. A reply must be
    # attributable to a profile, not to "whatever model happens to be loaded when
    # the row is read later", so these travel with the instance.
    profile_id: Optional[str] = None
    profile_display_name: Optional[str] = None
    target_profile_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            'base_url': self.base_url,
            'model_alias': self.model_alias,
            'actual_window': self.actual_window,
            'actual_window_source': self.actual_window_source if self.actual_window else 'unknown',
            'configured_window': self.configured_window,
            'owned': self.owned,
            'pid': self.pid,
            'identity': self.identity,
            'instance_seq': self.instance_seq,
            'status': self.status,
            'error': self.error,
            'profile_id': self.profile_id,
            'profile_display_name': self.profile_display_name,
            'target_profile_id': self.target_profile_id,
        }

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def __getitem__(self, key: str) -> Any:
        if hasattr(self, key):
            return getattr(self, key)
        raise KeyError(key)


class ChatService:
    """Owns the store and the single in-flight generation.

    ## Model occupancy (round 19)

    Chat generation and background extraction share ONE local model server that
    generates a single stream at a time. Sending both at once does not make the
    machine faster: it makes the server interleave or queue them, and it makes
    "stop" a lie, because a request that is already at the server keeps running.

    So there is one explicit slot, `_slot`, with four states:

        idle        nobody is using the model
        chat        a chat generation is in flight
        extracting  a background extraction is in flight
        cancelling  a stop was requested and the old call has not returned yet

    Chat holds the slot for the whole of its generation. Extraction holds it for
    the whole of its single request. A new chat asks the extraction to cancel and
    then WAITS for the slot, bounded by `EXTRACTION_DRAIN_TIMEOUT_S`; it does not
    start a second request while the extraction is still on the wire. The page is
    told that it is waiting, so it never says "generating" when it is not.

    What this does NOT claim: that the in-flight HTTP request is aborted
    instantly. `urllib` has no way to interrupt a request that is already being
    served, and killing the model server is not acceptable. So a chat that
    arrives during an extraction waits for that extraction to return or time out.
    The bound is real and is reported, not hidden.
    """

    #: How long a new chat will wait for a background extraction to release the
    #: model before it is refused with an explanation.
    EXTRACTION_DRAIN_TIMEOUT_S = 25.0
    #: Hard ceiling on any single extraction request.
    EXTRACT_TIMEOUT_S = 20.0

    def __init__(self, store: Optional[chatstore.ChatStore] = None,
                 model_url: Optional[str] = None,
                 model_port: Optional[int] = None,
                 model_config_path: Optional[Path] = None):
        self.store = store or chatstore.ChatStore()
        self._lock = threading.RLock()
        self._active: dict[str, Any] = {}
        self._bg_task: Optional[dict] = None
        self._summary_task: Optional[dict] = None
        self._sse_lock = threading.Lock()
        self._shutting_down = False
        self._paused_for_reload = False
        self._desktop_controller: Optional[Any] = None
        # The single model slot. `holder` names the request_id that owns it.
        self._slot: dict[str, Any] = {'state': 'idle', 'holder': None,
                                      'kind': None, 'since': None}
        self.model_url = model_url
        self.model_port = model_port
        self.model_config_path = model_config_path
        self._runtime_instance = self._resolve_initial_runtime_instance()

    def _resolve_initial_runtime_instance(self) -> RuntimeInstance:
        # 1. Determine base_url with strict priority:
        #    model_url > model_port > model_config_path > effective deployment
        if self.model_url:
            base = self.model_url.rstrip('/')
        elif self.model_port:
            base = f"http://127.0.0.1:{self.model_port}/v1"
        elif self.model_config_path and Path(self.model_config_path).is_file():
            try:
                c = json.loads(Path(self.model_config_path).read_text(encoding='utf-8-sig'))
                port = int(c.get('port', 18085))
                host = c.get('host', '127.0.0.1')
                base = f"http://{host}:{port}/v1"
            except Exception:
                base = chatengine.base_url()
        else:
            base = chatengine.base_url()

        # 2. Alias / declared window / profile identity.
        #
        # Round 43: no Qwen literal. With no explicit config the answer comes
        # from the EFFECTIVE deployment (deployment file + default profile), and
        # when nothing can be resolved the alias stays empty so the page reports
        # "未加载" instead of naming a model that may not be running.
        alias = ''
        declared_ctx = None
        profile_id = None
        if self.model_config_path and Path(self.model_config_path).is_file():
            try:
                c = json.loads(Path(self.model_config_path).read_text(encoding='utf-8-sig'))
                alias = c.get('alias') or ''
                ctx = c.get('context_size')
                declared_ctx = int(ctx) if isinstance(ctx, int) else None
                profile_id = c.get('profile_id')
            except Exception:
                pass
        else:
            try:
                dep = chatengine.effective_deployment()
                alias = dep.get('alias') or ''
                ctx = dep.get('context_size')
                declared_ctx = int(ctx) if isinstance(ctx, int) else None
                profile_id = dep.get('profile_id')
            except Exception:
                pass

        # 3. Probe /props on target base_url to obtain actual context window
        actual_win = None
        status = 'unavailable'
        try:
            root = base[:-3] if base.endswith('/v1') else base
            props = chatengine._get_json(f'{root}/props', 1.5)
            n = (props.get('default_generation_settings') or {}).get('n_ctx')
            if type(n) is int and n > 0:
                actual_win = n
                status = 'ready'
        except Exception:
            status = 'unavailable'

        return RuntimeInstance(
            base_url=base,
            model_alias=alias,
            actual_window=actual_win,
            owned=False,
            pid=None,
            identity=None,
            instance_seq=1,
            status=status,
            actual_window_source='service-probe' if actual_win else 'unknown',
            configured_window=declared_ctx,
            profile_id=profile_id,
        )

    def set_desktop_controller(self, ctrl: Any) -> None:
        with self._lock:
            self._desktop_controller = ctrl

    def get_runtime_instance(self) -> RuntimeInstance:
        with self._lock:
            if self._desktop_controller and hasattr(self._desktop_controller, 'get_runtime_instance'):
                return self._desktop_controller.get_runtime_instance()
            return self._runtime_instance

    def set_runtime_instance(self, inst: RuntimeInstance) -> None:
        with self._lock:
            self._runtime_instance = inst

    def pause_for_reload(self) -> None:
        with self._lock:
            self._paused_for_reload = True

    def resume_after_reload(self) -> None:
        with self._lock:
            self._paused_for_reload = False

    def busy_summary(self) -> Optional[str]:
        with self._lock:
            if self._active and 'stop' in self._active:
                return "聊天生成中"
            if self._bg_task:
                return "后台记忆提取中"
            if self._summary_task and not self._summary_task.get('done', threading.Event()).is_set():
                return "对话摘要压缩中"
            slot_state = self._slot.get('state')
            if slot_state == 'chat':
                return "聊天生成中"
            if slot_state == 'extracting':
                return "后台记忆提取中"
            if slot_state == 'summarizing':
                return "对话摘要压缩中"
        return None

    def invalidate_model_caches(self) -> None:
        """Clear cached model parameters or tokenizer caches if any."""
        pass

    def get_model_status(self) -> dict:
        if self._desktop_controller and hasattr(self._desktop_controller, 'get_model_status'):
            return self._desktop_controller.get_model_status()
        inst = self.get_runtime_instance()
        busy = self.busy_summary()
        return {
            'ok': True,
            'alias': inst.model_alias,
            'owned': inst.owned,
            'pid': inst.pid,
            'status': inst.status,
            'actual_window': inst.actual_window,
            'target_window': inst.actual_window,
            # Round 41: presets come from the shared module, not a local copy.
            'available_windows': list(chatparams.CONTEXT_WINDOW_PRESETS),
            'window_limits': {'min': chatparams.MIN_CONTEXT_WINDOW,
                              'max': chatparams.ABSOLUTE_MAX_CONTEXT_WINDOW,
                              'step': chatparams.CONTEXT_WINDOW_STEP},
            'reloading': getattr(self, '_paused_for_reload', False),
            'busy': bool(busy),
            'busy_reason': busy,
            'error': inst.error,
            'instance_seq': inst.instance_seq,
            'base_url': inst.base_url
        }

    def reload_model(self, target_window: Any) -> dict:
        # Round 41: any aligned integer in band, not just 2048/4096.
        try:
            target_window = chatparams.validate_context_window(target_window)
        except chatparams.ParamError as exc:
            return exc.to_dict()
        busy = self.busy_summary()
        if busy:
            return {'ok': False, 'error': 'busy', 'message': f'当前正在执行（{busy}），无法重新加载模型。'}
        if self._desktop_controller and hasattr(self._desktop_controller, 'reload_model'):
            return self._desktop_controller.reload_model(target_window)
        return {
            'ok': False,
            'error': 'external_service_read_only',
            'message': ('当前使用外部模型服务，本应用不会重启它。'
                        '如需修改上下文长度，请在该服务的启动参数中设置 '
                        f'n_ctx（当前实际窗口 {self.get_runtime_instance().actual_window}）。')
        }

    # -- Round 43: model profile management -------------------------------- #
    def switch_model_profile(self, profile_id: str) -> dict:
        """Load a different profile. Delegates to the ONE desktop lifecycle.

        Without a desktop controller (a bare `python -m hermes_lite.chatweb`
        page) there is no managed service to restart, so the request is refused
        with an explanation instead of pretending to have switched.
        """
        if not self._desktop_controller or not hasattr(self._desktop_controller,
                                                       'switch_profile'):
            return {'ok': False, 'error': 'no_managed_controller',
                    'message': ('当前页面没有托管模型服务，无法切换模型配置。'
                                '请从桌面应用（desktop.cmd）中切换。')}
        return self._desktop_controller.switch_profile(profile_id)

    def _model_session_defaults(self) -> dict:
        """The reply/budget defaults a NEW session inherits from the running model.

        Round 43: the model's profile can declare its own default reply cap. A new
        session starts from THAT, not from the global 512, so "switch to a model
        with a small window" produces sessions that fit it. Existing sessions are
        untouched: their stored settings are the user's.
        """
        inst = self.get_runtime_instance()
        out: dict[str, Any] = {}
        ctl = self._desktop_controller
        default_max = None
        if ctl is not None and hasattr(ctl, 'get_model_status'):
            try:
                st = ctl.get_model_status()
                default_max = st.get('profile_default_max_output')
            except Exception:  # noqa: BLE001
                default_max = None
        if isinstance(default_max, int) and default_max > 0:
            out['max_tokens'] = default_max
            # Keep the auto input budget consistent with the new reserve.
            window = self._confirmed_window(inst)
            if window:
                out['custom_budget'] = max(chatparams.MIN_CUSTOM_INPUT_BUDGET,
                                           window - default_max)
        return out

    def create_session(self, title: Optional[str] = None,
                       settings: Optional[dict] = None) -> dict:
        """Create a session seeded with the current model's defaults.

        Kept as a service method (not a raw store call) so the seed can never be
        forgotten by a caller and so tests can drive the real path.
        """
        return self.store.create_session(
            title, settings, model_defaults=self._model_session_defaults())

    def model_profiles(self) -> dict:
        if self._desktop_controller and hasattr(self._desktop_controller, 'list_profiles'):
            out = self._desktop_controller.list_profiles()
        else:
            # A bare page still shows the REGISTRY truthfully; it just cannot
            # load anything. Nothing is invented to fill the gap.
            try:
                from hermes_lite import modelprofiles
                reg = modelprofiles.ensure_initialized()
                out = {
                    'ok': True,
                    'profiles': [modelprofiles.profile_public_view(
                        p, default_id=reg.get('default_profile_id'),
                        last_success_id=reg.get('last_success_profile_id'))
                        for p in reg['profiles']],
                    'default_profile_id': reg.get('default_profile_id'),
                    'last_success_profile_id': reg.get('last_success_profile_id'),
                    'current_profile_id': None,
                    'registry': str(modelprofiles.registry_path()),
                    'no_managed_controller': True,
                }
            except Exception as exc:  # noqa: BLE001
                out = {'ok': False, 'error': 'registry_error',
                       'message': f'{type(exc).__name__}: {exc}', 'profiles': []}
        inst = self.get_runtime_instance()
        out.setdefault('current_profile_id', inst.profile_id)
        return out

    def _ctl_profile_op(self, op: str, *args, **kwargs) -> dict:
        if not self._desktop_controller:
            return {'ok': False, 'error': 'no_managed_controller',
                    'message': '当前页面没有托管模型服务，无法管理模型配置。'}
        fn = getattr(self._desktop_controller, op, None)
        if fn is None:
            return {'ok': False, 'error': 'unsupported', 'message': f'不支持的操作：{op}'}
        return fn(*args, **kwargs)

    # How long the PAGE waits for a search before the attempt is abandoned.
    # This is a foreground budget only: it does not bound how long the network
    # task lives, and it must not be described as one. Round 39 keeps 10.0s and
    # makes it a class attribute so a test can shrink it rather than sleep 10s.
    SEARCH_FOREGROUND_BUDGET_S = 10.0

    # -- status -----------------------------------------------------------
    def status(self) -> dict:
        inst = self.get_runtime_instance()
        base = inst.base_url
        out = {'base_url': base, 'reachable': False, 'server_model': None,
               'window': None, 'window_declared': inst.actual_window,
               'error': None, 'alias': inst.model_alias,
               'max_output_tokens': chatengine.MAX_OUTPUT_TOKENS,
               'instance_seq': inst.instance_seq,
               'owned': inst.owned,
               'model_pid': inst.pid,
               'model_status': inst.status}
        root = base[:-3] if base.endswith('/v1') else base
        try:
            props = chatengine._get_json(f'{root}/props', 4.0)
            settings = props.get('default_generation_settings') or {}
            out['window'] = settings.get('n_ctx')
            out['reachable'] = True
        except Exception as exc:  # noqa: BLE001
            out['error'] = f'{type(exc).__name__}: {exc}'
            return out
        try:
            models = chatengine._get_json(f'{base}/models', 4.0)
            data = models.get('data') or []
            if data:
                out['server_model'] = data[0].get('id')
        except Exception:  # noqa: BLE001
            pass
        out['window_matches'] = (out['window'] == out['window_declared'])
        out['store'] = self.store.stats()
        out['counters'] = self.store.counters()
        out['request_counts'] = self.store.request_counts()
        out['active'] = self.active_summary()
        out['model_slot'] = self.slot_summary()
        out['search_pool'] = search_pool_status()
        out['recovered'] = self.store.recovered
        out['pid'] = os.getpid()
        out['store_path'] = str(self.store.path.resolve())
        out['project_root'] = str(PROJECT.resolve())
        return out

    def validate_settings_write(self, session_id: str, patch: dict) -> dict:
        """Strictly validate a settings write against the LIVE window.

        Returns {'ok': True, 'settings': {...}} or a ParamError-shaped dict.
        The reply reserve and the custom input budget are checked TOGETHER
        against the actual window, because either alone can look fine while the
        pair cannot fit.
        """
        actual = self._confirmed_window(self.get_runtime_instance())
        try:
            validated = chatstore.validate_settings_patch(patch, window=None)
        except chatparams.ParamError as exc:
            return exc.to_dict()

        # Resolve the effective pair (patch may change only one of them).
        stored = self.store.get_session_settings(session_id) or {}
        eff_reply = validated.get('max_tokens', stored.get('max_tokens'))
        eff_budget = validated.get('custom_budget', stored.get('custom_budget'))
        eff_mode = validated.get('history_mode', stored.get('history_mode'))

        if actual:
            try:
                chatparams.validate_runtime_settings(
                    {'max_tokens': eff_reply, 'custom_budget': eff_budget,
                     'history_mode': eff_mode}, actual)
            except chatparams.ParamError as exc:
                return exc.to_dict()
        return {'ok': True, 'settings': validated,
                'actual_window': actual,
                'effective': {'max_tokens': eff_reply,
                              'custom_budget': eff_budget,
                              'history_mode': eff_mode}}

    def capabilities(self, session_id: Optional[str] = None) -> dict:
        """The single source of truth the page renders (round 41)."""
        inst = self.get_runtime_instance()
        ctl = self._desktop_controller
        status = None
        if ctl is not None and hasattr(ctl, 'get_model_status'):
            try:
                status = ctl.get_model_status()
            except Exception:  # noqa: BLE001
                status = None
        owned = bool(status.get('owned')) if status else bool(inst.owned)
        target = (status.get('target_window') if status else None) or inst.actual_window
        st_status = (status.get('status') if status else None) or inst.status

        # Round 43: the display name comes from the profile that is ACTUALLY
        # running. It is empty when nothing is loaded -- the page then says
        # "未加载" rather than a model name borrowed from a config file.
        display_name = (status or {}).get('profile_display_name') or inst.profile_display_name
        profile_id = (status or {}).get('profile_id') or inst.profile_id
        try:
            declared, declared_source = chatengine.model_declared_context(inst.base_url)
        except Exception:  # noqa: BLE001
            declared, declared_source = None, 'unknown'

        cfg = (self.store.get_session_settings(session_id) if session_id else
               dict(chatstore.DEFAULT_SESSION_SETTINGS))
        snap = chatparams.capability_snapshot(
            alias=inst.model_alias,
            display_name=display_name,
            target_window=target,
            actual_window=self._confirmed_window(inst),
            actual_source=('service-probe' if self._confirmed_window(inst) else 'unknown'),
            declared_window=declared,
            declared_source=declared_source,
            owned=owned,
            status=st_status,
            max_output=cfg.get('max_tokens', chatparams.DEFAULT_MAX_OUTPUT_TOKENS),
            history_mode=cfg.get('history_mode', 'auto'),
            custom_budget=cfg.get('custom_budget'),
        )
        # Round 43 additions: which configuration is running, what it was ASKED
        # to load, and what a cold start would load. These are three different
        # questions and the page shows them separately.
        snap['model'].update({
            'profile_id': profile_id,
            'display_source': ('runtime-verified' if display_name else 'unknown'),
            'declared_default_max_output': (status or {}).get('profile_default_max_output'),
            'target_profile_id': (status or {}).get('target_profile_id'),
            'launch_profile_id': (status or {}).get('launch_profile_id'),
        })
        snap['session'] = {
            'max_output': cfg.get('max_tokens'),
            'history_mode': cfg.get('history_mode'),
            'custom_budget': cfg.get('custom_budget'),
            'source': 'session-override' if session_id else 'model-default',
        }
        return snap

    # -- model slot --------------------------------------------------------
    def slot_summary(self) -> dict:
        """What the page shows: who holds the model, and whether we are waiting."""
        with self._lock:
            s = dict(self._slot)
        waiting = None
        with self._lock:
            if self._active.get('waiting_for_slot'):
                waiting = {'since': self._active['waiting_for_slot'],
                           'reason': '等待后台记忆请求释放模型'}
        return {'state': s['state'], 'holder': s['holder'],
                'kind': s['kind'], 'since': s['since'],
                'chat_waiting': waiting}

    def _slot_acquire(self, kind: str, holder: str,
                      timeout_s: Optional[float] = None) -> dict:
        """Take the single model slot, waiting up to `timeout_s` for a release.

        Returns {'ok': True} or {'ok': False, 'reason': ...}. The caller MUST
        release with `_slot_release(holder)` in a `finally`.
        """
        deadline = None if timeout_s is None else time.time() + timeout_s
        while True:
            with self._lock:
                if self._slot['state'] == 'idle' or self._slot['holder'] == holder:
                    st_val = 'chat' if kind == 'chat' else ('extracting' if kind == 'extract' else 'summarizing')
                    self._slot.update(state=st_val,
                                      holder=holder, kind=kind,
                                      since=time.time())
                    return {'ok': True}
                if deadline is not None and time.time() >= deadline:
                    return {'ok': False, 'reason': 'timeout',
                            'waited_s': round(time.time() - (deadline - timeout_s), 2)}
            time.sleep(0.05)

    def _slot_release(self, holder: str) -> None:
        """Release the slot, but only if THIS request still owns it.

        That is what stops a retiring background task from clearing a slot that a
        new chat has already taken.
        """
        with self._lock:
            if self._slot['holder'] == holder:
                self._slot.update(state='idle', holder=None, kind=None, since=None)

    def _slot_request_cancel(self, holder: Optional[str]) -> None:
        with self._lock:
            if holder and self._slot['holder'] == holder:
                self._slot['state'] = 'cancelling'

    def active_summary(self) -> Optional[dict]:
        with self._lock:
            if not self._active:
                return None
            a = self._active
            return {'session_id': a['session_id'],
                    'message_id': a['message_id'],
                    'request_id': a['request_id'],
                    'started_at': a['started_at'],
                    'stop_requested': a['stop'].is_set()}

    def _busy(self, session_id: str) -> Optional[dict]:
        """Busy only while a generation is genuinely still running."""
        with self._lock:
            a = dict(self._active) if self._active else None
        if a is None:
            return None
        msg_id = a.get('message_id')
        if msg_id:
            row = self.store.get_message(msg_id) or {}
            if row and row.get('status') and row['status'] != 'streaming':
                with self._lock:
                    if self._active.get('request_id') == a['request_id']:
                        self._active = {}
                return None
        return {'ok': False, 'error': 'busy',
                'message': ('这条会话正在生成中，请先停止或等它结束。'
                            if a['session_id'] == session_id else
                            '另一个会话正在生成，请先停止它。'),
                'active': self.active_summary()}

    # -- generation -------------------------------------------------------
    def send(self, session_id: str, text: str,
             client_msg_id: Optional[str] = None,
             web_search: Optional[bool] = None,
             retry_user_msg_id: Optional[str] = None) -> dict:
        if getattr(self, '_shutting_down', False):
            return {'ok': False, 'error': 'shutting_down', 'message': '应用正在退出，不再接收新消息。'}
        if getattr(self, '_paused_for_reload', False):
            return {'ok': False, 'error': 'model_reloading', 'message': '模型正在重新加载中，请稍候。'}
        inst = self.get_runtime_instance()
        if inst.status == 'unavailable':
            return {'ok': False, 'error': 'model_unavailable', 'message': '模型服务当前不可用，请重新加载或检查服务。'}
        if inst.status in ('stopping', 'loading', 'restoring'):
            return {'ok': False, 'error': 'model_reloading', 'message': '模型正在重新加载中，请稍候。'}

        text = (text or '').strip()
        if not text:
            return {'ok': False, 'error': 'empty', 'message': '消息不能为空。'}
        sess = self.store.get_session(session_id)
        if sess is None:
            return {'ok': False, 'error': 'no_session',
                    'message': '会话不存在，请新建一个。'}

        # Atomic reservation under lock to prevent double occupation
        request_id = chatstore.new_id('gen')
        stop = threading.Event()
        with self._lock:
            if getattr(self, '_shutting_down', False):
                return {'ok': False, 'error': 'shutting_down', 'message': '应用正在退出，不再接收新消息。'}
            if getattr(self, '_paused_for_reload', False):
                return {'ok': False, 'error': 'model_reloading', 'message': '模型正在重新加载中，请稍候。'}
            inst = self.get_runtime_instance()
            if inst.status == 'unavailable':
                return {'ok': False, 'error': 'model_unavailable', 'message': '模型服务当前不可用，请重新加载或检查服务。'}
            if inst.status in ('stopping', 'loading', 'restoring'):
                return {'ok': False, 'error': 'model_reloading', 'message': '模型正在重新加载中，请稍候。'}

            if self._summary_task and not self._summary_task.get('done', threading.Event()).is_set():
                return {'ok': False, 'error': 'busy', 'message': '当前会话正在压缩历史对话，请稍候完成或停止压缩后再发送。'}

            busy = self._busy(session_id)
            if busy is not None:
                return busy
            self._active = {'session_id': session_id, 'message_id': None,
                            'request_id': request_id, 'stop': stop,
                            'started_at': chatstore.now(), 'chunks': 0,
                            'text': '', 'instance_seq': inst.instance_seq}

        holder = request_id
        slot_acquired = False
        transferred_to_worker = False
        assistant = None

        # Fetch immutable session settings snapshot for this request
        settings_snapshot = self.store.get_session_settings(session_id)
        if web_search is not None:
            settings_snapshot['web_search'] = bool(web_search)

        # Round 54: no query validation here any more. The raw message is no
        # longer the query, so validating it as one refused ordinary long chat
        # messages ("this message is too long for a search query") that were
        # never going to be searched. The constructed query is validated in the
        # worker, before the search call, where it actually applies.

        # ---- Round 51: ONE persona read per send -------------------------- #
        # The submission instant of THIS request is the snapshot time. The result
        # is threaded into the preflight, the worker, the summary candidate
        # assembly, the search-budget fitting and the final send, so no later
        # stage can observe a different persona than the one the budget was
        # checked against. It is taken BEFORE anything is stored, so a read
        # failure refuses the message instead of leaving a user row behind for an
        # answer that was never going to be generated.
        #
        # A retry is a new send and therefore a new snapshot: this block runs
        # again for it. Nothing is cached across sends, and no global variable
        # holds "the current request's persona".
        persona_snapshot = self._persona_snapshot()
        if not persona_snapshot.get('ok'):
            with self._lock:
                if self._active.get('request_id') == request_id:
                    self._active = {}
            if slot_acquired:
                self._slot_release(holder)
                slot_acquired = False
            return {'ok': False, 'error': 'persona_error',
                    'message': f'人格文件读取失败，本次未发送：{persona_snapshot.get("error")}',
                    'kept_in_composer': True,
                    'persona_error_code': persona_snapshot.get('error_code')}

        try:
            try:
                chatparams.validate_runtime_settings(settings_snapshot, self._confirmed_window(inst))
            except chatparams.ParamError as exc:
                return exc.to_dict()
            # A very long single message is refused BEFORE it becomes a stored turn.
            custom_b = settings_snapshot.get('custom_budget') if settings_snapshot.get('history_mode') == 'custom' else None
            if len(text) >= 1500 or (custom_b and len(text) >= custom_b // 2):
                pre = self._preflight(session_id, text, settings=settings_snapshot, inst=inst,
                                      persona_snapshot=persona_snapshot)
                if not pre.get('ok'):
                    return pre

            # Chat has priority, but it does NOT start a second request while a
            # background extraction is still on the wire. Ask it to cancel, then wait
            # for the slot with a real bound. The page is told we are waiting.
            waited_from = None
            with self._lock:
                if self._slot['state'] == 'extracting':
                    waited_from = time.time()
                    self._active['waiting_for_slot'] = waited_from
            if waited_from is not None:
                bg = None
                with self._lock:
                    bg = self._bg_task
                if bg:
                    bg['cancel'].set()
                    self._slot_request_cancel(bg['task_id'])
                got = self._slot_acquire('chat', holder,
                                         timeout_s=self.EXTRACTION_DRAIN_TIMEOUT_S)
                with self._lock:
                    self._active.pop('waiting_for_slot', None)
                if not got.get('ok'):
                    return {'ok': False, 'error': 'model_busy',
                            'message': ('后台记忆请求还在使用模型，已等待 '
                                        f'{got.get("waited_s")} 秒仍未释放。'
                                        '请稍候再发一次。'),
                            'waited_s': got.get('waited_s')}
            else:
                got = self._slot_acquire('chat', holder)
                if not got.get('ok'):
                    return {'ok': False, 'error': 'model_busy',
                            'message': '模型正被另一个请求占用，请稍候再发。'}
            slot_acquired = True

            retry_resolution = None
            reuse_user_row = False
            if retry_user_msg_id:
                existing_user = self.store.get_message(retry_user_msg_id)
                if not existing_user or existing_user.get('session_id') != session_id or existing_user.get('role') != 'user':
                    return {'ok': False, 'error': 'invalid_retry_target',
                            'message': '重试的目标消息不存在或不属于当前会话。'}
                # A retry may only reuse a row whose stored text IS the text being
                # searched. Otherwise the database would keep the old question
                # while the search and the answer used a new one -- the exact
                # mismatch this requirement forbids. Edited text is a new question.
                stored_text = (existing_user.get('content') or '').strip()
                if stored_text == text:
                    reuse_user_row = True
                    retry_resolution = {
                        'retry_target_reused': True,
                        'retry_reason': 'same_text',
                        'retry_target_user_message_id': existing_user['id'],
                    }
                else:
                    retry_resolution = {
                        'retry_target_reused': False,
                        'retry_reason': 'text_changed',
                        'retry_target_user_message_id': existing_user['id'],
                        'original_text': stored_text,
                    }

            if reuse_user_row:
                user_msg = existing_user
                prior = self.store.find_assistant_for_user_message(
                    session_id, user_msg['id'], user_msg.get('seq'))
                if prior is not None:
                    if prior['status'] == 'streaming':
                        return {
                            'ok': False,
                            'duplicate': True,
                            'status': 'streaming',
                            'error': 'already_streaming',
                            'message': '该消息正在生成中，请勿重复提交。',
                            'user_message_id': user_msg['id'],
                            'assistant_message_id': prior['id'],
                            'assistant': prior
                        }
                    # If prior was error, refused, interrupted, or empty, drop placeholder to start clean attempt
                    if prior['status'] in ('error', 'refused', 'interrupted') or not prior.get('content'):
                        self.store.drop_empty_assistant(prior['id'], prior.get('request_id') or '')
            else:
                user_msg = self.store.add_user_message(session_id, text, client_msg_id)

                # A retried POST for a message we already attempted/answered must not generate twice.
                # Look up prior assistant message specifically attributed to this user message.
                prior = self.store.find_assistant_for_user_message(
                    session_id, user_msg['id'], user_msg.get('seq'))
                if prior is not None:
                    if prior['status'] in ('error', 'refused'):
                        return {
                            'ok': False,
                            'duplicate': True,
                            'status': prior['status'],
                            'error': prior.get('error') or 'previous_attempt_failed',
                            'message': f'该请求此前已执行失败（{prior.get("error") or "未知错误"}），未重复生成。如需重试请重新发送。',
                            'user_message_id': user_msg['id'],
                            'assistant_message_id': prior['id'],
                            'assistant': prior
                        }
                    elif prior['status'] == 'streaming':
                        return {
                            'ok': False,
                            'duplicate': True,
                            'status': 'streaming',
                            'error': 'already_streaming',
                            'message': '该消息正在生成中，请勿重复提交。',
                            'user_message_id': user_msg['id'],
                            'assistant_message_id': prior['id'],
                            'assistant': prior
                        }
                    else:
                        return {
                            'ok': True,
                            'duplicate': True,
                            'status': prior['status'],
                            'message': '这条消息已经处理过了，未重复发送。',
                            'user_message_id': user_msg['id'],
                            'assistant_message_id': prior['id'],
                            'assistant': prior
                        }

            # Auto-title from the first user message. No model call.
            if sess['title'] in ('新对话', '') and user_msg['seq'] == 1:
                self.store.rename_session(session_id, chatengine.guess_title(text))

            assistant = self.store.start_assistant_message(
                session_id, request_id, inst.model_alias,
                reply_to_id=user_msg['id'],
                profile_id=inst.profile_id)
            with self._lock:
                if self._active.get('request_id') == request_id:
                    self._active['message_id'] = assistant['id']
                    # Recorded so EVERY terminal event carries the question it
                    # answers, not only the search-failure path.
                    self._active['user_message_id'] = user_msg['id']
                    self._active['session_id'] = session_id
                    if retry_resolution:
                        self._active['retry_resolution'] = dict(retry_resolution)

            try:
                t = threading.Thread(target=self._worker, daemon=True,
                                     name=f'chat-{request_id}',
                                     args=(session_id, user_msg['id'], text,
                                           assistant['id'], request_id, stop,
                                           settings_snapshot, inst,
                                           persona_snapshot))
                with self._lock:
                    if self._active.get('request_id') == request_id:
                        self._active['thread'] = t
                t.start()
                transferred_to_worker = True
            except Exception as start_err:
                err_msg = f'工作线程启动失败：{type(start_err).__name__}: {start_err}'
                self.store.finish_assistant_message(
                    assistant['id'], session_id, request_id, 'error',
                    error=err_msg)
                self.store.bump('failed_requests', 1)
                self._emit_end(session_id, request_id, 'error', err_msg)
                raise

            out = {'ok': True, 'session_id': session_id,
                   'user_message_id': user_msg['id'],
                   'assistant_message_id': assistant['id'],
                   'request_id': request_id}
            if retry_resolution:
                out.update(retry_resolution)
            return out
        except Exception as e:
            if assistant is not None and not transferred_to_worker:
                try:
                    row = self.store.get_message(assistant['id'])
                    if row and row.get('status') == 'streaming':
                        err_msg = f'发送流程异常：{type(e).__name__}: {e}'
                        self.store.finish_assistant_message(
                            assistant['id'], session_id, request_id, 'error',
                            error=err_msg)
                        self.store.bump('failed_requests', 1)
                        self._emit_end(session_id, request_id, 'error', err_msg)
                except Exception:
                    pass
            raise
        finally:
            if not transferred_to_worker:
                if slot_acquired:
                    self._slot_release(holder)
                with self._lock:
                    if self._active.get('request_id') == request_id:
                        self._active = {}

    def _preflight(self, session_id: str, text: str, settings: Optional[dict] = None,
                   inst: Optional[RuntimeInstance] = None,
                   persona_snapshot: Optional[dict] = None) -> dict:
        """Will this single message fit at all? Asked BEFORE anything is stored.

        Round 51: `persona_snapshot` MUST be the snapshot the caller already took
        for this send. It is a required piece of the measurement, not something
        this method re-reads: reading again here would let the budget be checked
        against persona A while the worker sends persona B. When it is omitted the
        persona is treated as empty rather than silently re-read from disk, so a
        caller that forgets it produces a visible mismatch instead of a silent
        inconsistency.
        """
        active_inst = inst or self.get_runtime_instance()
        if active_inst.status != 'ready':
            return {'ok': False, 'error': 'model_not_ready',
                    'message': f'模型服务当前不可用（状态：{active_inst.status}）'}
        tokenizer = chatengine.Tokenizer(active_inst.base_url,
                                         counter=self._aux_counter_for(active_inst))
        cfg = settings or self.store.get_session_settings(session_id)
        use_mem = cfg.get('use_memory', True)
        style = cfg.get('style', 'standard')
        max_tokens = cfg.get('max_tokens', chatengine.MAX_OUTPUT_TOKENS)
        custom_budget = cfg.get('custom_budget') if cfg.get('history_mode') == 'custom' else None

        # Round 50: the preflight must measure the SAME prompt the worker will
        # send, persona included, or a message could pass here and then be
        # refused after it was already stored. Round 51: "the same" now means the
        # same OBJECT, not a second read of the file.
        psnap = persona_snapshot or {}
        if persona_snapshot is not None and not psnap.get('ok'):
            return {'ok': False, 'error': 'persona_error',
                    'message': f'人格文件读取失败，本次未发送：{psnap.get("error")}',
                    'kept_in_composer': True}
        persona_block = psnap.get('block') or ''

        # Always analyze turn-level temporary requirements
        analysis = chatmem.analyze(text)
        scope = analysis.get('request_scope')

        chosen = chatmem.select(self.store, text, request_scope=scope, style=style,
                                include_durable=use_mem)
        memory_block = chatmem.render(chosen)

        # Check active session summary
        summary_record = self.store.get_session_summary(session_id, status='ready')
        summary_block = None
        if summary_record and summary_record.get('is_active') and summary_record.get('status') == 'ready':
            if cfg.get('history_mode') != 'current_only':
                sess_row = self.store.get_session(session_id) or {}
                boundary_seq = sess_row.get('boundary_seq') or 0
                if boundary_seq == 0 or (summary_record.get('boundary_seq') == boundary_seq and (summary_record.get('start_seq') or 0) > boundary_seq):
                    if (summary_record.get('tokens_saved') or 0) > 0:
                        summary_block = summary_record.get('summary_text')

        plan = chatengine.assemble(tokenizer=tokenizer, memory_block=memory_block,
                                   history=[], user_text=text,
                                   window=active_inst.actual_window or self._window(),
                                   max_output=max_tokens,
                                   style=style,
                                   custom_budget=custom_budget,
                                   summary_block=summary_block,
                                   persona_block=persona_block)
        if plan.get('ok'):
            return {'ok': True, 'prompt_tokens': plan.get('prompt_tokens')}
        return {'ok': False, 'error': plan.get('error', 'input_too_long'),
                'message': plan.get('message'),
                'need_tokens': plan.get('need_tokens'),
                'budget': plan.get('budget'), 'over_by': plan.get('over_by'),
                'kept_in_composer': True}

    def _commit_buffered_reply(self, session_id: str, request_id: str,
                               assistant_id: str, content: str,
                               stop: threading.Event, *,
                               fallback_note: Optional[str] = None,
                               user_message_id: Optional[str] = None) -> bool:
        """Commit buffered text before stop, or reject it in its entirety.

        stop() takes this same lock before setting its flag. Keeping the write
        and publication together prevents a stopped request from publishing a
        delayed fallback or a candidate flush.
        """
        with self._lock:
            if stop.is_set() or self._active.get('request_id') != request_id:
                return False
            cur = self.store._exec(
                "UPDATE messages SET content=? WHERE id=? AND session_id=? "
                "AND request_id=? AND status='streaming'",
                (content, assistant_id, session_id, request_id))
            if cur.rowcount == 0:
                return False
            self._active['text'] = content
            self._publish(session_id, {
                'kind': 'delta', 'request_id': request_id,
                'message_id': assistant_id, 'text': content})
            if fallback_note:
                self._publish(session_id, {
                    'kind': 'search_phase', 'session_id': session_id,
                    'request_id': request_id, 'phase': 'gap_fill_failed',
                    'message': fallback_note, 'user_message_id': user_message_id})
            return True

    def _assemble_prompt_plan(self, *, tokenizer, memory_block: str,
                              history: list[dict], summary_history: list[dict],
                              text: str, window: int, max_output: int,
                              style: str, custom_budget: Optional[int],
                              summary_block: Optional[str],
                              search_block: Optional[str],
                              persona_block: str) -> dict:
        """Unified prompt assembly with budget, history trimming, and summary boundary handling."""
        plan = None
        if summary_block:
            cand_plan = chatengine.assemble(tokenizer=tokenizer,
                                            memory_block=memory_block,
                                            history=summary_history,
                                            user_text=text,
                                            window=window,
                                            max_output=max_output,
                                            style=style,
                                            custom_budget=custom_budget,
                                            summary_block=summary_block,
                                            search_block=search_block,
                                            persona_block=persona_block)
            raw_plan = chatengine.assemble(tokenizer=tokenizer,
                                           memory_block=memory_block,
                                           history=history,
                                           user_text=text,
                                           window=window,
                                           max_output=max_output,
                                           style=style,
                                           custom_budget=custom_budget,
                                           summary_block=None,
                                           search_block=search_block,
                                           persona_block=persona_block)
            if cand_plan.get('ok') and raw_plan.get('ok'):
                diff = raw_plan['prompt_tokens'] - cand_plan['prompt_tokens']
                if diff > 0:
                    plan = cand_plan
                    plan['summary_used'] = True
                    plan['summary_active'] = True
                    plan['tokens_saved'] = diff
                    plan['fallback_reason'] = None
                else:
                    plan = raw_plan
                    plan['summary_used'] = False
                    plan['summary_active'] = True
                    plan['tokens_saved'] = diff
                    plan['fallback_reason'] = f'无净空间节省（净增加 {-diff} tokens），自动回退原始历史'
            elif cand_plan.get('ok') and not raw_plan.get('ok'):
                plan = cand_plan
                plan['summary_used'] = True
                plan['summary_active'] = True
                plan['tokens_saved'] = max(1, cand_plan.get('budget', window) - cand_plan['prompt_tokens'])
                plan['fallback_reason'] = None
            elif not cand_plan.get('ok') and raw_plan.get('ok'):
                plan = raw_plan
                plan['summary_used'] = False
                plan['summary_active'] = True
                plan['tokens_saved'] = 0
                plan['fallback_reason'] = cand_plan.get('message') or '摘要装配超限'
            else:
                plan = cand_plan
                plan['summary_used'] = False
                plan['summary_active'] = True
                plan['tokens_saved'] = 0
                plan['fallback_reason'] = cand_plan.get('message') or '装配超限'

        if plan is None:
            plan = chatengine.assemble(tokenizer=tokenizer,
                                       memory_block=memory_block,
                                       history=history,
                                       user_text=text,
                                       window=window,
                                       max_output=max_output,
                                       style=style,
                                       custom_budget=custom_budget,
                                       summary_block=None,
                                       search_block=search_block,
                                       persona_block=persona_block)
            plan['summary_used'] = False
            plan['summary_active'] = False
            plan['tokens_saved'] = 0
            plan['fallback_reason'] = None
        return plan

    # -- Round 55: the single follow-up search ----------------------------- #
    def _gap_fill_search(self, session_id: str, request_id: str, text: str,
                         history: list[dict], stop: threading.Event) -> dict:
        """Run ONE search for a request whose first answer admitted a gap.

        Bounded, cancellable, and it never loops: the caller runs this at most
        once per user request (see `can_gap_fill`). It reuses the same query
        builder and the same relevance filter as the first pass, and it respects
        the same foreground budget and stop flag.

        Returns {'block', 'results', 'raw', 'found', 'dropped', 'filter_reason',
                 'search_id', 'resp', 'elapsed', 'query_info', 'note'}.
        """
        from hermes_lite import chatsearch_router as router
        out = {'block': '', 'results': [], 'raw': [], 'found': 0, 'dropped': [],
               'filter_reason': '', 'search_id': None, 'resp': None,
               'elapsed': 0.0, 'query_info': {}, 'note': ''}

        recent = self._recent_user_texts(history)
        # The object the model just admitted it lacked is the thing to look up.
        obj = router.named_object(text)
        decision = {'needs_search': True, 'allow_web': True,
                    'reason': 'gap_fill', 'matched': obj.get('token', ''),
                    'confidence': 'medium', 'blocked_by': None, 'followup': False}
        query_info = router.build_query(text, decision, recent_user_texts=recent)
        out['query_info'] = query_info
        query = query_info.get('query') or ''
        if not query_info.get('ok') or not query:
            out['note'] = '补查时没能确定要检索的对象，未发起检索'
            return out

        if stop.is_set():
            out['note'] = '用户停止，补查未发起'
            return out

        ticket_holder = _SEARCH_GATE.admit(label=f'{request_id}-gapfill')
        if ticket_holder is None:
            out['note'] = '搜索任务已满，补查未发起'
            return out
        ticket = ticket_holder['ticket']
        began = time.monotonic()
        try:
            future = _SEARCH_EXECUTOR.submit(self._gap_fill_run, ticket, query, stop)
        except Exception as exc:
            _SEARCH_GATE.release(ticket, 'executor_submit_failed')
            out['note'] = f'提交搜索任务失败：{exc}'
            return out

        deadline = began + self.SEARCH_FOREGROUND_BUDGET_S
        while not future.done():
            if stop.is_set():
                try:
                    if future.cancel():
                        _SEARCH_GATE.release(ticket, 'cancelled_before_start')
                except Exception:                          # noqa: BLE001
                    pass
                out['note'] = '用户停止，补查已取消'
                return out
            if time.monotonic() >= deadline:
                try:
                    if future.cancel():
                        _SEARCH_GATE.release(ticket, 'cancelled_before_start')
                except Exception:                          # noqa: BLE001
                    pass
                out['note'] = '补查超时，未取得可用资料'
                return out
            time.sleep(0.05)
        out['elapsed'] = round(time.monotonic() - began, 3)

        try:
            resp = future.result()
        except Exception as exc:                           # noqa: BLE001
            out['note'] = f'补查失败：{type(exc).__name__}'
            return out
        if not isinstance(resp, chatsearch.SearchResponse):
            out['note'] = '补查没有返回可用结果'
            return out
        out['resp'] = resp
        out['raw'] = [r.to_dict() for r in resp.results]
        out['found'] = len(resp.results)
        if not resp.results:
            out['note'] = '补查没有取得搜索结果'
            return out

        filt = router.filter_results(resp.results, query)
        out['dropped'] = list(filt['dropped'])
        out['filter_reason'] = filt['reason']
        kept = list(filt['kept'])
        for i, r in enumerate(kept, start=1):
            r.index = i
        if not kept:
            out['note'] = f'补查取得 {out["found"]} 条资料，但都与问题无关，已舍弃'
            return out

        block = chatsearch.format_reference_block(kept)
        out['block'] = block or ''
        out['results'] = kept
        out['search_id'] = self.store.save_web_search(
            session_id=session_id, request_id=request_id, query=query,
            status='ready', provider=resp.provider, backend=resp.active_backend,
            client_version=resp.client_version, attempts=resp.attempts,
            fallback_reason=resp.fallback_reason, raw_results=out['raw'],
            results=[r.to_dict() for r in kept],
            trimmed_count=len(out['dropped']), message_id=None,
            elapsed_s=out['elapsed'])
        out['note'] = f'补查取得 {len(kept)} 条可用资料'
        return out

    @staticmethod
    def _gap_fill_run(ticket: int, query: str, stop: Optional[threading.Event] = None) -> Any:
        """Executor body for the follow-up search (releases its ticket on exit)."""
        _SEARCH_GATE.mark_running(ticket)
        try:
            should_stop = stop.is_set if stop is not None else None
            return chatsearch.search(query, max_results=5, timeout=10.0, should_stop=should_stop)
        finally:
            _SEARCH_GATE.release(ticket, 'completed')

    # -- Round 54: search routing ----------------------------------------- #
    def _recent_user_texts(self, history: list[dict], limit: int = 3) -> list[str]:
        """The last few USER messages, newest first.

        Used only to recognise a follow-up/correction and to complete its
        subject. It is never sent to a search backend on its own: the query
        builder takes at most one predecessor and merges it into a short query.
        """
        out = [m.get('content') or '' for m in reversed(history or [])
               if m.get('role') == 'user']
        return [t for t in out if t.strip()][:limit]

    def _route_web_search(self, session_id: str, text: str, cfg: dict,
                          history: list[dict]) -> dict:
        """Decide whether to search, and what to search for. No network, no model.

        Round 54 replaces `is_web_search = bool(cfg.get('web_search'))`: the
        switch only says the network is ALLOWED. The five states are kept apart
        so the page can be honest about which one happened.
        """
        from hermes_lite import chatsearch_router as router

        allow_web = bool(cfg.get('web_search'))
        recent = self._recent_user_texts(history)
        out = {'allow_web': allow_web, 'notice': None,
               'needs_confirmation': False,
               'confirmation_message': '',
               'decision': {}, 'query': {}, 'record': {}}

        decision = router.decide(text, allow_web=allow_web, recent_user_texts=recent)
        out['decision'] = decision

        # The switch is OFF but the user clearly asked for the internet. Say so
        # plainly; never pretend a search happened.
        if decision.get('blocked_by') == 'web_search_disabled':
            out['notice'] = ('当前未联网（联网开关是关闭的），这条消息不会检索网络；'
                             '下面的回答基于本地模型与已有上下文。'
                             '如果需要联网，请打开「允许按需联网」后重发。')
            out['record'] = router.summarise_decision(decision)
            return out

        if not decision.get('needs_search'):
            out['record'] = router.summarise_decision(decision)
            return out

        query_info = router.build_query(text, decision, recent_user_texts=recent)
        out['query'] = query_info
        out['record'] = router.summarise_decision(decision, query_info)

        if not query_info.get('ok'):
            # Search was wanted but there is nothing to search for. Ask instead
            # of submitting a dangling pronoun.
            out['needs_confirmation'] = True
            out['confirmation_message'] = (
                '这条消息看起来需要联网，但我没找到可以检索的具体对象。'
                '请补充要查什么（例如「查一下明天上海的天气」），或直接说明你要问的内容。')
            out['decision'] = dict(decision, needs_search=False)
            out['record'] = router.summarise_decision(out['decision'], query_info)
        return out

    def _persona_snapshot(self) -> dict:
        """Round 50: ONE persona read, with failures made explicit.

        Called at the top of each generation. It never raises: a failure comes
        back as `ok=False` with a message, and the caller refuses the request
        rather than sending a prompt that silently lacks the persona the user
        turned on.
        """
        from hermes_lite import persona as persona_mod
        try:
            return persona_mod.snapshot()
        except Exception as exc:  # noqa: BLE001
            return {'ok': False, 'enabled': None, 'block': '', 'chars': 0,
                    'error': f'{type(exc).__name__}: {exc}',
                    'error_code': 'persona_read_exception'}

    # -- Round 50: persona file ------------------------------------------- #
    def persona_view(self) -> dict:
        """What the settings page shows: body, switch, counts, real path."""
        from hermes_lite import persona as persona_mod
        try:
            return persona_mod.view()
        except Exception as exc:  # noqa: BLE001
            return {'ok': False, 'error': 'persona_view_failed',
                    'message': f'读取人格设置失败：{type(exc).__name__}: {exc}',
                    'body': '', 'chars': 0, 'enabled': False}

    def persona_save(self, body: str, enabled: Optional[bool] = None) -> dict:
        """Save body and (optionally) the switch. Success means bytes on disk.

        The body is written FIRST: if the body write fails, the switch is left
        untouched, so the page can never show "enabled" for content that failed
        to save.

        Round 51: every response states BOTH facts separately --
        `body_saved` (bytes are on disk) and `enabled_saved` (the switch took
        effect) -- so the page never has to infer partial success from prose.
        A body that was written but failed read-back verification reports
        `ok=False, body_saved=True`: "written, not confirmed" is not the same
        thing as "not written", and the user must not be told to retype.
        """
        from hermes_lite import persona as persona_mod
        try:
            saved = persona_mod.write_body(body if isinstance(body, str) else '')
        except persona_mod.PersonaError as exc:
            out = {**exc.to_dict(), 'body_saved': bool(exc.extra.get('wrote')),
                   'enabled_saved': False, 'kept_input': True}
            return out
        out = {'ok': True, 'path': saved['path'], 'chars': saved['chars'],
               'bytes': saved['bytes'], 'saved_at': saved['saved_at'],
               'body_saved': True, 'enabled_saved': False,
               'verified': saved.get('verified', True),
               'message': f'人格正文已保存（{saved["chars"]} 字符）。'}
        if enabled is not None:
            try:
                st = persona_mod.set_enabled(bool(enabled))
            except persona_mod.PersonaError as exc:
                # Body saved, switch not. `ok` stays False (the operation as a
                # whole did not fully succeed) but `body_saved` is True, and the
                # message must not claim the text was lost. `enabled` reports the
                # switch's ACTUAL persisted value (read back from disk), so the
                # page can show the truth instead of guessing.
                try:
                    current = bool(persona_mod.read_state().get('enabled'))
                except Exception:  # noqa: BLE001
                    current = None
                return {**exc.to_dict(), 'ok': False,
                        'body_saved': True, 'enabled_saved': False,
                        'enabled': current, 'enabled_known': current is not None,
                        'chars': saved['chars'], 'kept_input': False,
                        'message': (f'人格正文已保存（{saved["chars"]} 字符），'
                                    f'但启用开关未能保存：{exc.message}')}
            out['enabled'] = st['enabled']
            out['enabled_known'] = True
            out['enabled_saved'] = True
        return out

    def persona_set_enabled(self, enabled: bool) -> dict:
        """Flip the switch only. The body is never touched by this call."""
        from hermes_lite import persona as persona_mod
        try:
            return persona_mod.set_enabled(bool(enabled))
        except persona_mod.PersonaError as exc:
            return exc.to_dict()

    def _bump(self, kind: str) -> None:
        field = {'chat': 'chat_requests', 'extraction': 'extraction_requests',
                 'aux': 'aux_requests'}[kind]
        self.store.bump(field, 1)

    def _aux_counter_for(self, inst: RuntimeInstance) -> Callable[[str, Optional[str]], None]:
        def _counter(kind: str, endpoint: Optional[str] = None) -> None:
            if kind != 'aux':
                self._bump(kind)
                return
            self._bump('aux')
            self.store.log_request(
                kind='aux', url=f'{inst.base_url}/{endpoint or "tokenize"}',
                session_id=None, message_id=None, request_id=None,
                status=None, ok=True, body={},
                profile_id=inst.profile_id, model_alias=inst.model_alias,
                instance_seq=inst.instance_seq)
        return _counter

    def _aux_counter(self, kind: str, endpoint: Optional[str] = None) -> None:
        """Compatibility fallback for default aux counter."""
        inst = self.get_runtime_instance()
        self._aux_counter_for(inst)(kind, endpoint)

    def _worker(self, session_id: str, user_message_id: str, text: str,
                assistant_id: str, request_id: str, stop: threading.Event,
                settings: Optional[dict] = None,
                inst: Optional[RuntimeInstance] = None,
                persona_snapshot: Optional[dict] = None) -> None:
        store = self.store
        status = 'error'
        error: Optional[str] = None
        usage: Optional[dict] = None
        usage1: Optional[dict] = None
        analysis: dict = {}
        # Exception handling must also work before prompt assembly or any call.
        req_row = req_row2 = None
        req_row_closed = req_row2_closed = False
        call1_attempted = call2_attempted = False
        usage2 = None
        candidate_buffer = ''
        is_gap_eligible = False

        try:
            active_inst = inst or self.get_runtime_instance()
            cfg = settings or store.get_session_settings(session_id)
            style = cfg.get('style', 'standard')
            history_mode = cfg.get('history_mode', 'auto')
            custom_budget = cfg.get('custom_budget') if history_mode == 'custom' else None
            max_output = cfg.get('max_tokens', chatengine.MAX_OUTPUT_TOKENS)
            use_mem = cfg.get('use_memory', True)
            learn_mem = cfg.get('learn_memory', True)
            if stop.is_set():
                self._finish(assistant_id, session_id, request_id, 'interrupted',
                             error='用户停止生成', usage=None)
                self._emit_end(session_id, request_id, 'interrupted', '用户停止生成')
                return

            if getattr(self, '_paused_for_reload', False) or getattr(self, '_shutting_down', False):
                self._finish(assistant_id, session_id, request_id, 'interrupted',
                             error='模型正在重新加载或应用正在退出', usage=None)
                self._emit_end(session_id, request_id, 'interrupted', '模型正在重新加载')
                return

            chatparams.validate_runtime_settings(cfg, self._confirmed_window(active_inst))

            # 1. Deterministic memory pass: ALWAYS analyze turn requirements (0 model requests spent).
            analysis = chatmem.analyze(text)
            scope = analysis.get('request_scope')

            # Only write/upsert/revoke long-term memories when learn_memory is True
            if learn_mem:
                chatmem.apply_analysis(
                    store, session_id=session_id, message_id=user_message_id,
                    analysis=analysis, model_items=[])

            # 2. Select + render memories for THIS send (omits durable memories when use_mem is False).
            chosen = chatmem.select(store, text, request_scope=scope, style=style,
                                    include_durable=use_mem)
            memory_block = chatmem.render(chosen)

            # 2b. Round 51: use the snapshot the SEND took, not a fresh read.
            # Reading here would reopen exactly the gap this round closes: the
            # preflight measured persona A and this thread would send persona B.
            # The snapshot therefore arrives as a parameter and is used verbatim.
            # If it is absent (an older caller) that is a bug, and it is treated
            # as a refusal rather than as "no persona" -- silently dropping a
            # persona the user enabled is the failure this feature must not have.
            if persona_snapshot is None:
                store.drop_empty_assistant(assistant_id, request_id)
                self._emit_end(session_id, request_id, 'refused',
                               '内部错误：本次请求没有携带人格快照，未发送。',
                               extra={'error_kind': 'persona_snapshot_missing'})
                return
            psnap = persona_snapshot
            if not psnap.get('ok'):
                # Nothing was generated, so no assistant turn is recorded -- same
                # shape as the budget refusal below. The page still gets an
                # explicit `refused` event carrying the reason.
                store.drop_empty_assistant(assistant_id, request_id)
                self._emit_end(session_id, request_id, 'refused',
                               f'人格文件读取失败，本次未发送：{psnap.get("error")}',
                               extra={'error_kind': 'persona_error',
                                      'persona_error_code': psnap.get('error_code')})
                return
            persona_block = psnap.get('block') or ''
            persona_meta = {'enabled': psnap.get('enabled'),
                            'chars': psnap.get('chars'),
                            'tokens': None,
                            'applied': bool(persona_block),
                            'path': psnap.get('path')}

            # 3. Budget-aware assembly (exact token counts from the verified runtime instance).
            tokenizer = chatengine.Tokenizer(active_inst.base_url,
                                             counter=self._aux_counter_for(active_inst))
            window = active_inst.actual_window or self._window()
            all_msgs = store.messages(session_id)
            history = [m for m in all_msgs if m['id'] != assistant_id
                       and m['id'] != user_message_id]

            # Boundary filter
            sess_row = store.get_session(session_id) or {}
            boundary_seq = sess_row.get('boundary_seq') or 0
            if boundary_seq > 0:
                history = [m for m in history if (m.get('seq') or 0) > boundary_seq]

            # History mode filter
            if history_mode == 'current_only':
                history = []

            # Check active session summary
            summary_record = None
            summary_block = None
            summary_history = history
            if history_mode != 'current_only':
                rec = store.get_session_summary(session_id, status='ready')
                if rec and rec.get('is_active') and rec.get('status') == 'ready':
                    rec_b = rec.get('boundary_seq') or 0
                    rec_end = rec.get('end_seq') or 0
                    rec_start = rec.get('start_seq') or 0
                    if boundary_seq == 0 or (rec_b == boundary_seq and rec_start > boundary_seq):
                        summary_record = rec
                        summary_block = rec.get('summary_text')
                        summary_history = [m for m in history if (m.get('seq') or 0) > rec_end]

            # ---------------------------------------------------------------- #
            # Round 54: the switch means "the network is ALLOWED", not
            # "search every message".
            #
            # The old code was `is_web_search = bool(cfg.get('web_search'))` and
            # then submitted `text` verbatim, so "介绍一下你自己" went to the
            # search backend and self-introduction templates came back into the
            # prompt. Five states are now separate (see chatsearch_router):
            #   allow_web / needs_search / search_executed / results_found / injected
            # Only `injected` may make the page show "联网回答".
            # ---------------------------------------------------------------- #
            route = self._route_web_search(session_id, text, cfg, history)
            allow_web = route['allow_web']
            search_decision = route['decision']
            query_info = route['query']
            is_web_search = bool(search_decision.get('needs_search'))
            search_query = query_info.get('query') or ''
            route_record = dict(route['record'] or {})

            if route['notice']:
                # Tell the user the switch is off so a "search" request is not
                # silently answered from model knowledge.
                self._publish(session_id, {
                    'kind': 'search_phase', 'session_id': session_id,
                    'request_id': request_id, 'phase': 'not_allowed',
                    'message': route['notice'], 'error': 'web_search_disabled',
                    'user_message_id': user_message_id,
                })

            search_block = None
            search_results: list[chatsearch.SearchResult] = []
            search_id = None
            search_elapsed = 0.0
            sources_meta: list[dict] = []
            raw_sources_meta: list[dict] = []
            trimmed_count = 0
            dropped_results: list[dict] = []
            filter_reason = ''
            results_found = 0
            # Round 55: `search_resp` used to be initialised INSIDE the search
            # branch only, but the terminal event reads it unconditionally, so
            # every non-search reply raised
            #   UnboundLocalError: cannot access local variable 'search_resp'
            # at the very end of the request -- after the answer had already been
            # streamed, which is why it showed up as a failed request rather than
            # a visible error. It is initialised here, with the other search
            # state, so all five outcomes (plain answer / success / no results /
            # failure / cancelled) can finish cleanly.
            search_resp: Optional[chatsearch.SearchResponse] = None
            search_outcome = 'not_attempted'

            if not is_web_search and route['needs_confirmation']:
                # The user asked to search but named nothing we can look up. Ask
                # instead of submitting a dangling pronoun to the backend.
                store.drop_empty_assistant(assistant_id, request_id)
                msg = route['confirmation_message']
                self._publish(session_id, {
                    'kind': 'search_phase', 'session_id': session_id,
                    'request_id': request_id, 'phase': 'failed',
                    'message': msg, 'error': 'search_needs_subject',
                    'user_message_id': user_message_id,
                })
                self._emit_end(session_id, request_id, 'refused', msg,
                               extra={'web_search_failed': True,
                                      'error_kind': 'search_needs_subject',
                                      'route': route['record'],
                                      'user_message_id': user_message_id})
                return

            if is_web_search:
                # Round 54: validate the CONSTRUCTED query, right before the
                # search call it applies to. A refusal here happens before any
                # network work is created and releases the ticket below.
                try:
                    search_query = chatsearch.validate_query(search_query)
                except chatsearch.QueryValidationError as q_err:
                    store.drop_empty_assistant(assistant_id, request_id)
                    msg = str(q_err)
                    self._publish(session_id, {
                        'kind': 'search_phase', 'session_id': session_id,
                        'request_id': request_id, 'phase': 'failed',
                        'message': msg, 'error': 'query_too_long',
                        'user_message_id': user_message_id,
                    })
                    self._emit_end(session_id, request_id, 'refused', msg,
                                   extra={'web_search_failed': True,
                                          'error_kind': 'query_too_long',
                                          'route': route_record,
                                          'query': search_query,
                                          'user_message_id': user_message_id})
                    return
                with self._lock:
                    if self._active.get('request_id') == request_id:
                        self._active.update(is_web_search=True, search_phase='searching')

                if stop.is_set():
                    store.drop_empty_assistant(assistant_id, request_id)
                    self._finish(assistant_id, session_id, request_id, 'interrupted',
                                 error='用户停止生成', usage=None)
                    self._emit_end(session_id, request_id, 'interrupted', '用户停止生成')
                    return

                self._publish(session_id, {
                    'kind': 'search_phase',
                    'session_id': session_id,
                    'request_id': request_id,
                    'phase': 'searching',
                    'message': '正在搜索...'
                })

                t_start_mono = time.monotonic()
                search_deadline = t_start_mono + self.SEARCH_FOREGROUND_BUDGET_S

                # Admission control: a full queue is refused HERE, before any
                # network work is created, so a user cannot grow the backlog by
                # stopping and re-asking. Plain offline chat never reaches this
                # branch, so it is never refused because search capacity is full.
                ticket_holder = _SEARCH_GATE.admit(label=request_id)
                pool_closed = _SEARCH_EXECUTOR_CLOSED['value']
                if ticket_holder is not None and pool_closed:
                    _SEARCH_GATE.release(ticket_holder['ticket'], 'cancelled_before_start')
                    ticket_holder = None
                if ticket_holder is None:
                    gate_now = _SEARCH_GATE.status()
                    fail_msg = ('搜索任务队列已满' if not pool_closed
                                else '应用正在退出，不再发起联网搜索')
                    fail_kind = ('search_queue_full' if not pool_closed
                                 else 'search_shutting_down')
                    store.save_web_search(
                        session_id=session_id, request_id=request_id, query=search_query,
                        status='error', provider='ddgs',
                        error=fail_msg, message_id=None,
                        elapsed_s=round(time.monotonic() - t_start_mono, 3))
                    store.drop_empty_assistant(assistant_id, request_id)
                    if not pool_closed:
                        msg = (f'联网搜索任务已满（执行中 {gate_now["running"]} / '
                               f'排队 {gate_now["queued"]}，上限 {gate_now["max_total"]}），'
                               '本次未发起检索。请稍候再试，或关闭联网搜索直接提问。')
                    else:
                        msg = '应用正在退出，本次联网搜索未发起。'
                    self._publish(session_id, {
                        'kind': 'search_phase',
                        'session_id': session_id,
                        'request_id': request_id,
                        'phase': 'failed',
                        'message': msg,
                        'error': fail_kind,
                        'user_message_id': user_message_id,
                    })
                    self._emit_end(session_id, request_id, 'refused', msg,
                                   extra={
                                       'session_id': session_id,
                                       'request_id': request_id,
                                       'web_search_failed': True,
                                       'error_kind': fail_kind,
                                       'query': text,
                                       'user_message_id': user_message_id,
                                       'search_gate': gate_now,
                                   })
                    return

                search_ticket = ticket_holder['ticket']

                def _run_search(tk: int, q: str) -> Any:
                    _SEARCH_GATE.mark_running(tk)
                    try:
                        return chatsearch.search(q, max_results=5, timeout=10.0,
                                                 should_stop=stop.is_set)
                    finally:
                        # Real release: the HTTP call is over. The user clicking
                        # stop does not release capacity, this does.
                        _SEARCH_GATE.release(tk, 'completed')

                def _abandon_search(reason_tag: str) -> None:
                    """Cancel the queued task; release capacity only if it never started."""
                    cancelled = False
                    try:
                        cancelled = search_fut.cancel()
                    except Exception:  # noqa: BLE001
                        cancelled = False
                    if cancelled:
                        _SEARCH_GATE.release(search_ticket, 'cancelled_before_start')

                # Round 54: submit the CONSTRUCTED query, not the raw message.
                # `search_query` has the ask-phrase removed and (for follow-ups)
                # the subject completed from the previous turn; the persona,
                # memories and the rest of the conversation are never included.
                search_fut = _SEARCH_EXECUTOR.submit(_run_search, search_ticket,
                                                     search_query)

                # Foreground wait: bounded at 10s. This is the budget for how
                # long the PAGE waits for an answer, not for how long the
                # network task lives -- see the timeout branch below.
                foreground_wait_expired = False
                while not search_fut.done():
                    if stop.is_set():
                        _abandon_search('user_stop')
                        store.drop_empty_assistant(assistant_id, request_id)
                        self._finish(assistant_id, session_id, request_id, 'interrupted',
                                     error='用户停止生成', usage=None)
                        self._emit_end(session_id, request_id, 'interrupted', '用户停止生成')
                        return
                    if time.monotonic() >= search_deadline:
                        # Give up WAITING. If the task is still queued we drop it
                        # entirely; if it is on the wire it keeps running, keeps
                        # holding its ticket, and its result is discarded below.
                        foreground_wait_expired = True
                        _abandon_search('foreground_budget')
                        break
                    if getattr(self, '_shutting_down', False) or getattr(self, '_paused_for_reload', False):
                        _abandon_search('shutdown_or_reload')
                        store.drop_empty_assistant(assistant_id, request_id)
                        self._finish(assistant_id, session_id, request_id, 'interrupted',
                                     error='模型正在重新加载或应用正在退出', usage=None)
                        self._emit_end(session_id, request_id, 'interrupted', '模型正在重新加载')
                        return
                    time.sleep(0.05)

                search_elapsed = round(time.monotonic() - t_start_mono, 3)

                # Checkpoint 1: Search completed or timed out
                if stop.is_set():
                    store.drop_empty_assistant(assistant_id, request_id)
                    self._finish(assistant_id, session_id, request_id, 'interrupted',
                                 error='用户停止生成', usage=None)
                    self._emit_end(session_id, request_id, 'interrupted', '用户停止生成')
                    return

                with self._lock:
                    if self._active.get('request_id') != request_id:
                        store.drop_empty_assistant(assistant_id, request_id)
                        return

                if not store.get_session(session_id):
                    store.drop_empty_assistant(assistant_id, request_id)
                    return

                inst_now = self.get_runtime_instance()
                if inst_now.instance_seq != active_inst.instance_seq or inst_now.status != 'ready':
                    store.drop_empty_assistant(assistant_id, request_id)
                    self._finish(assistant_id, session_id, request_id, 'interrupted',
                                 error='模型正在重新加载', usage=None)
                    self._emit_end(session_id, request_id, 'interrupted', '模型正在重新加载')
                    return

                # Collect search response.
                #
                # Three distinct outcomes are recorded separately on purpose:
                #   cancelled  -- the task never started, so no request was made
                #   timeout    -- we stopped WAITING; the request may still be
                #                 running in the background, still holding a
                #                 ticket, and its late result is discarded here
                #   ok/error   -- the task really finished
                # (`search_resp` itself is initialised with the rest of the
                # search state above, so the terminal event never sees it unbound.)
                search_err: Optional[str] = None
                search_outcome = 'ok'
                if search_fut.cancelled():
                    search_outcome = 'cancelled'
                    search_err = '搜索任务在开始执行前已被取消，未发出任何网络请求'
                    search_resp = chatsearch.SearchResponse(
                        results=[], provider='ddgs', client_version='unknown',
                        configured_backends=list(chatsearch.DEFAULT_BACKENDS),
                        active_backend=None,
                        attempts=[{'backend': 'all', 'status': 'cancelled',
                                   'elapsed_s': search_elapsed,
                                   'error': search_err, 'results_count': 0}],
                        fallback_reason='搜索任务被取消',
                        total_elapsed_s=search_elapsed
                    )
                elif not search_fut.done():
                    search_outcome = 'timeout'
                    still_running = _SEARCH_GATE.status()['running']
                    search_err = (f'搜索总超时（{self.SEARCH_FOREGROUND_BUDGET_S:.1f}秒），'
                                  '未在限时内完成检索'
                                  f'；后台仍有 {still_running} 个检索在运行，'
                                  '其结果将被丢弃')
                    search_resp = chatsearch.SearchResponse(
                        results=[], provider='ddgs', client_version='unknown',
                        configured_backends=list(chatsearch.DEFAULT_BACKENDS),
                        active_backend=None,
                        attempts=[{'backend': 'all', 'status': 'timeout', 'elapsed_s': search_elapsed, 'error': search_err, 'results_count': 0}],
                        fallback_reason=f'搜索总限时{self.SEARCH_FOREGROUND_BUDGET_S:.0f}秒超时',
                        total_elapsed_s=search_elapsed
                    )
                else:
                    try:
                        res_val = search_fut.result()
                        if isinstance(res_val, chatsearch.SearchResponse):
                            search_resp = res_val
                        elif isinstance(res_val, list):
                            search_resp = chatsearch.SearchResponse(
                                results=res_val, provider='ddgs', client_version='unknown',
                                configured_backends=['mock'],
                                active_backend='mock' if res_val else None,
                                attempts=[{'backend': 'mock', 'status': 'success' if res_val else 'empty',
                                           'elapsed_s': search_elapsed, 'results_count': len(res_val), 'error': None}],
                                fallback_reason=None,
                                total_elapsed_s=search_elapsed
                            )
                        else:
                            search_resp = chatsearch.SearchResponse(results=[], total_elapsed_s=search_elapsed)
                    except Exception as exc:
                        search_err = str(exc)
                        search_resp = chatsearch.SearchResponse(
                            results=[], provider='ddgs', client_version='unknown',
                            configured_backends=list(chatsearch.DEFAULT_BACKENDS),
                            active_backend=None,
                            attempts=[{'backend': 'all', 'status': 'failed', 'elapsed_s': search_elapsed, 'error': search_err, 'results_count': 0}],
                            fallback_reason=f'异常: {search_err}',
                            total_elapsed_s=search_elapsed
                        )

                # Check for empty or error response
                if search_err or not search_resp.results:
                    status_val = 'error' if search_err else 'empty'
                    err_display = '未获得可用搜索资料'
                    store.save_web_search(
                        session_id=session_id, request_id=request_id, query=search_query,
                        status=status_val, provider=search_resp.provider,
                        backend=search_resp.active_backend,
                        client_version=search_resp.client_version,
                        attempts=search_resp.attempts,
                        fallback_reason=search_resp.fallback_reason,
                        raw_results=[], results=[], trimmed_count=0,
                        message_id=None, elapsed_s=search_elapsed,
                        error=search_err or err_display
                    )
                    store.drop_empty_assistant(assistant_id, request_id)
                    self._publish(session_id, {
                        'kind': 'search_phase',
                        'session_id': session_id,
                        'request_id': request_id,
                        'phase': 'failed',
                        'message': err_display,
                        'error': search_err or err_display,
                        'user_message_id': user_message_id,
                    })
                    self._emit_end(session_id, request_id, 'refused', err_display,
                                   extra={
                                       'web_search_failed': True,
                                       'error_kind': 'search_failed' if search_err else 'no_search_results',
                                       'user_message_id': user_message_id,
                                       'attempts': search_resp.attempts,
                                       'backend': search_resp.active_backend,
                                       'query': text,
                                   })
                    return

                search_results = list(search_resp.results)
                raw_sources_meta = [r.to_dict() for r in search_results]
                results_found = len(search_results)

                # ------------------------------------------------------------ #
                # Round 54: "got results" is not "results are usable".
                #
                # Before anything is injected, drop results that cannot support
                # this question: no title/snippet/url, or no lexical overlap with
                # the query at all. Discarding everything is a legitimate outcome
                # -- the answer then goes out with an honest "no usable material"
                # note instead of a padded list. Relevancy is never lowered to
                # make the source card look populated.
                # ------------------------------------------------------------ #
                filt = chatsearch_router.filter_results(search_results, search_query)
                relevant = list(filt['kept'])
                dropped_results = list(filt['dropped'])
                filter_reason = filt['reason']

                # Re-index the survivors so the prompt's [1]..[n] match the card.
                for new_i, r in enumerate(relevant, start=1):
                    r.index = new_i

                # Checkpoint 2: Before saving sources to DB
                if stop.is_set():
                    store.drop_empty_assistant(assistant_id, request_id)
                    self._finish(assistant_id, session_id, request_id, 'interrupted',
                                 error='用户停止生成', usage=None)
                    self._emit_end(session_id, request_id, 'interrupted', '用户停止生成')
                    return
                with self._lock:
                    if self._active.get('request_id') != request_id:
                        store.drop_empty_assistant(assistant_id, request_id)
                        return
                if not store.get_session(session_id):
                    store.drop_empty_assistant(assistant_id, request_id)
                    return
                inst_now = self.get_runtime_instance()
                if inst_now.instance_seq != active_inst.instance_seq or inst_now.status != 'ready':
                    store.drop_empty_assistant(assistant_id, request_id)
                    self._finish(assistant_id, session_id, request_id, 'interrupted',
                                 error='模型正在重新加载', usage=None)
                    self._emit_end(session_id, request_id, 'interrupted', '模型正在重新加载')
                    return

                if not relevant:
                    # Nothing usable. Answer WITHOUT web material and say so --
                    # never dress model knowledge up as a fresh search result.
                    store.save_web_search(
                        session_id=session_id, request_id=request_id,
                        query=search_query, status='discarded',
                        provider=search_resp.provider,
                        backend=search_resp.active_backend,
                        client_version=search_resp.client_version,
                        attempts=search_resp.attempts,
                        fallback_reason=search_resp.fallback_reason,
                        raw_results=raw_sources_meta, results=[],
                        trimmed_count=results_found,
                        message_id=assistant_id, elapsed_s=search_elapsed,
                        error='检索到的资料与问题无关，已全部舍弃')
                    self._publish(session_id, {
                        'kind': 'search_phase', 'session_id': session_id,
                        'request_id': request_id, 'phase': 'discarded',
                        'message': (f'检索到 {results_found} 条资料，'
                                    '但与这个问题无关，已舍弃；将直接回答'),
                        'discarded': dropped_results[:5],
                        'backend': search_resp.active_backend,
                    })
                    # Fall through to the normal answer path with search_block=None.
                    search_results = []
                    sources_meta = []
                    trimmed_count = results_found
                else:
                    with self._lock:
                        if self._active.get('request_id') == request_id:
                            self._active['search_phase'] = 'found'

                    self._publish(session_id, {
                        'kind': 'search_phase',
                        'session_id': session_id,
                        'request_id': request_id,
                        'phase': 'found',
                        'message': (f'已找到资料（{len(relevant)}条）'
                                    + (f'，另有 {len(dropped_results)} 条无关已舍弃'
                                       if dropped_results else '')),
                        'sources': [r.to_dict() for r in relevant],
                        'discarded': dropped_results[:5],
                        'backend': search_resp.active_backend,
                    })
                    search_results = relevant
                    search_id = store.save_web_search(
                        session_id=session_id, request_id=request_id, query=search_query,
                        status='ready', provider=search_resp.provider,
                        backend=search_resp.active_backend,
                        client_version=search_resp.client_version,
                        attempts=search_resp.attempts,
                        fallback_reason=search_resp.fallback_reason,
                        raw_results=raw_sources_meta,
                        results=[r.to_dict() for r in relevant],
                        trimmed_count=len(dropped_results),
                        message_id=assistant_id,
                        elapsed_s=search_elapsed
                    )

                # Context budget fitting for search reference block
                model_budget = window - max_output
                eff_budget = min(custom_budget, model_budget) if (custom_budget and custom_budget > 0) else model_budget
                fitted_results = list(search_results)
                fitted_block = chatsearch.format_reference_block(fitted_results)

                def test_base_tokens(cand_block: str) -> int:
                    cand_msgs = chatengine.build_messages(
                        memory_block=memory_block, history=[],
                        user_text=text, style=style,
                        summary_block=summary_block, search_block=cand_block,
                        persona_block=persona_block
                    )
                    return chatengine.render_tokens(tokenizer, cand_msgs)

                while len(fitted_results) > 1 and test_base_tokens(fitted_block) > eff_budget:
                    fitted_results.pop()
                    for i, r in enumerate(fitted_results):
                        r.index = i + 1
                    fitted_block = chatsearch.format_reference_block(fitted_results)

                if test_base_tokens(fitted_block) > eff_budget and fitted_results:
                    trimmed = chatsearch.SearchResult(
                        index=1,
                        title=fitted_results[0].title,
                        url=fitted_results[0].url,
                        domain=fitted_results[0].domain,
                        snippet=chatsearch.clean_snippet(fitted_results[0].snippet, max_chars=80),
                        retrieved_at=fitted_results[0].retrieved_at,
                        published_date=fitted_results[0].published_date,
                        provider=fitted_results[0].provider,
                        backend=fitted_results[0].backend,
                    )
                    trimmed_block = chatsearch.format_reference_block([trimmed])
                    if test_base_tokens(trimmed_block) <= eff_budget:
                        fitted_results = [trimmed]
                        fitted_block = trimmed_block

                if test_base_tokens(fitted_block) > eff_budget:
                    store.drop_empty_assistant(assistant_id, request_id)
                    store.update_web_search_message(
                        request_id, assistant_id,
                        results=[], raw_results=raw_sources_meta,
                        trimmed_count=len(search_results),
                        backend=search_resp.active_backend,
                        fallback_reason=search_resp.fallback_reason
                    )
                    self._publish(session_id, {
                        'kind': 'search_phase',
                        'session_id': session_id,
                        'request_id': request_id,
                        'phase': 'failed',
                        'message': '当前上下文预算不足以容纳搜索参考资料',
                        'error': 'search_budget_exceeded',
                        'user_message_id': user_message_id,
                    })
                    self._emit_end(session_id, request_id, 'refused',
                                   '当前上下文预算不足以容纳搜索参考资料，请缩短问题或调整上下文窗口。',
                                   extra={
                                       'web_search_failed': True,
                                       'error_kind': 'search_budget_exceeded',
                                       'budget': eff_budget,
                                       'user_message_id': user_message_id,
                                       'query': text,
                                       'sources_found': len(search_results),
                                   })
                    return

                trimmed_count = len(search_results) - len(fitted_results)
                # Round 54: `''` (nothing survived filtering, or everything was
                # trimmed out) must stay falsy so build_messages injects nothing.
                search_block = fitted_block or None
                sources_meta = [r.to_dict() for r in fitted_results]
                if search_id:
                    store.update_web_search_message(
                        request_id, assistant_id,
                        results=sources_meta,
                        raw_results=raw_sources_meta,
                        trimmed_count=trimmed_count,
                        backend=search_resp.active_backend,
                        fallback_reason=search_resp.fallback_reason
                    )

                # Checkpoint 3: Before calling stream_chat
                if stop.is_set():
                    store.drop_empty_assistant(assistant_id, request_id)
                    self._finish(assistant_id, session_id, request_id, 'interrupted',
                                 error='用户停止生成', usage=None)
                    self._emit_end(session_id, request_id, 'interrupted', '用户停止生成')
                    return
                with self._lock:
                    if self._active.get('request_id') != request_id:
                        store.drop_empty_assistant(assistant_id, request_id)
                        return
                if not store.get_session(session_id):
                    store.drop_empty_assistant(assistant_id, request_id)
                    return
                inst_now = self.get_runtime_instance()
                if inst_now.instance_seq != active_inst.instance_seq or inst_now.status != 'ready':
                    store.drop_empty_assistant(assistant_id, request_id)
                    self._finish(assistant_id, session_id, request_id, 'interrupted',
                                 error='模型正在重新加载', usage=None)
                    self._emit_end(session_id, request_id, 'interrupted', '模型正在重新加载')
                    return

                with self._lock:
                    if self._active.get('request_id') == request_id:
                        self._active['search_phase'] = 'answering'

                self._publish(session_id, {
                    'kind': 'search_phase',
                    'session_id': session_id,
                    'request_id': request_id,
                    'phase': 'answering',
                    'message': '正在回答...',
                    'trimmed_count': trimmed_count,
                    'backend': search_resp.active_backend,
                })

            if stop.is_set():
                self._finish(assistant_id, session_id, request_id, 'interrupted',
                             error='用户停止生成', usage=None)
                self._emit_end(session_id, request_id, 'interrupted', '用户停止生成')
                return

            plan = self._assemble_prompt_plan(
                tokenizer=tokenizer, memory_block=memory_block,
                history=history, summary_history=summary_history,
                text=text, window=window, max_output=max_output,
                style=style, custom_budget=custom_budget,
                summary_block=summary_block, search_block=search_block,
                persona_block=persona_block)

            if stop.is_set():
                self._finish(assistant_id, session_id, request_id, 'interrupted',
                             error='用户停止生成', usage=None)
                self._emit_end(session_id, request_id, 'interrupted', '用户停止生成')
                return

            if not plan.get('ok'):
                # Refused before sending: no token was generated, so no assistant
                # turn is recorded. The user's text stays saved for editing.
                store.drop_empty_assistant(assistant_id, request_id)
                self._emit_end(session_id, request_id, 'refused',
                               plan.get('message'),
                               extra={'error_kind': plan.get('error'),
                                      'need_tokens': plan.get('need_tokens'),
                                      'budget': plan.get('budget')})
                return

            body = chatengine.build_body(plan['messages'],
                                         active_inst.model_alias,
                                         max_tokens=max_output)
            chatengine.assert_pure(body, persona_block=persona_block)
            req_row = store.log_request(
                kind='chat', url=f'{active_inst.base_url}/chat/completions',
                session_id=session_id, message_id=assistant_id,
                request_id=request_id, status=None, ok=False, body=body,
                prompt_tokens=plan['prompt_tokens'],
                profile_id=active_inst.profile_id,
                model_alias=active_inst.model_alias,
                instance_seq=active_inst.instance_seq)
            persona_meta['tokens'] = plan.get('persona_tokens')
            with self._lock:
                if self._active.get('request_id') == request_id:
                    self._active.update(memory_block=memory_block,
                                        memories=[m.get('id') for m in chosen],
                                        persona=persona_meta,
                                        plan={k: plan[k] for k in
                                              ('prompt_tokens', 'budget',
                                               'window', 'turns_included',
                                               'turns_omitted', 'note',
                                               'max_output', 'style',
                                               'is_custom_budget',
                                               'summary_used', 'summary_tokens',
                                               'summary_active', 'tokens_saved',
                                               'fallback_reason',
                                               'search_used', 'search_tokens',
                                               'persona_used',
                                               'persona_tokens') if k in plan})

            # Round 58: Candidate buffering and gap-fill gate
            gap_gate = chatsearch_router.can_gap_fill(
                search_decision, user_text=text,
                already_searched=bool(is_web_search),
                allow_web=allow_web,
                recent_user_texts=self._recent_user_texts(history))
            is_gap_eligible = bool(gap_gate.get('ok'))
            candidate_buffer = ''

            def on_delta(piece: str) -> None:
                nonlocal candidate_buffer
                if is_gap_eligible:
                    candidate_buffer += piece
                    with self._lock:
                        if self._active.get('request_id') == request_id:
                            self._active['chunks'] = self._active.get('chunks', 0) + 1
                    return
                if store.append_delta(assistant_id, session_id, request_id, piece):
                    with self._lock:
                        if self._active.get('request_id') == request_id:
                            self._active['chunks'] = self._active.get('chunks', 0) + 1
                            self._active['text'] = (
                                self._active.get('text', '') + piece)
                    self._publish(session_id, {'kind': 'delta',
                                               'request_id': request_id,
                                               'message_id': assistant_id,
                                               'text': piece})

            call1_attempted = True
            try:
                res = chatengine.stream_chat(
                    body=body, on_delta=on_delta, should_stop=stop.is_set,
                    counter=self._bump, base=active_inst.base_url,
                    persona_block=persona_block)
            except BaseException as exc:
                error1 = f'{type(exc).__name__}: {exc}'
                store.finalize_request_log(
                    req_row, ok=False, status=None,
                    prompt_tokens=None, completion_tokens=None,
                    elapsed_s=None, finish_reason='error',
                    error=error1)
                req_row_closed = True
                raise
            usage1 = res.get('usage')
            finish_reason1 = res.get('finish_reason')
            u1_p = usage1.get('prompt_tokens') if (usage1 and isinstance(usage1, dict)) else None
            u1_c = usage1.get('completion_tokens') if (usage1 and isinstance(usage1, dict)) else None
            store.finalize_request_log(
                req_row, ok=res['ok'], status=res.get('status'),
                prompt_tokens=u1_p,
                completion_tokens=u1_c,
                elapsed_s=res.get('elapsed_s'), finish_reason=res.get('finish_reason'),
                error=res.get('error'))
            req_row_closed = True
            usage = usage1
            finish_reason = finish_reason1
            if stop.is_set() or (res['ok'] and res.get('stopped')):
                status, error = 'interrupted', '用户停止生成'
            elif res['ok'] and res.get('truncated'):
                status = 'interrupted'
                error = '连接中断，已保留收到的部分'
            elif res['ok'] and finish_reason1 == 'length':
                status = 'complete'
                error = f'已达到回复上限（{max_output} tokens），回答已截断'
            elif res['ok']:
                status, error = 'complete', None
            else:
                status, error = 'error', res.get('error') or '模型请求失败'
            if status == 'error':
                store.bump('failed_requests', 1)

            # Check if live instance changed during streaming (superseded response)
            curr_inst = self.get_runtime_instance()
            if curr_inst.instance_seq != active_inst.instance_seq:
                pass

            # Round 58/59: Candidate evaluation & optional single gap-fill search
            gap_fill_note = None
            if is_gap_eligible:
                if status == 'interrupted' and candidate_buffer:
                    store._exec(
                        "UPDATE messages SET content=?, status='interrupted', error=? "
                        "WHERE id=? AND session_id=? AND request_id=?",
                        (candidate_buffer, error, assistant_id, session_id, request_id))
                elif status == 'error':
                    if candidate_buffer:
                        store._exec(
                            "UPDATE messages SET content=?, status='error', error=? "
                            "WHERE id=? AND session_id=? AND request_id=?",
                            (candidate_buffer, error, assistant_id, session_id, request_id))
                    else:
                        store._exec(
                            "UPDATE messages SET content='', status='error', error=? "
                            "WHERE id=? AND session_id=? AND request_id=?",
                            (error, assistant_id, session_id, request_id))

            if status == 'complete' and not stop.is_set() and is_gap_eligible:
                gap_probe = chatsearch_router.model_admits_gap(candidate_buffer, user_text=text)
                if gap_probe['gap']:
                    if stop.is_set():
                        status, error = 'interrupted', '用户停止生成'
                    else:
                        self._publish(session_id, {
                            'kind': 'search_phase', 'session_id': session_id,
                            'request_id': request_id, 'phase': 'gap_fill',
                            'message': '这条回答显示缺少该对象资料，正在补查资料…',
                            'matched': gap_probe.get('matched') or '',
                            'user_message_id': user_message_id,
                        })
                        self._publish(session_id, {
                            'kind': 'message_reset', 'session_id': session_id,
                            'request_id': request_id, 'message_id': assistant_id,
                        })
                        store._exec(
                            "UPDATE messages SET content='', status='streaming', error=NULL "
                            "WHERE id=? AND session_id=? AND request_id=? AND status='streaming'",
                            (assistant_id, session_id, request_id))

                        gap_res = self._gap_fill_search(session_id, request_id, text,
                                                        history, stop)
                        gap_fill_note = gap_res.get('note')

                        if stop.is_set():
                            status, error = 'interrupted', '用户停止生成'
                        elif gap_res.get('block'):
                            try:
                                plan2 = self._assemble_prompt_plan(
                                    tokenizer=tokenizer, memory_block=memory_block,
                                    history=history, summary_history=summary_history,
                                    text=text, window=window, max_output=max_output,
                                    style=style, custom_budget=custom_budget,
                                    summary_block=summary_block, search_block=gap_res['block'],
                                    persona_block=persona_block)
                            except Exception as exc:
                                plan2 = {'ok': False, 'error': f'assembly_error: {exc}',
                                         'message': f'装配异常：{exc}'}

                            if stop.is_set():
                                status, error = 'interrupted', '用户停止生成'
                            elif plan2.get('ok'):
                                search_block = gap_res['block']
                                search_results = list(gap_res.get('results') or [])
                                sources_meta = [r.to_dict() for r in search_results]
                                raw_sources_meta = list(gap_res.get('raw') or [])
                                results_found = gap_res.get('found', 0)
                                dropped_results = list(gap_res.get('dropped') or [])
                                filter_reason = gap_res.get('filter_reason') or ''
                                search_id = gap_res.get('search_id')
                                search_resp = gap_res.get('resp')
                                search_elapsed = gap_res.get('elapsed', 0.0)
                                is_web_search = True
                                query_info = gap_res.get('query_info') or query_info
                                search_query = (query_info or {}).get('query') or search_query
                                route_record.update({
                                    'gap_fill': True,
                                    'gap_fill_reason': gap_probe.get('why') or 'model_stated_gap',
                                    'gap_fill_matched': gap_probe.get('matched') or '',
                                    'gap_fill_query': search_query,
                                    'needs_search': True,
                                })

                                plan = plan2
                                plan['search_used'] = True
                                plan['search_tokens'] = chatengine.render_tokens(
                                    tokenizer, [{'role': 'system', 'content': search_block}])

                                body2 = chatengine.build_body(
                                    plan2['messages'], model=active_inst.model_alias,
                                    max_tokens=max_output)
                                chatengine.assert_pure(body2, persona_block=persona_block)
                                req_row2 = store.log_request(
                                    kind='chat', url=f'{active_inst.base_url}/chat/completions',
                                    session_id=session_id, message_id=assistant_id,
                                    request_id=request_id, status=None, ok=False, body=body2,
                                    prompt_tokens=plan2['prompt_tokens'],
                                    profile_id=active_inst.profile_id,
                                    model_alias=active_inst.model_alias,
                                    instance_seq=active_inst.instance_seq)

                                def on_delta2(piece: str) -> None:
                                    if store.append_delta(assistant_id, session_id,
                                                          request_id, piece):
                                        with self._lock:
                                            if self._active.get('request_id') == request_id:
                                                self._active['chunks'] = (
                                                    self._active.get('chunks', 0) + 1)
                                        self._publish(session_id, {
                                            'kind': 'delta', 'request_id': request_id,
                                            'message_id': assistant_id, 'text': piece})

                                call2_attempted = True
                                try:
                                    res2 = chatengine.stream_chat(
                                        body=body2, on_delta=on_delta2, should_stop=stop.is_set,
                                        counter=self._bump, base=active_inst.base_url,
                                        persona_block=persona_block)
                                except BaseException as exc2:
                                    error2 = f'{type(exc2).__name__}: {exc2}'
                                    store.finalize_request_log(
                                        req_row2, ok=False, status=None,
                                        prompt_tokens=None, completion_tokens=None,
                                        elapsed_s=None, finish_reason='error',
                                        error=error2)
                                    req_row2_closed = True
                                    raise

                                usage2 = res2.get('usage')
                                finish_reason2 = res2.get('finish_reason')
                                u2_p = usage2.get('prompt_tokens') if (usage2 and isinstance(usage2, dict)) else None
                                u2_c = usage2.get('completion_tokens') if (usage2 and isinstance(usage2, dict)) else None
                                store.finalize_request_log(
                                    req_row2, ok=res2['ok'], status=res2.get('status'),
                                    prompt_tokens=u2_p,
                                    completion_tokens=u2_c,
                                    elapsed_s=res2.get('elapsed_s'), finish_reason=res2.get('finish_reason'),
                                    error=res2.get('error'))
                                req_row2_closed = True
                                if stop.is_set() or (res2['ok'] and res2.get('stopped')):
                                    status, error = 'interrupted', '用户停止生成'
                                elif res2['ok'] and res2.get('truncated'):
                                    status = 'interrupted'
                                    error = '连接中断，已保留收到的部分'
                                elif res2['ok'] and finish_reason2 == 'length':
                                    status = 'complete'
                                    error = f'已达到回复上限（{max_output} tokens），回答已截断'
                                elif res2['ok']:
                                    status, error = 'complete', None
                                else:
                                    status, error = 'error', res2.get('error') or '模型请求失败'
                                finish_reason = finish_reason2
                            else:
                                route_record.update({
                                    'gap_fill': True,
                                    'gap_fill_used': False,
                                    'gap_fill_note': '补查资料超过模型上下文预算限制',
                                })
                                fallback_note = f'补查资料超过模型上下文预算（需 {plan2.get("need_tokens")}，可用预算 {plan2.get("budget")} tokens），未注入'
                                fallback_text = f"（{fallback_note}）\n\n{candidate_buffer}"
                                if not self._commit_buffered_reply(
                                        session_id, request_id, assistant_id, fallback_text, stop,
                                        fallback_note=fallback_note, user_message_id=user_message_id):
                                    status, error = 'interrupted', '用户停止生成'
                        else:
                            route_record.update({
                                'gap_fill': True,
                                'gap_fill_used': False,
                                'gap_fill_note': gap_fill_note,
                            })
                            fallback_note = gap_fill_note or '补查未取得可用资料'
                            fallback_text = f"（{fallback_note}）\n\n{candidate_buffer}"
                            if not self._commit_buffered_reply(
                                    session_id, request_id, assistant_id, fallback_text, stop,
                                    fallback_note=fallback_note, user_message_id=user_message_id):
                                status, error = 'interrupted', '用户停止生成'
                elif status == 'complete' and not stop.is_set():
                    if not self._commit_buffered_reply(
                            session_id, request_id, assistant_id, candidate_buffer, stop):
                        status, error = 'interrupted', '用户停止生成'

            # Chat streaming is finished.
            if search_id:
                store.update_web_search_message(
                    request_id, assistant_id,
                    elapsed_s=search_resp.total_elapsed_s if search_resp else search_elapsed,
                    tokens_used=plan.get('search_tokens', 0),
                    backend=search_resp.active_backend if search_resp else None,
                    fallback_reason=search_resp.fallback_reason if search_resp else None,
                )

            # Round 59: Accurate dual call usage accounting and final call metrics
            final_usage = usage2 if call2_attempted else usage1
            candidate_usage = usage1 if call2_attempted else None
            physical_calls = int(call1_attempted) + int(call2_attempted)

            u1_valid = bool(usage1 and isinstance(usage1, dict) and usage1.get('prompt_tokens') is not None and usage1.get('completion_tokens') is not None)
            u2_valid = bool(usage2 and isinstance(usage2, dict) and usage2.get('prompt_tokens') is not None and usage2.get('completion_tokens') is not None)

            total_usage = None
            if call2_attempted:
                if u1_valid and u2_valid:
                    total_usage = {
                        'prompt_tokens': usage1['prompt_tokens'] + usage2['prompt_tokens'],
                        'completion_tokens': usage1['completion_tokens'] + usage2['completion_tokens'],
                        'total_tokens': (usage1['prompt_tokens'] + usage2['prompt_tokens']) + (usage1['completion_tokens'] + usage2['completion_tokens']),
                        'is_complete': True,
                    }
                elif u1_valid and not u2_valid:
                    total_usage = {
                        'prompt_tokens': usage1['prompt_tokens'],
                        'completion_tokens': usage1['completion_tokens'],
                        'total_tokens': usage1['prompt_tokens'] + usage1['completion_tokens'],
                        'is_complete': False,
                        'missing_calls': [2],
                    }
                elif u2_valid and not u1_valid:
                    total_usage = {
                        'prompt_tokens': usage2['prompt_tokens'],
                        'completion_tokens': usage2['completion_tokens'],
                        'total_tokens': usage2['prompt_tokens'] + usage2['completion_tokens'],
                        'is_complete': False,
                        'missing_calls': [1],
                    }
                else:
                    total_usage = None
            else:
                if u1_valid:
                    total_usage = {
                        'prompt_tokens': usage1['prompt_tokens'],
                        'completion_tokens': usage1['completion_tokens'],
                        'total_tokens': usage1['prompt_tokens'] + usage1['completion_tokens'],
                        'is_complete': True,
                    }
                else:
                    total_usage = None

            # 1. Immediately finish assistant message in DB to complete/interrupted/error
            self._finish(assistant_id, session_id, request_id, status,
                         error=error, usage=final_usage)

            # 2. Emit SSE end event
            #
            # Round 54: `injected` is the ONLY flag that may make the page show
            # "联网回答". It is true only when a non-empty reference block really
            # went into the prompt that produced this answer. `allow_web` and
            # `search_executed` are reported separately so the log can tell
            # "was allowed", "did search", "found results", "used them" apart.
            injected = bool(plan.get('search_used')) and bool(search_block)
            route_record.update({
                'search_executed': bool(search_id is not None or search_results),
                'results_found': results_found,
                'injected': injected,
                'filter_reason': filter_reason,
            })

            row = store.get_message(assistant_id) or {}
            final_status = row.get('status') or status
            self._emit_end(session_id, request_id, final_status,
                           row.get('error') or error,
                           extra={'usage': final_usage,
                                  'model_calls': physical_calls,
                                  'candidate_usage': candidate_usage,
                                  'total_usage': total_usage,
                                  'plan': plan.get('note'),
                                  'finish_reason': finish_reason,
                                  'turns_included': plan.get('turns_included'),
                                  'turns_omitted': plan.get('turns_omitted'),
                                  'prompt_tokens': plan.get('prompt_tokens'),
                                  'summary_used': plan.get('summary_used', False),
                                  'summary_active': plan.get('summary_active', False),
                                  'tokens_saved': plan.get('tokens_saved', 0),
                                  'fallback_reason': plan.get('fallback_reason'),
                                  'summary_tokens': plan.get('summary_tokens', 0),
                                  # `search_used` now means "material was injected".
                                  'search_used': injected,
                                  'allow_web': allow_web,
                                  'search_executed': route_record.get('search_executed', False),
                                  'search_route': route_record,
                                  'search_tokens': plan.get('search_tokens', 0),
                                  'sources': sources_meta if injected else None,
                                  'raw_sources': raw_sources_meta if results_found else None,
                                  'trimmed_count': trimmed_count if injected else 0,
                                  'discarded_count': len(dropped_results),
                                  'filter_reason': filter_reason,
                                  'backend': search_resp.active_backend if search_resp else None,
                                  'client_version': search_resp.client_version if search_resp else None,
                                  'attempts': search_resp.attempts if search_resp else None,
                                  'max_output': max_output,
                                  'style': style,
                                  'is_custom_budget': plan.get('is_custom_budget')})

            # 3. Release generation slot immediately
            with self._lock:
                if self._active.get('request_id') == request_id:
                    self._active = {}
            self._slot_release(request_id)

            # 4. Asynchronously schedule background extraction only if chat completed cleanly.
            # Round 54: the condition is now "no material was injected", not "no
            # search happened" -- a request that searched but used nothing is an
            # ordinary chat and may still be learned from.
            if not injected and learn_mem and status == 'complete' and analysis.get('wants_model') and not stop.is_set():
                self._schedule_background_extract(session_id, user_message_id, text)
        except BaseException as exc:  # noqa: BLE001 - a crash must still finalise
            error = f'{type(exc).__name__}: {exc}'
            # Close only unfinished records; later application errors must not
            # erase successful HTTP metrics from an earlier physical call.
            for pending_row, closed, pending_usage in (
                    (req_row, req_row_closed, usage1),
                    (req_row2, req_row2_closed, usage2)):
                if pending_row and not closed:
                    try:
                        store.finalize_request_log(
                            pending_row, ok=False, status=None,
                            prompt_tokens=(pending_usage or {}).get('prompt_tokens'),
                            completion_tokens=(pending_usage or {}).get('completion_tokens'),
                            elapsed_s=None, finish_reason='error', error=error)
                    except Exception:
                        pass

            if stop.is_set():
                exc_status, exc_error = 'interrupted', '用户停止生成'
            else:
                exc_status, exc_error = 'error', error

            if is_gap_eligible and not call2_attempted and candidate_buffer:
                try:
                    store._exec(
                        "UPDATE messages SET content=?, status=?, error=? "
                        "WHERE id=? AND session_id=? AND request_id=?",
                        (candidate_buffer, exc_status, exc_error, assistant_id, session_id, request_id))
                except Exception:
                    pass

            final_usage = usage2 if call2_attempted else usage1
            try:
                self._finish(assistant_id, session_id, request_id, exc_status,
                             error=exc_error, usage=final_usage)
            except Exception:  # noqa: BLE001
                pass
            self._emit_end(session_id, request_id, exc_status, exc_error,
                           extra={'model_calls': int(call1_attempted) + int(call2_attempted),
                                  'usage': final_usage,
                                  'candidate_usage': usage1 if call2_attempted else None,
                                  'total_usage': None})
        finally:
            with self._lock:
                if self._active.get('request_id') == request_id:
                    self._active = {}
            # Always hand the model back, whoever owns it now or not.
            self._slot_release(request_id)

    def _schedule_background_extract(self, session_id: str, user_message_id: str, text: str) -> None:
        """Schedule asynchronous, non-blocking background extraction with atomic reservation."""
        if self.store.get_session(session_id) is None:
            return

        with self._lock:
            # Atomic checks under lock: shutting down, paused for reload, or instance not ready
            if getattr(self, '_shutting_down', False):
                self.store._event(session_id, user_message_id, None, 'skip',
                                  'extraction skipped: application shutting down')
                return

            if getattr(self, '_paused_for_reload', False):
                self.store._event(session_id, user_message_id, None, 'skip',
                                  'extraction skipped: model reloading in progress')
                return

            inst = self.get_runtime_instance()
            if inst.status != 'ready':
                self.store._event(session_id, user_message_id, None, 'skip',
                                  f'extraction skipped: model not ready (status={inst.status})')
                return

            # Cancel prior background task if any
            if self._bg_task:
                self._bg_task['cancel'].set()
                self._slot_request_cancel(self._bg_task['task_id'])

            cancel_ev = threading.Event()
            task_id = chatstore.new_id('bg')
            start_version = self.store.current_version()
            task = {
                'task_id': task_id,
                'session_id': session_id,
                'message_id': user_message_id,
                'text': text,
                'cancel': cancel_ev,
                'created_at': time.time(),
                'start_version': start_version,
                'instance': inst,
                'instance_seq': inst.instance_seq,
            }
            self._bg_task = task

        t = threading.Thread(target=self._run_background_extract, daemon=True,
                             name=f'bg-extract-{task_id}', args=(task,))
        with self._lock:
            if self._bg_task and self._bg_task.get('task_id') == task_id:
                self._bg_task['thread'] = t
        t.start()

    def _run_background_extract(self, task: dict) -> None:
        """One extraction request, holding the model slot for exactly its duration."""
        session_id = task['session_id']
        msg_id = task['message_id']
        cancel_ev = task['cancel']
        text = task['text']
        task_id = task['task_id']
        start_version = task.get('start_version')
        inst: RuntimeInstance = task.get('instance')
        task_instance_seq = task.get('instance_seq')

        try:
            if cancel_ev.is_set():
                self.store._event(session_id, msg_id, None, 'skip',
                                  'extraction skipped: cancelled before start')
                return
            if getattr(self, '_shutting_down', False) or getattr(self, '_paused_for_reload', False):
                self.store._event(session_id, msg_id, None, 'skip',
                                  'extraction skipped: paused for reload or shutting down')
                return
            if self.store.get_session(session_id) is None:
                return

            current_inst = self.get_runtime_instance()
            if current_inst.instance_seq != task_instance_seq or current_inst.status != 'ready':
                self.store._event(session_id, msg_id, None, 'skip',
                                  f'extraction skipped: instance invalidated (task_seq={task_instance_seq}, current_seq={current_inst.instance_seq})')
                return

            got = self._slot_acquire('extract', task_id,
                                     timeout_s=self.EXTRACTION_DRAIN_TIMEOUT_S)
            if not got.get('ok'):
                self.store._event(session_id, msg_id, None, 'skip',
                                  f'extraction skipped: model slot not free ({got.get("reason")})')
                return

            try:
                if cancel_ev.is_set() or getattr(self, '_paused_for_reload', False) or getattr(self, '_shutting_down', False):
                    self.store._event(session_id, msg_id, None, 'skip',
                                      'extraction skipped: cancelled or paused before request')
                    return
                if self.store.get_session(session_id) is None:
                    self.store._event(session_id, msg_id, None, 'skip',
                                      'extraction skipped: session gone')
                    return

                # Check instance again before issuing wire request
                current_inst = self.get_runtime_instance()
                if current_inst.instance_seq != task_instance_seq or current_inst.status != 'ready':
                    self.store._event(session_id, msg_id, None, 'skip',
                                      f'extraction skipped: instance changed before request')
                    return

                self._bump('extraction')
                body = {'model': inst.model_alias,
                        'messages': [
                            {'role': 'system', 'content': chatmem.EXTRACT_SYSTEM},
                            {'role': 'user',
                             'content': chatmem.extract_user_prompt(text)}],
                        'stream': False, 'temperature': 0, 'top_k': 1, 'top_p': 1,
                        'max_tokens': 220}
                req_row = self.store.log_request(
                    kind='extraction',
                    url=f'{inst.base_url}/chat/completions',
                    session_id=session_id, message_id=msg_id, request_id=task_id,
                    status=None, ok=False, body=body,
                    profile_id=inst.profile_id,
                    model_alias=inst.model_alias,
                    instance_seq=inst.instance_seq)

                res = chatengine.extract_once(user_text=text,
                                              model_alias=inst.model_alias,
                                              base=inst.base_url,
                                              timeout=self.EXTRACT_TIMEOUT_S)
                parsed = chatmem.parse_extraction(res.get('raw', ''), text)
                try:
                    self.store._exec(
                        'UPDATE request_log SET ok=?, status=?, elapsed_s=?, error=?, '
                        'completion_tokens=? WHERE id=?',
                        (1 if res.get('ok') else 0, res.get('status'),
                         res.get('elapsed_s'), res.get('error'),
                         (res.get('usage') or {}).get('completion_tokens'),
                         req_row))
                except Exception:  # noqa: BLE001
                    pass

                # Check validity before commit
                if cancel_ev.is_set():
                    self.store._event(session_id, msg_id, None, 'skip',
                                      'extraction cancelled before commit')
                    return
                if getattr(self, '_paused_for_reload', False) or getattr(self, '_shutting_down', False):
                    self.store._event(session_id, msg_id, None, 'skip',
                                      'extraction skipped: paused for reload during request')
                    return
                # Check instance sequence hasn't changed
                current_inst = self.get_runtime_instance()
                if current_inst.instance_seq != task_instance_seq:
                    self.store._event(session_id, msg_id, None, 'skip',
                                      f'extraction skipped: instance changed before commit (task_seq={task_instance_seq}, current_seq={current_inst.instance_seq})')
                    return

                if self.store.get_session(session_id) is None:
                    self.store._event(session_id, msg_id, None, 'skip',
                                      'extraction skipped: session deleted')
                    return
                user_msg = self.store.get_message(msg_id)
                if not user_msg:
                    self.store._event(session_id, msg_id, None, 'skip',
                                      'extraction skipped: user message deleted')
                    return
                if not res.get('ok') or not parsed.get('ok'):
                    self.store._event(session_id, msg_id, None, 'skip',
                                      f'extraction skipped: {res.get("error") or parsed.get("reason")}')
                    return
                if start_version is not None:
                    now_v = self.store.current_version()
                    if now_v != start_version:
                        self.store._event(
                            session_id, msg_id, None, 'skip',
                            f'extraction skipped: memory state changed while the request was in flight (v{start_version} -> v{now_v})')
                        return

                chatmem.apply_analysis(
                    self.store, session_id=session_id, message_id=msg_id,
                    analysis={'items': []}, model_items=parsed['items'])
            finally:
                self._slot_release(task_id)
        except Exception:  # noqa: BLE001 - a background task must never raise
            return
        finally:
            with self._lock:
                if self._bg_task and self._bg_task['task_id'] == task_id:
                    self._bg_task = None
            self._slot_release(task_id)

    def _background_extract(self, session_id: str, user_message_id: str, text: str) -> None:
        """Compatibility wrapper delegating to _schedule_background_extract."""
        self._schedule_background_extract(session_id, user_message_id, text)

    @staticmethod
    def _confirmed_window(inst: RuntimeInstance) -> Optional[int]:
        n = inst.actual_window
        if type(n) is int and n > 0 and inst.actual_window_source == 'service-probe':
            return n
        return None

    def _window(self) -> int:
        n = self._confirmed_window(self.get_runtime_instance())
        if n is None:
            raise chatparams.ParamError('actual_window_unknown',
                                       '尚未确认模型服务的实际上下文窗口。')
        return n

    def _extract(self, session_id: str, message_id: str, text: str) -> list[dict]:
        """At most ONE extraction request. Any failure = skip, never retry."""
        inst = self.get_runtime_instance()
        self._bump('extraction')
        body = {'model': inst.model_alias,
                'messages': [
                    {'role': 'system', 'content': chatmem.EXTRACT_SYSTEM},
                    {'role': 'user', 'content': chatmem.extract_user_prompt(text)}],
                'stream': False, 'temperature': 0, 'top_k': 1, 'top_p': 1,
                'max_tokens': 220}
        row = self.store.log_request(
            kind='extraction',
            url=f'{inst.base_url}/chat/completions',
            session_id=session_id, message_id=message_id, request_id=None,
            status=None, ok=False, body=body,
            profile_id=inst.profile_id, model_alias=inst.model_alias,
            instance_seq=inst.instance_seq)
        res = chatengine.extract_once(user_text=text, model_alias=inst.model_alias, base=inst.base_url)
        parsed = chatmem.parse_extraction(res.get('raw', ''), text)
        try:
            self.store._exec(
                'UPDATE request_log SET ok=?, elapsed_s=?, error=?, '
                'completion_tokens=? WHERE id=?',
                (1 if res.get('ok') else 0, res.get('elapsed_s'),
                 res.get('error'), (res.get('usage') or {}).get('completion_tokens'),
                 row))
        except Exception:  # noqa: BLE001
            pass
        if not res.get('ok') or not parsed.get('ok'):
            self.store._event(session_id, message_id, None, 'skip',
                              f'extraction skipped: {res.get("error") or parsed.get("reason")}')
            return []
        return parsed['items']

    def _finish(self, assistant_id: str, session_id: str, request_id: str,
                status: str, error: Optional[str],
                usage: Optional[dict]) -> None:
        n = self.store.finish_assistant_message(
            assistant_id, session_id, request_id, status, error=error, usage=usage)
        if n == 0:
            # Someone else already finalised this row (a stop raced the worker).
            return

    def _emit_end(self, session_id: str, request_id: str, status: str,
                  error: Optional[str], extra: Optional[dict] = None) -> None:
        """Emit the terminal `generation_end` SSE event for one attempt.

        ## Wire contract (round 39)

        The payload is FLAT. `extra` is spread onto the top level; it is never
        nested under an `extra` key. The frontend reads `d.<field>`, and the
        fields below are the stable, documented part of the contract:

            session_id       str   the session this attempt belongs to
            request_id       str   the attempt identity (new on every retry)
            status           str   complete | interrupted | error | refused
            error            str   human-readable reason, or null
            web_search_failed bool true only when a search attempt failed
            error_kind       str   machine reason: search_failed |
                                   no_search_results | search_budget_exceeded |
                                   search_queue_full | <plan error kind>
            query            str   the text that was actually searched
            user_message_id  str   the user row this attempt answers
            chunks           int   deltas emitted before the end

        `session_id` is included here rather than left to callers so that a late
        event from an abandoned attempt is always attributable: the page can see
        it belongs to another session/request instead of assuming it is current.
        """
        with self._lock:
            if self._active.get('request_id') != request_id:
                return
            snapshot = dict(self._active)
        payload = {'kind': 'generation_end',
                   'session_id': session_id,
                   'request_id': request_id,
                   'status': status,
                   'error': error,
                   'chunks': snapshot.get('chunks', 0)}
        if snapshot.get('user_message_id'):
            payload['user_message_id'] = snapshot['user_message_id']
        if snapshot.get('retry_resolution'):
            payload['retry_resolution'] = snapshot['retry_resolution']
        if extra:
            # Contract keys win over nothing: `extra` may add fields but must
            # not be able to rewrite the identity of the event it decorates.
            for k, v in extra.items():
                if k in ('kind',):
                    continue
                payload[k] = v
            payload['session_id'] = session_id
            payload['request_id'] = request_id
        self._publish(session_id, payload)

    # -- SSE --------------------------------------------------------------
    def _publish(self, session_id: str, event: dict) -> None:
        """Hand an event to every subscriber of this session.

        `notify_all` requires the condition to be held, so the append and the
        notify happen inside `with sub['cond']` -- otherwise a streaming reply
        would kill its own worker thread on the first chunk.
        """
        with self._sse_lock:
            subs = [s for s in _SUBSCRIBERS if s['session_id'] == session_id]
        for sub in subs:
            with sub['cond']:
                sub['queue'].append(event)
                sub['cond'].notify_all()

    def subscribe(self, session_id: str) -> dict:
        sub = {'session_id': session_id, 'queue': [],
               'cond': threading.Condition(), 'since': 0}
        with self._sse_lock:
            _SUBSCRIBERS.append(sub)
        return sub

    def unsubscribe(self, sub: dict) -> None:
        with self._sse_lock:
            if sub in _SUBSCRIBERS:
                _SUBSCRIBERS.remove(sub)

    # -- stop -------------------------------------------------------------
    def stop(self, session_id: Optional[str] = None) -> dict:
        with self._lock:
            if self._bg_task and (not session_id or self._bg_task.get('session_id') == session_id):
                self._bg_task['cancel'].set()
            a = self._active
            if not a:
                return {'ok': False, 'message': '当前没有正在生成的回复。'}
            if session_id and a['session_id'] != session_id:
                return {'ok': False, 'message': '正在生成的是另一个会话。'}
            a['stop'].set()
            request_id = a['request_id']
            assistant_id = a['message_id']
        # The worker also finalises; whoever gets there first wins, and the
        # guarded UPDATE makes a double finalise a no-op.
        self.store.finish_assistant_message(
            assistant_id, session_id or a['session_id'], request_id,
            'interrupted', error='用户停止生成')
        self._publish(session_id or a['session_id'],
                      {'kind': 'stop_ack', 'request_id': request_id})
        return {'ok': True,
                'message': ('已停止：不再接收这次回复的后续内容，已收到的部分会保留并'
                            '标记为「已中断」。'),
                'request_id': request_id}

    def cancel_and_drain(self, timeout_s: float = 3.0) -> dict[str, Any]:
        """Cancel in-flight chat generation and background extraction, waiting
        for worker thread to join and persist interrupted state."""
        self._shutting_down = True
        start_t = time.perf_counter()
        chat_t = None
        bg_t = None
        sum_t = None
        with self._lock:
            if self._bg_task:
                self._bg_task['cancel'].set()
                bg_t = self._bg_task.get('thread')
            if self._summary_task:
                self._summary_task['cancel'].set()
                sum_t = self._summary_task.get('thread')
            if self._active and 'stop' in self._active:
                self._active['stop'].set()
                chat_t = self._active.get('thread')
                req_id = self._active.get('request_id')
                msg_id = self._active.get('message_id')
                sess_id = self._active.get('session_id')
                if msg_id and sess_id:
                    self.store.finish_assistant_message(
                        msg_id, sess_id, req_id, 'interrupted', error='桌面退出，保存当前进度')

        # Bounded waiting for threads to exit
        deadline = time.time() + timeout_s
        for t in (chat_t, bg_t, sum_t):
            if t and t.is_alive():
                remaining = max(0.01, deadline - time.time())
                t.join(timeout=remaining)

        chat_alive = bool(chat_t and chat_t.is_alive())
        bg_alive = bool(bg_t and bg_t.is_alive())
        sum_alive = bool(sum_t and sum_t.is_alive())
        with self._lock:
            active_left = bool(self._active)
            bg_left = bool(self._bg_task)
            sum_left = bool(self._summary_task)

        drained = not (chat_alive or bg_alive or sum_alive or active_left or bg_left or sum_left)
        return {
            'drained': drained,
            'chat_drained': not chat_alive,
            'bg_drained': not bg_alive,
            'sum_drained': not sum_alive,
            'active_left': active_left,
            'bg_left': bg_left,
            'sum_left': sum_left,
            'elapsed_s': round(time.perf_counter() - start_t, 3),
        }

    def shutdown_search_pool(self, wait: bool = False,
                             timeout_s: float = 2.0,
                             drain: bool = False) -> dict:
        """Stop accepting new searches and deal with the ones already running.

        Queued searches have issued no request yet, so cancelling them is free
        and correct. A search already on the wire is NOT aborted here (the HTTP
        client cannot be interrupted); with `drain=True` we wait for it up to
        `timeout_s` and report whether it really finished, rather than implying
        that `wait=False` makes the process exit immediately.
        """
        self._shutting_down = True
        return shutdown_search_executor(wait=wait, timeout_s=timeout_s,
                                        drain=drain)

    def delete_session(self, session_id: str) -> dict:
        with self._lock:
            if self._bg_task and self._bg_task.get('session_id') == session_id:
                self._bg_task['cancel'].set()
            if self._summary_task and self._summary_task.get('session_id') == session_id:
                self._summary_task['cancel'].set()
        return self.store.delete_session(session_id)

    # -- summary compression ---------------------------------------------
    def get_session_summary_status(self, session_id: str) -> dict:
        sess = self.store.get_session(session_id)
        if sess is None:
            return {'ok': False, 'error': 'not_found', 'message': '会话不存在。'}
        ready_summary = self.store.get_session_summary(session_id, status='ready')
        latest_summary = self.store.get_session_summary(session_id)
        last_error = None
        if latest_summary and latest_summary.get('status') == 'failed':
            last_error = latest_summary.get('error')

        with self._lock:
            is_generating = bool(self._summary_task and self._summary_task.get('session_id') == session_id
                                and not self._summary_task.get('done', threading.Event()).is_set())
            busy = self.busy_summary()
            task_err = self._summary_task.get('error') if (self._summary_task and self._summary_task.get('session_id') == session_id) else None
        if not last_error and task_err:
            last_error = task_err

        all_msgs = self.store.messages(session_id)
        boundary_seq = sess.get('boundary_seq') or 0
        history = [m for m in all_msgs if (m.get('seq') or 0) > boundary_seq]
        partition = chatcompress.select_candidate_turns(history, recent_turns_kept=3, boundary_seq=boundary_seq)
        inst = self.get_runtime_instance()
        window = inst.actual_window or self._window()
        return {
            'ok': True,
            'session_id': session_id,
            'summary': ready_summary,
            'latest_summary': latest_summary,
            'last_error': last_error,
            'is_generating': is_generating,
            'can_compress': partition['can_compress'],
            'reason': partition.get('reason'),
            'candidate_turns_count': len(partition['candidate_turns']),
            'recent_turns_count': len(partition['recent_turns']),
            'recent_turns_kept': 3,
            'boundary_seq': boundary_seq,
            'window': window,
            'busy': bool(busy),
            'busy_reason': busy,
        }

    def schedule_summary_generation(self, session_id: str, recent_turns_kept: int = 3) -> dict:
        if getattr(self, '_shutting_down', False):
            return {'ok': False, 'error': 'shutting_down', 'message': '应用正在退出，无法压缩对话。'}
        if getattr(self, '_paused_for_reload', False):
            return {'ok': False, 'error': 'model_reloading', 'message': '模型正在重新加载中，请稍候。'}
        inst = self.get_runtime_instance()
        if inst.status != 'ready':
            return {'ok': False, 'error': 'model_not_ready', 'message': f'模型服务当前不可用（状态：{inst.status}）'}

        sess = self.store.get_session(session_id)
        if not sess:
            return {'ok': False, 'error': 'no_session', 'message': '会话不存在。'}

        boundary_seq = sess.get('boundary_seq') or 0
        all_msgs = self.store.messages(session_id)
        history = [m for m in all_msgs if (m.get('seq') or 0) > boundary_seq]
        partition = chatcompress.select_candidate_turns(history, recent_turns_kept=recent_turns_kept, boundary_seq=boundary_seq)
        if not partition['can_compress']:
            return {'ok': False, 'error': 'cannot_compress', 'message': partition.get('reason', '没有足够可压缩的较早对话轮次。')}

        task_id = chatstore.new_id('sum')
        cancel_ev = threading.Event()
        done_ev = threading.Event()
        task = {
            'task_id': task_id,
            'session_id': session_id,
            'cancel': cancel_ev,
            'done': done_ev,
            'started_at': chatstore.now(),
            'instance_seq': inst.instance_seq,
            'boundary_seq': boundary_seq,
            'recent_turns_kept': recent_turns_kept,
            'error': None
        }

        with self._lock:
            if getattr(self, '_shutting_down', False) or getattr(self, '_paused_for_reload', False):
                return {'ok': False, 'error': 'model_reloading', 'message': '模型正在重新加载中或应用正在退出。'}
            busy = self.busy_summary()
            if busy:
                return {'ok': False, 'error': 'busy', 'message': f'模型当前忙碌（{busy}），无法开始压缩。'}
            self._summary_task = task

        got = self._slot_acquire('summarize', task_id, timeout_s=0.5)
        if not got.get('ok'):
            with self._lock:
                if self._summary_task and self._summary_task.get('task_id') == task_id:
                    self._summary_task = None
            return {'ok': False, 'error': 'slot_unavailable', 'message': '模型正被其他任务占用。'}

        th = threading.Thread(target=self._run_summary_generation, daemon=True,
                              name=f'compress-{task_id}', args=(task,))
        task['thread'] = th
        th.start()
        return {'ok': True, 'task_id': task_id, 'session_id': session_id,
                'candidate_turns_count': len(partition['candidate_turns'])}

    def stop_summary_generation(self, session_id: Optional[str] = None) -> dict:
        with self._lock:
            task = self._summary_task
            if not task or task.get('done', threading.Event()).is_set():
                return {'ok': False, 'message': '当前没有正在进行的对话压缩任务。'}
            if session_id and task.get('session_id') != session_id:
                return {'ok': False, 'message': '正在压缩的是另一个会话。'}
            task['cancel'].set()
        return {'ok': True, 'message': '已请求停止对话压缩。'}

    def set_summary_active(self, session_id: str, is_active: bool) -> dict:
        sess = self.store.get_session(session_id)
        if not sess:
            return {'ok': False, 'error': 'no_session', 'message': '会话不存在。'}
        return self.store.set_summary_active(session_id, is_active)

    def delete_session_summary(self, session_id: str) -> dict:
        with self._lock:
            if self._summary_task and self._summary_task.get('session_id') == session_id:
                self._summary_task['cancel'].set()
                self._summary_task = None
        return self.store.delete_session_summary(session_id)

    def _run_summary_generation(self, task: dict) -> None:
        task_id = task['task_id']
        session_id = task['session_id']
        cancel = task['cancel']
        done = task['done']
        try:
            if cancel.is_set() or getattr(self, '_shutting_down', False) or getattr(self, '_paused_for_reload', False):
                return
            inst = self.get_runtime_instance()
            if inst.instance_seq != task['instance_seq'] or inst.status != 'ready':
                return

            sess = self.store.get_session(session_id)
            if sess is None:
                return
            curr_boundary = sess.get('boundary_seq') or 0
            if curr_boundary != task['boundary_seq']:
                return

            all_msgs = self.store.messages(session_id)
            history = [m for m in all_msgs if (m.get('seq') or 0) > curr_boundary]
            partition = chatcompress.select_candidate_turns(history, recent_turns_kept=task['recent_turns_kept'], boundary_seq=curr_boundary)
            if not partition['can_compress']:
                return
            candidate_turns = partition['candidate_turns']

            window = inst.actual_window or self._window()
            tokenizer = chatengine.Tokenizer(inst.base_url, counter=self._aux_counter_for(inst))
            budget_plan = chatcompress.calculate_compression_budget(candidate_turns=candidate_turns, window=window, tokenizer=tokenizer)
            if not budget_plan['ok']:
                err_msg = budget_plan.get('message') or budget_plan.get('error')
                task['error'] = err_msg
                self.store.log_request(
                    kind='aux', url=f'{inst.base_url}/summary/failed',
                    session_id=session_id, message_id=None, request_id=task_id,
                    status=None, ok=False, body={'error': err_msg},
                    profile_id=inst.profile_id, model_alias=inst.model_alias,
                    instance_seq=inst.instance_seq)
                self.store.save_session_summary(session_id, {
                    'status': 'failed',
                    'error': err_msg,
                    'boundary_seq': curr_boundary,
                    'instance_seq': inst.instance_seq,
                    'is_active': False
                })
                self._publish(session_id, {
                    'kind': 'summary_error',
                    'session_id': session_id,
                    'ok': False,
                    'error': err_msg
                })
                return

            selected_turns = budget_plan['selected_turns']
            if not selected_turns:
                return
            start_seq = budget_plan['start_seq']
            end_seq = budget_plan['end_seq']
            raw_turns_tokens = budget_plan['raw_tokens']
            req_messages = budget_plan['req_messages']
            alias_to_msg = budget_plan['alias_to_msg']
            valid_ids = budget_plan['valid_ids']

            if cancel.is_set():
                return

            self.store.bump('aux_requests', 1)
            req_row = self.store.log_request(
                kind='aux', url=f'{inst.base_url}/chat/completions',
                session_id=session_id, message_id=None, request_id=task_id,
                status=None, ok=False, body={'model': inst.model_alias, 'messages': req_messages},
                profile_id=inst.profile_id, model_alias=inst.model_alias,
                instance_seq=inst.instance_seq)

            res = chatcompress.execute_summary_generation(
                base_url=inst.base_url,
                model_alias=inst.model_alias,
                messages=req_messages,
                alias_to_msg=alias_to_msg,
                valid_ids=valid_ids,
                max_tokens=budget_plan.get('max_output_tokens', 512),
                should_stop=cancel.is_set)

            if cancel.is_set() or res.get('stopped'):
                self.store._exec('UPDATE request_log SET ok=?, status=?, finish_reason=?, error=? WHERE id=?',
                                 (0, 'interrupted', 'stopped', '用户停止压缩', req_row))
                return

            fin_reason = res.get('finish_reason')
            saw_done_val = res.get('saw_done', False)

            if not res.get('ok'):
                err_msg = res.get('error') or '摘要生成失败'
                task['error'] = err_msg
                self.store._exec('UPDATE request_log SET ok=?, status=?, finish_reason=?, error=? WHERE id=?',
                                 (0, res.get('status', 200), fin_reason, err_msg, req_row))
                with self._lock:
                    if cancel.is_set() or getattr(self, '_shutting_down', False) or getattr(self, '_paused_for_reload', False):
                        return
                    if self._summary_task is not task:
                        return
                    chk_inst = self.get_runtime_instance()
                    if chk_inst.instance_seq != inst.instance_seq:
                        return
                    chk_sess = self.store.get_session(session_id)
                    if chk_sess is None or (chk_sess.get('boundary_seq') or 0) != curr_boundary:
                        return
                    self.store.save_session_summary(session_id, {
                        'status': 'failed',
                        'error': f"{err_msg} (finish_reason={fin_reason}, saw_done={saw_done_val})",
                        'start_seq': start_seq,
                        'end_seq': end_seq,
                        'source_turn_count': len(selected_turns),
                        'recent_turns_kept': task['recent_turns_kept'],
                        'boundary_seq': curr_boundary,
                        'instance_seq': inst.instance_seq,
                        'is_active': False
                    })
                self._publish(session_id, {
                    'kind': 'summary_error',
                    'session_id': session_id,
                    'ok': False,
                    'error': err_msg
                })
                return

            parsed = res['summary_json']
            summary_text = res['summary_text']
            summary_tokens = chatengine.render_tokens(tokenizer, [{'role': 'system', 'content': summary_text}])
            tokens_saved = raw_turns_tokens - summary_tokens

            self.store._exec(
                'UPDATE request_log SET ok=?, status=?, prompt_tokens=?, '
                'completion_tokens=?, elapsed_s=?, finish_reason=?, error=? WHERE id=?',
                (1, 200, res.get('usage', {}).get('prompt_tokens', budget_plan.get('prompt_tokens', raw_turns_tokens)),
                 res.get('usage', {}).get('completion_tokens', summary_tokens),
                 res.get('elapsed_s'), fin_reason or 'stop', None, req_row))

            # Pre-save checks under lock
            with self._lock:
                if cancel.is_set() or getattr(self, '_shutting_down', False) or getattr(self, '_paused_for_reload', False):
                    return
                if self._summary_task is not task:
                    return
                chk_sess = self.store.get_session(session_id)
                if chk_sess is None or (chk_sess.get('boundary_seq') or 0) != curr_boundary:
                    return
                chk_inst = self.get_runtime_instance()
                if chk_inst.instance_seq != inst.instance_seq or chk_inst.status != 'ready':
                    return

                summary_data = {
                    'summary_json': parsed,
                    'summary_text': summary_text,
                    'start_msg_id': budget_plan.get('start_msg_id'),
                    'end_msg_id': budget_plan.get('end_msg_id'),
                    'start_seq': start_seq,
                    'end_seq': end_seq,
                    'source_turn_count': len(selected_turns),
                    'recent_turns_kept': task['recent_turns_kept'],
                    'boundary_seq': curr_boundary,
                    'instance_seq': inst.instance_seq,
                    'input_tokens': res.get('usage', {}).get('prompt_tokens', budget_plan.get('prompt_tokens', raw_turns_tokens)),
                    'output_tokens': res.get('usage', {}).get('completion_tokens', summary_tokens),
                    'tokens_saved': tokens_saved,
                    'status': 'ready',
                    'is_active': False
                }
                self.store.save_session_summary(session_id, summary_data)

            self._publish(session_id, {
                'kind': 'summary_generated',
                'session_id': session_id,
                'ok': True,
                'tokens_saved': tokens_saved,
                'turns_compressed': len(selected_turns)
            })
        except Exception as e:
            import traceback
            traceback.print_exc()
            try:
                self.store.log_request(
                    kind='aux', url=f'{task_id}/summary/crash',
                    session_id=session_id, message_id=None, request_id=task_id,
                    status=None, ok=False, body={'error': f'{type(e).__name__}: {e}'})
            except Exception:
                pass
        finally:
            done.set()
            self._slot_release(task_id)
            with self._lock:
                if self._summary_task and self._summary_task.get('task_id') == task_id:
                    self._summary_task = None

    # -- export -----------------------------------------------------------
    def export(self, session_id: str, fmt: str = 'md') -> dict:
        sess = self.store.get_session(session_id)
        if sess is None:
            return {'ok': False, 'error': 'not_found'}
        msgs = self.store.messages(session_id)
        stamp = time.strftime('%Y%m%dT%H%M%S')
        if fmt == 'json':
            payload = {'session': sess, 'messages': msgs,
                       'exported_at': chatstore.now(),
                       'note': ('status 字段保留生成状态：complete 完整、'
                                'interrupted 被中断、error 出错；'
                                'interrupted/error 的 content 只是已收到的部分。')}
            text = json.dumps(payload, ensure_ascii=False, indent=2)
            name = f'chat-{sess["id"][-8:]}-{stamp}.json'
            ctype = 'application/json; charset=utf-8'
        else:
            lines = [f'# {sess["title"]}', '',
                     f'- 会话：`{sess["id"]}`',
                     f'- 创建：{sess["created_at"]}',
                     f'- 导出：{chatstore.now()}',
                     f'- 消息数：{len(msgs)}', '']
            for m in msgs:
                who = '我' if m['role'] == 'user' else '助手'
                tag = {'complete': '', 'streaming': '（生成中）',
                       'interrupted': '（已中断，以下只是已收到的部分）',
                       'error': '（出错，以下只是已收到的部分）'}.get(m['status'], '')
                lines.append(f'## {who} {tag}'.rstrip())
                lines.append('')
                if m.get('web_search') and m['web_search'].get('results'):
                    lines.append('**参考搜索资料：**')
                    for r in m['web_search']['results']:
                        lines.append(f"- [{r.get('index', 1)}] [{r.get('title')}]({r.get('url')}) ({r.get('domain')}) [搜索摘要]")
                    lines.append('')
                lines.append(m['content'] or '（空）')
                lines.append('')
                lines.append(f'<sub>{m["created_at"]} · {m["status"]}</sub>')
                lines.append('')
            text = '\n'.join(lines)
            name = f'chat-{sess["id"][-8:]}-{stamp}.md'
            ctype = 'text/markdown; charset=utf-8'
        return {'ok': True, 'filename': name, 'content': text, 'ctype': ctype}


_SUBSCRIBERS: list[dict] = []
SERVICE: Optional[ChatService] = None


def service(model_url: Optional[str] = None,
            model_port: Optional[int] = None,
            model_config_path: Optional[Path] = None) -> ChatService:
    global SERVICE
    if SERVICE is None:
        SERVICE = ChatService(model_url=model_url, model_port=model_port,
                              model_config_path=model_config_path)
    return SERVICE


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = 'hermes-lite-chat/1.0'
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, body: bytes, ctype: str = 'application/json',
              extra: Optional[dict] = None) -> None:
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    def _json(self, obj: Any, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False,
                                    default=str).encode('utf-8'))

    def _body(self) -> dict:
        n = int(self.headers.get('Content-Length') or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b'{}')
        except ValueError:
            return {}

    # ---- GET ------------------------------------------------------------
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        p = u.path
        svc = service()

        if p in ('/', '/index.html'):
            return self._static('index.html')
        if p.startswith('/static/'):
            return self._static(p[len('/static/'):])
        if p == '/api/chat/status':
            return self._json(svc.status())
        if p == '/api/chat/model/status':
            return self._json(svc.get_model_status())
        if p == '/api/chat/model/profiles':
            return self._json(svc.model_profiles())
        if p == '/api/chat/persona':
            return self._json(svc.persona_view())
        if p == '/api/chat/model/profile':
            pid = (q.get('id') or q.get('profile_id') or [''])[0]
            if not pid:
                return self._json({'ok': False, 'error': 'missing_profile_id',
                                   'message': '缺少 profile_id。'}, 400)
            return self._json(svc._ctl_profile_op('profile_detail', pid))
        if p == '/api/chat/capabilities':
            sid = (q.get('session_id') or q.get('id') or [''])[0]
            return self._json(svc.capabilities(sid))
        if p == '/api/chat/sessions':
            return self._json({'sessions': svc.store.list_sessions(),
                               'counters': svc.store.counters(),
                               'request_counts': svc.store.request_counts()})
        if p == '/api/chat/session':
            sid = (q.get('id') or [''])[0]
            sess = svc.store.get_session(sid)
            if sess is None:
                return self._json({'ok': False, 'error': 'not_found'}, 404)
            return self._json({'ok': True, 'session': sess,
                               'messages': svc.store.messages(sid),
                               'active': svc.active_summary(),
                               'model_slot': svc.slot_summary(),
                               'summary': svc.store.get_session_summary(sid)})
        if p == '/api/chat/session/summary':
            sid = (q.get('session_id') or q.get('id') or [''])[0]
            if not sid:
                return self._json({'ok': False, 'error': 'missing_session_id'}, 400)
            return self._json(svc.get_session_summary_status(sid))
        if p == '/api/chat/stream':
            return self._sse((q.get('session_id') or [''])[0])
        if p == '/api/chat/memories':
            return self._json({
                'memories': svc.store.live_memories(),
                'events': svc.store.memory_events(limit=60),
                'counters': svc.store.counters(),
                'path': str(svc.store.path)})
        if p == '/api/chat/export':
            sid = (q.get('session_id') or [''])[0]
            fmt = (q.get('format') or ['md'])[0]
            out = svc.export(sid, fmt)
            if not out.get('ok'):
                return self._json(out, 404)
            data = out['content'].encode('utf-8')
            return self._send(200, data, out['ctype'],
                              {'Content-Disposition':
                               f'attachment; filename="{out["filename"]}"'})
        if p == '/api/chat/requests':
            return self._json({'requests': svc.store.requests(limit=80),
                               'counts': svc.store.request_counts()})
        return self._json({'ok': False, 'error': 'not_found'}, 404)

    # ---- POST -----------------------------------------------------------
    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        svc = service()
        body = self._body()
        if u.path == '/api/chat/sessions':
            sess = svc.create_session(body.get('title'))
            return self._json({'ok': True, 'session': sess})
        if u.path == '/api/chat/sessions/rename':
            return self._json(svc.store.rename_session(
                body.get('session_id') or '', body.get('title') or ''))
        if u.path == '/api/chat/sessions/delete':
            return self._json(svc.delete_session(
                body.get('session_id') or ''))
        if u.path == '/api/chat/sessions/settings':
            sid = body.get('session_id') or ''
            settings = body.get('settings') or {}
            client_seq = body.get('client_seq')
            base_seq = body.get('base_seq')
            # Round 41: validate strictly BEFORE storing. The window used for the
            # reply-reserve conflict check is the ACTUAL one the service reports.
            validated = svc.validate_settings_write(sid, settings)
            if not validated.get('ok'):
                return self._json(validated)
            settings = validated['settings']
            return self._json(svc.store.update_session_settings(
                sid, settings, client_seq=client_seq, base_seq=base_seq))
        if u.path == '/api/chat/sessions/settings/reset':
            sid = body.get('session_id') or ''
            client_seq = body.get('client_seq')
            base_seq = body.get('base_seq')
            return self._json(svc.store.reset_session_settings(sid, client_seq=client_seq, base_seq=base_seq))
        if u.path == '/api/chat/sessions/restart_boundary':
            sid = body.get('session_id') or ''
            with svc._lock:
                if svc._busy(sid) is not None:
                    return self._json({'ok': False, 'error': 'busy',
                                       'message': '生成进行中，请先停止生成再设置分界线。'})
            return self._json(svc.store.set_session_boundary(sid, body.get('boundary_msg_id')))
        if u.path == '/api/chat/sessions/clear_boundary':
            sid = body.get('session_id') or ''
            with svc._lock:
                if svc._busy(sid) is not None:
                    return self._json({'ok': False, 'error': 'busy',
                                       'message': '生成进行中，请先停止生成再清除分界线。'})
            return self._json(svc.store.clear_session_boundary(sid))
        if u.path == '/api/chat/send':
            try:
                res = svc.send(body.get('session_id') or '',
                               body.get('text') or '',
                               body.get('client_msg_id'),
                               web_search=body.get('web_search'),
                               retry_user_msg_id=body.get('retry_user_msg_id'))
                return self._json(res)
            except Exception as e:
                return self._json({
                    'ok': False,
                    'error': 'worker_start_failed',
                    'message': f'发送失败：{type(e).__name__}: {e}',
                    'session_id': body.get('session_id'),
                })
        if u.path == '/api/chat/stop':
            return self._json(svc.stop(body.get('session_id')))
        if u.path == '/api/chat/session/summary/generate':
            sid = body.get('session_id') or ''
            recent_kept = int(body.get('recent_turns_kept') or 3)
            return self._json(svc.schedule_summary_generation(sid, recent_turns_kept=recent_kept))
        if u.path == '/api/chat/session/summary/stop':
            sid = body.get('session_id')
            return self._json(svc.stop_summary_generation(sid))
        if u.path == '/api/chat/session/summary/active':
            sid = body.get('session_id') or ''
            active_flag = bool(body.get('active'))
            return self._json(svc.set_summary_active(sid, active_flag))
        if u.path == '/api/chat/session/summary/delete':
            sid = body.get('session_id') or ''
            return self._json(svc.delete_session_summary(sid))
        if u.path == '/api/chat/model/reload':
            return self._json(svc.reload_model(body.get('target_window')))
        # ---- Round 50: persona file -------------------------------------- #
        if u.path == '/api/chat/persona/save':
            raw = body.get('body')
            if raw is None:
                return self._json({'ok': False, 'error': 'missing_body',
                                   'message': '缺少人格正文（body）。'}, 400)
            enabled = body.get('enabled')
            return self._json(svc.persona_save(
                raw, bool(enabled) if enabled is not None else None))
        if u.path == '/api/chat/persona/enabled':
            if 'enabled' not in body:
                return self._json({'ok': False, 'error': 'missing_enabled',
                                   'message': '缺少 enabled 字段。'}, 400)
            return self._json(svc.persona_set_enabled(bool(body.get('enabled'))))
        # ---- Round 43: model profile management -------------------------- #
        if u.path == '/api/chat/model/profile/validate':
            return self._json(svc._ctl_profile_op('validate_profile_draft',
                                                  body.get('profile') or body))
        if u.path == '/api/chat/model/profile/create':
            return self._json(svc._ctl_profile_op('create_profile',
                                                  body.get('profile') or body))
        if u.path == '/api/chat/model/profile/update':
            pid = body.get('profile_id') or ''
            if not pid:
                return self._json({'ok': False, 'error': 'missing_profile_id',
                                   'message': '缺少 profile_id。'}, 400)
            return self._json(svc._ctl_profile_op(
                'update_profile', pid, body.get('profile') or body.get('fields') or {}))
        if u.path == '/api/chat/model/profile/delete':
            return self._json(svc._ctl_profile_op(
                'delete_profile', body.get('profile_id') or '',
                body.get('new_default_profile_id')))
        if u.path == '/api/chat/model/profile/default':
            return self._json(svc._ctl_profile_op(
                'set_default_profile', body.get('profile_id') or ''))
        if u.path == '/api/chat/model/profile/switch':
            return self._json(svc.switch_model_profile(body.get('profile_id') or ''))
        if u.path == '/api/chat/memories/confirm':
            return self._json(svc.store.confirm_memory(body.get('id') or ''))
        if u.path == '/api/chat/memories/update':
            return self._json(svc.store.set_memory(
                body.get('id') or '', content=body.get('content'),
                enabled=body.get('enabled'), confirm=bool(body.get('confirm'))))
        if u.path == '/api/chat/memories/delete':
            return self._json(svc.store.delete_memory(body.get('id') or ''))
        if u.path == '/api/chat/shutdown':
            drain_res = svc.cancel_and_drain(timeout_s=2.0)
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return self._json({'ok': True, 'message': 'shutdown initiated', 'drain': drain_res})
        return self._json({'ok': False, 'error': 'not_found'}, 404)

    # ---- static ---------------------------------------------------------
    def _static(self, name: str) -> None:
        target = (CHAT_STATIC / name).resolve()
        try:
            inside = os.path.commonpath([str(CHAT_STATIC.resolve()),
                                         str(target)]) == str(CHAT_STATIC.resolve())
        except ValueError:
            inside = False
        if not inside or not target.is_file():
            return self._json({'ok': False, 'error': 'not_found'}, 404)
        ctype = mimetypes.guess_type(str(target))[0] or 'application/octet-stream'
        if ctype.startswith('text/') or ctype == 'application/javascript':
            ctype += '; charset=utf-8'
        self._send(200, target.read_bytes(), ctype)

    # ---- SSE ------------------------------------------------------------
    def _sse(self, session_id: str) -> None:
        svc = service()
        sub = svc.subscribe(session_id)
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Accel-Buffering', 'no')
        self.end_headers()
        try:
            # Tell the client what is already in flight, so a refresh mid-reply
            # repaints the partial text instead of showing nothing.
            act = svc.active_summary()
            hello = {'kind': 'hello', 'active': act,
                     'server_time': chatstore.now()}
            self.wfile.write(f"event: hello\ndata: {json.dumps(hello, ensure_ascii=False)}\n\n"
                             .encode('utf-8'))
            self.wfile.flush()
            idle = 0.0
            while True:
                with sub['cond']:
                    if not sub['queue']:
                        sub['cond'].wait(0.4)
                    batch = sub['queue'][:]
                    sub['queue'].clear()
                for ev in batch:
                    payload = json.dumps(ev, ensure_ascii=False, default=str)
                    self.wfile.write(
                        f"event: {ev.get('kind')}\ndata: {payload}\n\n".encode('utf-8'))
                    self.wfile.flush()
                if not batch:
                    idle += 0.4
                    if idle >= 20:
                        self.wfile.write(b': keepalive\n\n')
                        self.wfile.flush()
                        idle = 0.0
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass
        finally:
            svc.unsubscribe(sub)
            try:
                self.close_connection = True
            except Exception:  # noqa: BLE001
                pass


def serve(host: str = '127.0.0.1', port: int = 8771,
          open_browser: bool = False,
          model_url: Optional[str] = None,
          model_port: Optional[int] = None,
          model_config_path: Optional[Path] = None) -> int:
    import sys
    import webbrowser
    if host not in ('127.0.0.1', 'localhost', '::1'):
        print(f'chat: refusing to bind {host!r}; loopback only.', file=sys.stderr)
        return 2
    if not (CHAT_STATIC / 'index.html').is_file():
        print(f'chat: static assets missing at {CHAT_STATIC}', file=sys.stderr)
        return 2
    svc = service(model_url=model_url, model_port=model_port,
                  model_config_path=model_config_path)
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    url = f'http://{host}:{port}/'
    print(f'hermes-lite 聊天页面：{url}')
    print(f'  数据目录：{svc.store.path}')
    if svc.store.recovered.get('interrupted_on_start'):
        print(f'  启动时把 {svc.store.recovered["interrupted_on_start"]} 条'
              '上次未完成的回复标记为「已中断」（未自动重发）。')
    print('  只监听回环地址；不加载任何外部资源。Ctrl+C 停止。')
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\n已停止。')
    finally:
        httpd.server_close()
        try:
            svc.store.close()
        except Exception:  # noqa: BLE001
            pass
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    """Standalone entry: `python -m hermes_lite.chatweb`.

    Deliberately NOT routed through `cli.py`, whose module scope imports the
    Agent core. The chat product must not need the Agent core to exist at all.
    """
    import argparse
    ap = argparse.ArgumentParser(prog='hermes_lite.chatweb',
                                 description='local chat page (loopback only)')
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=8771)
    ap.add_argument('--model-url', type=str, default=None, help='Model service base URL')
    ap.add_argument('--model-port', type=int, default=None, help='Model service port')
    ap.add_argument('--model-config', type=str, default=None, help='Model deployment config path')
    ap.add_argument('--open-browser', action='store_true')
    a = ap.parse_args(argv)
    m_cfg = Path(a.model_config).resolve() if a.model_config else None
    return serve(host=a.host, port=a.port, open_browser=a.open_browser,
                 model_url=a.model_url, model_port=a.model_port,
                 model_config_path=m_cfg)


if __name__ == '__main__':
    import sys
    for attr in ('stdout', 'stderr'):
        st = getattr(sys, attr)
        if hasattr(st, 'reconfigure'):
            try:
                st.reconfigure(encoding='utf-8', errors='replace')
            except Exception:  # noqa: BLE001
                pass
    raise SystemExit(main())
