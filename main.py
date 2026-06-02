import asyncio
import copy
import json
import os
from datetime import datetime
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register


PLUGIN_NAME = "dialog_fork"
OUTPUT_PREFIX = "[DialogFork插件]："
TIME_NAME_FORMAT = "%Y-%m-%d_%H:%M:%S.%f"
TIME_DISPLAY_FORMAT = "%Y-%m-%d %H:%M:%S"


@register(PLUGIN_NAME, "Nartsam", "对话分叉与跳转插件", "0.2.0")
class DialogForkPlugin(Star):
    """Manage immutable per-chat fork points stored as AstrBot conversations."""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_dir = str(StarTools.get_data_dir(PLUGIN_NAME))
        os.makedirs(self.data_dir, exist_ok=True)
        self.data_path = os.path.join(self.data_dir, "forkpoints.json")
        self._lock = asyncio.Lock()
        self._data = self._load_data()
        logger.info("dialog_fork 插件已加载")

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
        if isinstance(session, dict) and not session.get("f") and not session.get("w"):
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

    def _record_time_text(self, item: Any) -> str:
        if not isinstance(item, dict):
            return self._format_display_time(self._now_ms())
        try:
            ms = int(item.get("t", self._now_ms()))
        except (TypeError, ValueError):
            ms = self._now_ms()
        return self._format_display_time(ms)

    @staticmethod
    def _record_sort_time(item: Any) -> int:
        if not isinstance(item, dict):
            return 0
        try:
            return int(item.get("t", 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _parse_history(history_raw: Any) -> list[dict[str, Any]]:
        if isinstance(history_raw, list):
            filtered = [item for item in history_raw if isinstance(item, dict)]
            return copy.deepcopy(filtered)
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

    @staticmethod
    def _plain_result(event: AstrMessageEvent, text: str):
        return event.plain_result(f"{OUTPUT_PREFIX}{text}")

    @staticmethod
    def _snapshot_conversation_ids(forks: dict[str, Any]) -> set[str]:
        cids: set[str] = set()
        for item in forks.values():
            if not isinstance(item, dict):
                continue
            cid = item.get("c")
            if isinstance(cid, str) and cid:
                cids.add(cid)
        return cids

    async def _conversation_exists(self, umo: str, cid: str) -> bool:
        if not isinstance(cid, str) or not cid:
            return False
        conv = await self.context.conversation_manager.get_conversation(umo, cid)
        return conv is not None

    async def _record_exists_locked(self, umo: str, forks: dict[str, Any], name: str) -> bool | None:
        item = forks.get(name)
        if not item:
            return False
        if not isinstance(item, dict):
            forks.pop(name, None)
            self._drop_empty_session(umo)
            self._save_data()
            return False
        cid = item.get("c", "")
        try:
            exists = await self._conversation_exists(umo, cid)
        except Exception as e:
            logger.warning(f"dialog_fork 校验分叉 conversation 失败 ({cid}): {e}")
            return None
        if not exists:
            forks.pop(name, None)
            self._drop_empty_session(umo)
            self._save_data()
            return False
        return True

    async def _prune_invalid_locked(self, umo: str) -> list[str]:
        session = self._session(umo)
        forks = session["f"]
        removed: list[str] = []
        for name, item in list(forks.items()):
            cid = item.get("c") if isinstance(item, dict) else None
            if not cid:
                forks.pop(name, None)
                removed.append(name)
                continue
            try:
                exists = await self._conversation_exists(umo, cid)
            except Exception as e:
                logger.warning(f"dialog_fork 校验分叉 conversation 失败 ({cid}): {e}")
                continue
            if not exists:
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
        changed = False
        for name, item in list(forks.items()):
            cid = item.get("c") if isinstance(item, dict) else None
            if not cid:
                forks.pop(name, None)
                changed = True
                continue
            if cid == current_cid:
                forks.pop(name, None)
                changed = True
                continue
            try:
                conv = await self.context.conversation_manager.get_conversation(umo, cid)
                if not conv:
                    forks.pop(name, None)
                    changed = True
                    continue
                await self.context.conversation_manager.delete_conversation(umo, cid)
                forks.pop(name, None)
                deleted += 1
            except Exception as e:
                logger.warning(f"dialog_fork 删除分叉 conversation 失败 ({cid}): {e}")
                continue
            changed = True
        work_cid = session.pop("w", None)
        if work_cid:
            changed = True
            if work_cid != current_cid and work_cid not in self._snapshot_conversation_ids(forks):
                try:
                    conv = await self.context.conversation_manager.get_conversation(umo, work_cid)
                    if conv:
                        await self.context.conversation_manager.delete_conversation(umo, work_cid)
                except Exception as e:
                    logger.warning(f"dialog_fork 删除恢复工作 conversation 失败 ({work_cid}): {e}")
        self._drop_empty_session(umo)
        if changed:
            self._save_data()
        return deleted

    def _reset_command_names(self) -> set[str]:
        raw = self.config.get("reset_commands", ["/reset", "/new"])
        if not isinstance(raw, list):
            raw = ["/reset", "/new"]
        names: set[str] = set()
        for item in raw:
            if not isinstance(item, str):
                continue
            cmd = item.strip()
            if not cmd:
                continue
            if cmd[0] not in ("_",) and not cmd[0].isalnum():
                cmd = cmd[1:]
            if cmd:
                names.add(cmd)
        return names

    def _is_reset_event(self, event: AstrMessageEvent) -> bool:
        activated = event.get_extra("activated_handlers") or []
        if not activated:
            return False
        names = self._reset_command_names()
        for handler in activated:
            for f in getattr(handler, "event_filters", []):
                if getattr(f, "command_name", None) in names:
                    return True
        return False

    @filter.command("fork")
    async def fork(
        self,
        event: AstrMessageEvent,
        forkpoint_name: str = "",
        note: str = "",
        extra: str = "",
    ):
        """创建一个对话分叉点"""
        if extra:
            yield self._plain_result(event, "不合法的命令格式，正确格式为：/fork [分叉点名称] [注释]")
            return

        umo = event.unified_msg_origin
        name = forkpoint_name or self._format_name_time()

        async with self._lock:
            response = ""
            session = self._session(umo)
            forks = session["f"]
            if name in forks:
                exists = await self._record_exists_locked(umo, forks, name)
                if exists is None:
                    response = f"无法创建分叉点“{name}”，底层存档对话查询失败"
                elif exists:
                    created_at = self._record_time_text(forks[name])
                    response = f"无法创建分叉点“{name}”，同名分叉点已于{created_at}创建"
                else:
                    response = ""
            if not response:
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
                        new_cid = None
                        try:
                            new_cid = await cm.new_conversation(
                                umo,
                                event.get_platform_id(),
                                content=history,
                                title=f"fork:{name}",
                                persona_id=getattr(conv, "persona_id", None),
                            )
                            if not new_cid:
                                raise RuntimeError("new_conversation 返回了空 ID")
                            await cm.switch_conversation(umo, curr_cid)
                        except Exception as e:
                            logger.error(f"dialog_fork 创建分叉 conversation 失败: {e}")
                            if new_cid:
                                try:
                                    await cm.delete_conversation(umo, new_cid)
                                except Exception:
                                    pass
                            response = "无法创建分叉点，底层操作失败"
                        else:
                            item = {"c": new_cid, "t": self._now_ms()}
                            if note:
                                item["n"] = note
                            forks[name] = item
                            self._save_data()
                            response = self._reply_with_note(
                                f"成功创建分叉点“{name}”，共包含{count}段对话",
                                note,
                            )

        yield self._plain_result(event, response)

    @filter.command("jump")
    async def jump(self, event: AstrMessageEvent, forkpoint_name: str = "", extra: str = ""):
        """跳转到一个对话分叉点"""
        if not forkpoint_name or extra:
            yield self._plain_result(event, "不合法的命令格式，正确格式为：/jump <分叉点名称>")
            return

        umo = event.unified_msg_origin
        async with self._lock:
            session = self._session(umo)
            forks = session["f"]
            item = forks.get(forkpoint_name)
            if not item:
                response = f"无法跳转到分叉点“{forkpoint_name}”，该分叉点不存在"
            else:
                cid = item.get("c") if isinstance(item, dict) else None
                cm = self.context.conversation_manager
                if not isinstance(cid, str) or not cid:
                    snapshot_conv = None
                else:
                    try:
                        snapshot_conv = await cm.get_conversation(umo, cid)
                    except Exception as e:
                        logger.error(f"dialog_fork 读取分叉 conversation 失败 ({cid}): {e}")
                        snapshot_conv = "error"
                if snapshot_conv == "error":
                    response = f"无法跳转到分叉点“{forkpoint_name}”，底层操作失败"
                elif not snapshot_conv:
                    forks.pop(forkpoint_name, None)
                    self._drop_empty_session(umo)
                    self._save_data()
                    response = f"无法跳转到分叉点“{forkpoint_name}”，该分叉点不存在或已失效"
                else:
                    history = self._parse_history(snapshot_conv.history)
                    curr_cid = await cm.get_curr_conversation_id(umo)
                    if curr_cid:
                        curr_conv = await cm.get_conversation(umo, curr_cid)
                    else:
                        curr_conv = None

                    try:
                        if curr_conv and curr_cid not in self._snapshot_conversation_ids(forks):
                            await cm.update_conversation(
                                umo,
                                curr_cid,
                                history=history,
                                persona_id=getattr(snapshot_conv, "persona_id", None),
                            )
                        else:
                            work_cid = session.get("w")
                            work_conv = None
                            snapshot_cids = self._snapshot_conversation_ids(forks)
                            if isinstance(work_cid, str) and work_cid:
                                if work_cid not in snapshot_cids:
                                    work_conv = await cm.get_conversation(umo, work_cid)
                            if work_conv:
                                await cm.update_conversation(
                                    umo,
                                    work_cid,
                                    history=history,
                                    persona_id=getattr(snapshot_conv, "persona_id", None),
                                )
                                await cm.switch_conversation(umo, work_cid)
                            else:
                                work_cid = await cm.new_conversation(
                                    umo,
                                    event.get_platform_id(),
                                    content=history,
                                    title=f"jump:{forkpoint_name}",
                                    persona_id=getattr(snapshot_conv, "persona_id", None),
                                )
                                if not work_cid:
                                    raise RuntimeError("new_conversation 返回了空 ID")
                                session["w"] = work_cid
                                self._save_data()
                                await cm.switch_conversation(umo, work_cid)
                    except Exception as e:
                        logger.error(f"dialog_fork 跳转到分叉点失败 ({forkpoint_name}): {e}")
                        response = f"无法跳转到分叉点“{forkpoint_name}”，底层操作失败"
                    else:
                        count = self._dialog_count(history)
                        note = item.get("n", "")
                        if count == 0:
                            text = f"成功跳转到分叉点“{forkpoint_name}”，创建该分叉点时未进行任何对话"
                        else:
                            text = f"成功跳转到分叉点“{forkpoint_name}”，当前共有{count}段对话"
                        response = self._reply_with_note(text, note)

        yield self._plain_result(event, response)

    @filter.command("forkpoint-rename")
    async def forkpoint_rename(
        self,
        event: AstrMessageEvent,
        old_name: str = "",
        new_name: str = "",
        extra: str = "",
    ):
        """重命名一个分叉点"""
        if not old_name or not new_name or extra:
            yield self._plain_result(
                event,
                "不合法的命令格式，正确格式为：/forkpoint-rename <旧分叉点名> <新分叉点名>"
            )
            return

        umo = event.unified_msg_origin
        async with self._lock:
            response = ""
            forks = self._session(umo)["f"]
            item = forks.get(old_name)
            if not item:
                response = f"无法重命名分叉点“{old_name}”，该分叉点不存在"
            elif new_name in forks:
                exists = await self._record_exists_locked(umo, forks, new_name)
                if exists is None:
                    response = f"无法重命名为“{new_name}”，底层存档对话查询失败"
                elif exists:
                    created_at = self._record_time_text(forks[new_name])
                    response = f"无法重命名为“{new_name}”，同名分叉点已于{created_at}创建"
                else:
                    response = ""
            if not response:
                exists = await self._record_exists_locked(umo, forks, old_name)
                if exists is None:
                    response = f"无法重命名分叉点“{old_name}”，底层操作失败"
                else:
                    if not exists:
                        response = f"无法重命名分叉点“{old_name}”，该分叉点不存在或已失效"
                    else:
                        forks[new_name] = forks.pop(old_name)
                        self._save_data()
                        response = f"成功将分叉点“{old_name}”重命名为“{new_name}”"

        yield self._plain_result(event, response)

    @filter.command("forkpoint-list")
    async def forkpoint_list(self, event: AstrMessageEvent, extra: str = ""):
        """列出当前聊天窗口中的分叉点"""
        if extra:
            yield self._plain_result(event, "不合法的命令格式，正确格式为：/forkpoint-list")
            return

        umo = event.unified_msg_origin
        async with self._lock:
            await self._prune_invalid_locked(umo)
            forks = self._session(umo)["f"]
            rows = []
            for name, item in sorted(forks.items(), key=lambda pair: self._record_sort_time(pair[1])):
                created_at = self._record_time_text(item)
                note = item.get("n", "")
                line = f"名称：{name}，创建于：{created_at}"
                if note:
                    line += f"，备注：{note}"
                rows.append(line)

        if not rows:
            yield self._plain_result(event, "当前对话中暂无分叉点")
            return
        yield self._plain_result(event, "\n" + "\n".join(rows))

    @filter.command("forkpoint-remove")
    async def forkpoint_remove(
        self,
        event: AstrMessageEvent,
        forkpoint_name: str = "",
        extra: str = "",
    ):
        """删除一个分叉点"""
        if not forkpoint_name or extra:
            yield self._plain_result(event, "不合法的命令格式，正确格式为：/forkpoint-remove <分叉点名称>")
            return

        umo = event.unified_msg_origin
        async with self._lock:
            forks = self._session(umo)["f"]
            item = forks.get(forkpoint_name)
            if not item:
                response = f"无法删除分叉点“{forkpoint_name}”，该分叉点不存在"
            else:
                target_cid = item.get("c") if isinstance(item, dict) else None
                current_cid = await self.context.conversation_manager.get_curr_conversation_id(umo)
                if target_cid and target_cid == current_cid:
                    response = f"无法删除分叉点“{forkpoint_name}”，当前对话正在使用该分叉点的存档"
                else:
                    try:
                        exists = await self._conversation_exists(umo, target_cid)
                    except Exception as e:
                        logger.warning(f"dialog_fork 校验分叉 conversation 失败 ({target_cid}): {e}")
                        response = f"无法删除分叉点“{forkpoint_name}”，底层存档对话查询失败"
                    else:
                        if target_cid and exists:
                            try:
                                await self.context.conversation_manager.delete_conversation(umo, target_cid)
                            except Exception as e:
                                logger.warning(f"dialog_fork 删除分叉 conversation 失败 ({target_cid}): {e}")
                                response = f"无法删除分叉点“{forkpoint_name}”，底层存档对话删除失败"
                            else:
                                forks.pop(forkpoint_name, None)
                                self._drop_empty_session(umo)
                                self._save_data()
                                response = f"成功删除分叉点“{forkpoint_name}”"
                        else:
                            forks.pop(forkpoint_name, None)
                            self._drop_empty_session(umo)
                            self._save_data()
                            response = f"成功删除分叉点“{forkpoint_name}”"

        yield self._plain_result(event, response)

    @filter.after_message_sent()
    async def clear_after_reset_command(self, event: AstrMessageEvent):
        """Clear fork points after configured reset/new commands have completed."""
        if not self._is_reset_event(event):
            return

        umo = event.unified_msg_origin
        async with self._lock:
            initial = len(self._data.get("s", {}).get(umo, {}).get("f", {}))
            deleted = await self._delete_fork_conversations_locked(umo)
            remaining = len(self._data.get("s", {}).get(umo, {}).get("f", {}))
        if initial == 0:
            return
        if remaining:
            logger.info(
                f"dialog_fork 已清理 {umo} 的部分分叉点（{initial - remaining}/{initial}），"
                f"删除 {deleted} 条分叉对话，仍保留 {remaining} 条待后续清理"
            )
        else:
            logger.info(f"dialog_fork 已清空 {umo} 的 {initial} 个分叉点，删除 {deleted} 条分叉对话")
