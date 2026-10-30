"""主动敲门（proactive-ping）

按「空闲时长」探测聊天流，达到阈值时向 MaiBot 的 planner 投递一条主动任务，
由 planner 结合上下文与长期记忆，自行决定要不要开口、说什么。

设计原则：
- 插件不调 LLM、不直接发消息，只说一句「该想想了」；
- 探测频率、空闲阈值、冷却、每日上限、静默时段、提示词全部可在 WebUI 中配置；
- 状态（今日次数 / 上次触发时间）写在插件目录的 state.json，重启后保留。

配置：WebUI → 插件 → 主动敲门
      或 data/plugins/local.proactive-ping/config.toml
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from maibot_sdk import Field, MaiBotPlugin, PluginConfigBase

BASE_DIR = Path(__file__).resolve().parent
STATE_PATH = BASE_DIR / "state.json"

DEFAULT_PROMPT = (
    "【主动提醒】现在是 {now}。你们上一次说话是 {idle_minutes} 分钟前，"
    "那轮对话已经过去了，不要把它当成刚刚发生的对话来接。\n"
    "先看一眼最近的聊天记录和你记得的事，再自己判断要不要主动找他说话：\n"
    "- 如果要提起之前的事，必须带上时间感（比如「之前你说…」「刚才那个…」），"
    "不要无缝接着讲，否则会像一条延迟了很久的消息。\n"
    "- 更好的开场是：说说你此刻在做什么、想到什么，或者问问他现在在做什么。"
    "你可以有自己的当下（在发呆、在看视频、刚吃了什么），这些随你怎么说都行。\n"
    "- 但不要编造关于「他」的事实：他没说过的话、没做过的事、不存在的约定，都不要替他补。\n"
    "- 不要重复你最近说过的话或意思；同一件事不要换个说法再说一遍。\n"
    "- 觉得时机不对（他可能在忙、没什么可说、太晚了）就调用 stop()，不要勉强开口。\n"
    "不要解释这是定时触发，也不要提插件或提醒。今天你已经主动找过他 {count} 次。"
)


# ---------------------------------------------------------------------------
# 配置模型（WebUI 会自动渲染成配置页）
# ---------------------------------------------------------------------------


class BasicSection(PluginConfigBase):
    """基础设置"""

    __ui_label__ = "基础"
    __ui_icon__ = "settings"
    __ui_order__ = 0

    config_version: str = Field(
        default="0.2.0",
        description="配置版本号，由程序维护，请勿手动修改。",
    )
    enabled: bool = Field(
        default=True,
        description="是否启用插件。关闭后不再探测，也不会有任何主动行为。",
    )
    platform: str = Field(
        default="internal",
        description="目标平台：internal = WebUI 内置聊天室；qq = QQ 私聊。接入 QQ 后填 qq。",
    )
    stream_id: str = Field(
        default="",
        description="指定聊天流 ID。留空表示自动取该平台下的第一个私聊聊天流。",
    )


class TimingSection(PluginConfigBase):
    """时间设置"""

    __ui_label__ = "时间"
    __ui_icon__ = "clock"
    __ui_order__ = 10

    check_interval_minutes: int = Field(
        default=15,
        ge=1,
        le=240,
        description="探测间隔（分钟）：每隔多久检查一次要不要提醒。",
    )
    idle_minutes: int = Field(
        default=15,
        ge=1,
        le=1440,
        description="空闲阈值（分钟）：连续多久没有新消息，才考虑主动开口。",
    )
    cooldown_minutes: int = Field(
        default=15,
        ge=0,
        le=1440,
        description="冷却（分钟）：两次主动触发之间至少间隔多久。",
    )
    daily_max: int = Field(
        default=6,
        ge=0,
        le=100,
        description="每日上限：每天最多主动几次；0 表示不限制。",
    )
    quiet_start: str = Field(
        default="00:30",
        description="静默开始时间（HH:MM），此时间段内完全不打扰。",
    )
    quiet_end: str = Field(
        default="08:00",
        description="静默结束时间（HH:MM）。",
    )
    startup_delay_seconds: int = Field(
        default=30,
        ge=0,
        le=600,
        description="启动延迟（秒）：麦麦启动后等多久再开始第一次探测。",
    )


class PromptSection(PluginConfigBase):
    """提示词设置"""

    __ui_label__ = "提示词"
    __ui_icon__ = "message-square"
    __ui_order__ = 20

    intent_template: str = Field(
        default=DEFAULT_PROMPT,
        description=(
            "投递给 planner 的主动任务文本。可用占位符：{now} 当前时间、"
            "{idle_minutes} 空闲分钟数、{count} 今天已主动次数。"
        ),
        json_schema_extra={"x-widget": "textarea", "rows": 14},
    )


class ProactivePingConfig(PluginConfigBase):
    """主动敲门 配置"""

    plugin: BasicSection = Field(default_factory=BasicSection)
    timing: TimingSection = Field(default_factory=TimingSection)
    prompt: PromptSection = Field(default_factory=PromptSection)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def _load_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            with path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
    except Exception:
        pass
    return default


def _save_json(path: Path, data: Any) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _parse_hm(value: Any, fallback: tuple[int, int]) -> int:
    try:
        hour_text, minute_text = str(value).strip().split(":", 1)
        return max(0, min(23, int(hour_text))) * 60 + max(0, min(59, int(minute_text)))
    except Exception:
        return fallback[0] * 60 + fallback[1]


def _coerce_ts(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        try:
            return float(text)
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except Exception:
            return 0.0
    return 0.0


# ---------------------------------------------------------------------------
# 插件主体
# ---------------------------------------------------------------------------


class ProactivePingPlugin(MaiBotPlugin):
    """空闲探测 → 唤醒 planner 的轻量插件。"""

    config_model = ProactivePingConfig

    def __init__(self) -> None:
        super().__init__()
        self._state: dict[str, Any] = {}
        self._task: asyncio.Task[None] | None = None
        self._last_stream_report: Any = None

    # ---------- 生命周期 ----------

    async def on_load(self) -> None:
        self._state = _load_json(STATE_PATH, {}) or {}
        cfg = self.config
        self.ctx.logger.info(
            "主动敲门已加载：平台=%s 探测间隔=%s分钟 空闲阈值=%s分钟 冷却=%s分钟 每日上限=%s 静默=%s~%s",
            cfg.plugin.platform,
            cfg.timing.check_interval_minutes,
            cfg.timing.idle_minutes,
            cfg.timing.cooldown_minutes,
            cfg.timing.daily_max,
            cfg.timing.quiet_start,
            cfg.timing.quiet_end,
        )
        if not cfg.plugin.enabled:
            self.ctx.logger.info("主动敲门已禁用（配置中 enabled=false）")
            return
        self._task = asyncio.create_task(self._loop(), name="proactive-ping-loop")

    async def on_unload(self) -> None:
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.ctx.logger.info("主动敲门已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        """配置热更新：SDK 已把新配置写入 self.config，这里只记日志并重放状态。"""

        del config_data, version
        self.ctx.logger.info("主动敲门：配置已刷新（scope=%s），下轮探测生效", scope)

    # ---------- 主循环 ----------

    async def _loop(self) -> None:
        try:
            delay = max(0.0, float(self.config.timing.startup_delay_seconds))
            self.ctx.logger.info("主动敲门：探测循环已启动，首次探测在 %.0f 秒后", delay)
            await asyncio.sleep(delay)
            while True:
                try:
                    await self._tick()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    self.ctx.logger.warning("主动敲门：探测失败 %s", exc)
                interval = max(60.0, float(self.config.timing.check_interval_minutes) * 60)
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return

    async def _tick(self) -> None:
        cfg = self.config
        now = datetime.now()

        if not cfg.plugin.enabled:
            return
        if self._in_quiet_hours(now):
            return

        today = now.strftime("%Y-%m-%d")
        if self._state.get("date") != today:
            self._state["date"] = today
            self._state["count"] = 0

        if cfg.timing.daily_max > 0 and int(self._state.get("count", 0)) >= cfg.timing.daily_max:
            return

        last_trigger = _coerce_ts(self._state.get("last_trigger_ts", 0))
        if last_trigger and now.timestamp() - last_trigger < cfg.timing.cooldown_minutes * 60:
            return

        stream_id = await self._pick_stream_id()
        if not stream_id:
            return

        idle_min = await self._idle_minutes_since_last_message(stream_id, now)
        threshold = float(cfg.timing.idle_minutes)
        if idle_min is not None and idle_min < threshold:
            self.ctx.logger.info(
                "主动敲门：距上次聊天 %.1f 分钟 < 阈值 %.0f 分钟，继续等", idle_min, threshold
            )
            return

        self.ctx.logger.info(
            "主动敲门：已连续 %.0f 分钟无聊天（阈值 %.0f），唤醒 planner",
            idle_min if idle_min is not None else threshold,
            threshold,
        )
        shown_idle = f"{idle_min:.0f}" if idle_min is not None else f"{threshold:.0f}"
        try:
            intent = cfg.prompt.intent_template.format(
                now=now.strftime("%Y-%m-%d %H:%M"),
                idle_minutes=shown_idle,
                idle_hours=round(threshold / 60, 2),
                count=int(self._state.get("count", 0)),
            )
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("主动敲门：提示词占位符有误，改用默认模板 %s", exc)
            intent = DEFAULT_PROMPT.format(
                now=now.strftime("%Y-%m-%d %H:%M"),
                idle_minutes=shown_idle,
                idle_hours=round(threshold / 60, 2),
                count=int(self._state.get("count", 0)),
            )

        result = await asyncio.wait_for(
            self.ctx.maisaka.proactive.trigger(
                stream_id=stream_id,
                intent=intent,
                reason="idle-proactive-ping",
                priority="low",
                metadata={"plugin": "local.proactive-ping", "triggered_at": now.isoformat()},
            ),
            timeout=30,
        )
        self.ctx.logger.info("主动敲门：已向 planner 投递主动任务 stream=%s result=%s", stream_id, result)

        ok = not (isinstance(result, dict) and result.get("success") is False)
        if ok:
            self._state["count"] = int(self._state.get("count", 0)) + 1
            self._state["last_trigger_ts"] = now.timestamp()
            self._state["last_stream_id"] = stream_id
            _save_json(STATE_PATH, self._state)

    # ---------- 辅助 ----------

    def _in_quiet_hours(self, now: datetime) -> bool:
        cfg = self.config.timing
        start = _parse_hm(cfg.quiet_start, (0, 30))
        end = _parse_hm(cfg.quiet_end, (8, 0))
        if start == end:
            return False
        current = now.hour * 60 + now.minute
        if start < end:
            return start <= current < end
        return current >= start or current < end

    async def _pick_stream_id(self) -> str:
        cfg = self.config.plugin
        configured = str(cfg.stream_id or "").strip()
        if configured:
            return configured
        try:
            response = await asyncio.wait_for(
                self.ctx.chat.get_private_streams(platform=cfg.platform), timeout=20
            )
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("主动敲门：查询聊天流失败 %s", exc)
            return ""
        streams: Any = None
        if isinstance(response, dict):
            streams = response.get("streams")
        elif isinstance(response, list):
            streams = response
        if not streams:
            if self._last_stream_report != "empty":
                self.ctx.logger.warning(
                    "主动敲门：平台 %s 下未发现任何私聊流。请先在该平台的私聊里发一条消息，"
                    "或在配置里手动填写 stream_id（可在其他插件/日志里查到会话 ID）。",
                    cfg.platform,
                )
                self._last_stream_report = "empty"
            return ""

        # 找到流：把候选打印出来，方便填写 stream_id / 排查多账号抓错流的问题
        if self._last_stream_report != len(streams):
            self.ctx.logger.info(
                "主动敲门：平台 %s 发现 %d 个私聊流，使用第一个；如需固定请把对应 ID 填入配置 stream_id：",
                cfg.platform,
                len(streams),
            )
            for index, item in enumerate(streams[:5], start=1):
                if not isinstance(item, dict):
                    continue
                stream_id = str(item.get("stream_id") or item.get("session_id") or "")
                self.ctx.logger.info(
                    "  %d) %s…  user=%s  nickname=%s  account=%s",
                    index,
                    stream_id[:8],
                    str(item.get("user_id") or "")[:16],
                    str(item.get("user_nickname") or "")[:16],
                    str(item.get("account_id") or ""),
                )
            self._last_stream_report = len(streams)

        first = streams[0]
        if isinstance(first, dict):
            return str(first.get("stream_id") or first.get("session_id") or "")
        return ""

    async def _idle_minutes_since_last_message(self, stream_id: str, now: datetime) -> float | None:
        """返回距最后一条消息的分钟数；拿不到任何消息时返回 None。"""

        try:
            response = await asyncio.wait_for(
                self.ctx.message.get_recent(chat_id=stream_id, limit=20), timeout=20
            )
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("主动敲门：读取最近消息失败 %s", exc)
            return None
        messages: Any = None
        if isinstance(response, dict):
            messages = response.get("messages")
        elif isinstance(response, list):
            messages = response
        if not messages:
            return None
        latest = 0.0
        for item in messages:
            if not isinstance(item, dict):
                continue
            for key in ("timestamp", "time", "created_at", "message_time"):
                if key in item:
                    latest = max(latest, _coerce_ts(item[key]))
        if latest <= 0:
            return None
        return max(0.0, (now.timestamp() - latest) / 60.0)


def create_plugin() -> ProactivePingPlugin:
    """SDK 插件入口。"""

    return ProactivePingPlugin()
