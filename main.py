
from __future__ import annotations

import asyncio
import json
import os
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, time
from typing import Dict, List, Optional, Deque, Tuple
from collections import defaultdict, deque

import astrbot.api.message_components as Comp
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api import AstrBotConfig  # per docs: from astrbot.api import AstrBotConfig

# 工具函数
def _ensure_dir(p: str):
    os.makedirs(p, exist_ok=True)
    return p

def _now_tz(tz_name: str | None) -> datetime:
    try:
        if tz_name:
            import zoneinfo
            return datetime.now(zoneinfo.ZoneInfo(tz_name))
    except Exception:
        pass
    return datetime.now()

def _parse_hhmm(s: str) -> Optional[Tuple[int,int]]:
    if not s:
        return None
    m = re.match(r"^([01]?\d|2[0-3]):([0-5]\d)$", s.strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))

def _in_quiet(now: datetime, quiet: str) -> bool:
    if not quiet or "-" not in quiet:
        return False
    a, b = quiet.split("-", 1)
    p1 = _parse_hhmm(a); p2 = _parse_hhmm(b)
    if not p1 or not p2: return False
    t1 = time(p1[0], p1[1]); t2 = time(p2[0], p2[1])
    nt = now.time()
    if t1 <= t2:
        return t1 <= nt <= t2
    else:
        return nt >= t1 or nt <= t2

def _fmt_now(fmt: str, tz: str | None) -> str:
    return _now_tz(tz).strftime(fmt)

# 数据结构定义
@dataclass
class SessionState:
    last_ts: float = 0.0
    history: Deque[Dict] = field(default_factory=lambda: deque(maxlen=32))
    subscribed: bool = False
    last_fired_tag: str = ""
    last_user_reply_ts: float = 0.0  # 用户最后回复时间戳
    consecutive_no_reply_count: int = 0  # 连续无回复次数

@dataclass
class Reminder:
    id: str
    umo: str
    content: str
    at: str           # "YYYY-MM-DD HH:MM" 或 "HH:MM|daily"
    created_at: float

