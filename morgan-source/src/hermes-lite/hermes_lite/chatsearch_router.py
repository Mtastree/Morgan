# -*- coding: utf-8 -*-
"""Round 54: decide WHETHER to search, WHAT to search, and WHAT to keep.

## The bug this module fixes

The old code was one line:

    is_web_search = bool(cfg.get('web_search'))

The user's toggle means *"you may use the internet"*, but that line read it as
*"search every message"*. Then the current message text was handed straight to the
search backend. So "介绍一下你自己" / "介绍的是你，不是我" were sent to
DuckDuckGo, and self-introduction templates came back and were injected into the
prompt -- the model then answered from a stranger's blurb instead of as itself.

## The five states this module keeps apart

    allow_web        the user's switch: is the network ALLOWED at all
    needs_search     did THIS request decide to search (and why)
    search_executed  did a search call actually happen
    results_found    did the backend return anything
    injected         were results actually put into the prompt

Only `injected` may make the page say "联网回答". `allow_web` alone never does.

## How the decision is made

A deterministic rule layer, no model call. Reasons:

* the router must never send the conversation anywhere to decide whether to
  search -- a model router would leak the message to the local model at minimum,
  and the whole point is that ordinary chat must not leave the machine;
* a 1.5B model cannot be trusted to classify reliably, and its cost would land on
  every single message;
* rules are auditable: each decision records the matched reason, so a wrong call
  can be read off the log instead of guessed.

Two rule families, in this order:

1. **Direct-answer signals** (negation of search). Explicit search requests win
   over these, so "不用搜了，查一下明天的天气" still searches.
2. **Search-intent signals**: explicit ask ("搜一下/联网查/找资料"), fresh-info
   topics (weather, price, news, schedules...), external-verification asks.

Single words like 「今天」「介绍」「最新」 are deliberately NOT triggers on their own;
each search topic requires a topical anchor (weather/price/news/...) so "今天有点累"
stays a chat. When nothing matches, the answer is "do not search" -- the safe side,
because an unneeded search hurts far more than a missing one.

## Query construction

Decided separately from the decision, because "should I search" and "what do I
search" are different questions:

* an explicit, self-contained question is used as-is (trimmed of the ask-phrase);
* a follow-up ("那明天呢") is completed from the nearest recent user turn;
* a correction ("不是我，是你" / "再详细一点") is NOT submitted -- it has no external
  subject, so the router either reuses the earlier subject or asks the user;
* the persona, memories and the wider conversation are never sent as the query.

## Result filtering

`filter_results` drops results that cannot support the question: empty
title+url+snippet, and ones with no lexical overlap with the query's content
characters. "Got 5 results" never becomes "used 5 results"; it is acceptable --
expected, even -- to inject nothing.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional, Sequence

# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #
#: Explicit ask to use the network. These WIN over every direct-answer signal.
_EXPLICIT_SEARCH = re.compile(
    r'(?:'
    r'(?:联网|上网|网上|在线)\s*(?:搜|查|找|检索)'
    r'|(?:搜|查|找)\s*(?:一下|一搜|一查|一找|找|搜|查)'
    r'|(?:帮我|帮忙|麻烦|给我)\s*(?:搜|查|找)'
    r'|搜索(?:一下|一下儿)?'
    r'|检索'
    r'|(?:查|搜|找)(?:一?下)?(?:资料|信息|新闻|链接|来源|出处|证据)'
    r'|(?:有没有|有什么|有哪些)\s*(?:新闻|资料|来源|链接)'
    r'|google|百度|bing|必应'
    r')'
    # A message that OPENS with 搜/查/找 is asking for a search, even without the
    # "一下" particle: "搜一个不存在的词". Anchored on purpose -- 查 inside a
    # sentence ("我查过了") is not an instruction.
    r'|^\s*(?:搜|查|找)\s*(?:一下|一个|一搜|一查|一找|个|找|搜|查)?',
    re.IGNORECASE)

#: Asking to verify an external fact against sources.
_VERIFY = re.compile(
    r'(?:'
    r'(?:核实|验证|证实|确认)(?:一下)?(?:这个|这件事|这条|上面|说法|消息|新闻)'
    r'|(?:是不是|是否)\s*(?:真的|属实|有这件事)'
    r'|(?:有|有没有)\s*(?:来源|出处|依据|证据)'
    r'|(?:最新|最近|近期)\s*(?:的)?\s*(?:新闻|消息|进展|报道|动态)'
    r')',
    re.IGNORECASE)

#: Topics that inherently need data from outside the conversation.
_TOPIC_WEATHER = re.compile(r'(?:天气|气温|下雨|降雨|降雪|台风|雾霾|空气质量|风速|湿度|冷不冷|热不热|要不要带伞)')
_TOPIC_PRICE = re.compile(r'(?:股价|汇率|油价|金价|房价|币价|行情|多少钱|价格|报价|市值|涨停|跌停)')
_TOPIC_NEWS = re.compile(r'(?:新闻|时事|头条|发布会|公告|通报|声明|最新消息)')
_TOPIC_SCHEDULE = re.compile(r'(?:赛程|比赛结果|比分|开赛|上映|开售|发售|发布(?:时间|日期)|什么时候(?:开始|结束|上线))')
_TOPIC_FACT = re.compile(r'(?:谁(?:是|获得了)|哪一年|成立于|创始人|总部(?:在|位于)|人口|面积|海拔|距离)')
_TOPIC_TECH = re.compile(r'(?:最新版本|发布了?什么|更新了?什么|新版本|breaking\s+change)', re.IGNORECASE)
#: Research/experiment status: "最新实验结果" is a fact about the world that
#: changes, and a small local model's knowledge of it is stale by construction.
#: Requires a freshness word, so "实验结果怎么写报告" stays a chat.
_TOPIC_RESEARCH = re.compile(
    r'(?:实验(?:结果|进展|数据)|研究(?:结果|进展|成果)|论文|预印本|临床试验|'
    r'排行|榜单|市场份额|出货量|装机量)')

#: "Freshness" words. Only meaningful TOGETHER with a topic above.
_FRESH = re.compile(r'(?:今天|今日|现在|目前|当前|最近|近期|这两天|这几天|本周|这周|今年|本月|实时|最新)')

#: Signals that the user wants a rewrite/transformation of text they provided.
_TRANSFORM = re.compile(
    r'(?:'
    r'翻译|译成|译为|translat'
    r'|改写|润色|重写|改一下|换个说法|换种说法|换一个说法|改得更'
    r'|总结(?:一下)?(?:这段|下面|以上|这篇|上面的)'
    r'|概括(?:一下)?(?:这段|下面|以上|这篇)'
    r'|精简|缩写|压缩成'
    r'|纠错|改错|校对'
    # Round 55: "explain this passage" is also working ON the user's text, and
    # must not let a quoted "我不知道" look like the model admitting ignorance.
    r'|解释(?:一下)?(?:这段|下面|这句|这个|以上|这篇|上面的)'
    r'|(?:这段|这句|上面这?句)(?:话)?(?:是)?什么?意思'
    r')',
    re.IGNORECASE)

#: Style-only tinkering with the answer that was just given.
_STYLE_TWEAK = re.compile(
    r'(?:'
    r'(?:再|更)?(?:简短|简洁|简单|详细|具体|通俗|正式|口语)(?:一?点|一些|些)?'
    r'|(?:不要|别用|不用|去掉)(?:列表|分点|编号|emoji|表情|标题|加粗|markdown)'
    r'|用(?:列表|分点|表格|一段话)'
    r'|(?:再|重新)(?:说|讲|解释)(?:一?遍|一?次)'
    r'|(?:换个|换种)(?:说法|方式)'
    r'|太长了|太短了|说人话'
    r')',
    re.IGNORECASE)

#: Pure arithmetic the model can do itself.
_ARITHMETIC = re.compile(r'^[\s\d+\-*/×÷().,%％^=？?]*(?:计算|算一下|等于多少)?[\s\d+\-*/×÷().,%％^=？?]*$')

#: Meta questions about the app/assistant itself.
_SELF_REFERENTIAL = re.compile(
    r'(?:'
    r'你(?:是|叫|能|会|可以|有|支持|怎么|如何|为什么|平时|现在|刚才)'
    r'|介绍(?:一下)?(?:你|你自己|自己)'
    r'|你(?:的)?(?:名字|身份|功能|能力|设定|人格|模型|版本)'
    r'|自我(?:介绍|认知)'
    r'|你好|您好|嗨|哈喽|再见|谢谢|不客气'
    r'|(?:这个|本)(?:应用|软件|程序|工具|产品|聊天)'
    r')',
    re.IGNORECASE)

#: A message that mostly points at a previous turn instead of stating a subject.
_CORRECTION = re.compile(
    r'(?:'
    r'不是(?:我|这个|那样|这么)|我说的是|我指的是|不是这个意思'
    r'|(?:是指|指的是)(?:你|你自己)'
    r'|你(?:理解|误会|搞错)了'
    r'|不对|错了|重新来'
    r')',
    re.IGNORECASE)

# --------------------------------------------------------------------------- #
# Round 55: "tell me about <named external thing>"
#
# The Round-54 rules only searched on an explicit ask or on a closed list of
# topics (weather/price/news/...). So "给我讲一讲影之刃零的信息吧" fell through to
# `no_signal` and was answered from the model's own memory -- which for a 1.5B
# model means "I don't know", with no follow-up.
#
# The fix must NOT be a list of product names: that is unbounded and would be a
# whitelist in disguise. What generalises is the SHAPE of the request:
#
#     an information-seeking verb  +  a specific named object
#
# So we detect an INFO_REQUEST frame, and separately decide whether the message
# actually names something. "讲一讲" alone ("给我讲讲该怎么办") has no object;
# "影之刃零" is an object. A bare pronoun ("它") borrows the object from the
# previous turn, which is what makes "它什么时候发售" work as a follow-up.
# --------------------------------------------------------------------------- #
#: Asking for information/background about something.
_INFO_REQUEST = re.compile(
    r'(?:'
    r'(?:讲|说|聊|介绍|科普|谈谈|说说|聊聊|了解一下|了解|知道)(?:一讲|一说|一聊|一下|讲|说)?'
    r'|(?:什么|啥)(?:是|叫)(?:这个|那个)?'
    r'|(?:资料|信息|详情|背景|介绍|简介|情况|评测|评价|口碑|怎么样|如何|好玩|好用|值得)'
    r'|(?:是|属于)(?:什么|哪个)'
    r')',
    re.IGNORECASE)

#: Freshness/fact qualifiers for an object ("什么时候发售", "开发商是谁").
_OBJECT_FACT = re.compile(
    r'(?:'
    r'(?:什么|哪)(?:时候|年|年出|时|天)(?:发售|上市|发布|开服|上线|出|推出|开播|上映)'
    r'|(?:发售|上市|发布|开服|上线|开播|上映)(?:了|日期|时间)'
    r'|(?:开发商|发行商|出品方|制作方|厂商|作者|导演|主演|配音|平台|售价|价格|多少钱)'
    r'|(?:是|由)(?:谁|哪家|哪个)(?:开发|制作|发行|出品|写的|做的|导演的)'
    r'|(?:配置|要求|参数|规格|版本|更新内容|补丁|攻略|怎么玩|玩法)'
    r')',
    re.IGNORECASE)

#: Creative-writing intent. These name an object but ask us to INVENT, not look up.
_CREATIVE = re.compile(
    r'(?:'
    r'(?:帮我|给我|替我|请)?(?:编|写|创作|构思|设计|虚构|编造|杜撰|拟)(?:一|个|一个|一段|篇|首|下)?'
    r'|假如|假设|如果(?:你|我们)|设想|想象|脑洞'
    r'|(?:写|作)(?:一首|一篇|一段|个)(?:诗|故事|小说|剧本|歌|代码|文案)'
    r'|角色扮演|扮演|续写|接着写|帮我起(?:个|一个)?名'
    r')',
    re.IGNORECASE)

#: A named object: a CJK run of 2+ chars that is NOT itself a function word /
#: instruction word. This deliberately does not try to recognise "a product name";
#: it only asks "is there a concrete noun phrase here that isn't just grammar?".
_OBJECT_TOKEN = re.compile(r'[\u4e00-\u9fffA-Za-z0-9][\u4e00-\u9fffA-Za-z0-9·\-—_]{1,}')

#: Words that carry no object by themselves. If the only candidate tokens are in
#: this set, the message names nothing external.
_STOP_TOKENS = frozenset("""
我 你 您 他 她 它 我们 你们 他们 自己 这个 那个 这些 那些 什么 怎么 为什么 如何 哪个 哪些
一下 一点 一些 一个 一件 一段 一篇 一首 一句 有些 有点 很多 非常 特别 真的 确实
可以 能不能 能否 是否 应该 需要 想要 希望 觉得 认为 知道 了解 告诉 讲讲 说说 聊聊 介绍
信息 资料 详情 背景 简介 情况 内容 东西 事情 问题 方面 有关 关于 相关 一下儿
吧 呢 吗 啊 呀 哦 嗯 了 的 地 得 和 与 及 或 是 有 在 就 都 也 还 又 再 给 帮 请 麻烦
今天 明天 昨天 现在 目前 当前 最近 近期 以后 之前 时候 时间 哪里 哪儿 这儿 那儿
讲 说 聊 科普 谈谈 了解 咋 办 怎么办 怎样 咋样 玩儿 玩 用 做 干 弄
谁 哪家 开发商 发行商 出品方 制作方 厂商 作者 导演 主演 配音 平台 售价 价格 多少钱
发售 上市 发布 开服 上线 开播 上映 日期 时间 配置 要求 参数 规格 版本 更新内容 补丁 攻略 玩法
""".split())

#: Multi-character stop phrases to strip before tokenising (longest first).
_STOP_PHRASES = (
    '给我', '帮我', '麻烦', '请问', '介绍', '讲一讲', '说一说', '聊一聊', '讲一下',
    '说一下', '聊一下', '科普', '谈谈', '说说', '聊聊', '讲', '说', '聊',
    '了解一下', '了解', '信息', '资料', '详情', '背景', '简介', '情况', '内容',
    '什么时候', '什么', '怎么样', '怎么', '如何', '为什么', '哪个', '哪些', '哪里',
    '是不是', '有没有', '能不能', '可以', '一个', '这个', '那个', '一下', '一些',
    '它', '他', '她', '我', '你', '您', '吧', '呢', '吗', '啊', '呀', '哦', '嗯',
    '的', '了', '是', '有', '在', '和', '与', '就', '也', '还', '又', '再', '都',
    '开发商', '发行商', '出品方', '制作方', '厂商', '作者', '导演', '主演', '配音',
    '发售', '上市', '发布', '开服', '上线', '开播', '上映', '多少钱', '售价', '价格',
    '配置', '要求', '参数', '规格', '版本', '更新', '补丁', '攻略', '玩法', '谁', '哪家',
    '今天', '明天', '昨天', '现在', '目前', '当前', '最近', '近期', '时候', '时间',
    '关于', '有关', '相关', '东西', '事情', '问题', '方面',
)

#: Pronouns that stand in for an object mentioned earlier. Alone they do not
#: name anything; with context they inherit the previous turn's object.
_OBJECT_PRONOUN = re.compile(r'^\s*(?:它|他|她|这个|那个|这东西|那东西|这款|那款|这游戏|那游戏)')

#: Demonstrative + generic noun: refers back, does not name.
_GENERIC_DEICTIC = re.compile(
    r'(?:这|那|该|此)(?:个|款|部|本|位)?(?:游戏|作品|产品|软件|应用|东西|电影|剧|书|项目|工具|版本|公司|品牌)')

#: Judgement words: what is ASKED about an object, never the object itself.
#: Without this, "这游戏好玩吗" would search for "好玩".
_QUALITY_TOKENS = frozenset("""
好玩 好用 好看 好听 好吃 值得 推荐 厉害 优秀 不错 一般 差劲 垃圾 可以
怎么样 如何 好玩吗 值得买 值得玩 值不值 划得来 好使 靠谱 稳定 流畅 卡顿
""".split())

#: Round 55: the user forbids the network FOR THIS MESSAGE. Must beat every
#: search rule, including an explicit "搜一下" elsewhere in the sentence.
_NO_WEB = re.compile(
    r'(?:'
    r'不(?:要|用|需|必)(?:联网|上网|搜索|搜|查|上网查)'
    r'|别(?:联网|上网|搜索|搜|去搜|上网查)'
    r'|(?:不用|无需)(?:联网|上网|搜索|搜|查)'
    r'|离线(?:回答|答|说)'
    r'|不要(?:去)?(?:网上|网上找|查资料)'
    r')',
    re.IGNORECASE)

#: The model saying, in its own voice, that it lacks the material. Used by the
#: gap-fill check -- deliberately narrow, and never applied to quoted text.
_MODEL_INSUFFICIENT = re.compile(
    r'(?:'
    r'我(?:并)?(?:不|没有)(?:了解|知道|掌握|清楚)(?:这个|该|这款|相关|关于|这些|这一)?'
    r'|我(?:的)?(?:知识|训练数据|资料)(?:里|中)?(?:没有|不包含|未包含|截止|不涉及|缺乏)'
    r'|(?:无法|不能|没法)(?:确认|提供|确定|得知|核实)(?:这个|该|相关|关于|这方面)?'
    r'|(?:没有|缺乏)(?:关于|有关)(?:这个|该|这款|它|其)?的?(?:资料|信息|数据)'
    r'|(?:不太|不)(?:清楚|确定|了解)(?:这个|该|这款|它)'
    r'|据我所知(?:没有|并不)'
    r')',
    re.IGNORECASE)

#: Text that is quoted/translated material, not the model's own admission.
_QUOTED_OR_TRANSLATED = re.compile(
    r'(?:'
    r'我说的是|原文(?:是|如下)?|以下是(?:原文|翻译|内容)|这段话|文中|引用|题目|题目是'
    r'|翻译(?:成|为|如下)|这段(?:话|文字)|用户(?:说|提供的)|输入(?:的)?(?:文字|内容)'
    r'|"{1}|"{1}|「|」|『|』'
    r')',
    re.IGNORECASE)

#: Follow-up shape: starts by pointing back ("那…呢", "它…吗", "再…").
_FOLLOWUP = re.compile(
    r'^\s*(?:那|那么|还有|另外|再|它|他|她|这个|那个|这些|上面|刚才|刚刚)'
)

#: Ask-phrase to strip before submitting a query.
_ASK_PREFIX = re.compile(
    r'^\s*(?:请|麻烦|帮我|帮忙|给我|能不能|可以|你能|你可以|请你)*\s*'
    r'(?:联网|上网|网上|在线)?\s*'
    r'(?:搜索一下|搜一下|查一下|找一下|搜索|检索|查一查|搜一搜|查|搜|找)'
    r'\s*(?:一下|一搜|一查|一找)?\s*(?:资料|信息|新闻|链接|来源|证据)?\s*[，,：:]?\s*'
)

#: A subject-less pointer: the message asks for a search but names nothing.
_BARE_DEICTIC = re.compile(
    r'^\s*(?:那个|这个|那个东西|这些东西|它|他|她|上面那个|刚才那个|之前那个)\s*$'
)

#: Leading qualifier on a follow-up to be merged with the previous subject.
_FOLLOWUP_QUALIFIER = re.compile(
    r'^(?:那|那么|还有|另外|再|它|他|她|这个|那个)\s*'
)

MIN_QUERY_CHARS = 2
MAX_QUERY_CHARS = 120


def _norm(text: str) -> str:
    return (text or '').strip()


def named_object(text: str) -> dict:
    """Does this message name a concrete external thing to look something up about?

    Returns {'present': bool, 'token': str, 'why': str}.

    Structural, not lexical-on-names:

      1. strip the stop phrases (ask verbs, question words, fact qualifiers);
      2. whatever CJK/latin runs of >=2 chars remain are candidate objects;
      3. if anything remains, the message names something.

    So "影之刃零" survives, while "给我讲讲该怎么办" and "开发商是谁" reduce to
    nothing. No product-name list is involved -- any concrete noun phrase behaves
    the same way, which is what makes this generalise past the reported example.
    """
    t = _norm(text)
    if not t:
        return {'present': False, 'token': '', 'why': 'empty'}

    reduced = t
    # A demonstrative + generic noun ("这游戏", "那款软件") points at something
    # already mentioned; it is not itself an object name.
    reduced = _GENERIC_DEICTIC.sub(' ', reduced)
    for phrase in _STOP_PHRASES:
        reduced = reduced.replace(phrase, ' ')
    # Punctuation and connectives are separators, not object material.
    reduced = re.sub(r'[^\w\u4e00-\u9fff]+', ' ', reduced)

    candidates = []
    for m in _OBJECT_TOKEN.finditer(reduced):
        tok = m.group(0)
        if tok in _STOP_TOKENS or len(tok) < 2:
            continue
        # Quality adjectives are not objects ("这游戏好玩吗" must not become the
        # query "好玩"). The object is the thing being judged, not the judgement.
        if tok in _QUALITY_TOKENS:
            continue
        candidates.append(tok)

    if candidates:
        best = max(candidates, key=len)
        return {'present': True, 'token': best, 'why': f'object_token:{best}'}
    if _OBJECT_PRONOUN.match(t):
        return {'present': False, 'token': '', 'why': 'pronoun_needs_context'}
    return {'present': False, 'token': '', 'why': 'no_object_token'}


def _info_request_about_object(text: str) -> Optional[dict]:
    """`info-request verb + named object`, excluding creative writing.

    Returns {'reason', 'matched', 'object'} or None.
    """
    t = _norm(text)
    if not t:
        return None
    creative = _CREATIVE.search(t)
    info = _INFO_REQUEST.search(t) or _OBJECT_FACT.search(t)
    if not info:
        return None
    obj = named_object(t)
    if not obj['present']:
        return None
    if creative:
        # A named object inside a creative request is material to invent with,
        # not a thing to look up.
        return None
    reason = ('object_fact_request' if _OBJECT_FACT.search(t)
              else 'object_info_request')
    return {'reason': reason, 'matched': info.group(0), 'object': obj['token']}


def _content_chars(text: str) -> str:
    """CJK + latin + digits, lowercased. Used for overlap tests."""
    return ''.join(ch.lower() for ch in (text or '') if ch.isalnum())


# --------------------------------------------------------------------------- #
# Decision
# --------------------------------------------------------------------------- #
def decide(text: str, *, allow_web: bool,
           recent_user_texts: Optional[Sequence[str]] = None,
           ) -> dict:
    """Decide whether THIS message should search.

    Returns a dict that is also the audit record:

        allow_web        bool  -- the user's switch, echoed for the log
        needs_search     bool  -- the decision
        reason           str   -- stable machine reason code
        matched          str   -- the signal that fired (for human review)
        confidence       'high' | 'medium' | 'low'
        blocked_by       str|None -- when allow_web is False but intent was there
        followup         bool  -- whether context is needed to make a query

    `recent_user_texts` is used only to recognise a follow-up/correction; it is
    never sent anywhere and never used to build the query here.
    """
    t = _norm(text)
    recent = list(recent_user_texts or [])
    out = {'allow_web': bool(allow_web), 'needs_search': False,
           'reason': 'no_signal', 'matched': '', 'confidence': 'low',
           'blocked_by': None, 'followup': False}

    if not t:
        out['reason'] = 'empty_message'
        return out

    explicit = _EXPLICIT_SEARCH.search(t)
    verify = _VERIFY.search(t)
    transform = _TRANSFORM.search(t)
    style_tweak = _STYLE_TWEAK.search(t)
    selfref = _SELF_REFERENTIAL.search(t)
    correction = _CORRECTION.search(t)
    arithmetic = bool(_ARITHMETIC.match(t)) and any(ch.isdigit() for ch in t)

    weather = _TOPIC_WEATHER.search(t)
    price = _TOPIC_PRICE.search(t)
    news = _TOPIC_NEWS.search(t)
    schedule = _TOPIC_SCHEDULE.search(t)
    fact = _TOPIC_FACT.search(t)
    tech = _TOPIC_TECH.search(t)
    research = _TOPIC_RESEARCH.search(t)
    fresh = _FRESH.search(t)
    # Research topics only count when the message also asks about freshness:
    # "实验进展如何" is about the world now, "如何写实验报告" is a how-to.
    if research and not fresh:
        research = None
    topical = weather or price or news or schedule or fact or tech or research

    out['followup'] = bool(_FOLLOWUP.match(t)) or bool(correction)

    # ---- 0. Round 55: an explicit "don't go online" for THIS message ------ #
    # Beats everything, including an explicit "搜一下" elsewhere in the sentence:
    # "别联网，直接告诉我影之刃零是什么" must not touch the network.
    no_web = _NO_WEB.search(t)
    if no_web:
        out.update(needs_search=False, reason='user_disabled_for_this_message',
                   matched=no_web.group(0), confidence='high',
                   blocked_by='user_disabled_for_this_message')
        return out

    # ---- 1. Explicit ask wins over everything ---------------------------- #
    # "不用搜了，查一下天气" is contradictory prose; the explicit ask is the
    # actionable half, so it decides.
    if explicit:
        out.update(needs_search=True, reason='explicit_search_request',
                   matched=explicit.group(0), confidence='high')
        # A follow-up phrased as an explicit ask still needs its subject filled
        # in ("查一下那明天呢"), or it would submit a dangling pronoun.
        out['followup'] = _needs_context(t)
        return _gate(out)

    # ---- 2. Verify an external fact -------------------------------------- #
    if verify:
        out.update(needs_search=True, reason='external_verification',
                   matched=verify.group(0), confidence='high')
        return _gate(out)

    # ---- 3. Direct-answer signals (negations) ---------------------------- #
    # Order matters: these are checked only when no explicit/verify intent was
    # found, so "帮我查一下" still searches even though "帮我" could appear.
    if transform:
        out.update(reason='text_transformation', matched=transform.group(0))
        return _gate(out)
    if style_tweak and not topical:
        out.update(reason='style_only_request', matched=style_tweak.group(0))
        return _gate(out)
    if arithmetic:
        out.update(reason='arithmetic', matched=t[:40])
        return _gate(out)
    if selfref and not topical:
        out.update(reason='about_self_or_app', matched=selfref.group(0))
        return _gate(out)
    if correction and not topical:
        out.update(reason='correction_to_previous_turn', matched=correction.group(0))
        return _gate(out)

    # ---- 4. Freshness-gated topics --------------------------------------- #
    # A topic word alone is not enough for the "fresh" ones: 天气 asks for data,
    # so it searches on its own; but a bare 最新/今天 does not.
    if topical:
        topic_name = ('weather' if weather else 'price' if price else
                      'news' if news else 'schedule' if schedule else
                      'fact' if fact else 'research' if research else 'tech')
        wanted = weather or price or news or schedule or fact or research or tech
        out.update(needs_search=True,
                   reason=f'topical_info_request:{topic_name}',
                   matched=wanted.group(0),
                   confidence='high' if (weather or price or news) else 'medium')
        if fresh:
            out['reason'] += '+fresh'
        return _gate(out)

    # ---- 4b. Round 55: "tell me about <named object>" -------------------- #
    # Checked AFTER the direct-answer rules, so "介绍一下你自己" (about the
    # assistant) is already gone, and AFTER the topic rules so a weather query
    # keeps its more specific reason. What reaches here is a lookup request about
    # a concrete thing the model has no strong reason to know.
    obj_req = _info_request_about_object(t)
    if obj_req:
        out.update(needs_search=True, reason=obj_req['reason'],
                   matched=obj_req['matched'], confidence='medium',
                   object_token=obj_req['object'])
        return _gate(out)

    # ---- 4c. Round 55: object fact + pronoun follow-up ------------------- #
    # "它什么时候发售" names nothing itself; if the previous turn named an object,
    # this is a lookup about that same object. The same applies to a bare fact
    # question with no pronoun at all ("开发商是谁", "谁开发的") -- it can only be
    # about the object under discussion.
    if recent:
        asks_fact = _OBJECT_FACT.search(t)
        bare_fact = asks_fact and len(_content_chars(t)) <= 12
        if asks_fact and (_OBJECT_PRONOUN.search(t) or bare_fact):
            prev_obj = named_object(recent[0])
            if prev_obj['present']:
                out.update(needs_search=True,
                           reason='object_fact_request:followup',
                           matched=asks_fact.group(0),
                           confidence='medium',
                           object_token=prev_obj['token'])
                out['followup'] = True
                return _gate(out)

    # ---- 5. Nothing matched -> answer directly ---------------------------- #
    # A bare follow-up ("那明天呢") inherits its topic from the previous turn. If
    # that turn was about data (weather/price/news...), this one still is -- the
    # user is narrowing the same question, not starting a new one. Without a
    # topical predecessor it stays a chat, which is the safe side.
    if out['followup'] and recent:
        inherited = _topic_in(recent[0])
        if inherited:
            out.update(needs_search=True,
                       reason=f'inherited_topic_from_previous_turn:{inherited}',
                       matched=_norm(recent[0])[:40], confidence='medium')
            return _gate(out)

    out['reason'] = ('freshness_without_topic' if fresh else 'no_signal')
    out['matched'] = fresh.group(0) if fresh else ''
    return _gate(out)


def _topic_in(text: str) -> Optional[str]:
    """Which data topic, if any, a message is about. Used for context inheritance."""
    t = _norm(text)
    if _TOPIC_WEATHER.search(t):
        return 'weather'
    if _TOPIC_PRICE.search(t):
        return 'price'
    if _TOPIC_NEWS.search(t):
        return 'news'
    if _TOPIC_SCHEDULE.search(t):
        return 'schedule'
    if _TOPIC_TECH.search(t):
        return 'tech'
    if _TOPIC_FACT.search(t) and _FRESH.search(t):
        return 'fact'
    return None


def _gate(out: dict) -> dict:
    """Apply the switch: intent without permission is refused, not softened."""
    if out['needs_search'] and not out['allow_web']:
        out['blocked_by'] = 'web_search_disabled'
        out['needs_search'] = False
        out['confidence'] = 'low'
    return out


def _needs_context(text: str) -> bool:
    """True when the message cannot stand alone as a query.

    After stripping the ask-phrase, a message whose remaining subject is empty or
    a bare pointer ("那明天呢", "那个") has to borrow the previous turn's subject.
    `介绍的是你，不是我` is NOT this case -- it has content, it is simply not
    about anything external, which is handled by the direct-answer rules.
    """
    stripped = _strip_ask(text)
    body = _FOLLOWUP_QUALIFIER.sub('', stripped).strip(' ，,。.！!？?：:')
    if not body:
        return True
    if _BARE_DEICTIC.match(stripped):
        return True
    # "明天呢" / "呢" alone
    if len(_content_chars(body)) <= 3 and body.endswith(('呢', '吗', '?')):
        return True
    return False


# --------------------------------------------------------------------------- #
# Query construction
# --------------------------------------------------------------------------- #
def build_query(text: str, decision: dict,
                recent_user_texts: Optional[Sequence[str]] = None,
                ) -> dict:
    """Turn the user's message into a standalone query, or refuse to.

    Returns:

        ok       bool
        query    str   -- '' when ok is False
        source   str   -- how it was built: 'as_is' | 'stripped_ask' |
                          'with_context' | 'none'
        needs_confirmation bool -- ask the user for the missing subject
        note     str   -- human-readable explanation, also logged
    """
    t = _norm(text)
    recent = [x for x in (recent_user_texts or []) if _norm(x)]
    out = {'ok': False, 'query': '', 'source': 'none',
           'needs_confirmation': False, 'note': ''}

    if not decision.get('needs_search'):
        out['note'] = '没有搜索决定，因此不构造查询'
        return out

    # A correction/continuation alone has no subject of its own. Reuse the
    # previous user turn's subject instead of submitting "不是我，是你".
    followup = bool(decision.get('followup'))
    if followup or _CORRECTION.search(t):
        if recent:
            # Prefer the OBJECT from the previous turn over its whole sentence:
            # "给我讲一讲影之刃零的信息吧" contributes "影之刃零", not the framing.
            prev_obj = named_object(recent[0])
            base = (prev_obj['token'] if prev_obj['present']
                    else _strip_ask(_norm(recent[0])))
            merged = _merge_followup(base, t)
            if merged:
                out.update(ok=True, query=merged, source='with_context',
                           note=f'当前是追问/纠正，用上一条用户问题补全对象：{base[:40]}')
                return _finalize(out)
        out.update(needs_confirmation=True,
                   note='消息是纠正或追问，但没有可补全的上文对象，应向用户简短确认要查什么')
        return out

    # Round 55: a query should be the OBJECT plus the fact asked about, not the
    # whole sentence -- but ONLY for the object routes. A topical query
    # ("今天上海天气怎么样") should keep its original form, which is already a
    # good search phrase.
    object_route = str(decision.get('reason') or '').startswith(
        ('object_info_request', 'object_fact_request'))
    obj = named_object(t)
    obj_token = decision.get('object_token') or obj.get('token') or ''

    if object_route and obj_token and not decision.get('followup'):
        qualifier = _fact_qualifier(t, drop=obj_token)
        q = f'{obj_token} {qualifier}'.strip() if qualifier else obj_token
        out.update(ok=True, query=q, source='object_plus_fact',
                   note=f'以对象「{obj_token}」为主，附上要问的事实')
        return _finalize(out)

    stripped = _strip_ask(t)
    # A message that still names nothing ("那个") must not be submitted: a bare
    # pronoun returns whatever the engine feels like and reads as a wrong answer.
    if _BARE_DEICTIC.match(stripped or t) or len(_content_chars(stripped)) < MIN_QUERY_CHARS:
        out.update(needs_confirmation=True,
                   note='去掉请求语后没有可检索的对象，应向用户确认要查什么')
        return out
    if stripped and stripped != t:
        out.update(ok=True, query=stripped, source='stripped_ask',
                   note='去掉「搜一下/帮我查」等请求语，提交核心问题')
    else:
        out.update(ok=True, query=t, source='as_is',
                   note='问题本身自足，直接作为查询')
    return _finalize(out)


def _fact_qualifier(text: str, *, drop: str = '') -> str:
    """The part of the message that says WHAT is being asked about the object.

    "它什么时候发售" -> "什么时候发售"; "给我讲一讲影之刃零的信息吧" -> "" (nothing
    beyond the object). Framing words are removed so the search engine sees the
    object and a fact qualifier, not a chat sentence.

    Single characters are dropped: after phrase-stripping, what survives of a
    framing word is usually a stray char ("能说说黑神话悟空的背景吗" leaves "能"),
    which would pollute the query.
    """
    t = _norm(text)
    if drop:
        t = t.replace(drop, ' ')
    for phrase in _STOP_PHRASES:
        t = t.replace(phrase, ' ')
    t = re.sub(r'[^\w\u4e00-\u9fff]+', ' ', t).strip()
    keep = [tok for tok in t.split() if len(tok) >= 2 and tok not in _STOP_TOKENS]
    return ' '.join(keep)[:40]


def _strip_ask(text: str) -> str:
    q = _ASK_PREFIX.sub('', text)
    q = q.strip(' ，,。.！!？?：:')
    return q


def _merge_followup(base: str, follow: str) -> Optional[str]:
    """Complete a follow-up with the previous subject.

    Deliberately simple and additive: keep the previous subject, append the new
    qualifier. It must not invent entities -- if the previous turn was itself
    empty, there is nothing to merge and the caller asks the user.

    Question scaffolding is dropped from both halves so the result reads as a
    search query ("今天上海天气怎么样" + "那明天呢" -> "今天上海天气 明天") rather
    than two questions glued together.
    """
    def _tidy(s: str) -> str:
        # Drop the deictic ("这游戏") and question scaffolding so the query is the
        # object plus the fact, not a chat sentence.
        s = _GENERIC_DEICTIC.sub(' ', s)
        s = _OBJECT_PRONOUN.sub('', s)
        s = re.sub(r'(?:怎么样|如何|是什么|是多少|多少|有哪些|有什么|呢|吗|嘛|啊)\s*$', '', s.strip())
        return re.sub(r'\s+', ' ', s).strip(' ，,。.！!？?：:、')

    f = _FOLLOWUP_QUALIFIER.sub('', _strip_ask(follow))
    f = _tidy(f)
    b = _tidy(_norm(base))
    if not b:
        return None
    if not f:
        return b
    if f in b:
        return b
    return f'{b} {f}'[:MAX_QUERY_CHARS]


def _finalize(out: dict) -> dict:
    q = out['query']
    if len(_content_chars(q)) < MIN_QUERY_CHARS:
        return {'ok': False, 'query': '', 'source': 'none',
                'needs_confirmation': True,
                'note': '构造出的查询过短，无法独立理解，应向用户确认要查什么'}
    # Deliberately NOT truncated here. A silently shortened query searches for
    # something the user did not ask; `chatsearch.validate_query` refuses an
    # over-long query where the limit actually applies, and the user is told.
    return out


# --------------------------------------------------------------------------- #
# Result filtering
# --------------------------------------------------------------------------- #
def _overlaps(query_chars: str, candidate: str) -> bool:
    """Does the candidate share any content with the query?

    Two CJK characters in a row matching is a strong signal in Chinese; for Latin
    we require a whole word of 3+ chars. A single shared character is not enough
    (「的」「是」「在」 appear everywhere).
    """
    cand = _content_chars(candidate)
    if not cand or not query_chars:
        return False
    # build bigrams of the query and look for them in the candidate
    q = query_chars
    for i in range(len(q) - 1):
        if q[i:i + 2] in cand:
            return True
    # latin words
    for word in re.findall(r'[a-z]{3,}', q):
        if word in cand:
            return True
    return False


def filter_results(results: Iterable[Any], query: str,
                   *, max_keep: int = 5) -> dict:
    """Keep only results that can actually support `query`.

    Returns {'kept': [...], 'dropped': [{index,title,reason}], 'reason': str}.
    A result is dropped when it has no usable title/url/snippet, or shares no
    content with the query. Dropping everything is a valid outcome: the caller
    then answers without web material and says so.
    """
    q_chars = _content_chars(query)
    kept: list[Any] = []
    dropped: list[dict] = []
    for r in results or []:
        title = getattr(r, 'title', '') or ''
        url = getattr(r, 'url', '') or ''
        snippet = getattr(r, 'snippet', '') or ''
        idx = getattr(r, 'index', None)
        if not (title.strip() or snippet.strip()):
            dropped.append({'index': idx, 'title': title[:60],
                            'reason': 'empty_title_and_snippet'})
            continue
        if not url.strip():
            dropped.append({'index': idx, 'title': title[:60], 'reason': 'no_url'})
            continue
        haystack = f'{title} {snippet} {url}'
        if q_chars and not _overlaps(q_chars, haystack):
            dropped.append({'index': idx, 'title': title[:60],
                            'reason': 'no_lexical_overlap_with_query'})
            continue
        kept.append(r)
        if len(kept) >= max_keep:
            break
    reason = ('kept_all' if kept and not dropped else
              'kept_some' if kept else
              'all_irrelevant' if results else 'no_results')
    return {'kept': kept, 'dropped': dropped, 'reason': reason,
            'considered': len(list(results or []))}


def summarise_decision(decision: dict, query_info: Optional[dict] = None,
                       filter_info: Optional[dict] = None,
                       *, executed: bool = False,
                       injected: bool = False) -> dict:
    """The audit record attached to a request. Never written to user memory."""
    q = query_info or {}
    f = filter_info or {}
    return {
        'allow_web': decision.get('allow_web'),
        'needs_search': decision.get('needs_search'),
        'reason': decision.get('reason'),
        'matched_signal': decision.get('matched'),
        'confidence': decision.get('confidence'),
        'blocked_by': decision.get('blocked_by'),
        'query': q.get('query') or '',
        'query_source': q.get('source') or 'none',
        'query_note': q.get('note') or '',
        'needs_confirmation': bool(q.get('needs_confirmation')),
        'search_executed': bool(executed),
        'results_considered': f.get('considered', 0),
        'results_kept': len(f.get('kept') or []),
        'results_dropped': len(f.get('dropped') or []),
        'filter_reason': f.get('reason') or '',
        'injected': bool(injected),
    }


# --------------------------------------------------------------------------- #
# Round 55: the gap-fill ("did the model actually lack the material?")
# --------------------------------------------------------------------------- #
def model_admits_gap(candidate_text: str, *, user_text: str = '') -> dict:
    """Did the answer say "I don't have this", in its OWN voice?

    This is the ONLY justification for a follow-up search when the initial route
    said "answer directly". It is not a confidence score -- a small model's
    self-reported confidence is not trustworthy -- it is a narrow textual test
    with two deliberate guards:

      1. A hit does NOT count when the message is quoted or translated material.
         `user_text` is checked for quotation/translation framing, because
         "帮我把这段话翻译成英文：我不知道" must not look like the model admitting
         ignorance -- and the string "我不知道" inside a translation would
         otherwise match.
      2. The matched phrase must be attributed to the model, not to a character
         or to the user. Phrases beginning with 我 are used for that reason.

    Returns {'gap': bool, 'matched': str, 'why': str}.
    """
    text = (candidate_text or '')
    if not text.strip():
        return {'gap': False, 'matched': '', 'why': 'empty_candidate'}

    # The user's own message asked us to quote/translate/explain THAT text: any
    # "不知道" inside the answer is material we were handed, not an admission
    # about the world. Either guard alone is enough -- an earlier version nested
    # them, so "帮我解释这段：我不知道" slipped through.
    if user_text:
        if _QUOTED_OR_TRANSLATED.search(user_text):
            return {'gap': False, 'matched': '',
                    'why': 'user_text_is_quoted_material'}
        if _TRANSFORM.search(user_text):
            return {'gap': False, 'matched': '',
                    'why': 'user_text_is_transformation_task'}
        if _CREATIVE.search(user_text):
            return {'gap': False, 'matched': '',
                    'why': 'user_text_is_creative_task'}

    m = _MODEL_INSUFFICIENT.search(text)
    if not m:
        return {'gap': False, 'matched': '', 'why': 'no_gap_phrase'}
    return {'gap': True, 'matched': m.group(0), 'why': 'model_states_insufficient'}


def can_gap_fill(decision: dict, *, user_text: str = '',
                 already_searched: bool, allow_web: bool,
                 recent_user_texts: Optional[Sequence[str]] = None) -> dict:
    """May this request run ONE follow-up search after a weak first answer?

    The limits, all enforced here so the call site cannot forget one:

      * the switch must allow the network;
      * the user must not have forbidden it for this message;
      * at most ONE round of searching per user request -- a request that already
        searched (or whose search failed / found nothing) never searches again;
      * a creative-task answer is never gap-filled (nothing external to find);
      * translation, quotes, self-introductions, casual chat, arithmetic, and
        style tweaks are excluded;
      * the request must be relevant to external facts or a concrete named object.
    """
    if not allow_web:
        return {'ok': False, 'why': 'web_not_allowed'}
    if decision.get('blocked_by') == 'user_disabled_for_this_message' or (
            user_text and _NO_WEB.search(user_text)):
        return {'ok': False, 'why': 'user_disabled_for_this_message'}
    if already_searched:
        return {'ok': False, 'why': 'one_round_only'}
    if decision.get('needs_search'):
        # The first pass already searched; a second round is the loop we forbid.
        return {'ok': False, 'why': 'first_pass_already_searched'}

    t = _norm(user_text)
    if t:
        if _CREATIVE.search(t):
            return {'ok': False, 'why': 'creative_task'}
        if (_TRANSFORM.search(t) or _QUOTED_OR_TRANSLATED.search(t)
                or decision.get('reason') == 'text_transformation'):
            return {'ok': False, 'why': 'transformation_or_quoted_task'}
        if (_SELF_REFERENTIAL.search(t)
                or decision.get('reason') == 'about_self_or_app'):
            return {'ok': False, 'why': 'self_or_app_chat'}
        if (_STYLE_TWEAK.search(t)
                or decision.get('reason') == 'style_only_request'):
            return {'ok': False, 'why': 'style_tweak'}
        if ((bool(_ARITHMETIC.match(t)) and any(ch.isdigit() for ch in t))
                or decision.get('reason') == 'arithmetic'):
            return {'ok': False, 'why': 'arithmetic'}

        obj = named_object(t)
        has_obj = bool(obj.get('present'))
        has_topic = bool(
            _topic_in(t) or _TOPIC_FACT.search(t) or _TOPIC_TECH.search(t)
            or _TOPIC_SCHEDULE.search(t) or _OBJECT_FACT.search(t)
        )
        has_context_obj = False
        recent = list(recent_user_texts or [])
        if recent and (_FOLLOWUP.match(t) or _OBJECT_PRONOUN.match(t)):
            prev_obj = named_object(recent[0])
            has_context_obj = bool(prev_obj.get('present') or _topic_in(recent[0]))
        if not (has_obj or has_topic or has_context_obj):
            return {'ok': False, 'why': 'no_external_object_or_fact'}

    return {'ok': True, 'why': 'eligible'}
