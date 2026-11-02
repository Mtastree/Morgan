# -*- coding: utf-8 -*-
"""Round 53: refuse to run a TEST whose chat database resolves to production.

## Why this module exists

In Round 52 I started the chat page on port 8771 **without** setting any store
path. `chatweb` has no `--db-path` flag, so `chatstore.ChatStore()` fell back to
`chatdata()/chat.sqlite3` -- the production database. That instance opened the
production file in WAL mode; it was force-killed, and the next connection
checkpointed the leftover WAL into the main file. Bytes and mtime changed.

`HERMES_STORE_PATH`, which `desktop_round49_common.Desktop.launch()` sets, is
**not read anywhere in the product code** -- it was a dead variable, which is why
setting it did nothing for a `chatweb` process. The real resolution chain is:

    Desktop(--db-path)  ->  chatdesktop.DesktopController.expected_store_path
                        ->  chatstore.ChatStore(path)

    chatweb (no flag)   ->  chatstore.ChatStore()  ->  data_dir()/chat.sqlite3
                        ->  data_dir() honours HERMES_LITE_CHAT_DATA
                        ->  default LITE/chatdata

## What this module does

`require_isolated_test_db()` computes the path the process will ACTUALLY use --
from the same inputs the product uses, in the same precedence order -- and refuses
unless that resolved path is a test database. It runs before any ChatStore is
constructed, so a refusal means no database was ever opened.

Precedence (highest first), matching the real code:

    1. explicit argument (--db-path equivalent), i.e. what the caller passes
    2. HERMES_STORE_PATH          -- only if the target actually reads it
    3. HERMES_LITE_CHAT_DATA      -- chatweb's effective override
    4. chatdata()/chat.sqlite3    -- the production default

The check is on the **resolved** path (`Path.resolve()`, which collapses `..`,
`.`, symlinks and case on Windows), so `chatdata/../chatdata/chat.sqlite3`,
`CHATDATA/CHAT.SQLITE3` and every alias all land on the same resolved target and
are rejected together.

## Deliberately NOT applied to normal use

The normal desktop app must keep using the production database. This module is
only imported by test entry points; `hermes_lite.chatdesktop` and
`hermes_lite.chatweb` never import it.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

LITE_DIR = Path(__file__).resolve().parents[1]      # src/hermes-lite
PROJECT_ROOT = LITE_DIR.parents[1]                  # project root

#: The production database and its data directory. Nothing under here is a test db.
PRODUCTION_DATA_DIR = (LITE_DIR / 'chatdata').resolve()
PRODUCTION_DB = (PRODUCTION_DATA_DIR / 'chat.sqlite3').resolve()


class ProductionDatabaseRefused(RuntimeError):
    """Raised BEFORE any database is opened. Carries the reason for the operator."""

    def __init__(self, reason: str, detail: str, resolved: Optional[Path],
                 how_to_fix: str):
        self.reason = reason
        self.detail = detail
        self.resolved = str(resolved) if resolved else None
        self.how_to_fix = how_to_fix
        super().__init__(self.summary())

    def summary(self) -> str:
        return f'[测试数据库隔离检查] 拒绝启动：{self.detail}'

    def report(self) -> str:
        lines = [
            '',
            '=' * 74,
            '测试数据库隔离检查：拒绝启动',
            '=' * 74,
            f'拒绝原因：{self.detail}',
            f'判定依据：{self.reason}',
        ]
        if self.resolved:
            lines.append(f'最终生效路径：{self.resolved}')
        lines += [
            f'生产库路径（禁止使用）：{PRODUCTION_DB}',
            '',
            '如何指定一个独立的测试数据库：',
            self.how_to_fix,
            '',
            '检查发生在数据库初始化、桌面进程或模型服务启动之前，',
            '因此本次没有任何数据库被打开，也没有任何进程被启动。',
            '=' * 74,
        ]
        return '\n'.join(lines)


HOW_TO_FIX = '\n'.join([
    '  * 直接跑脚本时：设置环境变量后重跑，例如',
    f'      $env:HERMES_LITE_CHAT_DATA = "{PROJECT_ROOT / "audit" / "<本轮审计目录>" / "logs" / "tmp"}"',
    '      （chatweb 读取该变量；DB 会落在该目录下的 chat.sqlite3）',
    '',
    '  * 用桌面测试启动器时：给 Desktop(db_path=...) 传一个位于审计目录或',
    '    专用临时目录下的新文件，例如',
    '      C.Desktop(RUN / "r53_test.sqlite3")',
    '    启动器会把它作为 --db-path 传给 chatdesktop，并以 HERMES_LITE_CHAT_DATA',
    '    指向同一目录作为兜底。',
    '',
    '  * 临时目录也可以：环境变量 TEMP 下的任意新目录均可。',
    '',
    '  * 不要指向 src/hermes-lite/chatdata/ —— 那是生产聊天库。',
])


def _looks_like_production(p: Path) -> bool:
    """True when `p` (already resolved) is the production db or inside its dir."""
    if p == PRODUCTION_DB:
        return True
    try:
        p.relative_to(PRODUCTION_DATA_DIR)
        return True
    except ValueError:
        return False


def _authorised_roots() -> list[Path]:
    """Where a test database is allowed to live.

    `audit/` (this round's and every earlier round's evidence) plus the OS temp
    directory. Nothing else is acceptable -- in particular not the repo root and
    not the product data directory.
    """
    roots = [(PROJECT_ROOT / 'audit').resolve(),
             Path(os.environ.get('TEMP') or os.environ.get('TMP') or '/tmp').resolve()]
    return roots


def _inside_authorised(p: Path) -> Optional[Path]:
    for root in _authorised_roots():
        try:
            p.relative_to(root)
            return root
        except ValueError:
            continue
    return None


def resolve_effective_db_path(
    explicit: Optional[str | Path] = None,
    *,
    target: str = 'chatdesktop',
) -> tuple[Optional[Path], str]:
    """Return (resolved_path, source_label) using the product's real precedence.

    `target` decides which env var is even consulted:

      * 'chatdesktop' — the process is told its store via --db-path, i.e. the
        `explicit` argument. HERMES_STORE_PATH is NOT read by the product, so it
        is ignored here (recording that fact is part of the fix).
      * 'chatweb'     — there is no --db-path flag; `HERMES_LITE_CHAT_DATA`
        decides the directory, then `chat.sqlite3` under it.

    Returns (None, 'unresolvable') when the caller passed nothing AND no env var
    is set for a target that needs one — the caller must treat that as a
    refusal, because the product would silently use production.
    """
    target = (target or '').strip().lower()

    if explicit is not None and str(explicit).strip() != '':
        p = Path(str(explicit).strip()).expanduser().resolve()
        return p, 'explicit(--db-path / db_path argument)'

    if target == 'chatweb':
        override = os.environ.get('HERMES_LITE_CHAT_DATA')
        if override and override.strip():
            d = Path(override.strip()).expanduser().resolve()
            return (d / 'chat.sqlite3'), 'env(HERMES_LITE_CHAT_DATA)'
        # no override -> the product default, which IS production
        return PRODUCTION_DB, 'default(chatdata()/chat.sqlite3)'

    # chatdesktop with no --db-path: DesktopController falls back to the same
    # production path. Report that honestly instead of pretending it is unset.
    #
    # HERMES_STORE_PATH is deliberately NOT consulted here even though the Round-49
    # launcher sets it: grep the product for that name and there are no readers.
    # Treating it as an override would make this guard report a safety it does not
    # have -- which is exactly the mistake that caused the Round-52 incident.
    return PRODUCTION_DB, 'default(DesktopController built-in production path)'


def require_isolated_test_db(
    explicit: Optional[str | Path] = None,
    *,
    target: str = 'chatdesktop',
) -> Path:
    """Validate the effective test database path, or raise. Call BEFORE opening.

    Parameters
    ----------
    explicit
        What the caller would hand to the product (`--db-path`, or a path for the
        Desktop helper). May be None to exercise the "forgot it" case.
    target
        'chatdesktop' or 'chatweb' — selects which resolution chain to model.

    Raises
    ------
    ProductionDatabaseRefused
        Always before any ChatStore is constructed.
    """
    resolved, source = resolve_effective_db_path(explicit, target=target)

    if resolved is None:
        raise ProductionDatabaseRefused(
            'unresolvable',
            '没有指定测试数据库路径，且没有可用的环境变量覆盖；'
            '继续运行会回落到生产聊天库。',
            None, HOW_TO_FIX)

    if _looks_like_production(resolved):
        why = ('最终生效路径就是生产聊天库'
               if resolved == PRODUCTION_DB else
               '最终生效路径位于生产数据目录 chatdata/ 内')
        raise ProductionDatabaseRefused(
            f'{why}（来源：{source}）',
            '测试被要求使用生产聊天库。测试绝不允许读写生产聊天库。',
            resolved, HOW_TO_FIX)

    root = _inside_authorised(resolved)
    if root is None:
        raise ProductionDatabaseRefused(
            f'最终生效路径不在允许的测试目录内（来源：{source}）',
            '测试数据库只能位于 audit/ 审计目录或系统临时目录下。',
            resolved,
            HOW_TO_FIX + '\n  * 允许的根目录：\n'
            + '\n'.join(f'      {r}' for r in _authorised_roots()))

    if resolved.is_dir():
        raise ProductionDatabaseRefused(
            f'最终生效路径是一个目录而不是数据库文件（来源：{source}）',
            '数据库路径必须指向一个文件。', resolved, HOW_TO_FIX)

    return resolved


def check_name_only() -> dict:
    """Small self-description used by tests and reports (no filesystem writes)."""
    return {
        'production_data_dir': str(PRODUCTION_DATA_DIR),
        'production_db': str(PRODUCTION_DB),
        'authorised_roots': [str(r) for r in _authorised_roots()],
        'env_read_by_chatweb': 'HERMES_LITE_CHAT_DATA',
        'env_NOT_read_by_product': 'HERMES_STORE_PATH',
        'resolves_to_production_when_unset': {
            'chatdesktop': str(PRODUCTION_DB),
            'chatweb': str(PRODUCTION_DB),
        },
    }


def describe_decision(explicit: Optional[str | Path], *, target: str = 'chatdesktop') -> dict:
    """Non-raising form for the verification suite: what WOULD happen."""
    try:
        p = require_isolated_test_db(explicit, target=target)
        resolved, source = resolve_effective_db_path(explicit, target=target)
        return {'allowed': True, 'resolved': str(p), 'source': source}
    except ProductionDatabaseRefused as exc:
        return {'allowed': False, 'resolved': exc.resolved, 'reason': exc.reason,
                'detail': exc.detail}