# 主插件
@register("AIReplay", "LumineStory", "定时/间隔主动续聊 + 人格 + 历史 + 免打扰 + 提醒", "1.0.3", "https://github.com/oyxning/astrbot_plugin_AIReplay")
class AIReplay(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg: AstrBotConfig = config
        self._loop_task: Optional[asyncio.Task] = None
        self._states: Dict[str, SessionState] = {}
        self._reminders: Dict[str, Reminder] = {}

        root = os.getcwd()
        self._data_dir = _ensure_dir(os.path.join(root, "data", "plugin_data", "astrbot_plugin_aireplay"))
        self._state_path = os.path.join(self._data_dir, "state.json")
        self._remind_path = os.path.join(self._data_dir, "reminders.json")
        self._load_states()
        self._load_reminders()

        self._loop_task = asyncio.create_task(self._scheduler_loop())
        logger.info("[AIReplay] scheduler started.")

    # 数据持久化
    def _load_states(self):
        if os.path.exists(self._state_path):
            try:
                d = json.load(open(self._state_path, "r", encoding="utf-8"))
                for umo, st in d.get("states", {}).items():
                    # 恢复历史记录
                    history = deque(maxlen=32)
                    if "history" in st:
                        for h in st["history"]:
                            history.append(h)
                    
                    s = SessionState(
                        last_ts=st.get("last_ts", 0.0),
                        history=history,
                        subscribed=st.get("subscribed", False),
                        last_fired_tag=st.get("last_fired_tag", ""),
                        last_user_reply_ts=st.get("last_user_reply_ts", 0.0),
                        consecutive_no_reply_count=st.get("consecutive_no_reply_count", 0),
                    )
                    self._states[umo] = s
            except Exception as e:
                logger.error(f"[AIReplay] load states error: {e}")

    def _save_states(self):
        try:
            dump = {
                "states": {
                    k: {
                        "last_ts": v.last_ts,
                        "history": list(v.history),  # 保存历史记录
                        "subscribed": v.subscribed,
                        "last_fired_tag": v.last_fired_tag,
                        "last_user_reply_ts": v.last_user_reply_ts,
                        "consecutive_no_reply_count": v.consecutive_no_reply_count
                    } for k, v in self._states.items()
                }
            }
            json.dump(dump, open(self._state_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[AIReplay] save states error: {e}")

    def _load_reminders(self):
        if os.path.exists(self._remind_path):
            try:
                arr = json.load(open(self._remind_path, "r", encoding="utf-8"))
                for it in arr:
                    r = Reminder(**it)
                    self._reminders[r.id] = r
            except Exception as e:
                logger.error(f"[AIReplay] load reminders error: {e}")

    def _save_reminders(self):
        try:
            arr = [r.__dict__ for r in self._reminders.values()]
            json.dump(arr, open(self._remind_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[AIReplay] save reminders error: {e}")

<<<<<<< ours
<<<<<<< HEAD
    # Build chat contexts from persisted conversation history with plugin fallback.
    def _collect_contexts(self, conversation, st: Optional[SessionState], hist_n: int) -> List[Dict[str, str]]:
        contexts: List[Dict[str, str]] = []
        raw_sources = []
        if conversation:
            raw_sources.extend([
                getattr(conversation, "history", None),
                getattr(conversation, "messages", None),
            ])
        for raw in raw_sources:
            if not raw:
                continue
            contexts = self._normalize_history(raw)
            if contexts:
                break
        if not contexts and st and hist_n > 0:
            contexts = list(st.history)
        if hist_n > 0 and contexts:
            contexts = contexts[-hist_n:]
        return contexts

    @staticmethod
    def _normalize_history(raw: Any) -> List[Dict[str, str]]:
        data = raw
        contexts: List[Dict[str, str]] = []
        if isinstance(raw, str):
            try:
                data = json.loads(raw)
            except Exception:
                data = None
        if isinstance(data, (list, tuple)):
            for item in data:
                normalized = AIReplay._normalize_history_item(item)
                if normalized:
                    contexts.append(normalized)
        elif isinstance(data, dict):
            normalized = AIReplay._normalize_history_item(data)
            if normalized:
                contexts.append(normalized)
        return contexts

    @staticmethod
    def _normalize_history_item(item: Any) -> Optional[Dict[str, str]]:
        def normalize_role(role_value: Any) -> Optional[str]:
            if isinstance(role_value, str):
                v = role_value.lower()
                mapping = {
                    "assistant": "assistant",
                    "bot": "assistant",
                    "ai": "assistant",
                    "model": "assistant",
                    "system": "system",
                    "user": "user",
                    "human": "user"
                }
                if v in mapping:
                    return mapping[v]
                return v
            return None

        def extract_content(value: Any) -> str:
            if isinstance(value, str):
                return value.strip()
            if isinstance(value, (list, tuple)):
                parts: List[str] = []
                for seg in value:
                    if isinstance(seg, str):
                        parts.append(seg)
                    elif isinstance(seg, dict):
                        for key in ("text", "content", "value"):
                            val = seg.get(key)
                            if isinstance(val, str) and val.strip():
                                parts.append(val.strip())
                                break
                return "\n".join(p for p in parts if p)
            if isinstance(value, dict):
                for key in ("text", "content", "value"):
                    val = value.get(key)
                    if isinstance(val, str) and val.strip():
                        return val.strip()
            if value is None:
                return ""
            return str(value).strip()

        role: Optional[str] = None
        content: str = ""

        if isinstance(item, dict):
            role = normalize_role(item.get("role") or item.get("speaker") or item.get("type") or item.get("sender"))
            content = extract_content(item.get("content"))
            if not content:
                for key in ("text", "message", "value"):
                    candidate = item.get(key)
                    content = extract_content(candidate)
                    if content:
                        break
        elif hasattr(item, "role") and hasattr(item, "content"):
            role = normalize_role(getattr(item, "role"))
            content = extract_content(getattr(item, "content"))
        elif isinstance(item, str):
            role = "user"
            content = item.strip()

        if not role:
            role = "user"
        content = content.strip()
        if not content:
            return None
        if role not in ("user", "assistant", "system"):
            role = "user"
        return {"role": role, "content": content}

    @staticmethod
    def _extract_prompt(source: Any) -> str:
        if not source:
            return ""
        if isinstance(source, str):
            return source.strip()
        if isinstance(source, dict):
            for key in ("system_prompt", "prompt", "content", "text"):
                val = source.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
        for key in ("system_prompt", "prompt", "content", "text"):
            val = getattr(source, key, None)
            if isinstance(val, str) and val.strip():
                return val.strip()
        return ""

=======
    # 消息处理
>>>>>>> 309f8b84aca897dd369efecd327e8cfaccc62b2b
=======
>>>>>>> theirs
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def _on_any_message(self, event: AstrMessageEvent):
        umo = event.unified_msg_origin
        if umo not in self._states:
            self._states[umo] = SessionState()
        st = self._states[umo]
        now_ts = _now_tz(self.cfg.get("timezone") or None).timestamp()
        st.last_ts = now_ts
        st.last_user_reply_ts = now_ts  # 记录用户最后回复时间
        st.consecutive_no_reply_count = 0  # 重置无回复计数

        if (self.cfg.get("subscribe_mode") or "manual") == "auto":
            st.subscribed = True

        try:
            role = "user"
            content = event.message_str or ""
            if content:
                st.history.append({"role": role, "content": content})
        except Exception:
            pass

        self._save_states()

    # QQ命令处理
    @filter.command("aireplay")
    async def _cmd_aireplay(self, event: AstrMessageEvent):
        text = (event.message_str or "").strip()
        lower = text.lower()

        def reply(msg: str):
            return event.plain_result(msg)

        if "help" in lower or text.strip() == "/aireplay":
            yield reply(self._help_text())
            return

        if " debug" in lower:
            # 调试信息
            debug_info = []
            debug_info.append(f"插件启用状态: {self.cfg.get('enable', True)}")
            debug_info.append(f"订阅模式: {self.cfg.get('subscribe_mode', 'manual')}")
            debug_info.append(f"订阅用户数: {len([s for s in self._states.values() if s.subscribed])}")
            debug_info.append(f"当前用户: {event.unified_msg_origin}")
            umo = event.unified_msg_origin
            if umo not in self._states:
                self._states[umo] = SessionState()
            debug_info.append(f"用户订阅状态: {self._states[umo].subscribed}")
            debug_info.append(f"间隔触发设置: {self.cfg.get('after_last_msg_minutes', 0)}分钟")
            debug_info.append(f"免打扰时间: {self.cfg.get('quiet_hours', '')}")
            debug_info.append(f"最大无回复天数: {self.cfg.get('max_no_reply_days', 0)}")
            yield reply("🔍 调试信息:\n" + "\n".join(debug_info))
            return

        if " on" in lower:
            self.cfg["enable"] = True
            self.cfg.save_config()
            yield reply("✅ 已启用 AIReplay")
            return
        if " off" in lower:
            self.cfg["enable"] = False
            self.cfg.save_config()
            yield reply("🛑 已停用 AIReplay")
            return

        if " watch" in lower:
            umo = event.unified_msg_origin
            if umo not in self._states:
                self._states[umo] = SessionState()
            self._states[umo].subscribed = True
            self._save_states()
            yield reply(f"📌 已订阅当前会话：{umo}")
            return

        if " unwatch" in lower:
            umo = event.unified_msg_origin
            if umo not in self._states:
                self._states[umo] = SessionState()
            self._states[umo].subscribed = False
            self._save_states()
            yield reply(f"📭 已退订当前会话：{umo}")
            return

        if " show" in lower:
            umo = event.unified_msg_origin
            st = self._states.get(umo)
            info = {
                "enable": self.cfg.get("enable"),
                "timezone": self.cfg.get("timezone"),
                "after_last_msg_minutes": self.cfg.get("after_last_msg_minutes"),
                "daily": self.cfg.get("daily"),
                "quiet_hours": self.cfg.get("quiet_hours"),
                "history_depth": self.cfg.get("history_depth"),
                "subscribed": bool(st and st.subscribed),
            }
            yield reply("当前配置/状态：\n" + json.dumps(info, ensure_ascii=False, indent=2))
            return

        m = re.search(r"set\s+after\s+(\d+)", lower)
        if m:
            self.cfg["after_last_msg_minutes"] = int(m.group(1))
            self.cfg.save_config()
            yield reply(f"⏱️ 已设置 last_msg 后触发：{m.group(1)} 分钟")
            return

        m = re.search(r"set\s+daily1\s+(\d{1,2}:\d{2})", lower)
        if m:
            d = self.cfg.get("daily") or {}
            d["time1"] = m.group(1)
            self.cfg["daily"] = d
            self.cfg.save_config()
            yield reply(f"🗓️ 已设置 daily1：{m.group(1)}")
            return

        m = re.search(r"set\s+daily2\s+(\d{1,2}:\d{2})", lower)
        if m:
            d = self.cfg.get("daily") or {}
            d["time2"] = m.group(1)
            self.cfg["daily"] = d
            self.cfg.save_config()
            yield reply(f"🗓️ 已设置 daily2：{m.group(1)}")
            return

        m = re.search(r"set\s+quiet\s+(\d{1,2}:\d{2})-(\d{1,2}:\d{2})", lower)
        if m:
            self.cfg["quiet_hours"] = f"{m.group(1)}-{m.group(2)}"
            self.cfg.save_config()
            yield reply(f"🔕 已设置免打扰：{self.cfg['quiet_hours']}")
            return

        mp = re.search(r"set\s+history\s+(\d+)", lower)
        if mp:
            self.cfg["history_depth"] = int(mp.group(1))
            self.cfg.save_config()
            yield reply(f"🧵 已设置历史条数：{mp.group(1)}")
            return

        # 处理多提示词管理命令
        if " prompt " in lower:
            parts = text.split()
            if len(parts) >= 3 and parts[1].lower() == "prompt":
                sub = parts[2].lower()
                if sub == "list":
                    prompts = self.cfg.get("custom_prompts") or []
                    if not prompts:
                        yield reply("📝 暂无自定义提示词")
                    else:
                        result = "📝 当前提示词列表：\n"
                        for i, prompt in enumerate(prompts, 1):
                            result += f"{i}. {prompt[:50]}{'...' if len(prompt) > 50 else ''}\n"
                        yield reply(result)
                    return
                elif sub == "add" and len(parts) >= 4:
                    new_prompt = text.split("add", 1)[1].strip()
                    if new_prompt:
                        prompts = self.cfg.get("custom_prompts") or []
                        prompts.append(new_prompt)
                        self.cfg["custom_prompts"] = prompts
                        self.cfg.save_config()
                        yield reply(f"✏️ 已添加提示词（共{len(prompts)}个）")
                    else:
                        yield reply("❌ 提示词内容不能为空")
                    return
                elif sub == "del" and len(parts) >= 4:
                    try:
                        index = int(parts[3]) - 1
                        prompts = self.cfg.get("custom_prompts") or []
                        if 0 <= index < len(prompts):
                            del prompts[index]
                            self.cfg["custom_prompts"] = prompts
                            self.cfg.save_config()
                            yield reply(f"🗑️ 已删除提示词（剩余{len(prompts)}个）")
                        else:
                            yield reply("❌ 提示词索引超出范围")
                    except ValueError:
                        yield reply("❌ 请输入有效的数字索引")
                    return
                elif sub == "clear":
                    self.cfg["custom_prompts"] = []
                    self.cfg.save_config()
                    yield reply("🗑️ 已清空所有提示词")
                    return
            yield reply("用法：/aireplay prompt list|add <内容>|del <索引>|clear")
            return

        if " remind " in lower or lower.endswith(" remind"):
            parts = text.split()
            if len(parts) >= 3 and parts[1].lower() == "remind":
                sub = parts[2].lower()
                if sub == "list":
                    yield reply(self._remind_list_text(event.unified_msg_origin))
                    return
                if sub == "del" and len(parts) >= 4:
                    rid = parts[3].strip()
                    if rid in self._reminders and self._reminders[rid].umo == event.unified_msg_origin:
                        del self._reminders[rid]
                        self._save_reminders()
                        yield reply(f"🗑️ 已删除提醒 {rid}")
                    else:
                        yield reply("未找到该提醒 ID")
                    return
                if sub == "add":
                    txt = text.split("add", 1)[1].strip()
                    m1 = re.match(r"^(\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2})\s+(.+)$", txt)
                    m2 = re.match(r"^(\d{1,2}:\d{2})\s+(.+?)\s+daily$", txt, flags=re.I)
                    rid = f"R{int(datetime.now().timestamp())}"
                    if m1:
                        self._reminders[rid] = Reminder(
                            id=rid, umo=event.unified_msg_origin, content=m1.group(2).strip(),
                            at=m1.group(1).strip(), created_at=datetime.now().timestamp()
                        )
                        self._save_reminders()
                        yield reply(f"⏰ 已添加一次性提醒 {rid}")
                        return
                    elif m2:
                        hhmm = m2.group(1)
                        self._reminders[rid] = Reminder(
                            id=rid, umo=event.unified_msg_origin, content=m2.group(2).strip(),
                            at=f"{hhmm}|daily", created_at=datetime.now().timestamp()
                        )
                        self._save_reminders()
                        yield reply(f"⏰ 已添加每日提醒 {rid}")
                        return
            yield reply("用法：/aireplay remind add <YYYY-MM-DD HH:MM> <内容>  或  /aireplay remind add <HH:MM> <内容> daily")
            return

        yield reply(self._help_text())

    def _help_text(self) -> str:
        return (
            "AIReplay 帮助：\n"
            "/aireplay on|off\n"
            "/aireplay watch|unwatch\n"
            "/aireplay show\n"
            "/aireplay debug\n"
            "/aireplay set after <分钟>\n"
            "/aireplay set daily1 <HH:MM>\n"
            "/aireplay set daily2 <HH:MM>\n"
            "/aireplay set quiet <HH:MM-HH:MM>\n"
            "/aireplay set history <N>\n"
            "/aireplay prompt list|add <内容>|del <索引>|clear\n"
            "/aireplay remind add <YYYY-MM-DD HH:MM> <内容>\n"
            "/aireplay remind add <HH:MM> <内容> daily\n"
            "/aireplay remind list | /aireplay remind del <ID>\n"
        )

    def _remind_list_text(self, umo: str) -> str:
        arr = [r for r in self._reminders.values() if r.umo == umo]
        if not arr:
            return "暂无提醒"
        arr.sort(key=lambda x: x.created_at)
        return "提醒列表：\n" + "\n".join(f"{r.id} | {r.at} | {r.content}" for r in arr)

    # 调度器模块
    async def _scheduler_loop(self):
        try:
            while True:
                await asyncio.sleep(30)
                await self._tick()
        except asyncio.CancelledError:
            logger.info("[AIReplay] scheduler stopped.")
        except Exception as e:
            logger.error(f"[AIReplay] scheduler error: {e}")

    async def _tick(self):
        if not self.cfg.get("enable", True):
            return

        tz = self.cfg.get("timezone") or None
        now = _now_tz(tz)
        quiet = self.cfg.get("quiet_hours", "") or ""
        hist_n = int(self.cfg.get("history_depth") or 8)

        daily = self.cfg.get("daily") or {}
        t1 = _parse_hhmm(str(daily.get("time1", "") or ""))
        t2 = _parse_hhmm(str(daily.get("time2", "") or ""))
        if t1 and t2 and t1 == t2:
            h, m = t2; m = (m + 1) % 60; h = (h + (1 if m == 0 else 0)) % 24; t2 = (h, m)

        curr_min_tag_1 = f"daily1@{now.strftime('%Y-%m-%d')} {t1[0]:02d}:{t1[1]:02d}" if t1 else ""
        curr_min_tag_2 = f"daily2@{now.strftime('%Y-%m-%d')} {t2[0]:02d}:{t2[1]:02d}" if t2 else ""

        for umo, st in list(self._states.items()):
            if not st.subscribed:
                continue
            if _in_quiet(now, quiet):
                continue

            # 检查是否需要自动退订
            if await self._should_auto_unsubscribe(umo, st, now):
                continue

            idle_min = int(self.cfg.get("after_last_msg_minutes") or 0)
            if idle_min > 0 and st.last_ts > 0:
                last = datetime.fromtimestamp(st.last_ts, tz=now.tzinfo)
                if now - last >= timedelta(minutes=idle_min):
                    tag = f"idle@{now.strftime('%Y-%m-%d %H:%M')}"
                    if st.last_fired_tag != tag:
                        ok = await self._proactive_reply(umo, hist_n, tz)
                        if ok:
                            st.last_fired_tag = tag
                        else:
                            st.consecutive_no_reply_count += 1

            if t1 and now.hour == t1[0] and now.minute == t1[1]:
                if st.last_fired_tag != curr_min_tag_1:
                    ok = await self._proactive_reply(umo, hist_n, tz)
                    if ok:
                        st.last_fired_tag = curr_min_tag_1
                    else:
                        st.consecutive_no_reply_count += 1
            if t2 and now.hour == t2[0] and now.minute == t2[1]:
                if st.last_fired_tag != curr_min_tag_2:
                    ok = await self._proactive_reply(umo, hist_n, tz)
                    if ok:
                        st.last_fired_tag = curr_min_tag_2
                    else:
                        st.consecutive_no_reply_count += 1

        await self._check_reminders(now, tz)
        self._save_states()

    async def _should_auto_unsubscribe(self, umo: str, st: SessionState, now: datetime) -> bool:
        """检查是否需要自动退订"""
        max_days = int(self.cfg.get("max_no_reply_days") or 0)
        if max_days <= 0:
            return False
        
        if st.last_user_reply_ts > 0:
            last_reply = datetime.fromtimestamp(st.last_user_reply_ts, tz=now.tzinfo)
            days_since_reply = (now - last_reply).days
            
            if days_since_reply >= max_days:
                st.subscribed = False
                logger.info(f"[AIReplay] 自动退订 {umo}：用户{days_since_reply}天未回复")
                return True
        
        return False


    async def _check_reminders(self, now: datetime, tz: Optional[str]):
        fired_ids = []
        for rid, r in self._reminders.items():
            if "|daily" in r.at:
                hhmm = r.at.split("|", 1)[0]
                t = _parse_hhmm(hhmm)
                if not t: 
                    continue
                if now.hour == t[0] and now.minute == t[1]:
                    await self._send_text(r.umo, f"⏰ 提醒：{r.content}")
            else:
                try:
                    dt = datetime.strptime(r.at, "%Y-%m-%d %H:%M")
                    if now.strftime("%Y-%m-%d %H:%M") == dt.strftime("%Y-%m-%d %H:%M"):
                        await self._send_text(r.umo, f"⏰ 提醒：{r.content}")
                        fired_ids.append(rid)
                except Exception:
                    continue
        for rid in fired_ids:
            self._reminders.pop(rid, None)
        if fired_ids:
            self._save_reminders()

    # 主动回复
    async def _proactive_reply(self, umo: str, hist_n: int, tz: Optional[str]) -> bool:
        try:
            fixed_provider = (self.cfg.get("_special") or {}).get("provider") or ""
            provider = None
            if fixed_provider:
                provider = self.context.get_provider_by_id(fixed_provider)
            if not provider:
                provider = self.context.get_using_provider(umo=umo)
            if not provider:
                logger.warning(f"[AIReplay] provider missing for {umo}")
                return False

            conv_mgr = self.context.conversation_manager
            curr_cid = await conv_mgr.get_curr_conversation_id(umo)
            conversation = await conv_mgr.get_conversation(umo, curr_cid)

            system_prompt = ""
            if (self.cfg.get("persona_override") or "").strip():
                system_prompt = self.cfg.get("persona_override")
            else:
                fixed_persona = (self.cfg.get("_special") or {}).get("persona") or ""
                persona_id = fixed_persona or (getattr(conversation, "persona_id", "") or "")
                if persona_id:
                    try:
<<<<<<< ours
<<<<<<< HEAD
                        persona_obj = persona_mgr.get_persona(persona_id)
=======
                        persona_mgr = self.context.persona_manager
                        persona = persona_mgr.get_persona(persona_id)
                        if persona and getattr(persona, "system_prompt", None):
                            system_prompt = persona.system_prompt
>>>>>>> theirs
                    except Exception:
                        pass

<<<<<<< ours
            st = self._states.get(umo)
            contexts = self._collect_contexts(conversation, st, hist_n)
=======
                        persona_mgr = self.context.persona_manager
                        persona = persona_mgr.get_persona(persona_id)
                        if persona and hasattr(persona, "system_prompt") and persona.system_prompt:
                            system_prompt = persona.system_prompt
                            logger.info(f"[AIReplay] 使用人格 {persona_id} 的system_prompt")
                    except Exception as e:
                        logger.warning(f"[AIReplay] 获取人格 {persona_id} 失败: {e}")
                        # 尝试使用默认人格
                        try:
                            default_persona = persona_mgr.get_default_persona_v3(umo)
                            if default_persona and "prompt" in default_persona:
                                system_prompt = default_persona["prompt"]
                                logger.info(f"[AIReplay] 使用默认人格的system_prompt")
                        except Exception as e2:
                            logger.warning(f"[AIReplay] 获取默认人格失败: {e2}")

            # 规范化对话历史，兼容多种形态（JSON 字符串 / 列表 / 包含 messages 的字典）
            contexts: List[Dict] = []
            raw_history = getattr(conversation, "history", None)

            def _normalize_messages(msgs) -> List[Dict]:
                if not msgs:
                    return []
                # 可能是 {"messages": [...]} 结构
                if isinstance(msgs, dict) and "messages" in msgs:
                    msgs = msgs["messages"]
                normalized: List[Dict] = []
                for m in msgs:
                    if isinstance(m, dict):
                        role = m.get("role") or m.get("speaker") or m.get("from")
                        content = m.get("content") or m.get("text") or ""
                        if role in ("user", "assistant", "system") and isinstance(content, str) and content:
                            normalized.append({"role": role, "content": content})
                return normalized
=======
            contexts: List[Dict] = []
            try:
                if conversation and conversation.history:
                    arr = json.loads(conversation.history)
                    contexts = arr[-hist_n:]
            except Exception:
                pass
            if not contexts and hist_n > 0:
                st = self._states.get(umo)
                if st:
                    contexts = list(st.history)[-hist_n:]
>>>>>>> theirs

            try:
                if raw_history:
                    parsed = json.loads(raw_history) if isinstance(raw_history, str) else raw_history
                    contexts = _normalize_messages(parsed)[-hist_n:]
            except Exception:
                contexts = []

            # 回退：使用插件的轻量历史缓存
            if not contexts and hist_n > 0:
                st = self._states.get(umo)
                if st:
                    contexts = list(st.history)[-hist_n:]
>>>>>>> 309f8b84aca897dd369efecd327e8cfaccc62b2b

            # 获取自定义提示词列表
            custom_prompts = self.cfg.get("custom_prompts") or []
            logger.info(f"[AIReplay] 获取到的提示词数量: {len(custom_prompts)}")
            
            if custom_prompts and len(custom_prompts) > 0:
                # 随机选择一个提示词
                templ = random.choice(custom_prompts).strip()
                last_user = ""
                last_ai = ""
                for m in reversed(contexts):
                    if not last_user and m.get("role") == "user":
                        last_user = m.get("content", "")
                    if not last_ai and m.get("role") == "assistant":
                        last_ai = m.get("content", "")
                    if last_user and last_ai:
                        break
                prompt = templ.format(now=_fmt_now(self.cfg.get("time_format") or "%Y-%m-%d %H:%M", tz), last_user=last_user, last_ai=last_ai, umo=umo)
            else:
                prompt = "请自然地延续对话，与用户继续交流。"

            # 调试模式：显示完整上下文
            if self.cfg.get("debug_mode", False):
                logger.info(f"[AIReplay] 调试模式 - 用户: {umo}")
                logger.info(f"[AIReplay] 调试模式 - 系统提示词: {system_prompt or '(无)'}")
                logger.info(f"[AIReplay] 调试模式 - 用户提示词: {prompt}")
                logger.info(f"[AIReplay] 调试模式 - 上下文历史 ({len(contexts)}条):")
                for i, ctx in enumerate(contexts):
                    role = ctx.get("role", "unknown")
                    content = ctx.get("content", "")
                    logger.info(f"[AIReplay] 调试模式 - [{i+1}] {role}: {content[:100]}{'...' if len(content) > 100 else ''}")

            llm_resp = await provider.text_chat(
                prompt=prompt,
                context=contexts,
                system_prompt=system_prompt or ""
            )
            text = llm_resp.completion_text if hasattr(llm_resp, "completion_text") else ""

            if not text.strip():
                return False

            if bool(self.cfg.get("append_time_field")):
                text = f"[{_fmt_now(self.cfg.get('time_format') or '%Y-%m-%d %H:%M', tz)}] " + text

            await self._send_text(umo, text)
            logger.info(f"[AIReplay] 已发送主动回复给 {umo}: {text[:50]}...")

            # 更新最后时间戳为AI发送消息的时间，并把AI回复写入轻量历史，方便下次回退
            now_ts = _now_tz(tz).timestamp()
            st = self._states.get(umo)
            if st:
                st.last_ts = now_ts
                try:
                    st.history.append({"role": "assistant", "content": text})
                except Exception:
                    pass
                self._save_states()
            
            return True
        except Exception as e:
            logger.error(f"[AIReplay] proactive error({umo}): {e}")
            return False

    # 消息发送
    async def _send_text(self, umo: str, text: str):
        try:
            chain = MessageChain().message(text)
            await self.context.send_message(umo, chain)
        except Exception as e:
            logger.error(f"[AIReplay] send_message error({umo}): {e}")

    async def terminate(self):
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()
            try:
                await self._loop_task
            except Exception:
                pass
        
        # 检查插件是否被卸载（通过检查插件主文件是否存在）
        plugin_main_file = os.path.abspath(__file__)
        is_uninstall = not os.path.exists(plugin_main_file)
        
        if is_uninstall:
            # 插件被卸载 - 清除所有数据
            logger.info("[AIReplay] 检测到插件卸载，开始清理数据...")
            
            # 清除用户配置
            try:
                # 重置所有配置项为默认值
                self.cfg["enable"] = True
                self.cfg["custom_prompts"] = []
                self.cfg["max_no_reply_days"] = 0
                self.cfg["persona_override"] = ""
                self.cfg["quiet_hours"] = ""
                self.cfg["timezone"] = ""
                self.cfg["time_format"] = "%Y-%m-%d %H:%M"
                self.cfg["history_depth"] = 8
                self.cfg["after_last_msg_minutes"] = 0
                self.cfg["append_time_field"] = False
                self.cfg["daily"] = {}
                self.cfg["subscribe_mode"] = "manual"
                self.cfg["debug_mode"] = False
                self.cfg["_special"] = {}
                # 保存配置以确保清除生效
                self.cfg.save_config()
                logger.info("[AIReplay] 已清除用户配置")
            except Exception as e:
                logger.error(f"[AIReplay] 清除用户配置时出错: {e}")
            
            # 清理数据文件
            try:
                if os.path.exists(self._state_path):
                    os.remove(self._state_path)
                    logger.info(f"[AIReplay] 已删除状态文件: {self._state_path}")
                if os.path.exists(self._remind_path):
                    os.remove(self._remind_path)
                    logger.info(f"[AIReplay] 已删除提醒文件: {self._remind_path}")
                
                # 如果数据目录为空，删除整个目录
                if os.path.exists(self._data_dir) and not os.listdir(self._data_dir):
                    os.rmdir(self._data_dir)
                    logger.info(f"[AIReplay] 已删除数据目录: {self._data_dir}")
            except Exception as e:
                logger.error(f"[AIReplay] 清理数据文件时出错: {e}")
        else:
            # 插件被停用 - 只保存状态，不清理数据
            logger.info("[AIReplay] 检测到插件停用，保存状态...")
            try:
                self._save_states()
                self._save_reminders()
                logger.info("[AIReplay] 状态已保存")
            except Exception as e:
                logger.error(f"[AIReplay] 保存状态时出错: {e}")
        
        logger.info("[AIReplay] terminated.")
