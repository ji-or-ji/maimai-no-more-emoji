"""no-more-emoji 插件的离线单元测试。

不依赖 MaiBot 运行时：mock 掉 maibot_sdk 与插件 ctx，直接驱动
plugin.py 的纯逻辑与 handle_outbound_message hook，模拟
send_service.after_build_message 触发场景。

运行：
    python -m unittest tests.test_no_more_emoji -v

依赖：pydantic（插件配置模型）
"""

from __future__ import annotations

import json
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

# ─── 在 import plugin 前注入假的 maibot_sdk ───

_fake_sdk = types.ModuleType("maibot_sdk")
_fake_sdk_types = types.ModuleType("maibot_sdk.types")

from pydantic import BaseModel


class _FakePluginConfigBase(BaseModel):
    """mock PluginConfigBase：继承 pydantic BaseModel 即可用 Field 定义字段。"""

    class Config:
        arbitrary_types_allowed = True


class _FakeMaiBotPlugin:
    """mock MaiBotPlugin 基类：仅提供属性占位，不连接任何运行时。"""

    def __init__(self) -> None:
        self.config = None
        self.ctx = None


def _fake_hook_handler(*args: object, **kwargs: object) -> object:
    """mock @HookHandler 装饰器：原样返回函数，不注册到运行时。"""

    def decorator(func):
        return func

    return decorator


_fake_sdk.HookHandler = _fake_hook_handler
_fake_sdk.MaiBotPlugin = _FakeMaiBotPlugin
_fake_sdk.PluginConfigBase = _FakePluginConfigBase
_fake_sdk_types.HookMode = SimpleNamespace(BLOCKING="blocking")

sys.modules["maibot_sdk"] = _fake_sdk
sys.modules["maibot_sdk.types"] = _fake_sdk_types

# ─── 导入被测插件 ───
sys.path.insert(0, "plugins")
import plugin as plugin_mod  # noqa: E402


def _build_plugin(
    *,
    judge_mode: str = "always",
    llm_judge_result: object | None = None,
    llm_judge_error: Exception | None = None,
    emoji_base64: str = "fake-base64-data",
    recent_messages: list | None = None,
    config_overrides: dict | None = None,
) -> plugin_mod.EmojiReplacerPlugin:
    """构造测试用插件实例 + mock ctx，返回 (plugin, ctx)。"""
    plugin = plugin_mod.EmojiReplacerPlugin()

    # 配置
    cfg = plugin_mod.EmojiReplacerConfig()
    if config_overrides:
        for section, values in config_overrides.items():
            sec = getattr(cfg, section)
            for key, value in values.items():
                setattr(sec, key, value)
    cfg.judge.judge_mode = judge_mode
    plugin.config = cfg

    # ctx mock
    llm = AsyncMock()
    if llm_judge_error is not None:
        llm.generate.side_effect = llm_judge_error
    else:
        llm.generate.return_value = {"response": json.dumps(llm_judge_result or {"replace": False})}

    emoji = AsyncMock()
    if emoji_base64 is None:
        emoji.get_by_description.return_value = {}
    else:
        emoji.get_by_description.return_value = {"description": "测试表情", "base64": emoji_base64}

    message = AsyncMock()
    message.get_recent.return_value = recent_messages or []

    logger = MagicMock()

    plugin.ctx = SimpleNamespace(
        llm=llm,
        emoji=emoji,
        message=message,
        logger=logger,
    )
    return plugin, SimpleNamespace(llm=llm, emoji=emoji, message=message, logger=logger)


def _outbound_kwargs(text: str, *, stream_id: str = "g_test", extra_segments: list | None = None) -> dict:
    """构造 send_service.after_build_message 的 kwargs。"""
    raw: list = [{"type": "text", "data": text}]
    if extra_segments:
        raw.extend(extra_segments)
    return {
        "message": {
            "processed_plain_text": text,
            "raw_message": raw,
            "session_id": stream_id,
        },
        "stream_id": stream_id,
    }


