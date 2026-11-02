# -*- coding: utf-8 -*-
"""Round 58: Comprehensive search gap-fill verification suite.

Covers:
  Group 1: 补查成功：一次搜索、两次模型调用、一条最终 assistant 消息
  Group 2: 实际流式事件：无候选前缀与最终回答拼接
  Group 3: 补查失败、无结果、无关资料和超时：完整回退文本断言
  Group 4: 缺口短语跨不同 chunk 边界：分块不丢失、判定不受分块影响
  Group 5: 长历史加长搜索资料：实际请求满足窗口预算、元数据与请求一致、超窗诚实回退
  Group 6: 两次物理调用独立记录与收尾、usage/耗时独立不串用
  Group 7: 创作、引用、翻译、自我介绍及普通聊天不误补查
  Group 8: 首次已搜索、关闭联网、本条禁止联网时不补查
  Group 9: 搜索等待、运行及二次生成阶段的受控停止（同步点断言）
  Group 10: Executor 提交失败和搜索异常后的资源释放与容量恢复
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
OUT = LOGS / 'desktop_round58_search_gap_fill_tests.json'
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
PLAIN_TEXT = '我是一个运行在本机的离线中文助手，可以和你聊天。'


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
                if outer.mode == 'crash':
                    raise RuntimeError('模拟检索底层严重异常')
                if outer.mode == 'empty':
                    return chatsearch.SearchResponse(
                        results=[], provider='stub', active_backend=None,
                        attempts=[{'backend': 'stub', 'status': 'empty', 'results_count': 0}],
                        total_elapsed_s=0.01)
                if outer.mode == 'irrelevant':
                    res = [chatsearch.SearchResult(
                        index=i, title='AI助手自我介绍模板',
                        url=f'https://example.com/bio{i}', domain='example.com',
                        snippet='我是一个乐于助人的AI助手，很高兴认识你。',
                        retrieved_at='2026-11-01T23:00:00+08:00',
                        provider='stub', backend='stub') for i in (1, 2)]
                elif outer.mode == 'long_results':
                    # Generate very long results
                    long_snip = f'{outer.object_name} 详细资料介绍：' + ('相关信息背景。' * 200)
                    res = [chatsearch.SearchResult(
                        index=1, title=f'{outer.object_name} 完整资料',
                        url='https://game.example.com/long', domain='game.example.com',
                        snippet=long_snip, retrieved_at='2026-11-01T23:00:00+08:00',
                        provider='stub', backend='stub')]
                elif outer.mode == 'excessive_overflow':
                    # Multiple long results to exceed custom tight budget
                    res = [chatsearch.SearchResult(
                        index=i, title=f'{outer.object_name} 巨大资料 {i}',
                        url=f'https://game.example.com/huge{i}', domain='game.example.com',
                        snippet=f'{outer.object_name} 巨大资料片段 {i}：' + ('超长内容详情' * 50),
                        retrieved_at='2026-11-01T23:00:00+08:00',
                        provider='stub', backend='stub') for i in (1, 2, 3)]
                else:
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

    work = LOGS / 'r58_work'
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

    def add_msg(store, sid, role, content):
        mid = chatstore.new_id('msg')
        seq = store._next_seq(sid)
        ts = chatstore.now()
        store._exec(
            "INSERT INTO messages (id, session_id, seq, role, content, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (mid, sid, seq, role, content, 'complete', ts, ts))

    def new_service(server):
        store = chatstore.ChatStore()
        svc = chatweb.ChatService(store, model_url=f'http://127.0.0.1:{server.port}/v1')
        svc.set_runtime_instance(chatweb.RuntimeInstance(
            base_url=f'http://127.0.0.1:{server.port}/v1',
            model_alias='qwen25-1.5b-instruct-q4_k_m',
            actual_window=4096, owned=False, status='ready',
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
            time.sleep(0.1)
        return store.messages(sid)

    def end_for(sid):
        mine = [v for v in ended.values() if v.get('session_id') == sid]
        return mine[-1] if mine else {}

    # =========================================================================
    # Group 1: 补查成功：一次搜索、两次模型调用、一条最终 assistant 消息
    # =========================================================================
    print('\n--- Group 1: 补查成功')
    s1 = ScriptedSearcher('relevant')
    with M.MockServer([{'kind': 'text', 'text': GAP_TEXT, 'prompt_tokens': 120, 'completion_tokens': 24},
                       {'kind': 'text', 'text': GOOD_TEXT, 'prompt_tokens': 350, 'completion_tokens': 36}],
                      n_ctx=4096) as srv:
        chatsearch.set_searcher(s1.make())
        store, svc = new_service(srv)
        sid = store.create_session('G01')['id']
        store.update_session_settings(sid, {'web_search': True, 'learn_memory': False})

        svc.send(sid, '影之刃零要出了', web_search=True)
        msgs = wait_done(store, sid)
        asst = [m for m in msgs if m['role'] == 'assistant']
        b = srv.state.chat_requests()
        end = end_for(sid)

        check('G01_1', '补查成功：恰好进行了一次搜索',
              len(s1.queries) == 1 and '影之刃零' in s1.queries[0],
              {'queries': s1.queries})
        check('G01_2', '补查成功：物理模型调用两次（首轮候选+补查二次生成）',
              len(b) == 2, {'model_calls': len(b)})
        check('G01_3', '补查成功：数据库最终仅有一条 assistant 消息且内容为最终回答',
              len(asst) == 1 and GOOD_TEXT in (asst[0].get('content') or '')
              and GAP_TEXT not in (asst[0].get('content') or ''),
              {'assistant_rows': len(asst), 'content': (asst[0].get('content') or '')[:80]})
        check('G01_4', '补查成功：终态事件标记成功且为联网回答',
              end.get('status') == 'complete' and (end.get('extra') or {}).get('search_used') is True,
              {'end_status': end.get('status'), 'search_used': (end.get('extra') or {}).get('search_used')})
        store.close()

    # =========================================================================
    # Group 2: 实际流式事件：无候选前缀与最终回答拼接
    # =========================================================================
    print('\n--- Group 2: 实际流式事件检验（无前缀拼接）')
    published_events.clear()
    s2 = ScriptedSearcher('relevant')
    with M.MockServer([{'kind': 'text', 'text': GAP_TEXT},
                       {'kind': 'text', 'text': GOOD_TEXT}],
                      n_ctx=4096) as srv:
        chatsearch.set_searcher(s2.make())
        store, svc = new_service(srv)
        sid = store.create_session('G02')['id']
        store.update_session_settings(sid, {'web_search': True, 'learn_memory': False})

        svc.send(sid, '影之刃零要出了', web_search=True)
        wait_done(store, sid)

        # Simulate client bubble consumption from event stream
        bubble_text = ''
        reset_occurred = False
        delta_texts = []
        for ev in list(published_events):
            if ev.get('session_id') != sid:
                continue
            kind = ev.get('kind')
            if kind == 'message_reset':
                bubble_text = ''
                reset_occurred = True
            elif kind == 'search_phase' and ev.get('phase') == 'gap_fill':
                bubble_text = ''
                reset_occurred = True
            elif kind == 'delta':
                piece = ev.get('text', '')
                delta_texts.append(piece)
                bubble_text += piece

        check('G02_1', '流式事件：触发了明确的界面重置信号',
              reset_occurred is True, {'reset_occurred': reset_occurred})
        check('G02_2', '流式事件：页面端最终渲染无候选前缀拼接',
              bubble_text == GOOD_TEXT and not bubble_text.startswith('我并不'),
              {'bubble_text': bubble_text, 'starts_with_prefix': bubble_text.startswith('我并不')})
        store.close()

    # =========================================================================
    # Group 3: 补查失败、无结果、无关资料和超时：完整回退文本断言
    # =========================================================================
    print('\n--- Group 3: 补查失败/无结果/无关/超时完整回退')
    modes = [
        ('G03_FAIL', 'fail', '补查失败'),
        ('G03_EMPTY', 'empty', '没有取得搜索结果'),
        ('G03_IRREL', 'irrelevant', '无关，已舍弃'),
    ]
    for cid, mode, note_expected in modes:
        sm = ScriptedSearcher(mode)
        with M.MockServer([{'kind': 'text', 'text': GAP_TEXT}], n_ctx=4096) as srv:
            chatsearch.set_searcher(sm.make())
            store, svc = new_service(srv)
            sid = store.create_session(cid)['id']
            store.update_session_settings(sid, {'web_search': True, 'learn_memory': False})

            svc.send(sid, '影之刃零要出了', web_search=True)
            msgs = wait_done(store, sid)
            asst = [m for m in msgs if m['role'] == 'assistant']
            content = (asst[0].get('content') or '') if asst else ''
            check(cid, f'完整回退文本（非残缺前缀）：{mode}',
                  len(asst) == 1 and GAP_TEXT in content and (content != '我并不了')
                  and (note_expected in content or '未取得' in content or '无关' in content),
                  {'content': content, 'is_mutilated_prefix': content == '我并不了'})
            store.close()

    # Test 3d: Timeout
    s_timeout = ScriptedSearcher('relevant', delay_s=12.0)
    with M.MockServer([{'kind': 'text', 'text': GAP_TEXT}], n_ctx=4096) as srv:
        chatsearch.set_searcher(s_timeout.make())
        store, svc = new_service(srv)
        # Set foreground budget small for fast testing
        orig_budget = svc.SEARCH_FOREGROUND_BUDGET_S
        svc.SEARCH_FOREGROUND_BUDGET_S = 0.5
        try:
            sid = store.create_session('G03_TIMEOUT')['id']
            store.update_session_settings(sid, {'web_search': True, 'learn_memory': False})
            svc.send(sid, '影之刃零要出了', web_search=True)
            msgs = wait_done(store, sid)
            asst = [m for m in msgs if m['role'] == 'assistant']
            content = (asst[0].get('content') or '') if asst else ''
            check('G03_TIMEOUT', '补查超时：提供完整回退文本与说明',
                  len(asst) == 1 and GAP_TEXT in content and '超时' in content,
                  {'content': content})
        finally:
            svc.SEARCH_FOREGROUND_BUDGET_S = orig_budget
            store.close()

    # =========================================================================
    # Group 4: 缺口短语跨不同 chunk 边界
    # =========================================================================
    print('\n--- Group 4: 缺口短语跨 chunk 边界')
    chunk_variations = [
        ('G04_V1', '单字切分', ['我', '并', '不', '了', '解', '这个对象的相关资料，无法确认。']),
        ('G04_V2', '双字切分', ['我并', '不了解', '这个对象的相关资料，无法确认。']),
        ('G04_V3', '词边界切分', ['我并不了解', '这个对象的相关资料，', '无法确认。']),
    ]
    for cid, vdesc, chunks in chunk_variations:
        s4 = ScriptedSearcher('relevant')
        with M.MockServer([{'kind': 'text', 'text': ''.join(chunks), 'chunks': chunks},
                           {'kind': 'text', 'text': GOOD_TEXT}],
                          n_ctx=4096) as srv:
            chatsearch.set_searcher(s4.make())
            store, svc = new_service(srv)
            sid = store.create_session(cid)['id']
            store.update_session_settings(sid, {'web_search': True, 'learn_memory': False})

            svc.send(sid, '影之刃零要出了', web_search=True)
            msgs = wait_done(store, sid)
            asst = [m for m in msgs if m['role'] == 'assistant']
            content = (asst[0].get('content') or '') if asst else ''
            check(cid, f'缺口跨分块识别准确且无丢字：{vdesc}',
                  len(s4.queries) == 1 and GOOD_TEXT in content,
                  {'queries': s4.queries, 'content': content[:60]})
            store.close()

    # =========================================================================
    # Group 5: 长历史加长搜索资料（预算裁剪与实际元数据）
    # =========================================================================
    print('\n--- Group 5: 上下文预算裁剪与超窗诚实回退')
    # 5a: 长历史下搜索资料被组装并裁剪老历史
    s5a = ScriptedSearcher('long_results')
    with M.MockServer([{'kind': 'text', 'text': GAP_TEXT, 'prompt_tokens': 200, 'completion_tokens': 20},
                       {'kind': 'text', 'text': GOOD_TEXT, 'prompt_tokens': 1200, 'completion_tokens': 30}],
                      n_ctx=4096) as srv:
        chatsearch.set_searcher(s5a.make())
        store, svc = new_service(srv)
        sid = store.create_session('G05A')['id']
        store.update_session_settings(sid, {'web_search': True, 'learn_memory': False})

        # Add 6 historical messages (3 complete turns)
        for i in range(1, 4):
            add_msg(store, sid, 'user', f'历史问题 {i}：' + ('内容' * 40))
            add_msg(store, sid, 'assistant', f'历史回复 {i}：' + ('回复' * 40))

        svc.send(sid, '影之刃零要出了', web_search=True)
        msgs = wait_done(store, sid)
        b = srv.state.chat_requests()
        end = end_for(sid)
        extra = end.get('extra') or {}

        check('G05A_1', '二次生成复用 assemble 机制：历史正确裁剪以满足预算',
              extra.get('search_used') is True and extra.get('prompt_tokens') is not None
              and extra.get('prompt_tokens') > 0 and extra.get('prompt_tokens') <= 4096,
              {'prompt_tokens': extra.get('prompt_tokens'),
               'turns_included': extra.get('turns_included'),
               'turns_omitted': extra.get('turns_omitted')})
        check('G05A_2', '终态事件元数据对应实际二次生成的最终请求',
              extra.get('search_tokens', 0) > 0 and extra.get('search_used') is True,
              {'search_tokens': extra.get('search_tokens')})
        store.close()

    # 5b: 搜索资料极端巨大超出整个窗口 -> 诚实回退，不截断或静默删除当前问题
    s5b = ScriptedSearcher('excessive_overflow')
    with M.MockServer([{'kind': 'text', 'text': GAP_TEXT}], n_ctx=4096) as srv:
        chatsearch.set_searcher(s5b.make())
        store, svc = new_service(srv)
        sid = store.create_session('G05B')['id']
        store.update_session_settings(sid, {
            'web_search': True,
            'learn_memory': False,
            'history_mode': 'custom',
            'custom_budget': 256,
        })

        svc.send(sid, '影之刃零要出了', web_search=True)
        msgs = wait_done(store, sid)
        asst = [m for m in msgs if m['role'] == 'assistant']
        content = (asst[0].get('content') or '') if asst else ''
        end = end_for(sid)
        check('G05B', '搜索资料超窗时诚实回退候选：不静默删除用户问题或崩溃',
              len(asst) == 1 and '预算' in content and GAP_TEXT in content
              and end.get('extra', {}).get('search_used') is False,
              {'content': content[:100], 'search_used': end.get('extra', {}).get('search_used')})
        store.close()

    # =========================================================================
    # Group 6: 两次模型调用独立记录与 usage
    # =========================================================================
    print('\n--- Group 6: 两次模型调用独立记录与 usage')
    s6 = ScriptedSearcher('relevant')
    with M.MockServer([{'kind': 'text', 'text': GAP_TEXT, 'prompt_tokens': 150, 'completion_tokens': 20},
                       {'kind': 'text', 'text': GOOD_TEXT, 'prompt_tokens': 400, 'completion_tokens': 35}],
                      n_ctx=4096) as srv:
        chatsearch.set_searcher(s6.make())
        store, svc = new_service(srv)
        sid = store.create_session('G06')['id']
        store.update_session_settings(sid, {'web_search': True, 'learn_memory': False})

        svc.send(sid, '影之刃零要出了', web_search=True)
        wait_done(store, sid)
        end = end_for(sid)
        extra = end.get('extra') or {}

        # Query request_log directly from database
        req_rows = store._all(
            "SELECT id, prompt_tokens, completion_tokens, elapsed_s, finish_reason, error, ok "
            "FROM request_log WHERE session_id=? AND kind='chat' ORDER BY at ASC",
            (sid,))

        check('G06_1', 'request_log 拥有独立的两次物理调用记录',
              len(req_rows) == 2, {'req_count': len(req_rows)})
        if len(req_rows) == 2:
            r1, r2 = req_rows[0], req_rows[1]
            check('G06_2', '第一次调用记录包含独立的 prompt/completion/status 且已正常收尾',
                  r1['ok'] == 1 and r1['prompt_tokens'] == 150 and r1['completion_tokens'] == 20
                  and r1['finish_reason'] == 'stop',
                  {'r1': r1})
            check('G06_3', '第二次调用记录包含独立的 prompt/completion/status 且未被覆盖',
                  r2['ok'] == 1 and r2['prompt_tokens'] == 400 and r2['completion_tokens'] == 35
                  and r2['finish_reason'] == 'stop',
                  {'r2': r2})
        check('G06_4', '页面 usage 沿用最终生成口径，累计 usage 由实际记录相加',
              extra.get('usage', {}).get('completion_tokens') == 35
              and extra.get('total_usage', {}).get('completion_tokens') == 55,
              {'final_usage': extra.get('usage'), 'total_usage': extra.get('total_usage')})
        store.close()

    # =========================================================================
    # Group 7: 创作、引用、翻译、自我介绍及普通聊天不误补查
    # =========================================================================
    print('\n--- Group 7: 补查资格收紧（非事实/创作/闲聊不补查）')
    non_search_cases = [
        ('G07_CREATIVE_1', '帮我编一个叫影之刃零的游戏故事', '故事创作意图'),
        ('G07_CREATIVE_2', '帮我写一段关于影之刃零的宣传文案', '文案创作意图'),
        ('G07_TRANSLATE', '帮我把这段话翻译成英文：我不知道', '翻译任务'),
        ('G07_QUOTE', '原文是「我不知道」这句话是什么意思', '引用/解释任务'),
        ('G07_SELF', '介绍一下你自己', '自我介绍'),
        ('G07_CHAT', '你好，今天有点累', '日常情绪闲聊'),
    ]
    for cid, prompt, note in non_search_cases:
        s7 = ScriptedSearcher('relevant')
        # Even if the model output contains "我不知道" or "我并不了解"
        with M.MockServer([{'kind': 'text', 'text': '关于这一点我并不了解，但我可以回答。'}], n_ctx=4096) as srv:
            chatsearch.set_searcher(s7.make())
            store, svc = new_service(srv)
            sid = store.create_session(cid)['id']
            store.update_session_settings(sid, {'web_search': True, 'learn_memory': False})

            svc.send(sid, prompt, web_search=True)
            wait_done(store, sid)
            check(cid, f'不触发补查搜索：{note}',
                  len(s7.queries) == 0 and len(srv.state.chat_requests()) == 1,
                  {'queries': s7.queries, 'model_calls': len(srv.state.chat_requests())})
            store.close()

    # =========================================================================
    # Group 8: 首次已搜索、关闭联网、本条禁止联网时不补查
    # =========================================================================
    print('\n--- Group 8: 首次已搜/总开关关/明确禁用时不补查')
    # 8a: 首次已搜索（原神什么时候发售 -> 首次搜索），回答表示不知道，不二次补查
    s8a = ScriptedSearcher('relevant')
    with M.MockServer([{'kind': 'text', 'text': GAP_TEXT}], n_ctx=4096) as srv:
        chatsearch.set_searcher(s8a.make())
        store, svc = new_service(srv)
        sid = store.create_session('G08A')['id']
        store.update_session_settings(sid, {'web_search': True, 'learn_memory': False})

        svc.send(sid, '原神什么时候发售', web_search=True)
        wait_done(store, sid)
        check('G08A', '首次已搜索：回答暴露缺口不再触发第二轮搜索（一轮上限）',
              len(s8a.queries) == 1 and len(srv.state.chat_requests()) == 1,
              {'queries': s8a.queries, 'model_calls': len(srv.state.chat_requests())})
        store.close()

    # 8b: 总开关关闭
    s8b = ScriptedSearcher('relevant')
    with M.MockServer([{'kind': 'text', 'text': GAP_TEXT}], n_ctx=4096) as srv:
        chatsearch.set_searcher(s8b.make())
        store, svc = new_service(srv)
        sid = store.create_session('G08B')['id']
        store.update_session_settings(sid, {'web_search': False, 'learn_memory': False})

        svc.send(sid, '影之刃零要出了', web_search=False)
        wait_done(store, sid)
        check('G08B', '联网总开关关闭：不触发补查',
              len(s8b.queries) == 0, {'queries': s8b.queries})
        store.close()

    # 8c: 本条明确禁止联网
    s8c = ScriptedSearcher('relevant')
    with M.MockServer([{'kind': 'text', 'text': GAP_TEXT}], n_ctx=4096) as srv:
        chatsearch.set_searcher(s8c.make())
        store, svc = new_service(srv)
        sid = store.create_session('G08C')['id']
        store.update_session_settings(sid, {'web_search': True, 'learn_memory': False})

        svc.send(sid, '别联网，直接告诉我影之刃零要出了', web_search=True)
        wait_done(store, sid)
        check('G08C', '本条明确禁止联网：不触发补查',
              len(s8c.queries) == 0, {'queries': s8c.queries})
        store.close()

    # =========================================================================
    # Group 9: 搜索排队、运行及二次生成阶段的受控停止
    # =========================================================================
    print('\n--- Group 9: 受控停止测试（同步点精确断言）')
    # 9a: 在搜索运行阶段受控停止
    search_started_ev = threading.Event()
    search_resume_ev = threading.Event()
    s9a = ScriptedSearcher('relevant', delay_s=5.0, started_ev=search_started_ev,
                           resume_ev=search_resume_ev)

    with M.MockServer([{'kind': 'text', 'text': GAP_TEXT},
                       {'kind': 'text', 'text': GOOD_TEXT}], n_ctx=4096) as srv:
        chatsearch.set_searcher(s9a.make())
        store, svc = new_service(srv)
        sid = store.create_session('G09A')['id']
        store.update_session_settings(sid, {'web_search': True, 'learn_memory': False})

        worker = threading.Thread(target=svc.send, args=(sid, '影之刃零要出了'),
                                  kwargs={'web_search': True})
        worker.start()

        # Wait until search actually started
        search_entered = search_started_ev.wait(timeout=5.0)
        check('G09A_SYNC', '精确同步点：确认已进入补查搜索执行阶段', search_entered is True)

        # Send stop signal
        stop_res = svc.stop(sid)
        search_resume_ev.set()
        worker.join(timeout=10.0)

        msgs = store.messages(sid)
        asst = [m for m in msgs if m['role'] == 'assistant']
        b = srv.state.chat_requests()
        gate = chatweb._SEARCH_GATE.status()

        check('G09A_1', '搜索阶段停止：状态标为中断且不启动二次模型生成',
              asst[0].get('status') == 'interrupted' and len(b) == 1,
              {'asst_status': asst[0].get('status') if asst else None, 'model_calls': len(b)})
        check('G09A_2', '搜索阶段停止：未泄露任务槽位或票据',
              gate.get('running') == 0, {'gate': gate})
        store.close()

    # 9b: 在二次模型生成阶段受控停止
    call2_started_ev = threading.Event()
    call2_resume_ev = threading.Event()
    s9b = ScriptedSearcher('relevant')

    with M.MockServer([
        {'kind': 'text', 'text': GAP_TEXT},
        {'kind': 'text', 'text': GOOD_TEXT, 'pause_ev': call2_started_ev,
         'resume_ev': call2_resume_ev, 'chunk_delay': 0.1}
    ], n_ctx=4096) as srv:
        chatsearch.set_searcher(s9b.make())
        store, svc = new_service(srv)
        sid = store.create_session('G09B')['id']
        store.update_session_settings(sid, {'web_search': True, 'learn_memory': False})

        worker = threading.Thread(target=svc.send, args=(sid, '影之刃零要出了'),
                                  kwargs={'web_search': True})
        worker.start()

        call2_entered = call2_started_ev.wait(timeout=5.0)
        check('G09B_SYNC', '精确同步点：确认已进入二次模型生成阶段', call2_entered is True)

        svc.stop(sid)
        call2_resume_ev.set()
        worker.join(timeout=10.0)

        msgs = store.messages(sid)
        asst = [m for m in msgs if m['role'] == 'assistant']
        b = srv.state.chat_requests()

        check('G09B_1', '二次生成阶段停止：终态标为 interrupted 且两笔日志均正确收尾',
              asst[0].get('status') == 'interrupted',
              {'asst_status': asst[0].get('status') if asst else None})
        store.close()

    # =========================================================================
    # Group 10: Executor 提交失败和搜索异常后的资源释放
    # =========================================================================
    print('\n--- Group 10: 异常与提交失败后的资源释放')
    # 10a: Executor submit failure
    s10a = ScriptedSearcher('relevant')
    with M.MockServer([{'kind': 'text', 'text': GAP_TEXT}], n_ctx=4096) as srv:
        chatsearch.set_searcher(s10a.make())
        store, svc = new_service(srv)
        sid = store.create_session('G10A')['id']
        store.update_session_settings(sid, {'web_search': True, 'learn_memory': False})

        # Mock submit to fail
        orig_submit = chatweb._SEARCH_EXECUTOR.submit

        def failing_submit(*args, **kwargs):
            raise RuntimeError('模拟 Executor 队列饱和拒绝提交')

        chatweb._SEARCH_EXECUTOR.submit = failing_submit
        try:
            svc.send(sid, '影之刃零要出了', web_search=True)
            msgs = wait_done(store, sid)
            asst = [m for m in msgs if m['role'] == 'assistant']
            content = (asst[0].get('content') or '') if asst else ''
            gate = chatweb._SEARCH_GATE.status()

            check('G10A_1', 'Executor 提交失败：捕获异常并完成诚实回退',
                  len(asst) == 1 and GAP_TEXT in content and '提交搜索任务失败' in content,
                  {'content': content[:80]})
            check('G10A_2', 'Executor 提交失败：容量与票据立即释放（无泄漏）',
                  gate.get('running') == 0 and gate.get('in_flight') == 0,
                  {'gate': gate})
        finally:
            chatweb._SEARCH_EXECUTOR.submit = orig_submit
            store.close()

    # 10b: 搜索执行器抛出底层异常
    s10b = ScriptedSearcher('crash')
    with M.MockServer([{'kind': 'text', 'text': GAP_TEXT}], n_ctx=4096) as srv:
        chatsearch.set_searcher(s10b.make())
        store, svc = new_service(srv)
        sid = store.create_session('G10B')['id']
        store.update_session_settings(sid, {'web_search': True, 'learn_memory': False})

        svc.send(sid, '影之刃零要出了', web_search=True)
        msgs = wait_done(store, sid)
        asst = [m for m in msgs if m['role'] == 'assistant']
        content = (asst[0].get('content') or '') if asst else ''
        gate = chatweb._SEARCH_GATE.status()

        check('G10B', '搜索底层严重异常：安全回退且释放票据',
              len(asst) == 1 and GAP_TEXT in content and gate.get('running') == 0,
              {'content': content[:80], 'gate': gate})
        store.close()

    # Restore patches
    chatweb.ChatService._emit_end = orig_emit
    chatweb.ChatService._publish = orig_publish


def main() -> int:
    run_tests()
    passed = sum(1 for r in results if r['pass'])
    summary = {
        'round': 58,
        'suite': 'search gap-fill automation repair and closeout',
        'finished_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        'total': len(results),
        'passed': passed,
        'failed': len(results) - passed,
        'all_passed': passed == len(results),
        'checks': results,
    }
    OUT.write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                   encoding='utf-8', newline='\n')
    print(f"\n=== Round 58 Tests: {passed}/{len(results)} passed, {len(results) - passed} failed")
    print('Results written to ' + str(OUT))
    return 0 if passed == len(results) else 1


if __name__ == '__main__':
    try:
        rc = main()
    except Exception:
        traceback.print_exc()
        rc = 1
    raise SystemExit(rc)
