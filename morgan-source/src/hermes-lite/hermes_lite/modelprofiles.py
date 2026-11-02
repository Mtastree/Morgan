"""Round 43: the local model profile registry.

## Why this module exists

Before this round the product had exactly ONE model, and its identity was
scattered: `configs/deployment.json` held the alias / file / window / GPU layers,
`chatdesktop.py` read that file at four different places, `chatweb.py` read it
twice more, `chatengine.deployment()` read it again, and `scripts/local_model.py`
read it once per command. Every one of those readers assumed "the deployment file
IS the current model".

That assumption breaks the moment more than one model configuration exists. The
registry below is the single owner of "what model configurations exist". Nothing
else may invent a model identity.

## The four profile ids that must stay distinct

    default_profile_id   what the app loads at NEXT START (a registry setting)
    target/selected      what the user ASKED to load (page state / switch request)
    current_profile_id   what is ACTUALLY RUNNING (set only after verification)
    last_success         the last profile that loaded and verified successfully

Editing a profile never changes what is running. Asking to load a profile never
changes `last_success`; only a verified load does. `default_profile_id` is not
"current" either: a switched session keeps running the switched profile until the
next start.

## File location

    <project>/configs/model_profiles.json

next to `deployment.json`, which stays untouched as the DEPLOYMENT-level file
(endpoint, runtime executable, port). A profile only overrides the
MODEL-level fields. See `effective_deployment()`.

## Honesty rules encoded here

* A missing registry is migrated ONCE from `deployment.json`, idempotently; a
  present registry is never re-imported, so deleting the bootstrap profile does
  not resurrect it on the next start.
* A CORRUPT registry raises `ProfileError`. It is never silently replaced by an
  empty list and never overwritten. `load_registry()` is read-only.
* Writes are atomic (temp file + fsync + `os.replace`), so an interrupted process
  cannot leave half a JSON document behind.
* Profile ids are stable; renaming a profile never changes its id.
* The model file is never copied, moved, hashed in full, or deleted here. A path
  check reads the 4-byte GGUF magic and the size, nothing more.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

from hermes_lite import chatparams

SCHEMA_VERSION = 1

LITE_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = LITE_DIR.parents[1]
DEFAULT_REGISTRY_PATH = PROJECT_ROOT / 'configs' / 'model_profiles.json'
DEPLOYMENT_PATH = PROJECT_ROOT / 'configs' / 'deployment.json'

#: The id of the profile imported from `deployment.json` on first use. It is a
#: fixed constant on purpose: a rename or a window change must not create a new
#: profile, and "has the import already happened" must not depend on a display
#: name the user may have edited.
BOOTSTRAP_PROFILE_ID = 'mp-bootstrap-deployment'

#: Serving alias charset. The alias goes on a llama-server command line, so it
#: must not contain whitespace, quotes or shell metacharacters.
ALIAS_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')

MAX_DISPLAY_NAME = 64
#: `-ngl` accepts large values (llama.cpp clamps to the layer count), but a
#: nonsense ceiling still catches typos such as 999999.
MAX_GPU_LAYERS = 4096

#: Deployment-level keys a profile is NOT allowed to override. The endpoint and
#: the runtime executable belong to the deployment, not to a model choice.
NON_PROFILE_KEYS = ('host', 'port', 'base_url', 'runtime', 'runtime_version',
                    'scope')

_LOCK = threading.RLock()


class ProfileError(ValueError):
    """A rejected profile operation, carrying a machine code and a message."""

    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra

    def to_dict(self) -> dict:
        out = {'ok': False, 'error': self.code, 'message': self.message}
        out.update(self.extra)
        return out


# --------------------------------------------------------------------------- #
# Paths and IO
# --------------------------------------------------------------------------- #
def now_iso() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S%z')


def registry_path() -> Path:
    """Where the registry lives. `HERMES_MODEL_PROFILES_PATH` exists for tests."""
    override = os.environ.get('HERMES_MODEL_PROFILES_PATH')
    return Path(override).resolve() if override else DEFAULT_REGISTRY_PATH


def deployment_path() -> Path:
    override = os.environ.get('HERMES_MODEL_DEPLOYMENT_PATH')
    return Path(override).resolve() if override else DEPLOYMENT_PATH


def atomic_write_json(path: Path, data: Any) -> None:
    """temp + fsync + os.replace. An interrupted write cannot leave half a file."""
    p = Path(path).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.parent / f'{p.name}.tmp_{os.getpid()}_{time.time_ns()}'
    try:
        with tmp.open('w', encoding='utf-8', newline='\n') as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=2))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def read_deployment() -> dict:
    p = deployment_path()
    if not p.is_file():
        raise ProfileError('deployment_missing',
                           f'找不到部署配置：{p}')
    try:
        raw = json.loads(p.read_text(encoding='utf-8-sig'))
    except Exception as exc:  # noqa: BLE001
        raise ProfileError('deployment_unreadable',
                           f'部署配置无法解析：{p}（{exc}）') from None
    if not isinstance(raw, dict):
        raise ProfileError('deployment_invalid', f'部署配置不是对象：{p}')
    return raw


# --------------------------------------------------------------------------- #
# Model path handling
# --------------------------------------------------------------------------- #
def resolve_model_path(raw: str) -> tuple[Path, str, str]:
    """Resolve a user-supplied model path.

    Returns `(absolute_path, declared_text, base_description)`.

    Relative paths are resolved against the PROJECT ROOT, explicitly and always
    -- not against the current working directory (which differs between the
    desktop entry, `start.cmd` and a test) and not against the user's home. The
    base is reported back so the page can say what it resolved to.
    """
    text = (raw or '').strip().strip('"').strip("'")
    if not text:
        raise ProfileError('model_path_empty', '模型文件路径不能为空。')
    p = Path(text)
    if p.is_absolute():
        return (p.resolve(), text, 'absolute')
    return ((PROJECT_ROOT / p).resolve(), text, 'project-root')


def describe_model_file(path: Path) -> dict:
    """Cheap facts about a model file. Never a full-file hash.

    A 1.1 GB GGUF hashed on every profile operation would make the model manager
    unusable and is not needed: the loader itself rejects a corrupt file. What IS
    needed is "the path exists, is a file, and starts with GGUF".
    """
    out: dict[str, Any] = {'path': str(path), 'exists': False, 'is_file': False,
                           'bytes': None, 'gguf_magic': None, 'looks_like_gguf': False,
                           'error': None}
    try:
        out['exists'] = path.exists()
        out['is_file'] = path.is_file()
    except OSError as exc:
        out['error'] = f'{type(exc).__name__}: {exc}'
        return out
    if not out['is_file']:
        return out
    try:
        out['bytes'] = path.stat().st_size
        with path.open('rb') as stream:
            head = stream.read(4)
        out['gguf_magic'] = head.decode('latin-1')
        out['looks_like_gguf'] = head == b'GGUF'
    except OSError as exc:
        out['error'] = f'{type(exc).__name__}: {exc}'
    return out


# --------------------------------------------------------------------------- #
# Field validation
# --------------------------------------------------------------------------- #
def _clean_display_name(value: Any) -> str:
    if not isinstance(value, str):
        raise ProfileError('display_name_invalid', '显示名称必须是文本。')
    name = value.strip()
    if not name:
        raise ProfileError('display_name_empty', '显示名称不能为空。')
    if len(name) > MAX_DISPLAY_NAME:
        raise ProfileError('display_name_too_long',
                           f'显示名称最多 {MAX_DISPLAY_NAME} 个字符，收到 {len(name)} 个。')
    if '\n' in name or '\r' in name:
        raise ProfileError('display_name_invalid', '显示名称不能包含换行。')
    return name


def _clean_alias(value: Any, display_name: str) -> str:
    """The serving alias. Separate from the display name and from the file name."""
    if value is None or (isinstance(value, str) and not value.strip()):
        if not display_name:
            raise ProfileError('alias_empty', '服务别名不能为空。')
        # Derive a legal alias from the display name for convenience: ASCII kept,
        # everything else folded to '-'. Uniqueness is checked by the caller.
        base = re.sub(r'[^A-Za-z0-9._-]+', '-', display_name).strip('-._')
        if not base:
            base = 'model'
        return base[:48]
    if not isinstance(value, str):
        raise ProfileError('alias_invalid', '服务别名必须是文本。')
    alias = value.strip()
    if not ALIAS_RE.match(alias):
        raise ProfileError(
            'alias_invalid',
            (f'服务别名只能包含字母、数字、点、下划线和连字符，且必须以字母或数字开头：'
             f'「{alias}」。'))
    return alias


def _clean_gpu_layers(value: Any) -> int:
    n = chatparams.parse_strict_int(value, 'gpu_layers')
    if n < 0:
        raise ProfileError('gpu_layers_negative',
                           f'GPU 卸载层数不能为负，收到 {n}。')
    if n > MAX_GPU_LAYERS:
        raise ProfileError('gpu_layers_too_large',
                           f'GPU 卸载层数最多 {MAX_GPU_LAYERS}，收到 {n}。')
    return n


def _clean_advanced(raw: Any) -> dict:
    """Advanced runtime passthrough. Not exposed in the first version of the UI.

    Everything here is optional; omitted keys inherit `deployment.json`, which is
    how the verified defaults are reused instead of being re-typed.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ProfileError('advanced_invalid', '高级运行参数必须是对象。')
    out: dict[str, Any] = {}
    for key in ('parallel', 'batch_size', 'ubatch_size'):
        if key in raw and raw[key] is not None:
            n = chatparams.parse_strict_int(raw[key], key)
            if n <= 0:
                raise ProfileError(f'{key}_not_positive',
                                   f'{key} 必须大于 0，收到 {n}。')
            out[key] = n
    for key in ('flash_attention', 'offline'):
        if key in raw and raw[key] is not None:
            if not isinstance(raw[key], bool):
                raise ProfileError(f'{key}_not_bool', f'{key} 必须是布尔值。')
            out[key] = raw[key]
    return out