class TestPureFunctions(unittest.TestCase):
    """纯函数层：emoji 检测 / 移除 / 情绪映射。"""

    def test_extract_emoji_basic(self) -> None:
        self.assertEqual(plugin_mod._extract_emoji("哈哈😂好"), ["😂"])
        self.assertEqual(plugin_mod._extract_emoji("没有表情"), [])
        self.assertEqual(plugin_mod._extract_emoji(""), [])

    def test_extract_emoji_sequence(self) -> None:
        # 连续 emoji 视为一段
        self.assertEqual(plugin_mod._extract_emoji("😀😁😆"), ["😀😁😆"])

    def test_remove_emoji(self) -> None:
        self.assertEqual(plugin_mod._remove_emoji("哈哈😂好"), "哈哈好")
        self.assertEqual(plugin_mod._remove_emoji("纯文字"), "纯文字")

    def test_caomoji_not_detected(self) -> None:
        """颜文字不应被识别为 emoji。"""
        for text in ["(｡•̀ᴗ-)✧", "Orz", "T^T", "QwQ", "（゜ー゜）", "o(*￣▽￣*)o"]:
            self.assertEqual(plugin_mod._extract_emoji(text), [], f"颜文字误判: {text}")

    def test_emotion_mapping(self) -> None:
        self.assertEqual(plugin_mod._emotion_for_emoji("😂"), "开心")
        self.assertEqual(plugin_mod._emotion_for_emoji("😭"), "哭泣")
        self.assertEqual(plugin_mod._emotion_for_emoji("👍"), "赞同")
        # 未收录 emoji → 兜底
        self.assertEqual(plugin_mod._emotion_for_emoji("🦄"), "开心")

    def test_parse_judge_result(self) -> None:
        ok, emotion = plugin_mod._parse_judge_result({"response": '{"replace": true, "emotion": "开心"}'})
        self.assertTrue(ok)
        self.assertEqual(emotion, "开心")
        # 非法 → 保守不替换
        ok, _ = plugin_mod._parse_judge_result({"response": "不是JSON"})
        self.assertFalse(ok)


class TestHookNever(unittest.IsolatedAsyncioTestCase):
    async def test_never_mode_does_nothing(self) -> None:
        plugin, _ = _build_plugin(judge_mode="never")
        kwargs = _outbound_kwargs("这话有😂表情")
        result = await self._run(plugin, kwargs)
        self.assertEqual(result["modified_kwargs"]["message"]["raw_message"], kwargs["message"]["raw_message"])

    async def _run(self, plugin, kwargs):
        return await plugin.handle_outbound_message(**kwargs)


class TestHookNoEmoji(unittest.IsolatedAsyncioTestCase):
    async def test_no_emoji_passthrough(self) -> None:
        plugin, _ = _build_plugin(judge_mode="always")
        kwargs = _outbound_kwargs("完全不带表情的话")
        result = await plugin.handle_outbound_message(**kwargs)
        self.assertEqual(result["modified_kwargs"]["message"], kwargs["message"])

    async def test_min_text_length_skip(self) -> None:
        plugin, _ = _build_plugin(judge_mode="always")
        # 去掉 emoji 后文字长度 1 < 2
        kwargs = _outbound_kwargs("就😂")
        result = await plugin.handle_outbound_message(**kwargs)
        self.assertEqual(result["modified_kwargs"]["message"], kwargs["message"])


