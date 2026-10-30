# 主动敲门 (Proactive Ping)

> 让 MaiBot 在冷场的时候「想一想」要不要主动找你 —— 说什么、说不说，都由她自己的 planner 决定。

一个 MaiBot 插件：**不调用模型、不直接发消息**，只在合适的时机往聊天流里投递一条「主动任务」，剩下的交给人格与记忆。

---

## 它解决什么问题

普通定时机器人：到点 → 发一句预设文案 → 机械、重复、容易被看穿。

主动敲门：到点 → **提醒 planner「现在是 X 点，你们 30 分钟没说话了」** → planner 结合聊天记录与长期记忆，自己决定：

- 说一句自然的开场（今天做了什么、想起你说过的事）
- 或者调用 `stop()` —— **选择不打扰**

「有权拒绝开口」是它和定时播报最大的区别。

---

## 工作机制

```
每 15 分钟探测一次
   ↓
① 静默时段？(00:30~08:00)          → 跳过
② 今日次数到上限？                  → 跳过
③ 距离上次主动不足冷却时间？        → 跳过
④ 找聊天流（配置指定 / 自动发现）    → 找不到则提示
⑤ 距最后一条消息不足空闲阈值？      → 继续等
   ↓
投递 proactive 任务（带提示词）
   ↓
Planner 读上下文 + 记忆 → reply（开口）或 stop()（不说话）
   ↓
成功投递才计入当日次数（每日上限，默认 3）
```

状态写在插件目录的 `state.json`（日期、当日次数、上次触发时间），重启后保留。

---

## 安装

```bash
cd /path/to/MaiBot/plugins
git clone https://github.com/zzy-fxxxexxxyxxx/maibot-plugin-proactive-ping proactive_ping
cp proactive_ping/config.example.toml proactive_ping/config.toml
```

然后重启 MaiBot（或等插件热重载）。日志里应看到：

```
主动敲门已加载：平台=qq 探测间隔=15分钟 空闲阈值=20分钟 冷却=20分钟 每日上限=3 静默=00:30~08:00
主动敲门：探测循环已启动，首次探测在 60 秒后
```

---

## 配置

### 方式一：WebUI（推荐）

**插件管理 → 主动敲门 → 配置**，分三个分区：

| 分区 | 字段 |
|---|---|
| 基础 | `enabled` / `platform` / `stream_id` |
| 时间 | `check_interval_minutes` / `idle_minutes` / `cooldown_minutes` / `daily_max` / `quiet_start` / `quiet_end` / `startup_delay_seconds` |
| 提示词 | `intent_template`（14 行文本框，支持占位符） |

改完自动热重载，无需重启。

### 方式二：直接改 `config.toml`

### 字段说明

| 字段 | 默认 | 说明 |
|---|---|---|
| `platform` | `qq` | `qq` = QQ 私聊；`internal` = WebUI 内置聊天室 |
| `stream_id` | 空 | 留空自动取该平台第一个私聊流；多账号时建议填死 |
| `check_interval_minutes` | 15 | 探测间隔 |
| `idle_minutes` | 20 | 冷场多久才考虑开口 |
| `cooldown_minutes` | 20 | 两次主动的最短间隔 |
| `daily_max` | 3 | 每日上限，0 = 不限 |
| `quiet_start` / `quiet_end` | 00:30 / 08:00 | 静默时段，支持跨午夜 |
| `startup_delay_seconds` | 60 | 启动后延迟多久开始探测 |
| `intent_template` | 见下 | 投给 planner 的提示词 |

### 提示词占位符

| 占位符 | 含义 |
|---|---|
| `{now}` | 当前时间（`2026-10-30 22:15`） |
| `{idle_minutes}` | 距上次消息多少分钟 |
| `{count}` | 今天已主动几次 |

默认提示词（可在 WebUI 里直接改）：

```
【主动提醒】现在是 {now}。你们上一次说话是 {idle_minutes} 分钟前，
那轮对话已经过去了，不要把它当成刚刚发生的对话来接。
- 如果要提起之前的事，必须带上时间感（比如「之前你说…」），不要无缝接着讲。
- 更好的开场是：说说你此刻在做什么、想到什么，或者问问他现在在做什么。
  你可以有自己的当下（在发呆、在看视频、刚吃了什么），这些随你怎么说都行。
- 但不要编造关于「他」的事实：他没说过的话、没做过的事、不存在的约定，都不要替他补。
- 不要重复你最近说过的话或意思。
- 觉得时机不对就调用 stop()，不要勉强开口。
```

设计要点：
- **「那轮对话已经过去了」** —— 避免她隔一小时再开口时像一条迟到的回复。
- **允许虚构自己的当下** —— 角色扮演需要活人感。
- **禁止虚构「用户」的事实** —— 那会污染关系认知。

---

## 常见问题

**Q：日志说「平台 qq 下未发现任何私聊流」**
A：先在 QQ 上给机器人发一条消息，让 `chat_sessions` 里有会话记录；或在 `stream_id` 里填死。

**Q：发现多个私聊流，抓到错的那个**
A：日志会打印候选：

```
主动敲门：平台 qq 发现 2 个私聊流，使用第一个；如需固定请把对应 ID 填入配置 stream_id：
  1) 4297a11a…  user=…  nickname=…  account=2129736576
```

把完整 ID 填进 `stream_id` 即可。

**Q：她老是重复之前说过的话**
A：多半是 embedding（语义检索）没开/不可用，她能拿到的上下文变窄。检查 `[a_memorix]` 的 embedding 配置，或把 `idle_minutes` 调小（冷场短了，接旧话题不突兀）。

**Q：QQ 适配器加载失败 `bootstrap 超时 (10000ms)`**
A：这是适配器导入太慢导致的，和本插件无关。预编译一次字节码即可：

```bash
cd MaiBot/plugins/Mai-with-u_qq-adapter
~/path/to/maibot/bin/python -m compileall -q plugin.py
```

---

## 兼容性

- MaiBot ≥ 1.3.18
- maibot-plugin-sdk ≥ 2.20.0
- Python 3.10+

---

## 许可

MIT