def validate_profile_fields(data: dict) -> dict:
    """Normalise and validate the user-editable fields of one profile.

    Raises `ProfileError` with a specific code. Returns the cleaned field dict
    (no id, no timestamps -- the caller owns those).
    """
    if not isinstance(data, dict):
        raise ProfileError('profile_invalid', '模型配置必须是对象。')

    display_name = _clean_display_name(data.get('display_name'))
    declared = data.get('model_path')
    path, declared_text, base = resolve_model_path(
        declared if isinstance(declared, str) else '')

    try:
        window = chatparams.validate_context_window(data.get('target_context_window'))
    except chatparams.ParamError as exc:
        raise ProfileError(exc.code, f'目标上下文窗口不合法：{exc.message}',
                           **exc.extra) from None

    gpu_layers = _clean_gpu_layers(data.get('gpu_layers'))

    try:
        max_out = chatparams.validate_max_output(data.get('default_max_output_tokens'))
    except chatparams.ParamError as exc:
        raise ProfileError(exc.code, f'默认回复上限不合法：{exc.message}',
                           **exc.extra) from None
    # The profile's reply reserve must leave usable input room in ITS window, or
    # every new session created from it would be born invalid.
    room = window - max_out
    if room < chatparams.MIN_INPUT_ROOM_TOKENS:
        raise ProfileError(
            'profile_reply_exceeds_window',
            (f'默认回复上限 {max_out} 与目标窗口 {window} 冲突：'
             f'留给输入的空间只有 {room} tokens，至少要 {chatparams.MIN_INPUT_ROOM_TOKENS}。'),
            window=window, max_output=max_out, input_room=room)

    alias = _clean_alias(data.get('serving_alias'), display_name)
    advanced = _clean_advanced(data.get('advanced'))

    return {
        'display_name': display_name,
        'model_path': str(path),
        'model_path_declared': declared_text,
        'path_base': base,
        'serving_alias': alias,
        'target_context_window': window,
        'gpu_layers': gpu_layers,
        'default_max_output_tokens': max_out,
        'advanced': advanced,
    }


