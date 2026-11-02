"""Round 41: one place for chat runtime parameters and capability facts.

The chat product used to hardcode its budget model in several places at once:
`max_tokens` accepted only 128/512/1024, the model window only 2048/4096, the
page's custom-budget input stopped at 3584, and `available_windows` was
re-declared in `chatweb` and `chatdesktop`. This module replaces those copies
with a single contract that both the backend and the page read.

## The five things that must not be conflated

    1. DEFAULT      what a NEW session starts with (unchanged: 512 / 2048)
    2. PRESET       convenient choices offered in the UI (128/512/1024,
                    2048/4096). A preset is a shortcut, not a limit.
    3. PRODUCT RULE the range this product is willing to store and validate,
                    derived from the ACTUAL service window, not from a GPU model
    4. SERVICE FACT what the running server really reports (`/props` n_ctx)
    5. MODEL CLAIM  what the model file says it was trained for
                    (`/v1/models` meta.n_ctx_train). A claim, not a capability
                    of this machine.

`model_claims_32768` does NOT mean this 6GB card can run 32768: the declared
length is what the architecture supports, and the actual achievable window
depends on VRAM. That is why claim and actual are separate fields with separate
`source` strings, and why the page must never render the claim as "current".
"""
from __future__ import annotations

from typing import Any, Optional

# --------------------------------------------------------------------------- #
# 1. Defaults (unchanged by round 41)
# --------------------------------------------------------------------------- #
# These are what a NEW session gets. Keeping them identical to previous rounds
# means existing behaviour does not drift when the generalisation lands.
DEFAULT_MAX_OUTPUT_TOKENS = 512
DEFAULT_CUSTOM_BUDGET = 2048
DEFAULT_CONTEXT_WINDOW = 4096

# --------------------------------------------------------------------------- #
# 2. Presets (shortcuts shown in the UI, never validation limits)
# --------------------------------------------------------------------------- #
MAX_OUTPUT_PRESETS = (128, 512, 1024)
CONTEXT_WINDOW_PRESETS = (2048, 4096)

# --------------------------------------------------------------------------- #
# 3. Product validation rules
# --------------------------------------------------------------------------- #
# Reply budget: an integer, at least 32 tokens (below that a Chinese answer is
# truncated inside a sentence and the "reply cap" would be meaningless), and
# never more than the window itself can hold plus a margin -- the hard ceiling is
# enforced per-request against the ACTUAL window (see clamp_max_output), this is
# only the absolute sanity bound for a stored value.
MIN_MAX_OUTPUT_TOKENS = 32
ABSOLUTE_MAX_OUTPUT_TOKENS = 32768

# Context window: an integer inside a generous but finite band. The band exists
# because this round must not become a VRAM stress test: the app will not try
# values it cannot reason about, and it refuses nonsense instead of looping.
MIN_CONTEXT_WINDOW = 512
ABSOLUTE_MAX_CONTEXT_WINDOW = 131072
# Windows are multiples of this so the loader and the tokenizer agree on
# alignment; llama.cpp rounds n_ctx internally, and an unaligned target would
# make "actual == target" verification fail for a reason nobody can see.
CONTEXT_WINDOW_STEP = 256

# Custom input budget band. The upper bound is NOT a constant: it is derived from
# the live window minus the reply reserve (see custom_budget_limits). Only the
# floor is fixed, because a budget below ~256 tokens cannot hold the system
# prompt plus one short question. There is also an absolute ceiling so a
# corrupted/hostile stored value cannot survive sanitising: no input budget can
# exceed the largest window this product is willing to consider.
MIN_CUSTOM_INPUT_BUDGET = 256
ABSOLUTE_MAX_CUSTOM_BUDGET = ABSOLUTE_MAX_CONTEXT_WINDOW

# Reply reserve must leave at least this much room for the prompt, otherwise the
# session could never send anything at all.
MIN_INPUT_ROOM_TOKENS = 256


# --------------------------------------------------------------------------- #
# 4. Validation helpers (shared by backend and mirrored in the page)
# --------------------------------------------------------------------------- #
class ParamError(ValueError):
    """A rejected parameter, carrying a machine code and a human message."""

    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra

    def to_dict(self) -> dict:
        out = {'ok': False, 'error': self.code, 'message': self.message}
        out.update(self.extra)
        return out


