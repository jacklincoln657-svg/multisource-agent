# Steam 插件用法

## 何时用
- 用户问玩过什么、库时长、拥有哪些游戏、商店检索/详情、是否值得买等。
- 娱乐轨迹走 Steam，禁止误调飞书工作面工具。

## 工具
- `fetch_steam_data` / `fetch_mock_steam_data`
- `search_steam_store` / `lookup_steam_app`

## 常用字段
- 库：`name` / `playtime_forever`(分钟) / `playtime_2weeks` / `appid`
- 商店卡：`genres` / `short_description` / `price` / `review_label` / `positive_pct`

## 边界
- 只有游玩与商店元数据，不含完整剧情/长评正文。
- 问「讲了什么 / 剧情」且本站只有标题级信息 → 按全局规则升级网页证据（不算乱搜）。
- 轨迹类够用就停；不要无脑连环深挖。
