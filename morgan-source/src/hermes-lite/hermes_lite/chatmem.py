"""Round 16 & 17: user-memory extraction, correction and selection.

Three kinds:
    preference   how the user wants answers (brief / conclusion first / examples / etc.)
    address      what to call the user
    background   a durable fact the user stated about their situation

Two strengths:
    explicit     the user stated it as a lasting instruction -> injected
    tentative    inferred from one remark ("太长了") -> stored, NOT injected until confirmed

Rules first, model only when a rule cannot do the job.
Every quote must be a genuine continuous substring of the user's message.
Per-request requirements ("这次详细说") affect only the current send.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #
KIND_PREFERENCE = 'preference'
KIND_ADDRESS = 'address'
KIND_BACKGROUND = 'background'
KINDS = (KIND_PREFERENCE, KIND_ADDRESS, KIND_BACKGROUND)

STRENGTH_EXPLICIT = 'explicit'
STRENGTH_TENTATIVE = 'tentative'

# preference keys with a canonical one-line content.
PREF_LABELS = {
    'brevity': '回答尽量简短',
    'detail': '回答尽量详细、充分展开',
    'conclusion_first': '先给结论，再补充说明',
    'examples': '需要时给出具体例子',
    'no_examples': '不需要举例',
    'formal': '用正式的语气',
    'casual': '用轻松随意的语气',
    'chinese': '用中文回答',
    'markdown': '可以用 Markdown 排版',
}

# Mutual exclusions: setting one clears its opposite.
PREF_OPPOSITES = {
    'brevity': 'detail', 'detail': 'brevity',
    'examples': 'no_examples', 'no_examples': 'examples',
    'formal': 'casual', 'casual': 'formal',
}

# Cues that a remark is a LASTING instruction rather than a one-off request.
_DURABLE = (r'(以后|今后|从现在起|从今以后|之后都|每次|一直|always|平时|一般情况|'
            r'默认|长期|习惯|统一|一律)')
# Cues that bind the remark to THIS reply only.
_REQUEST_SCOPE = (r'(这次|这一条|这一回|这一句|本次|这个问题|刚才那条|this time)')

_EXAMPLE_CUES = r'(?:下面|以下|附上|这里)?(?:是)?(?:一个)?(?:示例|例子|例如|比如|比方说|假定|假设|假如|参考例子)[：:\s]+'

_CJK_NAME = r'[^\s，。、！？,.!?：:；;"“”\'（）()\[\]]{1,16}'


def _norm(text: str) -> str:
    return (text or '').replace('\u3000', ' ').strip()


def _has(pattern: str, text: str) -> bool:
    return re.search(pattern, text, re.IGNORECASE) is not None


def is_example_or_quote(text: str) -> bool:
    """Check if the text is presented as an example, quote, code, hearsay or hypothetical."""
    t = _norm(text)
    if '```' in t or re.search(r'`[^`]+`', t):
        return True
    if re.search(r'(?:^|\n)\s*>', t):
        return True
    if re.search(r'(?:示例|例子|例如|比如|比方说|假定|假设|假如|参考例子)', t):
        if re.search(r'(?:示例|例子|例如|比如|比方说|假定|假设|假如|参考例子).{0,10}[：:\s]', t):
            return True
        if re.search(r'(?:比如|例如|比方说)(?:你|我们|大家)?(?:可以)?(?:说|发|输入|问)[：:\s]', t):
            return True
        if re.search(r'^(?:示例|例如|比如|比方说|举例)', t):
            return True
    if re.search(r'引用[：:]', t):
        return True
    # Hearsay / third-party attribution like 他说“请叫我小王”、有人说请叫我小李
    if re.search(r'(?:他|她|它|他们|她们|别人|有人|网友|朋友|同事|领导|老师|对方|张三|李四)\s*(?:曾|曾经)?\s*(?:说|讲|问|称|写道|回复|发消息|留言|发来|提到)[：:\s]*[“"‘\']?', t):
        return True
    return False


# Words that are grammar, not a name. A candidate equal to one of these is
# rejected outright: trimming a question particle with the generic particle list
# would leave an empty name, and trimming nothing would store the particle.
_NOT_A_NAME = {
    '吗', '呢', '吧', '啊', '呀', '哦', '什么', '啥', '名字', '谁', '哪个',
    '怎么', '如何', '我', '你', '他', '她', '它', '我们', '你们',
}

# "你还记得怎么称呼我吗？" asks ABOUT the address; it does not state one.
_IS_A_QUESTION = (r'(?:吗|呢)\s*[？?]?\s*$'
                  r'|[？?]\s*$'
                  r'|怎么称呼|如何称呼|称呼什么|叫什么好')


def _looks_like_question(t: str) -> bool:
    return _has(_IS_A_QUESTION, t)


def _clean_name(raw: str) -> str:
    name = (raw or '').strip()
    name = name.strip('“”"\'‘’，,。.！!？? ')
    # Strip trailing modal/polite particles at the END of the name only.
    # Preserves names like "哈利" while stripping "叫我小陈哈" -> "小陈"
    name = re.sub(r'(?:就行|就好|好吗|谢谢|可以|吧|啊|呀|哦|哈|啦|咧|捏|呗|哩)+$', '', name).strip()
    name = name.strip('“”"\'‘’，,。.！!？? ')
    return name


# --------------------------------------------------------------------------- #
# Rule detection
# --------------------------------------------------------------------------- #
def detect_request_scope(text: str) -> Optional[dict]:
    """'这次详细说' / '这一条简单回答' / '这次叫我队长' / '请用Markdown回答' -> affects only the current send."""
    t = _norm(text)
    if is_example_or_quote(t):
        return None
    # Check Markdown single-turn: "请用Markdown回答", "用Markdown排版" (without durable cue)
    if _has(r'[Mm]arkdown', t) and not _has(_DURABLE, t):
        if _has(r'(?:请|就)?(?:用|使用|带|支持)?\s*[Mm]arkdown\s*(?:格式|排版)?(?:回答|回复|输出|写)?', t):
            return {'key': 'markdown', 'label': '本次回答使用 Markdown 格式'}
    if not _has(_REQUEST_SCOPE, t):
        return None
    if _has(rf'{_REQUEST_SCOPE}.{{0,8}}(详细|展开|多讲|多说)', t):
        return {'key': 'detail', 'label': '本次回答要详细展开'}
    if _has(rf'{_REQUEST_SCOPE}.{{0,8}}(简短|简洁|简单|短一?点|短一?些|一句话)', t):
        return {'key': 'brevity', 'label': '本次回答要简短'}
    m = re.search(rf'{_REQUEST_SCOPE}.{{0,8}}(?:叫我|称呼我|喊我)\s*(?P<name>{_CJK_NAME})', t)
    if m:
        name = _clean_name(m.group('name'))
        if name and name not in ('什么', '啥', '名字'):
            return {'key': 'address', 'label': f'本次称呼我为{name}', 'name': name}
    return None


def detect_address(text: str) -> Optional[dict]:
    """'请叫我小王' / '叫我阿杰就行' / '你可以称呼我王工' / '以后请叫我哈利'.

    A NEGATIVE statement about a name ("我不喜欢你叫我老王", "不希望被叫某个称呼")
    is not a request to use it. Those return None here; `analyze()` turns the ones
    that name an existing memory into a revocation instead (see
    `detect_address_revocation`).
    """
    t = _norm(text)
    if is_example_or_quote(t):
        return None
    if _has(_REQUEST_SCOPE, t):
        return None
    if _is_negative_address(t):
        return None
    # A question about the address is not a statement of one. Without this,
    # "你还记得怎么称呼我吗？" stored a new address of "吗".
    if _looks_like_question(t):
        return None
    m = re.search(rf'(?:请|以后|今后|就|可以|你)?\s*(?<!别)(?<!不要)(?<!不用)(?<!不准)(?<!别再)(?<!不要再)(?:叫我|称呼我|喊我)\s*(?P<name>{_CJK_NAME})', t)
    if not m:
        return None
    name = _clean_name(m.group('name'))
    if not name or len(name) > 16:
        return None
    if name in _NOT_A_NAME:
        return None
    quote = m.group(0).strip()
    return {'kind': KIND_ADDRESS, 'key': 'name', 'content': name,
            'quote': quote, 'strength': STRENGTH_EXPLICIT}


# "我不喜欢你叫我老王" / "不希望被叫某个称呼" / "别叫我老陈了" (the last one is a
# pure negation: no new name follows, so it is a revocation, not a new address).
# The optional middle covers the several ways Chinese puts the object between the
# verb and the name: 不喜欢(你)叫我X / 不希望(被)叫X / 不想(要)你叫我X.
_NEG_ADDRESS = (
    rf'(?:不喜欢|不想|不希望|不打算|别|不要|不用|不准|休要|不能)'
    rf'(?:再)?\s*(?:被|让|要|想)?\s*(?:你)?\s*(?:叫我|称呼我|喊我)'
)


def _is_negative_address(t: str) -> bool:
    return _has(_NEG_ADDRESS, t)


def detect_address_revocation(text: str) -> Optional[dict]:
    """The name a user explicitly says they do NOT want to be called.

    Only the name the user actually wrote is considered, and the caller checks it
    against the current memory: if it matches, the memory is revoked; if not,
    nothing is written and no opposite is invented.

    Trailing sentence particles (老陈了 -> 老陈) are stripped so the comparison is
    against the name itself, not the grammar around it.
    """
    t = _norm(text)
    if is_example_or_quote(t) or _has(_REQUEST_SCOPE, t):
        return None
    if not _is_negative_address(t):
        return None
    m = re.search(rf'{_NEG_ADDRESS}\s*(?P<name>{_CJK_NAME})', t)
    if not m:
        return None
    name = _clean_name(m.group('name'))
    # A trailing 了/过/吧 is grammar, not part of the name.
    name = re.sub(r'(?:了|过|吧|啊|呀|哦|啦)$', '', name).strip()
    if not name or name in _NOT_A_NAME:
        return None
    return {'kind': KIND_ADDRESS, 'key': 'name', 'content': name,
            'quote': m.group(0).strip(), 'strength': STRENGTH_EXPLICIT,
            'action': 'revoke_address_if_current'}


def detect_address_correction(text: str) -> Optional[dict]:
    """'别叫我小李了，叫我阿杰' -> an explicit correction of the old value."""
    t = _norm(text)
    if is_example_or_quote(t):
        return None
    if _has(_REQUEST_SCOPE, t):
        return None
    m = re.search(rf'(?:别|不要|不用)(?:再)?\s*(?:叫我|称呼我|喊我)?\s*(?P<old>{_CJK_NAME}?)[，,、\s]*(?:请|以后|今后|就)?\s*(?:叫我|称呼我|喊我)\s*(?P<name>{_CJK_NAME})', t)
    if not m:
        return None
    name = _clean_name(m.group('name'))
    if not name or len(name) > 16:
        return None
    quote = m.group(0).strip()
    return {'kind': KIND_ADDRESS, 'key': 'name', 'content': name,
            'quote': quote, 'strength': STRENGTH_EXPLICIT,
            'correction': True}


def detect_preferences(text: str) -> list[dict]:
    """Explicit, durable answer-form preferences. Clause-aware and negation-aware."""
    t = _norm(text)
    out: list[dict] = []
    if is_example_or_quote(t):
        return out
    if _has(r'(改写|改成|翻译|润色|重写|换成|这句话|这段话|这一句|下面这段|把.{0,12}(改成|改写|翻译))', t):
        return out

    def add(key: str, quote: str, strength: str = STRENGTH_EXPLICIT) -> None:
        if any(o['key'] == key for o in out):
            return
        out.append({'kind': KIND_PREFERENCE, 'key': key,
                    'content': PREF_LABELS[key], 'quote': quote,
                    'strength': strength, 'action': 'upsert'})

    def add_revoke(key: str, quote: str) -> None:
        if any(o['key'] == key for o in out):
            return
        out.append({'kind': KIND_PREFERENCE, 'key': key,
                    'content': PREF_LABELS[key], 'quote': quote,
                    'strength': STRENGTH_EXPLICIT, 'action': 'revoke'})

    # Clause segmentation
    raw_clauses = re.split(r'([，,、；;。！!\n]+|但是|但|不过)', t)
    clauses = []
    curr = ''
    for part in raw_clauses:
        curr += part
        if re.search(r'[，,、；;。！!\n]|但是|但|不过', part):
            clauses.append(curr.strip())
            curr = ''
    if curr.strip():
        clauses.append(curr.strip())

    # A clause that NEGATES a keyword is never a request for that keyword. The
    # adverb between the negation and the keyword ("别回答那么详细", "不要说得
    # 太正式") used to defeat the old pattern, so the negation match allows an
    # optional degree adverb there. Without it the positive branch then matched
    # the tail and stored the very thing the user rejected.
    _NEG = r'(?:不要|别|不用|无需|不必|禁止|不能|不喜欢|不想|不打算)'
    _DEG = r'(?:那么|太|这么|得|这么的)?'

    has_global_durable = _has(_DURABLE, t)

    for c in clauses:
        # If this clause has request scope, skip it for durable preferences
        if _has(_REQUEST_SCOPE, c):
            continue
        c_has_durable = _has(_DURABLE, c) or has_global_durable

        # 1. Brevity
        is_negated_brevity = _has(rf'{_NEG}(?:再)?\s*(?:回答|回复|说|答)?\s*{_DEG}\s*(?:简短|简洁|短)', c)
        if is_negated_brevity:
            if c_has_durable:
                m_rev = re.search(rf'{_NEG}(?:再)?\s*(?:回答|回复|说|答)?\s*{_DEG}\s*(?:简短|简洁|短一?点|短一?些)?', c)
                add_revoke('brevity', m_rev.group(0).strip() if m_rev else c)
        else:
            m = re.search(rf'({_DURABLE}.{{0,10}})?(回答|回复|说|答).{{0,6}}(简短|简洁|精简|言简意赅|短一?点|短一?些|简单点|简单一点)', c)
            if not m:
                m = re.search(rf'({_DURABLE}.{{0,10}})?(短一?点|短一?些|简洁些|精简些|少说废话|别啰嗦|不要啰嗦|直奔主题)', c)
            if m and c_has_durable:
                add('brevity', m.group(0).strip())

        # 2. Detail
        is_negated_detail = _has(rf'{_NEG}(?:再)?\s*(?:回答|回复|说|答)?\s*{_DEG}\s*(?:详细|展开|说长|多讲)', c)
        if is_negated_detail:
            if c_has_durable:
                m_rev = re.search(rf'{_NEG}(?:再)?\s*(?:回答|回复|说|答)?\s*{_DEG}\s*(?:详细|展开|说长|多讲)', c)
                add_revoke('detail', m_rev.group(0).strip() if m_rev else c)
        else:
            m = re.search(rf'({_DURABLE}.{{0,10}})?(回答|回复|说|答).{{0,6}}(详细|展开|多讲|多说|写长)', c)
            if not m:
                m = re.search(rf'({_DURABLE}.{{0,10}})?(详细一?点|详细一?些|多点细节|展开(说|讲)|说得?详细|写得?详细|多给点信息)', c)
            if m and c_has_durable:
                add('detail', m.group(0).strip())

        # 3. Conclusion first
        is_negated_conclusion = _has(rf'{_NEG}(?:再)?\s*{_DEG}\s*(?:先给?结论|先说结论|结论先行|结论放(?:在)?前面|先说结果|先说答案|直接给结论|直接说结论|先讲结论)', c)
        if is_negated_conclusion:
            if c_has_durable or _has(r'结论', c):
                m_rev = re.search(rf'{_NEG}(?:再)?\s*{_DEG}\s*(?:先给?结论|先说结论|结论先行|结论放(?:在)?前面|先说结果|先说答案|直接给结论|直接说结论|先讲结论)', c)
                add_revoke('conclusion_first', m_rev.group(0).strip() if m_rev else c)
        else:
            m = re.search(r'(先给?结论|先说结论|结论先行|结论放(在)?前面|先说结果|先说答案|直接给结论|直接说结论|先讲结论)', c)
            if m and (c_has_durable or _has(r'先说结论|先给结论|结论先行', c)):
                add('conclusion_first', m.group(0).strip())

        # 4. Examples (MUST have durable cue; single "请举个例子" is single-turn)
        is_negated_examples = _has(rf'{_NEG}(?:再)?\s*(?:举)?(?:例子|举例|示例)', c)
        if is_negated_examples:
            if c_has_durable:
                m = re.search(rf'({_DURABLE}.{{0,10}})?{_NEG}(?:再)?\s*(?:举)?(?:例子|举例|示例)', c)
                add('no_examples', m.group(0).strip() if m else c)
        else:
            m = re.search(rf'({_DURABLE}.{{0,10}})(?:需要|要|给我|多)?(?:举)?(?:例子|举例|示例)', c)
            if not m:
                m = re.search(rf'({_DURABLE}.{{0,10}})(?:举个例子|举例说明)', c)
            if m and c_has_durable:
                add('examples', m.group(0).strip())

        # 5. Formal / Casual
        is_negated_formal = _has(rf'{_NEG}(?:再)?\s*(?:用|使用|保持)?\s*{_DEG}\s*(?:正式|严肃|专业)', c)
        if is_negated_formal:
            if c_has_durable:
                m_rev = re.search(rf'{_NEG}(?:再)?\s*(?:用|使用|保持)?\s*{_DEG}\s*(?:正式|严肃|专业)(?:语气|口吻|风格)?', c)
                add_revoke('formal', m_rev.group(0).strip() if m_rev else c)
        else:
            m = re.search(rf'({_DURABLE}.{{0,12}})?(请|都|一律|统一)?\s*(用|使用|保持)?\s*(正式|严肃|专业)(一点|一些|点|些)?的?(语气|口吻|风格)', c)
            if not m:
                m = re.search(rf'({_DURABLE}.{{0,12}})?.{{0,4}}(正式|严肃|专业)(一点|一些)?(的)?(语气|口吻|风格)?(来)?(回答|回复|说)', c)
            if m and c_has_durable:
                add('formal', m.group(0).strip())

        is_negated_casual = _has(rf'{_NEG}(?:再)?\s*(?:用|使用|保持)?\s*{_DEG}\s*(?:轻松|随意|活泼|口语化)', c)
        if is_negated_casual:
            if c_has_durable:
                m_rev = re.search(rf'{_NEG}(?:再)?\s*(?:用|使用|保持)?\s*{_DEG}\s*(?:轻松|随意|活泼|口语化)(?:语气|口吻|风格)?', c)
                add_revoke('casual', m_rev.group(0).strip() if m_rev else c)
        else:
            m = re.search(rf'({_DURABLE}.{{0,12}})?.{{0,4}}(轻松|随意|活泼|口语化)(一点|一些|点|些)?(的)?(语气|口吻|风格)?(来)?(回答|回复|说)?', c)
            if not m:
                m = re.search(rf'({_DURABLE}.{{0,10}})?(别那么正式|不要太正式)', c)
            if m and c_has_durable and not is_negated_formal:
                add('casual', m.group(0).strip())

        # 6. Chinese / Markdown
        is_negated_chinese = _has(rf'{_NEG}(?:再)?\s*(?:用|说|默认用)?\s*{_DEG}\s*中文', c)
        if is_negated_chinese:
            if c_has_durable:
                m_rev = re.search(rf'{_NEG}(?:再)?\s*(?:用|说|默认用)?\s*{_DEG}\s*中文', c)
                add_revoke('chinese', m_rev.group(0).strip() if m_rev else c)
        else:
            if c_has_durable and _has(r'(用|说)中文', c):
                m = re.search(rf'({_DURABLE}.{{0,10}})?(?:用|说)中文', c)
                if m:
                    add('chinese', m.group(0).strip())

        is_negated_md = _has(rf'{_NEG}(?:再)?\s*(?:用|使用)?\s*{_DEG}\s*[Mm]arkdown', c)
        if is_negated_md:
            if c_has_durable:
                m_rev = re.search(rf'{_NEG}(?:再)?\s*(?:用|使用)?\s*{_DEG}\s*[Mm]arkdown', c)
                add_revoke('markdown', m_rev.group(0).strip() if m_rev else c)
        else:
            if c_has_durable and _has(r'[Mm]arkdown', c):
                m = re.search(rf'({_DURABLE}.{{0,10}})?.*?[Mm]arkdown.*', c)
                if m:
                    add('markdown', m.group(0).strip())

    return out


def detect_soft_feedback(text: str) -> Optional[dict]:
    """'太长了' after one answer is feedback about that answer, not a new rule.

    Stored as a TENTATIVE preference: visible in the panel, not injected until
    the user explicitly confirms it.
    """
    t = _norm(text)
    if len(t) > 40:
        return None
    if is_example_or_quote(t):
        return None
    if detect_request_scope(t):
        return None
    m = re.search(r'(太长了|太长|有点长|啰嗦|太长啦|说太多了|简短点|短一点|'
                  r'太啰嗦|内容太多)', t)
    if not m:
        return None
    return {'kind': KIND_PREFERENCE, 'key': 'brevity',
            'content': PREF_LABELS['brevity'], 'quote': m.group(0).strip(),
            'strength': STRENGTH_TENTATIVE}


# --------------------------------------------------------------------------- #
# What deserves a model call
# --------------------------------------------------------------------------- #
_BACKGROUND_CUES = (
    r'(我在|我正在|我目前|我最近|我们(在|正在)|我们最近|我负责|我做的|我参与)',
    r'(项目|工程|产品|系统|工作|公司|团队|学业|课程|论文|比赛|兼职|实习)',
    r'(我是|我从事|我学|我住|我在.{0,6}(城市|工作|上学))',
)


def looks_like_background(text: str) -> bool:
    """A personal, durable statement that the fixed rules cannot structure."""
    t = _norm(text)
    if len(t) < 6 or len(t) > 400:
        return False
    if is_example_or_quote(t):
        return False
    if not _has(_BACKGROUND_CUES[0], t):
        return False
    if not _has(_BACKGROUND_CUES[1], t) and not _has(_BACKGROUND_CUES[2], t):
        return False
    # A question is asking something, not stating a lasting fact.
    if t.endswith('？') or t.endswith('?') or _has(r'(吗|呢)[。！!]?$', t):
        return False
    return True


def analyze(text: str, *, rules_only: bool = False) -> dict:
    """Deterministic layer analysis for one user message."""
    t = _norm(text)
    items: list[dict] = []
    scope = detect_request_scope(t)

    corr = detect_address_correction(t)
    if corr:
        items.append(corr)
    else:
        rev = detect_address_revocation(t)
        if rev is not None:
            # A negative statement about a name never creates one. `apply_analysis`
            # revokes it only if it is the CURRENT memory; otherwise it is dropped,
            # because "I don't like X" does not mean "call me Y".
            items.append(rev)
        else:
            addr = detect_address(t)
            if addr:
                items.append(addr)

    for p in detect_preferences(t):
        items.append(p)

    soft = detect_soft_feedback(t)
    if soft and not any(i['kind'] == KIND_PREFERENCE and i['key'] == 'brevity'
                        for i in items):
        items.append(soft)

    wants_model = (not rules_only) and (not items) and looks_like_background(t)
    return {'request_scope': scope, 'items': items, 'wants_model': wants_model,
            'text': t}


# --------------------------------------------------------------------------- #
# Model extraction
# --------------------------------------------------------------------------- #
EXTRACT_SYSTEM = (
    '你是一个信息抽取器。只从「用户消息」里抽取用户本人明确说出的、值得长期记住的信息。\n'
    '允许的类型：\n'
    '- background：用户正在做的项目、工作、学习等持续背景\n'
    '- preference：用户对回答形式的长期要求\n'
    '规则：\n'
    '1. 只依据用户消息原文，不要推理、不要补充、不要编造。\n'
    '2. quote 必须是用户消息里原样出现的一段文字（连续子串）。\n'
    '3. 没有值得长期记住的信息，就输出 {"memories": []}。\n'
    '4. 用户只是提问或闲聊，输出 {"memories": []}。\n'
    '5. 最多 2 条。只输出 JSON，不要任何解释。\n'
    '输出格式：{"memories":[{"kind":"background","key":"项目名","'
    '"content":"一句话","quote":"原文片段"}]}'
)


def extract_user_prompt(text: str) -> str:
    return '用户消息：\n<<<\n' + text + '\n>>>\n\n只输出 JSON。'


def parse_extraction(raw: str, user_text: str,
                     max_items: int = 2) -> dict:
    out = {'items': [], 'rejected': [], 'ok': False, 'reason': ''}
    raw = (raw or '').strip()
    if not raw:
        out['reason'] = 'empty'
        return out
    m = re.search(r'\{.*\}', raw, re.S)
    if not m:
        out['reason'] = 'no_json'
        return out
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        out['reason'] = 'unparsable_json'
        return out
    items = obj.get('memories')
    if not isinstance(items, list):
        out['reason'] = 'not_a_list'
        return out
    haystack = _norm(user_text)
    for it in items[:max_items]:
        if not isinstance(it, dict):
            out['rejected'].append({'why': 'not_object', 'item': it})
            continue
        kind = str(it.get('kind') or '').strip()
        content = str(it.get('content') or '').strip()
        quote = str(it.get('quote') or '').strip()
        key = str(it.get('key') or '').strip()
        if kind not in (KIND_BACKGROUND, KIND_PREFERENCE):
            out['rejected'].append({'why': 'bad_kind', 'item': it})
            continue
        if not content or not quote:
            out['rejected'].append({'why': 'missing_field', 'item': it})
            continue
        if _norm(quote) not in haystack:
            out['rejected'].append({'why': 'quote_not_in_user_text', 'item': it})
            continue
        if len(content) > 200:
            out['rejected'].append({'why': 'content_too_long', 'item': it})
            continue
        if not key:
            key = (kind + '-' + re.sub(r'\W+', '-', content)[:24]).strip('-')
        out['items'].append({
            'kind': kind, 'key': key[:48], 'content': content,
            'quote': quote, 'strength': STRENGTH_EXPLICIT,
            'origin': 'model',
        })
    out['ok'] = True
    out['reason'] = 'parsed'
    return out


# --------------------------------------------------------------------------- #
# Applying a message's analysis to the store
# --------------------------------------------------------------------------- #
def apply_analysis(store, *, session_id: str, message_id: str,
                   analysis: dict, model_items: Optional[list[dict]] = None
                   ) -> dict:
    """Write the memories an analysis produced, with atomic version validation in store."""
    written: list[dict] = []
    actions: list[dict] = []
    items = list(analysis.get('items') or []) + list(model_items or [])

    for it in items:
        action_type = it.get('action', 'upsert')
        if action_type == 'revoke':
            res = store.revoke_memory(
                kind=it['kind'], key=it['key'],
                session_id=session_id, message_id=message_id,
                quote=it['quote'])
            actions.append({'action': res['action'], 'key': it['key'],
                            'kind': it['kind'], 'detail': res.get('detail', '')})
            continue

        if action_type == 'revoke_address_if_current':
            # Only revoke when the name the user rejected IS the current one.
            # Otherwise drop it silently: there is nothing to undo and inventing
            # a replacement would be guessing.
            cur = store.find_memory(KIND_ADDRESS, 'name')
            if cur is not None and cur['content'] == it['content']:
                res = store.revoke_memory(
                    kind=KIND_ADDRESS, key='name', session_id=session_id,
                    message_id=message_id, quote=it['quote'])
                actions.append({'action': res['action'], 'key': it['key'],
                                'kind': it['kind'],
                                'detail': res.get('detail', '')})
            else:
                actions.append({'action': 'skipped_not_current_address',
                                'key': it['key'], 'kind': it['kind'],
                                'content': it['content'],
                                'current': (cur or {}).get('content')})
            continue

        if it['kind'] == KIND_PREFERENCE and it.get('strength') == STRENGTH_TENTATIVE:
            opposite = PREF_OPPOSITES.get(it['key'])
            if opposite:
                old_opp = store.find_memory(KIND_PREFERENCE, opposite)
                if old_opp and old_opp.get('strength') == STRENGTH_EXPLICIT and old_opp.get('enabled'):
                    actions.append({'action': 'skipped_tentative_opposite_exists', 'key': it['key'],
                                    'kind': it['kind']})
                    continue

        enabled = (it.get('strength') == STRENGTH_EXPLICIT)
        res = store.upsert_memory(
            kind=it['kind'], key=it['key'], content=it['content'],
            strength=it.get('strength', STRENGTH_EXPLICIT),
            origin=it.get('origin', 'rule'),
            session_id=session_id, message_id=message_id,
            quote=it['quote'], enabled=enabled)
        actions.append({'action': res['action'], 'key': it['key'],
                        'kind': it['kind'], 'content': it['content'],
                        'strength': it.get('strength', STRENGTH_EXPLICIT),
                        'origin': it.get('origin', 'rule'),
                        'enabled': enabled,
                        'memory': res.get('memory')})
        if res['action'] in ('created', 'updated'):
            written.append(it)
    return {'written': written, 'actions': actions}


# --------------------------------------------------------------------------- #
# Selection + rendering
# --------------------------------------------------------------------------- #
MAX_MEMORY_CHARS = 600
MAX_MEMORIES_PER_TURN = 5

_STOP = set('的了是我你他她它在有和就不都一个上也很到说要去会着没有看好自己这'
            '什么怎么可以我们你们他们')


def _keywords(text: str) -> set[str]:
    out: set[str] = set()
    for tok in re.findall(r'[A-Za-z0-9_\-\u4e00-\u9fa5]+', text or ''):
        if len(tok) >= 2 and tok not in _STOP:
            out.add(tok.lower())
    cjk = re.findall(r'[\u4e00-\u9fa5]+', text or '')
    for run in cjk:
        for i in range(len(run) - 1):
            bg = run[i:i + 2]
            if bg not in _STOP:
                out.add(bg)
    return out


def select(store, user_text: str,
           request_scope: Optional[dict] = None,
           style: Optional[str] = None,
           include_durable: bool = True) -> list[dict]:
    """Pick enabled explicit memories for THIS send, with request_scope priority."""
    chosen: list[dict] = []
    live = ([m for m in store.live_memories() if m['enabled'] and
             m.get('strength') == STRENGTH_EXPLICIT]
            if include_durable else [])

    req_key = (request_scope or {}).get('key')
    conflicting_key = PREF_OPPOSITES.get(req_key) if req_key else None

    # Style conflict suppression: explicit turn style overrides conflicting durable memory
    style_conflict = None
    if style == 'concise':
        style_conflict = 'detail'
    elif style == 'detailed':
        style_conflict = 'brevity'

    for m in live:
        if m['kind'] == KIND_ADDRESS:
            if req_key == 'address':
                continue
            chosen.append(m)
        elif m['kind'] == KIND_PREFERENCE:
            # If current request explicitly contradicts durable preference, suppress for this turn
            if conflicting_key and m['key'] == conflicting_key:
                continue
            if style_conflict and m['key'] == style_conflict:
                continue
            chosen.append(m)

    kw = _keywords(user_text)
    for m in live:
        if m['kind'] != KIND_BACKGROUND:
            continue
        if kw & _keywords(m['content'] + ' ' + m['key']):
            chosen.append(m)

    # Durable memories capped at MAX_MEMORIES_PER_TURN (5)
    chosen = chosen[:MAX_MEMORIES_PER_TURN]

    # Per-request hint is appended AFTER capping so it is NEVER dropped
    if request_scope:
        chosen.append({'kind': 'request_scope', 'key': request_scope['key'],
                       'content': request_scope['label'], 'id': None,
                       'source_session_id': None, 'strength': 'request',
                       'enabled': 1})
    return chosen


def render(memories: list[dict]) -> str:
    if not memories:
        return ''
    lines: list[str] = []
    prefs: list[str] = []
    background: list[str] = []
    request_lines: list[str] = []
    for m in memories:
        if m['kind'] == KIND_ADDRESS:
            lines.append(f'称呼：{m["content"]}')
        elif m['kind'] == KIND_PREFERENCE:
            prefs.append(m['content'])
        elif m['kind'] == KIND_BACKGROUND:
            background.append(m['content'])
        elif m['kind'] == 'request_scope':
            request_lines.append(m['content'])
    if prefs:
        lines.append('偏好：' + '；'.join(dict.fromkeys(prefs)))
    if background:
        lines.append('背景：' + '；'.join(dict.fromkeys(background)))
    if request_lines:
        lines.append('本次要求：' + '；'.join(dict.fromkeys(request_lines)))
    if not lines:
        return ''
    body = '\n'.join('- ' + ln for ln in lines)
    text = ('关于用户（来自之前的交流。自然运用即可，不要逐条复述，'
            '也不要每次都声明"根据记忆"）：\n' + body)
    if len(text) > MAX_MEMORY_CHARS:
        text = text[:MAX_MEMORY_CHARS]
    return text
