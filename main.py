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
# NOTE: raw_message is a CQ-coded string, so literal "[", "]", ",", "&" in
# text arrive escaped as "&#91;", "&#93;", "&#44;", "&amp;" (SnowLuma
# helper/cq.ts cqEscape). Every placeholder comparison must unescape first,
# otherwise SnowLuma's "[引用消息]" placeholder arrives as "&#91;引用消息&#93;"
# and slips through the filter.
#
# Only the synthetic reply-target placeholder and the empty-message marker are
# real placeholders. "[引用]" and "[转发消息]" are the *renderings of real
# segments* (a reply without an id / a forward card) - filtering them would
# hide the very messages (and their message_id) the bot needs to re-forward.
_PLACEHOLDER_TOKENS = {"[引用消息]", "[空消息]"}
_PLACEHOLDER_RAW = _PLACEHOLDER_TOKENS | {""}
_CQ_ENTITIES = (("&#91;", "["), ("&#93;", "]"), ("&#44;", ","), ("&amp;", "&"))


def cq_unescape(text: str) -> str:
    """Decode OneBot CQ entities; "&amp;" must be last (see SnowLuma cq.ts)."""
    if not text:
        return text
    for entity, char in _CQ_ENTITIES:
        text = text.replace(entity, char)
    return text
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
                # Keep the forward's resource id: the outer message_id lets the
                # bot re-forward the card, the res id lets it inspect the
                # nested content.
                fid = seg_data.get("id", "")
                parts.append(f"[转发消息](id={fid})" if fid else "[转发消息]")
            else:
                parts.append(f"[{seg_type}]")
        return " ".join(parts)


    def _needs_refresh(self, msg: dict) -> bool:
        """True when the message needs a get_msg refresh: placeholder
        raw_message, or media segments without a usable source."""
        raw = cq_unescape((msg.get("raw_message") or "").strip())
        if raw in _PLACEHOLDER_RAW:
            return True
        for seg in msg.get("message") or []:
            if seg.get("type") in _MEDIA_TYPES:
                data = seg.get("data") or {}
                if not (data.get("url") or data.get("file") or data.get("file_id")):
                    return True
        return False


    def _is_placeholder(self, msg: dict) -> bool:
        """True only for the synthetic empty-quote rows SnowLuma stores for an
        unresolvable reply target (and genuinely empty messages).

        A real message - including a forward card or a bare reply - is kept:
        the bot needs its message_id to re-forward it.
        """
        raw = cq_unescape((msg.get("raw_message") or "").strip())
        segs = msg.get("message") or []
        # Genuinely empty (no raw text and no segments).
        if not raw and not segs:
            return True
        # SnowLuma's synthetic reply-target backfill (buildBackfillEvent) is a
        # single "[引用消息]" text whose sender identity is EMPTY: nickname and
        # card are always "", while user_id is the QUOTED message's sender uin
        # - frequently a real, non-zero uin - so an uid==0 test alone misses
        # most of them. A real user message keeps a nickname/card and is shown.
        sender = msg.get("sender") if isinstance(msg.get("sender"), dict) else {}
        uid = str(msg.get("user_id") or sender.get("user_id") or "").strip()
        nick = str(sender.get("nickname") or "").strip()
        card = str(sender.get("card") or "").strip()
        seg_text = self._segments_to_text(segs).strip() if segs else ""
        is_token = raw in _PLACEHOLDER_TOKENS or seg_text in _PLACEHOLDER_TOKENS
        if is_token and (uid in ("", "0") or (not nick and not card)):
            return True
        # Placeholder raw marker with no segments at all.
        if raw in _PLACEHOLDER_TOKENS and not segs:
            return True
        # Renders to nothing after stripping the trailing (msg_id:xxx).
        content = re.sub(r"\s*\(msg_id:-?\d+\)\s*$", "",
                         self._message_to_text(msg)).strip()
        return not content

    def _message_to_text(self, msg: dict) -> str:
        """Convert a message to formatted text. Uses raw_message only when it
        is real content; placeholder raw_message (e.g. SnowLuma's
        "[引用消息]") falls back to the segment array."""
        raw = cq_unescape((msg.get("raw_message") or "").strip())
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
        # Over-fetch so placeholder rows (which sit at the newest end) cannot
        # crowd real messages out of the returned window.
        fetch_count = min(80, max(count, count * 3))
        messages = None
        client = self._get_client(event) if self.use_ws else None
        if client is not None:
            messages = await self._fetch_ws(client, session_type, session_id, fetch_count)
        if messages is None:
            messages = await self._fetch_http(session_type, session_id, fetch_count)
        if not messages:
            return "No messages found."


        # ---------- 6. 强解析：对占位/缺媒体源的消息批量 get_msg 刷新 ----------
        if client is not None:
            target = messages[-fetch_count:]
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
        # NOTE: iterate `target` (the refreshed slice) - the old code
        # formatted `messages[-count:]` again, so every get_msg refresh was
        # silently discarded.
        formatted = []
        skipped = 0
        real = []
        for msg in (target if client is not None else messages[-fetch_count:]):
            if self._is_placeholder(msg):
                skipped += 1
                continue
            real.append(msg)
        for msg in real[-count:]:
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
