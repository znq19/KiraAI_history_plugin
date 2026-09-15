"""History location engine: scan OneBot history by time / user / keyword.

Shared by history_plugin (hp) and kira_session_merger (KSM) - keep both copies
byte-identical. Pure logic only: no framework imports, no httpx, no logging, so
it can be unit-tested standalone.

Design notes (see ALIGN.md):
  * OneBot has NO search API. Location is "page backwards from the newest
    message and filter locally", so every result carries a scan report that
    tells the caller how deep we actually went.
  * The three implementations disagree on the anchor parameter name and on the
    page direction, so the `impl` dict (built by onebot_compat) is passed in.
  * Everything here is bounded: steps, wall clock and (by the caller) total
    scanned messages per turn.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------- time parsing

_TIME_FORMATS = (
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%m-%d",
    "%H:%M",
)

# A bare "HH:MM" is ambiguous (today? yesterday?). We resolve it against `now`
# but never move it into the future - a 23:00 request at 09:00 means yesterday.
_TIME_ONLY_RE = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")


def _candidate_years(now: datetime) -> List[int]:
    return [now.year, now.year - 1]


def parse_time_arg(raw: Any, now: datetime) -> Tuple[Optional[int], Optional[str]]:
    """Parse a user/model supplied time into a local unix timestamp.

    Returns (timestamp, error). `error` is a model-readable string when the
    value cannot be understood - we never guess silently.
    """
    if raw is None:
        return None, None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        value = int(raw)
        if value <= 0:
            return None, None
        # Accept seconds and (generously) milliseconds.
        if value > 10_000_000_000:
            value //= 1000
        return value, None
    text = str(raw).strip()
    if not text:
        return None, None

    if _TIME_ONLY_RE.match(text):
        # "HH:MM" alone carries no date. Anchor it to today and, if that lands
        # in the future, to yesterday (23:00 asked at 09:00 means last night).
        for fmt in ("%H:%M:%S", "%H:%M"):
            try:
                clock = datetime.strptime(text, fmt)
            except ValueError:
                continue
            dt = now.replace(hour=clock.hour, minute=clock.minute,
                             second=clock.second, microsecond=0)
            if dt > now:
                dt -= timedelta(days=1)
            return int(dt.timestamp()), None
        return None, f"无法解析时间 {raw!r}"

    for year in _candidate_years(now):
        for fmt in _TIME_FORMATS:
            if "%H" in fmt and "%Y" not in fmt and "%m" not in fmt:
                continue
            probe, use_fmt = text, fmt
            if "%Y" not in fmt:
                probe, use_fmt = f"{year}-{text}", "%Y-" + fmt
            try:
                dt = datetime.strptime(probe, use_fmt)
            except ValueError:
                continue
            # strptime is lenient enough to accept a non-matching layout (e.g.
            # "2026-09:10" against "%Y-%H:%M" defaults month/day to 1-1).
            # Round-trip through the same format to prove the layout matched.
            if dt.strftime(use_fmt).lstrip("0") not in (probe.lstrip("0"), probe):
                continue
            return int(dt.timestamp()), None
    return None, f"无法解析时间 {raw!r}（支持 2026-09-15 09:10 / 09-15 09:10 / 09:10 / 2026-09-15）"


# ------------------------------------------------------------ condition parsing

def normalize_user(raw: Any) -> Tuple[List[str], List[str]]:
    """Split a `user_id` argument into (numeric ids, name fragments)."""
    if raw is None:
        return [], []
    values: List[str] = []
    if isinstance(raw, (list, tuple, set)):
        values = [str(v).strip() for v in raw]
    else:
        values = [p.strip() for p in str(raw).replace("，", ",").split(",")]
    ids: List[str] = []
    names: List[str] = []
    for value in values:
        if not value:
            continue
        if value.isdigit():
            ids.append(value)
        else:
            names.append(value)
    return ids, names


def normalize_keywords(raw: Any, max_keywords: int) -> Tuple[List[str], Optional[str]]:
    """Split a `keyword` argument into individual terms (whitespace/`,` ORed)."""
    if raw is None:
        return [], None
    if isinstance(raw, (list, tuple, set)):
        values = [str(v) for v in raw]
    else:
        values = re.split(r"[,，\s]+", str(raw))
    out: List[str] = []
    for value in values:
        value = value.strip()
        if value and value not in out:
            out.append(value)
    if max_keywords and len(out) > max_keywords:
        return out[:max_keywords], f"关键词过多，只使用前 {max_keywords} 个：{out[:max_keywords]}"
    return out, None


@dataclass
class LocateQuery:
    """One location request. All fields optional - an empty query matches
    every message, which is the legacy "recent N" behaviour."""

    since: Optional[int] = None
    until: Optional[int] = None
    user_ids: List[str] = field(default_factory=list)
    user_names: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    keyword_case_sensitive: bool = False
    offset: int = 0
    scan_limit: int = 300

    @property
    def is_selective(self) -> bool:
        """True when the query narrows the result set (anything but pure paging)."""
        return bool(
            self.since or self.until or self.user_ids or self.user_names or self.keywords
        )

    @property
    def needs_deep_scan(self) -> bool:
        """A `since` lower bound forces us to keep walking until we pass it:
        stopping early could hide matches we were explicitly asked about."""
        return self.since is not None

    def describe(self) -> str:
        parts: List[str] = []
        if self.since or self.until:
            parts.append(
                "时间[%s ~ %s]" % (
                    fmt_ts(self.since) if self.since else "最早",
                    fmt_ts(self.until) if self.until else "现在",
                )
            )
        if self.user_ids or self.user_names:
            parts.append("用户[%s]" % "/".join(self.user_ids + self.user_names))
        if self.keywords:
            parts.append("关键词[%s]" % "|".join(self.keywords))
        if not parts:
            parts.append("最近消息")
        if self.offset:
            parts.append("offset=%d" % self.offset)
        return " ".join(parts)


def fmt_ts(ts: Optional[int]) -> str:
    if not ts:
        return "-"
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%m-%d %H:%M:%S")
    except (ValueError, OSError, OverflowError):
        return "-"


# --------------------------------------------------------------- text render

_CQ_ENTITIES = (("&#91;", "["), ("&#93;", "]"), ("&#44;", ","), ("&amp;", "&"))

# SnowLuma writes a synthetic row (empty sender, placeholder body) for a reply
# whose target it could not resolve. Those must never match a keyword search.
# A CQ code such as [CQ:image,file=/tmp/a.jpg] - its parameters are
# paths/URLs, never user-visible text.
_CQ_CODE_RE = re.compile(r"\[CQ:[^\]]*\]")

# Segment types whose payload is a URL / binary handle. Keyword search skips
# them (a search for "http" would otherwise match every image message).
_OPAQUE_TYPES = {"image", "video", "record"}


def cq_unescape(text: str) -> str:
    if not text:
        return text
    for entity, char in _CQ_ENTITIES:
        text = text.replace(entity, char)
    return text


def render_text_only(msg: Dict[str, Any]) -> str:
    """The keyword haystack: the message's *visible text*, nothing else.

    Explicitly excluded:
      * media segments (image/video/record) - their CQ code carries file paths
        and URLs, so a search for "http" or "jpg" would match every image
      * structural markers ("[转发消息]", "[引用]", "[表情]") - they are not
        message content

    Anything the caller searches for must be something a human would have typed.
    """
    segments = msg.get("message") or []
    if isinstance(segments, list) and segments:
        texts: List[str] = []
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            if seg.get("type") == "text":
                texts.append(str((seg.get("data") or {}).get("text") or ""))
        joined = " ".join(t for t in texts if t).strip()
        if joined:
            return joined
        # No text segment: the message has no searchable visible text.
        return ""

    # No segment array at all - fall back to raw_message with the CQ codes
    # stripped out (their parameters are paths/URLs, never user text).
    #
    # Synthetic placeholder rows are deliberately NOT detected here: sender
    # identity is what distinguishes them, and that is the caller's msg_filter
    # job (the plugin's _is_placeholder does it). Guessing from text alone would
    # also drop real messages whose content happens to be "[引用消息]".
    raw = cq_unescape(str(msg.get("raw_message") or "")).strip()
    if not raw:
        return ""
    return re.sub(r"\s+", " ", _CQ_CODE_RE.sub(" ", raw)).strip()

# ------------------------------------------------------------------- matching

def sender_ids(msg: Dict[str, Any]) -> List[str]:
    sender = msg.get("sender") if isinstance(msg.get("sender"), dict) else {}
    out: List[str] = []
    for value in (msg.get("user_id"), sender.get("user_id")):
        if value is None:
            continue
        text = str(value).strip()
        if text and text not in out:
            out.append(text)
    return out


def sender_names(msg: Dict[str, Any]) -> List[str]:
    sender = msg.get("sender") if isinstance(msg.get("sender"), dict) else {}
    out: List[str] = []
    for value in (sender.get("card"), sender.get("nickname")):
        if value:
            text = str(value).strip()
            if text and text not in out:
                out.append(text)
    return out


def msg_time(msg: Dict[str, Any]) -> int:
    try:
        return int(msg.get("time") or 0)
    except (TypeError, ValueError):
        return 0


def match(msg: Dict[str, Any], query: LocateQuery, haystack: Optional[str]) -> bool:
    """All supplied conditions must hold (AND).

    Evaluate cheapest-first (time is an int compare, then sender, then text) and
    bail out as soon as one condition fails.

    NOTE this is a conjunction on purpose: `user_id=X` + `keyword=Y` means
    "messages by X that mention Y". Treating it as OR would silently return
    every message X ever sent, while the result header still claims both
    conditions were applied.
    """
    if query.since is not None or query.until is not None:
        ts = msg_time(msg)
        if ts:
            if query.since is not None and ts < query.since:
                return False
            if query.until is not None and ts > query.until:
                return False

    if query.user_ids or query.user_names:
        hit = False
        if query.user_ids:
            ids = sender_ids(msg)
            if any(uid in ids for uid in query.user_ids):
                hit = True
        if not hit and query.user_names:
            blob = " ".join(sender_names(msg))
            if blob:
                low = blob.lower()
                if any(n.lower() in low for n in query.user_names):
                    hit = True
        if not hit:
            return False

    if query.keywords:
        if haystack is None:
            # Keyword matching is always text-only by design: the plugin's own
            # renderer emits media URLs, which must never be searchable.
            haystack = render_text_only(msg)
        if not query.keyword_case_sensitive:
            haystack = haystack.lower()
        for word in query.keywords:
            needle = word if query.keyword_case_sensitive else word.lower()
            if needle in haystack:
                break
        else:
            return False

    return True


# ------------------------------------------------------------------ scanning

@dataclass
class ScanReport:
    scanned: int = 0
    pages: int = 0
    matched_total: int = 0
    returned: int = 0
    truncated: bool = False
    reached_start: bool = False      # walked past the oldest message we can get
    stop_reason: str = "ok"          # ok | scan_limit | time_limit | start | enough | empty | error
    oldest_ts: int = 0
    newest_ts: int = 0
    elapsed: float = 0.0
    error: str = ""
    warnings: List[str] = field(default_factory=list)

    STOP_TEXT = {
        "scan_limit": "已扫到 scan_limit 上限",
        "time_limit": "已扫到时间上限",
        # `since` reached: the boundary was passed, NOT the beginning of history.
        # Wording it as "已到会话最早" contradicts the 到最早=否 field right above.
        "start": "已扫到 since 时间边界，更早的消息不在查询范围内",
        "enough": "命中数已足够",
        "empty": "服务端返回空页（已到会话最早）",
        "stalled": "服务端未返回更早的消息，扫描已停止",
        "error": "扫描出错",
        "ok": "完成",
    }

    # Stop reasons where the scan ended WITHOUT reaching a known boundary, so
    # more matches may exist beyond what we saw. The hit count is then only a
    # lower bound and must be displayed as such.
    OPEN_ENDED = frozenset({"enough", "scan_limit", "time_limit", "stalled"})

    def hit_label(self) -> str:
        """`命中=N` or `命中=N+`.

        The `+` matters: when the scan stopped early (hit budget reached, step
        or time limit) we only know how many matched *within the scanned range*.
        A bare number there would read as "this chat has exactly N matching
        messages", which is a claim we cannot back up.
        """
        if self.stop_reason in self.OPEN_ENDED:
            return "命中=%d+" % self.matched_total
        return "命中=%d" % self.matched_total

    def header(self, query: LocateQuery, session_label: str) -> str:
        bits = [
            "【定位】session=%s" % session_label,
            "条件=%s" % query.describe(),
            self.hit_label(),
        ]
        if self.truncated:
            bits.append("返回=%d(已截断)" % self.returned)
        bits.append("扫描=%d条/%d页" % (self.scanned, self.pages))
        bits.append("覆盖=%s ~ %s" % (fmt_ts(self.oldest_ts), fmt_ts(self.newest_ts)))
        bits.append("到最早=%s" % ("是" if self.reached_start else "否"))
        bits.append("耗时=%.1fs" % self.elapsed)
        line = " | ".join(bits)
        tail = self.STOP_TEXT.get(self.stop_reason)
        if tail and self.stop_reason not in ("ok",):
            line += "\n（%s）" % tail
        if self.error:
            line += "\n（%s）" % self.error
        for warning in self.warnings:
            line += "\n（%s）" % warning
        return line


@dataclass
class Person:
    """One message author, aggregated by QQ.

    A person has two names (nickname + group card) and they frequently differ;
    keying by name would count one person twice and let two people collide.
    """

    user_id: str = ""
    nickname: str = ""
    card: str = ""
    count: int = 0

    @property
    def display(self) -> str:
        """`昵称[群名片:X](QQ)` - the card note is omitted when it is absent or
        identical to the nickname (avoids `赵六[群名片:赵六]` noise)."""
        name = self.nickname
        if not name:
            return "(%s)" % self.user_id if self.user_id else "(未知)"
        note = ""
        if self.card and self.card != self.nickname:
            note = "[群名片:%s]" % self.card
        suffix = "(%s)" % self.user_id if self.user_id else ""
        return "%s%s%s" % (name, note, suffix)

    def sender_label(self) -> str:
        """Line prefix for a message: `昵称(QQ)`."""
        name = self.nickname
        if name and self.user_id:
            return "%s(%s)" % (name, self.user_id)
        if name:
            return name
        if self.user_id:
            return "(%s)" % self.user_id
        return "Unknown"


def person_of(msg: Dict[str, Any]) -> Person:
    """Extract the author of a message."""
    sender = msg.get("sender") if isinstance(msg.get("sender"), dict) else {}
    uid = ""
    for value in (msg.get("user_id"), sender.get("user_id")):
        if value is not None and str(value).strip() not in ("", "0"):
            uid = str(value).strip()
            break
    return Person(
        user_id=uid,
        nickname=str(sender.get("nickname") or "").strip(),
        card=str(sender.get("card") or "").strip(),
    )


@dataclass
class ScanResult:
    """Matched messages plus the report explaining how far we actually looked."""

    messages: List[Dict[str, Any]] = field(default_factory=list)
    report: ScanReport = field(default_factory=ScanReport)
    people: List[Person] = field(default_factory=list)
    scanned_count: int = 0            # what to charge against the per-turn budget

    @property
    def ok(self) -> bool:
        return not self.report.error


def message_key(msg: Dict[str, Any]) -> str:
    """Dedup key. `message_id` is the only value all three implementations
    agree on; `message_seq` is a different namespace in each."""
    for field_name in ("message_id", "message_seq"):
        value = msg.get(field_name)
        if value is not None and str(value) != "":
            return "m:%s" % value
    return "t:%s|u:%s|%s" % (
        msg.get("time"),
        msg.get("user_id"),
        render_text_only(msg)[:40],
    )


def anchor_of(msg: Dict[str, Any], anchor_field: str) -> Any:
    value = msg.get(anchor_field)
    if value is None or str(value) == "":
        value = msg.get("message_id")
    return value


def page_progresses(page: Sequence[Dict[str, Any]], anchor: Any,
                    anchor_field: str) -> bool:
    """True when this page actually moved backwards relative to the anchor.

    Guards against an implementation whose direction flag means something else
    (e.g. "reverse THIS PAGE" rather than "walk older"): such a backend keeps
    returning the same newest window, and we would otherwise loop until the
    scan budget burned out while claiming a deep scan.
    """
    if not page:
        return False
    if anchor is None:
        return True
    return str(anchor_of(page[0], anchor_field)) != str(anchor)

async def scan_backwards(
    fetch_page: Callable[[Optional[Any], int], Any],
    impl: Dict[str, Any],
    query: LocateQuery,
    session_label: str = "",
    page_size: int = 50,
    max_seconds: float = 25.0,
    max_steps: int = 60,
    stop_when_enough: bool = True,
    detect_boundary: bool = True,
    want_matches: int = 0,
    msg_filter: Optional[Callable[[Dict[str, Any]], bool]] = None,
    clock: Callable[[], float] = time.monotonic,
) -> ScanResult:
    """Walk history from newest to oldest, collecting matches.

    `fetch_page(anchor, count)` returns the next page oldest->newest and must
    raise on failure. `impl` carries the per-implementation knob names (see
    onebot_compat.py) - only `anchor_field` is read here.

    `want_matches` is how many matches the caller needs (count + offset); the
    scan stops early once that many are collected *and* the query has no
    `since` lower bound. 0 means "one page is enough" (the plain recent case).

    Stop conditions (first one that fires wins):
      * matched >= want_matches and no `since` lower bound (enough)
      * scanned >= scan_limit
      * elapsed >= max_seconds
      * page came back empty / no anchor progress (start of history)
      * steps exhausted (hard safety net)
    """
    started = clock()
    result = ScanResult()
    report = result.report
    result.scanned_count = 0

    anchor_field = str(impl.get("anchor_field") or "message_id")
    # Without an anchor parameter the backend cannot page at all: every request
    # returns the same newest window, so "no progress" is expected rather than
    # evidence that we reached the beginning of the conversation.
    can_page = bool(impl.get("anchor_param"))
    seen: set = set()
    matches: List[Dict[str, Any]] = []
    anchor: Any = None
    last_anchor: Any = None
    stall = 0
    people: List[Person] = []
    people_index: Dict[str, Person] = {}
    wanted = max(1, int(want_matches or 0) or (query.offset + 1))

    for step in range(max_steps):
        if report.scanned >= query.scan_limit:
            report.stop_reason = "scan_limit"
            break
        if clock() - started >= max_seconds:
            report.stop_reason = "time_limit"
            break
        del step

        want = min(page_size, max(1, query.scan_limit - report.scanned))
        try:
            page = await fetch_page(anchor, want)
        except Exception as exc:  # noqa: BLE001 - surfaced to the model
            report.error = "翻页失败：%s" % (exc,)
            report.stop_reason = "error"
            break

        if not page:
            # Two very different situations both produce an empty page:
            #   1. we really are at the oldest message
            #   2. the request failed and the transport swallowed it
            # Only claim (1) when the caller enabled boundary detection AND the
            # backend is known to page correctly (not the anchor-less fallback,
            # where each request re-fetches the same newest page).
            report.stop_reason = "empty"
            if detect_boundary and can_page:
                report.reached_start = True
            else:
                report.warnings.append(
                    "服务端返回空页，但无法确认是否已到会话最早")
            break

        report.pages += 1
        fresh = 0
        page_oldest_ts = 0
        # Pages arrive oldest->newest, but we walk backwards, so iterate each
        # page newest->oldest: the match list then stays globally newest-first.
        for msg in reversed(page):
            if not isinstance(msg, dict):
                continue
            key = message_key(msg)
            if key in seen:
                continue
            seen.add(key)
            fresh += 1
            report.scanned += 1
            ts = msg_time(msg)
            if ts:
                page_oldest_ts = ts if not page_oldest_ts else min(page_oldest_ts, ts)
                report.oldest_ts = ts if not report.oldest_ts else min(report.oldest_ts, ts)
                report.newest_ts = ts if not report.newest_ts else max(report.newest_ts, ts)
            # Drop rows the caller considers unrenderable (e.g. SnowLuma's
            # synthetic placeholder) BEFORE matching: showing one to the model
            # would be a fake "empty quote".
            if msg_filter is not None and not msg_filter(msg):
                continue

            haystack = render_text_only(msg) if query.keywords else None
            if match(msg, query, haystack):
                matches.append(msg)
                # Aggregate by QQ, not by name: one person has two names
                # (nickname + card) and they must not count as two people.
                # Walking newest-first means the stored identity is the most
                # recent one this person used.
                author = person_of(msg)
                key = author.user_id or "%s|%s" % (author.nickname, author.card)
                entry = people_index.get(key)
                if entry is None:
                    entry = author
                    people_index[key] = entry
                    people.append(entry)
                entry.count += 1

        # `since` is a hard lower bound: we must keep walking until we pass it,
        # because any message older than the boundary is filtered out anyway.
        if query.since is not None and page_oldest_ts and page_oldest_ts < query.since:
            report.stop_reason = "start"
            report.warnings.append(
                "已扫过 since 边界（%s），更早的消息不会命中" % fmt_ts(query.since))
            break

        if stop_when_enough and not query.needs_deep_scan and len(matches) >= wanted:
            report.stop_reason = "enough"
            break

        if fresh == 0:
            stall += 1
            if stall >= 2:
                # A non-empty page that yields nothing new is NOT the same as an
                # empty page: it means the anchor no longer advances (or the
                # implementation's page direction is the opposite of what we
                # assume). Reporting "reached the oldest message" here would be
                # a lie, so the two cases are kept distinct.
                report.stop_reason = "stalled"
                break
        else:
            stall = 0

        new_anchor = anchor_of(page[0], anchor_field)
        if new_anchor is None or str(new_anchor) == str(last_anchor):
            report.stop_reason = "stalled"
            break
        # A page whose oldest entry still equals our anchor means the backend
        # did not move (wrong direction flag / unsupported anchor). Stop now
        # instead of burning the whole scan budget.
        if not page_progresses(page, anchor, anchor_field):
            report.stop_reason = "stalled"
            break
        last_anchor = anchor
        anchor = new_anchor
    else:
        report.stop_reason = "scan_limit"

    report.elapsed = clock() - started
    report.matched_total = len(matches)

    if query.offset:
        matches = matches[query.offset:]
    report.returned = len(matches)
    report.truncated = False

    result.messages = matches
    # Most active first: the display truncates to the first few, and the people
    # who actually drove the conversation are the useful ones to show.
    people.sort(key=lambda p: (-p.count, p.user_id or p.nickname))
    result.people = people
    result.scanned_count = report.scanned
    return result


def finalize(result: ScanResult, keep: int) -> ScanResult:
    """Trim the match list to `keep` (count + offset).

    `report.matched_total` keeps the FULL match count so the renderer can tell
    the model how many hits exist beyond this page; `truncated` records that
    we are not showing all of them.
    """
    total = result.report.matched_total
    if keep > 0 and len(result.messages) > keep:
        result.messages = result.messages[:keep]
        result.report.truncated = True
    else:
        result.report.truncated = False
    result.report.returned = len(result.messages)
    result.report.matched_total = total
    return result

