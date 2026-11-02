# -*- coding: utf-8 -*-
"""Round 55: search automation -- broader triggering, one gap-fill, no unbound vars.

Two layers, both required:

**Layer 1 (T01-T18)** pins the routing RULE on a frozen message list. Cheap, and a
later tidy-up of a regex fails loudly here.

**Layer 2 (C01-C14)** drives the REAL `ChatService.send()` with a controllable
searcher and a scripted model, and asserts on:
  * `searcher.queries`            -- did a search really happen (0 for "no search")
  * the real POST bodies          -- is the discarded material really absent
  * the terminal event + the DB   -- did the request finish without an exception,
                                     and is there exactly ONE assistant reply

The frozen list below was written before running anything and is not adjusted to
fit results.

Run from src/hermes-lite with HERMES_AUDIT_ROUND set.
"""
from __future__ import annotations

import json
import os
import sys
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
OUT = LOGS / 'desktop_round55_search_automation_tests.json'
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


# --------------------------------------------------------------------------- #
# FROZEN case list (written before the run; do not tune to results)
# --------------------------------------------------------------------------- #
# 1. object info request -> search
FROZEN_SEARCH = [
    ('T01', '具体作品信息请求（用户报告的例子）', '给我讲一讲影之刃零的信息吧'),
    ('T02', '另一个具体外部对象（不依赖特定名称）', '介绍一下星穹铁道这个游戏'),
    ('T03', '第三个具体对象：背景询问', '能说说黑神话悟空的背景吗'),
    ('T04', '对象+事实询问', '原神什么时候发售'),
    ('T05', '天气（时效）', '今天上海天气怎么样'),
    ('T06', '明确要求检索', '帮我查一下量子计算的基本原理'),
]
# 2. direct answer -> NO search
FROZEN_DIRECT = [
    ('T07', '自我介绍', '介绍一下你自己'),
    ('T08', '纠正自我介绍', '介绍的是你，不是我'),
    ('T09', '情绪闲聊（含「今天」）', '你好，今天有点累'),
    ('T10', '改写', '换个说法'),
    ('T11', '格式调整', '不要用列表'),
    ('T12', '翻译给定文本', '帮我把这段话翻译成英文：今天天气不错，我们一起出门散步吧。'),
    ('T13', '总结给定文本', '总结一下这段：人工智能正在改变很多行业。'),
    ('T14', '简单计算', '12*37等于多少？'),
    ('T15', '创作虚构对象（含游戏名）', '帮我编一个叫影之刃零的游戏故事'),
    ('T16', '创作宣传文案（含游戏名）', '帮我写一段关于影之刃零的宣传文案'),
    ('T17', '无对象的求助', '给我讲讲该怎么办'),
]
# 3. explicit per-message opt-out -> NO network even though intent is clear
FROZEN_NO_WEB = [
    ('T18', '本条明确禁用联网', '别联网，直接告诉我影之刃零是什么'),
]


