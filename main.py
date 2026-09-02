import httpx
import logging
import re
import time
from core.plugin import BasePlugin, register_tool as tool
from core.chat.message_utils import KiraMessageBatchEvent


logger = logging.getLogger(__name__)

# Placeholder raw_message produced by some OneBot implementations (e.g.
# SnowLuma) when the reply segment conversion fails - the message content
# is actually empty and must be rebuilt from segments or get_msg.
_PLACEHOLDER_RAW = {"[引用消息]", "[空消息]", ""}
# Segment types whose source (url/file) may be missing in stored history
# and needs a get_msg refresh (SnowLuma refreshes image URLs on get_msg).
_MEDIA_TYPES = {"image", "record", "video"}
# Max messages to refresh per call (get_msg is one round-trip each).
_MAX_REFRESH = 10


class HistoryPlugin(BasePlugin):
    def __init__(self, ctx, cfg):
        super().__init__(ctx, cfg)
        self.host = cfg.get("http_host", "localhost")
        self.port = cfg.get("http_port", 3000)
        self.token = cfg.get("access_token", "")
        self.base_url = f"http://{self.host}:{self.port}"
        self.master_id = cfg.get("master_id", "769690776")
        # WS channel first (same ID namespace as the adapter / forward_fix),
        # HTTP as fallback. Disable to keep the old HTTP-only behavior.
        self.use_ws = cfg.get("use_ws", True)
        allowed_users_str = cfg.get("allowed_users", "")
        self.allowed_list = [uid.strip() for uid in allowed_users_str.split(",") if uid.strip()] if allowed_users_str else []
        restricted_groups_str = cfg.get("restricted_groups", "")
        self.restricted_groups = [gid.strip() for gid in restricted_groups_str.split(",") if gid.strip()] if restricted_groups_str else []

        # ---------- 防循环调用缓存 ----------
        self._call_cache = {}  # {cache_key: {"count": int, "data": str, "timestamp": float}}


    async def initialize(self):
        logger.info(f"History plugin initialized with anti-loop cache (use_ws={self.use_ws})")
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


    # ---------- 通道 ----------

    def _get_client(self, event):
        """Get the adapter WS client from the event (same ID namespace as
        the adapter itself, so message IDs are usable by get_msg / forward)."""
        try:
            info = getattr(event, "adapter", None)
            if info is None:
                return None
            name = getattr(info, "name", None) or getattr(info, "adapter_id", None)
            if not name:
                return None
            adapter = self.ctx.adapter_mgr.get_adapter(name)
            if adapter is None:
                return None
            return adapter.get_client()
        except Exception as e:
            logger.error(f"[history] get client failed: {e}")
            return None


    async def _fetch_ws(self, client, session_type: str, session_id: str, count: int):
        """Fetch history via the WS channel (adapter's own OneBot connection)."""
        try:
            if session_type == "group":
                resp = await client.send_action(
                    "get_group_msg_history",
                    {"group_id": int(session_id), "count": count},
                    timeout=15,
                )
            else:
                resp = await client.send_action(
                    "get_friend_msg_history",
                    {"user_id": int(session_id), "count": count},
                    timeout=15,
                )
            if isinstance(resp, dict) and resp.get("status") == "ok":
                return resp.get("data", {}).get("messages") or []
        except Exception as e:
            logger.error(f"[history] WS history failed: {e}")
        return None


    async def _fetch_http(self, session_type: str, session_id: str, count: int):
        """Fetch history via the HTTP service (legacy channel)."""
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
                return None
            return result.get("data", {}).get("messages", [])
        except Exception as e:
            logger.error(f"[history] HTTP history failed: {e}")
            return None


    async def _get_msg_ws(self, client, message_id) -> dict | None:
        """Fetch a single message via get_msg (SnowLuma refreshes image URLs
        on get_msg, so this recovers media sources missing from history)."""
        try:
            resp = await client.send_action(
                "get_msg", {"message_id": message_id}, timeout=15
            )
            if isinstance(resp, dict) and resp.get("status") == "ok":
                return resp.get("data") or {}
        except Exception as e:
            logger.error(f"[history] get_msg({message_id}) failed: {e}")
        return None


    # ---------- 强解析 ----------

    @staticmethod
    def _segments_to_text(msg_segments) -> str:
        """Render message segments to text, keeping media URLs and reply IDs."""
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
                rid = seg_data.get("id", "")
                parts.append(f"[引用 msg_id:{rid}]" if rid else "[引用]")
            elif seg_type == "forward":
                parts.append("[转发消息]")
            else:
                parts.append(f"[{seg_type}]")
        return " ".join(parts)


    def _needs_refresh(self, msg: dict) -> bool:
        """True when the message needs a get_msg refresh: placeholder
        raw_message, or media segments without a usable source."""
        raw = (msg.get("raw_message") or "").strip()
        if raw in _PLACEHOLDER_RAW:
            return True
        for seg in msg.get("message") or []:
            if seg.get("type") in _MEDIA_TYPES:
                data = seg.get("data") or {}
                if not (data.get("url") or data.get("file") or data.get("file_id")):
                    return True
        return False


    def _is_placeholder(self, msg: dict) -> bool:
        """True when the message renders as a placeholder (empty quote) and
        carries no real content - SnowLuma stores reply-conversion failures
        as such (raw_message = "[引用消息]" with empty/placeholder segments).
        Filtering these keeps the LLM context clean."""
        raw = (msg.get("raw_message") or "").strip()
        segs = msg.get("message") or []
        # Placeholder raw_message (non-empty) marks a conversion failure.
        if raw and raw in _PLACEHOLDER_RAW:
            return True
        # Empty raw_message is normal for segment-based messages - only
        # filter when there is genuinely no content at all.
        if not raw and not segs:
            return True
        # Render the content; if it is empty or a pure placeholder after
        # stripping the trailing (msg_id:xxx), the message is not real.
        content = self._message_to_text(msg)
        content = re.sub(r"\s*\(msg_id:-?\d+\)\s*$", "", content).strip()
        if not content:
            return True
        if content in ("[空消息]", "[引用消息]", "[引用]", "[转发消息]"):
            return True
        return False

    def _message_to_text(self, msg: dict) -> str:
        """Convert a message to formatted text. Uses raw_message only when it
        is real content; placeholder raw_message (e.g. SnowLuma's
        "[引用消息]") falls back to the segment array."""
        raw = (msg.get("raw_message") or "").strip()
        if raw and raw not in _PLACEHOLDER_RAW:
            content = raw
        else:
            msg_segments = msg.get("message", [])
            if not msg_segments:
                content = "[空消息]"
            else:
                content = self._segments_to_text(msg_segments)

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


        # ---------- 5. 拉取数据：WS 通道优先，HTTP 兜底 ----------
        messages = None
        client = self._get_client(event) if self.use_ws else None
        if client is not None:
            messages = await self._fetch_ws(client, session_type, session_id, count)
        if messages is None:
            messages = await self._fetch_http(session_type, session_id, count)
        if not messages:
            return "No messages found."


        # ---------- 6. 强解析：对占位/缺媒体源的消息批量 get_msg 刷新 ----------
        if client is not None:
            target = messages[-count:]
            refreshed = 0
            for i, m in enumerate(target):
                if refreshed >= _MAX_REFRESH:
                    break
                if self._needs_refresh(m):
                    mid = m.get("message_id")
                    if mid is not None:
                        fresh = await self._get_msg_ws(client, mid)
                        if fresh and fresh.get("message"):
                            target[i] = fresh
                            refreshed += 1
            if refreshed:
                logger.info(f"[history] refreshed {refreshed} messages via get_msg")


        # ---------- 7. 格式化消息（取最近的 count 条） ----------
        # Filter out placeholder messages that could not be resolved even
        # after get_msg refresh (SnowLuma stores reply-conversion failures
        # as empty messages with a "[引用消息]" raw_message placeholder).
        # Showing them would confuse the LLM with fake "empty quotes".
        formatted = []
        skipped = 0
        for msg in messages[-count:]:
            if self._is_placeholder(msg):
                skipped += 1
                continue
            sender = msg.get("sender", {}).get("nickname", "Unknown")
            content = self._message_to_text(msg)
            formatted.append(f"{sender}: {content}")

        if skipped:
            logger.info(f"[history] filtered {skipped} unresolvable placeholder messages")

        if not formatted:
            return "No messages found."

        result_text = "\n".join(formatted)


        # ---------- 8. 更新缓存 ----------
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
