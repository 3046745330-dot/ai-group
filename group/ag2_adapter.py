"""AG2（原 AutoGen）可选适配层。

题目第 1 条点名了 AG2 等多智能体框架。本层的作用是：

* **探测**：AG2 装没装、版本多少，随时可在 ``/api/health`` 与界面右上角看到；
* **桥接**：装上 AG2 时，把本项目 config 里的成员转换成 AG2 的 ``ConversableAgent``，
  并用 AG2 原生 ``GroupChat`` + ``GroupChatManager`` 跑一版**对照实现**；
* **兜底**：没装 AG2 时，``describe()`` 给出明确的降级说明，全部功能由
  ``group/engine.py`` 的自研内核提供，系统照常可用、主流程不受任何影响。

这样做的理由（工程事实，不是偏好）：AG2 的 GroupChat 是「按 speaker_selection_method
轮流发言」，而本项目要的是「@ 谁谁来」的显式派发 + 逐字流式上屏 + 单轮额度上限，
后者在自研内核里更好控制、也更容易加约束。AG2 因此作为参考实现保留，
而不是当作唯一内核。
"""

from __future__ import annotations

import importlib
from typing import Any

try:  # 兼容 AG2 与旧版 autogen 两种包名
    _ag2 = importlib.import_module("autogen")
    AG2_PACKAGE = "autogen (AG2)"
except ImportError:
    try:
        _ag2 = importlib.import_module("ag2")
        AG2_PACKAGE = "ag2"
    except ImportError:
        _ag2 = None
        AG2_PACKAGE = ""

AG2_AVAILABLE = _ag2 is not None

_ACTIVE_KERNEL = "group.engine.GroupEngine"


def describe() -> dict[str, Any]:
    """适配层状态：无论 AG2 装没装都返回，调用方无需 try/except。"""
    if not AG2_AVAILABLE:
        return {
            "ag2": False,
            "package": "",
            "version": "",
            "kernel": _ACTIVE_KERNEL,
            "fallback": _ACTIVE_KERNEL,
            "detail": (
                "未安装 AG2：当前使用自研群聊内核 group.engine（@派发 / 流式 / 额度护栏功能完整），"
                "主流程不受影响。想要 GroupChat 对照实现：pip install ag2"
            ),
        }
    return {
        "ag2": True,
        "package": AG2_PACKAGE,
        "version": getattr(_ag2, "__version__", "unknown"),
        "kernel": _ACTIVE_KERNEL,
        "fallback": _ACTIVE_KERNEL,
        "detail": (
            f"AG2 已就绪（{AG2_PACKAGE} {getattr(_ag2, '__version__', 'unknown')}）："
            "可用 build_ag2_group(cfg, client) 生成 GroupChat 对照实现。"
        ),
    }


def build_llm_config(cfg, client, spec) -> dict[str, Any]:
    """把一个成员编译成 AG2 的 ``llm_config``（不依赖 AG2，可单独测试）。

    DeepSeek / Kimi / 豆包 都是 OpenAI 兼容协议，因此同一个写法通吃：
    只有 ``base_url`` 不同，密钥一律来自 ``config.resolve_credential``。
    """
    target = client.resolve(spec.provider, spec.model)
    return {
        "config_list": [
            {
                "model": target.model,
                "base_url": target.provider.base_url,
                "api_key": target.api_key,
                "api_type": "openai",
            }
        ],
        "temperature": spec.temperature,
        "max_tokens": spec.max_tokens,
        "cache_seed": None,
    }


def build_ag2_group(cfg, client, *, max_round: int = 8):
    """把本项目配置转换成 AG2 的 GroupChat。

    返回 ``(agents, group_chat, manager)``；AG2 未安装时抛 RuntimeError（带明确说明）。
    """
    if not AG2_AVAILABLE:
        raise RuntimeError(
            "AG2 未安装，无法生成 GroupChat 对照实现。"
            "请执行 `pip install ag2`（或 `pip install autogen`）后重试；"
            "当前主流程由自研内核 group.engine 提供，不受此影响。"
        )

    # 注意：用探测到的那个模块，而不是硬编码 `import autogen`——
    # 新版包名是 ag2，硬编码会在只装了 ag2 的机器上失败。
    ConversableAgent = getattr(_ag2, "ConversableAgent")
    GroupChat = getattr(_ag2, "GroupChat")
    GroupChatManager = getattr(_ag2, "GroupChatManager")

    agents = []
    for spec in cfg.agents:
        if not spec.enabled:
            continue
        agent = ConversableAgent(
            name=spec.id,
            system_message=spec.system_prompt,
            llm_config=build_llm_config(cfg, client, spec),
            human_input_mode="NEVER",
            max_consecutive_auto_reply=spec.max_turns_per_run,
            code_execution_config=False,
            description=f"{spec.display_name}（{spec.route}）",
        )
        agents.append(agent)

    if not agents:
        raise RuntimeError("没有启用任何成员，无法构建 AG2 群聊。")

    group_chat = GroupChat(
        agents=agents,
        messages=[],
        max_round=max_round,
        speaker_selection_method="auto",
        allow_repeat_speaker=False,
    )
    manager = GroupChatManager(groupchat=group_chat, llm_config=agents[0].llm_config)
    return agents, group_chat, manager
