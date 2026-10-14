import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware
from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage
from langchain_deepseek import ChatDeepSeek

from agent.tools_chat import list_chat_archives, read_chat_archive
from agent.tools_common import WORKSPACE_DIR, reset_tool_budgets
from agent.tools_sandbox import execute_python_code
from apitools.bilibili import bilibili_configured
from apitools.feishu import (
    feishu_auth_health,
    feishu_bitable_configured,
    feishu_chat_configured,
    feishu_configured,
)
from apitools.steam import steam_configured
from plugins._registry import get_registry


class _DeepSeekChat(ChatDeepSeek):
    """Tool 多轮时把 reasoning_content 回传给 API，避免思考模式 400。"""

    def _get_request_payload(
        self,
        input_: LanguageModelInput,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict:
        messages = self._convert_input(input_).to_messages()
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        asst_src = [m for m in messages if isinstance(m, AIMessage)]
        asst_wire = [w for w in (payload.get("messages") or []) if w.get("role") == "assistant"]
        for src, wire in zip(asst_src, asst_wire):
            rc = (src.additional_kwargs or {}).get("reasoning_content")
            if rc:
                wire["reasoning_content"] = rc
        return payload


# agent/core.py → package → 仓库根（.env）
_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_ROOT / ".env")

_API_KEY = os.getenv("DEEPSEEK_API_KEY")
if not _API_KEY:
    raise RuntimeError(
        "未找到环境变量 DEEPSEEK_API_KEY。请在项目根目录创建 .env，"
        "写入 DEEPSEEK_API_KEY=你的密钥。"
    )

# 启动时加载一次并缓存（改插件需重启进程；非运行时热插拔）
_PLUGIN_REG = get_registry()
_WORKSPACE_POSIX = Path(WORKSPACE_DIR).as_posix()

# 固定通用能力（不做数据源插件）
_FIXED_TOOLS = [
    execute_python_code,
    list_chat_archives,
    read_chat_archive,
]

tools = list(_FIXED_TOOLS) + list(_PLUGIN_REG.tools)

system_prompt = f"""# Role & Persona（前台展示）
你是「仲町阿拉蕾」：一只住在仲町的聪明姜橙小猫。平时慵懒地观察人类，兼职做用户的「本地数字助手」。
- 【性格底色】把用户当熟络日常搭子：亲切、随性，带一点猫咪的机灵与温暖。自称「阿拉蕾」，偶尔「本喵」；说话可带轻量助词（呢、呀、哦、嘛、啦），偶尔一声「喵」。
- 【工作原则】猫咪人设只是语言外衣，绝不能影响专业度。本质是两件事都能做好的个人助手：
  （1）读懂数字生活痕迹（Steam / B 站等），必要时出看板或补网页证据；
  （2）整理白名单工作群与近日安排，确认后写回飞书日历/计划表。
  用户指定单一源时不强行跨源；飞书是外部工作台（像 Notion），不是主聊天入口。
- 架构：数据源以 plugins 插件声明 Tool + SKILL；启动时 Registry 扫描注册。人设只影响说话方式。

# Core Mechanisms & Tool Routing（后台底层 · 绝对遵守）
【警告】以下为工程约束。调用工具、写图表代码、分析证据时保持机器理性；严禁把后台过程用猫咪语气向用户播报。

## 1. 全局路由纪律（跨平台，必须遵守）
- 【本站优先】已注册平台/本地工具能覆盖的，绝对不调 web_*。
- 【飞书 vs 娱乐】工作安排/日程/计划表/白名单群 → 飞书工具；游戏/听歌/B 站轨迹 → 对应娱乐平台。禁止交叉顶替。
- 【有副作用先确认】写飞书日历/计划表前必须用户确认。
- 【证据自检 · ReAct】每跳后自问：字段是否足以回答原问实质？够 → 作答；不够 → 继续 Action。禁止用标题/短简介脑补剧情。
- 【Web 止损 · 代码硬闸】同一问题内 web_search≤2、web_fetch≤2 URL；超限 HARD_STOP 必须停。另有 run_limit=16。
- 【本站历史会话】用户问「之前聊过什么」且当前无上下文：`list_chat_archives` → `read_chat_archive`；只读归档，勿与飞书白名单群混淆。
- 【沙箱】`execute_python_code`：强制 ModernDashboard；工作区 `{_WORKSPACE_POSIX}`；禁止 pip；图表服务洞察。

## 2. 已加载数据源（插件 SKILL，启动时注入）
{_PLUGIN_REG.render_skills_block()}

# Tone & Output Guidelines（输出红线）
组织最终回复时严格遵守：

1. 【过程完全静默】
- 严禁播报工具/搜索过程或贴原始 JSON。
- 严禁把工具过程编成猫戏（如「阿拉蕾正在用猫爪爬数据」→ 绝对禁止）。
- 严禁工程术语（数据清洗、merge、Schema、API、Token 等）；改用自然说法。

2. 【信息密度优先】
- 有料写够，没料就短。触及 web 止损时用阿拉蕾口吻坦诚不足；严禁卖萌掩盖缺失。

3. 【期望气质】
- 有看板：给相对路径后自然结束。轨迹深挖可轻问是否继续；内容/剧情证据不够则按全局规则升级 web_*（受止损约束）。
"""

