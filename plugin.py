"""no-more-emoji（麦麦不发emoji了...）：检测出站消息文字里的 emoji，自动替换为表情库里的合适表情。

挂载点：send_service.after_build_message（reply 出站消息构建完成后）
流程：
1. 从出站消息纯文本中检测 Unicode emoji 字符（码点判定，天然排除颜文字）
2. 按 judge_mode 决定是否替换：
   - auto   : 调 LLM 判断（考虑语境、跟风场景、贴切度）
   - always : 检测到直接替换
   - never  : 不替换
3. 替换时从表情库按情绪标签取一张，作为 emoji 消息段 append 到 raw_message 末尾
4. 原文中的 emoji 字符移除，保留文字

token 控制：
- emoji → 情绪标签走本地静态映射表，命中即零 LLM
- 映射未命中且 judge_mode=auto 时，LLM 一次调用同时完成「是否替换」判断 + 情绪标签推荐
- 不拉取全量表情库描述
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from maibot_sdk import HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import HookMode
from pydantic import Field

logger = logging.getLogger("no_more_emoji")

# ─── Unicode emoji 码点范围（粗匹配，覆盖主要区段，颜文字不在此列） ───
_EMOJI_RANGES: List[Tuple[int, int]] = [
    (0x1F300, 0x1FAFF),  # 表情符号、装饰、交通、动物、运动
    (0x1F1E6, 0x1F1FF),  # 区域指示符（旗帜）
    (0x2600, 0x27BF),    # 杂项符号、装饰符号
    (0x2B00, 0x2BFF),    # 箭头/星等杂项
    (0xFE00, 0xFE0F),    # 变体选择符（emoji 修饰）
    (0x1F900, 0x1F9FF),  # 补充符号（已含于 1F300 段，冗余保留）
]
# ZWJ 序列连接符与肤色修饰符
_EMOJI_EXTRA_CODEPOINTS = {0x200D, 0x1F3FB, 0x1F3FC, 0x1F3FD, 0x1F3FE, 0x1F3FF}

# 颜文字常用装饰符黑名单（落在 emoji 码段内但属于纯装饰，非表情）：
# ✦✧✲✳✴✵✶✷✸✹✺✻✼✽✾✿❀❁❂❃❥❦❧ 等
_EMOJI_EXCLUDED_CODEPOINTS = {
    0x2726, 0x2727,  # ✦ ✧ 四角星
    0x2732, 0x2733, 0x2734, 0x2735, 0x2736, 0x2737, 0x2738, 0x2739,
    0x273A, 0x273B, 0x273C, 0x273D, 0x273E,  # ✲✳✴✵✶✷✸✹✺✻✼✽✾ 星形装饰
    0x273F, 0x2740, 0x2741, 0x2742, 0x2743,  # ✿❀❁❂❃ 花饰
    0x2765, 0x2766, 0x2767,  # ❥❦❧ 心形花饰
}

# ─── emoji → 情绪标签本地映射（对齐麦麦表情库情绪标签风格） ───
_EMOJI_TO_EMOTION: Dict[str, str] = {
    "😀": "开心", "😁": "开心", "😂": "开心", "🤣": "开心", "😊": "开心",
    "😄": "开心", "😅": "尴尬", "😆": "开心", "🙂": "开心", "😉": "调皮",
    "😍": "喜欢", "🥰": "喜欢", "😘": "喜欢", "😗": "喜欢", "🤩": "惊讶",
    "😋": "贪吃", "😜": "调皮", "🤪": "调皮", "🤔": "思考", "🤨": "疑惑",
    "😐": "无语", "😑": "无语", "😶": "无语", "😏": "得意", "😒": "嫌弃",
    "🙄": "无语", "😬": "尴尬", "🤥": "说谎", "😌": "舒服", "😔": "难过",
    "😪": "困", "🤤": "贪吃", "😴": "困", "😷": "生病", "🤒": "生病",
    "🤕": "受伤", "🤢": "恶心", "🤮": "恶心", "🥵": "热", "🥶": "冷",
    "😵": "晕", "🤯": "震惊", "🥳": "开心", "😎": "得意", "🥺": "委屈",
    "😢": "哭泣", "😭": "哭泣", "😤": "生气", "😠": "生气", "😡": "生气",
    "🤬": "生气", "😱": "惊讶", "😨": "害怕", "😰": "害怕", "😥": "难过",
    "😓": "难过", "🤗": "拥抱", "🤫": "安静", "🤭": "偷笑", "😳": "害羞",
    "🥴": "晕", "🥱": "困", "😈": "坏笑", "👿": "生气", "💀": "无语",
    "👻": "调皮", "👽": "惊讶", "🤖": "科技", "💩": "恶心", "🔥": "震惊",
    "✨": "开心", "⭐": "开心", "💥": "震惊", "💫": "开心", "🌈": "开心",
    "❤️": "喜欢", "❤": "喜欢", "💔": "难过", "💕": "喜欢", "💖": "喜欢",
    "💗": "喜欢", "💓": "喜欢", "💞": "喜欢", "💢": "生气", "💣": "震惊",
    "💤": "困", "💦": "累", "💨": "快", "💯": "赞同", "👍": "赞同",
    "👎": "嫌弃", "👌": "赞同", "🙏": "感谢", "👏": "赞同", "🙌": "开心",
    "✌️": "胜利", "✌": "胜利", "🤝": "感谢", "👊": "生气", "✊": "生气",
    "💪": "加油", "👋": "再见", "🖕": "生气", "🤟": "开心", "🤘": "开心",
    "🤙": "赞同", "👀": "惊讶", "🧠": "思考", "🗣": "说话", "👄": "喜欢",
    "💋": "喜欢", "🫶": "喜欢", "🫡": "赞同", "🥹": "委屈", "🫠": "无语",
}

# 兜底情绪标签
_FALLBACK_EMOTION = "开心"

# ─── 正则：提取文本中的 emoji 字符序列 ───
def _is_emoji_char(ch: str) -> bool:
    """判断单个字符是否为 emoji（按码点分段，排除颜文字装饰符）。"""
    cp = ord(ch)
    if cp in _EMOJI_EXCLUDED_CODEPOINTS:
        return False
    for start, end in _EMOJI_RANGES:
        if start <= cp <= end:
            return True
    return cp in _EMOJI_EXTRA_CODEPOINTS


def _extract_emoji(text: str) -> List[str]:
    """提取文本中的 emoji 序列列表（每个元素是一段连续 emoji）。

    扫描式实现，与 _is_emoji_char / _remove_emoji 保持同一判定口径。
    """
    if not text:
        return []
    result: List[str] = []
    current: List[str] = []
    for ch in text:
        if _is_emoji_char(ch):
            current.append(ch)
        else:
            if current:
                result.append("".join(current))
                current = []
    if current:
        result.append("".join(current))
    return result


def _remove_emoji(text: str) -> str:
    """从文本中移除所有 emoji 字符。"""
    if not text:
        return text
    return "".join(ch for ch in text if not _is_emoji_char(ch))


def _emotion_for_emoji(seq: str) -> str:
    """取一段 emoji 对应的情绪标签；优先映射表，其次首个字符映射，最后兜底。"""
    if not seq:
        return _FALLBACK_EMOTION
    if seq in _EMOJI_TO_EMOTION:
        return _EMOJI_TO_EMOTION[seq]
    first = seq[0]
    if first in _EMOJI_TO_EMOTION:
        return _EMOJI_TO_EMOTION[first]
    return _FALLBACK_EMOTION


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"

    enabled: bool = Field(
        default=True,
        description="是否启用插件",
        json_schema_extra={"label": "启用"},
    )
    config_version: str = Field(
        default="1.0.0",
        description="配置版本号",
        json_schema_extra={"label": "配置版本"},
    )


class JudgeSectionConfig(PluginConfigBase):
    """替换判断配置。"""

    __ui_label__ = "替换判断"

    judge_mode: str = Field(
        default="auto",
        description="替换判断模式：auto=LLM 判断（考虑语境/跟风），always=检测到即替换，never=禁用替换",
        json_schema_extra={"label": "判断模式"},
    )
    llm_model: str = Field(
        default="emoji",
        description="LLM 判断用的模型任务名",
        json_schema_extra={"label": "LLM 模型"},
    )
    llm_timeout_seconds: int = Field(
        default=20,
        description="LLM 判断超时秒数，超时则跳过替换",
        json_schema_extra={"label": "LLM 超时"},
    )
    context_message_limit: int = Field(
        default=6,
        description="LLM 判断参考的最近聊天消息条数，0 表示不带上下文",
        json_schema_extra={"label": "上下文消息数"},
    )


class ReplacerSectionConfig(PluginConfigBase):
    """替换执行配置。"""

    __ui_label__ = "替换执行"

    append_at_end: bool = Field(
        default=True,
        description="表情包附加在消息末尾；关闭时替换消息原有 emoji 所在位置（当前仅支持末尾）",
        json_schema_extra={"label": "表情放末尾"},
    )
    max_replace_per_message: int = Field(
        default=1,
        description="单条消息最多替换的表情数，0 表示不限制",
        json_schema_extra={"label": "单条上限"},
    )
    skip_if_has_emoji_image: bool = Field(
        default=True,
        description="消息里已含表情包图片段时不重复添加",
        json_schema_extra={"label": "有图不重复"},
    )
    min_text_length: int = Field(
        default=2,
        description="纯文字长度低于此值不处理（避免纯表情刷屏被拦截）",
        json_schema_extra={"label": "最少文字长度"},
    )


class EmojiReplacerConfig(PluginConfigBase):
    """no-more-emoji 配置。"""

    __ui_label__ = "no-more-emoji（麦麦不发emoji了...）"

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    judge: JudgeSectionConfig = Field(default_factory=JudgeSectionConfig)
    replacer: ReplacerSectionConfig = Field(default_factory=ReplacerSectionConfig)


_JUDGE_SYSTEM_PROMPT = """\
你是消息审核助手，只做一件事：判断一段出站消息文字里出现的 emoji 是否应该被替换为表情包图片。