def profile_model_overrides(profile: dict) -> dict:
    """The deployment keys a profile overrides, in deployment.json vocabulary."""
    adv = profile.get('advanced') or {}
    out = {
        'alias': profile['serving_alias'],
        'model': profile['model_path'],
        'context_size': profile['target_context_window'],
        'gpu_layers_requested': profile['gpu_layers'],
    }
    for key in ('parallel', 'batch_size', 'ubatch_size'):
        if key in adv:
            out[key] = adv[key]
    for key in ('flash_attention', 'offline'):
        if key in adv:
            out[key] = adv[key]
    return out


# --------------------------------------------------------------------------- #
# Registry read/write
# --------------------------------------------------------------------------- #
def _empty_registry() -> dict:
    return {
        'schema_version': SCHEMA_VERSION,
        'deployment_import': {'done': False, 'at': None,
                              'source': None, 'profile_id': None},
        'default_profile_id': None,
        'last_success_profile_id': None,
        'profiles': [],
    }


def load_registry(*, allow_missing: bool = True) -> dict:
    """Read the registry. READ-ONLY: this function never writes anything.

    Missing file  -> empty structure with `initialized: False` (if allowed).
    Corrupt file  -> `ProfileError('registry_corrupt')`, carrying the path and the
                     parse error, so the caller can tell the user instead of
                     showing an empty model list.
    """
    p = registry_path()
    if not p.is_file():
        if not allow_missing:
            raise ProfileError('registry_missing', f'模型配置注册表不存在：{p}')
        reg = _empty_registry()
        reg['initialized'] = False
        reg['path'] = str(p)
        return reg
    try:
        raw = json.loads(p.read_text(encoding='utf-8-sig'))
    except Exception as exc:  # noqa: BLE001
        raise ProfileError(
            'registry_corrupt',
            f'模型配置注册表无法解析（不会被自动覆盖）：{p}',
            path=str(p), detail=f'{type(exc).__name__}: {exc}') from None
    return _validate_registry(raw, p)


