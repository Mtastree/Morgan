"""Round 50: one global persona file that the chat request can inject.

## Why a file and not a database row

The user asked to be able to write a short Markdown file by hand and have the
chat use it. So the file IS the content: `configs/persona.md`. The page editor
writes exactly that file and reads exactly that file. There is no second copy
that could drift.

## What is stored where

    configs/persona.md         the persona body, UTF-8, plain Markdown
    configs/persona_state.json {"enabled": bool, "updated_at": ...}

Two files, two different concerns. The body stays a file the user can edit in
Notepad; the switch stays machine state. Both are written with
temp+fsync+`os.replace`, so an interrupted write cannot leave half a file.

## The rules this module keeps

* **Read once per request.** `snapshot()` is called at the top of each generation
  and its result is what that request uses. Editing the file mid-generation
  therefore cannot change a request already in flight.
* **Manual edits win.** Nothing caches the body across requests. If the user
  edits `persona.md` in Notepad, the next request reads the new bytes.
* **Empty or disabled means no block at all.** An empty persona does not become
  an empty system message.
* **Failures are reported, never swallowed.** A read/decode/save failure returns
  an explicit error string; `snapshot()` returns `ok=False` and the request is
  refused rather than silently going out without the persona the user turned on.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

LITE = Path(__file__).resolve().parents[1]
PROJECT = LITE.parents[1]

DEFAULT_PERSONA_PATH = PROJECT / 'configs' / 'persona.md'
DEFAULT_STATE_PATH = PROJECT / 'configs' / 'persona_state.json'

#: The header that makes the block identifiable on the wire and tells the model
#: how it ranks against the user's explicit, current-turn requirements. Kept
#: short on purpose: the brief forbids piling on rules.
BLOCK_HEADER = '【聊天人格】'
BLOCK_NOTE = ('（以下为用户设定的交流风格，只影响说话方式，不改变上面的应用规则。'
              '用户在当前这条消息里明确提出长度、格式、详细程度等要求时，以用户要求为准。）')

#: A sanity ceiling. The real limit is the context budget, which is checked with
#: the model's own tokenizer during assembly; this only stops a pathological file
#: (a pasted book) from being loaded into memory on every request.
MAX_PERSONA_CHARS = 20000


class PersonaError(Exception):
    """A persona read/write failure the caller must report, not hide."""

    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra

    def to_dict(self) -> dict:
        out = {'ok': False, 'error': self.code, 'message': self.message}
        out.update(self.extra)
        return out


def persona_path() -> Path:
    """`HERMES_PERSONA_PATH` exists so a test can use its own file."""
    override = os.environ.get('HERMES_PERSONA_PATH')
    return Path(override).resolve() if override else DEFAULT_PERSONA_PATH


def state_path() -> Path:
    override = os.environ.get('HERMES_PERSONA_STATE_PATH')
    return Path(override).resolve() if override else DEFAULT_STATE_PATH


def now_iso() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S%z')


def _atomic_write_text(path: Path, text: str) -> None:
    """temp + fsync + os.replace. Never leaves a half-written persona."""
    p = Path(path).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.parent / f'{p.name}.tmp_{os.getpid()}_{time.time_ns()}'
    try:
        with tmp.open('w', encoding='utf-8', newline='\n') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# Switch state
# --------------------------------------------------------------------------- #
def read_state() -> dict:
    """The persisted switch. Missing file = disabled, which is the shipped state."""
    p = state_path()
    out = {'enabled': False, 'updated_at': None, 'path': str(p), 'exists': p.is_file(),
           'error': None}
    if not p.is_file():
        return out
    try:
        raw = json.loads(p.read_text(encoding='utf-8-sig'))
    except Exception as exc:  # noqa: BLE001
        out['error'] = f'人格开关状态文件无法解析（{type(exc).__name__}: {exc}）'
        return out
    if not isinstance(raw, dict):
        out['error'] = '人格开关状态文件不是对象'
        return out
    out['enabled'] = bool(raw.get('enabled'))
    out['updated_at'] = raw.get('updated_at')
    return out


def set_enabled(enabled: bool) -> dict:
    payload = {'enabled': bool(enabled), 'updated_at': now_iso()}
    try:
        _atomic_write_text(state_path(), json.dumps(payload, ensure_ascii=False,
                                                    indent=2))
    except OSError as exc:
        raise PersonaError('state_write_failed',
                           f'人格开关保存失败：{type(exc).__name__}: {exc}',
                           path=str(state_path())) from None
    return {'ok': True, 'enabled': bool(enabled), 'updated_at': payload['updated_at'],
            'path': str(state_path())}


# --------------------------------------------------------------------------- #
# Body
# --------------------------------------------------------------------------- #
def read_body() -> str:
    """The persona body exactly as it is on disk.

    A missing file is NOT an error for READING (a fresh install has none yet);
    it reads as an empty body. A file that exists but cannot be read or decoded
    IS an error: claiming "empty persona" would hide a real problem.
    """
    p = persona_path()
    if not p.is_file():
        return ''
    try:
        raw = p.read_text(encoding='utf-8')
    except UnicodeDecodeError as exc:
        raise PersonaError(
            'persona_decode_failed',
            (f'人格文件不是有效的 UTF-8，无法读取：{p}（{exc.reason} 于字节 {exc.start}）。'
             '请用 UTF-8 编码保存该文件。'),
            path=str(p)) from None
    except OSError as exc:
        raise PersonaError('persona_read_failed',
                           f'人格文件读取失败：{p}（{type(exc).__name__}: {exc}）',
                           path=str(p)) from None
    return raw


def write_body(text: str) -> dict:
    """Save the body. The caller's text is stored verbatim (no trimming).

    Round 51: a read-back that fails AFTER the bytes are on disk is reported as
    `wrote=True, verified=False`, not as a plain failure. The page must be able
    to say "written, but could not confirm" instead of "not written" -- telling
    the user a save failed when the file actually changed is its own bug, and it
    would make them type it again over what is already there.
    """
    if not isinstance(text, str):
        raise PersonaError('persona_invalid', '人格正文必须是文本。')
    if len(text) > MAX_PERSONA_CHARS:
        raise PersonaError(
            'persona_too_long',
            f'人格正文过长：{len(text)} 字符，上限 {MAX_PERSONA_CHARS}。',
            chars=len(text), limit=MAX_PERSONA_CHARS)
    p = persona_path()
    try:
        _atomic_write_text(p, text)
    except OSError as exc:
        raise PersonaError('persona_write_failed',
                           f'人格文件保存失败：{p}（{type(exc).__name__}: {exc}）',
                           path=str(p), wrote=False) from None
    # Verify by reading back: "saved" must mean the bytes are really there.
    try:
        back = p.read_text(encoding='utf-8')
    except Exception as exc:  # noqa: BLE001
        raise PersonaError('persona_verify_failed',
                           f'人格正文已写入，但读回校验失败：{type(exc).__name__}: {exc}',
                           path=str(p), wrote=True, verified=False,
                           chars=len(text)) from None
    if back != text:
        raise PersonaError(
            'persona_verify_mismatch',
            '人格正文已写入，但读回内容与保存内容不一致（可能有其他程序同时修改该文件）。',
            path=str(p), wrote=True, verified=False,
            stored_chars=len(back), sent_chars=len(text))
    return {'ok': True, 'path': str(p), 'chars': len(text),
            'bytes': p.stat().st_size, 'saved_at': now_iso(),
            'wrote': True, 'verified': True}


def view() -> dict:
    """Everything the page needs: body, switch, counts, paths, errors."""
    st = read_state()
    out: dict[str, Any] = {
        'ok': True,
        'enabled': st['enabled'],
        'state_updated_at': st['updated_at'],
        'path': str(persona_path()),
        'state_path': str(st['path']),
        'body': '',
        'chars': 0,
        'bytes': 0,
        'exists': persona_path().is_file(),
        'max_chars': MAX_PERSONA_CHARS,
        'block_header': BLOCK_HEADER,
        'error': None,
    }
    if st.get('error'):
        out['ok'] = False
        out['error'] = st['error']
    try:
        body = read_body()
    except PersonaError as exc:
        out['ok'] = False
        out['error'] = exc.message
        out['error_code'] = exc.code
        return out
    out['body'] = body
    out['chars'] = len(body)
    out['bytes'] = len(body.encode('utf-8'))
    return out


# --------------------------------------------------------------------------- #
# The thing the request actually uses
# --------------------------------------------------------------------------- #
def render_block(body: str) -> str:
    """Wrap the body as one explicit system paragraph. Empty in, empty out."""
    text = (body or '').strip()
    if not text:
        return ''
    return f'{BLOCK_HEADER}\n{BLOCK_NOTE}\n\n{text}'


def snapshot() -> dict:
    """ONE read per request. The caller must use the returned text and no other.

    Returns:
        ok        bool  -- False means "refuse the request", not "send without"
        enabled   bool
        block     str   -- '' when disabled or when the body is empty
        chars     int   -- characters of the body as read
        error     str|None
    """
    st = read_state()
    if st.get('error'):
        return {'ok': False, 'enabled': bool(st.get('enabled')), 'block': '',
                'chars': 0, 'error': st['error'], 'error_code': 'state_corrupt'}
    if not st.get('enabled'):
        return {'ok': True, 'enabled': False, 'block': '', 'chars': 0,
                'error': None, 'path': str(persona_path())}
    try:
        body = read_body()
    except PersonaError as exc:
        return {'ok': False, 'enabled': True, 'block': '', 'chars': 0,
                'error': exc.message, 'error_code': exc.code,
                'path': str(persona_path())}
    block = render_block(body)
    return {'ok': True, 'enabled': True, 'block': block, 'chars': len(body.strip()),
            'error': None, 'path': str(persona_path()),
            'empty_body': not block}
