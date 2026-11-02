# -*- coding: utf-8 -*-
"""Round 60: Remaining Search Gap-Fill Verification Suite.

Strictly covers the four critical areas required by the brief:
  Group 1: Usage 与日志测试 (双完整、单完整、双缺失、二次中断缺失)
  Group 2: 装配期间停止及回退附近停止 (同步点精确断言、绝无迟到回退或预算超限提示)
  Group 3: 候选部分输出后异常 (零输出错误、部分输出错误、部分输出抛异常、停止与异常交错)
  Group 4: 真正超过预算的长历史 (未裁剪超限、至少删一轮、实际请求满足窗口、当前问题保留、摘要边界不重叠)
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Optional

LITE = Path(__file__).resolve().parents[1]
for p in (str(LITE / 'scripts'), str(LITE)):
    if p not in sys.path:
        sys.path.insert(0, p)

AUDIT_ROUND = os.environ.get('HERMES_AUDIT_ROUND')
if not AUDIT_ROUND:
    raise RuntimeError('HERMES_AUDIT_ROUND must be explicit; refusing to guess '
                       'an audit directory and overwrite old evidence.')
AUDIT_DIR = LITE.parents[1] / 'audit' / AUDIT_ROUND
LOGS = AUDIT_DIR / 'logs'
LOGS.mkdir(parents=True, exist_ok=True)
OUT = LOGS / 'desktop_round60_search_closeout_tests.json'
if OUT.exists() and not os.environ.get('HERMES_OVERWRITE_TEST_LOG'):
    raise FileExistsError(f'Result file already exists: {OUT}')

from desktop_test_db_guard import (  # noqa: E402
    ProductionDatabaseRefused, require_isolated_test_db,
)

results: list[dict] = []


def check(cid: str, title: str, ok: bool, detail: Any = None) -> None:
    results.append({'id': cid, 'title': title, 'pass': bool(ok),
                    'detail': '' if detail is None else str(detail)[:1800]})
    print(f"{'PASS' if ok else 'FAIL'}  {cid}  {title}"
          + (f'   [{str(detail)[:230]}]' if detail is not None else ''))


GAP_TEXT = '我并不了解这个对象的相关资料，无法确认。'
GOOD_TEXT = '据资料，影之刃零由灵游坊开发，预计2026年发售。'


class ScriptedSearcher:
    """Configurable searcher for testing."""

    def __init__(self, mode: str = 'relevant', object_name: str = '影之刃零',
                 delay_s: float = 0.0, started_ev: Optional[threading.Event] = None,
                 resume_ev: Optional[threading.Event] = None):
        self.mode = mode
        self.object_name = object_name
        self.delay_s = delay_s
        self.started_ev = started_ev
        self.resume_ev = resume_ev
        self.queries: list[str] = []

    def make(self):
        from hermes_lite import chatsearch
        outer = self

        class S(chatsearch.WebSearcher):
            def search(self, query, max_results=5, timeout=10.0, should_stop=None):
                outer.queries.append(query)
                if outer.started_ev:
                    outer.started_ev.set()
                if outer.resume_ev:
                    outer.resume_ev.wait(timeout=10.0)
                if outer.delay_s > 0:
                    deadline = time.monotonic() + outer.delay_s
                    while time.monotonic() < deadline:
                        if should_stop and should_stop():
                            return chatsearch.SearchResponse(
                                results=[], provider='stub', active_backend='cancelled',
                                attempts=[], total_elapsed_s=0.01)
                        time.sleep(0.05)
                if outer.mode == 'fail':
                    raise chatsearch.SearchError('模拟检索失败')
                res = [chatsearch.SearchResult(
                    index=1, title=f'{outer.object_name} 官方信息',
                    url='https://game.example.com/p1', domain='game.example.com',
                    snippet=f'{outer.object_name}由灵游坊开发，预计2026年发售。',
                    retrieved_at='2026-11-01T23:00:00+08:00',
                    provider='stub', backend='stub')]
                return chatsearch.SearchResponse(
                    results=res, provider='stub', active_backend='stub',
                    attempts=[{'backend': 'stub', 'status': 'success',
                               'elapsed_s': 0.01, 'results_count': len(res)}],
                    total_elapsed_s=0.01)

        return S()


def run_tests() -> None:
    from hermes_lite import chatsearch, chatengine, chatstore, chatweb, chatsearch_router
    import mock_model_server as M

    work = LOGS / 'r60_work'
    work.mkdir(parents=True, exist_ok=True)
    os.environ['HERMES_LITE_CHAT_DATA'] = str(work)
    try:
        require_isolated_test_db(work / 'chat.sqlite3', target='chatdesktop')
    except ProductionDatabaseRefused as exc:
        print(exc.report(), file=sys.stderr)
        raise SystemExit(9) from None

    ended: dict[str, dict] = {}
    published_events: list[dict] = []
    orig_emit = chatweb.ChatService._emit_end
    orig_publish = chatweb.ChatService._publish

    def capture_emit(self, session_id, request_id, status, error, extra=None):
        ended[request_id] = {'status': status, 'error': error,
                             'session_id': session_id,
                             'extra': dict(extra or {})}
        return orig_emit(self, session_id, request_id, status, error, extra)

    def capture_publish(self, session_id, payload):
        p = dict(payload)
        p['session_id'] = session_id
        published_events.append(p)
        return orig_publish(self, session_id, payload)

    chatweb.ChatService._emit_end = capture_emit
    chatweb.ChatService._publish = capture_publish

    def add_msg(store, sid, role, content, status='complete'):
        mid = chatstore.new_id('msg')
        seq = store._next_seq(sid)
        ts = chatstore.now()
        store._exec(
            "INSERT INTO messages (id, session_id, seq, role, content, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (mid, sid, seq, role, content, status, ts, ts))
        return mid

    def new_service(server, actual_window=4096):
        store = chatstore.ChatStore()
        svc = chatweb.ChatService(store, model_url=f'http://127.0.0.1:{server.port}/v1')
        svc.set_runtime_instance(chatweb.RuntimeInstance(
            base_url=f'http://127.0.0.1:{server.port}/v1',
            model_alias='qwen25-1.5b-instruct-q4_k_m',
            actual_window=actual_window, owned=False, status='ready',
            profile_id='mp-bootstrap-deployment'))
        return store, svc

    def wait_done(store, sid, timeout=25.0):
        began = time.time()
        while time.time() - began < timeout:
            msgs = store.messages(sid)
            asst = [m for m in msgs if m['role'] == 'assistant']
            settled = bool(asst) and asst[-1].get('status') not in ('streaming', None)
            event_in = any(v.get('session_id') == sid for v in ended.values())
            if settled and event_in:
                return msgs
            time.sleep(0.05)
        raise TimeoutError(f'wait_done timed out for session {sid}')

    def end_for(sid):
        for req_id, data in reversed(list(ended.items())):
            if data.get('session_id') == sid:
                return data
        return {}

    # =========================================================================
    # Group 1: Usage 与日志测试 (5 项用例)
    # =========================================================================
    print('\n--- Group 1: Usage 与日志精准统计 (5 Cases) ---')

    # Case 1: 两次 usage 都完整
    scenario_both = [
        {'kind': 'text', 'text': GAP_TEXT, 'prompt_tokens': 150, 'completion_tokens': 20},
        {'kind': 'text', 'text': GOOD_TEXT, 'prompt_tokens': 400, 'completion_tokens': 35},
    ]
    with M.MockServer(scenario_both, n_ctx=4096) as srv:
        chatsearch.set_searcher(ScriptedSearcher('relevant').make())
        store, svc = new_service(srv)
        sid = store.create_session('U01_BOTH')['id']
        store.update_session_settings(sid, {'web_search': True, 'learn_memory': False})
        svc.send(sid, '影之刃零要出了', web_search=True)
        wait_done(store, sid)
        end = end_for(sid)
        extra = end.get('extra') or {}

        req_rows = store._all(
            "SELECT id, prompt_tokens, completion_tokens, elapsed_s, finish_reason, error, ok "
            "FROM request_log WHERE session_id=? AND kind='chat' ORDER BY at ASC",
            (sid,))

        r1 = req_rows[0] if len(req_rows) > 0 else {}
        r2 = req_rows[1] if len(req_rows) > 1 else {}
        u_final = extra.get('usage') or {}
        u_total = extra.get('total_usage') or {}
        u_cand = extra.get('candidate_usage') or {}

        check('U01_1', '两次 usage 都完整：物理调用次数等于 2', extra.get('model_calls') == 2)
        check('U01_2', '两次 usage 都完整：最终调用用量对应第二次 (400/35)',
              u_final.get('prompt_tokens') == 400 and u_final.get('completion_tokens') == 35)
        check('U01_3', '两次 usage 都完整：候选用量对应第一次 (150/20)',
              u_cand.get('prompt_tokens') == 150 and u_cand.get('completion_tokens') == 20)
        check('U01_4', '两次 usage 都完整：累计统计标记完整且总计 550/55',
              u_total.get('is_complete') is True and u_total.get('prompt_tokens') == 550 and u_total.get('completion_tokens') == 55)
        check('U01_5', '两次 usage 都完整：数据库两次日志分别完整记录',
              r1.get('prompt_tokens') == 150 and r1.get('completion_tokens') == 20
              and r2.get('prompt_tokens') == 400 and r2.get('completion_tokens') == 35)
        store.close()

    # Case 2: 仅第一次完整 (第二次缺失 usage)
    scenario_first_only = [
        {'kind': 'text', 'text': GAP_TEXT, 'prompt_tokens': 150, 'completion_tokens': 20},
        {'kind': 'text', 'text': GOOD_TEXT, 'no_usage': True},
    ]
    with M.MockServer(scenario_first_only, n_ctx=4096) as srv:
        chatsearch.set_searcher(ScriptedSearcher('relevant').make())
        store, svc = new_service(srv)
        sid = store.create_session('U02_FIRST_ONLY')['id']
        store.update_session_settings(sid, {'web_search': True, 'learn_memory': False})
        svc.send(sid, '影之刃零要出了', web_search=True)
        wait_done(store, sid)
        end = end_for(sid)
        extra = end.get('extra') or {}

        req_rows = store._all(
            "SELECT id, prompt_tokens, completion_tokens, finish_reason, error, ok "
            "FROM request_log WHERE session_id=? AND kind='chat' ORDER BY at ASC",
            (sid,))
        r1 = req_rows[0] if len(req_rows) > 0 else {}
        r2 = req_rows[1] if len(req_rows) > 1 else {}
        u_final = extra.get('usage')
        u_total = extra.get('total_usage') or {}
        asst_msg = (store._one("SELECT usage_json FROM messages WHERE session_id=? AND role='assistant'", (sid,)) or {})

        check('U02_1', '仅第一次完整：第二次缺失时 final_usage 明确为 None，不冒充第一次',
              u_final is None, {'u_final': u_final})
        check('U02_2', '仅第一次完整：total_usage 标记不完整 (is_complete=False) 且说明缺失调用 [2]',
              u_total.get('is_complete') is False and u_total.get('missing_calls') == [2]
              and u_total.get('prompt_tokens') == 150 and u_total.get('completion_tokens') == 20,
              {'u_total': u_total})
        check('U02_3', '仅第一次完整：消息表 usage_json 为空，不存入错误用量',
              asst_msg.get('usage_json') is None, {'usage_json': asst_msg.get('usage_json')})
        check('U02_4', '仅第一次完整：第二条日志 prompt/completion_tokens 为 None，不以估算充实测',
              r2.get('prompt_tokens') is None and r2.get('completion_tokens') is None,
              {'r2': r2})
        store.close()

    # Case 3: 仅第二次完整 (第一次缺失 usage)
    scenario_second_only = [
        {'kind': 'text', 'text': GAP_TEXT, 'no_usage': True},
        {'kind': 'text', 'text': GOOD_TEXT, 'prompt_tokens': 400, 'completion_tokens': 35},
    ]
    with M.MockServer(scenario_second_only, n_ctx=4096) as srv:
        chatsearch.set_searcher(ScriptedSearcher('relevant').make())
        store, svc = new_service(srv)
        sid = store.create_session('U03_SECOND_ONLY')['id']
        store.update_session_settings(sid, {'web_search': True, 'learn_memory': False})
        svc.send(sid, '影之刃零要出了', web_search=True)
        wait_done(store, sid)
        end = end_for(sid)
        extra = end.get('extra') or {}

        u_final = extra.get('usage') or {}
        u_total = extra.get('total_usage') or {}
        u_cand = extra.get('candidate_usage')

        check('U03_1', '仅第二次完整：final_usage 正确记录第二次真实实测 (400/35)',
              u_final.get('prompt_tokens') == 400 and u_final.get('completion_tokens') == 35)
        check('U03_2', '仅第二次完整：candidate_usage 明确为 None', u_cand is None)
        check('U03_3', '仅第二次完整：total_usage 标记不完整并标注缺失第 1 次调用',
              u_total.get('is_complete') is False and u_total.get('missing_calls') == [1]
              and u_total.get('prompt_tokens') == 400,
              {'u_total': u_total})
        store.close()

    # Case 4: 两次都缺失
    scenario_neither = [
        {'kind': 'text', 'text': GAP_TEXT, 'no_usage': True},
        {'kind': 'text', 'text': GOOD_TEXT, 'no_usage': True},
    ]
    with M.MockServer(scenario_neither, n_ctx=4096) as srv:
        chatsearch.set_searcher(ScriptedSearcher('relevant').make())
        store, svc = new_service(srv)
        sid = store.create_session('U04_NEITHER')['id']
        store.update_session_settings(sid, {'web_search': True, 'learn_memory': False})
        svc.send(sid, '影之刃零要出了', web_search=True)
        wait_done(store, sid)
        end = end_for(sid)
        extra = end.get('extra') or {}

        check('U04', '两次都缺失：final_usage 与 total_usage 均为 None，前端安全兼容',
              extra.get('usage') is None and extra.get('total_usage') is None
              and extra.get('model_calls') == 2,
              {'extra_usage': extra.get('usage'), 'total_usage': extra.get('total_usage')})
        store.close()

    # Case 5: 第二次停止或失败，未返回 usage
    call2_pause = threading.Event()
    call2_resume = threading.Event()
    scenario_call2_stop = [
        {'kind': 'text', 'text': GAP_TEXT, 'prompt_tokens': 150, 'completion_tokens': 20},
        {'kind': 'text', 'text': GOOD_TEXT, 'pause_ev': call2_pause, 'resume_ev': call2_resume,
         'no_usage': True, 'chunk_delay': 0.1},
    ]
    with M.MockServer(scenario_call2_stop, n_ctx=4096) as srv:
        chatsearch.set_searcher(ScriptedSearcher('relevant').make())
        store, svc = new_service(srv)
        sid = store.create_session('U05_STOP_NO_USAGE')['id']
        store.update_session_settings(sid, {'web_search': True, 'learn_memory': False})
        svc.send(sid, '影之刃零要出了', web_search=True)

        call2_pause.wait(timeout=10.0)
        svc.stop(sid)
        call2_resume.set()
        wait_done(store, sid)

        end = end_for(sid)
        extra = end.get('extra') or {}
        req_rows = store._all("SELECT id, ok, finish_reason FROM request_log WHERE session_id=? AND kind='chat' ORDER BY at ASC", (sid,))
        u_total = extra.get('total_usage') or {}

        check('U05_1', '第二次停止未返回 usage：终态为 interrupted',
              end.get('status') == 'interrupted' and extra.get('model_calls') == 2,
              {'end_status': end.get('status'), 'model_calls': extra.get('model_calls')})
        check('U05_2', '第二次停止未返回 usage：final_usage 为 None，total_usage 标记缺失 [2]',
              extra.get('usage') is None and u_total.get('is_complete') is False and u_total.get('missing_calls') == [2],
              {'u_final': extra.get('usage'), 'u_total': u_total})
        check('U05_3', '第二次停止未返回 usage：第二次调用日志正常收尾',
              len(req_rows) == 2 and req_rows[1]['finish_reason'] in ('stopped', 'stop'),
              {'req_rows': req_rows})
        store.close()

    # =========================================================================
    # Group 2: 装配期间停止及回退附近停止 (同步点精确断言)
    # =========================================================================
    print('\n--- Group 2: 装配期间停止及回退附近停止 (2 Cases) ---')

    # Case S01: 准确在二次装配期间停止
    assembly_entered_ev = threading.Event()
    assembly_resume_ev = threading.Event()
    orig_assemble_plan = chatweb.ChatService._assemble_prompt_plan

    def hooked_assemble_plan(self, *a, **kw):
        if kw.get('search_block'):
            assembly_entered_ev.set()
            assembly_resume_ev.wait(timeout=10.0)
        return orig_assemble_plan(self, *a, **kw)

    chatweb.ChatService._assemble_prompt_plan = hooked_assemble_plan

    scenario_gap = [
        {'kind': 'text', 'text': GAP_TEXT, 'prompt_tokens': 150, 'completion_tokens': 20},
        {'kind': 'text', 'text': GOOD_TEXT, 'prompt_tokens': 400, 'completion_tokens': 35},
    ]
    with M.MockServer(scenario_gap, n_ctx=4096) as srv:
        chatsearch.set_searcher(ScriptedSearcher('relevant').make())
        store, svc = new_service(srv)
        sid = store.create_session('S01_ASSEMBLY_STOP')['id']
        store.update_session_settings(sid, {'web_search': True, 'learn_memory': False})

        svc.send(sid, '影之刃零要出了', web_search=True)

        entered = assembly_entered_ev.wait(timeout=10.0)
        # Send stop while thread is frozen in prompt assembly
        svc.stop(sid)
        assembly_resume_ev.set()
        wait_done(store, sid)

        end = end_for(sid)
        extra = end.get('extra') or {}
        asst = [m for m in store.messages(sid) if m['role'] == 'assistant']
        chat_reqs = srv.state.chat_requests()
        gate = chatweb.search_pool_status()['gate']

        check('S01_1', '装配期间停止：精准进入二次装配同步点', entered is True)
        check('S01_2', '装配期间停止：绝不发起第二次模型调用 (model_calls == 1)',
              extra.get('model_calls') == 1 and len(chat_reqs) == 1,
              {'model_calls': extra.get('model_calls'), 'srv_chat_reqs': len(chat_reqs)})
        check('S01_3', '装配期间停止：消息状态为 interrupted，绝不出现预算超限提示或完整候选追加',
              asst[0].get('status') == 'interrupted' and '预算' not in (asst[0].get('content') or '')
              and '需' not in (asst[0].get('content') or '') and asst[0].get('content') == '',
              {'content': asst[0].get('content'), 'status': asst[0].get('status')})
        check('S01_4', '装配期间停止：搜索票据完全释放，槽位干净结束',
              gate.get('running') == 0 and svc._active == {},
              {'gate': gate, 'active': svc._active})
        store.close()

    chatweb.ChatService._assemble_prompt_plan = orig_assemble_plan

    # Stop at the actual buffered commit boundary, after search failure.
    commit_entered = threading.Event()
    commit_resume = threading.Event()
    orig_commit = chatweb.ChatService._commit_buffered_reply
    def paused_commit(self, *args, **kwargs):
        if kwargs.get('fallback_note'):
            commit_entered.set()
            if not commit_resume.wait(10.0):
                raise TimeoutError('commit barrier timed out')
        return orig_commit(self, *args, **kwargs)
    chatweb.ChatService._commit_buffered_reply = paused_commit
    try:
        with M.MockServer([{'kind': 'text', 'text': GAP_TEXT}], n_ctx=4096) as srv:
            searcher = ScriptedSearcher('fail')
            chatsearch.set_searcher(searcher.make())
            store, svc = new_service(srv)
            sid = store.create_session('S02_COMMIT_STOP')['id']
            store.update_session_settings(sid, {'web_search': True, 'learn_memory': False})
            svc.send(sid, '影之刃零要出了', web_search=True)
            entered = commit_entered.wait(10.0)
            stop_result = svc.stop(sid)
            cut = len(published_events)
            commit_resume.set()
            wait_done(store, sid)
            asst = [m for m in store.messages(sid) if m['role'] == 'assistant']
            late = [e for e in published_events[cut:] if e.get('session_id') == sid
                    and e.get('kind') in ('delta', 'message_reset')]
            check('S02_SYNC', 'confirmed stopped at actual fallback commit boundary',
                  entered and stop_result.get('ok') and len(searcher.queries) == 1)
            check('S02', 'stop rejects buffered database write and late publication',
                  asst[0]['status'] == 'interrupted' and asst[0]['content'] == ''
                  and end_for(sid).get('status') == 'interrupted' and not late,
                  {'status': asst[0]['status'], 'content': asst[0]['content'], 'late': late})
            store.close()
    finally:
        commit_resume.set()
        chatweb.ChatService._commit_buffered_reply = orig_commit

    # =========================================================================
    # Group 3: 候选部分输出后异常 (4 项用例)
    # =========================================================================
    print('\n--- Group 3: 候选部分输出后异常 (4 Cases) ---')

    # Case E01: 零输出错误
    with M.MockServer([{'kind': 'fail', 'status': 500, 'body': '{"error": "injected 500"}'}], n_ctx=4096) as srv:
        s_mock = ScriptedSearcher('relevant')
        chatsearch.set_searcher(s_mock.make())
        store, svc = new_service(srv)
        sid = store.create_session('E01_ZERO_ERR')['id']
        store.update_session_settings(sid, {'web_search': True, 'learn_memory': False})
        svc.send(sid, '影之刃零要出了', web_search=True)
        wait_done(store, sid)
        end = end_for(sid)
        asst = [m for m in store.messages(sid) if m['role'] == 'assistant']
        req_rows = store._all("SELECT id, ok, finish_reason, error FROM request_log WHERE session_id=?", (sid,))

        check('E01', '零输出错误：无搜索触发、内容为空、终态为 error、日志正常收尾',
              len(s_mock.queries) == 0 and asst[0].get('content') == ''
              and asst[0].get('status') == 'error' and 'HTTP 500' in (asst[0].get('error') or '')
              and len(req_rows) == 1 and req_rows[0]['ok'] == 0,
              {'asst': asst[0], 'queries': s_mock.queries, 'req_rows': req_rows})
        store.close()

    # Case E02: 部分输出后连接断开/返回错误 (partial_error)
    partial_text = '我正在了解影之刃零，但'
    with M.MockServer([{'kind': 'partial_error', 'text': partial_text, 'after': 2}], n_ctx=4096) as srv:
        s_mock = ScriptedSearcher('relevant')
        chatsearch.set_searcher(s_mock.make())
        store, svc = new_service(srv)
        sid = store.create_session('E02_PARTIAL_ERR')['id']
        store.update_session_settings(sid, {'web_search': True, 'learn_memory': False})
        svc.send(sid, '影之刃零要出了', web_search=True)
        wait_done(store, sid)
        end = end_for(sid)
        asst = [m for m in store.messages(sid) if m['role'] == 'assistant']
        turns = chatengine.complete_turns(store.messages(sid))

        check('E02_1', '部分输出后错误：完整保留已接收的部分内容',
              asst[0].get('content') != '' and asst[0].get('status') == 'error',
              {'content': asst[0].get('content'), 'status': asst[0].get('status')})
        check('E02_2', '部分输出后错误：不继续补查，异常候选不作为独立回复进入历史上下文',
              len(s_mock.queries) == 0 and len(turns) == 0,
              {'queries': s_mock.queries, 'turns': len(turns)})
        store.close()

    # Case E03: 部分输出后抛出未捕获异常
    orig_stream = chatengine.stream_chat

    def crashing_stream(*a, **kw):
        on_delta = kw.get('on_delta')
        if on_delta:
            on_delta('已收到前半段候选文字，')
        raise RuntimeError('模拟模型底层网络异常崩溃')

    chatengine.stream_chat = crashing_stream
    with M.MockServer([{'kind': 'text', 'text': 'ignored'}], n_ctx=4096) as srv:
        s_mock = ScriptedSearcher('relevant')
        chatsearch.set_searcher(s_mock.make())
        store, svc = new_service(srv)
        sid = store.create_session('E03_CRASH')['id']
        store.update_session_settings(sid, {'web_search': True, 'learn_memory': False})
        svc.send(sid, '影之刃零要出了', web_search=True)
        wait_done(store, sid)
        asst = [m for m in store.messages(sid) if m['role'] == 'assistant']
        req_rows = store._all("SELECT id, ok, finish_reason, error FROM request_log WHERE session_id=?", (sid,))

        check('E03', '流式抛出异常：已接收文本保留在消息中，request_log 成功被收尾',
              asst[0].get('content') == '已收到前半段候选文字，' and asst[0].get('status') == 'error'
              and len(req_rows) == 1 and req_rows[0]['ok'] == 0 and 'RuntimeError' in (req_rows[0]['error'] or ''),
              {'asst': asst[0], 'req_rows': req_rows})
        store.close()

    chatengine.stream_chat = orig_stream

    # Case E04: 停止与异常交错发生时的终态优先级
    e04_ctx = {'svc': None, 'sid': None}

    def stop_and_error_stream(*a, **kw):
        on_delta = kw.get('on_delta')
        if on_delta:
            on_delta('部分候选片段')
        # Simulate user stop occurring just as connection throws error
        if e04_ctx['svc'] and e04_ctx['sid']:
            e04_ctx['svc'].stop(e04_ctx['sid'])
        return {'ok': False, 'error': 'ConnectionResetError: 模拟网络中断'}

    chatengine.stream_chat = stop_and_error_stream
    with M.MockServer([{'kind': 'text', 'text': 'ignored'}], n_ctx=4096) as srv:
        store, svc = new_service(srv)
        sid = store.create_session('E04_STOP_ERR_RACE')['id']
        store.update_session_settings(sid, {'web_search': True, 'learn_memory': False})
        e04_ctx['svc'] = svc
        e04_ctx['sid'] = sid

        svc.send(sid, '影之刃零要出了', web_search=True)
        wait_done(store, sid)

        asst = [m for m in store.messages(sid) if m['role'] == 'assistant']
        check('E04', '停止与错误交错：停止优先级高于网络错误，终态正确为 interrupted',
              asst[0].get('status') == 'interrupted' and asst[0].get('content') == '部分候选片段',
              {'asst': asst[0]})
        store.close()

    chatengine.stream_chat = orig_stream

    # =========================================================================
    # Group 4: 真正超过预算的长历史 (真正裁剪、满足预留规则、边界不重复)
    # =========================================================================
    print('\n--- Group 4: 真正超预算长历史裁剪核验 (2 Cases) ---')

    # Case H01: 真正超预算的历史自动裁剪
    # Set actual_window = 2048, max_output = 512 -> model_budget = 1536
    scenario_h01 = [
        {'kind': 'text', 'text': GAP_TEXT, 'prompt_tokens': 150, 'completion_tokens': 20},
        {'kind': 'text', 'text': GOOD_TEXT, 'prompt_tokens': 400, 'completion_tokens': 35},
    ]
    with M.MockServer(scenario_h01, n_ctx=2048) as srv:
        s_mock = ScriptedSearcher('relevant')
        chatsearch.set_searcher(s_mock.make())
        store, svc = new_service(srv, actual_window=2048)
        sid = store.create_session('H01_TRULY_OVER_BUDGET')['id']
        store.update_session_settings(sid, {'web_search': True, 'learn_memory': False})

        # Add 5 large complete turns of history: each turn is ~600 tokens (total ~3000 tokens of history)
        # Search block + user query + system prompt is ~300 tokens.
        # Total un-trimmed tokens = ~3300 tokens > budget (1536 tokens)!
        for i in range(1, 6):
            add_msg(store, sid, 'user', f'用户提问第 {i} 轮：' + ('详细对话历史背景' * 150))
            add_msg(store, sid, 'assistant', f'助手回答第 {i} 轮：' + ('详细回复内容记录' * 150))

        svc.send(sid, '影之刃零要出了', web_search=True)
        wait_done(store, sid)
        end = end_for(sid)
        extra = end.get('extra') or {}

        # Inspect the actual second request that arrived at the mock server
        chat_reqs = srv.state.chat_requests()
        # Call 2 is the second chat completion
        call2_body = chat_reqs[1]['body'] if len(chat_reqs) > 1 else {}
        call2_messages = call2_body.get('messages', [])

        tokenizer = chatengine.Tokenizer(srv.base_url)
        actual_req_tokens = chatengine.render_tokens(tokenizer, call2_messages)

        check('H01_1', '真正超预算：至少裁剪了一轮历史 (turns_omitted >= 1)',
              extra.get('turns_omitted', 0) >= 1, {'turns_omitted': extra.get('turns_omitted'), 'turns_included': extra.get('turns_included')})
        check('H01_2', '真正超预算：实际发出的二次请求满足预算与窗口预留 (tokens <= 1536)',
              actual_req_tokens <= 1536 and (actual_req_tokens + 512) <= 2048,
              {'actual_req_tokens': actual_req_tokens, 'budget': 1536, 'window': 2048})
        check('H01_3', '真正超预算：当前用户问题完好保留在实际发送的请求中',
              any(m.get('role') == 'user' and '影之刃零要出了' in m.get('content', '') for m in call2_messages))
        check('H01_4', '真正超预算：终态元数据与实际请求一致',
              extra.get('prompt_tokens') == actual_req_tokens,
              {'meta_prompt_tokens': extra.get('prompt_tokens'), 'actual_rendered': actual_req_tokens})
        store.close()

    # Case H02: 摘要与历史覆盖边界不重复
    with M.MockServer(scenario_h01, n_ctx=4096) as srv:
        chatsearch.set_searcher(ScriptedSearcher('relevant').make())
        store, svc = new_service(srv, actual_window=4096)
        sid = store.create_session('H02_SUMMARY_BOUNDARY')['id']
        store.update_session_settings(sid, {'web_search': True, 'learn_memory': False})

        # Add 4 messages (seq 1, 2, 3, 4)
        m1 = add_msg(store, sid, 'user', '第一轮用户')
        m2 = add_msg(store, sid, 'assistant', '第一轮助手')
        m3 = add_msg(store, sid, 'user', '第二轮用户')
        m4 = add_msg(store, sid, 'assistant', '第二轮助手')

        # Add active summary covering up to seq 4
        store.save_session_summary(
            sid,
            {
                'status': 'ready',
                'is_active': True,
                'summary_text': '前两轮对话摘要背景',
                'summary_json': {'overview': '前两轮对话摘要背景'},
                'tokens_saved': 120,
                'boundary_seq': 0,
                'start_seq': 1,
                'end_seq': 4,
                'start_msg_id': m1,
                'end_msg_id': m4
            }
        )

        # Add messages after summary boundary (seq 5, 6)
        m5 = add_msg(store, sid, 'user', '第三轮用户')
        m6 = add_msg(store, sid, 'assistant', '第三轮助手')

        svc.send(sid, '影之刃零要出了', web_search=True)
        wait_done(store, sid)

        chat_reqs = srv.state.chat_requests()
        call2_msgs = chat_reqs[1]['body'].get('messages', []) if len(chat_reqs) > 1 else []

        # Ensure m1 and m2 are NOT in call2_msgs as conversation turns (covered by summary)
        user_texts = [m.get('content') for m in call2_msgs if m.get('role') == 'user']
        asst_texts = [m.get('content') for m in call2_msgs if m.get('role') == 'assistant']

        check('H02', '摘要与历史覆盖边界不重复：被摘要覆盖的消息未重复注入历史轮次',
              '第一轮用户' not in user_texts and '第二轮用户' not in user_texts
              and '第三轮用户' in user_texts and any('影之刃零要出了' in t for t in user_texts),
              {'user_texts': user_texts})
        store.close()

    # Early worker failures must always finalize the message and terminal event.
    from unittest.mock import patch
    for cid, attribute in [('I01', '_confirmed_window'), ('I02', '_assemble_prompt_plan')]:
        with M.MockServer([], n_ctx=4096) as srv:
            store, svc = new_service(srv)
            sid = store.create_session(cid)['id']
            store.update_session_settings(sid, {'web_search': False, 'learn_memory': False})
            response = svc.send(sid, '你好', web_search=False)
            # Run directly on a separate streaming row; send preflight remains real.
            wait_done(store, sid)
            aid = add_msg(store, sid, 'assistant', '', status='streaming')
            rid = 'r60-' + cid
            store._exec('UPDATE messages SET request_id=? WHERE id=?', (rid, aid))
            stop_ev = threading.Event()
            svc._active = {'request_id': rid, 'session_id': sid, 'message_id': aid, 'stop': stop_ev}
            with patch.object(svc, attribute, side_effect=RuntimeError('injected-' + cid)):
                svc._worker(sid, 'u', '你好', aid, rid, stop_ev,
                            settings={'web_search': False, 'learn_memory': False},
                            inst=svc.get_runtime_instance(), persona_snapshot={'ok': True, 'body': '', 'enabled': False})
            row = store.get_message(aid)
            check(cid, 'early exception emits terminal error and records zero physical calls',
                  row['status'] == 'error' and ended[rid]['status'] == 'error'
                  and ended[rid]['extra']['model_calls'] == 0 and svc._active == {},
                  {'status': row['status'], 'end': ended[rid]})
            store.close()

    # A later application failure must not rewrite successful physical-call logs.
    for cid, prompt, scenario, fail_after in [
        ('L01', '影之刃零要出了', [{'kind': 'text', 'text': GAP_TEXT, 'prompt_tokens': 150, 'completion_tokens': 20}], 'gap'),
        ('L02', '影之刃零要出了', [{'kind': 'text', 'text': GAP_TEXT, 'prompt_tokens': 150, 'completion_tokens': 20},
                                   {'kind': 'text', 'text': GOOD_TEXT, 'prompt_tokens': 400, 'completion_tokens': 35}], 'finish'),
    ]:
        with M.MockServer(scenario, n_ctx=4096) as srv:
            chatsearch.set_searcher(ScriptedSearcher('relevant').make())
            store, svc = new_service(srv)
            sid = store.create_session(cid)['id']
            store.update_session_settings(sid, {'web_search': True, 'learn_memory': False})
            finalized = []
            original_finalize = store.finalize_request_log
            def counted_finalize(req_id, **kwargs):
                finalized.append(req_id)
                return original_finalize(req_id, **kwargs)
            store.finalize_request_log = counted_finalize
            if fail_after == 'gap':
                svc._gap_fill_search = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('after-first-success'))
            else:
                original_finish = svc._finish
                once = [False]
                def fail_finish_once(*args, **kwargs):
                    if not once[0]:
                        once[0] = True
                        raise RuntimeError('after-second-success')
                    return original_finish(*args, **kwargs)
                svc._finish = fail_finish_once
            svc.send(sid, prompt, web_search=True)
            wait_done(store, sid)
            rows = store._all("SELECT id, ok, prompt_tokens, completion_tokens, elapsed_s, error FROM request_log WHERE session_id=? AND kind='chat' ORDER BY at", (sid,))
            expected = [(150, 20)] + ([(400, 35)] if fail_after == 'finish' else [])
            check(cid, 'successful call metrics survive later error and finalize exactly once',
                  len(rows) == len(expected) and all(
                      row['ok'] == 1 and row['error'] is None
                      and (row['prompt_tokens'], row['completion_tokens']) == pair
                      and row['elapsed_s'] is not None and finalized.count(row['id']) == 1
                      for row, pair in zip(rows, expected)),
                  {'rows': rows, 'finalizations': finalized})
            store.close()

    # =========================================================================
    # Summary & Output
    # =========================================================================
    passed = sum(1 for r in results if r['pass'])
    failed = sum(1 for r in results if not r['pass'])
    print(f'\n=== Round 60 Tests: {passed}/{len(results)} passed, {failed} failed ===')

    payload = {
        'total': len(results),
        'passed': passed,
        'failed': failed,
        'results': results,
    }
    OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding='utf-8')
    print(f'Results written to {OUT}')
    if failed > 0:
        raise SystemExit(1)


if __name__ == '__main__':
    run_tests()