def _validate_registry(raw: Any, p: Path) -> dict:
    if not isinstance(raw, dict):
        raise ProfileError('registry_invalid',
                           f'模型配置注册表结构不正确（应为对象）：{p}', path=str(p))
    profiles = raw.get('profiles')
    if not isinstance(profiles, list):
        raise ProfileError('registry_invalid',
                           f'模型配置注册表缺少 profiles 列表：{p}', path=str(p))
    seen: set[str] = set()
    clean: list[dict] = []
    for item in profiles:
        if not isinstance(item, dict):
            raise ProfileError('registry_invalid',
                               f'模型配置注册表包含非对象条目：{p}', path=str(p))
        pid = item.get('profile_id')
        if not isinstance(pid, str) or not pid.strip():
            raise ProfileError('registry_invalid',
                               f'模型配置注册表存在缺少 profile_id 的条目：{p}',
                               path=str(p))
        if pid in seen:
            raise ProfileError('registry_invalid',
                               f'模型配置注册表存在重复 profile_id：{pid}',
                               path=str(p), profile_id=pid)
        seen.add(pid)
        clean.append(item)
    reg = {
        'schema_version': raw.get('schema_version') or SCHEMA_VERSION,
        'deployment_import': raw.get('deployment_import') or {
            'done': False, 'at': None, 'source': None, 'profile_id': None},
        'default_profile_id': raw.get('default_profile_id'),
        'last_success_profile_id': raw.get('last_success_profile_id'),
        'profiles': clean,
        'initialized': True,
        'path': str(p),
    }
    # Another reader may have written a default that no longer exists (e.g. an
    # external edit). Report it instead of silently inventing one.
    dp = reg.get('default_profile_id')
    if dp and dp not in seen:
        reg['default_profile_id_missing'] = True
    return reg


def save_registry(reg: dict) -> None:
    with _LOCK:
        payload = {
            'schema_version': SCHEMA_VERSION,
            'deployment_import': reg.get('deployment_import') or {
                'done': False, 'at': None, 'source': None, 'profile_id': None},
            'default_profile_id': reg.get('default_profile_id'),
            'last_success_profile_id': reg.get('last_success_profile_id'),
            'profiles': list(reg.get('profiles') or []),
        }
        atomic_write_json(registry_path(), payload)


# --------------------------------------------------------------------------- #
# Migration from deployment.json
# --------------------------------------------------------------------------- #
def bootstrap_profile_from_deployment(dep: Optional[dict] = None) -> dict:
    """Build the profile that represents the model the project already runs."""
    dep = dep if dep is not None else read_deployment()
    model_rel = dep.get('model')
    if not model_rel:
        raise ProfileError('deployment_invalid',
                           '部署配置缺少 model 字段，无法导入模型配置。')
    path, declared, base = resolve_model_path(str(model_rel))
    window = dep.get('context_size') or chatparams.DEFAULT_CONTEXT_WINDOW
    alias = dep.get('alias') or 'local-model'
    ts = now_iso()
    advanced: dict[str, Any] = {}
    for key in ('parallel', 'batch_size', 'ubatch_size',
                'flash_attention', 'offline'):
        if key in dep:
            advanced[key] = dep[key]
    try:
        window_ok = chatparams.validate_context_window(window)
    except chatparams.ParamError:
        window_ok = chatparams.DEFAULT_CONTEXT_WINDOW
    default_out = chatparams.DEFAULT_MAX_OUTPUT_TOKENS
    if window_ok - default_out < chatparams.MIN_INPUT_ROOM_TOKENS:
        default_out = max(chatparams.MIN_MAX_OUTPUT_TOKENS,
                          window_ok - chatparams.MIN_INPUT_ROOM_TOKENS)
    return {
        'profile_id': BOOTSTRAP_PROFILE_ID,
        'display_name': str(Path(str(model_rel)).stem),
        'model_path': str(path),
        'model_path_declared': declared,
        'path_base': base,
        'serving_alias': alias,
        'target_context_window': window_ok,
        'gpu_layers': int(dep.get('gpu_layers_requested') or 0),
        'default_max_output_tokens': default_out,
        'advanced': advanced,
        'origin': 'migrated-from-deployment',
        'created_at': ts,
        'updated_at': ts,
    }


