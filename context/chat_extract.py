"""
白名单会话 · 旁路信息卡提取（专用 Prompt，不走主 Agent 人设）。

原文只进会话缓存；本模块把有效信息抽成信息卡，再交给主 Agent。
原则：有效优先——有用内容保留足够细节，禁止为「短」而砍掉关键信息。
"""
from __future__ import annotations

import json
import re
from typing import Any

from apitools.feishu.client import CHAT_INFO_CARD_CONTRACT

# 分块：单次提取喂给旁路模型的原始条数（仍远小于全文进主 Agent 多轮滚雪球）
_CHUNK_SIZE = 80
_MERGE_MAX_CARDS = 40

EXTRACT_SYSTEM_PROMPT = """你是「白名单群聊 · 有效信息提取器」。这是一次独立任务，不是聊天助手人设。

# 目标
从近窗原始消息中提取**对用户仍有用的全部有效信息**，整理为信息卡片 JSON。
主 Agent 只会看到你的卡片，看不到群聊全文——所以你必须把有效内容写够，不能偷懒省略。

# 什么叫「有效」（必须保留）
- 可执行待办、截止日期、负责人/提出人
- 明确时间点的会议/评审/演示（schedule）
- 已达成的结论、字段/规范/流程约定（summary）
- 未决问题、待确认事项（open_question）
- 对上述内容的重要补充、约束条件、反对意见、依赖关系

# 什么叫「无效」（可丢弃）
- 纯应答：收到、嗯、好的、+1、表情、无信息闲聊
- 与工作无关的玩笑（若夹带有效信息则只保留有效部分）

# 细节原则（极其重要）
1. **有效优先，不为短而短**：action/schedule/open_question 的 detail 应写清「谁、要做什么、关键约束/上下文」；必要时 3～6 句也可以。禁止把有用信息压成含糊一句话。
2. **summary** 可相对概括，但仍须写清「约定了什么」，不要空标题。
3. **冲突或修订**：若后文修正前文，以较新表述为准，并在 detail 里点明「已更新」。
4. **说话人**：action / schedule / open_question 必须有 speakers；用消息里的 sender_name，否则用 sender_id；禁止臆造姓名。
5. **证据**：每张卡带 evidence_message_ids（来自输入的 message_id）；可多条。
6. **合并**：同一事项多轮讨论合成一张卡，detail 里保留关键分歧与最终倾向；不要拆成一堆碎片卡，也不要合并到丢失信息。
7. **不要编造**：消息里没有的时间、负责人、结论不要补全；不确定就放 open_question 或在 detail 标明不确定。
8. **不要输出原始群聊流水账**；输出的是结构化卡片。但卡片内容必须足够让主 Agent 向用户讲清楚，并决定是否写计划表/日历。
9. **检索锚点 quote_hints（必填）**：每张卡必须给出 3～5 个**原文中真实出现过的**短词/短语（黑话、专名、关键动词、独特约束），供主 Agent 字面检索追问原话。
   - 必须 verbatim 来自证据消息正文，禁止同义改写、禁止臆造；
   - 优先选「只在这段讨论里常见、能一针扎中原文」的字眼，不要空泛词（如「好的」「收到」）；
   - 不要大段粘贴整句；单条锚点宜 ≤20 字。

# 卡片 kind
- summary：结论 / 共识 / 规范约定
- action：可执行待办
- schedule：带明确时间的日程候选
- open_question：未决，勿当成已确认待办

# 输出格式（严格）
只输出一个 JSON 对象，不要 Markdown 围栏，不要前言后语：
{
  "cards": [
    {
      "kind": "summary|action|schedule|open_question",
      "title": "标题",
      "detail": "足够详细的有效说明（有效则写够，勿无谓压缩）",
      "when": "时间提示或空字符串",
      "priority": "高|中|低或空字符串",
      "speakers": ["说话人"],
      "evidence_message_ids": ["message_id"],
      "quote_hints": ["原文锚点1", "原文锚点2", "原文锚点3"]
    }
  ],
  "discarded_noise_note": "可选：一句说明滤掉了哪些类灌水"
}
"""


def _format_messages_for_extract(messages: list[dict[str, Any]]) -> str:
    lines = []
    for m in messages:
        mid = m.get("message_id") or ""
        who = m.get("speaker") or m.get("sender_name") or m.get("sender_id") or "?"
        when = m.get("time") or ""
        text = (m.get("text") or "").strip()
        if not text:
            continue
        lines.append(f"[{when}] ({mid}) {who}: {text}")
    return "\n".join(lines)


