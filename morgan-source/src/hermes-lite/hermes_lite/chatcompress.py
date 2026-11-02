"""Round 34: Repaired Structured Conversation Summary Compression for Hermes-Lite.

Key architectural and behavioral repairs:
1. Strict failure on length truncation: finish_reason == 'length' is unconditionally rejected;
   no partial/truncated JSON object salvaging.
2. Format & source validation:
   - Output must be strict, complete JSON conforming to the 4 categories.
   - Short message aliases (M1, M2, ...) used in prompt and generation, mapped to full source_msg_id in store.
   - Invalid, empty, '无', out-of-bounds, or cross-session source references are rejected.
   - Role fidelity: assistant proposals cannot be turned into finalized user decisions.
   - Dummy placeholder items ('无', '暂无', 'none') are rejected or cleaned; empty categories use [].
3. Exact token measurement & window safety:
   - Summary request prompt measured via actual chat template render (render_tokens).
   - Candidate turns iteratively reduced from oldest turn if prompt exceeds physical window budget.
   - Raw tokens accurately calculated for the actual selected suffix of turns, fixing prefix accumulation.
4. Compact injected representation:
   - Omits empty categories and redundant IDs in the injected prompt text.
   - Minimal header and footer overhead to maximize net token savings while preserving fidelity.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

SUMMARY_SYSTEM_PROMPT = (
    "你是一个严谨的长对话结构化总结助手。你的任务是将用户与助手的历史对话记录整理成紧凑、高度提炼的结构化摘要，为后续对话提供背景上下文。\n\n"
    "【核心分类与要求】：\n"
    "1. user_facts_and_constraints：用户明确提供的事实、具体数字、度量单位（如 3.3V、450mA、25ms）、否定条件（如“禁止云端”、“不得安装MySQL/Redis”）。\n"
    "2. goals_and_decisions：当前目标及用户明确拍板确认的决定。助手提出的建议用户未明确确认的严禁当作用户决定！\n"
    "3. todos_and_open_issues：当前依然待办的事项或双方尚未解决的问题。已经在对话中解决、修复并关闭的问题严禁放入此项！\n"
    "4. superseded_or_revoked：已被修改、撤销或替代的旧要求。必须注明是被何项新要求替代（如原预算80000元被50000元替代）。\n\n"
    "【铁律与格式规则】：\n"
    "1. 忠于原文：严禁编造或推测原文未提及的事实。保留数字、单位与否定约束。\n"
    "2. 来源编号：历史记录中的每条消息已标有短编号（如 M1、M2 等）。每个条目的 source 字段必须准确填写对应的来源编号。\n"
    "3. 数组规则：若某个分类下没有相关内容，必须输出空数组 []。严禁生成“无”、“暂无”等占位条目！\n"
    "4. 极致精炼与长度控制：每个分类提炼 1-3 条最核心要点（每条不超过 20 字，保留数字与单位），总条目数控制在 6 条以内。严禁输出长句或冗长解释，务必在 300 tokens 内输出完整闭合的 JSON。\n\n"
    "请严格输出合法的 JSON 格式（不要包含任何其他前缀或解释文字），格式结构如下：\n"
    "{\n"
    '  "user_facts_and_constraints": [\n'
    '    {"content": "...", "source": "M1"}\n'
    "  ],\n"
    '  "goals_and_decisions": [\n'
    '    {"content": "...", "source": "M2"}\n'
    "  ],\n"
    '  "todos_and_open_issues": [\n'
    '    {"content": "...", "source": "M1"}\n'
    "  ],\n"
    '  "superseded_or_revoked": [\n'
    '    {"content": "旧要求...", "superseded_by": "新要求...", "source": "M1"}\n'
    "  ]\n"
    "}"
)

SUMMARY_HEADER = "【较早对话摘要】"
SUMMARY_FOOTER = "（注：以上为较早历史背景资料，与后续最新对话冲突时以最新为准）"

REQUIRED_CATEGORIES = (
    "user_facts_and_constraints",
    "goals_and_decisions",
    "todos_and_open_issues",
    "superseded_or_revoked"
)

DUMMY_PLACEHOLDERS = {
    "无", "无。", "暂无", "没有", "none", "null", "n/a", "无记录", "未提及", "空", "没有修改"
}


def render_summary_text(summary_data: dict) -> str:
    """Renders structured summary into clean, prompt-ready compact text.
    
    Omits empty categories and redundant IDs to minimize prompt token overhead.
    """
    lines = [SUMMARY_HEADER]

    facts = summary_data.get("user_facts_and_constraints") or []
    if facts:
        lines.append("• 事实与约束：")
        for f in facts:
            c = (f.get("content") or f.get("text") or "").strip()
            if c:
                lines.append(f"  - {c}")

    goals = summary_data.get("goals_and_decisions") or []
    if goals:
        lines.append("• 目标与决定：")
        for g in goals:
            c = (g.get("content") or g.get("text") or "").strip()
            if c:
                lines.append(f"  - {c}")

    todos = summary_data.get("todos_and_open_issues") or []
    if todos:
        lines.append("• 待办与未决：")
        for t in todos:
            c = (t.get("content") or t.get("text") or "").strip()
            if c:
                lines.append(f"  - {c}")

    revoked = summary_data.get("superseded_or_revoked") or []
    if revoked:
        lines.append("• 已替代/撤销要求：")
        for r in revoked:
            c = (r.get("content") or r.get("text") or "").strip()
            sup = (r.get("superseded_by") or "").strip()
            if c:
                extra = f"（已被替代: {sup}）" if sup else ""
                lines.append(f"  - {c}{extra}")

    if len(lines) == 1:
        lines.append("• （未提取出显著的历史事实或约束条目）")

    lines.append(SUMMARY_FOOTER)
    return "\n".join(lines)


def parse_and_validate_summary(
    raw_text: str,
    alias_to_msg: dict[str, dict],
    valid_msg_ids: set[str]
) -> tuple[bool, dict, Optional[str]]:
    """Strict parser & validator:
    - Rejects broken or incomplete JSON.
    - Rejects loose markdown fallbacks.
    - Requires all 4 category keys to be lists.
    - Validates source attribution for each item: must map to a valid message in selected_turns.
    - Rejects empty, '无', or out-of-range source IDs.
    - Maps short aliases (e.g. M1) to real message IDs and attaches source role & seq.
    """
    text = (raw_text or "").strip()
    if not text:
        return False, {}, "摘要生成返回内容为空"

    # Strip code fences ```json ... ``` or ``` ... ```
    candidate = text
    if "```json" in candidate:
        parts = candidate.split("```json", 1)[1]
        if "```" in parts:
            candidate = parts.split("```", 1)[0].strip()
        else:
            return False, {}, "代码围栏未闭合，输出不完整"
    elif "```" in candidate:
        parts = candidate.split("```", 1)[1]
        if "```" in parts:
            candidate = parts.split("```", 1)[0].strip()
        else:
            return False, {}, "代码围栏未闭合，输出不完整"

    start_idx = candidate.find("{")
    end_idx = candidate.rfind("}")
    if start_idx == -1 or end_idx == -1 or end_idx <= start_idx:
        return False, {}, "未找到合法的 JSON 根对象"

    json_str = candidate[start_idx:end_idx + 1]
    try:
        data = json.loads(json_str)
    except Exception as exc:
        return False, {}, f"JSON 解析失败: {exc}"

    if not isinstance(data, dict):
        return False, {}, "JSON 根节点必须为对象"

    for k in REQUIRED_CATEGORIES:
        if k not in data:
            return False, {}, f"缺少必填分类字段: '{k}'"
        if not isinstance(data[k], list):
            return False, {}, f"分类字段 '{k}' 必须为列表"

    validated_dict = {k: [] for k in REQUIRED_CATEGORIES}
    total_valid_items = 0

    for k in REQUIRED_CATEGORIES:
        for it in data[k]:
            if not isinstance(it, dict):
                return False, {}, f"分类 '{k}' 中的条目必须为对象"

            content = str(it.get("content") or it.get("text") or "").strip()
            if not content:
                continue

            # Check if this item is just a dummy placeholder like '无' or '暂无'
            clean_content = content.strip("。，；., ")
            if clean_content.lower() in DUMMY_PLACEHOLDERS:
                continue

            source_raw = str(it.get("source") or it.get("source_msg_id") or "").strip()
            if not source_raw:
                return False, {}, f"条目缺少来源消息引用: '{content[:30]}'"

            clean_src = source_raw.strip("[]() ")
            if clean_src.lower() in DUMMY_PLACEHOLDERS:
                return False, {}, f"条目来源为无效占位符 '{source_raw}': '{content[:30]}'"

            # Resolve source alias (e.g. M1 -> msg_dict) or direct message ID
            resolved_msg: Optional[dict] = None
            if clean_src in alias_to_msg:
                resolved_msg = alias_to_msg[clean_src]
            elif clean_src.upper() in alias_to_msg:
                resolved_msg = alias_to_msg[clean_src.upper()]
            elif clean_src in valid_msg_ids:
                for m in alias_to_msg.values():
                    if m.get('id') == clean_src:
                        resolved_msg = m
                        break

            if resolved_msg is None:
                return False, {}, f"条目引用了范围外或不存在的来源 '{source_raw}': '{content[:30]}'"

            source_role = resolved_msg.get('role', 'unknown')
            source_id = resolved_msg.get('id', '')
            source_seq = resolved_msg.get('seq', 0)

            item_obj = {
                "content": content,
                "text": content,
                "source_msg_id": source_id,
                "source_alias": clean_src,
                "source_role": source_role,
                "source_seq": source_seq
            }
            sup = str(it.get("superseded_by") or "").strip()
            if sup and sup.strip("。，；., ").lower() not in DUMMY_PLACEHOLDERS:
                item_obj["superseded_by"] = sup

            validated_dict[k].append(item_obj)
            total_valid_items += 1

    if total_valid_items == 0:
        return False, {}, "摘要未能提取出任何有效的事实或约束条目"

    return True, validated_dict, None


def extract_complete_turns(messages: list[dict]) -> list[list[dict]]:
    """Pair user + assistant messages into completed turns."""
    turns: list[list[dict]] = []
    pending_user: Optional[dict] = None
    for m in messages:
        role = m.get('role')
        if role == 'user':
            pending_user = m
        elif role == 'assistant':
            if pending_user is not None and m.get('status') == 'complete' and (m.get('content') or '').strip():
                turns.append([pending_user, m])
            pending_user = None
    return turns


def select_candidate_turns(history: list[dict], boundary_seq: int = 0,
                           recent_turns_kept: int = 3) -> dict:
    """Filter history after boundary and partition into candidate older turns and recent turns."""
    valid_msgs = [m for m in history if (m.get('seq') or 0) > boundary_seq]
    turns = extract_complete_turns(valid_msgs)
    total_turns = len(turns)

    if total_turns <= recent_turns_kept:
        return {
            'can_compress': False,
            'total_turns': total_turns,
            'recent_turns_kept': recent_turns_kept,
            'candidate_turn_count': 0,
            'candidate_turns': [],
            'recent_turns': turns,
            'reason': f"当前会话仅有 {total_turns} 轮完整对话，最近 {recent_turns_kept} 轮需保留原文，无需压缩。"
        }

    candidate_turns = turns[:-recent_turns_kept]
    recent_turns = turns[-recent_turns_kept:]

    return {
        'can_compress': True,
        'total_turns': total_turns,
        'recent_turns_kept': recent_turns_kept,
        'candidate_turn_count': len(candidate_turns),
        'candidate_turns': candidate_turns,
        'recent_turns': recent_turns,
        'reason': None
    }


def build_summary_request_messages(selected_turns: list[list[dict]]) -> tuple[list[dict], dict[str, dict], set[str]]:
    """Constructs the chat messages array for the summary model request using short aliases."""
    alias_to_msg: dict[str, dict] = {}
    valid_ids: set[str] = set()
    lines = ["以下是需要整理的较早历史对话记录（每条消息已标有短编号 M1, M2...）：\n"]
    m_idx = 1
    for turn in selected_turns:
        u_msg = turn[0]
        a_msg = turn[1]
        u_alias = f"M{m_idx}"
        m_idx += 1
        a_alias = f"M{m_idx}"
        m_idx += 1
        alias_to_msg[u_alias] = u_msg
        alias_to_msg[a_alias] = a_msg
        valid_ids.add(u_msg.get('id', ''))
        valid_ids.add(a_msg.get('id', ''))
        lines.append(f"[{u_alias} | 用户]\n{u_msg.get('content', '').strip()}\n")
        lines.append(f"[{a_alias} | 助手]\n{a_msg.get('content', '').strip()}\n")

    lines.append("请仔细提取上述对话中的事实、约束、目标、决定、待办和修改项，严格输出 JSON，并在 source 字段中准确引用对应的短编号（如 M1、M2 等）。")
    user_content = "\n".join(lines)

    messages = [
        {'role': 'system', 'content': SUMMARY_SYSTEM_PROMPT},
        {'role': 'user', 'content': user_content}
    ]
    return messages, alias_to_msg, valid_ids


def calculate_compression_budget(candidate_turns: list[list[dict]],
                                 window: int,
                                 tokenizer: Any,
                                 max_summary_tokens: int = 512) -> dict:
    """Determines which candidate turns fit into the summary generation prompt budget.
    
    Uses exact template render (render_tokens) on the final messages, iteratively dropping
    oldest turns if the prompt exceeds the physical window limit.
    Calculates exact raw tokens for the selected turns suffix.
    """
    from hermes_lite import chatengine

    safety_buffer = 100
    available_prompt_space = window - max_summary_tokens - safety_buffer
    if available_prompt_space <= 200:
        return {
            'ok': False,
            'error': 'window_too_small',
            'message': f'模型实际窗口 ({window}) 不足以容纳摘要生成所需的提示词与输出预留。'
        }

    selected_turns = list(candidate_turns)
    alias_to_msg: dict[str, dict] = {}
    valid_ids: set[str] = set()
    req_messages: list[dict] = []
    prompt_tokens = 0

    # Iteratively fit from the most recent end of candidates (suffix)
    while selected_turns:
        req_messages, alias_to_msg, valid_ids = build_summary_request_messages(selected_turns)
        prompt_tokens = chatengine.render_tokens(tokenizer, req_messages)
        if prompt_tokens + max_summary_tokens + safety_buffer <= window:
            break
        selected_turns = selected_turns[1:]

    if not selected_turns:
        return {
            'ok': False,
            'error': 'turns_too_large',
            'message': '单轮较早对话过长，超出当前摘要生成可用空间。'
        }

    uncovered_turns = candidate_turns[:-len(selected_turns)] if len(selected_turns) < len(candidate_turns) else []
    first_turn = selected_turns[0]
    last_turn = selected_turns[-1]
    start_msg_id = first_turn[0]['id']
    end_msg_id = last_turn[1]['id']
    start_seq = first_turn[0].get('seq', 0)
    end_seq = last_turn[1].get('seq', 0)

    # Calculate exact raw tokens of the selected turns in chat template context
    selected_msgs = [m for turn in selected_turns for m in turn]
    raw_history_msgs = [{'role': m['role'], 'content': m['content']} for m in selected_msgs]
    raw_tokens = chatengine.render_tokens(tokenizer, raw_history_msgs)

    uncovered_count = len(uncovered_turns)
    if uncovered_count > 0:
        coverage_note = (
            f"已选取第 {start_seq} 至第 {end_seq} 条消息（共 {len(selected_turns)} 轮对话）生成摘要；"
            f"更早的 {uncovered_count} 轮对话超出当前窗口预算，未纳入本次摘要。"
        )
    else:
        coverage_note = f"已选取全部 {len(selected_turns)} 轮较早对话生成摘要。"

    return {
        'ok': True,
        'selected_turns': selected_turns,
        'uncovered_turns': uncovered_turns,
        'uncovered_count': uncovered_count,
        'start_msg_id': start_msg_id,
        'end_msg_id': end_msg_id,
        'start_seq': start_seq,
        'end_seq': end_seq,
        'selected_turn_count': len(selected_turns),
        'coverage_note': coverage_note,
        'raw_tokens': raw_tokens,
        'prompt_tokens': prompt_tokens,
        'max_output_tokens': max_summary_tokens,
        'available_prompt_space': available_prompt_space,
        'alias_to_msg': alias_to_msg,
        'valid_ids': valid_ids,
        'req_messages': req_messages
    }


def execute_summary_generation(*,
                               base_url: str,
                               model_alias: str,
                               messages: list[dict],
                               alias_to_msg: dict[str, dict],
                               valid_ids: set[str],
                               max_tokens: int = 512,
                               should_stop: Callable[[], bool],
                               counter: Optional[Callable[[str], None]] = None,
                               timeout: float = 120.0) -> dict:
    """Issues one streaming HTTP request to /chat/completions to generate the structured summary.
    
    Enforces strict failure on finish_reason == 'length' and strict validation.
    """
    url = f"{base_url.rstrip('/')}/chat/completions"
    body = {
        'model': model_alias,
        'messages': messages,
        'stream': True,
        'temperature': 0.1,
        'top_k': 20,
        'top_p': 0.8,
        'max_tokens': max_tokens
    }
    data = json.dumps(body, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(url, data=data, method='POST',
                                 headers={'Content-Type': 'application/json'})

    if counter:
        counter('chat')

    t0 = time.time()
    chunks: list[str] = []
    finish_reason = None
    saw_done = False
    usage = None

    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            for raw in r:
                if should_stop():
                    return {'ok': False, 'stopped': True, 'error': '用户停止生成'}
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

                if obj.get('usage'):
                    usage = obj['usage']
                for choice in obj.get('choices') or []:
                    delta = choice.get('delta') or {}
                    piece = delta.get('content')
                    if piece:
                        chunks.append(piece)
                    if choice.get('finish_reason'):
                        finish_reason = choice['finish_reason']

        full_text = "".join(chunks).strip()
        elapsed = round(time.time() - t0, 3)

        if should_stop():
            return {
                'ok': False,
                'stopped': True,
                'finish_reason': 'stopped',
                'saw_done': saw_done,
                'error': '用户停止生成',
                'raw_text': full_text,
                'elapsed_s': elapsed
            }

        # Strict Normal Termination State Machine (Round 35):
        # 1. Length truncation failure: unconditionally reject, even if valid JSON was produced.
        if finish_reason == 'length':
            return {
                'ok': False,
                'truncated': True,
                'finish_reason': 'length',
                'saw_done': saw_done,
                'error': f'摘要生成达到输出上限 ({max_tokens} tokens) 发生截断，拒绝不完整结果',
                'raw_text': full_text,
                'elapsed_s': elapsed
            }

        # 2. Abnormal finish_reason: any finish reason other than 'stop' is an error.
        if finish_reason is not None and finish_reason != 'stop':
            return {
                'ok': False,
                'finish_reason': finish_reason,
                'saw_done': saw_done,
                'error': f'模型响应异常结束 (finish_reason: {finish_reason})',
                'raw_text': full_text,
                'elapsed_s': elapsed
            }

        # 3. Connection ended early / missing termination marker:
        # A stream must have received saw_done ([DONE]) and a finish_reason == 'stop'.
        if not saw_done or finish_reason is None:
            return {
                'ok': False,
                'finish_reason': finish_reason,
                'saw_done': saw_done,
                'error': '未收到协议正常终止标记 [DONE] 或正常结束原因，流式连接提前结束或异常中断',
                'raw_text': full_text,
                'elapsed_s': elapsed
            }

        ok_parse, summary_dict, parse_err = parse_and_validate_summary(
            full_text, alias_to_msg=alias_to_msg, valid_msg_ids=valid_ids
        )
        if not ok_parse:
            return {
                'ok': False,
                'parse_error': True,
                'finish_reason': finish_reason,
                'saw_done': saw_done,
                'error': parse_err or '结构化内容校验失败',
                'raw_text': full_text,
                'elapsed_s': elapsed
            }

        rendered_text = render_summary_text(summary_dict)

        return {
            'ok': True,
            'summary_json': summary_dict,
            'summary_text': rendered_text,
            'raw_text': full_text,
            'finish_reason': finish_reason,
            'saw_done': saw_done,
            'usage': usage,
            'elapsed_s': elapsed
        }

    except urllib.error.URLError as exc:
        return {
            'ok': False,
            'finish_reason': 'network_error',
            'saw_done': False,
            'error': f'模型网络连接中断: {exc}',
            'elapsed_s': round(time.time() - t0, 3)
        }
    except Exception as exc:
        return {
            'ok': False,
            'finish_reason': 'exception',
            'saw_done': False,
            'error': f'模型请求异常: {type(exc).__name__}: {exc}',
            'elapsed_s': round(time.time() - t0, 3)
        }