def ensure_initialized() -> dict:
    """Load the registry, importing `deployment.json` exactly once if absent.

    Idempotent in the strong sense: once `deployment_import.done` is true the
    import never runs again, so a user who deletes the bootstrap profile does not
    find it re-created on the next launch.
    """
    with _LOCK:
        p = registry_path()
        if not p.is_file():
            prof = bootstrap_profile_from_deployment()
            reg = _empty_registry()
            reg['profiles'] = [prof]
            reg['default_profile_id'] = prof['profile_id']
            reg['deployment_import'] = {
                'done': True, 'at': now_iso(),
                'source': str(deployment_path()),
                'profile_id': prof['profile_id'],
            }
            reg['initialized'] = True
            reg['path'] = str(p)
            save_registry(reg)
            return load_registry()
        reg = load_registry()
        if not reg.get('deployment_import', {}).get('done'):
            # A registry exists but the import was never recorded (hand-written
            # file). Record it only if the bootstrap profile is genuinely absent.
            if not any(x.get('profile_id') == BOOTSTRAP_PROFILE_ID
                       for x in reg['profiles']):
                try:
                    prof = bootstrap_profile_from_deployment()
                    reg['profiles'].append(prof)
                    if not reg.get('default_profile_id'):
                        reg['default_profile_id'] = prof['profile_id']
                except ProfileError:
                    prof = None
            else:
                prof = None
            reg['deployment_import'] = {
                'done': True, 'at': now_iso(),
                'source': str(deployment_path()),
                'profile_id': (prof or {}).get('profile_id'),
            }
            save_registry(reg)
            return load_registry()
        return reg


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #
def get_profile(reg: dict, profile_id: str) -> Optional[dict]:
    for p in reg.get('profiles') or []:
        if p.get('profile_id') == profile_id:
            return p
    return None


def require_profile(reg: dict, profile_id: str) -> dict:
    prof = get_profile(reg, profile_id)
    if prof is None:
        raise ProfileError('profile_not_found',
                           f'模型配置不存在：{profile_id}', profile_id=profile_id)
    return prof


def _new_profile_id() -> str:
    return f'mp-{os.urandom(8).hex()}'


def _assert_alias_unique(reg: dict, alias: str, *, ignore_id: Optional[str] = None) -> None:
    """Two profiles may share a FILE, but not a serving alias.

    Sharing a file is a supported case (same GGUF at 3072 vs 4096). Sharing an
    alias is not: the alias is how the running service identifies itself, so a
    duplicate would make identity verification meaningless.
    """
    for p in reg.get('profiles') or []:
        if p.get('profile_id') == ignore_id:
            continue
        if (p.get('serving_alias') or '').lower() == alias.lower():
            raise ProfileError(
                'alias_conflict',
                (f'服务别名「{alias}」已被配置「{p.get("display_name")}」使用。'
                 '两个配置不能共用同一别名，否则无法区分实际运行的是哪一份。'),
                alias=alias, conflict_profile_id=p.get('profile_id'))


def create_profile(data: dict, *, registry: Optional[dict] = None) -> dict:
    """Add a profile. Never touches the model file, never changes what runs."""
    with _LOCK:
        reg = registry or ensure_initialized()
        fields = validate_profile_fields(data)
        _assert_alias_unique(reg, fields['serving_alias'])
        ts = now_iso()
        prof = dict(fields)
        prof.update({
            'profile_id': _new_profile_id(),
            'origin': 'user',
            'created_at': ts,
            'updated_at': ts,
        })
        reg['profiles'].append(prof)
        if not reg.get('default_profile_id'):
            reg['default_profile_id'] = prof['profile_id']
        save_registry(reg)
        return prof


def update_profile(profile_id: str, data: dict, *,
                   registry: Optional[dict] = None) -> dict:
    """Edit a profile in place. The profile_id never changes, even on rename."""
    with _LOCK:
        reg = registry or ensure_initialized()
        prof = require_profile(reg, profile_id)
        fields = validate_profile_fields(data)
        _assert_alias_unique(reg, fields['serving_alias'], ignore_id=profile_id)
        prof.update(fields)
        prof['updated_at'] = now_iso()
        save_registry(reg)
        return prof


