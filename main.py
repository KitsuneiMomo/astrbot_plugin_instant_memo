import json
import os
import time
import asyncio
import uuid
import re
from datetime import datetime, timedelta
from typing import Optional

from astrbot.api import logger
from astrbot.api.all import *
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import StarTools  # 导入标准的 StarTools 模块

def get_next_daily_timestamp(time_str: str) -> float:
    """根据 HH:MM 获取下一次的绝对时间戳（如果是今天已过，则为明天）"""
    try:
        parts = time_str.strip().split(":")
        hour = int(parts[0])
        minute = int(parts[1])
    except Exception:
        hour, minute = 12, 0  # 默认兜底
    
    now = datetime.now()
    target_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target_dt <= now:
        target_dt += timedelta(days=1)
    return target_dt.timestamp()

def get_next_workday_timestamp(time_str: str) -> float:
    """根据 HH:MM 获取下一次工作日（周一至周五）的绝对时间戳。若当天已过或为周末，则顺延到下一个工作日。"""
    try:
        parts = time_str.strip().split(":")
        hour = int(parts[0])
        minute = int(parts[1])
    except Exception:
        hour, minute = 12, 0  # 默认兜底

    now = datetime.now()
    target_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target_dt <= now:
        target_dt += timedelta(days=1)
    # weekday(): 周一=0 ... 周五=4, 周六=5, 周日=6。跳过周末
    while target_dt.weekday() >= 5:
        target_dt += timedelta(days=1)
    return target_dt.timestamp()

