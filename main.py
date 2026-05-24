import asyncio
import json
import os
from datetime import datetime
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register


PLUGIN_NAME = "dialog_fork"
TIME_NAME_FORMAT = "%Y-%m-%d_%H:%M:%S.%f"
TIME_DISPLAY_FORMAT = "%Y-%m-%d %H:%M:%S"


@register(PLUGIN_NAME, "Nartsam", "对话分叉与跳转插件", "0.1.0")
class DialogForkPlugin(Star):
    """Manage per-chat fork points by mapping them to AstrBot conversations."""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_dir = str(StarTools.get_data_dir(PLUGIN_NAME))
        os.makedirs(self.data_dir, exist_ok=True)
        self.data_path = os.path.join(self.data_dir, "forkpoints.json")
        self._lock = asyncio.Lock()
        self._data = self._load_data()
        logger.info("dialog_fork 插件已加载。")

    def _load_data(self) -> dict[str, Any]:
        if not os.path.exists(self.data_path):
            return {"v": 1, "s": {}}
        try:
            with open(self.data_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                data.setdefault("v", 1)
                data.setdefault("s", {})
                if isinstance(data["s"], dict):
                    return data
        except Exception as e:
            logger.error(f"dialog_fork 加载数据失败: {e}")
        return {"v": 1, "s": {}}

    def _save_data(self) -> None:
        tmp_path = self.data_path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp_path, self.data_path)
        except Exception as e:
            logger.error(f"dialog_fork 保存数据失败: {e}")
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    def _session(self, umo: str) -> dict[str, Any]:
        sessions = self._data.setdefault("s", {})
        session = sessions.setdefault(umo, {})
        session.setdefault("f", {})
        return session

    def _drop_empty_session(self, umo: str) -> None:
        session = self._data.get("s", {}).get(umo)
        if isinstance(session, dict) and not session.get("f"):
            self._data.get("s", {}).pop(umo, None)

    @staticmethod
    def _now_ms() -> int:
        return int(datetime.now().timestamp() * 1000)

    @staticmethod
    def _format_name_time(ms: int | None = None) -> str:
        dt = datetime.now() if ms is None else datetime.fromtimestamp(ms / 1000)
        return dt.strftime(TIME_NAME_FORMAT)[:-3]

    @staticmethod
    def _format_display_time(ms: int) -> str:
        dt = datetime.fromtimestamp(ms / 1000)
        return f"{dt.strftime(TIME_DISPLAY_FORMAT)}.{dt.microsecond // 1000:03d}"

    @staticmethod
    def _parse_history(history_raw: Any) -> list[dict[str, Any]]:
        if isinstance(history_raw, list):
            return [item for item in history_raw if isinstance(item, dict)]
        if not history_raw:
            return []
        try:
            data = json.loads(history_raw)
        except (TypeError, json.JSONDecodeError):
            return []
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]

    @staticmethod
    def _is_command_content(content: Any) -> bool:
        if isinstance(content, str):
            return content.strip().startswith("/")
        return False

    @classmethod
    def _dialog_count(cls, history: list[dict[str, Any]]) -> int:
        count = 0
        for item in history:
            if item.get("role") != "user":
                continue
            if cls._is_command_content(item.get("content")):
                continue
            count += 1
        return count

    @staticmethod
    def _reply_with_note(text: str, note: str) -> str:
        if note:
            return f"{text}。分叉点说明：{note}"
        return text

    async def _conversation_exists(self, umo: str, cid: str) -> bool:
        conv = await self.context.conversation_manager.get_conversation(umo, cid)
        return conv is not None

    async def _prune_invalid_locked(self, umo: str) -> list[str]:
        session = self._session(umo)
        forks = session["f"]
        removed: list[str] = []
        for name, item in list(forks.items()):
            cid = item.get("c") if isinstance(item, dict) else None
            if not cid or not await self._conversation_exists(umo, cid):
                forks.pop(name, None)
                removed.append(name)
        if removed:
            self._drop_empty_session(umo)
            self._save_data()
        return removed

    async def _delete_fork_conversations_locked(self, umo: str) -> int:
        session = self._session(umo)
        forks = session.get("f", {})
        current_cid = await self.context.conversation_manager.get_curr_conversation_id(umo)
        deleted = 0
        for item in list(forks.values()):
            cid = item.get("c") if isinstance(item, dict) else None
            if not cid or cid == current_cid:
                continue
            try:
                await self.context.conversation_manager.delete_conversation(umo, cid)
                deleted += 1
            except Exception as e:
                logger.warning(f"dialog_fork 删除分叉 conversation 失败 ({cid}): {e}")
        forks.clear()
        self._drop_empty_session(umo)
        self._save_data()
        return deleted

    def _reset_commands(self) -> set[str]:
        raw = self.config.get("reset_commands", ["/reset", "/new"])
        if not isinstance(raw, list):
            raw = ["/reset", "/new"]
        commands: set[str] = set()
        for item in raw:
            if not isinstance(item, str):
                continue
            command = item.strip()
            if command:
                bare = command
                while bare and not bare[0].isalnum() and bare[0] != "_":
                    bare = bare[1:]
                commands.add(command)
                if bare:
                    commands.add(bare)
                    commands.add(f"/{bare}")
        return commands

    @staticmethod
    def _message_command(message: Any) -> str:
        if not isinstance(message, str):
            return ""
        return message.strip().split(maxsplit=1)[0] if message.strip() else ""

    @filter.command("fork")
    async def fork(
        self,
        event: AstrMessageEvent,
        forkpoint_name: str = "",
        note: str = "",
        extra: str = "",
    ):
        """创建一个对话分叉点。"""
        if extra:
            yield event.plain_result("不合法的命令格式，正确格式为：/fork [分叉点名称] [注释]")
            return

        umo = event.unified_msg_origin
        name = forkpoint_name or self._format_name_time()

        async with self._lock:
            response = ""
            session = self._session(umo)
            forks = session["f"]
            if name in forks:
                created_at = self._format_display_time(int(forks[name].get("t", self._now_ms())))
                response = f"无法创建分叉点“{name}”，同名分叉点已于{created_at}创建"
            else:
                cm = self.context.conversation_manager
                curr_cid = await cm.get_curr_conversation_id(umo)
                if not curr_cid:
                    response = "无法创建分叉点，当前没有正在使用的对话"
                else:
                    conv = await cm.get_conversation(umo, curr_cid)
                    if not conv:
                        response = "无法创建分叉点，当前对话不存在或已失效"
                    else:
                        history = self._parse_history(conv.history)
                        count = self._dialog_count(history)
                        new_cid = await cm.new_conversation(
                            umo,
                            event.get_platform_id(),
                            content=history,
                            title=f"fork:{name}",
                            persona_id=getattr(conv, "persona_id", None),
                        )
                        await cm.switch_conversation(umo, curr_cid)

                        item = {"c": new_cid, "t": self._now_ms()}
                        if note:
                            item["n"] = note
                        forks[name] = item
                        self._save_data()
                        response = self._reply_with_note(
                            f"成功创建分叉点“{name}”，共包含{count}段对话",
                            note,
                        )

        yield event.plain_result(response)

    @filter.command("jump")
    async def jump(self, event: AstrMessageEvent, forkpoint_name: str = "", extra: str = ""):
        """跳转到一个对话分叉点。"""
        if not forkpoint_name or extra:
            yield event.plain_result("不合法的命令格式，正确格式为：/jump <分叉点名称>")
            return

        umo = event.unified_msg_origin
        async with self._lock:
            session = self._session(umo)
            forks = session["f"]
            item = forks.get(forkpoint_name)
            if not item:
                response = f"无法跳转到分叉点“{forkpoint_name}”，该分叉点不存在"
            else:
                cid = item.get("c")
                conv = await self.context.conversation_manager.get_conversation(umo, cid)
                if not conv:
                    forks.pop(forkpoint_name, None)
                    self._save_data()
                    response = f"无法跳转到分叉点“{forkpoint_name}”，该分叉点不存在或已失效"
                else:
                    await self.context.conversation_manager.switch_conversation(umo, cid)
                    history = self._parse_history(conv.history)
                    count = self._dialog_count(history)
                    note = item.get("n", "")
                    if count == 0:
                        text = f"成功跳转到分叉点“{forkpoint_name}”，创建该分叉点时未进行任何对话"
                    else:
                        text = f"成功跳转到分叉点“{forkpoint_name}”，当前共有{count}段对话"
                    response = self._reply_with_note(text, note)

        yield event.plain_result(response)

    @filter.command("forkpoint-rename")
    async def forkpoint_rename(
        self,
        event: AstrMessageEvent,
        old_name: str = "",
        new_name: str = "",
        extra: str = "",
    ):
        """重命名一个分叉点。"""
        if not old_name or not new_name or extra:
            yield event.plain_result(
                "不合法的命令格式，正确格式为：/forkpoint-rename <旧分叉点名> <新分叉点名>"
            )
            return

        umo = event.unified_msg_origin
        async with self._lock:
            forks = self._session(umo)["f"]
            if old_name not in forks:
                response = f"无法重命名分叉点“{old_name}”，该分叉点不存在"
            elif new_name in forks:
                created_at = self._format_display_time(int(forks[new_name].get("t", self._now_ms())))
                response = f"无法重命名为“{new_name}”，同名分叉点已于{created_at}创建"
            else:
                forks[new_name] = forks.pop(old_name)
                self._save_data()
                response = f"成功将分叉点“{old_name}”重命名为“{new_name}”"

        yield event.plain_result(response)

    @filter.command("forkpoint-list")
    async def forkpoint_list(self, event: AstrMessageEvent, extra: str = ""):
        """列出当前聊天窗口中的分叉点。"""
        if extra:
            yield event.plain_result("不合法的命令格式，正确格式为：/forkpoint-list")
            return

        umo = event.unified_msg_origin
        async with self._lock:
            await self._prune_invalid_locked(umo)
            forks = self._session(umo)["f"]
            rows = []
            for name, item in sorted(forks.items(), key=lambda pair: int(pair[1].get("t", 0))):
                created_at = self._format_display_time(int(item.get("t", 0)))
                note = item.get("n", "")
                rows.append(f"{name} {created_at} {note}".rstrip())

        if not rows:
            yield event.plain_result("当前对话中暂无分叉点")
            return
        yield event.plain_result("\n".join(rows))

    @filter.command("forkpoint-remove")
    async def forkpoint_remove(
        self,
        event: AstrMessageEvent,
        forkpoint_name: str = "",
        extra: str = "",
    ):
        """删除一个分叉点。"""
        if not forkpoint_name or extra:
            yield event.plain_result("不合法的命令格式，正确格式为：/forkpoint-remove <分叉点名称>")
            return

        umo = event.unified_msg_origin
        async with self._lock:
            forks = self._session(umo)["f"]
            item = forks.get(forkpoint_name)
            if not item:
                response = f"无法删除分叉点“{forkpoint_name}”，该分叉点不存在"
            else:
                target_cid = item.get("c")
                current_cid = await self.context.conversation_manager.get_curr_conversation_id(umo)
                if target_cid == current_cid:
                    response = f"无法删除分叉点“{forkpoint_name}”，你当前正处于该分叉点"
                else:
                    if target_cid:
                        try:
                            await self.context.conversation_manager.delete_conversation(umo, target_cid)
                        except Exception as e:
                            logger.warning(f"dialog_fork 删除分叉 conversation 失败 ({target_cid}): {e}")
                    forks.pop(forkpoint_name, None)
                    self._drop_empty_session(umo)
                    self._save_data()
                    response = f"成功删除分叉点“{forkpoint_name}”"

        yield event.plain_result(response)

    @filter.after_message_sent()
    async def clear_after_reset_command(self, event: AstrMessageEvent):
        """Clear fork points after configured reset/new commands have completed."""
        command = self._message_command(event.message_str)
        if command not in self._reset_commands():
            return

        umo = event.unified_msg_origin
        async with self._lock:
            deleted = await self._delete_fork_conversations_locked(umo)
        logger.info(f"dialog_fork 已在 {command} 后清空 {umo} 的分叉点，删除 {deleted} 条分叉对话。")