class TestHookAlways(unittest.IsolatedAsyncioTestCase):
    async def test_replace_appends_emoji_at_end(self) -> None:
        plugin, _ = _build_plugin(judge_mode="always", emoji_base64="bm90LXJlYWw=")
        kwargs = _outbound_kwargs("今天好累啊😭要不要休息")
        result = await plugin.handle_outbound_message(**kwargs)

        raw = result["modified_kwargs"]["message"]["raw_message"]
        # emoji 字符被移除，文字保留
        self.assertEqual(raw[0], {"type": "text", "data": "今天好累啊要不要休息"})
        # 末尾追加 emoji 段
        self.assertEqual(raw[-1]["type"], "emoji")
        self.assertIn("binary_data_base64", raw[-1])
        self.assertEqual(raw[-1]["binary_data_base64"], "bm90LXJlYWw=")
        # processed_plain_text 同步去 emoji
        self.assertEqual(result["modified_kwargs"]["message"]["processed_plain_text"], "今天好累啊要不要休息")

    async def test_emotion_uses_local_mapping(self) -> None:
        plugin, ctx = _build_plugin(judge_mode="always")
        kwargs = _outbound_kwargs("这个太好笑了😂")
        await plugin.handle_outbound_message(**kwargs)
        # always 模式不调 LLM
        ctx.llm.generate.assert_not_awaited()
        # 取图用本地映射的「开心」
        ctx.emoji.get_by_description.assert_awaited_with("开心")

    async def test_already_has_emoji_segment_skip(self) -> None:
        plugin, _ = _build_plugin(judge_mode="always")
        extra = [{"type": "emoji", "data": "已有表情", "binary_data_base64": "xxx"}]
        kwargs = _outbound_kwargs("有带图了😂", extra_segments=extra)
        result = await plugin.handle_outbound_message(**kwargs)
        self.assertEqual(result["modified_kwargs"]["message"], kwargs["message"])

    async def test_emoji_missing_from_library_skip(self) -> None:
        plugin, _ = _build_plugin(judge_mode="always", emoji_base64=None)
        kwargs = _outbound_kwargs("库查不到😂")
        result = await plugin.handle_outbound_message(**kwargs)
        self.assertEqual(result["modified_kwargs"]["message"], kwargs["message"])


class TestHookAuto(unittest.IsolatedAsyncioTestCase):
    async def test_llm_judges_replace(self) -> None:
        plugin, ctx = _build_plugin(
            judge_mode="auto",
            llm_judge_result={"replace": True, "emotion": "哭泣"},
        )
        kwargs = _outbound_kwargs("这话带😭有点出戏")
        result = await plugin.handle_outbound_message(**kwargs)
        raw = result["modified_kwargs"]["message"]["raw_message"]
        self.assertEqual(raw[-1]["type"], "emoji")
        self.assertEqual(raw[-1]["data"], "哭泣")  # LLM 推荐情绪
        ctx.llm.generate.assert_awaited_once()

    async def test_llm_judges_no_replace_following(self) -> None:
        """跟风场景：LLM 判定不替换 → 消息原样通过。"""
        plugin, _ = _build_plugin(
            judge_mode="auto",
            llm_judge_result={"replace": False, "emotion": "", "reason": "群里在跟风刷😂"},
        )
        kwargs = _outbound_kwargs("跟大家一起😂😂😂")
        result = await plugin.handle_outbound_message(**kwargs)
        self.assertEqual(result["modified_kwargs"]["message"], kwargs["message"])

    async def test_llm_error_conservative(self) -> None:
        """LLM 调用失败 → 保守不替换。"""
        plugin, _ = _build_plugin(
            judge_mode="auto",
            llm_judge_error=RuntimeError("llm down"),
        )
        kwargs = _outbound_kwargs("出bug了😂")
        result = await plugin.handle_outbound_message(**kwargs)
        self.assertEqual(result["modified_kwargs"]["message"], kwargs["message"])

    async def test_llm_gets_context(self) -> None:
        """auto 模式应把近期聊天喂给 LLM。"""
        recent = [
            {
                "message_info": {"user_info": {"user_nickname": "小明"}},
                "processed_plain_text": "今天天气真不错",
            }
        ]
        plugin, ctx = _build_plugin(
            judge_mode="auto",
            llm_judge_result={"replace": False},
            recent_messages=recent,
        )
        kwargs = _outbound_kwargs("同意！😄")
        await plugin.handle_outbound_message(**kwargs)
        # 确认 generate 收到含上下文的 prompt
        call_kwargs = ctx.llm.generate.await_args.kwargs
        self.assertIn("小明: 今天天气真不错", call_kwargs["prompt"])


class TestPromptBuild(unittest.TestCase):
    def test_system_prompt_merged(self) -> None:
        prompt = plugin_mod._build_judge_prompt("测试消息😂", ["😂"], "")
        self.assertIn("跟风刷", prompt)  # system prompt 规则已并入
        self.assertIn("测试消息", prompt)
        self.assertIn("😂", prompt)


if __name__ == "__main__":
    unittest.main()