@register("astrbot_plugin_instant_memo", "kitsuneimomo", "AI自我备忘录与主动定时提醒插件", "1.2")
class AIMemoPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config or {}
        self.poll_interval = int(self.config.get("poll_interval", 15))
        self.allow_global_memo = bool(self.config.get("allow_global_memo", True))
        self.trigger_mode = self.config.get("trigger_mode", "tool")
        
        # XML 模式下的正则匹配器与属性提取器
        self.memo_tag_pattern = re.compile(r'<ai_memo\s+([^>]*?)(?:/>|>(.*?)</ai_memo>)', re.DOTALL | re.IGNORECASE)
        self.attr_pattern = re.compile(r'(\w+)\s*=\s*(?:"([^"]*?)"|\'([^\']*?)\')')
        self._index_to_item = {}

        # 协程安全同步锁，防止高并发时 JSON 读写冲突和文件损坏
        self.lock = asyncio.Lock()

        # 规范化：使用 AstrBot 官方标准的持久化目录，防止重装/热更新时丢失数据 [已修正]
        self.data_dir = str(StarTools.get_data_dir("astrbot_plugin_instant_memo"))
        if not os.path.exists(self.data_dir):
            os.makedirs(self.data_dir, exist_ok=True)
            
        self.data_file = os.path.join(self.data_dir, "memos.json")
        self.data = {}
        self._load_data()
        
        # 在 __init__ 中直接启动后台异步轮询任务是正确的
        self.poll_task = asyncio.create_task(self._polling_loop())

        # 注册前端 Web API
        context.register_web_api(
            "/astrbot_plugin_instant_memo/get_data",
            self.web_get_data,
            ["GET"],
            "获取备忘录条目与当前设置"
        )
        context.register_web_api(
            "/astrbot_plugin_instant_memo/save_config",
            self.web_save_config,
            ["POST"],
            "保存备忘录插件配置"
        )
        context.register_web_api(
            "/astrbot_plugin_instant_memo/add_item",
            self.web_add_item,
            ["POST"],
            "手动添加备忘条目"
        )
        context.register_web_api(
            "/astrbot_plugin_instant_memo/update_item",
            self.web_update_item,
            ["POST"],
            "手动修改备忘条目"
        )
        context.register_web_api(
            "/astrbot_plugin_instant_memo/delete_item",
            self.web_delete_item,
            ["POST"],
            "删除指定备忘条目"
        )

    def on_config_update(self, config: dict):
        """热更新插件配置"""
        if config and config is not self.config:
            for k, v in config.items():
                self.config[k] = v
        self.poll_interval = int(self.config.get("poll_interval", 15))
        self.allow_global_memo = bool(self.config.get("allow_global_memo", True))
        self.trigger_mode = self.config.get("trigger_mode", "tool")
        logger.info(f"[InstantMemo] 配置已更新，当前触发模式为: {self.trigger_mode}")

    def terminate(self):
        """生命周期结束时，安全取消后台轮询协程防止报错"""
        if hasattr(self, "poll_task") and self.poll_task:
            self.poll_task.cancel()

    def _load_data(self):
        """加载 JSON 数据 (初始化时同步加载)"""
        if os.path.exists(self.data_file):
            try:
                with open(self.data_file, "r", encoding="utf-8") as f:
                    loaded_data = json.load(f)
                    if isinstance(loaded_data, dict):
                        self.data = loaded_data
            except Exception as e:
                logger.error(f"[InstantMemo] 读取数据文件失败，将使用默认空数据: {e}")
                
        # 基础数据表初始化
        self.data.setdefault("status_memos", {})
        self.data.setdefault("tasks", {})
        self.data.setdefault("keyword_triggers", {})
        
        # 兼容层数据迁移
        if "active_tasks" in self.data:
            old_tasks = self.data.pop("active_tasks")
            for t_id, o_task in old_tasks.items():
                self.data["tasks"][t_id] = {
                    "type": "one_off",
                    "task_description": o_task.get("task_description", "定时任务"),
                    "target_umo": o_task.get("target_umo", ""),
                    "context_history_limit": 5,
                    "scheduled_time": "15",
                    "trigger_timestamp": o_task.get("trigger_timestamp", time.time() + 300),
                    "status": "pending",
                    "generated_message": o_task.get("exact_message_to_send", ""),
                    "last_run_timestamp": 0.0
                }
            self._save_data_sync()
            logger.info("[InstantMemo] 检测到旧版本数据，已自动迁移至新版任务管理中。")

        if not os.path.exists(self.data_file):
            self._save_data_sync()

    def _save_data_sync(self):
        """仅供初始化或无事件循环时使用的同步保存"""
        try:
            with open(self.data_file, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=4)
        except Exception as e:
            logger.error(f"[InstantMemo] 保存数据文件失败: {e}")

    async def _save_data(self):
        """异步非阻塞的保存方式，避免并发 IO 阻塞主事件循环"""
        async with self.lock:
            def save():
                try:
                    with open(self.data_file, "w", encoding="utf-8") as f:
                        json.dump(self.data, f, ensure_ascii=False, indent=4)
                except Exception as e:
                    logger.error(f"[InstantMemo] 保存数据文件失败: {e}")
            await asyncio.to_thread(save)

    def _get_all_items(self) -> list:
        """获取所有状态备忘录、定时任务和关键词搭话，排序并返回"""
        items = []
        
        memos_data = self.data.get("status_memos", {})
        for m_id, memo in sorted(memos_data.items(), key=lambda x: x[1].get("expire_timestamp", 0)):
            items.append({"type": "status_memo", "key": m_id, "data": memo})
            
        tasks_data = self.data.get("tasks", {})
        for t_id, task in sorted(tasks_data.items(), key=lambda x: x[1].get("trigger_timestamp", 0)):
            items.append({"type": "task", "key": t_id, "data": task})
            
        triggers_data = self.data.get("keyword_triggers", {})
        for tg_id, trigger in sorted(triggers_data.items(), key=lambda x: x[1].get("keyword", "")):
            items.append({"type": "keyword_trigger", "key": tg_id, "data": trigger})
            
        return items

    def _get_item_by_index(self, index_str: str):
        """通过序号（如 1）或 8位/全量 UUID 获取备忘录/任务/搭话数据"""
        if hasattr(self, "_index_to_item") and index_str in self._index_to_item:
            item_type, key = self._index_to_item[index_str]
            if item_type == "status_memo" and key in self.data.get("status_memos", {}):
                return item_type, key, self.data["status_memos"][key]
            elif item_type == "task" and key in self.data.get("tasks", {}):
                return item_type, key, self.data["tasks"][key]
            elif item_type == "keyword_trigger" and key in self.data.get("keyword_triggers", {}):
                return item_type, key, self.data["keyword_triggers"][key]

        items = self._get_all_items()
        
        try:
            idx = int(index_str) - 1
            if 0 <= idx < len(items):
                item = items[idx]
                return item["type"], item["key"], item["data"]
        except ValueError:
            pass
            
        index_str_clean = index_str.strip()
        for item in items:
            key = item["key"]
            if key == index_str_clean or key.startswith(index_str_clean):
                return item["type"], key, item["data"]
                
        return None, None, None

    @filter.command("memo")
    async def memo_cmd(self, event: AstrMessageEvent):
        """备忘录、定时任务和关键词搭话管理指令"""
        text = event.message_str.strip()
        if text.startswith("/memo"):
            text = text[5:].strip()
        elif text.startswith("memo"):
            text = text[4:].strip()
            
        parts = text.split(None, 3)
        action = parts[0].lower() if parts else "list"
        
        if action in ["list", "show", "列出", "列表"]:
            items = self._get_all_items()
            self._index_to_item = {str(i + 1): (item["type"], item["key"]) for i, item in enumerate(items)}
            
            if not items:
                yield event.plain_result("🍵 目前没有任何备忘录、定时任务或关键词搭话。")
                return
                
            memos = []
            tasks = []
            triggers = []
            
            for i, item in enumerate(items):
                idx_str = f"[{i + 1}]"
                data = item["data"]
                key = item["key"]
                short_id = key[:8]
                
                if item["type"] == "status_memo":
                    expire_time = data.get("expire_timestamp", 0)
                    remaining = int((expire_time - time.time()) / 60)
                    remaining_str = f"{remaining} 分钟后过期" if remaining > 0 else "已过期"
                    scope = "全局" if data.get("target_umo") == "GLOBAL" else "当前会话"
                    memos.append(f"{idx_str} ID: {short_id} | 内容: {data.get('content')} ({remaining_str}, 作用域: {scope})")
                    
                elif item["type"] == "task":
                    t_type = data.get("type", "one_off")
                    type_cn = {"one_off": "单次", "daily": "每日", "workday": "工作日", "interval": "间隔"}
                    trigger_t = data.get("trigger_timestamp", 0)
                    time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(trigger_t))
                    status_cn = {"pending": "等待生成", "generating": "正在生成", "ready": "就绪", "failed": "失败"}
                    tasks.append(f"{idx_str} ID: {short_id} | [{type_cn.get(t_type, t_type)}] 描述: {data.get('task_description')} (下一次触发: {time_str}, 状态: {status_cn.get(data.get('status'), '未知')})")
                    
                elif item["type"] == "keyword_trigger":
                    scope = "全局" if data.get("is_global") or data.get("target_umo") == "GLOBAL" else "当前会话"
                    triggers.append(f"{idx_str} ID: {short_id} | 关键词: \"{data.get('keyword')}\" -> 设定: {data.get('task_description')} (作用域: {scope})")
                    
            result_parts = ["📋 === AI 备忘录/任务/监听 列表 ==="]
            if memos:
                result_parts.append("📌 【临时人设/状态备忘录】\n" + "\n".join(memos))
            if tasks:
                result_parts.append("⏰ 【定时提醒与周期计划】\n" + "\n".join(tasks))
            if triggers:
                result_parts.append("🔑 【关键词主动监听搭话】\n" + "\n".join(triggers))
                
            result_parts.append("\n💡 提示：使用 `/memo del <序号>` 删除条目，`/memo edit <序号> <属性> <新值>` 修改属性。")
            yield event.plain_result("\n\n".join(result_parts))
            return
            
        elif action in ["del", "delete", "删除", "remove", "rm"]:
            if len(parts) < 2:
                yield event.plain_result("❌ 用法：/memo del <序号>")
                return
                
            index_or_id = parts[1]
            item_type, key, data = self._get_item_by_index(index_or_id)
            if not key:
                yield event.plain_result(f"❌ 未找到序号/ID 为 '{index_or_id}' 的备忘录/任务/搭话。")
                return
                
            if item_type == "status_memo":
                content = self.data["status_memos"].pop(key)["content"]
                await self._save_data()
                yield event.plain_result(f"✅ 已成功删除状态备忘录：'{content}'。")
            elif item_type == "task":
                desc = self.data["tasks"].pop(key)["task_description"]
                await self._save_data()
                yield event.plain_result(f"✅ 已成功删除定时任务：'{desc}'。")
            elif item_type == "keyword_trigger":
                keyword = self.data["keyword_triggers"].pop(key)["keyword"]
                await self._save_data()
                yield event.plain_result(f"✅ 已成功删除关键词 '{keyword}' 的搭话监听。")
            return
            
        elif action in ["edit", "modify", "修改", "update"]:
            if len(parts) < 4:
                yield event.plain_result(
                    "❌ 用法：/memo edit <序号> <属性> <新值>\n\n"
                    "💡 支持修改的属性：\n"
                    "- 状态备忘录：content(内容), expire(时间/分钟), global(是否全局)\n"
                    "- 定时任务：desc(描述), value(触发时间), type(类型:one_off/daily/workday/interval)\n"
                    "- 关键词搭话：keyword(触发词), desc(回复设定), global(是否全局)"
                )
                return
                
            index_or_id = parts[1]
            field = parts[2].lower()
            val = parts[3].strip()
            
            item_type, key, data = self._get_item_by_index(index_or_id)
            if not key:
                yield event.plain_result(f"❌ 未找到序号/ID 为 '{index_or_id}' 的备忘录/任务/搭话。")
                return
                
            if item_type == "status_memo":
                if field in ["content", "内容"]:
                    old_val = data["content"]
                    data["content"] = val
                    await self._save_data()
                    yield event.plain_result(f"✅ 已将状态备忘录内容从 '{old_val}' 修改为 '{val}'。")
                elif field in ["expire", "time", "时间", "过期时间"]:
                    try:
                        mins = int(val)
                        data["expire_timestamp"] = time.time() + mins * 60
                        await self._save_data()
                        yield event.plain_result(f"✅ 已更新状态备忘录时间，将在 {mins} 分钟后过期。")
                    except ValueError:
                        yield event.plain_result("❌ 时间参数必须是整数（分钟数）。")
                elif field in ["global", "全局"]:
                    is_global = val.lower() in ["true", "y", "yes", "1", "是"]
                    data["target_umo"] = "GLOBAL" if is_global else event.unified_msg_origin
                    await self._save_data()
                    yield event.plain_result(f"✅ 已将状态备忘录作用域修改为：{'全局' if is_global else '当前会话'}。")
                else:
                    yield event.plain_result(f"❌ 状态备忘录不支持修改属性 '{field}'。可修改属性: content, expire, global")
                    
            elif item_type == "task":
                if field in ["desc", "description", "描述", "内容"]:
                    old_val = data["task_description"]
                    data["task_description"] = val
                    await self._save_data()
                    yield event.plain_result(f"✅ 已将定时任务描述从 '{old_val}' 修改为 '{val}'。")
                elif field in ["value", "time", "时间"]:
                    t_type = data.get("type", "one_off")
                    current_time = time.time()
                    trigger_time = 0.0
                    if t_type == "one_off":
                        try:
                            mins = int(val)
                            trigger_time = current_time + mins * 60
                        except ValueError:
                            yield event.plain_result("❌ 错误：对于单次定时(one_off)，时间值必须为整数（分钟数）。")
                            return
                    elif t_type == "daily":
                        if ":" not in val:
                            yield event.plain_result("❌ 错误：对于每日定时(daily)，时间值必须是 HH:MM 格式，例如 '12:00'。")
                            return
                        trigger_time = get_next_daily_timestamp(val)
                    elif t_type == "workday":
                        if ":" not in val:
                            yield event.plain_result("❌ 错误：对于工作日定时(workday)，时间值必须是 HH:MM 格式，例如 '09:00'。")
                            return
                        trigger_time = get_next_workday_timestamp(val)
                    elif t_type == "interval":
                        try:
                            mins = int(val)
                            trigger_time = current_time + mins * 60
                        except ValueError:
                            yield event.plain_result("❌ 错误：对于周期循环(interval)，时间值必须为整数（分钟数）。")
                            return
                    
                    data["scheduled_time"] = val
                    data["trigger_timestamp"] = trigger_time
                    data["status"] = "pending"
                    data["generated_message"] = ""
                    await self._save_data()
                    yield event.plain_result(f"✅ 已将定时任务触发参数修改为 '{val}'，并已重新排程。")
                elif field in ["type", "类型"]:
                    val_clean = val.lower().strip()
                    if val_clean not in ["one_off", "daily", "workday", "interval"]:
                        yield event.plain_result("❌ 错误：类型必须是 'one_off'、'daily'、'workday' 或 'interval'。")
                        return
                    data["type"] = val_clean
                    val_s = data.get("scheduled_time", "")
                    current_time = time.time()
                    try:
                        if val_clean == "one_off":
                            mins = int(val_s)
                            trigger_time = current_time + mins * 60
                        elif val_clean == "daily":
                            trigger_time = get_next_daily_timestamp(val_s)
                        elif val_clean == "workday":
                            trigger_time = get_next_workday_timestamp(val_s)
                        elif val_clean == "interval":
                            mins = int(val_s)
                            trigger_time = current_time + mins * 60
                        data["trigger_timestamp"] = trigger_time
                        data["status"] = "pending"
                        data["generated_message"] = ""
                    except Exception:
                        yield event.plain_result(f"⚠️ 类型已修改为 '{val_clean}'，但由于现有的时间值 '{val_s}' 不兼容，请修改 time/value 属性。")
                    await self._save_data()
                    yield event.plain_result(f"✅ 已将任务类型修改为 '{val_clean}'。")
                else:
                    yield event.plain_result(f"❌ 定时任务不支持修改属性 '{field}'。可修改属性: desc, value, type")
                    
            elif item_type == "keyword_trigger":
                if field in ["keyword", "关键词", "触发词"]:
                    old_val = data["keyword"]
                    data["keyword"] = val
                    await self._save_data()
                    yield event.plain_result(f"✅ 已将监听关键词从 '{old_val}' 修改为 '{val}'。")
                elif field in ["desc", "description", "描述", "设定"]:
                    old_val = data["task_description"]
                    data["task_description"] = val
                    await self._save_data()
                    yield event.plain_result(f"✅ 已将接话语气描述从 '{old_val}' 修改为 '{val}'。")
                elif field in ["global", "全局"]:
                    is_global = val.lower() in ["true", "y", "yes", "1", "是"]
                    data["is_global"] = is_global
                    data["target_umo"] = "GLOBAL" if is_global else event.unified_msg_origin
                    await self._save_data()
                    yield event.plain_result(f"✅ 已将关键词监听作用域修改为：{'全局' if is_global else '当前会话'}。")
                else:
                    yield event.plain_result(f"❌ 关键词搭话不支持修改属性 '{field}'。可修改属性: keyword, desc, global")
            return
        else:
            yield event.plain_result(
                "📋 === AI 备忘录管理系统 ===\n\n"
                "💡 可用指令：\n"
                "- /memo 或 /memo list : 列出所有备忘录、定时任务与搭话监听器\n"
                "- /memo del <序号> : 删除指定的条目\n"
                "- /memo edit <序号> <属性> <新值> : 修改指定条目的属性\n\n"
                "💡 可修改属性说明：\n"
                "- 状态备忘录：content, expire, global\n"
                "- 定时任务：desc, value, type(one_off/daily/workday/interval)\n"
                "- 关键词搭话：keyword, desc, global"
            )
            return

    async def _get_umo_history(self, umo: str, limit: int) -> list:
        try:
            conv_mgr = self.context.conversation_manager
            curr_cid = await conv_mgr.get_curr_conversation_id(umo)
            if not curr_cid:
                return []
            conversation = await conv_mgr.get_conversation(umo, curr_cid)
            if not conversation or not conversation.history:
                return []
            
            if isinstance(conversation.history, str):
                try:
                    history_list = json.loads(conversation.history)
                except Exception:
                    history_list = []
            elif isinstance(conversation.history, list):
                history_list = conversation.history
            else:
                history_list = []
                
            if not history_list:
                return []
                
            normalized = []
            for msg in history_list[-limit:]:
                content = msg.get("content", "")
                if isinstance(content, list):
                    text_parts = []
                    for part in content:
                        if isinstance(part, dict):
                            text_parts.append(part.get("text", ""))
                        else:
                            text_parts.append(str(part))
                    content = "".join(text_parts)
                normalized.append({
                    "role": msg.get("role", "user"),
                    "content": str(content)
                })
            return normalized
        except Exception as e:
            logger.error(f"[InstantMemo] 获取 UMO {umo} 历史会话失败: {e}")
            return []

    # ========================== 工具 1：状态/人设备忘录 ==========================
    
    @llm_tool(name="set_status_memo")
    async def set_status_memo(self, event: AstrMessageEvent, content: str, minutes_later: int, is_global: bool) -> str:
        if not self._is_allowed(event, "add", "status_memo"):
            return "[后台隐蔽消息] 新增状态备忘的操作已被管理员禁用，或在当前群聊中未启用该功能。请【严格隐秘】，保持当前人设正常回复用户，绝不能暴露或承认此设置的存在。"

        if is_global and not self.allow_global_memo:
            is_global = False
            
        memo_id = str(uuid.uuid4())
        expire_time = time.time() + minutes_later * 60
        target_umo = "GLOBAL" if is_global else event.unified_msg_origin

        self.data["status_memos"][memo_id] = {
            "content": content,
            "expire_timestamp": expire_time,
            "target_umo": target_umo
        }
        await self._save_data()
        
        target_str = "全局" if is_global else "当前会话"
        return f"[后台隐蔽消息] 已成功记录{target_str}状态备忘录。请【严格隐秘存储】，完全以人设身份正常回复用户，【严禁】在回复中向用户暴露备忘录内容、备忘ID或输出系统确认话术！"

    @llm_tool(name="delete_status_memo")
    async def delete_status_memo(self, event: AstrMessageEvent, memo_id: str) -> str:
        if not self._is_allowed(event, "delete", "status_memo"):
            return "[后台隐蔽消息] 删除状态备忘的操作已被管理员禁用。请保持人设回复用户。"

        m_id_clean = memo_id.strip()
        memos_data = self.data.setdefault("status_memos", {})
        
        if m_id_clean in memos_data:
            content = memos_data.pop(m_id_clean)["content"]
            await self._save_data()
            return f"[后台隐蔽消息] 已成功删除状态备忘录：'{content}'。请保持人设回复用户。"
            
        matched_id = None
        for real_id in memos_data:
            if real_id.startswith(m_id_clean):
                matched_id = real_id
                break
        if matched_id:
            content = memos_data.pop(matched_id)["content"]
            await self._save_data()
            return f"[后台隐蔽消息] 已成功删除状态备忘录：'{content}'。请保持人设回复用户。"
            
        return f"[后台隐蔽消息] 未找到 ID 为 '{memo_id}' 的状态备忘录。"

    # ========================== 工具 2：主动定时任务 ==========================

    @llm_tool(name="set_scheduled_task")
    async def set_scheduled_task(self, event: AstrMessageEvent, task_description: str, task_type: str, schedule_value: str, context_history_limit: int = 5) -> str:
        """
        设立一个定时提醒或循环任务（主动给用户发消息）。
        
        Args:
            task_description (string): 定时提醒的任务具体内容，AI需要根据此描述给用户发送相应的提醒消息。
            task_type (string): 任务类型。必须为 'one_off' (单次定时), 'daily' (每日定时), 'workday' (工作日定时) 或 'interval' (周期循环)。
            schedule_value (string): 触发时间参数。
                - 当 task_type 为 'one_off' 时，value 必须为整数代表分钟数，如 '30' 表示30分钟后。
                - 当 task_type 为 'daily' 时，value 必须是 HH:MM 格式，例如 '12:30' 表示每天中午12点30分。
                - 当 task_type 为 'workday' 时，value 必须是 HH:MM 格式，例如 '09:00' 表示每个工作日（周一至周五）上午9点触发，周末不触发。
                - 当 task_type 为 'interval' 时，value 必须为整数代表循环间隔分钟数，如 '60' 表示每60分钟一次。
            context_history_limit (int, optional): 触发时携带的前文历史条数，默认 5。
        """
        if not self._is_allowed(event, "add", "task"):
            return "[后台隐蔽消息] 创建定时提醒任务的操作已被管理员禁用，或在当前群聊中未启用该功能。请【严格隐秘】，保持当前人设正常回复用户，绝不能暴露或承认此设置的存在。"

        if context_history_limit == 5:
            context_history_limit = self.config.get("context_history_limit", 5)

        task_id = str(uuid.uuid4())
        current_time = time.time()
        
        t_type = task_type.strip().lower()
        if t_type not in ["one_off", "daily", "workday", "interval"]:
            return "错误：task_type 参数必须是 'one_off'、'daily'、'workday' 或 'interval' 中的一个。"
            
        trigger_time = 0.0
        val = schedule_value.strip()
        
        def parse_minutes(value_str: str) -> Optional[int]:
            match = re.search(r'\d+', value_str)
            return int(match.group(0)) if match else None

        if t_type == "one_off":
            try:
                mins = int(val)
                trigger_time = current_time + mins * 60
            except ValueError:
                mins = parse_minutes(val)
                if mins is not None:
                    trigger_time = current_time + mins * 60
                    val = str(mins)
                else:
                    return "错误：对于 one_off 单次定时，schedule_value 必须为整数代表分钟数（如 '30'）。"
        elif t_type == "daily":
            if ":" not in val:
                match = re.search(r'(\d{1,2})[:：点\s](\d{2})', val)
                if match:
                    hours = int(match.group(1))
                    minutes = int(match.group(2))
                    if 0 <= hours <= 23 and 0 <= minutes <= 59:
                        val = f"{hours:02d}:{minutes:02d}"
                else:
                    match_single = re.search(r'^(\d{1,2})(?:点|时)?$', val)
                    if match_single:
                        hours = int(match_single.group(1))
                        if 0 <= hours <= 23:
                            val = f"{hours:02d}:00"
            
            if ":" not in val:
                return "错误：对于 daily 每日定时，schedule_value 必须是 HH:MM 格式，例如 '12:00'。"
            trigger_time = get_next_daily_timestamp(val)
        elif t_type == "workday":
            if ":" not in val:
                match = re.search(r'(\d{1,2})[:：点\s](\d{2})', val)
                if match:
                    hours = int(match.group(1))
                    minutes = int(match.group(2))
                    if 0 <= hours <= 23 and 0 <= minutes <= 59:
                        val = f"{hours:02d}:{minutes:02d}"
                else:
                    match_single = re.search(r'^(\d{1,2})(?:点|时)?$', val)
                    if match_single:
                        hours = int(match_single.group(1))
                        if 0 <= hours <= 23:
                            val = f"{hours:02d}:00"

            if ":" not in val:
                return "错误：对于 workday 工作日定时，schedule_value 必须是 HH:MM 格式，例如 '09:00'。"
            trigger_time = get_next_workday_timestamp(val)
        elif t_type == "interval":
            try:
                mins = int(val)
                trigger_time = current_time + mins * 60
            except ValueError:
                mins = parse_minutes(val)
                if mins is not None:
                    trigger_time = current_time + mins * 60
                    val = str(mins)
                else:
                    return "错误：对于 interval 间隔循环，schedule_value 必须为整数代表间隔分钟（如 '60'）。"
                
        target_umo = event.unified_msg_origin
        
        self.data.setdefault("tasks", {})[task_id] = {
            "type": t_type,
            "task_description": task_description,
            "target_umo": target_umo,
            "context_history_limit": int(context_history_limit),
            "scheduled_time": val,
            "trigger_timestamp": trigger_time,
            "status": "pending",
            "generated_message": "",
            "last_run_timestamp": 0.0
        }
        await self._save_data()
        
        type_cn = {"one_off": "单次定时", "daily": "每日定时", "workday": "工作日定时", "interval": "周期性时间间隔循环"}
        time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(trigger_time))
        return f"[后台隐蔽消息] 已成功设立主动任务（{type_cn[t_type]}）。将在 {time_str} 首次触发。请完全以人设身份正常回复用户，【严禁】在回复中向用户暴露任务内容或系统确认话术！"

    @llm_tool(name="delete_active_task")
    async def delete_active_task(self, event: AstrMessageEvent, task_id: str) -> str:
        if not self._is_allowed(event, "delete", "task"):
            return "[后台隐蔽消息] 删除定时提醒任务的操作已被管理员禁用。请保持人设回复用户。"

        t_id_clean = task_id.strip()
        tasks_data = self.data.setdefault("tasks", {})
        
        if t_id_clean in tasks_data:
            desc = tasks_data.pop(t_id_clean)["task_description"]
            await self._save_data()
            return f"[后台隐蔽消息] 已成功删除定时任务：'{desc}'。请保持人设回复用户。"
            
        matched_id = None
        for real_id in tasks_data:
            if real_id.startswith(t_id_clean):
                matched_id = real_id
                break
        if matched_id:
            desc = tasks_data.pop(matched_id)["task_description"]
            await self._save_data()
            return f"[后台隐蔽消息] 已成功删除定时任务：'{desc}'。请保持人设回复用户。"
            
        return f"[后台隐蔽消息] 未找到 ID 为 '{task_id}' 的定时任务。"

    @llm_tool(name="set_active_reminder")
    async def set_active_reminder(self, event: AstrMessageEvent, task_description: str, minutes_later: int, exact_message_to_send: str = "") -> str:
        """兼容保留的 API 工具"""
        return await self.set_scheduled_task(
            event=event,
            task_description=task_description,
            task_type="one_off",
            schedule_value=str(minutes_later),
            context_history_limit=5
        )

    # ========================== 工具 3：关键词搭话唤醒 ==========================

    @llm_tool(name="set_keyword_trigger_task")
    async def set_keyword_trigger_task(self, event: AstrMessageEvent, keyword: str, task_description: str, context_history_limit: int = 5, is_global: bool = True) -> str:
        if not self._is_allowed(event, "add", "keyword_trigger"):
            return "[后台隐蔽消息] 创建关键词搭话监听的操作已被管理员禁用，或在当前群聊中未启用该功能。请【严格隐秘】，保持当前人设正常回复用户，绝不能暴露或承认此设置的存在。"

        if context_history_limit == 5:
            context_history_limit = self.config.get("context_history_limit", 5)

        trigger_id = str(uuid.uuid4())
        if is_global and not self.allow_global_memo:
            is_global = False
            
        target_umo = "GLOBAL" if is_global else event.unified_msg_origin
        
        self.data.setdefault("keyword_triggers", {})[trigger_id] = {
            "keyword": keyword.strip(),
            "task_description": task_description,
            "target_umo": target_umo,
            "context_history_limit": int(context_history_limit),
            "is_global": is_global
        }
        await self._save_data()
        
        scope_str = "全局" if is_global else "当前会话"
        return f"[后台隐蔽消息] 已成功注册关键词监听任务（{scope_str}，触发词: '{keyword}'）。请完全以人设身份正常回复用户，【严禁】泄漏监听设置。"

    @llm_tool(name="delete_keyword_trigger")
    async def delete_keyword_trigger(self, event: AstrMessageEvent, trigger_id: str) -> str:
        if not self._is_allowed(event, "delete", "keyword_trigger"):
            return "[后台隐蔽消息] 删除关键词搭话监听的操作已被管理员禁用。请保持人设回复用户。"

        tg_id_clean = trigger_id.strip()
        triggers = self.data.setdefault("keyword_triggers", {})
        
        if tg_id_clean in triggers:
            keyword = triggers.pop(tg_id_clean)["keyword"]
            await self._save_data()
            return f"[后台隐蔽消息] 已成功删除关键词 '{keyword}' 的搭话监听器。"
            
        matched_id = None
        for real_id in triggers:
            if real_id.startswith(tg_id_clean):
                matched_id = real_id
                break
        if matched_id:
            keyword = triggers.pop(matched_id)["keyword"]
            await self._save_data()
            return f"[后台隐蔽消息] 已成功删除关键词 '{keyword}' 的搭话监听器。"
            
        return f"[后台隐蔽消息] 未找到 ID 为 '{trigger_id}' 的关键词搭话监听任务。"

    # ========================== 核心事件拦截：注入 System Prompt ==========================

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        if self.trigger_mode == "xml" and req.func_tool:
            for tool_name in ["set_status_memo", "delete_status_memo", "set_scheduled_task", "delete_active_task", "set_active_reminder", "set_keyword_trigger_task", "delete_keyword_trigger"]:
                req.func_tool.remove_tool(tool_name)
            if req.func_tool.empty():
                req.func_tool = None

        umo = event.unified_msg_origin if event else ""
        current_time = time.time()

        to_delete_memos = []
        active_memos = []
        memos_data = self.data.setdefault("status_memos", {})
        for m_id, memo in list(memos_data.items()):
            if current_time > memo["expire_timestamp"]:
                to_delete_memos.append(m_id)
                continue
            if memo["target_umo"] == "GLOBAL" or memo["target_umo"] == umo:
                time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(memo['expire_timestamp']))
                active_memos.append(f" - [备忘录ID: {m_id[:8]}] [有效期至 {time_str}]: {memo['content']}")

        if to_delete_memos:
            for m_id in to_delete_memos:
                memos_data.pop(m_id, None)
            await self._save_data()

        active_tasks_info = []
        tasks_data = self.data.setdefault("tasks", {})
        to_delete_tasks = []
        
        for t_id, task in list(tasks_data.items()):
            t_type = task.get("type", "one_off")
            trigger_t = task.get("trigger_timestamp", 0)
            
            if t_type == "one_off" and current_time > (trigger_t + 7200):
                to_delete_tasks.append(t_id)
                continue
                
            if task.get("target_umo") == umo:
                type_cn = {"one_off": "单次定时", "daily": "每日定时", "workday": "工作日定时", "interval": "周期性循环"}
                time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(trigger_t))
                status_cn = {"pending": "等待生成", "generating": "正在生成", "ready": "就绪发送", "failed": "执行失败"}
                active_tasks_info.append(
                    f" - [任务ID: {t_id[:8]}] [{type_cn[t_type]} - 触发时刻: {time_str}] [当前状态: {status_cn.get(task.get('status'), '未知')}]: {task.get('task_description')}"
                )
                
        if to_delete_tasks:
            for t_id in to_delete_tasks:
                tasks_data.pop(t_id, None)
            await self._save_data()

        active_triggers_info = []
        triggers_data = self.data.setdefault("keyword_triggers", {})
        for tg_id, trigger in list(triggers_data.items()):
            tg_umo = trigger.get("target_umo", "GLOBAL")
            if tg_umo == "GLOBAL" or tg_umo == umo:
                scope_str = "全局" if tg_umo == "GLOBAL" else "当前会话"
                active_triggers_info.append(
                    f" - [触发器ID: {tg_id[:8]}] [作用域: {scope_str}] [触发关键词: '{trigger.get('keyword')}']: {trigger.get('task_description')}"
                )

        memo_sections = []
        if active_memos:
            memo_sections.append("【临时人设/状态备忘录（请严格遵循扮演）】\n" + "\n".join(active_memos))
        if active_tasks_info:
            memo_sections.append("【正在后台运行的主动定时提醒与周期循环计划】\n" + "\n".join(active_tasks_info))
        if active_triggers_info:
            memo_sections.append("【已注册的群聊关键词主动监听搭话列表】\n" + "\n".join(active_triggers_info))

        memo_text_parts = []
        if memo_sections:
            memo_text_parts.append(
                "=== [系统动态备忘录与运行状态提示] ===\n" + 
                "\n\n".join(memo_sections) + 
                (
                    "\n\n【说明】您可以调用对应的工具传入条目 ID 取消并移除不再需要的状态备忘、计划任务或搭话触发器。"
                    if self.trigger_mode == "tool"
                    else "\n\n【说明】您可以使用对应的 XML 标签传入条目 ID 取消并移除不再需要的状态备忘、计划任务或搭话触发器。"
                ) +
                "\n======================================="
            )

        privacy_instruction = (
            "=== [核心保密与人设原则] ===\n"
            "1. AI备忘录/定时任务/关键词触发器是你后台的【私密记事本】。\n"
            "2. 当你在备忘录中记下信息（如猜数字游戏的目标数字、暗号、秘密状态等）时，【绝对不要】在给用户的回复中泄漏备忘的具体内容、答案、条目ID，也不要输出'已为您记下'、'成功设置备忘'等机械确认话术！\n"
            "3. 记下备忘后，请完全以你的人设角色自然地回复用户（例如玩猜数字时，你只需自然回答：'好呀，我已经记好数字了，你来猜吧！'，决不能在回复中把数字说出来）。\n"
            "======================================="
        )
        memo_text_parts.append(privacy_instruction)

        if self.trigger_mode == "xml":
            xml_instruction = (
                "=== [AI备忘录系统 XML 交互指南] ===\n"
                "你可以通过输出特殊的 XML 标签 `<ai_memo>` 管理备忘录、定时任务和关键词搭话任务。标签会被系统提取并自动从回复中擦除，用户无法看到。\n"
                "格式如下：\n"
                "1. 状态备忘录：<ai_memo action=\"set_status\" minutes_later=\"分钟数\" is_global=\"true|false\">状态/隐秘内容</ai_memo>\n"
                "2. 删状态备忘：<ai_memo action=\"delete_status\" memo_id=\"备忘录ID或前8位短ID\" />\n"
                "3. 定时/循环：<ai_memo action=\"set_task\" type=\"one_off|daily|workday|interval\" value=\"具体数值或时间\" history_limit=\"条数\">任务设定描述</ai_memo>（注意：当 type 为 one_off 或 interval 时，value 必须为整数代表分钟数，如 \"30\"；当 type 为 daily 或 workday 时，value 必须为 HH:MM 格式，如 \"12:30\"，其中 workday 仅在周一至周五触发）\n"
                "4. 删定时任务：<ai_memo action=\"delete_task\" task_id=\"任务ID或前8位短ID\" />\n"
                "5. 监听关键词：<ai_memo action=\"set_keyword\" keyword=\"触发词\" history_limit=\"条数\" is_global=\"true|false\">接话语气和任务设定</ai_memo>\n"
                "6. 删关键监听：<ai_memo action=\"delete_keyword\" trigger_id=\"触发器ID或前8位短ID\" />\n"
                "================================================"
            )
            memo_text_parts.append(xml_instruction)

        if memo_text_parts:
            memo_text = "\n\n" + "\n\n".join(memo_text_parts)
            if req.system_prompt:
                req.system_prompt += memo_text
            else:
                req.system_prompt = memo_text

    @filter.on_decorating_result()
    async def process_xml_memos(self, event: AstrMessageEvent):
        if self.trigger_mode != "xml":
            return

        result = event.get_result()
        if not result or not result.chain:
            return

        new_chain = []
        for comp in result.chain:
            if isinstance(comp, Plain):
                text = comp.text
                matches = list(self.memo_tag_pattern.finditer(text))
                if not matches:
                    new_chain.append(comp)
                    continue

                last_idx = 0
                parts = []
                for match in matches:
                    start, end = match.span()
                    if start > last_idx:
                        parts.append(text[last_idx:start])

                    attr_str = match.group(1) or ""
                    content = (match.group(2) or "").strip()

                    attrs = {}
                    for attr_match in self.attr_pattern.finditer(attr_str):
                        key = attr_match.group(1).lower()
                        val = attr_match.group(2) if attr_match.group(2) is not None else attr_match.group(3)
                        attrs[key] = val

                    action = attrs.get("action", "")

                    try:
                        ret_msg = await self._execute_xml_action(event, action, attrs, content)
                        logger.info(f"[InstantMemo] 后台静默执行 XML 备忘录动作 ({action}): {ret_msg}")
                    except Exception as e:
                        logger.error(f"[InstantMemo] XML action {action} failed: {e}")

                    last_idx = end

                if last_idx < len(text):
                    parts.append(text[last_idx:])

                cleaned_text = "".join(parts)
                if cleaned_text.strip():
                    comp.text = cleaned_text
                    new_chain.append(comp)
            else:
                new_chain.append(comp)

        result.chain = new_chain

    async def _execute_xml_action(self, event: AstrMessageEvent, action: str, attrs: dict, content: str) -> str:
        if action == "set_status":
            try:
                minutes_later = int(attrs.get("minutes_later", "60"))
            except ValueError:
                minutes_later = 60
            is_global = attrs.get("is_global", "false").lower() == "true"
            return await self.set_status_memo(event, content, minutes_later, is_global)
            
        elif action == "delete_status":
            return await self.delete_status_memo(event, attrs.get("memo_id", "").strip())
            
        elif action == "set_task":
            task_type = attrs.get("type", "one_off").strip()
            schedule_value = attrs.get("value", "").strip()
            try:
                history_limit = int(attrs.get("history_limit", "5"))
            except ValueError:
                history_limit = 5
            return await self.set_scheduled_task(event, content, task_type, schedule_value, history_limit)
            
        elif action == "delete_task":
            return await self.delete_active_task(event, attrs.get("task_id", "").strip())
            
        elif action == "set_keyword":
            keyword = attrs.get("keyword", "").strip()
            try:
                history_limit = int(attrs.get("history_limit", "5"))
            except ValueError:
                history_limit = 5
            is_global = attrs.get("is_global", "true").lower() != "false"
            return await self.set_keyword_trigger_task(event, keyword, content, history_limit, is_global)
            
        elif action == "delete_keyword":
            return await self.delete_keyword_trigger(event, attrs.get("trigger_id", "").strip())
            
        else:
            raise ValueError(f"未知的动作: {action}")

    # ========================== 核心事件拦截：关键词捕获 ==========================

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10000)
    async def on_message(self, event: AstrMessageEvent):
        msg_str = event.message_str.strip() if event.message_str else ""
        if not msg_str:
            return
            
        umo = event.unified_msg_origin
        triggers = self.data.setdefault("keyword_triggers", {})
        
        sender_id = event.message_obj.sender.user_id if event.message_obj.sender else None
        if sender_id and sender_id == event.message_obj.self_id:
            return
            
        for tg_id, trigger in list(triggers.items()):
            keyword_str = trigger.get("keyword", "")
            if not keyword_str:
                continue
                
            import re
            keywords = [k.strip() for k in re.split(r'[\s,，;；]+', keyword_str) if k.strip()]
            
            matched_keyword = None
            for kw in keywords:
                if kw in msg_str:
                    matched_keyword = kw
                    break
                    
            if matched_keyword:
                target_umo = trigger.get("target_umo", "GLOBAL")
                if target_umo != "GLOBAL" and target_umo != umo:
                    continue
                    
                trigger_copy = trigger.copy()
                trigger_copy["matched_keyword"] = matched_keyword
                asyncio.create_task(self._execute_keyword_trigger(event, trigger_copy))
                event.stop_event()
                break

    async def _execute_keyword_trigger(self, event: AstrMessageEvent, trigger: dict):
        umo = event.unified_msg_origin
        desc = trigger["task_description"]
        keyword = trigger.get("matched_keyword", trigger.get("keyword", ""))
        history_limit = trigger.get("context_history_limit", 5)
        
        try:
            history_contexts = []
            if history_limit > 0:
                history_contexts = await self._get_umo_history(umo, history_limit)
                
            provider_id = await self.context.get_current_chat_provider_id(umo=umo)
            
            system_prompt = (
                f"你是一个智能搭话助手。当前有对话者发送的消息中触发了预设搭话关键词 '{keyword}'。\n"
                f"你的本次插话任务要求：{desc}\n"
                "请根据当前的上下文聊天历史（如果有），生成一段契合人设、生动自然的搭话内容。\n"
                "【重要格式限制】：直接输出发送的原话本身。严禁带有分析、前缀、双引号包裹或动作补充。"
            )
            
            prompt = f"对话中已出现触发词: '{keyword}'。\n"
            if history_contexts:
                prompt += "【会话历史记录（按时间先后顺序）】:\n"
                for msg in history_contexts:
                    role = "用户" if msg.get("role") == "user" else "AI"
                    prompt += f"{role}: {msg.get('content')}\n"
                prompt += "\n"
            prompt += "请生成要发送的原话消息正文："
            
            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt=system_prompt
            )
            
            generated_text = llm_resp.completion_text.strip().strip('"').strip("'")
            if generated_text:
                msg = MessageChain().message(generated_text)
                await self.context.send_message(umo, msg)
                logger.info(f"[InstantMemo] 关键词 '{keyword}' 搭话生成并推送成功")
                
        except Exception as e:
            logger.error(f"[InstantMemo] 关键词唤醒执行异常, keyword: {keyword}, err: {e}")

    # ========================== 后台轮询与计划任务管理器 ==========================

    async def _polling_loop(self):
        try:
            while True:
                await asyncio.sleep(self.poll_interval)
                try:
                    await self._check_active_tasks()
                except Exception as e:
                    logger.error(f"[InstantMemo] 轮询周期执行异常: {e}")
        except asyncio.CancelledError:
            pass

    async def _check_active_tasks(self):
        current_time = time.time()
        tasks_data = self.data.setdefault("tasks", {})
        
        for t_id, task in list(tasks_data.items()):
            status = task.get("status", "pending")
            trigger_time = task.get("trigger_timestamp", 0)
            
            if status == "pending" and current_time >= (trigger_time - 60):
                task["status"] = "generating"
                await self._save_data()
                asyncio.create_task(self._generate_task_message(t_id, task))
                
            elif status == "ready" and current_time >= trigger_time:
                await self._send_task_message(t_id, task)

    async def _reschedule_task(self, task_id: str, task: dict):
        current_time = time.time()
        tasks_data = self.data.setdefault("tasks", {})
        
        if task_id not in tasks_data:
            return
            
        t_type = task.get("type", "one_off")
        if t_type == "one_off":
            tasks_data.pop(task_id, None)
        elif t_type == "daily":
            task["trigger_timestamp"] = get_next_daily_timestamp(task["scheduled_time"])
            task["status"] = "pending"
            task["generated_message"] = ""
            task["last_run_timestamp"] = current_time
        elif t_type == "workday":
            task["trigger_timestamp"] = get_next_workday_timestamp(task["scheduled_time"])
            task["status"] = "pending"
            task["generated_message"] = ""
            task["last_run_timestamp"] = current_time
        elif t_type == "interval":
            try:
                interval_mins = int(task["scheduled_time"])
            except ValueError:
                interval_mins = 60
            next_trigger = current_time + interval_mins * 60
            while next_trigger <= current_time:
                next_trigger += interval_mins * 60
            task["trigger_timestamp"] = next_trigger
            task["status"] = "pending"
            task["generated_message"] = ""
            task["last_run_timestamp"] = current_time
            
        await self._save_data()

    async def _generate_task_message(self, task_id: str, task: dict):
        umo = task["target_umo"]
        desc = task["task_description"]
        history_limit = task.get("context_history_limit", 5)
        
        try:
            history_contexts = []
            try:
                if history_limit > 0:
                    history_contexts = await self._get_umo_history(umo, history_limit)
            except Exception as e:
                logger.error(f"[InstantMemo] 获取会话历史记录异常: {e}")

            max_retries = 3
            retry_delay = 5
            
            for attempt in range(1, max_retries + 1):
                try:
                    provider_id = await self.context.get_current_chat_provider_id(umo=umo)
                    
                    system_prompt = (
                        "你是一个定时任务消息处理器。\n"
                        "目标：请你根据【计划描述和人设目标】以及【最新会话历史记录】（如果有），生成一段即将主动推送给该用户的自然问候或计划内容文本。\n"
                        "【重要格式限制】：请直接给出要发送的原话内容。严禁带有分析过程、前缀注释、表情操作描述或双引号。"
                    )
                    
                    prompt = f"【当前计划目标描述】:\n{desc}\n\n"
                    if history_contexts:
                        prompt += "【会话历史记录（按时间先后顺序，请参考最新话题）】:\n"
                        for msg in history_contexts:
                            role = "user" if msg.get("role") == "user" else "AI"
                            prompt += f"{role}: {msg.get('content')}\n"
                        prompt += "\n"
                    prompt += "请生成最合适的、将直接推送的原话正文："
                    
                    llm_resp = await self.context.llm_generate(
                        chat_provider_id=provider_id,
                        prompt=prompt,
                        system_prompt=system_prompt
                    )
                    
                    generated_text = llm_resp.completion_text.strip().strip('"').strip("'")
                    
                    if not generated_text or (generated_text == desc and len(desc) > 10):
                        raise ValueError("生成文本为空或与任务描述完全一致，可能未正确生成")
                    
                    task["generated_message"] = generated_text
                    task["status"] = "ready"
                    await self._save_data()
                    logger.info(f"[InstantMemo] 任务 {task_id} 动态内容渲染成功")
                    return
                    
                except Exception as e:
                    logger.warning(f"[InstantMemo] 任务 {task_id} 动态内容渲染失败 (第 {attempt}/{max_retries} 次尝试): {e}")
                    if attempt < max_retries:
                        await asyncio.sleep(retry_delay)
                    else:
                        raise e
                        
        except Exception as e:
            logger.error(f"[InstantMemo] 任务 {task_id} 动态内容渲染失败，重新调度: {e}")
            await self._reschedule_task(task_id, task)

    async def _send_task_message(self, task_id: str, task: dict):
        umo = task["target_umo"]
        msg_text = task.get("generated_message")
        
        try:
            if msg_text:
                msg = MessageChain().message(msg_text)
                await self.context.send_message(umo, msg)
                logger.info(f"[InstantMemo] 定时任务 {task_id} 已成功下发")
            else:
                logger.warning(f"[InstantMemo] 任务 {task_id} 没有就绪的生成消息，跳过发送。")
        except Exception as e:
            logger.error(f"[InstantMemo] 定时任务 {task_id} 下推异常, umo: {umo}, err: {e}")
        finally:
            await self._reschedule_task(task_id, task)

    def _is_allowed(self, event: AstrMessageEvent, action_type: str, item_type: str) -> bool:
        """
        检查是否允许操作。
        action_type: 'add', 'update', 'delete'
        item_type: 'status_memo', 'task', 'keyword_trigger'
        """
        if not event:
            return True
            
        # 1. 验证群聊限制
        group_id = event.get_group_id()
        if group_id:
            group_str = str(group_id)
            filter_mode = self.config.get("group_filter_mode", "all")
            group_list_str = self.config.get("group_list", "")
            
            # 解析群号列表
            import re
            configured_groups = set(re.split(r'[\s,，;；\n\r]+', group_list_str.strip()))
            configured_groups = {g for g in configured_groups if g}
            
            if filter_mode == "whitelist":
                if group_str not in configured_groups:
                    logger.warning(f"[InstantMemo] 群聊 {group_str} 不在白名单中，拒绝操作。")
                    return False
            elif filter_mode == "blacklist":
                if group_str in configured_groups:
                    logger.warning(f"[InstantMemo] 群聊 {group_str} 在黑名单中，拒绝操作。")
                    return False
                    
        # 2. 验证操作权限
        if action_type == "add" and not self.config.get("ai_allow_add", True):
            logger.warning("[InstantMemo] AI 新增操作被禁用。")
            return False
        if action_type == "update" and not self.config.get("ai_allow_update", True):
            logger.warning("[InstantMemo] AI 修改操作被禁用。")
            return False
        if action_type == "delete" and not self.config.get("ai_allow_delete", True):
            logger.warning("[InstantMemo] AI 删除操作被禁用。")
            return False
            
        # 3. 验证条目类型权限
        if item_type == "status_memo" and not self.config.get("enable_status_memo_ai", True):
            logger.warning("[InstantMemo] 状态备忘录已被 AI 禁用。")
            return False
        if item_type == "task" and not self.config.get("enable_task_ai", True):
            logger.warning("[InstantMemo] 定时任务已被 AI 禁用。")
            return False
        if item_type == "keyword_trigger" and not self.config.get("enable_keyword_trigger_ai", True):
            logger.warning("[InstantMemo] 关键词搭话已被 AI 禁用。")
            return False
            
        return True

    async def web_get_data(self):
        from quart import jsonify
        response = {"status": "success", "config": self.config}
        response.update(self.data)
        return jsonify(response)

    async def web_save_config(self):
        from quart import request, jsonify
        try:
            data = await request.json
            if not data or not isinstance(data, dict):
                return jsonify({"status": "error", "message": "Invalid request body"}), 400
            for k, v in data.items():
                self.config[k] = v
            if hasattr(self.config, "save_config"):
                self.config.save_config()
            self.on_config_update(self.config)
            return jsonify({"status": "success"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    async def web_add_item(self):
        from quart import request, jsonify
        try:
            req_data = await request.json
            if not req_data or not isinstance(req_data, dict):
                return jsonify({"status": "error", "message": "Invalid request body"}), 400
                
            item_type = req_data.get("type")
            if item_type == "status_memo":
                content = req_data.get("content", "").strip()
                minutes = int(req_data.get("minutes_later", 60))
                target_umo = req_data.get("target_umo", "GLOBAL").strip()
                memo_id = str(uuid.uuid4())
                expire_time = time.time() + minutes * 60
                
                self.data.setdefault("status_memos", {})[memo_id] = {
                    "content": content,
                    "expire_timestamp": expire_time,
                    "target_umo": target_umo
                }
                await self._save_data()
                return jsonify({"status": "success", "id": memo_id})
                
            elif item_type == "task":
                task_desc = req_data.get("task_description", "").strip()
                task_type = req_data.get("task_type", "one_off").strip().lower()
                schedule_val = req_data.get("schedule_value", "").strip()
                context_history_limit = int(req_data.get("context_history_limit", 5))
                target_umo = req_data.get("target_umo", "GLOBAL").strip()
                
                if task_type not in ["one_off", "daily", "workday", "interval"]:
                    return jsonify({"status": "error", "message": "Invalid task type"}), 400
                    
                trigger_time = 0.0
                current_time = time.time()
                if task_type == "one_off":
                    try:
                        mins = int(schedule_val)
                        trigger_time = current_time + mins * 60
                    except ValueError:
                        return jsonify({"status": "error", "message": "schedule_value 必须为分钟数"}), 400
                elif task_type == "daily":
                    if ":" not in schedule_val:
                        return jsonify({"status": "error", "message": "schedule_value 必须是 HH:MM 格式"}), 400
                    trigger_time = get_next_daily_timestamp(schedule_val)
                elif task_type == "workday":
                    if ":" not in schedule_val:
                        return jsonify({"status": "error", "message": "schedule_value 必须是 HH:MM 格式"}), 400
                    trigger_time = get_next_workday_timestamp(schedule_val)
                elif task_type == "interval":
                    try:
                        mins = int(schedule_val)
                        trigger_time = current_time + mins * 60
                    except ValueError:
                        return jsonify({"status": "error", "message": "schedule_value 必须为分钟数"}), 400
                        
                task_id = str(uuid.uuid4())
                self.data.setdefault("tasks", {})[task_id] = {
                    "type": task_type,
                    "task_description": task_desc,
                    "target_umo": target_umo,
                    "context_history_limit": context_history_limit,
                    "scheduled_time": schedule_val,
                    "trigger_timestamp": trigger_time,
                    "status": "pending",
                    "generated_message": "",
                    "last_run_timestamp": 0.0
                }
                await self._save_data()
                return jsonify({"status": "success", "id": task_id})
                
            elif item_type == "keyword_trigger":
                keyword = req_data.get("keyword", "").strip()
                task_desc = req_data.get("task_description", "").strip()
                context_history_limit = int(req_data.get("context_history_limit", 5))
                target_umo = req_data.get("target_umo", "GLOBAL").strip()
                
                trigger_id = str(uuid.uuid4())
                self.data.setdefault("keyword_triggers", {})[trigger_id] = {
                    "keyword": keyword,
                    "task_description": task_desc,
                    "target_umo": target_umo,
                    "context_history_limit": context_history_limit
                }
                await self._save_data()
                return jsonify({"status": "success", "id": trigger_id})
            
            else:
                return jsonify({"status": "error", "message": "Unknown item type"}), 400
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    async def web_update_item(self):
        from quart import request, jsonify
        try:
            req_data = await request.json
            if not req_data or not isinstance(req_data, dict):
                return jsonify({"status": "error", "message": "Invalid request body"}), 400
                
            item_type = req_data.get("type")
            item_id = req_data.get("id")
            update_fields = req_data.get("data", {})
            
            if not item_id:
                return jsonify({"status": "error", "message": "Missing ID"}), 400
                
            if item_type == "status_memo":
                memos = self.data.setdefault("status_memos", {})
                if item_id not in memos:
                    return jsonify({"status": "error", "message": "Item not found"}), 404
                    
                memo = memos[item_id]
                if "content" in update_fields:
                    memo["content"] = update_fields["content"].strip()
                if "minutes_later" in update_fields:
                    try:
                        minutes = int(update_fields["minutes_later"])
                        memo["expire_timestamp"] = time.time() + minutes * 60
                    except ValueError:
                        pass
                if "target_umo" in update_fields:
                    memo["target_umo"] = update_fields["target_umo"].strip()
                    
                await self._save_data()
                return jsonify({"status": "success"})
                
            elif item_type == "task":
                tasks = self.data.setdefault("tasks", {})
                if item_id not in tasks:
                    return jsonify({"status": "error", "message": "Item not found"}), 404
                    
                task = tasks[item_id]
                if "task_description" in update_fields:
                    task["task_description"] = update_fields["task_description"].strip()
                if "target_umo" in update_fields:
                    task["target_umo"] = update_fields["target_umo"].strip()
                if "context_history_limit" in update_fields:
                    try:
                        task["context_history_limit"] = int(update_fields["context_history_limit"])
                    except ValueError:
                        pass
                if "schedule_value" in update_fields or "task_type" in update_fields:
                    t_type = update_fields.get("task_type", task.get("type")).strip().lower()
                    schedule_val = update_fields.get("schedule_value", task.get("scheduled_time")).strip()
                    
                    if t_type not in ["one_off", "daily", "workday", "interval"]:
                        return jsonify({"status": "error", "message": "Invalid task type"}), 400

                    trigger_time = 0.0
                    current_time = time.time()
                    if t_type == "one_off":
                        try:
                            mins = int(schedule_val)
                            trigger_time = current_time + mins * 60
                        except ValueError:
                            return jsonify({"status": "error", "message": "schedule_value 必须为分钟数"}), 400
                    elif t_type == "daily":
                        if ":" not in schedule_val:
                            return jsonify({"status": "error", "message": "schedule_value 必须是 HH:MM 格式"}), 400
                        trigger_time = get_next_daily_timestamp(schedule_val)
                    elif t_type == "workday":
                        if ":" not in schedule_val:
                            return jsonify({"status": "error", "message": "schedule_value 必须是 HH:MM 格式"}), 400
                        trigger_time = get_next_workday_timestamp(schedule_val)
                    elif t_type == "interval":
                        try:
                            mins = int(schedule_val)
                            trigger_time = current_time + mins * 60
                        except ValueError:
                            return jsonify({"status": "error", "message": "schedule_value 必须为分钟数"}), 400
                            
                    task["type"] = t_type
                    task["scheduled_time"] = schedule_val
                    task["trigger_timestamp"] = trigger_time
                    task["status"] = "pending"
                    task["generated_message"] = ""
                    
                await self._save_data()
                return jsonify({"status": "success"})
                
            elif item_type == "keyword_trigger":
                triggers = self.data.setdefault("keyword_triggers", {})
                if item_id not in triggers:
                    return jsonify({"status": "error", "message": "Item not found"}), 404
                    
                trigger = triggers[item_id]
                if "keyword" in update_fields:
                    trigger["keyword"] = update_fields["keyword"].strip()
                if "task_description" in update_fields:
                    trigger["task_description"] = update_fields["task_description"].strip()
                if "context_history_limit" in update_fields:
                    try:
                        trigger["context_history_limit"] = int(update_fields["context_history_limit"])
                    except ValueError:
                        pass
                if "target_umo" in update_fields:
                    trigger["target_umo"] = update_fields["target_umo"].strip()
                    
                await self._save_data()
                return jsonify({"status": "success"})
                
            else:
                return jsonify({"status": "error", "message": "Unknown item type"}), 400
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    async def web_delete_item(self):
        from quart import request, jsonify
        try:
            req_data = await request.json
            if not req_data or not isinstance(req_data, dict):
                return jsonify({"status": "error", "message": "Invalid request body"}), 400
                
            item_type = req_data.get("type")
            item_id = req_data.get("id")
            
            if not item_id:
                return jsonify({"status": "error", "message": "Missing ID"}), 400
                
            if item_type == "status_memo":
                memos = self.data.setdefault("status_memos", {})
                if item_id in memos:
                    memos.pop(item_id)
                    await self._save_data()
                    return jsonify({"status": "success"})
                return jsonify({"status": "error", "message": "Item not found"}), 404
                
            elif item_type == "task":
                tasks = self.data.setdefault("tasks", {})
                if item_id in tasks:
                    tasks.pop(item_id)
                    await self._save_data()
                    return jsonify({"status": "success"})
                return jsonify({"status": "error", "message": "Item not found"}), 404
                
            elif item_type == "keyword_trigger":
                triggers = self.data.setdefault("keyword_triggers", {})
                if item_id in triggers:
                    triggers.pop(item_id)
                    await self._save_data()
                    return jsonify({"status": "success"})
                return jsonify({"status": "error", "message": "Item not found"}), 404
                
            else:
                return jsonify({"status": "error", "message": "Unknown item type"}), 400
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500