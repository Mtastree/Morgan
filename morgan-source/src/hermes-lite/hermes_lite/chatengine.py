"""Round 16: the pure chat engine. One streaming generation per send.

## What a chat request contains -- and what it must NOT contain

    [system]   a short chat system prompt
    [system]   a short memory block (only when there is something to inject)
    [...]      the most recent COMPLETE turns that fit the budget
    [user]     the newest user message

No `tools`, no `tool_choice`, no Skill text, no experience card, no Hermes
environment guidance (no directory map, no terminal hints, no plugin notes).
`assert_pure()` checks the body right before the send and raises if any of those
ever come back, so the property is enforced at the wire, not just intended.

## Budget

The verified runtime window is used and this chat reserves `MAX_OUTPUT_TOKENS` for the
reply. The prompt must fit in what is left. Counting uses the model's own
tokenizer (`/tokenize`), not a character heuristic, and the assembly loop drops
the OLDEST complete turns until it fits. What it dropped is reported to the page
as "earlier messages were not sent this time; they are still saved".

A single user message that cannot fit even on its own is refused BEFORE sending,
with a message telling the user to shorten it. It is never silently truncated,
and creating a new session is not offered as a fix (it would not be one).

## Counting

Three counters, incremented at the boundary where each request is actually
issued: `chat_requests`, `extraction_requests`, `aux_requests`. A plain chat turn
that triggers no memory rule and no background-looking statement issues exactly
one chat request and nothing else.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

from hermes_lite import chatparams

LITE = Path(__file__).resolve().parents[1]
PROJECT = LITE.parents[1]

MAX_OUTPUT_TOKENS = 512
CHAT_SYSTEM = (
    '你是一个运行在本机的离线中文助手，用自然、简洁的方式和用户对话。\n'
    '直接回答，不要复述用户的整句话，不要假装记得没有记录过的事情。'
)
STYLE_INSTRUCTIONS = {
    'standard': '',
    'concise': '\n\n本次回答要求：请用极其简练、扼要的语言直接作答，开门见山，无需冗长铺垫。',
    'detailed': '\n\n本次回答要求：请全面、深入、充分展开作答，条理清晰，提供详实的说明与必要解释。',
}
TITLE_MAX_CHARS = 18

# Anything here reaching the wire means the pure-chat contract is broken.
FORBIDDEN_KEYS = ('tools', 'tool_choice', 'functions', 'tool_call', 'tool_calls',
                  'parallel_tool_calls', 'response_format')
FORBIDDEN_TEXT = ('list_dir', 'read_text', 'search_text', 'csv_query',
                  'write_text', '工具使用指南', '经验卡')


# --------------------------------------------------------------------------- #
# Endpoint + tokenizer
# --------------------------------------------------------------------------- #
def deployment() -> dict:
    p = PROJECT / 'configs' / 'deployment.json'
    return json.loads(p.read_text(encoding='utf-8-sig'))


def effective_deployment(profile_id: Optional[str] = None) -> dict:
    """The deployment with the selected model profile applied.

    Round 43: `deployment.json` alone is no longer the answer to "which model".
    Every reader in this package goes through here so that a switch cannot leave
    the tokenizer, the declared-window probe or the page describing a different
    model from the one being served.

    A corrupt registry is NOT fatal for a read: the deployment file is returned
    with `registry_error` attached, so an auxiliary path keeps working while the
    page reports the problem.
    """
    from hermes_lite import modelprofiles
    try:
        return modelprofiles.effective_deployment(profile_id, strict=False)
    except modelprofiles.ProfileError:
        return deployment()



def chat_sampling() -> dict:
    """The chat sampling profile. Falls back to the deployed defaults."""
    p = PROJECT / 'configs' / 'sampling_chat.json'
    try:
        prof = json.loads(p.read_text(encoding='utf-8-sig'))
        params = prof.get('params') or {}
    except (OSError, ValueError):
        params = {}
    out = {'temperature': params.get('temperature', 0.8),
           'top_k': params.get('top_k', 40),
           'top_p': params.get('top_p', 0.95),
           'repeat_penalty': params.get('repeat_penalty', 1.0),
           'max_tokens': params.get('max_tokens', MAX_OUTPUT_TOKENS)}
    # seed is intentionally absent: chat does not need reproducibility.
    return out


def base_url() -> str:
    import os
    override = os.environ.get('HERMES_LITE_CHAT_BASE_URL')
    if override:
        return override.rstrip('/')
    return effective_deployment()['base_url'].rstrip('/')


def model_declared_context(base: Optional[str] = None,
                           timeout: float = 2.0) -> tuple[Optional[int], str]:
    """The context length the MODEL FILE declares, and where that came from.

    Round 41 separates three different numbers that used to be conflated:

        declared  -- what the architecture supports (this is a CLAIM)
        actual    -- what the running server was configured with (`/props` n_ctx)
        default   -- what a new session asks for

    llama.cpp's `/v1/models` reports `meta.n_ctx_train`, which is a property of
    the model, not of this machine. A 6GB card cannot necessarily run the
    declared length, so this value must never be rendered as "current".

    Returns (value_or_None, source_string). Source is 'model-metadata' when it
    came from the server, 'deployment-config' when the server was unreachable
    and the config declared it, or 'unknown' when nothing was available.
    """
    root = _root(base or base_url())
    try:
        data = _get_json(f'{root}/v1/models', timeout)
        for entry in (data.get('data') or []):
            meta = entry.get('meta') or {}
            n = meta.get('n_ctx_train')
            if isinstance(n, int) and n > 0:
                return n, 'model-metadata'
        # Some builds expose models at /models without the /v1 prefix.
        data = _get_json(f'{root}/models', timeout)
        for entry in (data.get('data') or []):
            meta = entry.get('meta') or {}
            n = meta.get('n_ctx_train')
            if isinstance(n, int) and n > 0:
                return n, 'model-metadata'
    except Exception:  # noqa: BLE001
        pass
    try:
        dep = effective_deployment()
        n = dep.get('declared_context_size') or dep.get('model_context_size')
        if isinstance(n, int) and n > 0:
            return n, 'deployment-config'
    except Exception:  # noqa: BLE001
        pass
    return None, 'unknown'


def _root(base: str) -> str:
    return base[:-3] if base.endswith('/v1') else base


def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _post_json(url: str, payload: dict, timeout: float) -> Any:
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(url, data=data, method='POST',
                                 headers={'Content-Type': 'application/json'})
    with _opener().open(req, timeout=timeout) as r:
        return json.load(r)


def _get_json(url: str, timeout: float) -> Any:
    with _opener().open(url, timeout=timeout) as r:
        return json.load(r)


class Tokenizer:
    """The model's own tokenizer. Short-lived caches, one HTTP call per miss.

    The counter is told WHICH endpoint was called, so the auxiliary ledger names
    `/apply-template` and `/tokenize` separately instead of lumping them. Cached
    answers cost nothing and are not counted, because no request was made.
    """

    def __init__(self, base: str,
                 counter: Optional[Callable[..., None]] = None):
        self.base = base
        self._root = _root(base)
        self._cache: dict[str, int] = {}
        self._render_cache: dict[str, str] = {}
        self._lock = threading.Lock()
        self.count_request = counter or (lambda *a: None)
        self.calls = {'apply_template': 0, 'tokenize': 0}

    def _tick(self, endpoint: str) -> None:
        self.calls[endpoint] += 1
        self.count_request('aux', endpoint)

    def count(self, text: str, timeout: float = 30.0) -> int:
        if not text:
            return 0
        with self._lock:
            if text in self._cache:
                return self._cache[text]
        self._tick('tokenize')
        obj = _post_json(f'{self._root}/tokenize', {'content': text}, timeout)
        n = len(obj.get('tokens') or [])
        with self._lock:
            self._cache[text] = n
        return n

    def render(self, messages: list[dict], timeout: float = 30.0) -> str:
        """Apply the server's own chat template. Cached: rendering the same
        message list twice must not cost two HTTP requests."""
        key = json.dumps(messages, ensure_ascii=False, sort_keys=True)
        with self._lock:
            if key in self._render_cache:
                return self._render_cache[key]
        self._tick('apply_template')
        obj = _post_json(f'{self._root}/apply-template', {'messages': messages},
                         timeout)
        prompt = obj.get('prompt') or ''
        with self._lock:
            self._render_cache[key] = prompt
        return prompt

    @property
    def aux_calls(self) -> int:
        return self.calls['apply_template'] + self.calls['tokenize']


# --------------------------------------------------------------------------- #
# The prompt
# --------------------------------------------------------------------------- #
def build_messages(*, memory_block: str, history: list[dict],
                   user_text: str, style: str = 'standard',
                   summary_block: Optional[str] = None,
                   search_block: Optional[str] = None,
                   persona_block: str = '') -> list[dict]:
    """System (+ memory + summary + search) + as much recent history as the caller passed + user.

    Round 50: `persona_block` is ONE pre-rendered paragraph appended to the single
    application system message. It is placed BEFORE the per-turn style
    instruction so that the user's explicit, current-turn requirement (concise /
    detailed) still comes last and wins -- the persona is a standing style, the
    style selector is a live instruction. Passing '' (disabled, empty body, or a
    request that has no persona) adds nothing at all: no empty system message.
    """
    system_content = CHAT_SYSTEM
    if persona_block:
        system_content += '\n\n' + persona_block
    system_content += STYLE_INSTRUCTIONS.get(style, '')
    msgs: list[dict] = [{'role': 'system', 'content': system_content}]
    if memory_block:
        msgs.append({'role': 'system', 'content': memory_block})
    if summary_block:
        msgs.append({'role': 'system', 'content': summary_block})
    for m in history:
        msgs.append({'role': m['role'], 'content': m['content']})
    if search_block:
        msgs.append({'role': 'system', 'content': search_block})
    msgs.append({'role': 'user', 'content': user_text})
    return msgs


def build_body(messages: list[dict], model: str, max_tokens: int = MAX_OUTPUT_TOKENS) -> dict:
    """The request body. Sampling comes from the chat profile, never from eval."""
    body = {'model': model, 'messages': messages, 'stream': True}
    sampling = chat_sampling()
    sampling['max_tokens'] = max_tokens
    body.update(sampling)
    return body


def assert_pure(body: dict, *, persona_block: str = '') -> None:
    """Refuse to send anything that is not a plain chat completion.

    Round 50: `persona_block` is USER-AUTHORED text that legitimately lives in the
    system prompt. It is removed from the scanned content first, so this guard
    keeps catching an application-side regression (a tool guide leaking back into
    `CHAT_SYSTEM`) without failing because the user happened to write an English
    word like "read_text" in their own persona.
    """
    for k in FORBIDDEN_KEYS:
        if k in body:
            raise AssertionError(f'pure-chat violated: body contains {k!r}')
    for m in body.get('messages') or []:
        role = m.get('role')
        if role not in ('system', 'user', 'assistant'):
            raise AssertionError(f'pure-chat violated: role {role!r}')
        if 'tool_calls' in m or 'tool_call_id' in m or 'function_call' in m:
            raise AssertionError('pure-chat violated: tool message fields present')
        # Only check application base system prompts, never user/assistant conversations
        # or memory blocks containing user's own statements about these keywords.
        if role == 'system':
            c = str(m.get('content') or '')
            if persona_block:
                c = c.replace(persona_block, '')
            if (c.startswith('关于用户（来自之前的交流') or
                c.startswith('【较早对话摘要') or
                c.startswith('【历史摘要') or
                c.startswith('【联网搜索参考资料')):
                continue
            for bad in FORBIDDEN_TEXT:
                if bad in c:
                    raise AssertionError(f'pure-chat violated: system prompt mentions {bad!r}')


def render_tokens(tokenizer: Tokenizer, messages: list[dict]) -> int:
    """Exact count after applying the server's own chat template."""
    return tokenizer.count(tokenizer.render(messages))


