"""Round 16: the chat store (SQLite). Sessions, messages, memories, counters.

## Why a separate data directory

The Agent experiments live in `logs/trajectories/` (JSON per run, frozen, never
edited). Chat is a different product with different rules: rows get updated as a
generation streams in. Mixing the two would put mutable chat rows next to
immutable experiment evidence. So chat gets its own directory and its own file:

    <project>/src/hermes-lite/chatdata/chat.sqlite3

`HERMES_LITE_CHAT_DATA` can point it elsewhere (the tests use a temp dir).

## Message states

    streaming    being generated right now; content grows; NOT a finished reply
    complete     the generation ended normally; content is what the model sent
    interrupted  stopped by the user, or the app closed mid-generation
    error        the request failed; content holds whatever arrived before it

`streaming` never survives a restart: `recover()` turns any leftover into
`interrupted` and does NOT resend. That is the honest record of "we lost the rest
of that reply", not a silent retry.

## Why an old request cannot corrupt a new one

Every write that advances a generation carries three predicates:

    WHERE id = ? AND session_id = ? AND request_id = ? AND status = 'streaming'

So a stale worker thread (a `stop` and a fresh send, or a second session opened
while the first is still finalising) updates zero rows instead of overwriting
somebody else's text, and it can never write into a different session.

## Duplicate protection

The page sends a `client_msg_id` per composed message. It is stored as the user
message id, so a refresh-and-resend, a double click, or a retried POST maps onto
the SAME row (`INSERT ... ON CONFLICT DO NOTHING` then read back).
"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from hermes_lite import chatparams

LITE = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = LITE / 'chatdata'

SCHEMA_VERSION = 2

# The two strength values, duplicated here as plain strings so `upsert_memory`
# does not have to import `chatmem` (the store must stay usable on its own).
STRENGTH_EXPLICIT_STORE = 'explicit'
STRENGTH_TENTATIVE_STORE = 'tentative'

PREF_OPPOSITES_STORE = {
    'brevity': 'detail', 'detail': 'brevity',
    'examples': 'no_examples', 'no_examples': 'examples',
    'formal': 'casual', 'casual': 'formal',
}

DEFAULT_SESSION_SETTINGS = {
    'style': 'standard',         # 'concise' | 'standard' | 'detailed'
    'history_mode': 'auto',       # 'auto' | 'current_only' | 'custom'
    'custom_budget': 2048,        # integer tokens
    'max_tokens': 512,            # 128 | 512 | 1024
    'use_memory': True,           # bool
    'learn_memory': True,         # bool
    'web_search': False,          # bool, default False for manual web search assistance
}

def sanitize_session_settings(raw: Optional[dict]) -> dict:
    """Validate and normalize session-specific settings against defined contracts.

    Round 41: the accepted ranges for `max_tokens` and `custom_budget` come from
    `chatparams`, which derives them from the ACTUAL service window instead of a
    fixed enum. `sanitize` keeps its old lenient role (it is used when reading
    stored rows, where a bad value must not crash the page), so an out-of-range
    stored value falls back to the default here; the strict, user-facing
    rejection happens in `validate_settings_patch()` below, which is what the
    settings endpoint calls.
    """
    raw = raw or {}
    out = dict(DEFAULT_SESSION_SETTINGS)

    style = raw.get('style')
    if style in ('concise', 'standard', 'detailed'):
        out['style'] = style

    mode = raw.get('history_mode')
    if mode in ('auto', 'current_only', 'custom'):
        out['history_mode'] = mode

    # Round 41: accept any integer inside the product band, not only 256..3584.
    # The upper bound that depends on the live window is checked at save time;
    # here we only apply the absolute band so a corrupt stored value cannot
    # survive sanitising as a nonsense budget.
    try:
        cb = chatparams.parse_strict_int(raw.get('custom_budget',
                                                 DEFAULT_SESSION_SETTINGS['custom_budget']),
                                         'custom_budget')
        if chatparams.MIN_CUSTOM_INPUT_BUDGET <= cb <= chatparams.ABSOLUTE_MAX_CUSTOM_BUDGET:
            out['custom_budget'] = cb
    except (chatparams.ParamError, ValueError, TypeError):
        pass

    # Round 41: any integer in band, not just the three legacy presets. Values
    # like 768 are now legal; the three presets remain the UI defaults.
    try:
        mt = chatparams.parse_strict_int(raw.get('max_tokens',
                                                 DEFAULT_SESSION_SETTINGS['max_tokens']),
                                         'max_tokens')
        if chatparams.MIN_MAX_OUTPUT_TOKENS <= mt <= chatparams.ABSOLUTE_MAX_OUTPUT_TOKENS:
            out['max_tokens'] = mt
    except (chatparams.ParamError, ValueError, TypeError):
        pass

    if 'use_memory' in raw:
        out['use_memory'] = bool(raw['use_memory'])
    if 'learn_memory' in raw:
        out['learn_memory'] = bool(raw['learn_memory'])
    if 'web_search' in raw:
        out['web_search'] = bool(raw['web_search'])
    if '_seq' in raw:
        try:
            out['_seq'] = int(raw['_seq'])
        except (ValueError, TypeError):
            pass

    return out


def validate_settings_patch(patch: dict, *, window: Optional[int] = None) -> dict:
    """Strict validation for a settings WRITE. Raises chatparams.ParamError.

    This is deliberately stricter than `sanitize_session_settings`: reading old
    rows must be forgiving, but accepting a write must not silently replace what
    the user asked for with something else.
    """
    patch = patch or {}
    out: dict = {}

    if 'style' in patch:
        if patch['style'] not in ('concise', 'standard', 'detailed'):
            raise chatparams.ParamError(
                'invalid_style', f'未知的回答方式：{patch["style"]}')
        out['style'] = patch['style']

    if 'history_mode' in patch:
        if patch['history_mode'] not in ('auto', 'current_only', 'custom'):
            raise chatparams.ParamError(
                'invalid_history_mode', f'未知的历史携带方式：{patch["history_mode"]}')
        out['history_mode'] = patch['history_mode']

    if 'max_tokens' in patch:
        out['max_tokens'] = chatparams.validate_max_output(patch['max_tokens'],
                                                          window=window)

    if 'custom_budget' in patch:
        # Window-dependent upper bound is checked later, once the reply reserve
        # of the same patch is known; here only the absolute floor applies.
        out['custom_budget'] = chatparams.validate_custom_budget(patch['custom_budget'])

    for key in ('use_memory', 'learn_memory', 'web_search'):
        if key in patch:
            if not isinstance(patch[key], bool):
                raise chatparams.ParamError(
                    f'{key}_not_bool', f'{key} 必须是布尔值。')
            out[key] = patch[key]

    return out

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id         TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    -- A one-line note the page shows when this session lost its tail reply.
    note       TEXT,
    settings_json   TEXT DEFAULT '{}',
    boundary_msg_id TEXT,
    boundary_seq    INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    id         TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL,
    role       TEXT NOT NULL,          -- user | assistant
    content    TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL,          -- streaming | complete | interrupted | error
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    request_id TEXT,                   -- which generation produced/updates this row
    model      TEXT,                   -- serving alias actually used
    -- Round 43: which model CONFIGURATION produced this row. NULL means the row
    -- predates profiles (a historical record). It is NEVER back-filled from
    -- whatever model happens to be loaded later: an old answer must not acquire
    -- a new model's identity.
    profile_id TEXT,
    tokens     INTEGER,                -- cached /tokenize count of `content`
    usage_json TEXT,
    error      TEXT,
    -- The operation sequence number allocated when this message was RECEIVED.
    -- A result derived from this message may only be applied while its version
    -- is newer than the state it would overwrite.
    version    INTEGER NOT NULL DEFAULT 0,
    -- Which user message this assistant message was generated in response to.
    reply_to_id TEXT,
    UNIQUE(session_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, seq);

CREATE TABLE IF NOT EXISTS memories (
    id                TEXT PRIMARY KEY,
    kind              TEXT NOT NULL,   -- preference | address | background
    key               TEXT NOT NULL,   -- brevity | conclusion_first | examples ...
    content           TEXT NOT NULL,
    strength          TEXT NOT NULL,   -- explicit | tentative
    enabled           INTEGER NOT NULL DEFAULT 1,
    origin            TEXT NOT NULL,   -- rule | model | user
    source_session_id TEXT,
    source_message_id TEXT,
    evidence_quote    TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    deleted_at        TEXT,
    deleted_reason    TEXT,
    -- The operation sequence number of the last change to this row. 0 means
    -- "written by a build before operation order existed" and is treated as
    -- "unknown", which the guards read as "do not overwrite" (see `_guard`).
    version           INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_memories_live
    ON memories(deleted_at, enabled, kind);

-- One memory can be supported by several sessions. Deleting a session removes
-- only ITS sources; a memory that still has another source survives.
CREATE TABLE IF NOT EXISTS memory_sources (
    memory_id  TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    quote      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (memory_id, message_id)
);

-- Deleted/stopped content must not come back from an old chat. A tombstone
-- records the exact user message that produced the memory, so re-processing that
-- same message cannot resurrect it.
CREATE TABLE IF NOT EXISTS tombstones (
    kind       TEXT NOT NULL,
    key        TEXT NOT NULL,
    message_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    -- The operation sequence number of the delete/revoke that wrote it. A later
    -- explicit statement from a NEWER message is allowed to re-establish; an
    -- older source is not.
    version    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (kind, key, message_id)
);

-- Every memory change, so the report can say what happened and why.
CREATE TABLE IF NOT EXISTS memory_events (
    id         TEXT PRIMARY KEY,
    at         TEXT NOT NULL,
    session_id TEXT,
    message_id TEXT,
    memory_id  TEXT,
    action     TEXT NOT NULL,          -- create|update|disable|enable|delete|skip
    detail     TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS counters (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    chat_requests       INTEGER NOT NULL DEFAULT 0,
    extraction_requests INTEGER NOT NULL DEFAULT 0,
    aux_requests        INTEGER NOT NULL DEFAULT 0,
    failed_requests     INTEGER NOT NULL DEFAULT 0,
    updated_at          TEXT NOT NULL DEFAULT ''
);

-- ---------------------------------------------------------------------------
-- Operation order (the fix for "a late result overwrites a manual action").
--
-- A single monotonically increasing sequence covers EVERY event that can change
-- a memory's state, whatever its source:
--
--     a user message being received      -> the message carries `version`
--     a manual edit / enable / disable   -> the memory carries `version`
--     a manual delete                    -> the tombstone carries `version`
--     a tentative memory being confirmed -> the memory carries `version`
--     a user instruction revoking one    -> the tombstone carries `version`
--     a background task starting         -> the task carries `start_version`
--
-- Comparing only the ORDER OF MESSAGES is not enough: "the source message is
-- newer than the one that created the memory" does NOT imply "the source message
-- arrived after the user's latest action", because the message can be received
-- first and PROCESSED later. Every write therefore compares versions drawn from
-- this one sequence, and a result whose source version is not newer than the
-- current state's version is rejected instead of applied.
--
-- `op_seq` is an AUTOINCREMENT rowid, so it survives a restart and is never
-- reused. Nothing else in the product allocates from it.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS op_seq (
    seq       INTEGER PRIMARY KEY AUTOINCREMENT,
    at        TEXT NOT NULL,
    kind      TEXT NOT NULL,     -- user_message | manual_* | revoke | confirm | task
    subject   TEXT NOT NULL DEFAULT ''   -- id of the row the event is about
);

-- One row per (kind, key) mutation attempt, for auditing what was rejected and
-- why. It is the evidence trail behind "the user's edit survived".
CREATE TABLE IF NOT EXISTS memory_ops (
    id         TEXT PRIMARY KEY,
    seq        INTEGER NOT NULL,
    at         TEXT NOT NULL,
    kind       TEXT NOT NULL,
    key        TEXT NOT NULL,
    source_version INTEGER,
    target_version INTEGER,
    action     TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT ''
);

-- One row per HTTP request this product actually issued. The point is
-- verifiability: "the chat request carried no tools" is a stored fact about the
-- body that went on the wire, not a claim about the code.
CREATE TABLE IF NOT EXISTS request_log (
    id                TEXT PRIMARY KEY,
    at                TEXT NOT NULL,
    kind              TEXT NOT NULL,   -- chat | extraction | aux
    url               TEXT NOT NULL,
    session_id        TEXT,
    message_id        TEXT,
    request_id        TEXT,
    status            INTEGER,
    ok                INTEGER NOT NULL DEFAULT 0,
    has_tools         INTEGER NOT NULL DEFAULT 0,
    has_skill_text    INTEGER NOT NULL DEFAULT 0,
    body_chars        INTEGER NOT NULL DEFAULT 0,
    body_sha256       TEXT NOT NULL DEFAULT '',
    message_count     INTEGER NOT NULL DEFAULT 0,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    elapsed_s         REAL,
    finish_reason     TEXT,
    error             TEXT,
    -- The whole body of chat/extraction requests, so "no tools were sent" can be
    -- checked against what actually went on the wire instead of against the code.
    body_json         TEXT
);
CREATE INDEX IF NOT EXISTS idx_request_log_at ON request_log(at);

CREATE TABLE IF NOT EXISTS session_summaries (
    id                TEXT PRIMARY KEY,
    session_id        TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    summary_seq       INTEGER NOT NULL DEFAULT 1,
    is_active         INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL DEFAULT 'ready',
    summary_json      TEXT NOT NULL DEFAULT '{}',
    summary_text      TEXT NOT NULL DEFAULT '',
    start_msg_id      TEXT,
    end_msg_id        TEXT,
    start_seq         INTEGER DEFAULT 0,
    end_seq           INTEGER DEFAULT 0,
    source_turn_count INTEGER NOT NULL DEFAULT 0,
    recent_turns_kept INTEGER NOT NULL DEFAULT 3,
    boundary_seq      INTEGER NOT NULL DEFAULT 0,
    instance_seq      INTEGER NOT NULL DEFAULT 0,
    input_tokens      INTEGER,
    output_tokens     INTEGER,
    tokens_saved      INTEGER DEFAULT 0,
    error             TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_summaries_session ON session_summaries(session_id);

CREATE TABLE IF NOT EXISTS web_search_results (
    id                TEXT PRIMARY KEY,
    session_id        TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    message_id        TEXT,
    request_id        TEXT NOT NULL,
    query             TEXT NOT NULL,
    status            TEXT NOT NULL,   -- ready | error | empty | refused
    provider          TEXT NOT NULL DEFAULT 'ddgs',
    backend           TEXT,            -- duckduckgo | yandex | wikipedia
    client_version    TEXT,            -- ddgs==9.16.1
    attempts_json     TEXT DEFAULT '[]', -- detailed attempts log
    fallback_reason   TEXT,            -- fallback explanation
    raw_results_json  TEXT DEFAULT '[]', -- raw results before budget trimming
    results_json      TEXT NOT NULL DEFAULT '[]', -- fitted results sent to model
    trimmed_count     INTEGER DEFAULT 0,
    tokens_used       INTEGER DEFAULT 0,
    elapsed_s         REAL DEFAULT 0.0,
    error             TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_web_search_session ON web_search_results(session_id);
CREATE INDEX IF NOT EXISTS idx_web_search_message ON web_search_results(message_id);
CREATE INDEX IF NOT EXISTS idx_web_search_request ON web_search_results(request_id);
"""