def delete_profile(profile_id: str, *, registry: Optional[dict] = None,
                   new_default_profile_id: Optional[str] = None,
                   current_profile_id: Optional[str] = None,
                   target_profile_id: Optional[str] = None,
                   protected_profile_ids: Optional[set[str] | list[str]] = None) -> dict:
    """Remove a CONFIGURATION. Never removes the GGUF file.

    Refusals, each with its own code:
      * the profile does not exist;
      * it is the profile that is ACTUALLY RUNNING (stop it or switch away first);
      * it is the last remaining profile (a registry with no model is not usable);
      * it is the default and no replacement was named.
    """
    with _LOCK:
        reg = registry or ensure_initialized()
        prof = require_profile(reg, profile_id)
        if current_profile_id and profile_id == current_profile_id:
            raise ProfileError(
                'cannot_delete_running_profile',
                ('该配置当前正在运行，不能直接删除。'
                 '请先切换到其他配置，或关闭模型服务后再删除。'),
                profile_id=profile_id)
        if target_profile_id and profile_id == target_profile_id:
            raise ProfileError(
                'cannot_delete_target_profile',
                ('该配置正在作为目标加载切换中，不能直接删除。'
                 '请等待切换完成或取消后再操作。'),
                profile_id=profile_id)
        if protected_profile_ids and profile_id in protected_profile_ids:
            raise ProfileError(
                'cannot_delete_protected_profile',
                ('该配置正在切换流程中被依赖（如回滚备份或正在切换），不能直接删除。'),
                profile_id=profile_id)
        if len(reg['profiles']) <= 1:
            raise ProfileError(
                'cannot_delete_last_profile',
                '这是最后一个模型配置，不能删除。请先添加另一个配置。',
                profile_id=profile_id)
        was_default = profile_id == reg.get('default_profile_id')
        if was_default:
            replacement = new_default_profile_id
            if not replacement:
                others = [p for p in reg['profiles']
                          if p.get('profile_id') != profile_id]
                raise ProfileError(
                    'default_profile_requires_replacement',
                    ('该配置是默认启动配置。删除前请指定新的默认配置。'),
                    profile_id=profile_id,
                    candidates=[{'profile_id': p['profile_id'],
                                 'display_name': p.get('display_name')}
                                for p in others])
            require_profile(reg, replacement)
            if replacement == profile_id:
                raise ProfileError('default_profile_requires_replacement',
                                   '新的默认配置不能是被删除的配置本身。')
        reg['profiles'] = [p for p in reg['profiles']
                           if p.get('profile_id') != profile_id]
        if was_default:
            reg['default_profile_id'] = new_default_profile_id
        if reg.get('last_success_profile_id') == profile_id:
            # History is not rewritten to a new model: record that the entry is
            # gone instead of pretending another model was the last success.
            reg['last_success_profile_id'] = None
        save_registry(reg)
        return {'ok': True, 'deleted_profile_id': profile_id,
                'deleted_display_name': prof.get('display_name'),
                'model_file_kept': prof.get('model_path'),
                'new_default_profile_id': reg.get('default_profile_id')}


def set_default_profile(profile_id: str, *,
                        registry: Optional[dict] = None) -> dict:
    with _LOCK:
        reg = registry or ensure_initialized()
        prof = require_profile(reg, profile_id)
        reg['default_profile_id'] = profile_id
        save_registry(reg)
        return {'ok': True, 'default_profile_id': profile_id,
                'display_name': prof.get('display_name')}


def set_last_success_profile(profile_id: Optional[str]) -> None:
    """Record a VERIFIED successful load. Called only by the controller."""
    with _LOCK:
        reg = ensure_initialized()
        reg['last_success_profile_id'] = profile_id
        save_registry(reg)


# --------------------------------------------------------------------------- #
# Effective deployment
# --------------------------------------------------------------------------- #
def effective_deployment(profile_id: Optional[str] = None, *,
                         strict: bool = False) -> dict:
    """`deployment.json` with one profile's model-level fields applied.

    This is what every consumer should use instead of reading the raw file, so
    that "which model is configured" has exactly one answer.

    Fallback rules:
      * no registry          -> the raw deployment file, unchanged;
      * corrupt registry     -> `strict=True` raises; `strict=False` returns the
                                raw file PLUS `registry_error`, so an auxiliary
                                reader can still work while the page shows the
                                problem. It is never silently blank.
    """
    dep = read_deployment()
    out = dict(dep)
    out['profile_id'] = None
    out['profile_source'] = 'deployment-file'
    try:
        reg = load_registry()
    except ProfileError as exc:
        if strict:
            raise
        out['registry_error'] = exc.to_dict()
        return out
    if not reg.get('profiles'):
        return out
    pid = profile_id or reg.get('default_profile_id')
    prof = get_profile(reg, pid) if pid else None
    if prof is None:
        if profile_id:
            raise ProfileError('profile_not_found',
                               f'模型配置不存在：{profile_id}', profile_id=profile_id)
        return out
    out.update(profile_model_overrides(prof))
    out['profile_id'] = prof['profile_id']
    out['profile_display_name'] = prof.get('display_name')
    out['profile_source'] = 'model-registry'
    out['default_max_output_tokens'] = prof.get('default_max_output_tokens')
    return out


