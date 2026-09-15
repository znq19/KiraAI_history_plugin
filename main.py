import asyncio
import httpx
import logging
import re
import time
from datetime import datetime
from typing import Any, Dict, List

from core.plugin import BasePlugin, register_tool as tool
from core.chat.message_utils import KiraMessageBatchEvent

from . import locate
from .onebot_compat import build_payload, resolve_impl

logger = logging.getLogger(__name__)

# Locate-mode scan budget: a single tool call may issue dozens of OneBot
# requests, so the wall clock is bounded well below the framework's
# tool_call_timeout (60s default) to leave room for the reply itself.
_SCAN_MAX_STEPS = 80
# Max characters of the locate-aware result body.
_MAX_RESULT_CHARS = 3500
# How many distinct people the `涉及` line lists before collapsing to `…等N人`.
# Each entry costs ~15-25 chars, and the body budget is shared with messages.
_MAX_PEOPLE_SHOWN = 5
# Legacy-path over-fetch ceiling: at least this many messages are requested so
# placeholder rows cannot crowd real ones out of the window.
_FETCH_CEILING = 80

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
        self.allowed_list = self._split_ids(cfg.get("allowed_users", ""))
        self.restricted_groups = self._split_ids(cfg.get("restricted_groups", ""))

        # ---------- locate (time / user / keyword) ----------
        # Master switch. Off = byte-for-byte the legacy "recent N messages"
        # behaviour, so an existing install that never touches the new config
        # sees no change at all.
        self.enable_locate = bool(cfg.get("enable_locate", True))
        self.enable_keyword = bool(cfg.get("enable_keyword", True))
        self.enable_time_range = bool(cfg.get("enable_time_range", True))
        self.enable_user_filter = bool(cfg.get("enable_user_filter", True))
        self.default_scan_limit = max(1, int(cfg.get("default_scan_limit", 300) or 300))
        self.max_scan_limit = max(0, int(cfg.get("max_scan_limit", 0) or 0))  # 0 = per-impl
        self.scan_max_seconds = max(1.0, float(cfg.get("scan_max_seconds", 25) or 25))
        self.max_fetch_per_request = max(1, int(cfg.get("max_fetch_per_request", 50) or 50))
        self.fetch_timeout_sec = max(1.0, float(cfg.get("fetch_timeout_sec", 15) or 15))
        self.max_scanned_per_turn = max(0, int(cfg.get("max_scanned_per_turn", 3000) or 3000))
        self.max_calls_per_target_per_turn = max(
            1, int(cfg.get("max_calls_per_target_per_turn", 2) or 2))
        self.max_calls_per_turn = max(1, int(cfg.get("max_calls_per_turn", 3) or 3))
        self.early_stop_on_enough = bool(cfg.get("early_stop_on_enough", True))
        self.detect_boundary = bool(cfg.get("detect_boundary", True))
        self.keyword_case_sensitive = bool(cfg.get("keyword_case_sensitive", False))
        self.max_keywords = max(1, int(cfg.get("max_keywords", 5) or 5))
        self.offset_max = max(0, int(cfg.get("offset_max", 1000) or 1000))
        self.max_return_count = max(1, int(cfg.get("max_return_count", 80) or 80))
        self.locate_fallback_on_error = bool(cfg.get("locate_fallback_on_error", True))
        self.locate_head_meta = bool(cfg.get("locate_head_meta", True))
        self.locate_cache_ttl_sec = max(0, int(cfg.get("locate_cache_ttl_sec", 120) or 120))

        # ---------- 防循环调用缓存 ----------
        self._call_cache = {}  # legacy: {session_key: {"count": int, "data": str, "timestamp": float}}
        self._locate_cache = {}  # {signature: {"data": str, "timestamp": float}}
        self._locate_meta_cache = {}  # {signature: {scan_limit, conditions, data}}
        self._impl_cache = {}  # {adapter_name: impl dict}


    @staticmethod
    def _split_ids(value) -> list:
        """Accept both the legacy comma-separated string and the list form
        that config UIs (and KSM) use for the same field."""
        if value is None:
            return []
        if isinstance(value, (list, tuple, set)):
            return [str(v).strip() for v in value if str(v).strip()]
        return [p.strip() for p in str(value).replace("，", ",").split(",") if p.strip()]


    async def initialize(self):
        logger.info(f"History plugin initialized with anti-loop cache (use_ws={self.use_ws})")
        logger.info(f"Master: {self.master_id}")
        logger.info(f"Allowed users: {self.allowed_list}")
        logger.info(f"Restricted groups: {self.restricted_groups}")
        logger.info(f"Locate: enabled={self.enable_locate} default_scan_limit={self.default_scan_limit}")


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


    # ---------- locate: implementation probe ----------

    async def _resolve_impl(self, client, adapter_name: str) -> Dict[str, Any]:
        """Probe the OneBot implementation once per adapter.

        The three implementations need different anchor parameters, so we ask
        `get_version_info` and cache the answer. On any failure we fall back to
        the generic knob set (no anchor), which still works everywhere - it just
        cannot page deeper than one request.
        """
        cached = self._impl_cache.get(adapter_name)
        if cached is not None:
            return cached

        impl = resolve_impl("")
        if client is not None:
            try:
                resp = await client.send_action("get_version_info", {}, timeout=8)
                data = (resp or {}).get("data") or {}
                app_name = str(data.get("app_name") or "")
                if app_name:
                    impl = resolve_impl(app_name)
                    logger.info(f"[history] OneBot impl={app_name} anchor={impl.get('anchor_param')}")
            except Exception as e:
                logger.warning(f"[history] get_version_info failed, using generic knobs: {e}")

        # Config override wins over the per-implementation default.
        if self.max_scan_limit > 0:
            impl["max_scan_limit"] = self.max_scan_limit
        impl["max_page"] = max(1, min(int(impl.get("max_page") or 30),
                                      self.max_fetch_per_request))
        self._impl_cache[adapter_name] = impl
        return impl


    # ---------- locate: paging ----------

    async def _fetch_page_ws(self, client, session_type: str, session_id: str,
                             impl: Dict[str, Any], anchor, count: int):
        """One page via the adapter WS channel, oldest->newest."""
        if session_type == "group":
            action, base = "get_group_msg_history", {"group_id": str(session_id)}
        else:
            action, base = "get_friend_msg_history", {"user_id": str(session_id)}
        payload = build_payload(impl, base, anchor, count)
        try:
            resp = await client.send_action(action, payload, timeout=self.fetch_timeout_sec)
        except Exception as e:
            logger.error(f"[history] WS page failed: {e}")
            return None
        if not isinstance(resp, dict) or resp.get("status") != "ok":
            logger.warning(f"[history] WS page status={(resp or {}).get('status')}")
            return None
        messages = (resp.get("data") or {}).get("messages") or []
        return messages if isinstance(messages, list) else None


    async def _fetch_page_http(self, session_type: str, session_id: str,
                               impl: Dict[str, Any], anchor, count: int):
        """One page via the legacy HTTP service."""
        if session_type == "group":
            api, base = "get_group_msg_history", {"group_id": str(session_id)}
        else:
            api, base = "get_friend_msg_history", {"user_id": str(session_id)}
        payload = build_payload(impl, base, anchor, count)
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            async with httpx.AsyncClient() as http:
                resp = await http.post(f"{self.base_url}/{api}", json=payload,
                                       headers=headers, timeout=self.fetch_timeout_sec)
                resp.raise_for_status()
                result = resp.json()
        except Exception as e:
            logger.error(f"[history] HTTP page failed: {e}")
            return None
        if not isinstance(result, dict) or result.get("status") != "ok":
            return None
        messages = (result.get("data") or {}).get("messages") or []
        return messages if isinstance(messages, list) else None


    def _make_page_fetcher(self, client, session_type: str, session_id: str,
                           impl: Dict[str, Any]):
        """Build the scanner's fetch_page(anchor, count) callback.

        A WS failure on the FIRST page falls back to HTTP for the whole walk
        (same session, same ID namespace semantics differ but the walk is
        internally consistent). A failure later in the walk is surfaced - the
        caller may retry once from the newest page.
        """
        state = {"fell_back": False}

        async def fetch_page(anchor, count):
            if client is not None and not state["fell_back"]:
                page = await self._fetch_page_ws(client, session_type, session_id,
                                                 impl, anchor, count)
                if page is not None:
                    return page
                if anchor is None:
                    state["fell_back"] = True
                    logger.warning("[history] WS page failed, falling back to HTTP")
                else:
                    raise RuntimeError("WS 翻页失败")
            page = await self._fetch_page_http(session_type, session_id, impl,
                                               anchor, count)
            if page is None:
                raise RuntimeError("HTTP 翻页失败")
            return page

        return fetch_page


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
        "Fetch messages from a group or private chat, including image URLs in "
        "[图片](url) form and message IDs in (msg_id:数字) at the end of each line.\n"
        "Two modes:\n"
        "1) RECENT (default): omit every locate parameter to get the newest `count` messages.\n"
        "2) LOCATE: pass any of `since` / `until` / `user_id` / `keyword` to find messages "
        "matching them. Location pages BACKWARDS from the newest message and filters locally, "
        "then returns a scan report (命中/扫描/覆盖/到最早) so you know how far back it really "
        "looked. It is a rewind, NOT a search index: the recent hundreds~thousands of messages "
        "are fast, very old ones may be out of reach. When nothing matched, say it was not "
        "found WITHIN THE SCANNED RANGE - never claim the chat never mentioned it.",
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
                    "description": "Number of messages to return (建议20-50条，最少5条)"
                },
                "since": {
                    "type": "string",
                    "description": "只返回此时间之后的消息。格式：2026-09-15 09:10 / 09-15 09:10 / 09:10 / 2026-09-15"
                },
                "until": {
                    "type": "string",
                    "description": "只返回此时间之前的消息，格式同 since"
                },
                "user_id": {
                    "type": "string",
                    "description": "只看这个人发的消息。填 QQ 号（多个逗号分隔），也可填群名片/昵称的一部分"
                },
                "keyword": {
                    "type": "string",
                    "description": "只看包含该关键词的消息（多个用空格或逗号分隔，任一命中即可）"
                },
                "offset": {
                    "type": "integer",
                    "default": 0,
                    "description": "跳过前 N 条命中结果，用于翻页查看后续命中"
                },
                "scan_limit": {
                    "type": "integer",
                    "description": "最多往回扫描多少条消息（默认 300）。命中不够时可加大，耗时随之增加"
                }
            },
            "required": ["session_type", "session_id"]
        }
    )
    async def get_history(self, event: KiraMessageBatchEvent, session_type: str,
                          session_id: str, count: int = 20,
                          since: str = None, until: str = None,
                          user_id: str = None, keyword: str = None,
                          offset: int = 0, scan_limit: int = None, **_) -> str:
        # ---------- 1. 获取调用者用户ID ----------
        if event.messages and event.messages[0].sender:
            caller_id = event.messages[0].sender.user_id
        else:
            caller_id = "unknown"

        # ---------- 2. 权限检查 ----------
        if not self._check_permission(caller_id, session_type, session_id):
            logger.warning(f"Permission denied: user {caller_id} tried to access {session_type}:{session_id}")
            return "抱歉，您没有权限查看此会话的历史消息。"

        # ---------- 3. 硬限制 count 范围（防 LLM 传 0 或超大值） ----------
        # Models routinely send numbers as strings ("20") or as floats; coerce
        # before comparing, otherwise `count < 5` raises TypeError and the tool
        # call dies instead of returning anything.
        try:
            count = int(float(count))
        except (TypeError, ValueError):
            count = 20
        if count < 5:
            count = 5
        elif count > self.max_return_count:
            count = self.max_return_count

        wants_locate = self.enable_locate and any(
            arg not in (None, "", 0)
            for arg in (since, until, user_id, keyword, offset, scan_limit)
        )
        session_key = f"{session_type}:{session_id}"

        if not wants_locate:
            return await self._recent_path(event, session_type, session_id,
                                           count, session_key)
        return await self._locate_path(event, session_type, session_id, count,
                                       since, until, user_id, keyword,
                                       offset, scan_limit, session_key)


    # ---------- legacy path: newest `count` messages ----------

    async def _recent_path(self, event, session_type: str, session_id: str,
                           count: int, session_key: str) -> str:
        # ---------- 4. 核心防循环逻辑（缓存拦截） ----------
        cache_key = session_key
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
        # crowd real messages out of the returned window. The ceiling follows
        # `count` but never below _FETCH_CEILING, so raising `count` (up to
        # max_return_count) actually fetches enough instead of silently
        # capping at 80.
        fetch_count = min(max(count, _FETCH_CEILING), max(count, count * 3))
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
            formatted.append(self._format_line(msg))

        if skipped:
            logger.info(f"[history] filtered {skipped} unresolvable placeholder messages")

        if not formatted:
            return "No messages found."

        result_text = "\n".join(formatted)
        # Bound the body: a large `count` can exceed the model's context. KSM
        # has always capped its result this way; keep both plugins aligned.
        if len(result_text) > _MAX_RESULT_CHARS:
            result_text = result_text[:_MAX_RESULT_CHARS] + "\n…(truncated)"


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


    # ---------- locate path: scan backwards + filter ----------

    def _format_line(self, msg: dict) -> str:
        """`昵称(QQ): 内容`.

        The QQ number is not decoration: nickname and group card both change,
        and in a group the two can differ from each other. Without a stable id
        the model cannot tell that two names refer to the same person.
        """
        label = locate.person_of(msg).sender_label()
        content = self._message_to_text(msg)
        return f"{label}: {content}"


    def _scan_limit_cap(self) -> int:
        caps = [impl.get("max_scan_limit") for impl in self._impl_cache.values()]
        caps = [int(c) for c in caps if c]
        cap = min(caps) if caps else (self.max_scan_limit or 600)
        if self.max_scan_limit:
            cap = min(cap, self.max_scan_limit)
        return max(1, cap)


    def _build_query(self, since, until, user_id, keyword, offset, scan_limit):
        """Parse raw tool args into (query, notes, error)."""
        now = datetime.now()
        query = locate.LocateQuery()
        notes: List[str] = []

        if self.enable_time_range:
            ts, err = locate.parse_time_arg(since, now)
            if err:
                return None, notes, err
            query.since = ts
            ts, err = locate.parse_time_arg(until, now)
            if err:
                return None, notes, err
            query.until = ts
        elif since or until:
            notes.append("时间过滤已在配置中关闭（enable_time_range）")

        if self.enable_user_filter and user_id:
            query.user_ids, query.user_names = locate.normalize_user(user_id)
        elif user_id:
            notes.append("用户过滤已在配置中关闭（enable_user_filter）")

        if self.enable_keyword and keyword:
            words, warn = locate.normalize_keywords(keyword, self.max_keywords)
            query.keywords = words
            if warn:
                notes.append(warn)
        elif keyword:
            notes.append("关键词过滤已在配置中关闭（enable_keyword）")

        if not (query.since or query.until or query.user_ids or query.user_names
                or query.keywords):
            notes.append("定位参数均无效，本次按最近消息返回")

        try:
            query.offset = max(0, int(offset or 0))
        except (TypeError, ValueError):
            query.offset = 0
        if self.offset_max and query.offset > self.offset_max:
            notes.append(f"offset 超过上限 {self.offset_max}，已截断")
            query.offset = self.offset_max

        cap = self._scan_limit_cap()
        try:
            requested = int(scan_limit) if scan_limit else self.default_scan_limit
        except (TypeError, ValueError):
            requested = self.default_scan_limit
        # Floor at one page worth: a model typo (0 / -5) must not reduce the
        # scan to a single message and then report "not found".
        floor = min(cap, max(20, self.max_fetch_per_request))
        query.scan_limit = max(floor, min(requested, cap))
        # Report BOTH directions of adjustment - a silently raised limit can
        # otherwise look like the requested value was honoured.
        if requested > cap:
            notes.append(f"scan_limit 超过上限 {cap}，已截断为 {query.scan_limit}")
        elif requested < query.scan_limit:
            notes.append(f"scan_limit 低于下限 {query.scan_limit}，已提升到该值")

        # The per-turn scan budget may clamp further. Record what was actually
        # decided here so a later message does not quote the pre-clamp number.
        self._last_effective_scan_limit = query.scan_limit

        query.keyword_case_sensitive = self.keyword_case_sensitive
        return query, notes, None


    # ---------- per-turn budgets ----------

    @staticmethod
    def _turn_budget(event):
        """Return the per-event scratch dict (created on demand).

        Returns None only when the event refuses the attribute entirely. An
        *empty* dict is a perfectly normal state and must NOT disable the
        budgets - that would silently turn anti-loop off for every real event.
        """
        try:
            extra = getattr(event, "extra", None)
            if isinstance(extra, dict):
                return extra
            extra = {}
            try:
                event.extra = extra
            except Exception:
                return None
            return extra
        except Exception:
            return None

    def _take_call_budget(self, event, session_key: str):
        """Reserve one locate call. Returns None on success, else a refusal."""
        extra = self._turn_budget(event)
        if extra is None:
            return None
        total = int(extra.get("hp_hist_total", 0) or 0)
        by_target = extra.get("hp_hist_by_target")
        if not isinstance(by_target, dict):
            by_target = {}
            extra["hp_hist_by_target"] = by_target
        if total >= self.max_calls_per_turn:
            return ("已到达本回合历史查询次数上限，请基于已有信息回答，"
                    "不要再次调用 get_history。")
        used = int(by_target.get(session_key, 0) or 0)
        if used >= self.max_calls_per_target_per_turn:
            return (f"本回合已查询过 {session_key} 的历史 {used} 次，"
                    "请基于已有结果回答；如需不同条件，请先说明已查到的内容。")
        by_target[session_key] = used + 1
        extra["hp_hist_total"] = total + 1
        return None

    def _charge_scan_budget(self, event, scanned: int) -> bool:
        """Charge `scanned` against the per-turn total. False = exhausted."""
        if scanned <= 0 or self.max_scanned_per_turn <= 0:
            return True
        extra = self._turn_budget(event)
        if extra is None:
            return True
        used = int(extra.get("hp_hist_scanned", 0) or 0) + scanned
        extra["hp_hist_scanned"] = used
        return used <= self.max_scanned_per_turn

    def _scan_budget_left(self, event) -> int:
        if self.max_scanned_per_turn <= 0:
            return 1 << 30
        extra = self._turn_budget(event)
        used = int(extra.get("hp_hist_scanned", 0) or 0) if extra else 0
        return max(0, self.max_scanned_per_turn - used)


    def _conditions(self, session_key: str, query):
        """Identity of a location query, ignoring scan depth/count.

        `session_key` is part of it: two different chats asked with the same
        filter must not be treated as each other's superset.
        """
        return (session_key, query.since, query.until, tuple(query.user_ids),
                tuple(query.user_names), tuple(query.keywords))


    def _signature(self, session_key: str, query, count: int) -> str:
        import hashlib
        raw = "|".join(str(x) for x in (
            self._conditions(session_key, query), query.offset, query.scan_limit, count,
        ))
        return hashlib.md5(raw.encode("utf-8")).hexdigest()


    def _trim_locate_cache(self) -> None:
        if len(self._locate_cache) > 100:
            now = time.time()
            for k in [k for k, v in self._locate_cache.items()
                      if now - v.get("timestamp", 0) > 300]:
                del self._locate_cache[k]


    def _start_reached(self, conds):
        """A previous run with identical conditions already walked back to the
        oldest message: nothing older exists, so re-scanning cannot help.

        Scoped to the cache TTL: new messages arrive at the NEWEST end, so the
        claim "walked to the very beginning" stays true for the older part but
        the *answer* can go stale at any moment. Outside the TTL we let the
        query through and rescan instead of refusing forever.
        """
        now = time.time()
        for entry in self._locate_cache.values():
            if entry.get("conditions") != conds or not entry.get("reached_start"):
                continue
            if self.locate_cache_ttl_sec > 0 and \
                    (now - entry.get("timestamp", 0)) >= self.locate_cache_ttl_sec:
                continue
            return locate.fmt_ts(entry.get("oldest_ts"))
        return None


    def _subset_refusal(self, conds, current_limit: int):
        """Refuse a strictly-narrower re-scan of an identical condition set.

        The earlier answer already walked deeper, so a shallower scan can only
        return a subset of it. An *equal* or *bigger* scan_limit is a legitimate
        escalation, not a subset - only `prior > current` refuses.
        """
        for entry in self._locate_cache.values():
            if entry.get("conditions") != conds:
                continue
            prior_limit = int(entry.get("scan_limit", 0) or 0)
            if prior_limit > current_limit:
                return prior_limit
        return None


    async def _locate_path(self, event, session_type: str, session_id: str,
                           count: int, since, until, user_id, keyword,
                           offset, scan_limit, session_key: str) -> str:
        # Resolve the OneBot implementation FIRST: the scan cap depends on it
        # (NapCat 2000 / LLOneBot 1200 / SnowLuma 800), and probing later would
        # clamp the very first call to the conservative generic cap.
        client = self._get_client(event) if self.use_ws else None
        adapter_name = ""
        try:
            info = getattr(event, "adapter", None)
            adapter_name = str(getattr(info, "name", None)
                               or getattr(info, "adapter_id", "") or "")
        except Exception:
            adapter_name = ""
        impl = await self._resolve_impl(client, adapter_name or session_type)

        query, notes, error = self._build_query(
            since, until, user_id, keyword, offset, scan_limit)
        if error:
            return f"Error: {error}"

        # Anti-loop: budget -> exact-signature cache -> reached-start -> subset.
        refusal = self._take_call_budget(event, session_key)
        if refusal:
            return f"Rejected: {refusal}"

        requested_scan_limit = query.scan_limit
        conds = self._conditions(session_key, query)
        signature = self._signature(session_key, query, count)
        cached = self._locate_cache.get(signature)
        if cached and (time.time() - cached.get("timestamp", 0)) < self.locate_cache_ttl_sec:
            return (cached["data"]
                    + "\n\n---\n⚠️ 本次定位条件与刚才完全相同，结果见上。"
                    "请直接基于已有内容回答，不要重复查询；"
                    "如需更早的消息请加大 scan_limit。")
        reached = self._start_reached(conds)
        if reached:
            return (f"Rejected: 上文中相同条件的查询已扫到该会话最早"
                    f"（{reached}），更早没有消息了。请直接使用已有结果。")
        prior = self._subset_refusal(conds, query.scan_limit)
        if prior:
            return (f"Rejected: 本次条件与上文某次查询相同，但扫描范围更小"
                    f"（本次 {query.scan_limit} < 上次 {prior}），"
                    "结果必然是上次的子集。请直接使用上文已有结果；"
                    "若需更多命中，请加大 scan_limit 或收窄条件。")

        budget_left = self._scan_budget_left(event)
        if budget_left <= 0:
            return ("Rejected: 本回合的扫描预算已用尽，请基于已有信息回答，"
                    "不要再次调用 get_history。")
        if budget_left < query.scan_limit:
            # Say so: otherwise the "try a bigger scan_limit" hint later quotes
            # a number this call never actually used.
            notes.append(
                f"本回合剩余扫描预算只有 {budget_left} 条，"
                f"本次按 {budget_left} 条执行（原定 {query.scan_limit}）")
            requested_scan_limit = budget_left
        query.scan_limit = min(query.scan_limit, budget_left)

        session_label = f"{session_type}:{session_id}"
        result = await self._run_scan(client, impl, session_type, session_id,
                                      query, count, session_label)
        if result is None:
            return "Error: 历史扫描失败（请勿重复调用，基于已有信息回答）。"

        self._charge_scan_budget(event, result.scanned_count)
        if result.report.error and not result.messages:
            return f"Error: {result.report.error}"

        text = self._render_result(result, query, count, session_label, notes,
                                   requested_scan_limit)
        self._locate_cache[signature] = {
            "data": text,
            "timestamp": time.time(),
            "scan_limit": query.scan_limit,
            "conditions": conds,
            "reached_start": result.report.reached_start,
            "oldest_ts": result.report.oldest_ts,
        }
        self._trim_locate_cache()
        return text


    async def _run_scan(self, client, impl, session_type, session_id,
                        query, count, session_label):
        """Run the scan; retry once from the newest page if the anchor went
        stale mid-walk (NapCat's short-id map is an LRU and can evict)."""
        for attempt in (1, 2):
            fetcher = self._make_page_fetcher(client, session_type, session_id, impl)
            local = locate.LocateQuery(**query.__dict__)
            result = await locate.scan_backwards(
                fetcher, impl, local,
                session_label=session_label,
                page_size=int(impl.get("max_page") or 30),
                max_seconds=self.scan_max_seconds,
                max_steps=_SCAN_MAX_STEPS,
                stop_when_enough=self.early_stop_on_enough,
                detect_boundary=self.detect_boundary,
                want_matches=count + query.offset,
                # Reuse the same placeholder test the legacy path uses so a
                # keyword can never match (and show) a synthetic row.
                msg_filter=lambda m: not self._is_placeholder(m),
            )
            result = locate.finalize(result, count + query.offset)
            if not result.report.error:
                return result
            if attempt == 2 or not self.locate_fallback_on_error:
                return result
            logger.warning(
                f"[history] scan failed ({result.report.error}); retrying from newest page")
        return None


    def _format_people(self, people) -> str:
        """`涉及: 昵称[群名片:X](QQ)、...`

        Each entry is ONE PERSON (aggregated by QQ) - a nickname and a group
        card that differ are shown together rather than as two separate names.
        Truncated at _MAX_PEOPLE_SHOWN; the list is already sorted by message
        count so the busiest speakers survive the cut.
        """
        shown = people[:_MAX_PEOPLE_SHOWN]
        text = "、".join(p.display for p in shown)
        if len(people) > _MAX_PEOPLE_SHOWN:
            # "共N人" (total) rather than "等N人" (ambiguous: N total, or N more?)
            text += "…（共%d人）" % len(people)
        return "涉及: " + text


    def _render_result(self, result, query, count, session_label, notes,
                       requested_scan_limit) -> str:
        report = result.report
        lines: List[str] = []

        # Trim to `count` BEFORE the header is rendered: the header reports how
        # many messages are actually being returned, and computing it from the
        # pre-trim list makes it disagree with the body.
        selected = result.messages
        if count > 0 and len(selected) > count:
            selected = selected[:count]
        report.returned = len(selected)

        if self.locate_head_meta:
            lines.append(report.header(query, session_label))
            if result.people:
                lines.append(self._format_people(result.people))
            lines.append("---")

        if not selected:
            if report.reached_start:
                lines.append("已扫到会话最早，未找到符合条件的消息。")
            else:
                lines.append(
                    f"扫描范围内未命中（已扫最近 {report.scanned} 条，未到会话最早）。")
                lines.append(
                    "如需继续向前，请加大 scan_limit 重试"
                    f"（本次 {requested_scan_limit}，可试 "
                    f"{min(max(requested_scan_limit * 3, 600), self._scan_limit_cap())}）。")
            if notes and self.locate_head_meta:
                lines.extend(f"（{n}）" for n in notes)
            return "\n".join(lines)

        for msg in selected:
            lines.append(self._format_line(msg))

        if report.matched_total > len(selected) and self.locate_head_meta:
            first = query.offset + 1
            last = query.offset + len(selected)
            lines.append("---")
            # With an offset these are NOT "the newest N" - say which slice of
            # the match list is actually shown.
            lines.append(
                f"提示: 命中 {report.matched_total} 条，此处列出第 {first}-{last} 条。"
                f"可用 offset={last} 查看后续命中，或用 since/until 收窄条件。")
        if notes and self.locate_head_meta:
            lines.extend(f"（{n}）" for n in notes)

        text = "\n".join(lines)
        if len(text) > _MAX_RESULT_CHARS:
            text = text[:_MAX_RESULT_CHARS] + "\n…(truncated)"
        return text
