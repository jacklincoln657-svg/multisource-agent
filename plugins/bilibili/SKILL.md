# B 站插件用法

## 何时用
- 用户问最近看了什么、观看历史、关注列表、某 UP 主页/近期投稿等。
- 娱乐轨迹走 B 站，禁止误调飞书。

## 工具
- `fetch_bilibili_data`：资料 + 观看历史（默认轻量）
- `fetch_bilibili_followings` / `lookup_bilibili_up`：仅用户要细说或同意后再调
- `fetch_mock_bilibili_data`：无 Cookie 时离线演示

## 调用节制
- 默认 `fetch_bilibili_data(history_n≈30)`；用户明确「多拉 / 几百条」时 history_n 上限 300。
- 禁止对观看历史里每个作者自动 `lookup_bilibili_up`。
- 禁止顺手连环深挖关注/多 UP。

## 常用字段
- `profile` / `history[]`（含 `author_mid`）/ `followings[]` / `uploads[]`

## 边界
- **只有轨迹与元数据**，不含口播/完整剧情。
- 问「讲了什么 / 剧情 / 内容详解」且只有标题 → 升级 `web_search`（必要时 `web_fetch`）；证据升级，不算乱搜。
- 查 B 站空间页用 `lookup_bilibili_up`，勿 `web_fetch` 空间 HTML。