# --------------------------------------------------------------------------- #
# The budget-aware assembly
# --------------------------------------------------------------------------- #
def complete_turns(history: list[dict]) -> list[list[dict]]:
    """Pair user+assistant, keeping ONLY complete replies.

    An interrupted or failed reply is not context we can present as a finished
    answer, so it does not travel as history. Its text is still saved and still
    shown in the page.
    """
    turns: list[list[dict]] = []
    pending: Optional[dict] = None
    for m in history:
        if m['role'] == 'user':
            pending = m
        elif m['role'] == 'assistant':
            if pending is not None and m.get('status') == 'complete' \
                    and (m.get('content') or ''):
                turns.append([pending, m])
            pending = None
    return turns


def assemble(*, tokenizer: Tokenizer, memory_block: str, history: list[dict],
             user_text: str, window: int,
             max_output: int = MAX_OUTPUT_TOKENS,
             style: str = 'standard',
             custom_budget: Optional[int] = None,
             summary_block: Optional[str] = None,
             search_block: Optional[str] = None,
             persona_block: str = '') -> dict:
    """Fit system + persona + memory + summary + search + newest message + recent COMPLETE turns.

    Tries everything first (the common case costs one render). If it does not
    fit, turns are dropped from the OLDEST end in a batch sized by the measured
    overflow, using per-message token counts, so the loop converges in a couple
    of exact renders instead of one per turn. Every "earlier messages were not
    sent" note therefore has a measurement behind it, and a long conversation
    does not pay a tokenizer request per dropped turn.

    Round 50: the persona is part of the system message that `build()` renders, so
    its tokens are counted by the model's own tokenizer like everything else. It
    is NEVER dropped to make room -- dropping the user's persona silently would
    defeat the feature. History is dropped instead; if even the base prompt
    (system + persona + memory + the newest message) does not fit, assembly fails
    with an explicit message naming the persona as a possible cause.
    """
    try:
        chatparams.validate_runtime_settings(
            {'max_tokens': max_output, 'history_mode': 'custom' if custom_budget is not None else 'auto',
             'custom_budget': custom_budget}, window)
    except chatparams.ParamError as exc:
        return exc.to_dict()
    model_budget = window - max_output
    if model_budget <= 0:
        return {'ok': False, 'error': 'bad_window',
                'message': f'上下文窗口 {window} 不足以容纳输出预留 {max_output}。'}

    is_custom = False
    if custom_budget is not None and custom_budget > 0:
        budget = custom_budget
        is_custom = True
    else:
        budget = model_budget

    def build(hist: list[dict]) -> list[dict]:
        return build_messages(memory_block=memory_block, history=hist,
                              user_text=user_text, style=style,
                              summary_block=summary_block,
                              search_block=search_block,
                              persona_block=persona_block)

    def turn_tokens(pair: list[dict]) -> int:
        return sum(tokenizer.count((m.get('content') or '') + m['role'])
                   for m in pair) + 8      # per-message template overhead

    turns = complete_turns(history)
    kept = list(turns)
    omitted = 0

    # Common case: everything fits. ONE exact render+count (two auxiliary
    # requests), and the base-only measurement below is never needed.
    used = render_tokens(tokenizer, build([x for p in kept for x in p]))
    if used > budget:
        base_tokens = render_tokens(tokenizer, build([]))
        if base_tokens > budget:
            budget_label = f'自定义预算 {budget}' if is_custom else f'可用预算 {budget}'
            persona_hint = ('如果开启了「聊天人格」，它也会占用预算；'
                            '可以先把人格正文缩短，或关闭人格后重试。'
                            if persona_block else '')
            return {'ok': False, 'error': 'input_too_long',
                    'message': (f'这条消息本身太长：仅系统提示、记忆和这条消息就需要 '
                                f'{base_tokens} tokens，{budget_label}。'
                                f'{persona_hint}'
                                '请把这条消息删短一些再发（记录会保留，不需要新建会话）。'),
                    'need_tokens': base_tokens, 'budget': budget,
                    'over_by': base_tokens - budget,
                    'persona_included': bool(persona_block),
                    'persona_tokens_note': (
                        '人格已包含在上述 token 数内，装配不会为腾空间而删除人格。'
                        if persona_block else None)}
        # Drop from the OLDEST end in a batch sized by the measured overflow, so a
        # long conversation converges in a couple of exact renders rather than one
        # render per dropped turn.
        for _ in range(len(turns) + 2):
            if used <= budget:
                break
            if not kept:
                budget_label = f'自定义预算 {budget}' if is_custom else f'可用预算 {budget}'
                persona_hint = ('如果开启了「聊天人格」，它也会占用预算；'
                                '可以先把人格正文缩短，或关闭人格后重试。'
                                if persona_block else '')
                return {'ok': False, 'error': 'input_too_long',
                        'message': (f'即使只保留系统提示与这条消息也需要 {used} '
                                    f'tokens，超过{budget_label}。'
                                    f'{persona_hint}'
                                    '请把这条消息删短一些再发。'),
                        'need_tokens': used, 'budget': budget,
                        'over_by': used - budget,
                        'persona_included': bool(persona_block)}
            excess = used - budget
            freed = 0
            while kept and freed < excess:
                freed += turn_tokens(kept[0])
                kept = kept[1:]
                omitted += 1
            used = render_tokens(tokenizer, build([x for p in kept for x in p]))

    return {'ok': True, 'messages': build([x for p in kept for x in p]),
            'prompt_tokens': used,
            'budget': budget, 'window': window,
            'max_output': max_output,
            'style': style,
            'is_custom_budget': is_custom,
            'turns_included': len(kept), 'turns_omitted': omitted,
            'history_omitted': omitted > 0,
            'summary_used': bool(summary_block),
            'summary_tokens': tokenizer.count(summary_block) if summary_block else 0,
            'search_used': bool(search_block),
            'search_tokens': tokenizer.count(search_block) if search_block else 0,
            'persona_used': bool(persona_block),
            'persona_tokens': tokenizer.count(persona_block) if persona_block else 0,
            'aux_calls': tokenizer.aux_calls,
            'note': (f'本次只带入了最近 {len(kept)} 轮完整对话；'
                     f'更早的 {omitted} 轮没有带进这次请求，但记录仍保存在本地。'
                     if omitted else '')}