判定规则：
1. emoji 出现得突兀、与文意无关、或数量过多显得廉价 → 应该替换
2. 群聊里大家在跟风刷同一类 emoji、或 emoji 本身就是这轮玩梗的一部分 → 不替换
3. 纯表情回复、消息本身没有实质文字 → 不替换
4. 难以判断时倾向不替换（保守）

只输出 JSON：{"replace": true/false, "emotion": "<建议的情绪标签，replace=false 时可为空>", "reason": "<一句话原因>"}
不要输出其他内容。"""


def _build_judge_prompt(
    message_text: str,
    emojis: List[str],
    context_text: str = "",
) -> str:
    context_block = f"\n近期聊天上下文：\n{context_text}\n" if context_text else ""
    return f"""\
{_JUDGE_SYSTEM_PROMPT}

待审核的出站消息文字：
{message_text}

其中出现的 emoji：{" ".join(emojis)}
{context_block}
请判断是否应将 emoji 替换为表情包图片。"""


def _parse_judge_result(raw: Any) -> Tuple[bool, str]:
    """解析 LLM 判断结果，失败时保守返回不替换。"""
    if isinstance(raw, dict):
        text = str(raw.get("response") or raw.get("content") or "")
    elif isinstance(raw, str):
        text = raw
    else:
        return False, ""
    text = text.strip()
    if not text:
        return False, ""
    # 提取 JSON 块
    try:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            parsed = json.loads(text[start:end + 1])
        else:
            parsed = {}
    except (json.JSONDecodeError, ValueError):
        parsed = {}
    replace = bool(parsed.get("replace"))
    emotion = str(parsed.get("emotion") or "").strip()
    return replace, emotion


class EmojiReplacerPlugin(MaiBotPlugin):
    """检测文字 emoji 并替换为表情包图片的插件。"""

    config_model = EmojiReplacerConfig

    def __init__(self) -> None:
        super().__init__()
        self._recent_messages: Dict[str, List[Dict[str, Any]]] = {}

    async def on_load(self) -> None:
        self.ctx.logger.info("no-more-emoji 已加载")

    async def on_unload(self) -> None:
        self._recent_messages.clear()
        self.ctx.logger.info("no-more-emoji 已卸载")

    async def on_config_update(
        self, scope: str, config_data: dict[str, object], version: str
    ) -> None:
        del scope, config_data, version
        self.ctx.logger.info("no-more-emoji 配置已更新")

    # ─── 工具方法 ───

    def _get_message_text(self, message: Dict[str, Any]) -> str:
        """从消息字典提取纯文本（优先 processed_plain_text，其次拼 raw_message 的 text 段）。"""
        processed = message.get("processed_plain_text")
        if isinstance(processed, str) and processed.strip():
            return processed
        raw = message.get("raw_message")
        if isinstance(raw, list):
            parts: List[str] = []
            for seg in raw:
                if isinstance(seg, dict) and seg.get("type") == "text":
                    data = seg.get("data")
                    if isinstance(data, str):
                        parts.append(data)
            return "".join(parts)
        return ""

    def _has_emoji_component(self, message: Dict[str, Any]) -> bool:
        """消息里是否已含 emoji 段。"""
        raw = message.get("raw_message")
        if not isinstance(raw, list):
            return False
        return any(
            isinstance(seg, dict) and seg.get("type") == "emoji"
            for seg in raw
        )

    async def _fetch_recent_context(self, stream_id: str) -> str:
        """取最近几条聊天文本做 LLM 判断上下文。失败返回空串。"""
        limit = self.config.judge.context_message_limit
        if not limit:
            return ""
        try:
            messages = await self.ctx.message.get_recent(
                stream_id=stream_id, limit=limit
            )
        except Exception as exc:
            logger.debug(f"[no-more-emoji] 获取上下文失败: {exc}")
            return ""
        if not isinstance(messages, list):
            return ""
        lines: List[str] = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            info = msg.get("message_info") or {}
            user_info = info.get("user_info") or {}
            nickname = user_info.get("user_nickname") or "未知"
            text = ""
            plain = msg.get("processed_plain_text")
            if isinstance(plain, str) and plain.strip():
                text = plain.strip()[:200]
            elif isinstance(msg.get("raw_message"), list):
                raw = msg["raw_message"]
                text = "".join(
                    str(seg.get("data", ""))
                    for seg in raw
                    if isinstance(seg, dict) and seg.get("type") == "text"
                )[:200]
            if text:
                lines.append(f"{nickname}: {text}")
        return "\n".join(lines[-limit:])

    async def _llm_judge(
        self, message_text: str, emojis: List[str], stream_id: str
    ) -> Tuple[bool, str]:
        """LLM 判断是否替换；返回 (是否替换, 建议情绪标签)。失败保守返回不替换。"""
        context_text = await self._fetch_recent_context(stream_id)
        prompt = _build_judge_prompt(message_text, emojis, context_text)
        try:
            result = await asyncio.wait_for(
                self.ctx.llm.generate(
                    prompt=prompt,
                    model=self.config.judge.llm_model,
                ),
                timeout=self.config.judge.llm_timeout_seconds,
            )
        except Exception as exc:
            logger.warning(f"[no-more-emoji] LLM 判断失败，跳过替换: {exc}")
            return False, ""
        return _parse_judge_result(result)

    async def _fetch_emoji_base64(self, emotion: str) -> Optional[str]:
        """按情绪标签从表情库取一张表情，返回 base64 或 None（失败时可轻微重试）。"""
        tag = emotion.strip()
        if not tag:
            tag = _FALLBACK_EMOTION
        for attempt in range(2):
            try:
                result = await self.ctx.emoji.get_by_description(tag)
                if isinstance(result, dict):
                    base64_data = str(result.get("base64") or "")
                    if base64_data:
                        return base64_data
            except Exception as exc:
                logger.debug(f"[no-more-emoji] get_by_description('{tag}') 第{attempt+1}次异常: {exc}")
        return None

    async def _pick_emotion(self, emojis: List[str], llm_emotion: str) -> str:
        """确定最终情绪标签：LLM 推荐优先，其次本地映射，最后兜底。"""
        if llm_emotion:
            return llm_emotion
        for seq in emojis:
            emotion = _emotion_for_emoji(seq)
            if emotion != _FALLBACK_EMOTION:
                return emotion
        if emojis:
            return _emotion_for_emoji(emojis[0])
        return _FALLBACK_EMOTION

    # ─── Hook ───

    @HookHandler(
        hook="send_service.after_build_message",
        name="no_more_emoji_hook",
        description="检测出站消息文字里的 emoji，判断后从表情库选取表情附加到消息末尾",
        mode=HookMode.BLOCKING,
    )
    async def handle_outbound_message(self, **kwargs: Any) -> Dict[str, Any]:
        try:
            if not self.config.plugin.enabled:
                return {"modified_kwargs": kwargs}
        except RuntimeError:
            return {"modified_kwargs": kwargs}

        judge_mode = self.config.judge.judge_mode
        if judge_mode == "never":
            return {"modified_kwargs": kwargs}

        message = kwargs.get("message")
        if not isinstance(message, dict):
            return {"modified_kwargs": kwargs}

        # 已含表情包段且配置为不重复，跳过
        if self.config.replacer.skip_if_has_emoji_image and self._has_emoji_component(message):
            return {"modified_kwargs": kwargs}

        message_text = self._get_message_text(message)
        text_no_emoji = _remove_emoji(message_text)
        plain_len = len(text_no_emoji.strip())
        if plain_len < self.config.replacer.min_text_length:
            return {"modified_kwargs": kwargs}

        emojis = _extract_emoji(message_text)
        if not emojis:
            return {"modified_kwargs": kwargs}

        # 单条替换上限
        max_replace = self.config.replacer.max_replace_per_message
        if max_replace > 0:
            emojis = emojis[:max_replace]

        # 判断是否替换
        replace = True
        llm_emotion = ""
        if judge_mode == "auto":
            replace, llm_emotion = await self._llm_judge(
                message_text, emojis, kwargs.get("stream_id") or ""
            )
            if not replace:
                logger.info(
                    f"[no-more-emoji] LLM 判定不替换: {emojis} in {message_text[:60]}"
                )
                return {"modified_kwargs": kwargs}
        elif judge_mode != "always":
            return {"modified_kwargs": kwargs}

        # 取情绪标签并查表情
        emotion = await self._pick_emotion(emojis, llm_emotion)
        emoji_base64 = await self._fetch_emoji_base64(emotion)
        if not emoji_base64:
            logger.warning(
                f"[no-more-emoji] 表情库未取到 '{emotion}' 的表情，跳过"
            )
            return {"modified_kwargs": kwargs}

        # 改写消息：移除 emoji 字符 + 末尾追加 emoji 段
        mutated = dict(message)
        raw = mutated.get("raw_message")
        if isinstance(raw, list):
            new_raw: List[Dict[str, Any]] = []
            for seg in raw:
                if isinstance(seg, dict) and seg.get("type") == "text":
                    seg_data = seg.get("data")
                    if isinstance(seg_data, str):
                        cleaned = _remove_emoji(seg_data)
                        if cleaned:
                            new_raw.append({"type": "text", "data": cleaned})
                        continue
                new_raw.append(seg)
            if self.config.replacer.append_at_end:
                new_raw.append(
                    {
                        "type": "emoji",
                        "data": emotion,
                        "binary_data_base64": emoji_base64,
                    }
                )
            mutated["raw_message"] = new_raw

        # 同步更新 processed_plain_text（去掉 emoji 字符，保留文字）
        if isinstance(mutated.get("processed_plain_text"), str):
            mutated["processed_plain_text"] = text_no_emoji

        logger.info(
            f"[no-more-emoji] 替换完成: {emojis} → 表情[{emotion}], "
            f"stream={kwargs.get('stream_id') or ''}"
        )
        return {"modified_kwargs": {**kwargs, "message": mutated}}


def create_plugin() -> EmojiReplacerPlugin:
    """插件工厂函数，由 SDK Runner 调用。"""
    return EmojiReplacerPlugin()