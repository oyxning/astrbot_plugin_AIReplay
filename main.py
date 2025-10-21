
from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, time
from typing import Dict, List, Optional, Deque, Tuple
from collections import defaultdict, deque

import astrbot.api.message_components as Comp
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult, EventMessageType
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.config import AstrBotConfig

def _ensure_dir(p: str):
    os.makedirs(p, exist_ok=True); return p

def _now_tz(tz_name: str | None) -> datetime:
    try:
        if tz_name:
            import zoneinfo; return datetime.now(zoneinfo.ZoneInfo(tz_name))
    except Exception: pass
    return datetime.now()

def _parse_hhmm(s: str) -> Optional[Tuple[int,int]]:
    if not s: return None
    m = re.match(r"^([01]?\d|2[0-3]):([0-5]\d)$", s.strip())
    if not m: return None
    return int(m.group(1)), int(m.group(2))

def _in_quiet(now: datetime, quiet: str) -> bool:
    if not quiet or "-" not in quiet: return False
    a, b = quiet.split("-", 1)
    p1 = _parse_hhmm(a); p2 = _parse_hhmm(b)
    if not p1 or not p2: return False
    t1 = time(p1[0], p1[1]); t2 = time(p2[0], p2[1])
    nt = now.time()
    if t1 <= t2: return t1 <= nt <= t2
    return nt >= t1 or nt <= t2

def _fmt_now(fmt: str, tz: str | None) -> str:
    return _now_tz(tz).strftime(fmt)

@dataclass
class SessionState:
    last_ts: float = 0.0
    history: Deque[Dict] = field(default_factory=lambda: deque(maxlen=32))
    subscribed: bool = False
    last_fired_tag: str = ""

@dataclass
class Reminder:
    id: str
    umo: str
    content: str
    at: str
    created_at: float