# --------------------------------------------------------------------------- #
# Streaming
# --------------------------------------------------------------------------- #
def stream_chat(*, body: dict, on_delta: Callable[[str], None],
                should_stop: Callable[[], bool], counter: Callable[[str], None],
                timeout: float = 300.0,
                base: Optional[str] = None,
                persona_block: str = '') -> dict:
    """One streaming POST to /chat/completions.

    `counter` is called once, at the moment the request is issued -- not once per
    chunk, and not after a retry (there is no retry).

    Round 50: `persona_block` is passed straight to `assert_pure` so user-authored
    persona text is not mistaken for an application prompt regression.
    """
    assert_pure(body, persona_block=persona_block)
    url = f'{(base or base_url()).rstrip("/")}/chat/completions'
    data = json.dumps(body, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(url, data=data, method='POST',
                                 headers={'Content-Type': 'application/json'})
    t0 = time.time()
    text_parts: list[str] = []
    usage = None
    finish = None
    saw_done = False
    status = None
    counter('chat')
    try:
        with _opener().open(req, timeout=timeout) as r:
            status = getattr(r, 'status', None) or r.getcode()
            for raw in r:
                if should_stop():
                    break
                line = raw.decode('utf-8', 'replace').strip()
                if not line.startswith('data:'):
                    continue
                payload = line[5:].strip()
                if not payload:
                    continue
                if payload == '[DONE]':
                    saw_done = True
                    continue
                try:
                    obj = json.loads(payload)
                except ValueError:
                    continue
                if isinstance(obj.get('usage'), dict):
                    usage = obj['usage']
                for ch in (obj.get('choices') or []):
                    if ch.get('finish_reason'):
                        finish = ch['finish_reason']
                    d = ch.get('delta') or {}
                    piece = d.get('content')
                    if piece:
                        text_parts.append(piece)
                        on_delta(piece)
    except urllib.error.HTTPError as exc:
        body_txt = ''
        try:
            body_txt = exc.read().decode('utf-8', 'replace')
        except Exception:  # noqa: BLE001
            pass
        return {'ok': False, 'error': f'HTTP {exc.code}: {body_txt[:300]}',
                'status': exc.code, 'text': ''.join(text_parts),
                'elapsed_s': round(time.time() - t0, 3), 'usage': usage}
    except Exception as exc:  # noqa: BLE001
        return {'ok': False, 'error': f'{type(exc).__name__}: {exc}',
                'status': None, 'text': ''.join(text_parts),
                'elapsed_s': round(time.time() - t0, 3), 'usage': usage}
    stopped = should_stop()
    # A stream that ends without `[DONE]` or a finish_reason was cut, even though
    # urllib sometimes reports a clean end-of-line for a dropped connection.
    # Calling that "complete" would present a truncated reply as a finished one.
    truncated = (not stopped) and (not saw_done) and (finish is None)
    return {'ok': True, 'text': ''.join(text_parts), 'status': status,
            'finish_reason': 'stopped' if stopped else finish,
            'stopped': stopped, 'truncated': truncated,
            'elapsed_s': round(time.time() - t0, 3),
            'usage': usage, 'completion_tokens': (usage or {}).get('completion_tokens')}


# --------------------------------------------------------------------------- #
# Memory extraction: exactly one request, never blocking, never retried
# --------------------------------------------------------------------------- #
def extract_once(*, user_text: str, model_alias: Optional[str] = None,
                 base: Optional[str] = None, timeout: float = 45.0) -> dict:
    """One non-streaming request to structure a personal statement.

    Called only when the rules found nothing and the text looks like durable
    background. Failure is a skipped update, not an error the user sees.
    """
    from hermes_lite import chatmem

    messages = [{'role': 'system', 'content': chatmem.EXTRACT_SYSTEM},
                {'role': 'user', 'content': chatmem.extract_user_prompt(user_text)}]
    alias = model_alias or deployment()['alias']
    body = {'model': alias, 'messages': messages,
            'stream': False, 'temperature': 0, 'top_k': 1, 'top_p': 1,
            'max_tokens': 220}
    url = f'{(base or base_url()).rstrip("/")}/chat/completions'
    t0 = time.time()
    try:
        obj = _post_json(url, body, timeout)
    except urllib.error.HTTPError as exc:
        return {'ok': False, 'error': f'HTTP {exc.code}', 'raw': '',
                'elapsed_s': round(time.time() - t0, 3)}
    except Exception as exc:  # noqa: BLE001
        return {'ok': False, 'error': f'{type(exc).__name__}: {exc}', 'raw': '',
                'elapsed_s': round(time.time() - t0, 3)}
    raw = ''
    try:
        raw = (((obj.get('choices') or [{}])[0].get('message') or {})
               .get('content') or '')
    except Exception:  # noqa: BLE001
        raw = ''
    return {'ok': True, 'raw': raw, 'usage': obj.get('usage'),
            'elapsed_s': round(time.time() - t0, 3)}


def guess_title(text: str) -> str:
    """A short session title from the first user message. No model call."""
    t = re.sub(r'\s+', ' ', (text or '').strip())
    t = re.sub(r'^[#>\-\*\d.\s]+', '', t)
    if not t:
        return '新对话'
    cut = re.split(r'[。！？?!\n]', t)[0].strip() or t
    return cut[:TITLE_MAX_CHARS] + ('…' if len(cut) > TITLE_MAX_CHARS else '')
