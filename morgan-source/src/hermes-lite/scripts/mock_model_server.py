"""A controllable local model server for engineering tests.

The brief requires verifying, on a LOCAL simulated service, that:
  * the real number of model HTTP requests matches the cap;
  * a retry (Hermes' own, or a forced one) is really covered by the six-request
    cap;
  * a failure, a mid-stream interruption and a slow first byte all leave a
    usable record;
  * cancellation stops FURTHER requests and FURTHER tool calls.

This module provides exactly that service and nothing else. It is NOT the model:
it answers with scripted SSE frames. Any test that uses it is labelled
"engineering test (controlled local service)", never "task run".

## Scripting

A scenario is a list of "turns". Each turn is either:

    {'kind': 'tool_call', 'calls': [{'name': ..., 'arguments': '...'}], 'id': 'c1'}
    {'kind': 'text', 'text': '...'}
    {'kind': 'fail', 'status': 500, 'body': '...'}          -> HTTP error, no SSE
    {'kind': 'interrupt', 'text': 'partial', 'after': 2}    -> SSE that stops mid-stream
    {'kind': 'slow_first_byte', 'text': '...', 'delay': 0.6}
    {'kind': 'garbage'}                                     -> unparsable body

Two delays, deliberately separate:
  ``delay``          before the headers are sent (connection/scheduling latency)
  ``content_delay``  after the headers, before the first content chunk (prefill)

That split is what makes "the first-content time is not the header time" a
measurable property instead of an assumption.

Turns are consumed in order; the last one repeats. The server records every
request it received, with the body, so a test can assert on what reached the
wire rather than on what the client believed it sent.
"""
from __future__ import annotations

import json
import socket
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional


class ServerState:
    """Shared, plain object -- a threading.local() would be invisible to handlers."""

    def __init__(self, scenario: list[dict], n_ctx: int = 4096):
        self.scenario = scenario
        self.turn = 0
        self.requests: list[dict] = []
        self.lock = threading.Lock()
        self.first_byte_delay = 0.0
        self.interrupt_at_chunk: Optional[int] = None
        # The window the server REPORTS. A test can make it differ from the
        # declared one, which is how "the runtime refuses to start when the
        # window does not match" is exercised deterministically -- picking a dead
        # port is not reliable, because on Windows a just-closed listener can
        # still hold the port.
        self.n_ctx = n_ctx

    def next_turn(self) -> dict:
        with self.lock:
            if not self.scenario:
                return {'kind': 'text', 'text': 'ok'}
            t = self.scenario[min(self.turn, len(self.scenario) - 1)]
            self.turn += 1
            return t

    def record(self, path: str, body: Any, headers: dict) -> None:
        with self.lock:
            self.requests.append({'path': path, 'body': body, 'headers': headers,
                                  'at': time.time()})

    def chat_requests(self) -> list[dict]:
        with self.lock:
            return [r for r in self.requests
                    if r['path'].endswith('/chat/completions')]

    def aux_requests(self) -> list[dict]:
        with self.lock:
            return [r for r in self.requests
                    if not r['path'].endswith('/chat/completions')]


STATE: Optional[ServerState] = None


def _sse(obj: dict) -> bytes:
    return f'data: {json.dumps(obj, ensure_ascii=False)}\n\n'.encode('utf-8')