@register("AIReplay", "LumineStory", "定时/间隔主动续聊 + 人格 + 历史 + 免打扰 + 提醒", "1.0.0", "https://github.com/oyxning/astrbot_plugin_AIReplay")
class AIReplay(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg: AstrBotConfig = config
        self._loop_task: Optional[asyncio.Task] = None
        self._states: Dict[str, SessionState] = defaultdict(SessionState)
        self._reminders: Dict[str, Reminder] = {}
        root = os.getcwd()
        self._data_dir = _ensure_dir(os.path.join(root, "data", "plugins", "astrbot_plugin_aireplay"))
        self._state_path = os.path.join(self._data_dir, "state.json")
        self._remind_path = os.path.join(self._data_dir, "reminders.json")
        self._load_states(); self._load_reminders()
        self._loop_task = asyncio.create_task(self._scheduler_loop())

    def _load_states(self):
        if os.path.exists(self._state_path):
            try:
                d = json.load(open(self._state_path, "r", encoding="utf-8"))
                for umo, st in d.get("states", {}).items():
                    s = SessionState(last_ts=st.get("last_ts", 0.0), subscribed=st.get("subscribed", False), last_fired_tag=st.get("last_fired_tag", ""))
                    self._states[umo] = s
            except Exception as e: pass

    def _save_states(self):
        dump = {"states": {k: {"last_ts": v.last_ts, "subscribed": v.subscribed, "last_fired_tag": v.last_fired_tag} for k, v in self._states.items()}}
        json.dump(dump, open(self._state_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    def _load_reminders(self):
        if os.path.exists(self._remind_path):
            try:
                arr = json.load(open(self._remind_path, "r", encoding="utf-8"))
                for it in arr:
                    r = Reminder(**it); self._reminders[r.id] = r
            except Exception: pass

    def _save_reminders(self):
        arr = [r.__dict__ for r in self._reminders.values()]
        json.dump(arr, open(self._remind_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    @filter.event_message_type(EventMessageType.ALL)
    async def _on_any_message(self, event: AstrMessageEvent):
        umo = event.unified_msg_origin
        st = self._states[umo]
        st.last_ts = _now_tz(self.cfg.get("timezone") or None).timestamp()
        if (self.cfg.get("subscribe_mode") or "manual") == "auto":
            st.subscribed = True
        content = (event.message_str or "").strip()
        if content: st.history.append({"role": "user", "content": content})
        self._save_states()

    @filter.command("aireplay")
    async def _cmd_aireplay(self, event: AstrMessageEvent, *args: str):
        text = (event.message_str or "").strip()
        lower = text.lower()
        def reply(msg: str): return event.plain_result(msg)

        if "help" in lower or text.strip() == "/aireplay":
            yield reply(self._help_text()); return
        if " on" in lower:
            self.cfg["enable"] = True; self.cfg.save_config(); yield reply("✅ 已启用 AIReplay"); return
        if " off" in lower:
            self.cfg["enable"] = False; self.cfg.save_config(); yield reply("🛑 已停用 AIReplay"); return
        if " watch" in lower:
            self._states[event.unified_msg_origin].subscribed = True; self._save_states(); yield reply("📌 已订阅当前会话"); return
        if " unwatch" in lower:
            self._states[event.unified_msg_origin].subscribed = False; self._save_states(); yield reply("📭 已退订当前会话"); return
        if " show" in lower:
            umo = event.unified_msg_origin; st = self._states.get(umo)
            info = {"enable": self.cfg.get("enable"), "timezone": self.cfg.get("timezone"),
                "after_last_msg_minutes": self.cfg.get("after_last_msg_minutes"),
                "daily": self.cfg.get("daily"), "quiet_hours": self.cfg.get("quiet_hours"),
                "history_depth": self.cfg.get("history_depth"), "subscribed": bool(st and st.subscribed)}
            yield reply("当前配置/状态：\n" + json.dumps(info, ensure_ascii=False, indent=2)); return

        m = re.search(r"set\s+after\s+(\d+)", lower)
        if m:
            self.cfg["after_last_msg_minutes"] = int(m.group(1)); self.cfg.save_config(); yield reply("⏱️ 已设置"); return
        m = re.search(r"set\s+daily1\s+(\d{1,2}:\d{2})", lower)
        if m:
            d = self.cfg.get("daily") or {}; d["time1"] = m.group(1); self.cfg["daily"] = d; self.cfg.save_config(); yield reply("🗓️ 已设置 daily1"); return
        m = re.search(r"set\s+daily2\s+(\d{1,2}:\d{2})", lower)
        if m:
            d = self.cfg.get("daily") or {}; d["time2"] = m.group(1); self.cfg["daily"] = d; self.cfg.save_config(); yield reply("🗓️ 已设置 daily2"); return
        m = re.search(r"set\s+quiet\s+(\d{1,2}:\d{2})-(\d{1,2}:\d{2})", lower)
        if m:
            self.cfg["quiet_hours"] = f"{m.group(1)}-{m.group(2)}"; self.cfg.save_config(); yield reply("🔕 已设置免打扰"); return
        m = re.search(r"set\s+history\s+(\d+)", lower)
        if m:
            self.cfg["history_depth"] = int(m.group(1)); self.cfg.save_config(); yield reply("🧵 已设置历史条数"); return
        m = re.search(r"set\s+prompt\s+(.+)$", text, flags=re.I | re.S)
        if m:
            self.cfg["custom_prompt"] = m.group(1).strip(); self.cfg.save_config(); yield reply("✏️ 已更新自定义提示词"); return

        if " remind " in lower or lower.endswith(" remind"):
            parts = text.split()
            if len(parts) >= 3 and parts[1].lower() == "remind":
                sub = parts[2].lower()
                if sub == "list":
                    yield reply(self._remind_list_text(event.unified_msg_origin)); return
                if sub == "del" and len(parts) >= 4:
                    rid = parts[3].strip()
                    if rid in self._reminders and self._reminders[rid].umo == event.unified_msg_origin:
                        del self._reminders[rid]; self._save_reminders(); yield reply("🗑️ 已删除"); return
                    else:
                        yield reply("未找到该提醒 ID"); return
                if sub == "add":
                    txt = text.split("add", 1)[1].strip()
                    m1 = re.match(r"^(\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2})\s+(.+)$", txt)
                    m2 = re.match(r"^(\d{1,2}:\d{2})\s+(.+?)\s+daily$", txt, flags=re.I)
                    rid = f"R{int(datetime.now().timestamp())}"
                    if m1:
                        self._reminders[rid] = Reminder(id=rid, umo=event.unified_msg_origin, content=m1.group(2).strip(), at=m1.group(1).strip(), created_at=datetime.now().timestamp())
                        self._save_reminders(); yield reply(f"⏰ 已添加一次性提醒 {rid}"); return
                    if m2:
                        hhmm = m2.group(1)
                        self._reminders[rid] = Reminder(id=rid, umo=event.unified_msg_origin, content=m2.group(2).strip(), at=f"{hhmm}|daily", created_at=datetime.now().timestamp())
                        self._save_reminders(); yield reply(f"⏰ 已添加每日提醒 {rid}"); return
            yield reply("用法：/aireplay remind add <YYYY-MM-DD HH:MM> <内容>  或  /aireplay remind add <HH:MM> <内容> daily"); return

        yield reply(self._help_text())

    def _help_text(self) -> str:
        return (
            "AIReplay 帮助：\n"
            "/aireplay on|off\n"
            "/aireplay watch|unwatch\n"
            "/aireplay show\n"
            "/aireplay set after <分钟>\n"
            "/aireplay set daily1 <HH:MM>\n"
            "/aireplay set daily2 <HH:MM>\n"
            "/aireplay set quiet <HH:MM-HH:MM>\n"
            "/aireplay set history <N>\n"
            "/aireplay set prompt <文本>\n"
            "/aireplay remind add <YYYY-MM-DD HH:MM> <内容>\n"
            "/aireplay remind add <HH:MM> <内容> daily\n"
            "/aireplay remind list | /aireplay remind del <ID>\n"
        )

    def _remind_list_text(self, umo: str) -> str:
        arr = [r for r in self._reminders.values() if r.umo == umo]
        if not arr: return "暂无提醒"
        arr.sort(key=lambda x: x.created_at)
        return "提醒列表：\n" + "\n".join(f\"{r.id} | {r.at} | {r.content}\" for r in arr)

    async def _scheduler_loop(self):
        try:
            while True:
                await asyncio.sleep(30)
                await self._tick()
        except asyncio.CancelledError:
            pass

    async def _tick(self):
        if not self.cfg.get("enable", True): return
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
            if not st.subscribed: continue
            if _in_quiet(now, quiet): continue
            idle_min = int(self.cfg.get("after_last_msg_minutes") or 0)
            if idle_min > 0 and st.last_ts > 0:
                last = datetime.fromtimestamp(st.last_ts, tz=now.tzinfo)
                if now - last >= timedelta(minutes=idle_min):
                    tag = f"idle@{now.strftime('%Y-%m-%d %H:%M')}"
                    if st.last_fired_tag != tag:
                        ok = await self._proactive_reply(umo, hist_n, tz)
                        if ok: st.last_fired_tag = tag
            if t1 and now.hour == t1[0] and now.minute == t1[1]:
                if st.last_fired_tag != curr_min_tag_1:
                    ok = await self._proactive_reply(umo, hist_n, tz)
                    if ok: st.last_fired_tag = curr_min_tag_1
            if t2 and now.hour == t2[0] and now.minute == t2[1]:
                if st.last_fired_tag != curr_min_tag_2:
                    ok = await self._proactive_reply(umo, hist_n, tz)
                    if ok: st.last_fired_tag = curr_min_tag_2
        await self._check_reminders(now, tz)
        self._save_states()

    async def _check_reminders(self, now: datetime, tz: Optional[str]):
        fired_ids = []
        for rid, r in self._reminders.items():
            if "|daily" in r.at:
                hhmm = r.at.split("|", 1)[0]
                t = _parse_hhmm(hhmm)
                if not t: continue
                if now.hour == t[0] and now.minute == t[1]:
                    await self._send_text(r.umo, f"⏰ 提醒：{r.content}")
            else:
                try:
                    dt = datetime.strptime(r.at, "%Y-%m-%d %H:%M")
                    if now.strftime("%Y-%m-%d %H:%M") == dt.strftime("%Y-%m-%d %H:%M"):
                        await self._send_text(r.umo, f"⏰ 提醒：{r.content}")
                        fired_ids.append(rid)
                except Exception: pass
        for rid in fired_ids: self._reminders.pop(rid, None)
        if fired_ids: self._save_reminders()

    async def _proactive_reply(self, umo: str, hist_n: int, tz: Optional[str]) -> bool:
        try:
            fixed_provider = (self.cfg.get("_special") or {}).get("provider") or ""
            provider = None
            if fixed_provider: provider = self.context.get_provider_by_id(fixed_provider)
            if not provider: provider = self.context.get_using_provider(umo=umo)
            if not provider: return False
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
                        persona_mgr = self.context.persona_manager
                        persona = persona_mgr.get_persona(persona_id)
                        if persona and getattr(persona, "system_prompt", None): system_prompt = persona.system_prompt
                    except Exception: pass
            contexts: List[Dict] = []
            try:
                if conversation and conversation.history:
                    arr = json.loads(conversation.history); contexts = arr[-hist_n:]
            except Exception: pass
            if not contexts and hist_n > 0:
                st = self._states.get(umo)
                if st: contexts = list(st.history)[-hist_n:]
            templ = (self.cfg.get("custom_prompt") or "").strip()
            if templ:
                last_user = ""; last_ai = ""
                for m in reversed(contexts):
                    if not last_user and m.get("role") == "user": last_user = m.get("content", "")
                    if not last_ai and m.get("role") == "assistant": last_ai = m.get("content", "")
                    if last_user and last_ai: break
                prompt = templ.format(now=_fmt_now(self.cfg.get("time_format") or "%Y-%m-%d %H:%M", tz), last_user=last_user, last_ai=last_ai, umo=umo)
            else:
                prompt = "请自然地延续对话，与用户继续交流。"
            llm_resp = await provider.text_chat(prompt=prompt, context=contexts, system_prompt=system_prompt or "")
            text = getattr(llm_resp, "completion_text", "")
            if not text.strip(): return False
            if bool(self.cfg.get("append_time_field")):
                text = f"[{_fmt_now(self.cfg.get('time_format') or '%Y-%m-%d %H:%M', tz)}] " + text
            await self._send_text(umo, text)
            return True
        except Exception:
            return False

    async def _send_text(self, umo: str, text: str):
        chain = [Comp.Plain(text=text)]
        await self.context.send_message(umo, chain)

    async def terminate(self):
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()
            try: await self._loop_task
            except Exception: pass
        self._save_states(); self._save_reminders()
