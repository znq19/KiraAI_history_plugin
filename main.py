import httpx
import logging
import re
import time
from core.plugin import BasePlugin, register_tool as tool
from core.chat.message_utils import KiraMessageBatchEvent

logger = logging.getLogger(__name__)

class HistoryPlugin(BasePlugin):
    def __init__(self, ctx, cfg):
        super().__init__(ctx, cfg)
        self.host = cfg.get("http_host", "localhost")
        self.port = cfg.get("http_port", 3000)
        self.token = cfg.get("access_token", "")
        self.base_url = f"http://{self.host}:{self.port}"
        self.master_id = cfg.get("master_id", "769690776")
        allowed_users_str = cfg.get("allowed_users", "")
        self.allowed_list = [uid.strip() for uid in allowed_users_str.split(",") if uid.strip()] if allowed_users_str else []
        restricted_groups_str = cfg.get("restricted_groups", "")
        self.restricted_groups = [gid.strip() for gid in restricted_groups_str.split(",") if gid.strip()] if restricted_groups_str else []

        # ---------- 防循环调用缓存 ----------
        self._call_cache = {}  # {cache_key: {"count": int, "data": str, "timestamp": float}}

    async def initialize(self):
        logger.info(f"History plugin initialized with anti-loop cache")
        logger.info(f"Master: {self.master_id}")
        logger.info(f"Allowed users: {self.allowed_list}")
        logger.info(f"Restricted groups: {self.restricted_groups}")

    async def terminate(self):
        logger.info("History plugin terminated")

    def _check_permission(self, user_id: str, session_type: str, session_id: str) -> bool:
        """权限检查：主人全权限，普通用户只能看自己的私聊和非限制群聊"""
        if user_id == self.master_id:
            return True
        if session_type == "private":
            return session_id == user_id
        if session_type == "group":
            return session_id not in self.restricted_groups
        return False

    def _message_to_text(self, msg: dict) -> str:
        """将单条消息转换为带格式的文本摘要（包含图片URL等），并附加消息ID"""
        # 优先使用 raw_message
        if msg.get("raw_message"):
            content = msg["raw_message"]
        else:
            msg_segments = msg.get("message", [])
            if not msg_segments:
                content = "[空消息]"
            else:
                parts = []
                for seg in msg_segments:
                    seg_type = seg.get("type")
                    seg_data = seg.get("data", {})
                    if seg_type == "text":
                        parts.append(seg_data.get("text", ""))
                    elif seg_type == "at":
                        parts.append(f"@{seg_data.get('qq', 'someone')}")
                    elif seg_type == "face":
                        parts.append("[表情]")
                    elif seg_type == "image":
                        img_url = seg_data.get("url", "")
                        if img_url:
                            parts.append(f"[图片]({img_url})")
                        else:
                            parts.append("[图片]")
                    elif seg_type == "video":
                        parts.append("[视频]")
                    elif seg_type == "file":
                        file_name = seg_data.get("name", "文件")
                        parts.append(f"[文件]{file_name}")
                    elif seg_type == "reply":
                        parts.append("[回复]")
                    elif seg_type == "forward":
                        parts.append("[转发消息]")
                    else:
                        parts.append(f"[{seg_type}]")
                content = " ".join(parts)

        # 附加消息ID
        msg_id = msg.get("message_id")
        if msg_id:
            content += f" (msg_id:{msg_id})"
        return content

    @tool(
        "get_history",
        "Fetch recent messages from a group or private chat, including image URLs in format [图片](url) and message IDs in (msg_id:数字) at the end of each line.",
        {
            "type": "object",
            "properties": {
                "session_type": {
                    "type": "string",
                    "enum": ["group", "private"],
                    "description": "Session type: group or private"
                },
                "session_id": {
                    "type": "string",
                    "description": "Session ID (group number or QQ number)"
                },
                "count": {
                    "type": "integer",
                    "default": 20,
                    "description": "Number of messages to fetch (建议20-50条，最少5条)"
                }
            },
            "required": ["session_type", "session_id"]
        }
    )
    async def get_history(self, event: KiraMessageBatchEvent, session_type: str, session_id: str, count: int = 20) -> str:
        # ---------- 1. 获取调用者用户ID ----------
        if event.messages and event.messages[0].sender:
            user_id = event.messages[0].sender.user_id
        else:
            user_id = "unknown"

        # ---------- 2. 权限检查 ----------
        if not self._check_permission(user_id, session_type, session_id):
            logger.warning(f"Permission denied: user {user_id} tried to access {session_type}:{session_id}")
            return "抱歉，您没有权限查看此会话的历史消息。"

        # ---------- 3. 硬限制 count 范围（防 LLM 传 0 或超大值） ----------
        if count < 5:
            count = 5
        elif count > 80:
            count = 80

        # ---------- 4. 核心防循环逻辑（缓存拦截） ----------
        cache_key = f"{session_type}:{session_id}"
        current_time = time.time()
        cached = self._call_cache.get(cache_key)

        # 如果缓存存在且在有效期内（120秒）
        if cached and (current_time - cached.get("timestamp", 0)) < 120:
            # 如果本次请求的 count 小于或等于缓存中的 count，判定为「试探性重试」，直接拦截
            if count <= cached.get("count", 0):
                logger.warning(f"[防循环] 拦截递减重试: {cache_key}, count={count} (cached_count={cached['count']})")
                return (
                    cached["data"]
                    + "\n\n---\n⚠️ 系统提示：检测到您使用更少的条数重复查询同一会话。"
                    "以上是已获取的完整历史消息，请直接基于此内容进行总结或回复，"
                    "**请勿再次调用 get_history 工具**。"
                )

        # ---------- 5. 正常拉取数据（首次调用 或 count 大于缓存） ----------
        try:
            if session_type == "group":
                api = "get_group_msg_history"
                params = {"group_id": int(session_id), "count": count}
            else:
                api = "get_friend_msg_history"
                params = {"user_id": int(session_id), "count": count}

            headers = {}
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"

            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{self.base_url}/{api}",
                    json=params,
                    headers=headers,
                    timeout=10
                )
                resp.raise_for_status()
                result = resp.json()

            if result.get("status") != "ok":
                return f"Failed: {result.get('message', 'unknown error')}"

            messages = result.get("data", {}).get("messages", [])
            if not messages:
                return "No messages found."

            # 格式化消息（取最近的 count 条）
            formatted = []
            for msg in messages[-count:]:
                sender = msg.get("sender", {}).get("nickname", "Unknown")
                content = self._message_to_text(msg)
                formatted.append(f"{sender}: {content}")

            result_text = "\n".join(formatted)

            # ---------- 6. 更新缓存 ----------
            self._call_cache[cache_key] = {
                "count": count,
                "data": result_text,
                "timestamp": current_time
            }

            # 清理过期缓存（超过5分钟或超过100条）
            if len(self._call_cache) > 100:
                now = time.time()
                expired_keys = [k for k, v in self._call_cache.items() if now - v.get("timestamp", 0) > 300]
                for k in expired_keys:
                    del self._call_cache[k]

            return result_text

        except Exception as e:
            logger.error(f"Error fetching history: {e}")
            return f"Error: {str(e)}"