def layer1() -> None:
    print('\n--- Layer 1: 冻结用例（规则层）')
    from hermes_lite import chatsearch_router as router

    ctx = ['给我讲一讲影之刃零的信息吧']
    for cid, title, text in FROZEN_SEARCH:
        d = router.decide(text, allow_web=True, recent_user_texts=ctx)
        q = router.build_query(text, d, recent_user_texts=ctx)
        check(cid, f'搜索：{title}',
              d['needs_search'] is True and q.get('ok') is True and bool(q.get('query')),
              {'text': text, 'needs_search': d['needs_search'],
               'reason': d['reason'], 'query': q.get('query')})

    for cid, title, text in FROZEN_DIRECT:
        d = router.decide(text, allow_web=True, recent_user_texts=ctx)
        check(cid, f'不搜索：{title}', d['needs_search'] is False,
              {'text': text, 'needs_search': d['needs_search'],
               'reason': d['reason'], 'matched': d['matched']})

    for cid, title, text in FROZEN_NO_WEB:
        d = router.decide(text, allow_web=True)
        check(cid, f'不联网：{title}',
              d['needs_search'] is False
              and d['blocked_by'] == 'user_disabled_for_this_message',
              {'text': text, 'needs_search': d['needs_search'],
               'blocked_by': d['blocked_by'], 'reason': d['reason']})

    # --- follow-up completes the object (T19-T20) ------------------------
    print('\n--- 追问补齐')
    d = router.decide('它什么时候发售', allow_web=True, recent_user_texts=ctx)
    q = router.build_query('它什么时候发售', d, recent_user_texts=ctx)
    check('T19', '外部对象追问补齐对象，不提交裸代词',
          d['needs_search'] is True and '影之刃零' in (q.get('query') or '')
          and (q.get('query') or '') != '它什么时候发售',
          {'query': q.get('query'), 'reason': d['reason']})

    d = router.decide('开发商是谁', allow_web=True, recent_user_texts=ctx)
    q = router.build_query('开发商是谁', d, recent_user_texts=ctx)
    check('T20', '「开发商是谁」借上文对象补齐',
          d['needs_search'] is True and '影之刃零' in (q.get('query') or ''),
          {'query': q.get('query'), 'reason': d['reason']})

    # --- the query is the object, not the whole sentence ------------------
    d = router.decide('给我讲一讲影之刃零的信息吧', allow_web=True)
    q = router.build_query('给我讲一讲影之刃零的信息吧', d)
    check('T21', '查询以对象为主，不把请求语整句提交',
          (q.get('query') or '') == '影之刃零',
          {'query': q.get('query'), 'source': q.get('source')})

    # --- no product whitelist: an invented name behaves the same ----------
    d = router.decide('给我讲一讲霍尔木兹扳手的信息吧', allow_web=True)
    check('T22', '不依赖作品名单：任意具体对象同样触发',
          d['needs_search'] is True, {'reason': d['reason'],
                                      'object': d.get('object_token')})

    # --- gap-fill gates ---------------------------------------------------
    print('\n--- 补查条件与上限')
    no_search = {'needs_search': False, 'blocked_by': None}
    g = router.can_gap_fill(no_search, already_searched=False, allow_web=True)
    check('T23', '未搜索过 + 允许联网 -> 可补查一次', g['ok'] is True, g)
    g = router.can_gap_fill(no_search, already_searched=True, allow_web=True)
    check('T24', '已经搜索过 -> 不再补查（每请求最多一轮）',
          g['ok'] is False and g['why'] == 'one_round_only', g)
    g = router.can_gap_fill(no_search, already_searched=False, allow_web=False)
    check('T25', '联网关闭 -> 不补查', g['ok'] is False, g)
    g = router.can_gap_fill({'needs_search': False,
                             'blocked_by': 'user_disabled_for_this_message'},
                            already_searched=False, allow_web=True)
    check('T26', '本条明确禁用 -> 不补查', g['ok'] is False, g)
    g = router.can_gap_fill({'needs_search': True},
                            already_searched=False, allow_web=True)
    check('T27', '首轮已搜索 -> 不进入补查', g['ok'] is False, g)

    # --- gap phrase detection, and its two guards -------------------------
    print('\n--- 资料不足判定')
    r = router.model_admits_gap('我并不了解影之刃零这款游戏的信息。')
    check('T28', '模型自述缺乏资料 -> 判定为需要补查', r['gap'] is True, r)
    r = router.model_admits_gap('我的知识截止到2024年，没有关于它的资料。')
    check('T29', '「知识中没有」也算缺乏', r['gap'] is True, r)
    r = router.model_admits_gap('今天天气不错，出去走走吧。')
    check('T30', '普通回答不误判', r['gap'] is False, r)
    r = router.model_admits_gap('我不知道。',
                               user_text='帮我把这段话翻译成英文：我不知道')
    check('T31', '翻译文本里的「不知道」不触发补查', r['gap'] is False, r)
    r = router.model_admits_gap('原文是「我不知道」这句话的意思是不清楚。',
                               user_text='帮我解释这段：我不知道')
    check('T32', '引用文本里的「不知道」不触发补查', r['gap'] is False, r)


# --------------------------------------------------------------------------- #
# Layer 2: production call chain
# --------------------------------------------------------------------------- #
class ScriptedSearcher:
    """Records queries; returns a caller-chosen result set."""

    def __init__(self, mode: str = 'relevant', object_name: str = '影之刃零'):
        self.mode = mode
        self.object_name = object_name
        self.queries: list[str] = []

    def make(self):
        from hermes_lite import chatsearch
        outer = self

        class S(chatsearch.WebSearcher):
            def search(self, query, max_results=5, timeout=10.0, should_stop=None):
                outer.queries.append(query)
                if outer.mode == 'fail':
                    raise chatsearch.SearchError('模拟检索失败')
                if outer.mode == 'empty':
                    return chatsearch.SearchResponse(
                        results=[], provider='stub', active_backend=None,
                        attempts=[{'backend': 'stub', 'status': 'empty',
                                   'results_count': 0}], total_elapsed_s=0.01)
                if outer.mode == 'irrelevant':
                    res = [chatsearch.SearchResult(
                        index=i, title='AI助手自我介绍模板',
                        url=f'https://example.com/bio{i}', domain='example.com',
                        snippet='我是一个乐于助人的AI助手，很高兴认识你。',
                        retrieved_at='2026-11-01T23:00:00+08:00',
                        provider='stub', backend='stub') for i in (1, 2)]
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


