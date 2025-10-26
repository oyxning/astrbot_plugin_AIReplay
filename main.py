
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
@register("AIReplay", "LumineStory", "定时/间隔主动续聊 + 人格 + 历史 + 免打扰 + 提醒", "1.1.0", "https://github.com/oyxning/astrbot_plugin_AIReplay")
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
        self._sync_subscribed_users_from_config()  # 从配置同步订阅列表到内部状态

        self._loop_task = asyncio.create_task(self._scheduler_loop())
        logger.info("[AIReplay] scheduler started.")

    # 数据持久化
    def _load_states(self):
        """从磁盘加载所有会话状态（订阅状态、历史记录、时间戳等）"""
        if os.path.exists(self._state_path):
            try:
                with open(self._state_path, "r", encoding="utf-8") as f:
                    d = json.load(f)
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
        """保存所有会话状态到磁盘，并同步订阅用户列表到配置"""
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
            with open(self._state_path, "w", encoding="utf-8") as f:
                json.dump(dump, f, ensure_ascii=False, indent=2)
            
            # 同步订阅用户列表到配置（以用户ID形式存储，方便WebUI管理）
            subscribed_ids = []
            for umo, st in self._states.items():
                if st.subscribed:
                    # 提取用户ID（去掉平台前缀）
                    user_id = umo.split(":")[-1] if ":" in umo else umo
                    subscribed_ids.append(user_id)
            
            self.cfg["subscribed_users"] = subscribed_ids
            self.cfg.save_config()
            
        except Exception as e:
            logger.error(f"[AIReplay] save states error: {e}")

    def _load_reminders(self):
        """从磁盘加载所有提醒事项（一次性提醒和每日提醒）"""
        if os.path.exists(self._remind_path):
            try:
                with open(self._remind_path, "r", encoding="utf-8") as f:
                    arr = json.load(f)
                for it in arr:
                    r = Reminder(**it)
                    self._reminders[r.id] = r
            except Exception as e:
                logger.error(f"[AIReplay] load reminders error: {e}")

    def _save_reminders(self):
        """保存所有提醒事项到磁盘"""
        try:
            arr = [r.__dict__ for r in self._reminders.values()]
            with open(self._remind_path, "w", encoding="utf-8") as f:
                json.dump(arr, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[AIReplay] save reminders error: {e}")
    
    def _sync_subscribed_users_from_config(self):
        """
        从配置文件同步订阅用户列表到内部状态
        
        功能：
        - 读取配置中的 subscribed_users 列表（纯用户ID）
        - 将这些用户标记为已订阅
        - 支持用户在 WebUI 中直接编辑订阅列表
        
        注意：
        - 配置中存储的是纯用户ID（如 "49025031"）
        - 内部 _states 的 key 是完整的 umo（如 "aulus-beta:FriendMessage:49025031"）
        - 需要遍历所有 _states，匹配 ID 后缀来应用订阅状态
        """
        try:
            config_subscribed_ids = self.cfg.get("subscribed_users") or []
            if not isinstance(config_subscribed_ids, list):
                logger.warning(f"[AIReplay] subscribed_users 配置格式错误，应为列表")
                return
            
            # 将配置中的用户ID应用到内部状态
            for umo, st in self._states.items():
                user_id = umo.split(":")[-1] if ":" in umo else umo
                if user_id in config_subscribed_ids:
                    st.subscribed = True
                    logger.debug(f"[AIReplay] 从配置同步订阅状态: {umo}")
            
            # 为配置中但尚未存在于 _states 的用户创建状态（标记为已订阅）
            # 注意：这些用户的完整 umo 要等到他们第一次发消息时才能确定
            # 所以这里只是做个标记，实际订阅会在 _on_any_message 中生效
            
            logger.info(f"[AIReplay] 已从配置同步 {len(config_subscribed_ids)} 个订阅用户")
            
        except Exception as e:
            logger.error(f"[AIReplay] 同步订阅用户配置失败: {e}")

    # 消息处理
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def _on_any_message(self, event: AstrMessageEvent):
        """
        监听所有消息事件的 Handler
        
        功能：
        1. 更新会话的最后活跃时间戳（用于触发定时回复）
        2. 更新用户最后回复时间（用于自动退订检测）
        3. 重置连续无回复计数器
        4. 如果是自动订阅模式，自动订阅新会话
        5. 记录用户消息到轻量历史缓存（供上下文获取降级使用）
        
        注意：这个 handler 会捕获所有消息，包括机器人自己发的消息
        """
        umo = event.unified_msg_origin
        if umo not in self._states:
            self._states[umo] = SessionState()
        st = self._states[umo]
        now_ts = _now_tz(self.cfg.get("timezone") or None).timestamp()
        st.last_ts = now_ts
        st.last_user_reply_ts = now_ts  # 记录用户最后回复时间
        st.consecutive_no_reply_count = 0  # 重置无回复计数

        # 检查订阅状态：支持自动订阅模式 + WebUI配置列表
        if (self.cfg.get("subscribe_mode") or "manual") == "auto":
            st.subscribed = True
        else:
            # manual 模式下，检查用户ID是否在配置的订阅列表中
            user_id = umo.split(":")[-1] if ":" in umo else umo
            config_subscribed_ids = self.cfg.get("subscribed_users") or []
            if user_id in config_subscribed_ids and not st.subscribed:
                st.subscribed = True
                logger.info(f"[AIReplay] 用户 {user_id} 在配置订阅列表中，已自动订阅")

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
        """
        AIReplay 插件的命令处理器
        
        支持的子命令：
        - help: 显示帮助信息
        - debug: 显示当前配置和调试信息
        - on/off: 启用/停用插件
        - watch: 订阅当前会话（开始接收主动回复）
        - unwatch: 退订当前会话（停止接收主动回复）
        - show: 显示当前会话的配置和状态
        - set after <分钟>: 设置消息后多久触发主动回复
        - set daily1/daily2 <HH:MM>: 设置每日定时回复时间
        - set quiet <HH:MM-HH:MM>: 设置免打扰时间段
        - set history <N>: 设置上下文历史条数
        - prompt list/add/del/clear: 管理自定义提示词
        - remind add/list/del: 管理提醒事项
        
        用法示例：
        /aireplay watch - 订阅当前会话
        /aireplay set after 30 - 设置30分钟无消息后主动回复
        /aireplay prompt add 现在是{now}，请继续聊天 - 添加自定义提示词
        """
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
            yield reply(f"📌 已订阅当前会话")
            return

        if " unwatch" in lower:
            umo = event.unified_msg_origin
            if umo not in self._states:
                self._states[umo] = SessionState()
            self._states[umo].subscribed = False
            self._save_states()
            yield reply(f"📭 已退订当前会话")
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
        """返回插件的帮助文本，展示所有可用命令"""
        return (
            "AIReplay 帮助：\n"
            "/aireplay on|off - 启用/停用插件\n"
            "/aireplay watch - 订阅当前会话\n"
            "/aireplay unwatch - 退订当前会话\n"
            "/aireplay show - 显示当前配置\n"
            "/aireplay debug - 显示调试信息\n"
            "/aireplay set after <分钟> - 设置间隔触发\n"
            "/aireplay set daily1/daily2 <HH:MM> - 设置定时触发\n"
            "/aireplay set quiet <HH:MM-HH:MM> - 设置免打扰\n"
            "/aireplay set history <N> - 设置历史条数\n"
            "/aireplay prompt list|add|del|clear - 管理提示词\n"
            "/aireplay remind add/list/del - 管理提醒\n"
        )

    def _remind_list_text(self, umo: str) -> str:
        """生成指定用户的提醒列表文本"""
        arr = [r for r in self._reminders.values() if r.umo == umo]
        if not arr:
            return "暂无提醒"
        arr.sort(key=lambda x: x.created_at)
        return "提醒列表：\n" + "\n".join(f"{r.id} | {r.at} | {r.content}" for r in arr)

    # 上下文获取方法
    async def _safe_get_full_contexts(self, umo: str, conversation=None) -> List[Dict]:
        """
        安全获取完整上下文，使用多重降级策略
        
        参数:
            umo: 统一消息来源
            conversation: 已获取的对话对象（可选）
        """
        contexts = []
        
        # 策略1：从传入的 conversation 对象获取
        if conversation:
            try:
                # 1.1 尝试从 messages 属性获取
                if hasattr(conversation, "messages") and conversation.messages:
                    contexts = self._normalize_messages(conversation.messages)
                    if contexts:
                        logger.debug(f"[AIReplay] 从conversation.messages获取{len(contexts)}条历史")
                        return contexts
                
                # 1.2 尝试调用 get_messages() 方法
                if hasattr(conversation, "get_messages"):
                    try:
                        messages = await conversation.get_messages()
                        if messages:
                            contexts = self._normalize_messages(messages)
                            if contexts:
                                logger.debug(f"[AIReplay] 从conversation.get_messages()获取{len(contexts)}条历史")
                                return contexts
                    except Exception:
                        pass
                
                # 1.3 尝试从 history 属性解析JSON
                if hasattr(conversation, 'history') and conversation.history:
                    if isinstance(conversation.history, str):
                        try:
                            history = json.loads(conversation.history)
                            if history:
                                contexts = self._normalize_messages(history)
                                if contexts:
                                    logger.debug(f"[AIReplay] 从conversation.history(JSON)获取{len(contexts)}条历史")
                                    return contexts
                        except json.JSONDecodeError:
                            pass
                    elif isinstance(conversation.history, list):
                        contexts = self._normalize_messages(conversation.history)
                        if contexts:
                            logger.debug(f"[AIReplay] 从conversation.history(list)获取{len(contexts)}条历史")
                            return contexts
            except Exception as e:
                logger.warning(f"[AIReplay] 从传入的conversation获取失败: {e}")
        
        # 策略2：通过 conversation_manager 重新获取最新对话
        try:
            if hasattr(self.context, "conversation_manager"):
                conv_mgr = self.context.conversation_manager
                conversation_id = await conv_mgr.get_curr_conversation_id(umo)
                if conversation_id:
                    # 2.2 根据ID获取完整的对话对象
                    conversation = await conv_mgr.get_conversation(umo, conversation_id)
                    if conversation:
                        # 尝试 messages 属性
                        if hasattr(conversation, "messages") and conversation.messages:
                            contexts = self._normalize_messages(conversation.messages)
                            if contexts:
                                logger.debug(f"[AIReplay] 从conversation_manager.messages获取{len(contexts)}条历史")
                                return contexts
                        
                        # 尝试 history 属性
                        if hasattr(conversation, 'history') and conversation.history:
                            if isinstance(conversation.history, str):
                                try:
                                    history = json.loads(conversation.history)
                                    if history:
                                        contexts = self._normalize_messages(history)
                                        if contexts:
                                            logger.debug(f"[AIReplay] 从conversation_manager.history获取{len(contexts)}条历史")
                                            return contexts
                                except json.JSONDecodeError:
                                    pass
                            elif isinstance(conversation.history, list):
                                contexts = self._normalize_messages(conversation.history)
                                if contexts:
                                    logger.debug(f"[AIReplay] 从conversation_manager.history(list)获取{len(contexts)}条历史")
                                    return contexts
        except Exception as e:
            logger.warning(f"[AIReplay] 从conversation_manager获取历史失败: {e}")
        
        # 策略3：使用插件的轻量历史缓存（最后的降级方案）
        try:
            st = self._states.get(umo)
            if st and st.history:
                contexts = list(st.history)
                logger.debug(f"[AIReplay] 使用插件缓存历史，共{len(contexts)}条")
                return contexts
        except Exception as e:
            logger.warning(f"[AIReplay] 从插件缓存获取历史失败: {e}")
        
        logger.warning(f"[AIReplay] ⚠️ 无法获取 {umo} 的对话历史，将使用空上下文")
        return contexts

    def _normalize_messages(self, msgs) -> List[Dict]:
        """
        标准化消息格式，兼容多种形态
        """
        if not msgs:
            return []
        
        # 如果是字典且包含 messages 键
        if isinstance(msgs, dict) and "messages" in msgs:
            msgs = msgs["messages"]
        
        normalized = []
        for m in msgs:
            if isinstance(m, dict):
                role = m.get("role") or m.get("speaker") or m.get("from")
                content = m.get("content") or m.get("text") or ""
                if role in ("user", "assistant", "system") and isinstance(content, str) and content:
                    normalized.append({"role": role, "content": content})
        
        return normalized

    # 调度器模块
    async def _scheduler_loop(self):
        """
        后台调度循环任务，每30秒检查一次是否需要触发主动回复
        
        这是插件的核心后台任务，在插件初始化时通过 asyncio.create_task() 启动。
        会持续运行直到插件被卸载或停用。
        
        每次循环会调用 _tick() 方法来检查：
        - 是否有会话达到间隔触发条件
        - 是否有会话需要每日定时回复
        - 是否有提醒需要触发
        """
        try:
            while True:
                await asyncio.sleep(30)
                await self._tick()
        except asyncio.CancelledError:
            logger.info("[AIReplay] scheduler stopped.")
        except Exception as e:
            logger.error(f"[AIReplay] scheduler error: {e}")

    async def _tick(self):
        """
        单次调度检查（每30秒执行一次）
        
        检查逻辑：
        1. 如果插件被停用，直接返回
        2. 遍历所有已订阅的会话，检查是否需要主动回复：
           a. 间隔触发：距离最后一条消息超过设定分钟数
           b. 每日定时1/2：到达设定的时间点（如每天早上9点）
        3. 检查是否在免打扰时间段内，如果是则跳过
        4. 检查是否需要自动退订（用户连续多天未回复）
        5. 检查并触发提醒事项
        6. 保存状态到磁盘
        
        注意：每个触发条件都会记录一个唯一的 tag，防止同一时刻重复触发
        """
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
        """
        检查是否需要自动退订（根据用户无回复天数）
        
        参数：
            umo: 统一消息来源（用户标识）
            st: 该用户的会话状态
            now: 当前时间
            
        返回：
            True: 已自动退订该用户
            False: 不需要退订
            
        逻辑：
        - 如果配置了 max_no_reply_days > 0
        - 且用户最后回复时间距今超过设定天数
        - 则自动将该用户的 subscribed 状态设为 False
        - 这样可以避免长期无人回复的会话持续消耗 LLM 额度
        """
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
        """
        检查并触发到期的提醒事项
        
        支持两种提醒类型：
        1. 一次性提醒：格式 "YYYY-MM-DD HH:MM"，触发后自动删除
        2. 每日提醒：格式 "HH:MM|daily"，每天相同时间触发，不删除
        """
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
        """
        执行主动回复的核心方法（这是插件最重要的功能！）
        
        参数：
            umo: 统一消息来源（会话标识）
            hist_n: 需要获取的历史消息条数
            tz: 时区名称（用于时间格式化）
            
        返回：
            True: 成功发送回复
            False: 发送失败或回复内容为空
            
        完整流程：
        1. 获取 LLM Provider（支持固定provider配置）
        2. 获取当前对话对象（通过 conversation_manager）
        3. 获取人格/系统提示词（多策略降级）：
           - 优先：配置中的 persona_override
           - 其次：指定的 persona_id
           - 降级：conversation.persona
           - 兜底：默认人格（get_default_persona_v3等）
        4. 获取完整上下文历史（调用 _safe_get_full_contexts，多策略降级）
        5. 构造主动回复的 prompt：
           - 如果配置了 custom_prompts，随机选择一个并格式化
           - 否则使用默认提示词："请自然地延续对话，与用户继续交流。"
        6. 调用 LLM 的 text_chat 接口（注意参数名是 contexts 复数！）
        7. 如果配置了 append_time_field，在回复前添加时间戳
        8. 发送消息并更新会话状态
        
        重要修复点：
        - persona 获取必须使用 await（如果是异步方法）
        - LLM 调用参数名必须是 contexts（复数），不是 context（单数）
        - 上下文获取要有多层降级策略，确保健壮性
        """
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

            # 获取 system_prompt（多重降级策略）
            system_prompt = ""
            persona_obj = None
            
            # 优先使用配置文件中的自定义人格
            if (self.cfg.get("persona_override") or "").strip():
                system_prompt = self.cfg.get("persona_override")
                logger.debug(f"[AIReplay] 使用配置文件中的自定义人格")
            else:
                # 尝试获取 persona_manager
                persona_mgr = getattr(self.context, "persona_manager", None)
                if not persona_mgr:
                    logger.warning(f"[AIReplay] persona_manager 不可用")
                else:
                    # 策略1: 尝试从配置或 conversation 获取指定的 persona_id
                    fixed_persona = (self.cfg.get("_special") or {}).get("persona") or ""
                    persona_id = fixed_persona or (getattr(conversation, "persona_id", "") or "")
                    
                    if persona_id:
                        try:
                            # 尝试异步调用（如果是异步方法）
                            if asyncio.iscoroutinefunction(persona_mgr.get_persona):
                                persona_obj = await persona_mgr.get_persona(persona_id)
                            else:
                                persona_obj = persona_mgr.get_persona(persona_id)
                            
                            if persona_obj:
                                logger.debug(f"[AIReplay] 成功获取指定人格: {persona_id}")
                        except Exception as e:
                            logger.warning(f"[AIReplay] 获取指定人格 {persona_id} 失败: {e}")
                    
                    # 策略2: 如果没有获取到，尝试从 conversation.persona 直接获取
                    if not persona_obj and conversation:
                        persona_obj = getattr(conversation, "persona", None)
                        if persona_obj:
                            logger.debug(f"[AIReplay] 从 conversation.persona 获取人格")
                    
                    # 策略3: 尝试获取默认人格（多种方法）
                    if not persona_obj:
                        for getter_name in ("get_default_persona_v3", "get_default_persona", "get_default"):
                            getter = getattr(persona_mgr, getter_name, None)
                            if not callable(getter):
                                continue
                            try:
                                # 尝试带参数调用
                                try:
                                    if asyncio.iscoroutinefunction(getter):
                                        persona_obj = await getter(umo)
                                    else:
                                        persona_obj = getter(umo)
                                except TypeError:
                                    # 不需要参数，直接调用
                                    if asyncio.iscoroutinefunction(getter):
                                        persona_obj = await getter()
                                    else:
                                        persona_obj = getter()
                                
                                if persona_obj:
                                    logger.debug(f"[AIReplay] 通过 {getter_name} 获取默认人格")
                                    break
                            except Exception as e:
                                logger.debug(f"[AIReplay] 通过 {getter_name} 获取默认人格失败: {e}")
                
                # 从 persona 对象或 conversation 提取 system_prompt
                if persona_obj:
                    # 尝试多种属性名
                    for attr in ("system_prompt", "prompt", "content", "text"):
                        if hasattr(persona_obj, attr):
                            prompt_value = getattr(persona_obj, attr, None)
                            if isinstance(prompt_value, str) and prompt_value.strip():
                                system_prompt = prompt_value.strip()
                                logger.info(f"[AIReplay] 从 persona.{attr} 获取 system_prompt")
                                break
                        # 如果是字典
                        if isinstance(persona_obj, dict) and attr in persona_obj:
                            prompt_value = persona_obj[attr]
                            if isinstance(prompt_value, str) and prompt_value.strip():
                                system_prompt = prompt_value.strip()
                                logger.info(f"[AIReplay] 从 persona['{attr}'] 获取 system_prompt")
                                break
                
                # 最后尝试从 conversation 直接获取
                if not system_prompt and conversation:
                    for attr in ("system_prompt", "prompt"):
                        if hasattr(conversation, attr):
                            prompt_value = getattr(conversation, attr, None)
                            if isinstance(prompt_value, str) and prompt_value.strip():
                                system_prompt = prompt_value.strip()
                                logger.info(f"[AIReplay] 从 conversation.{attr} 获取 system_prompt")
                                break
            
            if not system_prompt:
                logger.warning(f"[AIReplay] 未能获取任何 system_prompt，将使用空值")

            # 获取完整上下文（使用新的安全方法，传入已获取的 conversation 对象）
            contexts: List[Dict] = []
            try:
                # 传入已获取的 conversation 对象，优先从它获取历史
                contexts = await self._safe_get_full_contexts(umo, conversation)
                
                # 限制历史条数
                if contexts and hist_n > 0:
                    contexts = contexts[-hist_n:]
                
                logger.info(f"[AIReplay] 为 {umo} 获取到 {len(contexts)} 条上下文")
            except Exception as e:
                logger.error(f"[AIReplay] 获取上下文时出错: {e}")
                contexts = []

            # 获取自定义提示词列表
            custom_prompts = self.cfg.get("custom_prompts") or []
            
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

            # 调试模式：显示完整上下文（已可删除）
            if self.cfg.get("debug_mode", False):
                logger.info(f"[AIReplay] ========== 调试模式开始 ==========")
                logger.info(f"[AIReplay] 用户: {umo}")
                logger.info(f"[AIReplay] 系统提示词长度: {len(system_prompt) if system_prompt else 0} 字符")
                if system_prompt:
                    logger.info(f"[AIReplay] 系统提示词前100字符: {system_prompt[:100]}...")
                else:
                    logger.warning(f"[AIReplay] ⚠️ 警告：system_prompt 为空！")
                logger.info(f"[AIReplay] 用户提示词: {prompt}")
                logger.info(f"[AIReplay] 上下文历史共 {len(contexts)} 条:")
                if contexts:
                    for i, ctx in enumerate(contexts):
                        role = ctx.get("role", "unknown")
                        content = ctx.get("content", "")
                        logger.info(f"[AIReplay]   [{i+1}] {role}: {content[:100]}{'...' if len(content) > 100 else ''}")
                else:
                    logger.warning(f"[AIReplay] ⚠️ 警告：上下文为空！这会导致AI无法记住之前的对话")
                logger.info(f"[AIReplay] ========== 调试模式结束 ==========")

            # 调用 LLM（注意：参数名是 contexts 复数！！！）
            llm_resp = await provider.text_chat(
                prompt=prompt,
                contexts=contexts,  # ← 修复：使用 contexts（复数）。
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
        """
        发送纯文本消息到指定会话，并记录到插件的历史缓存
        
        参数：
            umo: 统一消息来源（会话标识）
            text: 要发送的文本内容
            
        功能：
        1. 构造消息链（MessageChain）
        2. 通过 context.send_message 发送消息
        3. 将消息记录到插件的轻量历史缓存（作为 assistant 角色）
        
        注意：
        - 这里记录的历史仅供降级使用（当conversation_manager无法获取历史时）
        - 历史缓存使用 deque(maxlen=32)，会自动丢弃最旧的消息
        """
        try:
            chain = MessageChain().message(text)
            await self.context.send_message(umo, chain)
        except Exception as e:
            logger.error(f"[AIReplay] send_message error({umo}): {e}")

    async def terminate(self):
        """
        插件卸载/停用时的清理方法
        
        功能：
        1. 停止后台调度循环任务（_scheduler_loop）
        2. 根据插件是卸载还是停用，执行不同的清理策略：
           
           卸载（检测到插件文件不存在）：
           - 清除所有用户配置（重置为默认值）
           - 删除所有数据文件（state.json, reminders.json）
           - 删除数据目录（如果为空）
           
           停用（插件文件仍存在）：
           - 仅保存当前状态到磁盘
           - 保留所有配置和数据
        
        注意：
        - 这个方法在 AstrBot 卸载/停用插件时自动调用
        - 卸载检测可能不可靠（文件可能还在磁盘上），建议在WebUI提供明确的清理选项
        """
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