def parse_strict_int(value: Any, field: str) -> int:
    """Integer parsing that refuses everything ambiguous.

    Explicitly rejected: None, '', bools (True/False are ints in Python and would
    silently become 1/0), floats and float-strings ("768.7" must not be truncated
    to 768), non-numeric strings, NaN/inf.
    """
    if value is None:
        raise ParamError(f'{field}_empty', f'{field} 不能为空。')
    if isinstance(value, bool):
        # bool is a subclass of int: refuse it BEFORE the int check below.
        raise ParamError(f'{field}_not_integer', f'{field} 必须是整数，不能是布尔值。')
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != value or value in (float('inf'), float('-inf')):
            raise ParamError(f'{field}_not_integer', f'{field} 必须是有限整数。')
        raise ParamError(f'{field}_not_integer',
                         f'{field} 必须是整数，收到小数 {value}（不会自动截断）。')
    if isinstance(value, str):
        s = value.strip()
        if not s:
            raise ParamError(f'{field}_empty', f'{field} 不能为空。')
        # Accept only a plain integer literal: no sign tricks, no decimals.
        try:
            n = int(s, 10)
        except ValueError:
            raise ParamError(f'{field}_not_integer',
                             f'{field} 必须是整数，收到「{s}」。') from None
        # Reject "0512" style and " 12 3" style inputs that int() tolerates oddly.
        if s.lstrip('+-').isdigit() is False:
            raise ParamError(f'{field}_not_integer',
                             f'{field} 必须是整数，收到「{s}」。')
        return n
    raise ParamError(f'{field}_not_integer', f'{field} 必须是整数。')


def validate_max_output(value: Any, window: Optional[int] = None) -> int:
    """Validate a reply reserve. Returns the integer or raises ParamError."""
    n = parse_strict_int(value, 'max_tokens')
    if n <= 0:
        raise ParamError('max_tokens_not_positive',
                         f'回复上限必须大于 0，收到 {n}。')
    if n < MIN_MAX_OUTPUT_TOKENS:
        raise ParamError('max_tokens_too_small',
                         f'回复上限至少 {MIN_MAX_OUTPUT_TOKENS} tokens，收到 {n}。')
    if n > ABSOLUTE_MAX_OUTPUT_TOKENS:
        raise ParamError('max_tokens_too_large',
                         f'回复上限最大 {ABSOLUTE_MAX_OUTPUT_TOKENS} tokens，收到 {n}。')
    if window:
        room = window - n
        if room < MIN_INPUT_ROOM_TOKENS:
            raise ParamError(
                'max_tokens_window_conflict',
                (f'回复上限 {n} 与当前实际窗口 {window} 冲突：'
                 f'留给输入的空间只剩 {room} tokens，'
                 f'至少要保留 {MIN_INPUT_ROOM_TOKENS} tokens。'
                 '请调小回复上限，或先把模型窗口调大。'),
                window=window, max_tokens=n, input_room=room,
                min_input_room=MIN_INPUT_ROOM_TOKENS)
    return n


def validate_context_window(value: Any) -> int:
    """Validate a requested context window (the TARGET, not the actual)."""
    n = parse_strict_int(value, 'window')
    if n <= 0:
        raise ParamError('window_not_positive', f'上下文窗口必须大于 0，收到 {n}。')
    if n < MIN_CONTEXT_WINDOW:
        raise ParamError('window_too_small',
                         f'上下文窗口至少 {MIN_CONTEXT_WINDOW} tokens，收到 {n}。')
    if n > ABSOLUTE_MAX_CONTEXT_WINDOW:
        raise ParamError('window_too_large',
                         f'上下文窗口最大 {ABSOLUTE_MAX_CONTEXT_WINDOW} tokens，收到 {n}。')
    if n % CONTEXT_WINDOW_STEP:
        raise ParamError(
            'window_not_aligned',
            (f'上下文窗口需要是 {CONTEXT_WINDOW_STEP} 的整数倍，收到 {n}。'
             f'相邻可取值为 {(n // CONTEXT_WINDOW_STEP) * CONTEXT_WINDOW_STEP} '
             f'或 {(n // CONTEXT_WINDOW_STEP + 1) * CONTEXT_WINDOW_STEP}。'),
            step=CONTEXT_WINDOW_STEP)
    return n


def validate_custom_budget(value: Any, window: Optional[int] = None,
                           max_output: Optional[int] = None) -> int:
    """Validate a custom input budget against the LIVE window."""
    n = parse_strict_int(value, 'custom_budget')
    if n <= 0:
        raise ParamError('custom_budget_not_positive',
                         f'自定义输入预算必须大于 0，收到 {n}。')
    if n < MIN_CUSTOM_INPUT_BUDGET:
        raise ParamError('custom_budget_too_small',
                         f'自定义输入预算至少 {MIN_CUSTOM_INPUT_BUDGET} tokens，收到 {n}。')
    if n > ABSOLUTE_MAX_CUSTOM_BUDGET:
        # Absolute sanity bound, independent of the live window: a stored value
        # beyond any window this product considers is simply invalid.
        raise ParamError('custom_budget_too_large',
                         f'自定义输入预算最大 {ABSOLUTE_MAX_CUSTOM_BUDGET} tokens，收到 {n}。')
    if window and max_output:
        upper = window - max_output
        if upper < MIN_CUSTOM_INPUT_BUDGET:
            raise ParamError(
                'budget_window_conflict',
                (f'当前实际窗口 {window} 减去回复预留 {max_output} 后只剩 '
                 f'{upper} tokens，不足以设置自定义输入预算。'
                 '请调小回复上限或调大窗口。'),
                window=window, max_tokens=max_output, upper_bound=upper)
        if n > upper:
            raise ParamError(
                'custom_budget_exceeds_room',
                (f'自定义输入预算 {n} 超出当前可用空间：'
                 f'窗口 {window} − 回复预留 {max_output} = {upper} tokens。'
                 f'请输入不超过 {upper} 的值，或调整窗口/回复上限。'),
                window=window, max_tokens=max_output, upper_bound=upper)
    return n