def runtime_model_config(profile_id: Optional[str] = None) -> dict:
    """The dict handed to `scripts/local_model.py --config` for one profile.

    Built from the EFFECTIVE deployment so that a profile only states what it
    changes; everything else keeps the verified deployment value.
    """
    dep = effective_deployment(profile_id, strict=True)
    dep['profile_id'] = dep.get('profile_id')
    return dep


def is_profile_usable(p: Optional[dict]) -> bool:
    """A profile is USABLE when its model file exists and starts with GGUF."""
    if not p or not isinstance(p, dict):
        return False
    path_str = p.get('model_path') or ''
    if not path_str:
        return False
    info = describe_model_file(Path(path_str))
    return bool(info.get('is_file') and info.get('looks_like_gguf'))


def resolve_startup_target(
    *,
    explicit_profile_id: Optional[str] = None,
    explicit_config_path: Optional[str | Path] = None,
) -> dict[str, Any]:
    """Unified startup target resolution for desktop and legacy launcher.

    Returns dict with keys:
      'profile': Optional[dict]
      'profile_id': Optional[str]
      'source': str
      'fallback_reason': Optional[str]
      'effective_config': dict
      'registry_error': Optional[dict]
    """
    # 1. Explicit config path override (--config)
    if explicit_config_path:
        p = Path(explicit_config_path).resolve()
        if not p.is_file():
            raise ProfileError('explicit_config_missing', f'显式指定的配置文件不存在：{p}')
        try:
            data = json.loads(p.read_text(encoding='utf-8-sig'))
        except Exception as exc:  # noqa: BLE001
            raise ProfileError('explicit_config_corrupt', f'显式指定的配置文件无法解析：{p} ({exc})') from exc
        if not isinstance(data, dict):
            raise ProfileError('explicit_config_invalid', f'显式指定的配置内容不是对象：{p}')
        return {
            'profile': None,
            'profile_id': data.get('profile_id'),
            'source': 'explicit-config',
            'fallback_reason': None,
            'effective_config': data,
            'registry_error': None,
        }

    # 2. Explicit profile ID override (--profile)
    if explicit_profile_id:
        try:
            reg = load_registry(allow_missing=False)
        except ProfileError as exc:
            raise ProfileError('registry_error', f'读取注册表失败，无法加载显式指定的配置：{exc.message}') from exc
        prof = get_profile(reg, explicit_profile_id)
        if prof is None:
            raise ProfileError('explicit_profile_not_found',
                               f'显式指定的模型配置不存在：{explicit_profile_id}',
                               profile_id=explicit_profile_id)
        if not is_profile_usable(prof):
            info = describe_model_file(Path(prof.get('model_path') or ''))
            if not info.get('is_file'):
                raise ProfileError('explicit_profile_file_missing',
                                   f'显式指定的配置「{prof.get("display_name")}」模型文件不存在：{prof.get("model_path")}',
                                   profile_id=explicit_profile_id)
            raise ProfileError('explicit_profile_file_invalid',
                               f'显式指定的配置「{prof.get("display_name")}」文件不是合法的 GGUF：{prof.get("model_path")}',
                               profile_id=explicit_profile_id)
        eff = effective_deployment(explicit_profile_id, strict=True)
        return {
            'profile': prof,
            'profile_id': explicit_profile_id,
            'source': 'explicit-profile',
            'fallback_reason': None,
            'effective_config': eff,
            'registry_error': None,
        }

    # 3. Registry auto-resolution
    try:
        reg = load_registry()
    except ProfileError as exc:
        try:
            eff = read_deployment()
        except Exception:  # noqa: BLE001
            eff = {}
        return {
            'profile': None,
            'profile_id': None,
            'source': 'registry-error',
            'fallback_reason': f'模型配置注册表无法读取（{exc.message}），已回退到部署配置',
            'effective_config': eff,
            'registry_error': exc.to_dict(),
        }

    if not reg.get('profiles'):
        try:
            eff = read_deployment()
        except Exception:  # noqa: BLE001
            eff = {}
        return {
            'profile': None,
            'profile_id': None,
            'source': 'deployment-file',
            'fallback_reason': None,
            'effective_config': eff,
            'registry_error': None,
        }

    # 4. Check default_profile_id
    pid = reg.get('default_profile_id')
    default_prof = get_profile(reg, pid) if pid else None
    if is_profile_usable(default_prof):
        eff = effective_deployment(pid, strict=True)
        return {
            'profile': default_prof,
            'profile_id': pid,
            'source': 'default-profile',
            'fallback_reason': None,
            'effective_config': eff,
            'registry_error': None,
        }

    # Default profile is NOT usable; determine reason
    if default_prof is None:
        default_reason = f'默认配置 {pid} 不存在于注册表中'
    else:
        dinfo = describe_model_file(Path(default_prof.get('model_path') or ''))
        if not dinfo.get('is_file'):
            default_reason = f'默认配置「{default_prof.get("display_name")}」的模型文件不存在（{default_prof.get("model_path")}）'
        elif not dinfo.get('looks_like_gguf'):
            default_reason = f'默认配置「{default_prof.get("display_name")}」的文件不是 GGUF（{default_prof.get("model_path")}）'
        else:
            default_reason = f'默认配置「{default_prof.get("display_name")}」不可用'

    # 5. Check last_success_profile_id
    ls = reg.get('last_success_profile_id')
    last_prof = get_profile(reg, ls) if ls else None
    if is_profile_usable(last_prof):
        eff = effective_deployment(ls, strict=True)
        return {
            'profile': last_prof,
            'profile_id': ls,
            'source': 'last-success',
            'fallback_reason': f'{default_reason}，已回退到最近成功配置「{last_prof.get("display_name")}」',
            'effective_config': eff,
            'registry_error': None,
        }

    # 6. Fallback to ANY usable profile in reg['profiles']
    for p in reg['profiles']:
        if is_profile_usable(p):
            eff = effective_deployment(p['profile_id'], strict=True)
            return {
                'profile': p,
                'profile_id': p['profile_id'],
                'source': 'fallback-any-usable',
                'fallback_reason': f'{default_reason}，且最近成功配置亦不可用，已改用可用配置「{p.get("display_name")}」',
                'effective_config': eff,
                'registry_error': None,
            }

    # 7. No usable profile in registry -> fallback to deployment-file
    try:
        eff = read_deployment()
    except Exception:  # noqa: BLE001
        eff = {}
    return {
        'profile': None,
        'profile_id': None,
        'source': 'no-usable-default',
        'fallback_reason': f'{default_reason}，且注册表中无任何可用模型，已回退到部署配置',
        'effective_config': eff,
        'registry_error': None,
    }