def now() -> str:
    """Local time with an offset, second resolution. Stable and sortable."""
    return datetime.now().astimezone().isoformat(timespec='seconds')


def new_id(prefix: str) -> str:
    return f'{prefix}-{uuid.uuid4().hex[:20]}'


def data_dir() -> Path:
    override = os.environ.get('HERMES_LITE_CHAT_DATA')
    if override:
        return Path(override)
    return DEFAULT_DATA_DIR


class ChatStore:
    """Thin, explicit wrapper. No ORM, no migration framework.

    `check_same_thread=False` plus one connection per call would be simplest, but
    the page streams while background threads record counters. A single
    connection guarded by a lock keeps write ordering deterministic and avoids
    SQLITE_BUSY entirely on this workload (one user, one generation at a time).
    """

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else (data_dir() / 'chat.sqlite3')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._tx_depth = 0
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False,
                                     timeout=15.0)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.execute(
                'INSERT OR IGNORE INTO counters (id, updated_at) VALUES (1, ?)',
                (now(),))
            self._conn.execute(
                'INSERT INTO meta (key, value) VALUES (?, ?) '
                'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                ('schema_version', str(SCHEMA_VERSION)))
            self._conn.commit()
        self.recovered = self.recover()

    def _migrate(self) -> None:
        """Add columns that a database written by an earlier build lacks.

        `CREATE TABLE IF NOT EXISTS` does nothing to an existing table, so a new
        column has to be added explicitly. Only additive changes are handled: this
        is a local single-user file, not a service with rolling upgrades.

        The production database is never dropped or rebuilt -- it is migrated in
        place, and the new `version` columns default to 0, which the guards read
        as "unknown" and refuse to overwrite (see `_guard`).
        """
        wanted = {
            'request_log': {'body_json': 'TEXT',
                            'profile_id': 'TEXT',
                            'instance_seq': 'INTEGER',
                            'model_alias': 'TEXT'},
            'messages': {'version': 'INTEGER NOT NULL DEFAULT 0',
                         'reply_to_id': 'TEXT',
                         'profile_id': 'TEXT'},
            'memories': {'version': 'INTEGER NOT NULL DEFAULT 0'},
            'tombstones': {'version': 'INTEGER NOT NULL DEFAULT 0'},
            'sessions': {'settings_json': "TEXT DEFAULT '{}'",
                         'boundary_msg_id': 'TEXT',
                         'boundary_seq': 'INTEGER DEFAULT 0'},
        }
        for table, cols in wanted.items():
            have = {r[1] for r in self._conn.execute(
                f'PRAGMA table_info({table})').fetchall()}
            for name, decl in cols.items():
                if name not in have:
                    self._conn.execute(f'ALTER TABLE {table} ADD COLUMN {name} {decl}')
        self._conn.execute(
            'CREATE INDEX IF NOT EXISTS idx_messages_reply_to ON messages(session_id, reply_to_id)')
        self._conn.execute('''
CREATE TABLE IF NOT EXISTS session_summaries (
    id                TEXT PRIMARY KEY,
    session_id        TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    summary_seq       INTEGER NOT NULL DEFAULT 1,
    is_active         INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL DEFAULT 'ready',
    summary_json      TEXT NOT NULL DEFAULT '{}',
    summary_text      TEXT NOT NULL DEFAULT '',
    start_msg_id      TEXT,
    end_msg_id        TEXT,
    start_seq         INTEGER DEFAULT 0,
    end_seq           INTEGER DEFAULT 0,
    source_turn_count INTEGER NOT NULL DEFAULT 0,
    recent_turns_kept INTEGER NOT NULL DEFAULT 3,
    boundary_seq      INTEGER NOT NULL DEFAULT 0,
    instance_seq      INTEGER NOT NULL DEFAULT 0,
    input_tokens      INTEGER,
    output_tokens     INTEGER,
    tokens_saved      INTEGER DEFAULT 0,
    error             TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
)''')
        self._conn.execute('CREATE INDEX IF NOT EXISTS idx_summaries_session ON session_summaries(session_id)')
        self._conn.execute('''
CREATE TABLE IF NOT EXISTS web_search_results (
    id                TEXT PRIMARY KEY,
    session_id        TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    message_id        TEXT,
    request_id        TEXT NOT NULL,
    query             TEXT NOT NULL,
    status            TEXT NOT NULL,
    provider          TEXT NOT NULL DEFAULT 'ddgs',
    backend           TEXT,
    client_version    TEXT,
    attempts_json     TEXT DEFAULT '[]',
    fallback_reason   TEXT,
    raw_results_json  TEXT DEFAULT '[]',
    results_json      TEXT NOT NULL DEFAULT '[]',
    trimmed_count     INTEGER DEFAULT 0,
    tokens_used       INTEGER DEFAULT 0,
    elapsed_s         REAL DEFAULT 0.0,
    error             TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
)''')
        self._conn.execute('CREATE INDEX IF NOT EXISTS idx_web_search_session ON web_search_results(session_id)')
        self._conn.execute('CREATE INDEX IF NOT EXISTS idx_web_search_message ON web_search_results(message_id)')
        self._conn.execute('CREATE INDEX IF NOT EXISTS idx_web_search_request ON web_search_results(request_id)')
        # Migrate any missing columns in existing web_search_results tables
        existing_cols = {r[1] for r in self._conn.execute('PRAGMA table_info(web_search_results)').fetchall()}
        col_defs = [
            ('backend', 'TEXT'),
            ('client_version', 'TEXT'),
            ('attempts_json', "TEXT DEFAULT '[]'"),
            ('fallback_reason', 'TEXT'),
            ('raw_results_json', "TEXT DEFAULT '[]'"),
            ('trimmed_count', 'INTEGER DEFAULT 0'),
        ]
        for col_name, col_type in col_defs:
            if col_name not in existing_cols:
                self._conn.execute(f'ALTER TABLE web_search_results ADD COLUMN {col_name} {col_type}')
        # An existing memory row has no known operation version. Assign one that
        # is LOWER than any future message, so a brand-new explicit statement can
        # still update it, while an old source cannot silently resurrect it.
        floor = self._conn.execute(
            'SELECT COALESCE(MIN(seq), 0) FROM op_seq').fetchone()[0]
        self._conn.execute(
            'UPDATE memories SET version = ? - 1000 WHERE version = 0', (floor,))
        self._conn.execute(
            'UPDATE tombstones SET version = ? - 1000 WHERE version = 0', (floor,))

    # -- plumbing ---------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    def _exec(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, tuple(args))
            if self._tx_depth == 0:
                self._conn.commit()
            return cur

    # -- operation order ----------------------------------------------------
    def next_version(self, kind: str, subject: str = '') -> int:
        """Allocate the next operation sequence number.

        This is the ONLY writer of `op_seq`, so every comparable event in the
        system is ordered by one clock. Nothing else may invent a version.
        """
        with self._lock:
            cur = self._conn.execute(
                'INSERT INTO op_seq (at, kind, subject) VALUES (?,?,?)',
                (now(), kind, subject or ''))
            if self._tx_depth == 0:
                self._conn.commit()
            return int(cur.lastrowid)

    def current_version(self) -> int:
        row = self._one('SELECT COALESCE(MAX(seq), 0) AS m FROM op_seq')
        return int((row or {}).get('m', 0))

    @contextlib.contextmanager
    def transaction(self):
        """A protected update: check and write in the SAME critical section.

        Uses explicit transaction boundaries:
          - Outermost transaction issues BEGIN IMMEDIATE.
          - Nested transactions use SQLite SAVEPOINTs.
          - During an active transaction, helper methods (_exec, next_version)
            do NOT commit prematurely, preserving atomicity.
          - On clean exit, outer transaction commits (or inner savepoint releases).
          - On exception, outer transaction rolls back (or inner savepoint rolls back),
            restoring DB state completely.
        """
        with self._lock:
            depth = self._tx_depth
            if depth == 0:
                self._conn.execute('BEGIN IMMEDIATE')
            else:
                self._conn.execute(f'SAVEPOINT sp_{depth}')
            self._tx_depth += 1
            try:
                yield
            except BaseException:
                self._tx_depth -= 1
                if depth == 0:
                    try:
                        self._conn.rollback()
                    except sqlite3.Error:
                        pass
                else:
                    try:
                        self._conn.execute(f'ROLLBACK TO SAVEPOINT sp_{depth}')
                        self._conn.execute(f'RELEASE SAVEPOINT sp_{depth}')
                    except sqlite3.Error:
                        pass
                raise
            else:
                self._tx_depth -= 1
                if depth == 0:
                    self._conn.commit()
                else:
                    self._conn.execute(f'RELEASE SAVEPOINT sp_{depth}')

    def _all(self, sql: str, args: Iterable[Any] = ()) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(sql, tuple(args)).fetchall()
        return [dict(r) for r in rows]

    def _one(self, sql: str, args: Iterable[Any] = ()) -> Optional[dict]:
        rows = self._all(sql, args)
        return rows[0] if rows else None

    # -- restart recovery -------------------------------------------------
    def recover(self) -> dict:
        """Any `streaming` row is from a generation that died with the process.

        Marked `interrupted`, never resent, never deleted: the partial text the
        user saw stays, labelled for what it is.
        """
        leftovers = self._all(
            "SELECT id, session_id, content FROM messages WHERE status='streaming'")
        for m in leftovers:
            self._exec(
                "UPDATE messages SET status='interrupted', updated_at=?, "
                "error=COALESCE(error, '应用在生成过程中结束，已保留已收到的部分') "
                "WHERE id=? AND status='streaming'", (now(), m['id']))
        if leftovers:
            self._event(None, None, None, 'skip',
                        f'restart: {len(leftovers)} streaming message(s) -> interrupted')
        return {'interrupted_on_start': len(leftovers)}

    # -- sessions ---------------------------------------------------------
    def create_session(self, title: Optional[str] = None, settings: Optional[dict] = None,
                       model_defaults: Optional[dict] = None) -> dict:
        """Create a session.

        Round 43: a NEW session takes the CURRENT model's defaults for the reply
        reserve (and the custom budget that goes with it) at creation time, so a
        model whose profile says "default 256 tokens" produces sessions that start
        at 256. Old sessions keep their stored settings -- switching the model
        never rewrites them, which is what makes a per-session override meaningful.

        `model_defaults` is applied FIRST and then the explicit `settings` win, so
        a caller that wants specific values still gets them.
        """
        sid = new_id('s')
        ts = now()
        title = (title or '').strip() or '新对话'
        seed = dict(model_defaults or {})
        seed.update(settings or {})
        clean_settings = sanitize_session_settings(seed)
        settings_str = json.dumps(clean_settings, ensure_ascii=False)
        self._exec('INSERT INTO sessions (id, title, created_at, updated_at, settings_json, boundary_seq) '
                   'VALUES (?, ?, ?, ?, ?, 0)', (sid, title, ts, ts, settings_str))
        return self.get_session(sid)

    def get_session(self, sid: str) -> Optional[dict]:
        s = self._one('SELECT * FROM sessions WHERE id=?', (sid,))
        if s is None:
            return None
        s['message_count'] = (self._one(
            'SELECT COUNT(*) AS n FROM messages WHERE session_id=?',
            (sid,)) or {}).get('n', 0)
        raw_settings = {}
        if s.get('settings_json'):
            try:
                raw_settings = json.loads(s['settings_json'])
            except Exception:
                pass
        s['settings'] = sanitize_session_settings(raw_settings)
        if '_seq' not in s['settings']:
            s['settings']['_seq'] = 0
        return s

    def list_sessions(self) -> list[dict]:
        rows = self._all(
            'SELECT s.*, '
            ' (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) '
            '   AS message_count, '
            ' (SELECT content FROM messages m WHERE m.session_id = s.id '
            '    ORDER BY seq DESC LIMIT 1) AS last_message, '
            ' (SELECT role FROM messages m WHERE m.session_id = s.id '
            '    ORDER BY seq DESC LIMIT 1) AS last_role '
            'FROM sessions s ORDER BY s.updated_at DESC')
        for r in rows:
            raw_settings = {}
            if r.get('settings_json'):
                try:
                    raw_settings = json.loads(r['settings_json'])
                except Exception:
                    pass
            r['settings'] = sanitize_session_settings(raw_settings)
            if '_seq' not in r['settings']:
                r['settings']['_seq'] = 0
        return rows

    def get_session_settings(self, sid: str) -> dict:
        s = self._one('SELECT settings_json FROM sessions WHERE id=?', (sid,))
        if not s or not s.get('settings_json'):
            res = dict(DEFAULT_SESSION_SETTINGS)
            res['_seq'] = 0
            return res
        try:
            res = sanitize_session_settings(json.loads(s['settings_json']))
            if '_seq' not in res:
                res['_seq'] = 0
            return res
        except Exception:
            res = dict(DEFAULT_SESSION_SETTINGS)
            res['_seq'] = 0
            return res

    def update_session_settings(self, sid: str, settings: dict,
                                client_seq: Optional[int] = None,
                                base_seq: Optional[int] = None) -> dict:
        """Atomic version-checked settings update inside a BEGIN IMMEDIATE transaction."""
        settings = dict(settings or {})
        b_seq = base_seq
        if b_seq is None and 'base_seq' in settings:
            b_seq = settings.pop('base_seq')
        elif b_seq is None and '_base_seq' in settings:
            b_seq = settings.pop('_base_seq')

        c_seq = client_seq
        if c_seq is None and 'client_seq' in settings:
            c_seq = settings.pop('client_seq')
        elif c_seq is None and '_client_seq' in settings:
            c_seq = settings.pop('_client_seq')

        def _is_content_identical(s1: dict, s2: dict) -> bool:
            # `web_search` MUST be compared: without it, flipping the manual
            # search toggle looked like "no change" and the save was skipped,
            # leaving the session permanently unable to turn search on.
            keys = ('style', 'history_mode', 'custom_budget', 'max_tokens',
                    'use_memory', 'learn_memory', 'web_search')
            return all(s1.get(k) == s2.get(k) for k in keys)

        with self.transaction():
            row = self._conn.execute('SELECT settings_json FROM sessions WHERE id=?', (sid,)).fetchone()
            if row is None:
                return {'ok': False, 'error': 'not_found', 'message': '会话不存在。'}

            cur_settings = {}
            if row['settings_json']:
                try:
                    cur_settings = sanitize_session_settings(json.loads(row['settings_json']))
                except Exception:
                    cur_settings = dict(DEFAULT_SESSION_SETTINGS)
            else:
                cur_settings = dict(DEFAULT_SESSION_SETTINGS)

            cur_seq = cur_settings.get('_seq', 0)

            candidate = dict(cur_settings)
            candidate.update(settings)
            cleaned_candidate = sanitize_session_settings(candidate)

            # Protocol 1: OCC using base_seq (client's observed version)
            if b_seq is not None:
                try:
                    b_seq_int = int(b_seq)
                except (ValueError, TypeError):
                    return {'ok': False, 'error': 'invalid_base_seq', 'message': 'base_seq 必须为有效整数。'}

                if b_seq_int == cur_seq:
                    if _is_content_identical(cleaned_candidate, cur_settings):
                        return {'ok': True, 'session_id': sid, 'settings': cur_settings,
                                'current_seq': cur_seq, 'no_op': True}
                    new_seq = cur_seq + 1
                    cleaned_candidate['_seq'] = new_seq
                    str_val = json.dumps(cleaned_candidate, ensure_ascii=False)
                    self._conn.execute('UPDATE sessions SET settings_json=?, updated_at=? WHERE id=?',
                                       (str_val, now(), sid))
                    return {'ok': True, 'session_id': sid, 'settings': cleaned_candidate, 'current_seq': new_seq}
                elif b_seq_int < cur_seq:
                    if _is_content_identical(cleaned_candidate, cur_settings):
                        return {'ok': True, 'session_id': sid, 'settings': cur_settings,
                                'current_seq': cur_seq, 'duplicate': True}
                    return {'ok': False, 'error': 'version_conflict', 'session_id': sid,
                            'current_seq': cur_seq, 'base_seq': b_seq_int,
                            'settings': cur_settings,
                            'message': f'设置版本冲突（当前为第 {cur_seq} 版，提交基于第 {b_seq_int} 版），未保存。已同步最新设置。'}
                else:
                    return {'ok': False, 'error': 'future_version', 'session_id': sid,
                            'current_seq': cur_seq, 'base_seq': b_seq_int,
                            'message': f'提交的基准版本 {b_seq_int} 大于服务端当前版本 {cur_seq}。'}

            # Protocol 2: Backward-compatible client_seq target
            if c_seq is not None:
                try:
                    c_seq_int = int(c_seq)
                except (ValueError, TypeError):
                    c_seq_int = cur_seq + 1

                if c_seq_int < cur_seq:
                    if _is_content_identical(cleaned_candidate, cur_settings):
                        return {'ok': True, 'session_id': sid, 'settings': cur_settings,
                                'current_seq': cur_seq, 'duplicate': True}
                    return {'ok': False, 'error': 'stale_sequence', 'session_id': sid,
                            'current_seq': cur_seq, 'client_seq': c_seq_int,
                            'settings': cur_settings,
                            'message': f'设置已被更新的版本覆盖（当前第 {cur_seq} 版）。'}
                elif c_seq_int == cur_seq:
                    if _is_content_identical(cleaned_candidate, cur_settings):
                        return {'ok': True, 'session_id': sid, 'settings': cur_settings,
                                'current_seq': cur_seq, 'duplicate': True}
                    return {'ok': False, 'error': 'stale_sequence', 'session_id': sid,
                            'current_seq': cur_seq, 'client_seq': c_seq_int,
                            'settings': cur_settings,
                            'message': '相同版本号但内容不同的请求已被拒绝。'}
                else:
                    cleaned_candidate['_seq'] = c_seq_int
                    str_val = json.dumps(cleaned_candidate, ensure_ascii=False)
                    self._conn.execute('UPDATE sessions SET settings_json=?, updated_at=? WHERE id=?',
                                       (str_val, now(), sid))
                    return {'ok': True, 'session_id': sid, 'settings': cleaned_candidate, 'current_seq': c_seq_int}

            # Protocol 3: No version provided: auto-increment
            new_seq = cur_seq + 1
            cleaned_candidate['_seq'] = new_seq
            str_val = json.dumps(cleaned_candidate, ensure_ascii=False)
            self._conn.execute('UPDATE sessions SET settings_json=?, updated_at=? WHERE id=?',
                               (str_val, now(), sid))
            return {'ok': True, 'session_id': sid, 'settings': cleaned_candidate, 'current_seq': new_seq}

    def reset_session_settings(self, sid: str,
                               client_seq: Optional[int] = None,
                               base_seq: Optional[int] = None) -> dict:
        """Atomic reset to defaults with version checking inside a BEGIN IMMEDIATE transaction."""
        def _is_content_identical(s1: dict, s2: dict) -> bool:
            # `web_search` MUST be compared: without it, flipping the manual
            # search toggle looked like "no change" and the save was skipped,
            # leaving the session permanently unable to turn search on.
            keys = ('style', 'history_mode', 'custom_budget', 'max_tokens',
                    'use_memory', 'learn_memory', 'web_search')
            return all(s1.get(k) == s2.get(k) for k in keys)

        with self.transaction():
            row = self._conn.execute('SELECT settings_json FROM sessions WHERE id=?', (sid,)).fetchone()
            if row is None:
                return {'ok': False, 'error': 'not_found', 'message': '会话不存在。'}

            cur_settings = {}
            if row['settings_json']:
                try:
                    cur_settings = sanitize_session_settings(json.loads(row['settings_json']))
                except Exception:
                    cur_settings = dict(DEFAULT_SESSION_SETTINGS)
            else:
                cur_settings = dict(DEFAULT_SESSION_SETTINGS)

            cur_seq = cur_settings.get('_seq', 0)

            new_settings = dict(DEFAULT_SESSION_SETTINGS)

            # 1. base_seq OCC check
            if base_seq is not None:
                try:
                    b_seq_int = int(base_seq)
                except (ValueError, TypeError):
                    return {'ok': False, 'error': 'invalid_base_seq', 'message': 'base_seq 必须为有效整数。'}

                if b_seq_int == cur_seq:
                    if _is_content_identical(new_settings, cur_settings):
                        return {'ok': True, 'session_id': sid, 'settings': cur_settings,
                                'current_seq': cur_seq, 'no_op': True}
                    new_seq = cur_seq + 1
                    new_settings['_seq'] = new_seq
                    str_val = json.dumps(new_settings, ensure_ascii=False)
                    self._conn.execute('UPDATE sessions SET settings_json=?, updated_at=? WHERE id=?',
                                       (str_val, now(), sid))
                    return {'ok': True, 'session_id': sid, 'settings': new_settings, 'current_seq': new_seq}
                elif b_seq_int < cur_seq:
                    if _is_content_identical(new_settings, cur_settings):
                        return {'ok': True, 'session_id': sid, 'settings': cur_settings,
                                'current_seq': cur_seq, 'duplicate': True}
                    return {'ok': False, 'error': 'version_conflict', 'session_id': sid,
                            'current_seq': cur_seq, 'base_seq': b_seq_int,
                            'settings': cur_settings,
                            'message': f'恢复默认时检测到版本冲突（当前第 {cur_seq} 版），未保存。已同步最新设置。'}
                else:
                    return {'ok': False, 'error': 'future_version', 'session_id': sid,
                            'current_seq': cur_seq, 'base_seq': b_seq_int,
                            'message': f'提交的基准版本 {b_seq_int} 大于服务端当前版本 {cur_seq}。'}

            # 2. client_seq check
            if client_seq is not None:
                try:
                    c_seq_int = int(client_seq)
                except (ValueError, TypeError):
                    c_seq_int = cur_seq + 1

                if c_seq_int < cur_seq:
                    if _is_content_identical(new_settings, cur_settings):
                        return {'ok': True, 'session_id': sid, 'settings': cur_settings,
                                'current_seq': cur_seq, 'duplicate': True}
                    return {'ok': False, 'error': 'stale_sequence', 'session_id': sid,
                            'current_seq': cur_seq, 'client_seq': c_seq_int,
                            'settings': cur_settings,
                            'message': '设置已被更新的版本覆盖。'}
                elif c_seq_int == cur_seq:
                    if _is_content_identical(new_settings, cur_settings):
                        return {'ok': True, 'session_id': sid, 'settings': cur_settings,
                                'current_seq': cur_seq, 'duplicate': True}
                    return {'ok': False, 'error': 'stale_sequence', 'session_id': sid,
                            'current_seq': cur_seq, 'client_seq': c_seq_int,
                            'settings': cur_settings,
                            'message': '相同版本号但内容不同的请求已被拒绝。'}
                else:
                    new_settings['_seq'] = c_seq_int
                    str_val = json.dumps(new_settings, ensure_ascii=False)
                    self._conn.execute('UPDATE sessions SET settings_json=?, updated_at=? WHERE id=?',
                                       (str_val, now(), sid))
                    return {'ok': True, 'session_id': sid, 'settings': new_settings, 'current_seq': c_seq_int}

            # 3. No version param: auto increment
            new_seq = cur_seq + 1
            new_settings['_seq'] = new_seq
            str_val = json.dumps(new_settings, ensure_ascii=False)
            self._conn.execute('UPDATE sessions SET settings_json=?, updated_at=? WHERE id=?',
                               (str_val, now(), sid))
            return {'ok': True, 'session_id': sid, 'settings': new_settings, 'current_seq': new_seq}

    def set_session_boundary(self, sid: str, boundary_msg_id: Optional[str] = None) -> dict:
        if self.get_session(sid) is None:
            return {'ok': False, 'error': 'not_found', 'message': '会话不存在。'}
        if boundary_msg_id:
            m = self._one('SELECT id, seq FROM messages WHERE id=? AND session_id=?',
                          (boundary_msg_id, sid))
            if not m:
                return {'ok': False, 'error': 'message_not_found', 'message': '指定的消息不存在。'}
            b_id = m['id']
            b_seq = m['seq']
        else:
            m = self._one('SELECT id, seq FROM messages WHERE session_id=? ORDER BY seq DESC LIMIT 1',
                          (sid,))
            if not m:
                return {'ok': False, 'error': 'empty_session', 'message': '当前对话还没有消息，无需设置分界线。'}
            b_id = m['id']
            b_seq = m['seq']

        self._exec('UPDATE sessions SET boundary_msg_id=?, boundary_seq=?, updated_at=? WHERE id=?',
                   (b_id, b_seq, now(), sid))
        # Deactivate summary if it contains messages before or at the new boundary
        cur_sum = self._one('SELECT id, start_seq, end_seq FROM session_summaries WHERE session_id=? AND is_active=1', (sid,))
        if cur_sum and ((cur_sum.get('start_seq') or 0) <= b_seq or (cur_sum.get('end_seq') or 0) <= b_seq):
            self._exec('UPDATE session_summaries SET is_active=0, updated_at=? WHERE id=?', (now(), cur_sum['id']))
        return {'ok': True, 'session_id': sid, 'boundary_msg_id': b_id, 'boundary_seq': b_seq}

    def clear_session_boundary(self, sid: str) -> dict:
        if self.get_session(sid) is None:
            return {'ok': False, 'error': 'not_found', 'message': '会话不存在。'}
        self._exec('UPDATE sessions SET boundary_msg_id=NULL, boundary_seq=0, updated_at=? WHERE id=?',
                   (now(), sid))
        cur_sum = self._one('SELECT id, boundary_seq FROM session_summaries WHERE session_id=? AND is_active=1', (sid,))
        if cur_sum and (cur_sum.get('boundary_seq') or 0) > 0:
            self._exec('UPDATE session_summaries SET is_active=0, updated_at=? WHERE id=?', (now(), cur_sum['id']))
        return {'ok': True, 'session_id': sid}

    def rename_session(self, sid: str, title: str) -> dict:
        title = (title or '').strip()
        if not title:
            return {'ok': False, 'error': 'empty_title',
                    'message': '标题不能为空。'}
        cur = self._exec('UPDATE sessions SET title=?, updated_at=? WHERE id=?',
                         (title[:120], now(), sid))
        if cur.rowcount == 0:
            return {'ok': False, 'error': 'not_found'}
        return {'ok': True, 'session': self.get_session(sid)}

    def delete_session(self, sid: str) -> dict:
        """Delete a session, and only the memories it alone supports.

        A memory whose other sources survive stays (its bookkeeping is repointed
        at the surviving session); a memory that this session was the only
        support for goes with it, and its exact source message is tombstoned so a
        re-read of old chat cannot bring it back.
        """
        if self.get_session(sid) is None:
            return {'ok': False, 'error': 'not_found'}
        removed, kept = [], []
        # Drive the loop from memory_sources, not memories.source_session_id: a
        # memory shared by two sessions records only the LATEST one there.
        rows = self._all(
            'SELECT DISTINCT m.* FROM memories m '
            'JOIN memory_sources src ON src.memory_id = m.id '
            'WHERE src.session_id = ? AND m.deleted_at IS NULL', (sid,))
        for m in rows:
            other = self._all(
                'SELECT session_id, message_id, quote FROM memory_sources '
                'WHERE memory_id=? AND session_id<>?', (m['id'], sid))
            self._exec('DELETE FROM memory_sources WHERE memory_id=? '
                       'AND session_id=?', (m['id'], sid))
            if other:
                src = other[0]
                self._exec(
                    'UPDATE memories SET source_session_id=?, source_message_id=?, '
                    'evidence_quote=? WHERE id=?',
                    (src['session_id'], src['message_id'], src['quote'], m['id']))
                kept.append(m['id'])
            else:
                self._exec('UPDATE memories SET deleted_at=?, deleted_reason=?, '
                           'enabled=0, updated_at=? WHERE id=?',
                           (now(), 'source_session_deleted', now(), m['id']))
                self._exec('INSERT OR IGNORE INTO tombstones '
                           '(kind, key, message_id, created_at) VALUES (?,?,?,?)',
                           (m['kind'], m['key'], m['source_message_id'] or '', now()))
                removed.append(m['id'])
        self._exec('DELETE FROM messages WHERE session_id=?', (sid,))
        self._exec('DELETE FROM session_summaries WHERE session_id=?', (sid,))
        self._exec('DELETE FROM sessions WHERE id=?', (sid,))
        self._event(sid, None, None, 'delete',
                    f'session deleted; memories removed={len(removed)} '
                    f'kept={len(kept)}')
        return {'ok': True, 'memories_removed': removed, 'memories_kept': kept}

    def touch_session(self, sid: str) -> None:
        self._exec('UPDATE sessions SET updated_at=? WHERE id=?', (now(), sid))

    def set_session_note(self, sid: str, note: Optional[str]) -> None:
        self._exec('UPDATE sessions SET note=? WHERE id=?', (note, sid))

    # -- session summaries ------------------------------------------------
    def get_session_summary(self, sid: str, status: Optional[str] = None) -> Optional[dict]:
        """Fetch the latest summary for the session, parsing summary_json."""
        if status:
            row = self._one(
                'SELECT * FROM session_summaries WHERE session_id=? AND status=? ORDER BY summary_seq DESC LIMIT 1',
                (sid, status))
        else:
            row = self._one(
                'SELECT * FROM session_summaries WHERE session_id=? ORDER BY summary_seq DESC LIMIT 1',
                (sid,))
        if not row:
            return None
        res = dict(row)
        try:
            res['summary_json'] = json.loads(res.get('summary_json') or '{}')
        except Exception:
            res['summary_json'] = {}
        return res

    def save_session_summary(self, sid: str, data: dict) -> dict:
        """Insert a newly generated session summary, deactivating older ones if ready."""
        sess = self.get_session(sid)
        if sess is None:
            return {'ok': False, 'error': 'session_not_found', 'message': '会话不存在。'}

        sum_id = data.get('id') or new_id('sum')
        summary_json_str = json.dumps(data.get('summary_json') or {}, ensure_ascii=False)
        summary_text = data.get('summary_text') or ''
        status_val = data.get('status', 'ready')

        row_prev = self._one('SELECT COALESCE(MAX(summary_seq), 0) AS m FROM session_summaries WHERE session_id=?', (sid,))
        next_seq = int((row_prev or {}).get('m', 0)) + 1

        with self.transaction():
            if status_val == 'ready':
                # Deactivate prior summaries for this session
                self._exec('UPDATE session_summaries SET is_active=0, updated_at=? WHERE session_id=?', (now(), sid))

            self._exec(
                'INSERT INTO session_summaries ('
                'id, session_id, summary_seq, is_active, status, summary_json, summary_text, '
                'start_msg_id, end_msg_id, start_seq, end_seq, source_turn_count, recent_turns_kept, '
                'boundary_seq, instance_seq, input_tokens, output_tokens, tokens_saved, error, created_at, updated_at'
                ') VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (
                    sum_id, sid, next_seq,
                    1 if (data.get('is_active') and status_val == 'ready') else 0,
                    status_val,
                    summary_json_str,
                    summary_text,
                    data.get('start_msg_id'),
                    data.get('end_msg_id'),
                    data.get('start_seq', 0),
                    data.get('end_seq', 0),
                    data.get('source_turn_count', 0),
                    data.get('recent_turns_kept', 3),
                    data.get('boundary_seq', 0),
                    data.get('instance_seq', 0),
                    data.get('input_tokens'),
                    data.get('output_tokens'),
                    data.get('tokens_saved', 0),
                    data.get('error'),
                    now(), now()
                )
            )
        return {'ok': True, 'id': sum_id, 'summary_seq': next_seq}

    def set_summary_active(self, sid: str, is_active: bool) -> dict:
        """Enable or disable the current ready summary for this session."""
        cur = self._one('SELECT id, summary_seq, status FROM session_summaries WHERE session_id=? AND status="ready" ORDER BY summary_seq DESC LIMIT 1', (sid,))
        if not cur:
            return {'ok': False, 'error': 'no_summary', 'message': '当前会话没有可用的就绪摘要。'}
        self._exec('UPDATE session_summaries SET is_active=?, updated_at=? WHERE id=?',
                   (1 if is_active else 0, now(), cur['id']))
        return {'ok': True, 'session_id': sid, 'is_active': bool(is_active), 'summary_id': cur['id']}

    def delete_session_summary(self, sid: str) -> dict:
        """Completely remove all summaries for this session."""
        self._exec('DELETE FROM session_summaries WHERE session_id=?', (sid,))
        return {'ok': True, 'session_id': sid}

    # -- messages ---------------------------------------------------------
    def _next_seq(self, sid: str) -> int:
        row = self._one('SELECT COALESCE(MAX(seq), 0) AS m FROM messages '
                        'WHERE session_id=?', (sid,))
        return int((row or {}).get('m', 0)) + 1

    def add_user_message(self, sid: str, content: str,
                         client_msg_id: Optional[str] = None) -> dict:
        """Insert the user's turn once, even if the POST is retried.

        The row also receives an OPERATION VERSION, allocated at receive time.
        That is the number every later memory write is compared against: a result
        derived from this message may only change a memory whose own version is
        older.
        """
        mid = client_msg_id or new_id('m')
        ts = now()
        with self._lock:
            existing = self._one('SELECT * FROM messages WHERE id=?', (mid,))
            if existing is not None:
                return existing
            version = self.next_version('user_message', mid)
            seq = self._next_seq(sid)
            self._exec(
                "INSERT INTO messages (id, session_id, seq, role, content, status,"
                " created_at, updated_at, version) VALUES (?,?,?,?,?,?,?,?,?)",
                (mid, sid, seq, 'user', content, 'complete', ts, ts, version))
            self._exec('UPDATE sessions SET updated_at=? WHERE id=?', (ts, sid))
            return self._one('SELECT * FROM messages WHERE id=?', (mid,)) or {}

    def start_assistant_message(self, sid: str, request_id: str,
                                model: Optional[str] = None,
                                reply_to_id: Optional[str] = None,
                                profile_id: Optional[str] = None) -> dict:
        """Create the assistant row in `streaming` BEFORE the request is sent.

        If the process dies mid-generation the row exists, so the record shows a
        reply that was cut off rather than a question with no answer at all.

        Round 43: `profile_id` records WHICH model configuration answered. It is
        written at creation time from the verified running instance, so a later
        switch cannot change the identity of an existing reply.
        """
        mid = new_id('a')
        ts = now()
        with self._lock:
            seq = self._next_seq(sid)
            self._exec(
                "INSERT INTO messages (id, session_id, seq, role, content, status,"
                " created_at, updated_at, request_id, model, reply_to_id, profile_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (mid, sid, seq, 'assistant', '', 'streaming', ts, ts,
                 request_id, model, reply_to_id, profile_id))
            return self._one('SELECT * FROM messages WHERE id=?', (mid,)) or {}

    def find_assistant_for_user_message(self, sid: str, user_msg_id: str,
                                        user_seq: Optional[int] = None) -> Optional[dict]:
        """Find the assistant message answering a specific user message.

        Prefers exact reply_to_id attribution. For legacy records where reply_to_id
        is NULL, conservatively matches the immediately adjacent row (seq == user_seq + 1)
        only if reply_to_id is NULL.
        """
        if user_msg_id:
            row = self._one(
                "SELECT * FROM messages WHERE session_id=? AND role='assistant' "
                "AND reply_to_id=? ORDER BY seq DESC LIMIT 1",
                (sid, user_msg_id))
            if row is not None:
                return row
        if user_seq is not None:
            return self._one(
                "SELECT * FROM messages WHERE session_id=? AND role='assistant' "
                "AND seq = ? AND reply_to_id IS NULL",
                (sid, user_seq + 1))
        return None

    def append_delta(self, mid: str, sid: str, request_id: str,
                     text: str) -> int:
        """Append streamed text. Guarded by id + session + request + streaming."""
        if not text:
            return 0
        cur = self._exec(
            "UPDATE messages SET content = content || ?, updated_at=? "
            "WHERE id=? AND session_id=? AND request_id=? AND status='streaming'",
            (text, now(), mid, sid, request_id))
        return cur.rowcount

    def finish_assistant_message(self, mid: str, sid: str, request_id: str,
                                 status: str, error: Optional[str] = None,
                                 usage: Optional[dict] = None) -> int:
        """The single place a generation becomes a recorded outcome."""
        cur = self._exec(
            "UPDATE messages SET status=?, updated_at=?, error=?, usage_json=? "
            "WHERE id=? AND session_id=? AND request_id=? AND status='streaming'",
            (status, now(), error,
             json.dumps(usage, ensure_ascii=False) if usage else None,
             mid, sid, request_id))
        if cur.rowcount:
            self._exec('UPDATE sessions SET updated_at=? WHERE id=?', (now(), sid))
        return cur.rowcount

    def get_message(self, mid: str) -> Optional[dict]:
        row = self._one('SELECT * FROM messages WHERE id=?', (mid,))
        if row is None:
            return None
        row['usage'] = None
        if row.get('usage_json'):
            try:
                row['usage'] = json.loads(row['usage_json'])
            except ValueError:
                pass
        sr = self._one('SELECT * FROM web_search_results WHERE message_id=? OR request_id=?',
                       (mid, row.get('request_id')))
        if sr:
            try:
                res_list = json.loads(sr['results_json'] or '[]')
            except Exception:
                res_list = []
            row['web_search'] = {
                'id': sr['id'],
                'query': sr['query'],
                'status': sr['status'],
                'provider': sr['provider'],
                'results': res_list,
                'elapsed_s': sr['elapsed_s'],
                'error': sr['error'],
            }
        else:
            row['web_search'] = None
        return row

    def drop_empty_assistant(self, mid: str, request_id: str) -> int:
        """Remove a placeholder that never received a single token.

        Used when the round refuses BEFORE sending (input too long). A visible
        empty bubble would suggest "the model answered nothing", which is not
        what happened -- nothing was sent at all.
        """
        cur = self._exec(
            "DELETE FROM messages WHERE id=? AND request_id=? "
            "AND role='assistant' AND content='' AND status='streaming'",
            (mid, request_id))
        return cur.rowcount

    def messages(self, sid: str, limit: Optional[int] = None) -> list[dict]:
        rows = self._all('SELECT * FROM messages WHERE session_id=? '
                         'ORDER BY seq ASC', (sid,))
        if limit is not None:
            rows = rows[-limit:]

        search_rows = self._all('SELECT * FROM web_search_results WHERE session_id=?', (sid,))
        search_by_mid = {}
        search_by_req = {}
        for sr in search_rows:
            if sr.get('message_id'):
                search_by_mid[sr['message_id']] = sr
            if sr.get('request_id'):
                search_by_req[sr['request_id']] = sr

        for r in rows:
            if r.get('usage_json'):
                try:
                    r['usage'] = json.loads(r['usage_json'])
                except ValueError:
                    r['usage'] = None
            else:
                r['usage'] = None

            sr = search_by_mid.get(r['id']) or search_by_req.get(r.get('request_id'))
            if sr:
                try:
                    res_list = json.loads(sr['results_json'] or '[]')
                except Exception:
                    res_list = []
                try:
                    raw_list = json.loads(sr.get('raw_results_json') or '[]')
                except Exception:
                    raw_list = []
                try:
                    att_list = json.loads(sr.get('attempts_json') or '[]')
                except Exception:
                    att_list = []
                r['web_search'] = {
                    'id': sr['id'],
                    'query': sr['query'],
                    'status': sr['status'],
                    'provider': sr['provider'],
                    'backend': sr.get('backend'),
                    'client_version': sr.get('client_version'),
                    'attempts': att_list,
                    'fallback_reason': sr.get('fallback_reason'),
                    'raw_results': raw_list,
                    'results': res_list,
                    'trimmed_count': sr.get('trimmed_count') or 0,
                    'tokens_used': sr.get('tokens_used') or 0,
                    'elapsed_s': sr['elapsed_s'],
                    'error': sr['error'],
                }
            else:
                r['web_search'] = None
        return rows

    def set_tokens(self, mid: str, tokens: int) -> None:
        self._exec('UPDATE messages SET tokens=? WHERE id=?', (tokens, mid))

    def find_assistant_for_request(self, request_id: str) -> Optional[dict]:
        return self._one('SELECT * FROM messages WHERE request_id=? '
                         "AND role='assistant' ORDER BY seq DESC LIMIT 1",
                         (request_id,))

    # -- web search -------------------------------------------------------
    def save_web_search(self, *, session_id: str, request_id: str, query: str,
                        status: str, provider: str = 'ddgs',
                        backend: Optional[str] = None,
                        client_version: Optional[str] = None,
                        attempts: Optional[list[dict]] = None,
                        fallback_reason: Optional[str] = None,
                        raw_results: Optional[list[dict]] = None,
                        results: Optional[list[dict]] = None,
                        trimmed_count: int = 0,
                        message_id: Optional[str] = None,
                        elapsed_s: float = 0.0,
                        error: Optional[str] = None,
                        tokens_used: int = 0) -> str:
        search_id = new_id('srch')
        ts = now()
        results_json = json.dumps(results or [], ensure_ascii=False)
        raw_results_json = json.dumps(raw_results or [], ensure_ascii=False)
        attempts_json = json.dumps(attempts or [], ensure_ascii=False)
        self._exec(
            'INSERT INTO web_search_results (id, session_id, message_id, request_id, '
            'query, status, provider, backend, client_version, attempts_json, '
            'fallback_reason, raw_results_json, results_json, trimmed_count, '
            'tokens_used, elapsed_s, error, created_at, updated_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (search_id, session_id, message_id, request_id, query, status,
             provider, backend, client_version, attempts_json, fallback_reason,
             raw_results_json, results_json, trimmed_count, tokens_used, elapsed_s, error, ts, ts)
        )
        return search_id

    def update_web_search_message(self, request_id: str, message_id: str,
                                  elapsed_s: Optional[float] = None,
                                  tokens_used: Optional[int] = None,
                                  results: Optional[list[dict]] = None,
                                  raw_results: Optional[list[dict]] = None,
                                  trimmed_count: Optional[int] = None,
                                  backend: Optional[str] = None,
                                  fallback_reason: Optional[str] = None) -> None:
        updates = ['message_id = ?', 'updated_at = ?']
        params: list[Any] = [message_id, now()]
        if elapsed_s is not None:
            updates.append('elapsed_s = ?')
            params.append(elapsed_s)
        if tokens_used is not None:
            updates.append('tokens_used = ?')
            params.append(tokens_used)
        if results is not None:
            updates.append('results_json = ?')
            params.append(json.dumps(results, ensure_ascii=False))
        if raw_results is not None:
            updates.append('raw_results_json = ?')
            params.append(json.dumps(raw_results, ensure_ascii=False))
        if trimmed_count is not None:
            updates.append('trimmed_count = ?')
            params.append(trimmed_count)
        if backend is not None:
            updates.append('backend = ?')
            params.append(backend)
        if fallback_reason is not None:
            updates.append('fallback_reason = ?')
            params.append(fallback_reason)
        params.append(request_id)
        self._exec(
            f'UPDATE web_search_results SET {", ".join(updates)} WHERE request_id = ?',
            params
        )

    def get_web_search_for_message(self, message_id: str) -> Optional[dict]:
        row = self._one('SELECT * FROM web_search_results WHERE message_id=?', (message_id,))
        if not row:
            return None
        out = dict(row)
        try:
            out['results'] = json.loads(row['results_json'] or '[]')
        except Exception:
            out['results'] = []
        try:
            out['raw_results'] = json.loads(row.get('raw_results_json') or '[]')
        except Exception:
            out['raw_results'] = []
        try:
            out['attempts'] = json.loads(row.get('attempts_json') or '[]')
        except Exception:
            out['attempts'] = []
        return out

    def get_web_search_for_request(self, request_id: str) -> Optional[dict]:
        row = self._one('SELECT * FROM web_search_results WHERE request_id=?', (request_id,))
        if not row:
            return None
        out = dict(row)
        try:
            out['results'] = json.loads(row['results_json'] or '[]')
        except Exception:
            out['results'] = []
        try:
            out['raw_results'] = json.loads(row.get('raw_results_json') or '[]')
        except Exception:
            out['raw_results'] = []
        try:
            out['attempts'] = json.loads(row.get('attempts_json') or '[]')
        except Exception:
            out['attempts'] = []
        return out

    # -- memories ---------------------------------------------------------
    def live_memories(self, kind: Optional[str] = None) -> list[dict]:
        # Join the source session title so the panel can say WHERE a memory came
        # from in words the user recognises, not just an opaque id.
        sql = ('SELECT m.*, s.title AS source_session_title '
               'FROM memories m '
               'LEFT JOIN sessions s ON s.id = m.source_session_id '
               'WHERE m.deleted_at IS NULL')
        args: list[Any] = []
        if kind:
            sql += ' AND m.kind=?'
            args.append(kind)
        sql += ' ORDER BY m.kind ASC, m.updated_at DESC'
        return self._all(sql, args)

    def all_memories(self) -> list[dict]:
        return self._all('SELECT * FROM memories ORDER BY created_at DESC')

    def find_memory(self, kind: str, key: str) -> Optional[dict]:
        return self._one('SELECT * FROM memories WHERE kind=? AND key=? '
                         'AND deleted_at IS NULL ORDER BY updated_at DESC '
                         'LIMIT 1', (kind, key))

    def tombstoned(self, kind: str, key: str, message_id: str) -> bool:
        if not message_id:
            return False
        return self._one('SELECT 1 AS x FROM tombstones WHERE kind=? AND key=? '
                         'AND message_id=?', (kind, key, message_id)) is not None

    def _memory_group_keys(self, kind: str, key: str) -> list[str]:
        if kind == 'address':
            return ['name']
        if kind == 'preference':
            opp = PREF_OPPOSITES_STORE.get(key)
            if opp:
                return sorted([key, opp])
            return [key]
        return [key]

    def _msg_order(self, mid: Optional[str]) -> tuple:
        """Legacy ordering, kept for callers that only have ids.

        It is NOT what decides whether a write is allowed any more -- see
        `_guard()`. Comparing message receive order alone is what let a message
        that was received early but processed late overwrite a manual edit.
        """
        if not mid:
            return (0, 0, '')
        row = self._one('SELECT rowid, seq, created_at FROM messages WHERE id=?', (mid,))
        if not row:
            return (0, 0, '')
        return (row['rowid'], row['seq'], row['created_at'])

    def _is_older_message(self, mid_in: Optional[str], mid_cur: Optional[str]) -> bool:
        if not mid_in or not mid_cur or mid_in == mid_cur:
            return False
        o_in = self._msg_order(mid_in)
        o_cur = self._msg_order(mid_cur)
        if o_in[0] and o_cur[0]:
            return o_in[0] < o_cur[0]
        if o_in[2] and o_cur[2] and o_in[2] != o_cur[2]:
            return o_in[2] < o_cur[2]
        return False

    def _is_newer_message(self, mid_in: Optional[str], mid_cur: Optional[str]) -> bool:
        if not mid_in or not mid_cur or mid_in == mid_cur:
            return False
        return not self._is_older_message(mid_in, mid_cur)

    # ------------------------------------------------------------------ #
    # Operation-order guard
    #
    # One question decides every write: is the SOURCE of this result newer than
    # the CURRENT STATE of the memory group?
    #
    # The source is the user message the result was derived from, and it carries
    # the operation version it was given when it was RECEIVED. The current state
    # is whatever last touched the group: a rule write, a manual edit, a manual
    # enable/disable, a manual delete, a tentative confirmation, or a user
    # instruction that revoked it. Each of those bumped the same sequence.
    #
    # Therefore "received first, processed later" is handled correctly: a
    # message received before the user's edit has a LOWER version than the edit,
    # so its late-arriving result is refused no matter when it is processed.
    # ------------------------------------------------------------------ #
    def _source_version(self, message_id: Optional[str]) -> Optional[int]:
        if not message_id:
            return None
        row = self._one('SELECT version FROM messages WHERE id=?', (message_id,))
        if not row:
            return None
        v = row.get('version')
        return int(v) if v is not None else None

    def _group_state_version(self, kind: str, key: str) -> tuple:
        """The newest operation version that touched this group.

        Returns (version, {'id', 'content', 'enabled', 'origin', 'deleted'}).
        A version of 0 means "written before operation order existed", which the
        caller must treat as UNKNOWN and refuse to overwrite.
        """
        keys = self._memory_group_keys(kind, key)
        best_v = -1
        best = None

        # Active memories in the group.
        for k in keys:
            m = self.find_memory(kind, k)
            if m is None:
                continue
            v = int(m.get('version') or 0)
            if v > best_v:
                best_v, best = v, {'id': m['id'], 'content': m['content'],
                                   'enabled': bool(m['enabled']),
                                   'origin': m['origin'],
                                   'deleted': False,
                                   'source_message_id': m.get('source_message_id')}

        # Deleted memories in the group (a delete is state too).
        ph = ','.join('?' * len(keys))
        for d in self._all(
                f'SELECT * FROM memories WHERE kind=? AND key IN ({ph}) '
                'AND deleted_at IS NOT NULL', [kind] + keys):
            v = int(d.get('version') or 0)
            if v > best_v:
                best_v, best = v, {'id': d['id'], 'content': d['content'],
                                   'enabled': False, 'origin': d['origin'],
                                   'deleted': True,
                                   'source_message_id': d.get('source_message_id')}

        # Tombstones written by a delete or a user revocation.
        for k in keys:
            for t in self._all('SELECT * FROM tombstones WHERE kind=? AND key=?',
                               (kind, k)):
                v = int(t.get('version') or 0)
                if v > best_v:
                    best_v, best = v, {'id': None, 'content': None,
                                       'enabled': False, 'origin': 'revoke',
                                       'deleted': True,
                                       'source_message_id': t['message_id']}
        return (best_v, best)

    def _guard(self, *, kind: str, key: str, message_id: Optional[str],
               origin: str, strength: str) -> dict:
        """Decide whether a result derived from `message_id` may be written.

        Returns {'allow': bool, 'action': str, 'detail': str, 'state': ...}.
        The caller must run this inside `transaction()` so the answer it gets is
        still true when it writes.

        Rules, in order:
          1. unknown source version      -> skip (conservative, per the brief)
          2. unknown group state version -> skip (a pre-migration row)
          3. source <= state             -> skip (stale result, whatever its kind)
          4. state is a manual action and the source is not newer -> skip
        """
        src = self._source_version(message_id)
        state_v, state = self._group_state_version(kind, key)

        if src is None:
            return {'allow': False, 'action': 'skipped_unknown_source',
                    'detail': 'the source message is unknown; refusing to write',
                    'state': state}
        if src == 0:
            return {'allow': False, 'action': 'skipped_unknown_source',
                    'detail': 'the source message carries no operation version',
                    'state': state}
        if state is not None and state_v == 0:
            return {'allow': False, 'action': 'skipped_unknown_state',
                    'detail': 'the current memory state has no operation version '
                              '(written before operation order existed)',
                    'state': state}
        if state is None:
            return {'allow': True, 'action': 'allowed', 'detail': 'no prior state',
                    'state': None}
        if src <= state_v:
            why = ('a manual action' if (state.get('origin') in
                                         ('user', 'user_confirmed', 'revoke'))
                   else 'a newer statement')
            return {'allow': False, 'action': 'skipped_stale_source',
                    'detail': f'the source (v{src}) is not newer than the current '
                              f'state (v{state_v}, {why})',
                    'state': state}
        return {'allow': True, 'action': 'allowed',
                'detail': f'source v{src} is newer than state v{state_v}',
                'state': state}

    def _log_op(self, *, kind: str, key: str, source_version, target_version,
                action: str, detail: str) -> None:
        self._exec(
            'INSERT INTO memory_ops (id, seq, at, kind, key, source_version,'
            ' target_version, action, detail) VALUES (?,?,?,?,?,?,?,?,?)',
            (new_id('op'), self.current_version(), now(), kind, key,
             source_version, target_version, action, str(detail)[:400]))

    def revoke_memory(self, *, kind: str, key: str, session_id: Optional[str],
                      message_id: Optional[str], quote: str) -> dict:
        """Revoke a durable preference/memory. Never creates an opposite memory."""
        with self.transaction():
            g = self._guard(kind=kind, key=key, message_id=message_id,
                            origin='revoke', strength=STRENGTH_EXPLICIT_STORE)
            if not g['allow']:
                self._log_op(kind=kind, key=key,
                             source_version=self._source_version(message_id),
                             target_version=self._group_state_version(kind, key)[0],
                             action=g['action'], detail=g['detail'])
                self._event(session_id, message_id, None, 'skip',
                            f'{g["action"]}: {g["detail"]}')
                return {'action': g['action'], 'detail': g['detail']}
            cur = self.find_memory(kind, key)
            ts = now()
            src_v = self._source_version(message_id)

            if cur is None:
                # No active memory currently exists, but we must record a durable
                # semantic revoke boundary so that any earlier positive message arriving/processed
                # later cannot establish this memory.
                exec_version = self.next_version('revoke_boundary', message_id or '')
                semantic_version = src_v if src_v is not None else exec_version
                if message_id:
                    self._exec('INSERT OR REPLACE INTO tombstones '
                               '(kind, key, message_id, created_at, version) '
                               'VALUES (?,?,?,?,?)',
                               (kind, key, message_id, ts, semantic_version))
                self._event(session_id, message_id, None, 'revoke_boundary',
                            f'recorded revoke boundary for {kind}/{key} without active memory ({quote!r})')
                self._log_op(kind=kind, key=key, source_version=src_v,
                             target_version=semantic_version, action='revoked_boundary',
                             detail=f'no active memory; recorded revoke boundary: {quote!r}')
                return {'action': 'revoked_boundary',
                        'detail': 'recorded revoke boundary without existing memory',
                        'version': semantic_version}

            exec_version = self.next_version('revoke', cur['id'])
            semantic_version = src_v if src_v is not None else exec_version
            self.delete_memory_explicit(cur['id'], reason='revoked_by_user_instruction',
                                        version=semantic_version)
            if message_id:
                self._exec('INSERT OR REPLACE INTO tombstones '
                           '(kind, key, message_id, created_at, version) '
                           'VALUES (?,?,?,?,?)',
                           (kind, key, message_id, ts, semantic_version))
            self._event(session_id, message_id, cur['id'], 'revoke',
                        f'revoked {kind}/{key} ({quote!r})')
            self._log_op(kind=kind, key=key, source_version=src_v,
                         target_version=semantic_version, action='revoked',
                         detail=f'revoked existing: {quote!r}')
            return {'action': 'revoked', 'memory': cur}

    def upsert_memory(self, *, kind: str, key: str, content: str,
                      strength: str, origin: str, session_id: Optional[str],
                      message_id: Optional[str], quote: str,
                      enabled: bool = True) -> dict:
        """Create or correct one memory. The guard and the write are one transaction.

        The guard runs INSIDE `transaction()` on purpose: validating first and
        writing later would let a manual edit land in between and be overwritten
        by a result that was checked against the older state.
        """
        with self.transaction():
            return self._upsert_locked(kind=kind, key=key, content=content,
                                       strength=strength, origin=origin,
                                       session_id=session_id,
                                       message_id=message_id, quote=quote,
                                       enabled=enabled)

    def _upsert_locked(self, *, kind: str, key: str, content: str,
                       strength: str, origin: str, session_id: Optional[str],
                       message_id: Optional[str], quote: str,
                       enabled: bool) -> dict:
        g = self._guard(kind=kind, key=key, message_id=message_id,
                        origin=origin, strength=strength)
        src_v = self._source_version(message_id)
        state_v = self._group_state_version(kind, key)[0]
        if not g['allow']:
            self._log_op(kind=kind, key=key, source_version=src_v,
                         target_version=state_v, action=g['action'],
                         detail=g['detail'])
            self._event(session_id, message_id, None, 'skip',
                        f'{g["action"]}: {g["detail"]}')
            state = g.get('state') or {}
            return {'action': g['action'], 'detail': g['detail'],
                    'memory': ({'id': state.get('id'),
                                'content': state.get('content'),
                                'enabled': state.get('enabled')}
                               if state.get('id') else None)}

        cur = self.find_memory(kind, key)

        # Strength only ever moves up: a tentative remark cannot demote an
        # explicit preference, and cannot silently disable it.
        if cur and (cur['strength'] == STRENGTH_EXPLICIT_STORE
                    and strength != STRENGTH_EXPLICIT_STORE):
            self._log_op(kind=kind, key=key, source_version=src_v,
                         target_version=state_v, action='skipped_weaker',
                         detail=f'kept explicit {kind}/{key}')
            self._event(session_id, message_id, cur['id'], 'skip',
                        f'kept explicit {kind}/{key}; a tentative remark does not '
                        f'override it ({quote!r})')
            return {'action': 'skipped_weaker', 'memory': cur}

        # A user's manual edit / disable is never quietly re-enabled by an
        # automatic source, even a newer one: only the user may re-enable it.
        if cur and cur.get('origin') in ('user', 'user_confirmed') \
                and origin not in ('user', 'user_confirmed') \
                and not cur.get('enabled'):
            self._log_op(kind=kind, key=key, source_version=src_v,
                         target_version=state_v, action='skipped_user_override',
                         detail='the memory was disabled by the user')
            self._event(session_id, message_id, cur['id'], 'skip',
                        f'kept user-disabled memory {kind}/{key}')
            return {'action': 'skipped_user_override', 'memory': cur}

        ts = now()
        exec_version = self.next_version('memory_write', cur['id'] if cur else '')
        # Semantic order: for automatic rules and model results, order is determined
        # by the arrival version of the source message (messages.version).
        # Execution order (exec_version) is preserved in op_seq and memory_ops for auditing.
        semantic_version = src_v if (src_v is not None and origin not in ('user', 'user_confirmed')) else exec_version

        # Validated and newer: supersede the conflicting items in the same group.
        if kind == 'address' and key == 'name' and strength == STRENGTH_EXPLICIT_STORE:
            old_addr = self.find_memory('address', 'name')
            if old_addr is not None and old_addr['content'] != content:
                self.delete_memory_explicit(old_addr['id'],
                                            reason='superseded_by_new_address',
                                            version=semantic_version)

        if kind == 'preference' and strength == STRENGTH_EXPLICIT_STORE:
            opp_key = PREF_OPPOSITES_STORE.get(key)
            if opp_key:
                old_opp = self.find_memory('preference', opp_key)
                if old_opp is not None:
                    self.delete_memory_explicit(old_opp['id'],
                                                reason=f'superseded_by_{key}',
                                                version=semantic_version)

        # Re-read after the supersede: the row we were about to update may have
        # just been retired.
        cur = self.find_memory(kind, key)
        if cur is None:
            mid = new_id('mem')
            self._exec(
                'INSERT INTO memories (id, kind, key, content, strength, enabled,'
                ' origin, source_session_id, source_message_id, evidence_quote,'
                ' created_at, updated_at, version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (mid, kind, key, content, strength, 1 if enabled else 0, origin,
                 session_id, message_id, quote, ts, ts, semantic_version))
            if session_id and message_id:
                self._exec(
                    'INSERT OR IGNORE INTO memory_sources (memory_id, session_id,'
                    ' message_id, quote, created_at) VALUES (?,?,?,?,?)',
                    (mid, session_id, message_id, quote, ts))
            self._event(session_id, message_id, mid, 'create',
                        f'{kind}/{key} = {content!r} ({strength}, {origin})')
            self._log_op(kind=kind, key=key, source_version=src_v,
                         target_version=semantic_version, action='created',
                         detail=f'{content!r} ({strength}, {origin})')
            return {'action': 'created', 'memory': self.find_memory(kind, key)}

        # Same content: record the extra source and update effective semantic version.
        if cur['content'] == content:
            changed = False
            cur_v = cur.get('version') or 0
            if semantic_version > cur_v:
                self._exec(
                    'UPDATE memories SET version=?, updated_at=?, '
                    'source_message_id=COALESCE(?, source_message_id) WHERE id=?',
                    (semantic_version, ts, message_id, cur['id']))
                changed = True
            if strength == STRENGTH_EXPLICIT_STORE and cur['strength'] != STRENGTH_EXPLICIT_STORE:
                self._exec('UPDATE memories SET strength=?, enabled=1, version=?, '
                           'updated_at=? WHERE id=?',
                           (strength, semantic_version, ts, cur['id']))
                changed = True
            if session_id and message_id:
                self._exec(
                    'INSERT OR IGNORE INTO memory_sources (memory_id, session_id,'
                    ' message_id, quote, created_at) VALUES (?,?,?,?,?)',
                    (cur['id'], session_id, message_id, quote, ts))
            self._log_op(kind=kind, key=key, source_version=src_v,
                         target_version=semantic_version,
                         action='updated' if changed else 'unchanged',
                         detail='same content; source order updated' if changed else 'same content; source recorded')
            return {'action': 'updated' if changed else 'unchanged',
                    'memory': self.find_memory(kind, key)}

        # Content changed: the old source no longer supports this memory.
        if cur.get('source_message_id'):
            self._exec('INSERT OR IGNORE INTO tombstones '
                       '(kind, key, message_id, created_at, version) '
                       'VALUES (?,?,?,?,?)',
                       (cur['kind'], cur['key'], cur['source_message_id'], ts,
                        semantic_version))
        self._exec('DELETE FROM memory_sources WHERE memory_id=?', (cur['id'],))

        changed = (cur['content'] != content or cur['strength'] != strength
                   or bool(cur['enabled']) != bool(enabled)
                   or semantic_version > (cur.get('version') or 0))
        self._exec(
            'UPDATE memories SET content=?, strength=?, enabled=?, origin=?,'
            ' source_session_id=COALESCE(?, source_session_id),'
            ' source_message_id=COALESCE(?, source_message_id),'
            ' evidence_quote=?, updated_at=?, version=? WHERE id=?',
            (content, strength, 1 if enabled else 0, origin, session_id,
             message_id, quote, ts, semantic_version, cur['id']))
        if session_id and message_id:
            self._exec(
                'INSERT OR IGNORE INTO memory_sources (memory_id, session_id,'
                ' message_id, quote, created_at) VALUES (?,?,?,?,?)',
                (cur['id'], session_id, message_id, quote, ts))
        self._event(session_id, message_id, cur['id'],
                    'update' if changed else 'skip',
                    f'{kind}/{key} -> {content!r} ({strength})'
                    + ('' if changed else ' (unchanged)'))
        self._log_op(kind=kind, key=key, source_version=src_v,
                     target_version=semantic_version,
                     action='updated' if changed else 'unchanged',
                     detail=f'{content!r} ({strength}, {origin})')
        return {'action': 'updated' if changed else 'unchanged',
                'memory': self.find_memory(kind, key)}

    def confirm_memory(self, mem_id: str) -> dict:
        """User confirms and promotes a tentative memory to explicit.

        A confirmation is a USER ACTION, so it bumps the operation sequence: any
        result derived from an older source is refused afterwards.
        """
        with self.transaction():
            cur = self._one('SELECT * FROM memories WHERE id=?', (mem_id,))
            if cur is None:
                return {'ok': False, 'error': 'not_found'}
            ts = now()
            version = self.next_version('confirm', mem_id)
            self._exec(
                'UPDATE memories SET strength=?, enabled=1, origin=?, updated_at=?,'
                ' version=? WHERE id=?',
                (STRENGTH_EXPLICIT_STORE, 'user_confirmed', ts, version, mem_id))
            if cur['kind'] == 'preference':
                opposite = PREF_OPPOSITES_STORE.get(cur['key'])
                if opposite:
                    old = self.find_memory('preference', opposite)
                    if old is not None:
                        self.delete_memory_explicit(
                            old['id'],
                            reason=f'superseded_by_confirmed_{cur["key"]}',
                            version=version)
            self._event(cur.get('source_session_id'),
                        cur.get('source_message_id'), mem_id, 'confirm',
                        f'user confirmed tentative {cur["kind"]}/{cur["key"]} '
                        f'as explicit')
            return {'ok': True,
                    'memory': self._one('SELECT * FROM memories WHERE id=?', (mem_id,))}

    def set_memory(self, mem_id: str, *, content: Optional[str] = None,
                   enabled: Optional[bool] = None, confirm: bool = False) -> dict:
        if confirm:
            return self.confirm_memory(mem_id)
        with self.transaction():
            # Re-read inside the transaction: the row may have been retired
            # between the caller's request and now.
            cur = self._one('SELECT * FROM memories WHERE id=?', (mem_id,))
            if cur is None:
                return {'ok': False, 'error': 'not_found'}
            ts = now()
            if content is not None:
                content = content.strip()
                if not content:
                    return {'ok': False, 'error': 'empty_content',
                            'message': '记忆内容不能为空。'}
                # A manual edit is a USER ACTION on the whole row, not just its
                # text: it moves the version AND drops the old source, so an old
                # message can neither overwrite it nor stay evidence for it.
                version = self.next_version('manual_edit', mem_id)
                self._exec('DELETE FROM memory_sources WHERE memory_id=?', (mem_id,))
                self._exec('UPDATE memories SET content=?, origin=?, updated_at=?,'
                           ' version=? WHERE id=?',
                           (content, 'user', ts, version, mem_id))
                self._event(cur.get('source_session_id'),
                            cur.get('source_message_id'), mem_id, 'update',
                            f'user updated content to {content!r}')
            if enabled is not None:
                version = self.next_version('manual_enable' if enabled
                                            else 'manual_disable', mem_id)
                self._exec('UPDATE memories SET enabled=?, updated_at=?, version=? '
                           'WHERE id=?', (1 if enabled else 0, ts, version, mem_id))
                self._event(cur.get('source_session_id'),
                            cur.get('source_message_id'), mem_id,
                            'enable' if enabled else 'disable',
                            f'user set enabled={bool(enabled)}')
            return {'ok': True,
                    'memory': self._one('SELECT * FROM memories WHERE id=?', (mem_id,))}

    def delete_memory(self, mem_id: str) -> dict:
        """User deletion. Tombstones all supporting source messages so nothing revives it."""
        with self.transaction():
            cur = self._one('SELECT * FROM memories WHERE id=?', (mem_id,))
            if cur is None:
                return {'ok': False, 'error': 'not_found'}
            ts = now()
            version = self.next_version('manual_delete', mem_id)
            self._exec('UPDATE memories SET deleted_at=?, deleted_reason=?, enabled=0, '
                       'updated_at=?, version=? WHERE id=?',
                       (ts, 'user_deleted', ts, version, mem_id))
            sources = self._all('SELECT message_id FROM memory_sources WHERE memory_id=?',
                                (mem_id,))
            mids = {s['message_id'] for s in sources if s.get('message_id')}
            if cur.get('source_message_id'):
                mids.add(cur['source_message_id'])
            for mid in mids:
                self._exec('INSERT OR IGNORE INTO tombstones '
                           '(kind, key, message_id, created_at, version) '
                           'VALUES (?,?,?,?,?)',
                           (cur['kind'], cur['key'], mid, ts, version))
            self._event(cur.get('source_session_id'), cur.get('source_message_id'),
                        mem_id, 'delete',
                        f'user deleted {cur["kind"]}/{cur["key"]}')
            return {'ok': True}

    def delete_memory_explicit(self, mem_id: str, reason: str,
                               version: Optional[int] = None) -> dict:
        """Retire a memory because a NEWER statement replaced it.

        The caller supplies the operation version it allocated, so the retirement
        and the replacement form one ordered step and no other thread can read a
        half-applied state.
        """
        cur = self._one('SELECT * FROM memories WHERE id=?', (mem_id,))
        if cur is None:
            return {'ok': False, 'error': 'not_found'}
        if version is None:
            version = self.next_version('memory_write', mem_id)
        ts = now()
        self._exec('UPDATE memories SET deleted_at=?, deleted_reason=?, enabled=0, '
                   'updated_at=?, version=? WHERE id=?',
                   (ts, reason, ts, version, mem_id))
        sources = self._all('SELECT message_id FROM memory_sources WHERE memory_id=?',
                            (mem_id,))
        mids = {s['message_id'] for s in sources if s.get('message_id')}
        if cur.get('source_message_id'):
            mids.add(cur['source_message_id'])
        for mid in mids:
            self._exec('INSERT OR IGNORE INTO tombstones '
                       '(kind, key, message_id, created_at, version) '
                       'VALUES (?,?,?,?,?)',
                       (cur['kind'], cur['key'], mid, ts, version))
        self._event(cur.get('source_session_id'), cur.get('source_message_id'),
                    mem_id, 'update', f'superseded ({reason})')
        return {'ok': True}

    def _event(self, session_id, message_id, memory_id, action, detail) -> None:
        self._exec(
            'INSERT INTO memory_events (id, at, session_id, message_id, memory_id,'
            ' action, detail) VALUES (?,?,?,?,?,?,?)',
            (new_id('ev'), now(), session_id, message_id, memory_id, action,
             str(detail)[:400]))

    def memory_events(self, limit: int = 200) -> list[dict]:
        return self._all('SELECT * FROM memory_events ORDER BY at DESC, id DESC '
                         'LIMIT ?', (limit,))

    # -- counters ---------------------------------------------------------
    def bump(self, field: str, n: int = 1) -> None:
        if field not in ('chat_requests', 'extraction_requests', 'aux_requests',
                         'failed_requests'):
            raise ValueError(f'unknown counter {field!r}')
        self._exec(f'UPDATE counters SET {field} = {field} + ?, updated_at=? '
                   f'WHERE id=1', (n, now()))

    def counters(self) -> dict:
        row = self._one('SELECT * FROM counters WHERE id=1') or {}
        row.pop('id', None)
        return row

    # -- request log ------------------------------------------------------
    def log_request(self, *, kind: str, url: str, session_id: Optional[str],
                    message_id: Optional[str], request_id: Optional[str],
                    status: Optional[int], ok: bool, body: Optional[dict],
                    prompt_tokens: Optional[int] = None,
                    completion_tokens: Optional[int] = None,
                    elapsed_s: Optional[float] = None,
                    finish_reason: Optional[str] = None,
                    error: Optional[str] = None,
                    profile_id: Optional[str] = None,
                    model_alias: Optional[str] = None,
                    instance_seq: Optional[int] = None) -> str:
        """Record one outbound HTTP request.

        Round 43: the row carries the model CONFIGURATION (`profile_id`), the
        serving alias and the instance generation, so "which model produced this
        request" is a stored fact rather than something reconstructed later from
        whichever model happens to be loaded.
        """
        import hashlib
        body = body or {}
        blob = json.dumps(body, ensure_ascii=False)
        msgs = body.get('messages') or []
        sys_joined = '\n'.join(str(m.get('content') or '') for m in msgs
                               if isinstance(m, dict) and m.get('role') == 'system'
                               and not str(m.get('content') or '').startswith('关于用户（来自之前的交流'))
        rid = new_id('req')
        self._exec(
            'INSERT INTO request_log (id, at, kind, url, session_id, message_id,'
            ' request_id, status, ok, has_tools, has_skill_text, body_chars,'
            ' body_sha256, message_count, prompt_tokens, completion_tokens,'
            ' elapsed_s, finish_reason, error, body_json,'
            ' profile_id, model_alias, instance_seq)'
            ' VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (rid, now(), kind, url, session_id, message_id, request_id, status,
             1 if ok else 0, 1 if ('tools' in body or 'tool_choice' in body) else 0,
             1 if ('工具使用指南' in sys_joined or '经验卡' in sys_joined) else 0,
             len(blob), hashlib.sha256(blob.encode('utf-8')).hexdigest(),
             len(msgs), prompt_tokens, completion_tokens, elapsed_s,
             finish_reason, error,
             blob if kind in ('chat', 'extraction') else None,
             profile_id, model_alias, instance_seq))
        return rid

    def finalize_request_log(self, req_id: str, *, ok: bool,
                             status: Optional[int] = None,
                             prompt_tokens: Optional[int] = None,
                             completion_tokens: Optional[int] = None,
                             elapsed_s: Optional[float] = None,
                             finish_reason: Optional[str] = None,
                             error: Optional[str] = None) -> None:
        """Close an outbound HTTP request record with its actual metrics."""
        self._exec(
            'UPDATE request_log SET ok=?, status=?, prompt_tokens=?, '
            'completion_tokens=?, elapsed_s=?, finish_reason=?, error=? '
            'WHERE id=?',
            (1 if ok else 0, status, prompt_tokens, completion_tokens,
             elapsed_s, finish_reason, error, req_id))

    def requests(self, limit: int = 200) -> list[dict]:
        return self._all('SELECT * FROM request_log ORDER BY at DESC, id DESC '
                         'LIMIT ?', (limit,))

    def request_counts(self) -> dict:
        rows = self._all('SELECT kind, COUNT(*) AS n FROM request_log '
                         'GROUP BY kind')
        out = {'chat': 0, 'extraction': 0, 'aux': 0}
        for r in rows:
            out[r['kind']] = r['n']
        out['total'] = sum(out.values())
        out['with_tools'] = (self._one(
            'SELECT COUNT(*) AS n FROM request_log WHERE has_tools=1') or {}).get('n', 0)
        out['with_skill_text'] = (self._one(
            'SELECT COUNT(*) AS n FROM request_log WHERE has_skill_text=1')
            or {}).get('n', 0)
        return out

    def stats(self) -> dict:
        db = self.path
        # WAL mode keeps recent writes in `-wal`, so the main file alone badly
        # under-reports the size of the data. Report the whole set.
        related = []
        total = 0
        for suffix in ('', '-wal', '-shm'):
            p = Path(str(db) + suffix)
            if p.is_file():
                size = p.stat().st_size
                related.append({'file': p.name, 'bytes': size})
                total += size
        return {
            'sessions': (self._one('SELECT COUNT(*) AS n FROM sessions') or {}).get('n', 0),
            'messages': (self._one('SELECT COUNT(*) AS n FROM messages') or {}).get('n', 0),
            'memories_live': (self._one('SELECT COUNT(*) AS n FROM memories '
                                        'WHERE deleted_at IS NULL') or {}).get('n', 0),
            'memories_enabled': (self._one('SELECT COUNT(*) AS n FROM memories '
                                           'WHERE deleted_at IS NULL AND enabled=1')
                                 or {}).get('n', 0),
            'memories_deleted': (self._one('SELECT COUNT(*) AS n FROM memories '
                                           'WHERE deleted_at IS NOT NULL') or {}).get('n', 0),
            'database': str(db),
            'database_bytes': total,
            'database_files': related,
        }