def validate_runtime_settings(settings: dict, window: Optional[int]) -> None:
    """Validate the complete effective pair before any chat side effects."""
    if type(window) is not int or window <= 0:
        raise ParamError('actual_window_unknown',
                         '尚未确认模型服务的实际上下文窗口，请检查服务后重试。')
    reply = validate_max_output(settings.get('max_tokens', DEFAULT_MAX_OUTPUT_TOKENS), window)
    if settings.get('history_mode') == 'custom':
        validate_custom_budget(settings.get('custom_budget', DEFAULT_CUSTOM_BUDGET), window, reply)


def custom_budget_limits(window: Optional[int], max_output: int) -> dict:
    """The [min, max, step] band for the custom-budget input, live-derived."""
    lo = MIN_CUSTOM_INPUT_BUDGET
    if not window:
        # Unknown actual window: report the floor only, and say so.
        return {'min': lo, 'max': None, 'step': 64,
                'known': False,
                'reason': '尚未获得实际上下文窗口，无法确定上限'}
    hi = max(0, int(window) - int(max_output))
    return {'min': lo, 'max': hi if hi >= lo else None, 'step': 64,
            'known': hi >= lo,
            'reason': None if hi >= lo else
            f'窗口 {window} − 回复预留 {max_output} 不足以容纳任何自定义预算'}


# --------------------------------------------------------------------------- #
# 5. The capability snapshot handed to the page
# --------------------------------------------------------------------------- #
def capability_snapshot(*, alias: Optional[str],
                        display_name: Optional[str],
                        target_window: Optional[int],
                        actual_window: Optional[int],
                        actual_source: str,
                        declared_window: Optional[int],
                        declared_source: str,
                        owned: bool,
                        status: str,
                        max_output: int,
                        history_mode: str,
                        custom_budget: Optional[int]) -> dict:
    """Everything the page needs to render windows, budgets and limits.

    Each fact carries its own `source` so the UI can distinguish a measured
    value from a configured one. Nothing here falls back to "4096 means fine":
    when a value is unknown it is reported as None/unknown and the page says so.
    """
    budget_limits = custom_budget_limits(actual_window, max_output)
    if actual_window:
        auto_input_budget = max(0, int(actual_window) - int(max_output))
    else:
        auto_input_budget = None

    return {
        'model': {
            'alias': alias,
            'display_name': display_name,
            'display_source': ('deployment-config' if display_name else 'unknown'),
        },
        'window': {
            'target': target_window,
            'target_source': 'user-request',
            'actual': actual_window,
            'actual_source': actual_source,
            'declared': declared_window,
            'declared_source': declared_source,
            'matches_target': (actual_window is not None
                               and target_window is not None
                               and int(actual_window) == int(target_window)),
            'presets': list(CONTEXT_WINDOW_PRESETS),
            'step': CONTEXT_WINDOW_STEP,
            'min': MIN_CONTEXT_WINDOW,
            'max': ABSOLUTE_MAX_CONTEXT_WINDOW,
        },
        'reply': {
            'max_output': max_output,
            'default': DEFAULT_MAX_OUTPUT_TOKENS,
            'presets': list(MAX_OUTPUT_PRESETS),
            'min': MIN_MAX_OUTPUT_TOKENS,
            'max': ABSOLUTE_MAX_OUTPUT_TOKENS,
            'label': '回复上限',
        },
        'input': {
            'history_mode': history_mode,
            'auto_budget': auto_input_budget,
            'custom_budget': custom_budget,
            'custom_limits': budget_limits,
            'min_room': MIN_INPUT_ROOM_TOKENS,
        },
        'service': {
            'owned': owned,
            'status': status,
            # Only an owned service may be restarted by this app.
            'window_resizable': bool(owned),
            'resize_hint': None if owned else
            '当前使用外部模型服务，本应用不会重启它。请在该服务的启动参数中设置上下文长度。',
        },
    }