def launch_profile() -> tuple[Optional[dict], str]:
    """Which profile a COLD START should load, and where that decision came from."""
    target = resolve_startup_target()
    return target['profile'], target['source']


def launch_fallback_reason() -> Optional[str]:
    """A human sentence when the default could NOT be used, else None."""
    target = resolve_startup_target()
    return target['fallback_reason']


# --------------------------------------------------------------------------- #
# Presentation helpers
# --------------------------------------------------------------------------- #
def profile_public_view(prof: dict, *, current_id: Optional[str] = None,
                        default_id: Optional[str] = None,
                        last_success_id: Optional[str] = None,
                        check_file: bool = False) -> dict:
    """One profile as the page sees it, with its relationships made explicit."""
    pid = prof.get('profile_id')
    view = {
        'profile_id': pid,
        'display_name': prof.get('display_name'),
        'serving_alias': prof.get('serving_alias'),
        'model_path': prof.get('model_path'),
        'model_path_declared': prof.get('model_path_declared'),
        'path_base': prof.get('path_base'),
        'file_name': Path(prof.get('model_path') or '').name,
        'target_context_window': prof.get('target_context_window'),
        'gpu_layers': prof.get('gpu_layers'),
        'default_max_output_tokens': prof.get('default_max_output_tokens'),
        'advanced': prof.get('advanced') or {},
        'origin': prof.get('origin'),
        'created_at': prof.get('created_at'),
        'updated_at': prof.get('updated_at'),
        'is_default': pid == default_id,
        'is_current': pid == current_id,
        'is_last_success': pid == last_success_id,
    }
    if check_file:
        view['file'] = describe_model_file(Path(prof.get('model_path') or ''))
    return view