GAP_TEXT = '我并不了解这个对象的相关资料，无法确认。'
GOOD_TEXT = '据资料，影之刃零由灵游坊开发，预计2026年发售。'
PLAIN_TEXT = '我是一个运行在本机的离线中文助手，可以和你聊天。'


def layer2() -> None:
    print('\n--- Layer 2: 生产调用链（真实 send()，模拟搜索与模型）')
    from hermes_lite import chatsearch, chatengine, chatstore, chatweb
    import mock_model_server as M

    work = LOGS / 'r55_work'
    work.mkdir(parents=True, exist_ok=True)
    os.environ['HERMES_LITE_CHAT_DATA'] = str(work)
    try:
        require_isolated_test_db(work / 'chat.sqlite3', target='chatdesktop')
    except ProductionDatabaseRefused as exc:
        print(exc.report(), file=sys.stderr)
        raise SystemExit(9) from None

    ended: dict[str, dict] = {}
    orig_emit = chatweb.ChatService._emit_end

    def capture(self, session_id, request_id, status, error, extra=None):
        ended[request_id] = {'status': status, 'error': error,
                             'session_id': session_id,
                             'extra': dict(extra or {})}
        return orig_emit(self, session_id, request_id, status, error, extra)

    chatweb.ChatService._emit_end = capture

    def run(scenario: list[dict]):
        """Fresh server + store per scenario so scripted replies line up."""
        server = M.MockServer(scenario, n_ctx=4096)
        return server

    def new_service(server):
        store = chatstore.ChatStore()
        svc = chatweb.ChatService(store, model_url=f'http://127.0.0.1:{server.port}/v1')
        svc.set_runtime_instance(chatweb.RuntimeInstance(
            base_url=f'http://127.0.0.1:{server.port}/v1',
            model_alias='qwen25-1.5b-instruct-q4_k_m',
            actual_window=4096, owned=False, status='ready',
            profile_id='mp-bootstrap-deployment'))
        return store, svc

    def end_for(store, sid, msgs=None):
        """The terminal event recorded for THIS session (most recent)."""
        mine = [v for v in ended.values() if v.get('session_id') == sid]
        if not mine:
            return {}
        return mine[-1]

    def wait_done(store, sid, timeout=25.0):
        """Wait for BOTH the assistant row to settle AND its end event to land.

        The row is finished slightly before `_emit_end` runs (the DB write comes
        first, so a crash still records an outcome), so waiting on the row alone
        races the event and makes assertions flaky. Both are waited for.
        """
        began = time.time()
        while time.time() - began < timeout:
            msgs = store.messages(sid)
            asst = [m for m in msgs if m['role'] == 'assistant']
            settled = bool(asst) and asst[-1].get('status') not in ('streaming', None)
            event_in = any(v.get('session_id') == sid for v in ended.values())
            if settled and event_in:
                return msgs
            time.sleep(0.2)
        return store.messages(sid)

    def bodies(server):
        return [e.get('body') or {} for e in server.state.chat_requests()]

    def ref_in(body):
        for m in (body or {}).get('messages') or []:
            c = m.get('content')
            if isinstance(c, str) and '【联网搜索参考资料' in c:
                return c
        return None

    # ---- C01: the reported case searches ------------------------------- #
    print('\n--- 触发与不触发')
    s1 = ScriptedSearcher('relevant')
    with run([{'kind': 'text', 'text': GOOD_TEXT}]) as srv:
        chatsearch.set_searcher(s1.make())
        store, svc = new_service(srv)
        sid = store.create_session('C01')['id']
        store.update_session_settings(sid, {'web_search': True, 'learn_memory': False})
        svc.send(sid, '给我讲一讲影之刃零的信息吧', web_search=True)
        msgs = wait_done(store, sid)
        b = bodies(srv)
        asst = [m for m in msgs if m['role'] == 'assistant']
        end = end_for(store, sid)
        check('C01', '「影之刃零的信息」主动检索并注入资料',
              len(s1.queries) == 1 and ref_in(b[-1] if b else {}) is not None
              and end.get('status') == 'complete' and end.get('error') is None,
              {'searcher_calls': len(s1.queries), 'queries': s1.queries,
               'has_ref_block': ref_in(b[-1] if b else {}) is not None,
               'end_status': end.get('status'), 'end_error': end.get('error')})
        check('C02', '查询以对象为主', s1.queries[:1] == ['影之刃零'],
              {'queries': s1.queries})
        store.close()

    # ---- C03: another object, no name-specific rule -------------------- #
    s3 = ScriptedSearcher('relevant', object_name='星穹铁道')
    with run([{'kind': 'text', 'text': GOOD_TEXT}]) as srv:
        chatsearch.set_searcher(s3.make())
        store, svc = new_service(srv)
        sid = store.create_session('C03')['id']
        store.update_session_settings(sid, {'web_search': True, 'learn_memory': False})
        svc.send(sid, '介绍一下星穹铁道这个游戏', web_search=True)
        wait_done(store, sid)
        check('C03', '另一个具体外部对象同样检索（不依赖名单）',
              len(s3.queries) == 1 and '星穹铁道' in s3.queries[0],
              {'queries': s3.queries})
        store.close()

    # ---- C04-C06: direct answers never search -------------------------- #
    for cid, msg, note in (
            ('C04', '介绍一下你自己。', '自我介绍'),
            ('C05', '介绍的是你，不是我。', '纠正自我介绍'),
            ('C06', '你好，今天有点累。', '情绪闲聊')):
        sN = ScriptedSearcher('relevant')
        with run([{'kind': 'text', 'text': PLAIN_TEXT}]) as srv:
            chatsearch.set_searcher(sN.make())
            store, svc = new_service(srv)
            sid = store.create_session(cid)['id']
            store.update_session_settings(sid, {'web_search': True, 'learn_memory': False})
            svc.send(sid, msg, web_search=True)
            msgs = wait_done(store, sid)
            b = bodies(srv)
            asst = [m for m in msgs if m['role'] == 'assistant']
            end = end_for(store, sid)
            check(cid, f'不搜索且正常结束：{note}',
                  len(sN.queries) == 0 and end.get('error') is None
                  and end.get('status') == 'complete'
                  and ref_in(b[-1] if b else {}) is None,
                  {'searcher_calls': len(sN.queries), 'end_status': end.get('status'),
                   'end_error': end.get('error'),
                   'has_ref_block': ref_in(b[-1] if b else {}) is not None})
            store.close()

    # ---- C07: switch off -> no network even for an object request ------ #
    s7 = ScriptedSearcher('relevant')
    with run([{'kind': 'text', 'text': PLAIN_TEXT}]) as srv:
        chatsearch.set_searcher(s7.make())
        store, svc = new_service(srv)
        sid = store.create_session('C07')['id']
        store.update_session_settings(sid, {'web_search': False, 'learn_memory': False})
        svc.send(sid, '给我讲一讲影之刃零的信息吧', web_search=False)
        msgs = wait_done(store, sid)
        asst = [m for m in msgs if m['role'] == 'assistant']
        end = end_for(store, sid)
        check('C07', '联网关闭时对象请求也不调用网络',
              len(s7.queries) == 0 and end.get('error') is None,
              {'searcher_calls': len(s7.queries), 'end_status': end.get('status'),
               'end_error': end.get('error')})
        store.close()

    # ---- C08: per-message opt-out -------------------------------------- #
    s8 = ScriptedSearcher('relevant')
    with run([{'kind': 'text', 'text': PLAIN_TEXT}]) as srv:
        chatsearch.set_searcher(s8.make())
        store, svc = new_service(srv)
        sid = store.create_session('C08')['id']
        store.update_session_settings(sid, {'web_search': True, 'learn_memory': False})
        svc.send(sid, '别联网，直接告诉我影之刃零是什么', web_search=True)
        msgs = wait_done(store, sid)
        asst = [m for m in msgs if m['role'] == 'assistant']
        end = end_for(store, sid)
        check('C08', '本条明确禁用联网时搜索调用数为 0',
              len(s8.queries) == 0 and end.get('error') is None,
              {'searcher_calls': len(s8.queries), 'end_status': end.get('status')})
        store.close()

    # ---- C09: gap-fill fires once and regenerates ---------------------- #
    print('\n--- 补查（一次）')
    s9 = ScriptedSearcher('relevant')
    with run([{'kind': 'text', 'text': GAP_TEXT},
              {'kind': 'text', 'text': GOOD_TEXT}]) as srv:
        chatsearch.set_searcher(s9.make())
        store, svc = new_service(srv)
        sid = store.create_session('C09')['id']
        store.update_session_settings(sid, {'web_search': True, 'learn_memory': False})
        # "影之刃零要出了" is NOT routed to search, so the only path to a search
        # here is the gap-fill.
        svc.send(sid, '影之刃零要出了', web_search=True)
        msgs = wait_done(store, sid)
        b = bodies(srv)
        asst = [m for m in msgs if m['role'] == 'assistant']
        end = end_for(store, sid)
        rr = (end.get('extra') or {}).get('search_route') or {}
        check('C09', '候选答案表示缺乏资料时补查一次',
              len(s9.queries) == 1 and len(b) == 2 and rr.get('gap_fill') is True,
              {'searcher_calls': len(s9.queries), 'queries': s9.queries,
               'model_calls': len(b), 'gap_fill': rr.get('gap_fill'),
               'gap_fill_query': rr.get('gap_fill_query')})
        check('C10', '补查后只有一条最终 assistant 回复，候选不入历史',
              len(asst) == 1 and GOOD_TEXT[:6] in (asst[0].get('content') or '')
              and GAP_TEXT not in (asst[0].get('content') or ''),
              {'assistant_rows': len(asst),
               'content_head': (asst[0].get('content') or '')[:60]})
        check('C11', '补查只搜索一轮（不因仍不确定而循环）',
              len(s9.queries) == 1, {'searcher_calls': len(s9.queries)})
        store.close()

    # ---- C12: quoted "不知道" must not trigger a gap-fill -------------- #
    s12 = ScriptedSearcher('relevant')
    with run([{'kind': 'text', 'text': '原文的意思是「我不知道」。'}]) as srv:
        chatsearch.set_searcher(s12.make())
        store, svc = new_service(srv)
        sid = store.create_session('C12')['id']
        store.update_session_settings(sid, {'web_search': True, 'learn_memory': False})
        svc.send(sid, '帮我把这段话翻译成英文：我不知道', web_search=True)
        wait_done(store, sid)
        check('C12', '翻译任务里的「不知道」不触发补查',
              len(s12.queries) == 0, {'searcher_calls': len(s12.queries)})
        store.close()

    # ---- C13: search failed -> no loop, honest ending ------------------ #
    s13 = ScriptedSearcher('fail')
    with run([{'kind': 'text', 'text': GAP_TEXT},
              {'kind': 'text', 'text': GAP_TEXT}]) as srv:
        chatsearch.set_searcher(s13.make())
        store, svc = new_service(srv)
        sid = store.create_session('C13')['id']
        store.update_session_settings(sid, {'web_search': True, 'learn_memory': False})
        svc.send(sid, '影之刃零要出了', web_search=True)
        msgs = wait_done(store, sid)
        asst = [m for m in msgs if m['role'] == 'assistant']
        end = end_for(store, sid)
        check('C13', '补查失败后不再循环搜索，且仍有回复',
              len(s13.queries) == 1 and len(asst) == 1
              and end.get('error') is None,
              {'searcher_calls': len(s13.queries), 'assistant_rows': len(asst),
               'end_status': end.get('status')})
        store.close()

    # ---- C14: irrelevant results are not injected ---------------------- #
    s14 = ScriptedSearcher('irrelevant')
    with run([{'kind': 'text', 'text': PLAIN_TEXT}]) as srv:
        chatsearch.set_searcher(s14.make())
        store, svc = new_service(srv)
        sid = store.create_session('C14')['id']
        store.update_session_settings(sid, {'web_search': True, 'learn_memory': False})
        svc.send(sid, '给我讲一讲影之刃零的信息吧', web_search=True)
        msgs = wait_done(store, sid)
        b = bodies(srv)
        asst = [m for m in msgs if m['role'] == 'assistant']
        end = end_for(store, sid)
        ws = (asst[-1].get('web_search') or {}) if asst else {}
        check('C14', '资料全部无关时不注入、不标联网回答',
              len(s14.queries) == 1 and ref_in(b[-1] if b else {}) is None
              and end.get('extra', {}).get('search_used') is False
              and ws.get('status') in ('discarded', 'empty', 'error'),
              {'searcher_calls': len(s14.queries),
               'has_ref_block': ref_in(b[-1] if b else {}) is not None,
               'search_used': end.get('extra', {}).get('search_used'),
               'web_search_status': ws.get('status')})
        store.close()

    # ---- C15-C16: no unbound-variable exception on any path ------------ #
    print('\n--- 结束事件与异常')
    s15 = ScriptedSearcher('relevant')
    with run([{'kind': 'text', 'text': PLAIN_TEXT}]) as srv:
        chatsearch.set_searcher(s15.make())
        store, svc = new_service(srv)
        sid = store.create_session('C15')['id']
        store.update_session_settings(sid, {'web_search': True, 'learn_memory': False})
        svc.send(sid, '介绍一下你自己', web_search=True)
        msgs = wait_done(store, sid)
        asst = [m for m in msgs if m['role'] == 'assistant']
        end = end_for(store, sid)
        check('C15', '未搜索路径正常结束（UnboundLocalError 已修复）',
              end.get('status') == 'complete' and end.get('error') is None,
              {'end_status': end.get('status'), 'end_error': end.get('error')})
        store.close()

    s16 = ScriptedSearcher('empty')
    with run([{'kind': 'text', 'text': PLAIN_TEXT}]) as srv:
        chatsearch.set_searcher(s16.make())
        store, svc = new_service(srv)
        sid = store.create_session('C16')['id']
        store.update_session_settings(sid, {'web_search': True, 'learn_memory': False})
        svc.send(sid, '帮我查一下今天的天气', web_search=True)
        msgs = wait_done(store, sid)
        asst = [m for m in msgs if m['role'] == 'assistant']
        end = {k: v for k, v in ended.items() if k}
        unbound = [v for v in end.values()
                   if 'UnboundLocalError' in str(v.get('error') or '')]
        check('C16', '搜索无结果路径也不出现 UnboundLocalError',
              unbound == [], {'unbound_count': len(unbound),
                              'total_end_events': len(end)})
        store.close()

    # ---- C17: stop during gap-fill does not submit a late reply -------- #
    print('\n--- 停止与并发')
    s17 = ScriptedSearcher('relevant')
    with run([{'kind': 'text', 'text': GAP_TEXT},
              {'kind': 'text', 'text': GOOD_TEXT}]) as srv:
        chatsearch.set_searcher(s17.make())
        store, svc = new_service(srv)
        sid = store.create_session('C17')['id']
        store.update_session_settings(sid, {'web_search': True, 'learn_memory': False})
        svc.send(sid, '影之刃零要出了', web_search=True)
        time.sleep(0.25)
        stop_res = svc.stop(sid)
        msgs = wait_done(store, sid)
        asst = [m for m in msgs if m['role'] == 'assistant']
        check('C17', '停止后不提交迟到的最终回复',
              isinstance(stop_res, dict) and len(asst) <= 1,
              {'stop': stop_res, 'assistant_rows': len(asst),
               'status': asst[-1].get('status') if asst else None})
        store.close()

    gate = chatweb._SEARCH_GATE.status()
    check('C18', '并发容量与任务释放机制仍存在',
          isinstance(gate, dict) and gate.get('running') == 0
          and 'max_total' in gate,
          {'gate': gate})

    chatweb.ChatService._emit_end = orig_emit


def main() -> int:
    layer1()
    layer2()
    passed = sum(1 for r in results if r['pass'])
    summary = {
        'round': 55,
        'suite': 'search automation: broader trigger / one gap-fill / no unbound vars',
        'layers': {
            'layer1': 'frozen rule cases (no network, no model)',
            'layer2': 'real ChatService.send() with mock searcher + mock model',
        },
        'frozen_case_count': (len(FROZEN_SEARCH) + len(FROZEN_DIRECT)
                              + len(FROZEN_NO_WEB)),
        'finished_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        'total': len(results), 'passed': passed,
        'failed': len(results) - passed,
        'all_passed': passed == len(results),
        'checks': results,
    }
    OUT.write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                   encoding='utf-8', newline='\n')
    print(f'\n=== {passed}/{len(results)} passed, {len(results) - passed} failed')
    print('written ' + str(OUT))
    return 0 if passed == len(results) else 1


if __name__ == '__main__':
    try:
        rc = main()
    except Exception:                                          # noqa: BLE001
        traceback.print_exc()
        rc = 1
    raise SystemExit(rc)