def _parse_json_object(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            return {"cards": data}
    except json.JSONDecodeError:
        pass
    # 容错：截取第一个 { ... } 或 [ ... ]
    for open_c, close_c in (("{", "}"), ("[", "]")):
        i = raw.find(open_c)
        j = raw.rfind(close_c)
        if i >= 0 and j > i:
            try:
                data = json.loads(raw[i : j + 1])
                if isinstance(data, dict):
                    return data
                if isinstance(data, list):
                    return {"cards": data}
            except json.JSONDecodeError:
                continue
    return {"cards": [], "parse_error": raw[:500]}


def _filter_verbatim_hints(hints: list[str], corpus: str) -> list[str]:
    """只保留能在原文 corpus 中字面找到的锚点；去重、截断。无 corpus 时原样轻裁。"""
    out: list[str] = []
    seen: set[str] = set()
    for raw in hints:
        h = str(raw or "").strip()
        if len(h) < 2:
            continue
        h = h[:40]
        key = h.lower()
        if key in seen:
            continue
        if corpus and h not in corpus and h.lower() not in corpus.lower():
            continue
        seen.add(key)
        out.append(h)
        if len(out) >= 5:
            break
    return out


def _normalize_cards(cards: Any, *, corpus: str = "") -> list[dict[str, Any]]:
    if not isinstance(cards, list):
        return []
    out: list[dict[str, Any]] = []
    for c in cards:
        if not isinstance(c, dict):
            continue
        kind = str(c.get("kind") or "summary").strip().lower()
        if kind not in ("summary", "action", "schedule", "open_question"):
            kind = "summary"
        title = str(c.get("title") or "").strip()
        detail = str(c.get("detail") or "").strip()
        if not title and not detail:
            continue
        speakers = c.get("speakers") or []
        if isinstance(speakers, str):
            speakers = [speakers]
        if not isinstance(speakers, list):
            speakers = []
        speakers = [str(s).strip() for s in speakers if str(s).strip()]
        evid = c.get("evidence_message_ids") or []
        if isinstance(evid, str):
            evid = [evid]
        if not isinstance(evid, list):
            evid = []
        evid = [str(x).strip() for x in evid if str(x).strip()]
        quotes = c.get("quote_hints") or []
        if isinstance(quotes, str):
            quotes = [quotes]
        if not isinstance(quotes, list):
            quotes = []
        quotes = _filter_verbatim_hints(
            [str(q).strip() for q in quotes if str(q).strip()],
            corpus,
        )
        out.append(
            {
                "kind": kind,
                "title": title or detail[:40],
                "detail": detail,
                "when": str(c.get("when") or "").strip(),
                "priority": str(c.get("priority") or "").strip(),
                "speakers": speakers,
                "evidence_message_ids": evid,
                "quote_hints": quotes,
            }
        )
    return out


def _invoke_extract(llm: Any, user_body: str) -> dict[str, Any]:
    from langchain_core.messages import HumanMessage, SystemMessage

    resp = llm.invoke(
        [
            SystemMessage(content=EXTRACT_SYSTEM_PROMPT),
            HumanMessage(content=user_body),
        ]
    )
    text = getattr(resp, "content", None) or str(resp)
    if isinstance(text, list):
        text = "".join(str(x) for x in text)
    return _parse_json_object(str(text))


def extract_chat_info_cards(
    messages: list[dict[str, Any]],
    *,
    llm: Any,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    旁路提取：返回 {cards, card_count, chunks_used, contract_version}。
    messages 建议已是最新在前；有效信息写够细节，不为短而短。
    """
    msgs = [m for m in (messages or []) if isinstance(m, dict) and (m.get("text") or "").strip()]
    meta = meta or {}
    if not msgs:
        return {
            "ok": True,
            "cards": [],
            "card_count": 0,
            "chunks_used": 0,
            "note": "no messages to extract",
        }

    corpus = "\n".join(str(m.get("text") or "") for m in msgs)

    chunks: list[list[dict[str, Any]]] = []
    for i in range(0, len(msgs), _CHUNK_SIZE):
        chunks.append(msgs[i : i + _CHUNK_SIZE])

    all_cards: list[dict[str, Any]] = []
    noise_notes: list[str] = []
    for idx, chunk in enumerate(chunks):
        header = (
            f"时间窗/元数据：{json.dumps(meta, ensure_ascii=False)}\n"
            f"本块 {idx + 1}/{len(chunks)}，本块 {len(chunk)} 条原始消息（共 {len(msgs)} 条）。\n"
            f"契约字段：{json.dumps(CHAT_INFO_CARD_CONTRACT.get('card_fields'), ensure_ascii=False)}\n"
            "请提取本块有效信息。有效则 detail 写够；勿为短而短；滤灌水；"
            "每卡必须给 quote_hints（3～5 个原文 verbatim 锚点）。\n\n"
            "消息列表：\n"
        )
        parsed = _invoke_extract(llm, header + _format_messages_for_extract(chunk))
        all_cards.extend(_normalize_cards(parsed.get("cards"), corpus=corpus))
        note = parsed.get("discarded_noise_note")
        if note:
            noise_notes.append(str(note))

    # 多块时再合并一轮，避免重复卡，同时禁止过度压缩 detail
    if len(chunks) > 1 and all_cards:
        merge_body = (
            "以下是多块提取得到的信息卡，请合并重复项，保留全部有效细节"
            "（合并后 detail 只许更完整，不许变空泛；quote_hints 合并后仍须是原文 verbatim）。"
            "输出同一 JSON schema。\n\n"
            + json.dumps({"cards": all_cards[:_MERGE_MAX_CARDS]}, ensure_ascii=False, indent=2)
        )
        merged = _invoke_extract(llm, merge_body)
        merged_cards = _normalize_cards(merged.get("cards"), corpus=corpus)
        if merged_cards:
            all_cards = merged_cards

    return {
        "ok": True,
        "cards": all_cards[:_MERGE_MAX_CARDS],
        "card_count": len(all_cards[:_MERGE_MAX_CARDS]),
        "chunks_used": len(chunks),
        "raw_message_count": len(msgs),
        "discarded_noise_note": "；".join(noise_notes)[:500] if noise_notes else "",
        "detail_policy": "preserve_useful_detail_not_forced_short",
    }