def _chunk(delta: dict, finish: Optional[str] = None, usage: Optional[dict] = None,
           model: str = 'mock-model') -> dict:
    out: dict[str, Any] = {
        'id': 'chatcmpl-mock', 'object': 'chat.completion.chunk',
        'created': int(time.time()), 'model': model,
        'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}],
    }
    if usage:
        out['usage'] = usage
    return out


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    # ---- helpers ---------------------------------------------------------
    def _json(self, code: int, obj: Any) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _sse_start(self) -> None:
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Transfer-Encoding', 'chunked')
        self.end_headers()

    def _write_chunk(self, payload: bytes) -> None:
        self.wfile.write(f'{len(payload):X}\r\n'.encode() + payload + b'\r\n')
        self.wfile.flush()

    def _end_chunked(self) -> None:
        self.wfile.write(b'0\r\n\r\n')
        self.wfile.flush()

    # ---- routes ----------------------------------------------------------
    def do_GET(self):
        STATE.record(self.path, None, dict(self.headers))
        if self.path.endswith('/props'):
            return self._json(200, {
                'default_generation_settings': {'n_ctx': STATE.n_ctx},
                'total_slots': 1, 'model_path': 'mock.gguf'})
        if self.path.endswith('/health'):
            return self._json(200, {'status': 'ok'})
        if self.path.endswith('/models'):
            return self._json(200, {'object': 'list', 'data': [
                {'id': 'mock-model', 'object': 'model'}]})
        return self._json(404, {'error': 'not found'})

    def do_POST(self):
        n = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(n) if n else b''
        try:
            body = json.loads(raw or b'{}')
        except ValueError:
            body = {'_unparsable': raw[:400].decode('utf-8', 'replace')}
        STATE.record(self.path, body, dict(self.headers))

        if self.path.endswith('/apply-template'):
            msgs = body.get('messages') or []
            text = '\n'.join(f"<|im_start|>{m.get('role')}\n{m.get('content')}"
                             f"<|im_end|>" for m in msgs if isinstance(m, dict))
            if body.get('tools'):
                text += '\n' + json.dumps(body['tools'], ensure_ascii=False)
            return self._json(200, {'prompt': text})
        if self.path.endswith('/tokenize'):
            content = body.get('content') or ''
            # A stable, deterministic stand-in: 4 chars per token.
            return self._json(200, {'tokens': list(range(max(1, len(content) // 4)))})
        if not self.path.endswith('/chat/completions'):
            return self._json(404, {'error': 'not found'})

        turn = STATE.next_turn()
        kind = turn.get('kind')

        if kind == 'fail':
            data = (turn.get('body') or '{"error":"injected failure"}').encode()
            self.send_response(int(turn.get('status', 500)))
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if kind == 'garbage':
            data = b'not-json-and-not-sse'
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        # A non-streaming client gets the same scripted turn, folded into one
        # JSON completion. This is what lets an offline test exercise the REAL
        # request path (`stream: false`, one HTTP call, one response object)
        # instead of a stubbed-out client. The failure turns above are handled
        # first, so a non-streaming client sees the failure too.
        if body.get('stream') is False:
            payload = sse_to_completion(_scripted_sse(turn))
            return self._json(200, payload)

        delay = float(turn.get('delay') or STATE.first_byte_delay or 0.0)
        if delay:
            time.sleep(delay)

        self._sse_start()
        # A separate delay AFTER the headers, so the test can prove that the
        # header time and the first-content time are measured separately. A
        # server that sends headers immediately and then prefills is the real
        # llama-server behaviour; conflating the two is the exact mistake the
        # brief calls out.
        content_delay = float(turn.get('content_delay') or 0.0)
        if content_delay:
            time.sleep(content_delay)
        if turn.get('kind') == 'tool_call':
            calls = turn.get('calls') or []
            self._write_chunk(_sse(_chunk({'role': 'assistant', 'content': None})))
            for i, c in enumerate(calls):
                # `id` is sent only when the scenario supplies one: some
                # OpenAI-compatible servers omit it, and that case has to be
                # testable (it is what forces positional pairing).
                head = {'index': i, 'type': 'function',
                        'function': {'name': c['name'], 'arguments': ''}}
                if c.get('id'):
                    head['id'] = c['id']
                self._write_chunk(_sse(_chunk({'tool_calls': [head]})))
                # Arguments arrive in fragments, like the real wire format.
                args = c.get('arguments') or ''
                for k in range(0, len(args), 6):
                    self._write_chunk(_sse(_chunk({'tool_calls': [{
                        'index': i, 'function': {'arguments': args[k:k + 6]}}]})))
            self._write_chunk(_sse(_chunk({}, finish='tool_calls', usage={
                'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120})))
            self._write_chunk(b'data: [DONE]\n\n')
            return self._end_chunked()

        if kind == 'interrupt':
            text = turn.get('text') or '部分'
            after = int(turn.get('after') or 2)
            self._write_chunk(_sse(_chunk({'role': 'assistant', 'content': None})))
            for i, ch in enumerate(text):
                if i >= after:
                    # Abruptly drop the connection mid-stream: no finish chunk,
                    # no [DONE]. The client must still have a usable record.
                    try:
                        self.wfile.write(b'')   # force a flush of what is there
                        self.close_connection = True
                        self.connection.close()
                    except Exception:  # noqa: BLE001
                        pass
                    return
                self._write_chunk(_sse(_chunk({'content': ch})))
            return self._end_chunked()

        if kind == 'partial_error':
            text = turn.get('text') or '部分内容'
            chunks = turn.get('chunks')
            after = int(turn.get('after') or (len(chunks) if chunks else 2))
            self._write_chunk(_sse(_chunk({'role': 'assistant', 'content': None})))
            if chunks is not None:
                for i, c in enumerate(chunks):
                    if i >= after:
                        break
                    self._write_chunk(_sse(_chunk({'content': c})))
            else:
                for k in range(0, min(after * 8, len(text)), 8):
                    self._write_chunk(_sse(_chunk({'content': text[k:k + 8]})))
            try:
                # Force immediate TCP connection abort / reset
                self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
                self.connection.close()
            except Exception:
                pass
            return

        # default: a plain text answer
        text = turn.get('text') or 'ok'
        if turn.get('on_start'):
            turn['on_start']()
        if turn.get('pause_ev') and turn.get('resume_ev'):
            turn['pause_ev'].set()
            turn['resume_ev'].wait(timeout=10.0)
        self._write_chunk(_sse(_chunk({'role': 'assistant', 'content': None})))
        chunks = turn.get('chunks')
        if chunks is not None:
            for c in chunks:
                if turn.get('chunk_delay'):
                    time.sleep(turn['chunk_delay'])
                self._write_chunk(_sse(_chunk({'content': c})))
        else:
            for k in range(0, len(text), 8):
                if turn.get('chunk_delay'):
                    time.sleep(turn['chunk_delay'])
                self._write_chunk(_sse(_chunk({'content': text[k:k + 8]})))
        if turn.get('no_usage'):
            self._write_chunk(_sse(_chunk({}, finish='stop', usage=None)))
        else:
            prompt_toks = turn.get('prompt_tokens', 100)
            comp_toks = turn.get('completion_tokens', len(text) // 2)
            self._write_chunk(_sse(_chunk({}, finish='stop', usage={
                'prompt_tokens': prompt_toks, 'completion_tokens': comp_toks,
                'total_tokens': prompt_toks + comp_toks})))
        self._write_chunk(b'data: [DONE]\n\n')
        self._end_chunked()


def _scripted_sse(turn: dict) -> str:
    """Render one scripted turn as an SSE body, for folding or streaming."""
    if turn.get('kind') == 'tool_call':
        chunks = [_chunk({'role': 'assistant', 'content': None})]
        for i, c in enumerate(turn.get('calls') or []):
            head = {'index': i, 'type': 'function',
                    'function': {'name': c['name'], 'arguments': ''}}
            if c.get('id'):
                head['id'] = c['id']
            chunks.append(_chunk({'tool_calls': [head]}))
            args = c.get('arguments') or ''
            for k in range(0, len(args), 6):
                chunks.append(_chunk({'tool_calls': [
                    {'index': i, 'function': {'arguments': args[k:k + 6]}}]}))
        chunks.append(_chunk({}, finish='tool_calls', usage={
            'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120}))
    elif turn.get('kind') == 'interrupt':
        chunks = [_chunk({'role': 'assistant', 'content': None})]
        for ch in (turn.get('text') or '部分')[:int(turn.get('after') or 2)]:
            chunks.append(_chunk({'content': ch}))
    else:
        text = turn.get('text') or 'ok'
        chunks = [_chunk({'role': 'assistant', 'content': None})]
        for k in range(0, len(text), 8):
            chunks.append(_chunk({'content': text[k:k + 8]}))
        chunks.append(_chunk({}, finish='stop', usage={
            'prompt_tokens': 100, 'completion_tokens': len(text) // 2,
            'total_tokens': 100 + len(text) // 2}))
    return ''.join(_sse(c).decode('utf-8') for c in chunks) + 'data: [DONE]\n\n'


def sse_to_completion(raw: str) -> dict:
    """Fold a streamed SSE body into the non-streaming completion shape.

    The round-12 generation path deliberately uses `stream: false`, so a test
    that wants to exercise it against this scripted service has to hand the
    client a single JSON object. This is a test-side adapter, not a change to
    the service's wire format: the service still streams exactly as before.
    """
    content = ''
    finish = None
    usage = None
    model = None
    cid = None
    for line in (raw or '').splitlines():
        if not line.startswith('data: '):
            continue
        data = line[6:].strip()
        if data == '[DONE]':
            break
        try:
            obj = json.loads(data)
        except ValueError:
            continue
        cid = cid or obj.get('id')
        model = model or obj.get('model')
        for ch in (obj.get('choices') or []):
            delta = ch.get('delta') or {}
            if delta.get('content'):
                content += delta['content']
            if ch.get('finish_reason'):
                finish = ch['finish_reason']
            if obj.get('usage'):
                usage = obj['usage']
    if finish is None:
        finish = 'stop'
    if usage is None:
        usage = {'prompt_tokens': 100, 'completion_tokens': len(content) // 2,
                 'total_tokens': 100 + len(content) // 2}
    return {'id': cid or 'chatcmpl-mock', 'object': 'chat.completion',
            'created': 0, 'model': model or 'mock-model',
            'choices': [{'index': 0, 'finish_reason': finish,
                         'message': {'role': 'assistant', 'content': content}}],
            'usage': usage}


def free_port() -> int:
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


class MockServer:
    """Start / stop a scripted model server on a free loopback port."""

    def __init__(self, scenario: list[dict] | None = None, n_ctx: int = 4096):
        global STATE
        STATE = ServerState(scenario or [], n_ctx=n_ctx)
        self.port = free_port()
        self.base_url = f'http://127.0.0.1:{self.port}/v1'
        self.root = f'http://127.0.0.1:{self.port}'
        self._httpd = ThreadingHTTPServer(('127.0.0.1', self.port), Handler)
        self._httpd.daemon_threads = True
        self._thread: Optional[threading.Thread] = None

    def __enter__(self) -> 'MockServer':
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        time.sleep(0.15)
        return self

    def __exit__(self, *exc) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    @property
    def state(self) -> ServerState:
        return STATE


if __name__ == '__main__':
    # Manual use: a server that answers with one list_dir tool call, then text.
    with MockServer([
        {'kind': 'tool_call', 'calls': [{'name': 'list_dir',
                                         'arguments': '{"path":"."}'}]},
        {'kind': 'text', 'text': '完成。'},
    ]) as s:
        print(f'mock model server: {s.base_url}')
        print('Ctrl+C to stop')
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
