# 飞书工作面插件用法

## 何时用
- 工作安排 / 日程 / 近日计划 / 写回日历或表格 / 整理白名单群聊待办。
- **禁止**用 Steam、B 站或网页代替飞书完成上述工作面任务。
- 娱乐轨迹（游戏/听歌/B 站）仍走对应平台，禁止误调飞书。

## 工具一览
- 日历：`fetch_feishu_calendar` / `create_feishu_event` / `create_feishu_events`（批量串行）
- 计划表：`fetch_feishu_plan_table` / `upsert_feishu_plan_rows`
- 白名单会话：`fetch_feishu_chat_digest` / Mock → **info_cards**；追问原话 `query_cached_chat`
- Mock：`fetch_mock_feishu_calendar` / `fetch_mock_feishu_plan_table` / `fetch_mock_feishu_chat_digest`

## 写回纪律
- **用户确认前**不得 `upsert_feishu_plan_rows` / `create_feishu_event(s)`。
- 多条日程必须用 `create_feishu_events` 一次写入；同题内第 2 次 `create_feishu_event` 会被**代码硬闸**拒绝。

## 白名单会话流程
1. `fetch_feishu_chat_digest`（或 Mock）→ 直接使用返回的 **info_cards**（有效细节写够，不为短而短）。
2. 向用户展示并问如何处理（只整理 / 写计划表 / 写日程 / 混合）。
3. 用户确认后再写计划表/日程。
4. **追问某句原话** → `query_cached_chat`；禁止臆造、禁止把群聊全文塞进历史。
   - **优先**把对应卡的 `quote_hints` 用逗号拼进 `keyword`；不够再换用户说法或 speaker。
5. **换摄入参数 / 重新整理 / 重拉**（改 hours、recent_n）→ 再调 `fetch_feishu_chat_digest`；
   上一轮助手正文不是当前群聊真相，勿复述「只有几条」搪塞。

### 摄入模式
- 活跃群用 `hours` 时间窗；不活跃/隔很久才聊 → 用 `recent_n`（如 50）。
- 用户说「最近 50 条」→ `recent_n=50`；说「近 12/48 小时」→ `hours=…` 且 `recent_n=0`。
- 原文只在后端缓存；禁止监控全部聊天，仅白名单。

## 常用字段
- 日程 `events[].summary/start/end`
- 计划表 `records[].日期/事项/优先级/来源/状态/备注`
- 信息卡 `info_cards[]`（kind/title/detail/speakers/evidence_message_ids/quote_hints）