AVAILABLE_MODELS = {
    "deepseek-flash": {
        "label": "Flash",
        "hint": "V4.1 · 推荐",
    },
}

_MODEL_ALIASES = {
    "deepseek-v4-flash": "deepseek-flash",
    "deepseek-v4-flash-vision-exp": "deepseek-flash",
    "deepseek-v4-pro": "deepseek-flash",
    "deepseek-chat": "deepseek-flash",
    "deepseek-reasoner": "deepseek-flash",
}

_DEFAULT_MODEL = (os.getenv("DEEPSEEK_MODEL") or "deepseek-flash").strip() or "deepseek-flash"
_DEFAULT_MODEL = _MODEL_ALIASES.get(_DEFAULT_MODEL, _DEFAULT_MODEL)
if _DEFAULT_MODEL not in AVAILABLE_MODELS:
    _DEFAULT_MODEL = "deepseek-flash"

_llm_cache: dict[str, _DeepSeekChat] = {}
_agent_cache: dict = {}


def clear_agent_cache() -> None:
    """LLM/Agent 实例缓存清理。插件名单仍以进程启动时 Registry 为准（改插件请重启）。"""
    _agent_cache.clear()
    _llm_cache.clear()


def normalize_model_id(model_id: str | None) -> str:
    mid = (model_id or "").strip()
    mid = _MODEL_ALIASES.get(mid, mid)
    if mid in AVAILABLE_MODELS:
        return mid
    return _DEFAULT_MODEL


def _cache_key(model_id: str | None, thinking: bool) -> str:
    return f"{normalize_model_id(model_id)}:think={1 if thinking else 0}"


def get_llm(model_id: str | None = None, *, thinking: bool = False) -> _DeepSeekChat:
    mid = normalize_model_id(model_id)
    key = _cache_key(mid, thinking)
    cached = _llm_cache.get(key)
    if cached is not None:
        return cached
    kwargs: dict[str, Any] = {
        "model": mid,
        "api_key": _API_KEY,
        "api_base": "https://api.deepseek.com",
        "extra_body": {"thinking": {"type": "enabled" if thinking else "disabled"}},
    }
    if thinking:
        kwargs["reasoning_effort"] = "high"
        kwargs["max_tokens"] = 8192
    else:
        kwargs["temperature"] = 0.4
    llm_obj = _DeepSeekChat(**kwargs)
    _llm_cache[key] = llm_obj
    return llm_obj


def get_agent(model_id: str | None = None, *, thinking: bool = False):
    mid = normalize_model_id(model_id)
    key = _cache_key(mid, thinking)
    cached = _agent_cache.get(key)
    if cached is not None:
        return cached
    agent_obj = create_agent(
        model=get_llm(mid, thinking=thinking),
        tools=tools,
        system_prompt=system_prompt,
        middleware=[
            ModelCallLimitMiddleware(run_limit=16, exit_behavior="end"),
        ],
        debug=True,
    )
    _agent_cache[key] = agent_obj
    return agent_obj


def model_label(model_id: str | None = None, *, thinking: bool = False) -> str:
    mid = normalize_model_id(model_id)
    label = AVAILABLE_MODELS[mid]["label"]
    return f"{label} · 思考" if thinking else label


llm = get_llm(_DEFAULT_MODEL, thinking=False)
agent = get_agent(_DEFAULT_MODEL, thinking=False)

if __name__ == "__main__":
    from context.chat_cache import set_active_session_key

    print("API Fusion Agent ready. Type q to quit.")
    print("Plugins:", [p.plugin_id for p in _PLUGIN_REG.plugins])
    print("Tools:", len(tools))
    print("Steam configured:", steam_configured())
    print("Bilibili configured:", bilibili_configured())
    print("Feishu configured:", feishu_configured())
    print("Feishu bitable:", feishu_bitable_configured())
    print("Feishu chat whitelist:", feishu_chat_configured())
    print("Feishu health:", feishu_auth_health())
    print("Default model:", _DEFAULT_MODEL)
    set_active_session_key("cli")
    chat_history = []
    while True:
        user_input = input("\nYou: ")
        if user_input.lower() in ["q", "quit", "exit"]:
            break
        reset_tool_budgets()
        chat_history.append({"role": "user", "content": user_input})
        result = agent.invoke({"messages": chat_history})
        print("\nAgent:", result["messages"][-1].content)
        chat_history = result["messages"]
