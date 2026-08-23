# 麦麦不发emoji了...

检测麦麦出站消息文字里的 emoji，由 LLM 判断是否应该替换，若应替换则从表情包库选取一张合适的表情包图片，放在消息末尾发出。

解决「麦麦说话时结尾/句中硬塞 emoji、用得多且不贴切、显得出戏」的问题。

## 功能

- 检测出站消息纯文本中的 Unicode emoji 字符（码点判定，天然排除颜文字）
- 按 `judge_mode` 判断是否替换：
  - `auto`：调 LLM 判断。考虑语境、贴切度和群内跟风玩梗场景，判断不了时保守不替换
  - `always`：检测到 emoji 直接替换
  - `never`：禁用（仅检测不处理）
- 替换时从表情库按情绪标签取一张表情包，以表情包图片段附加到消息末尾
- 原文中 emoji 字符被移除，文字保留

## 工作原理

挂载 `send_service.after_build_message` hook，在出站消息构建完成后、真正发送前拦截：

1. 提取消息纯文本，检测 emoji 字符
2. `judge_mode=auto` 时调用 LLM 判断（prompt 内嵌规则：突兀/过量 → 替换，跟风玩梗 → 不替换，纯表情 → 不替换，难判断 → 保守）
3. 判定替换后，将 emoji 映射为情绪标签（如 😂 → 开心），从表情库取对应情绪的表情包
4. emoji 段追加到消息 `raw_message` 末尾，作为图片发出；文字里原本的 emoji 字符被移除

## 安装

1. 将插件目录放入 MaiBot 的 `plugins/` 目录（或通过插件市场/后台安装）
2. 重启 MaiBot（或热重载插件）
3. 在 WebUI 插件列表确认「麦麦不发emoji了...」已启用

## 配置

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `plugin.enabled` | `true` | 插件总开关 |
| `judge.judge_mode` | `auto` | `auto`/`always`/`never` |
| `judge.llm_model` | `emoji` | LLM 判断用的模型任务名 |
| `judge.llm_timeout_seconds` | `20` | LLM 判断超时，超时跳过替换 |
| `judge.context_message_limit` | `6` | LLM 判断参考最近消息条数，0 不带上下文 |
| `replacer.append_at_end` | `true` | 表情包放在消息末尾 |
| `replacer.max_replace_per_message` | `1` | 单条消息最多替换表情数，0 不限 |
| `replacer.skip_if_has_emoji_image` | `true` | 消息已含表情包图片段时不重复加 |
| `replacer.min_text_length` | `2` | 纯文字长度低于此值不处理 |

示例 `config.toml`：

```toml
[plugin]
enabled = true
config_version = "1.0.0"

[judge]
judge_mode = "auto"
llm_model = "emoji"
llm_timeout_seconds = 20
context_message_limit = 6

[replacer]
append_at_end = true
max_replace_per_message = 1
skip_if_has_emoji_image = true
min_text_length = 2
```

## token 开销

- emoji → 情绪标签走内置本地映射表（约 150 个常见 emoji），映射命中零 LLM 调用
- 仅 `judge_mode=auto` 时调一次 LLM，一次调用同时完成「是否替换」判断与情绪标签推荐
- 不拉取表情库全量描述，避免反复消耗 token

## 与表情包机制的关系

复用 MaiBot 现有表情库注册体系（`emoji_manager`，`data/emoji/` + 情绪标签/描述索引），不另建库、不干预麦麦主动发表情（如 hsd221 的 emoji-text-selector 等插件）的流程。二者共存互不冲突。

## License

MIT