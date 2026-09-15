# -*- coding: utf-8 -*-
"""Unit tests for locate.py + onebot_compat.py (no framework stubs needed).

Run: python3 tests/test_locate.py
"""
import asyncio
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import locate  # noqa: E402
import onebot_compat  # noqa: E402

results = []


def check(label, cond, detail=""):
    results.append((label, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + label + ((" — " + str(detail)) if detail else ""))


def text(t):
    return {"type": "text", "data": {"text": t}}


def msg(i, ts, uid=100, nick="U", body=None, segs=None):
    return {
        "message_id": 1000 + i,
        "message_seq": 5000 + i,
        "time": ts,
        "user_id": uid,
        "raw_message": body if body is not None else "m%d" % i,
        "message": segs if segs is not None else [text(body if body is not None else "m%d" % i)],
        "sender": {"user_id": uid, "nickname": nick, "card": ""},
    }


NOW = datetime(2026, 9, 15, 12, 0, 0)

# ---------------------------------------------------------------- time parsing
print("\n[time parsing]")
ts, err = locate.parse_time_arg("2026-09-15 09:10", NOW)
check("full datetime parses", err is None and ts == int(datetime(2026, 9, 15, 9, 10).timestamp()), ts)
ts, err = locate.parse_time_arg("09-15 09:10", NOW)
check("MM-DD HH:MM parses", err is None and ts == int(datetime(2026, 9, 15, 9, 10).timestamp()), (ts, err))
ts, err = locate.parse_time_arg("09:10", NOW)
check("HH:MM resolves to today when past", err is None and ts == int(datetime(2026, 9, 15, 9, 10).timestamp()), (ts, err))
ts, err = locate.parse_time_arg("23:00", NOW)
check("HH:MM in the future rolls back a day", err is None and ts == int(datetime(2026, 9, 14, 23, 0).timestamp()), (ts, err))
ts, err = locate.parse_time_arg("2026-09-15", NOW)
check("date-only parses to midnight", err is None and ts == int(datetime(2026, 9, 15, 0, 0).timestamp()), (ts, err))
ts, err = locate.parse_time_arg("", NOW)
check("empty -> None, no error", ts is None and err is None, (ts, err))
ts, err = locate.parse_time_arg(None, NOW)
check("None -> None, no error", ts is None and err is None, (ts, err))
ts, err = locate.parse_time_arg("昨天下午", NOW)
check("garbage -> error message (never guess)", ts is None and err, err)
ts, err = locate.parse_time_arg(1757300000, NOW)
check("epoch seconds pass through", ts == 1757300000, ts)
ts, err = locate.parse_time_arg(1757300000000, NOW)
check("epoch milliseconds normalised", ts == 1757300000, ts)

# -------------------------------------------------------------- arg normalising
print("\n[arg normalising]")
ids, names = locate.normalize_user("769690776")
check("numeric user_id -> ids", ids == ["769690776"] and names == [], (ids, names))
ids, names = locate.normalize_user("769690776,张三")
check("mixed user_id splits", ids == ["769690776"] and names == ["张三"], (ids, names))
ids, names = locate.normalize_user("张三，李四")
check("full-width comma splits", names == ["张三", "李四"], names)
ids, names = locate.normalize_user(None)
check("None user_id -> empty", ids == [] and names == [], (ids, names))
words, warn = locate.normalize_keywords("部署 上线，发布", 5)
check("keywords split on space/comma", words == ["部署", "上线", "发布"], words)
check("no warning under limit", warn is None, warn)
words, warn = locate.normalize_keywords("a b c d e f g", 3)
check("keyword cap truncates + warns", words == ["a", "b", "c"] and warn, (words, warn))
words, warn = locate.normalize_keywords("部署 部署", 5)
check("duplicate keywords deduped", words == ["部署"], words)

# ------------------------------------------------------------ text-only render
print("\n[text-only render]")
img = {"type": "image", "data": {"url": "http://x/a.jpg", "file": "/tmp/a.jpg"}}
mixed = msg(1, 100, body="看看这个 http://x/a.jpg",
            segs=[text("看看这个"), img])
check("image URL excluded from keyword haystack",
      "http" not in locate.render_text_only(mixed), locate.render_text_only(mixed))
check("visible text kept", "看看这个" in locate.render_text_only(mixed))
img_only = msg(4, 100, body="[CQ:image,file=/tmp/secret/a.jpg]",
                segs=[{"type": "image", "data": {"file": "/tmp/secret/a.jpg"}}])
check("image-only message renders EMPTY (no raw CQ fallback)",
      locate.render_text_only(img_only) == "", repr(locate.render_text_only(img_only)))
txt_img = msg(5, 100, body="[CQ:image,file=x.jpg]",
              segs=[{"type": "text", "data": {"text": "看这个"}},
                    {"type": "image", "data": {"file": "x.jpg"}}])
check("text+image keeps only the text",
      locate.render_text_only(txt_img) == "看这个", repr(locate.render_text_only(txt_img)))
fwd = msg(2, 100, body="[CQ:forward,id=res-1]",
          segs=[{"type": "forward", "data": {"id": "res-1"}}])
check("forward card has no visible text",
      locate.render_text_only(fwd) == "", repr(locate.render_text_only(fwd)))
file_seg = msg(3, 100, body="[CQ:file,name=报告.pdf,file=/tmp/x]",
               segs=[{"type": "file", "data": {"name": "报告.pdf", "file": "/tmp/x"}}])
check("file message has no visible text (name lives in the CQ param)",
      locate.render_text_only(file_seg) == "", repr(locate.render_text_only(file_seg)))
at_seg = msg(6, 100, body="[CQ:at,qq=12345]",
             segs=[{"type": "at", "data": {"qq": "12345"}}])
check("at-only message has no visible text",
      locate.render_text_only(at_seg) == "", repr(locate.render_text_only(at_seg)))
at_text = msg(7, 100, body="[CQ:at,qq=123] 看这个",
              segs=[{"type": "at", "data": {"qq": "123"}},
                    {"type": "text", "data": {"text": "看这个"}}])
check("at + text keeps the text", locate.render_text_only(at_text) == "看这个",
      repr(locate.render_text_only(at_text)))
esc = {"message_id": 9, "raw_message": "&#91;引用消息&#93;", "message": [text("[引用消息]")],
       "user_id": 0, "time": 1, "sender": {}}
# The RENDERER returns visible text; classifying a row as synthetic is the
# caller's msg_filter job (see _is_placeholder), so a "[引用消息]" text segment
# is returned as-is here.
check("renderer returns text segments verbatim",
      locate.render_text_only(esc) == "[引用消息]",
      repr(locate.render_text_only(esc)))
esc_raw = {"message_id": 12, "raw_message": "&#91;引用消息&#93;", "message": [],
           "user_id": 0, "time": 1, "sender": {"nickname": "", "card": ""}}
check("renderer strips CQ codes from raw_message",
      locate.render_text_only(esc_raw) == "[引用消息]",
      repr(locate.render_text_only(esc_raw)))
real_quote = {"message_id": 13, "raw_message": "&#91;引用消息&#93;",
              "message": [text("[引用消息]")], "user_id": 444, "time": 1,
              "sender": {"user_id": 444, "nickname": "真人"}}
check("a real message saying [引用消息] is NOT dropped by the renderer",
      locate.render_text_only(real_quote) == "[引用消息]",
      repr(locate.render_text_only(real_quote)))
no_segs = {"message_id": 10, "raw_message": "纯文本", "message": [], "user_id": 1, "time": 1, "sender": {}}
check("no segments -> CQ codes stripped from raw_message",
      locate.render_text_only(no_segs) == "纯文本", repr(locate.render_text_only(no_segs)))
cq_only = {"message_id": 11, "raw_message": "[CQ:image,file=/tmp/a.jpg]",
           "message": [], "user_id": 1, "time": 1, "sender": {}}
check("raw with only a CQ code renders empty",
      locate.render_text_only(cq_only) == "", repr(locate.render_text_only(cq_only)))

# ------------------------------------------------- text-only keyword contract
print("\n[keyword is text-only, always]")
# There is deliberately NO option to widen the keyword haystack: the plugin's
# own renderer emits media URLs, and a keyword must never match those.
_media = {"message_id": 1, "raw_message": "[CQ:image,file=/tmp/secret/a.jpg]",
          "message": [{"type": "image", "data": {"file": "/tmp/secret/a.jpg"}}],
          "user_id": 1, "time": 1, "sender": {}}
check("media path is not searchable",
      locate.match(_media, locate.LocateQuery(keywords=["secret"]), None) is False,
      locate.render_text_only(_media))
check("LocateQuery has no text_only_keywords switch",
      not hasattr(locate.LocateQuery(), "text_only_keywords"))
check("render_full is gone", not hasattr(locate, "render_full"))
check("is_recent_first is gone", not hasattr(locate, "is_recent_first"))
check("scan_fingerprint is gone", not hasattr(locate, "scan_fingerprint"))


# -------------------------------------------------------------------- matching
print("\n[matching]")
q = locate.LocateQuery()
check("empty query matches everything", locate.match(msg(1, 100), q, None) is True)
q = locate.LocateQuery(keywords=["部署"])
check("keyword hit", locate.match(msg(1, 100, body="今晚部署"), q, "今晚部署") is True)
check("keyword miss", locate.match(msg(1, 100, body="今晚吃饭"), q, "今晚吃饭") is False)
q = locate.LocateQuery(keywords=["ABC"], keyword_case_sensitive=False)
check("case-insensitive by default", locate.match(msg(1, 100, body="abc"), q, "abc") is True)
q = locate.LocateQuery(keywords=["ABC"], keyword_case_sensitive=True)
check("case-sensitive when configured", locate.match(msg(1, 100, body="abc"), q, "abc") is False)
q = locate.LocateQuery(user_ids=["100"])
check("user id hit", locate.match(msg(1, 100), q, None) is True)
check("user id miss", locate.match(msg(1, 100, uid=200), q, None) is False)
q = locate.LocateQuery(user_names=["张"])
card = msg(1, 100, nick="张三"); card["sender"]["card"] = "群名片张"
check("name matches nickname or card", locate.match(card, q, None) is True)
q = locate.LocateQuery(since=1000, until=2000)
check("time window inside", locate.match(msg(1, 1500), q, None) is True)
check("time window before", locate.match(msg(1, 500), q, None) is False)
check("time window after", locate.match(msg(1, 2500), q, None) is False)
q = locate.LocateQuery(since=1000, user_ids=["100"])
check("conditions combine with AND", locate.match(msg(1, 500), q, None) is False)
check("conditions combine with AND (both ok)", locate.match(msg(1, 1500), q, None) is True)

# ------------------------------------------------------------------- people
print("\n[people aggregation]")
p1 = locate.person_of({"user_id": 10001,
                       "sender": {"user_id": 10001, "nickname": "小明", "card": "张三"}})
check("person keys on the QQ", p1.user_id == "10001", p1)
check("nickname stored", p1.nickname == "小明", p1)
check("card stored", p1.card == "张三", p1)
check("display annotates a differing card",
      p1.display == "小明[群名片:张三](10001)", p1.display)
check("sender_label is 昵称(QQ)", p1.sender_label() == "小明(10001)", p1.sender_label())

p2 = locate.Person(user_id="10005", nickname="赵六", card="赵六")
check("identical card is not annotated", p2.display == "赵六(10005)", p2.display)
p3 = locate.Person(user_id="10005", nickname="赵六", card="")
check("empty card is not annotated", p3.display == "赵六(10005)", p3.display)
p4 = locate.Person(user_id="10006", nickname="", card="某名片")
check("missing nickname keeps the QQ",
      p4.display == "(10006)" and p4.sender_label() == "(10006)",
      (p4.display, p4.sender_label()))
p5 = locate.Person(user_id="", nickname="孤儿名", card="")
check("missing QQ still shows a name", p5.display == "孤儿名", p5.display)
p6 = locate.Person()
check("fully empty person is safe", p6.display == "(未知)" and p6.sender_label() == "Unknown",
      (p6.display, p6.sender_label()))
p7 = locate.person_of({"user_id": 0,
                       "sender": {"user_id": 0, "nickname": "零号", "card": ""}})
check("uid 0 is treated as absent", p7.user_id == "" and p7.display == "零号", p7)

# ---------------------------------------------------------------- message_key
print("\n[dedup key]")
check("message_id preferred", locate.message_key({"message_id": 7, "message_seq": 9}) == "m:7")
check("falls back to message_seq", locate.message_key({"message_seq": 9}) == "m:9")

# -------------------------------------------------------------------- scanning
print("\n[scanning]")


def make_backend(total, ts_of, page_cap=30, skip=None):
    """Fake OneBot: history[i] is newest-first; page(anchor, count) returns the
    `count` messages strictly older than `anchor` (or the newest ones)."""
    history = [msg(i, ts_of(i)) for i in range(total)]
    calls = {"n": 0}

    async def fetch_page(anchor, count):
        calls["n"] += 1
        count = min(count, page_cap)
        start = 0
        if anchor is not None:
            # Honour whichever anchor field the implementation uses
            # (message_id or message_seq) - the real OneBot does the same.
            for idx, m in enumerate(history):
                if str(m["message_id"]) == str(anchor) or str(m["message_seq"]) == str(anchor):
                    start = idx + 1
                    break
        page = history[start:start + count]
        page = list(reversed(page))  # oldest -> newest
        if skip:
            page = [m for m in page if m["message_id"] not in skip]
        return page

    return fetch_page, calls, history


IMPL = onebot_compat.resolve_impl("NapCat.Onebot")

# 1) plain recent: no conditions, scan_limit large
fetch, calls, history = make_backend(500, lambda i: 1000000 - i)
q = locate.LocateQuery(scan_limit=100)
res = asyncio.run(locate.scan_backwards(fetch, IMPL, q, page_size=30, max_seconds=5, want_matches=20))
res = locate.finalize(res, 20)
check("plain recent returns 20 newest", len(res.messages) == 20, len(res.messages))
check("newest first", res.messages[0]["message_id"] == 1000, res.messages[0]["message_id"])
check("scan report counts pages", res.report.pages >= 1, res.report.pages)
check("did not scan the whole history", res.report.scanned <= 100, res.report.scanned)

# 2) stop_when_enough: with no `since`, one page is enough for offset 0
q = locate.LocateQuery(scan_limit=500)
res = asyncio.run(locate.scan_backwards(fetch, IMPL, q, page_size=30, max_seconds=5, want_matches=20))
check("early stop after the first page when nothing is filtered",
      res.report.pages == 1, (res.report.pages, res.report.scanned))

# 3) keyword forces deeper scanning
q = locate.LocateQuery(keywords=["m400"], scan_limit=500)
res = asyncio.run(locate.scan_backwards(fetch, IMPL, q, page_size=30, max_seconds=5, want_matches=20))
check("keyword found deep in history", len(res.messages) == 1 and res.messages[0]["message_id"] == 1400,
      [m["message_id"] for m in res.messages])
check("keyword scan is bounded by scan_limit", res.report.scanned <= 500, res.report.scanned)

# 4) keyword missing -> reports depth instead of "not found"
q = locate.LocateQuery(keywords=["nope"], scan_limit=120)
res = asyncio.run(locate.scan_backwards(fetch, IMPL, q, page_size=30, max_seconds=5, want_matches=20))
check("missing keyword => zero matches", len(res.messages) == 0, len(res.messages))
check("missing keyword => stop_reason=scan_limit", res.report.stop_reason == "scan_limit", res.report.stop_reason)
check("missing keyword => reached_start False (honest report)",
      res.report.reached_start is False, res.report.reached_start)
check("scan never exceeds scan_limit", res.report.scanned <= 120, res.report.scanned)

# 5) `since` is a hard lower bound: walk until passed, not until "enough"
boundary = 1000000 - 200
q = locate.LocateQuery(since=boundary, scan_limit=500)
res = asyncio.run(locate.scan_backwards(fetch, IMPL, q, page_size=30, max_seconds=5, want_matches=20))
check("since walks past the boundary", res.report.scanned > 200, res.report.scanned)
check("since keeps only messages after the boundary",
      all(locate.msg_time(m) >= boundary for m in res.messages), len(res.messages))
check("since stops at the boundary", res.report.stop_reason == "start", res.report.stop_reason)

# 6) offset is applied against matches
q = locate.LocateQuery(user_ids=["999"], scan_limit=400)
res = asyncio.run(locate.scan_backwards(fetch, IMPL, q, page_size=30, max_seconds=5, want_matches=20))
check("user filter with no hit is empty", len(res.messages) == 0)
q = locate.LocateQuery(offset=5, scan_limit=200)
res = asyncio.run(locate.scan_backwards(fetch, IMPL, q, page_size=30, max_seconds=5, want_matches=20))
check("offset skips the newest hits",
      res.messages[0]["message_id"] == 1005, res.messages[0]["message_id"])

# 7) end of history detection
fetch2, calls2, _ = make_backend(10, lambda i: 1000000 - i)
q = locate.LocateQuery(keywords=["nope"], scan_limit=500)
res = asyncio.run(locate.scan_backwards(fetch2, IMPL, q, page_size=30, max_seconds=5))
check("short history => reached_start True", res.report.reached_start is True, res.report.stop_reason)
check("short history => scanned all 10", res.report.scanned == 10, res.report.scanned)

# 8) anchors actually advance (no infinite loop on a broken backend)
def broken_backend():
    async def fetch_page(anchor, count):
        return [msg(i, 1000000 - i) for i in range(5)]  # never advances
    return fetch_page

q = locate.LocateQuery(keywords=["nope"], scan_limit=500)
res = asyncio.run(locate.scan_backwards(broken_backend(), IMPL, q, page_size=5, max_seconds=5))
check("non-advancing backend terminates", res.report.scanned <= 5, res.report.scanned)
check("non-advancing backend flagged as stalled",
      res.report.stop_reason == "stalled", res.report.stop_reason)
check("non-advancing backend does NOT claim reached_start",
      res.report.reached_start is False, res.report.reached_start)

# 9) wall-clock guard
def slow_clock(start=[0.0]):
    def clock():
        start[0] += 0.6
        return start[0]
    return clock

fetch3, calls3, _ = make_backend(1000, lambda i: 1000000 - i)
q = locate.LocateQuery(keywords=["nope"], scan_limit=1000)
res = asyncio.run(locate.scan_backwards(fetch3, IMPL, q, page_size=30,
                                        max_seconds=2.0, clock=slow_clock()))
check("time limit stops the scan", res.report.stop_reason == "time_limit", res.report.stop_reason)

# 10) empty first page
def empty_backend():
    async def fetch_page(anchor, count):
        return []
    return fetch_page

q = locate.LocateQuery(keywords=["x"], scan_limit=100)
res = asyncio.run(locate.scan_backwards(empty_backend(), IMPL, q, page_size=30, max_seconds=5))
check("empty history => reached_start True", res.report.reached_start is True, res.report.stop_reason)

# 11) transport error surfaces, does not raise
def failing_backend():
    async def fetch_page(anchor, count):
        raise RuntimeError("boom")
    return fetch_page

q = locate.LocateQuery(keywords=["x"], scan_limit=100)
res = asyncio.run(locate.scan_backwards(failing_backend(), IMPL, q, page_size=30, max_seconds=5))
check("transport error reported, not raised", "boom" in res.report.error, res.report.error)

# ------------------------------------------------------------- impl resolution
print("\n[impl resolution]")
check("NapCat detected", onebot_compat.resolve_impl("NapCat.Onebot")["anchor_param"] == "message_seq")
check("LLOneBot detected", onebot_compat.resolve_impl("LLOneBot")["order_param"] == "reverseOrder")
snow = onebot_compat.resolve_impl("SnowLuma")
check("SnowLuma anchor is message_id", snow["anchor_param"] == "message_id", snow["anchor_param"])
check("SnowLuma scan cap is the tightest", snow["max_scan_limit"] == 800, snow["max_scan_limit"])
check("unknown impl has no anchor (safe)", onebot_compat.resolve_impl("SomeBot")["anchor_param"] is None)
check("NapCat order param name", onebot_compat.resolve_impl("NapCat.Onebot")["order_param"] == "reverse_order")

payload = onebot_compat.build_payload(onebot_compat.resolve_impl("NapCat.Onebot"),
                                      {"group_id": "1"}, 777, 30)
check("payload carries the anchor", payload.get("message_seq") == 777, payload)
check("payload flips direction only with an anchor",
      onebot_compat.build_payload(IMPL, {"group_id": "1"}, None, 30).get("reverse_order") is None)
snow_payload = onebot_compat.build_payload(snow, {"group_id": "1"}, -12345, 30)
check("SnowLuma payload uses a negative anchor",
      snow_payload.get("message_id") == -12345 and snow_payload.get("reverse_order") is True,
      snow_payload)
check("group_id stays a string", isinstance(payload.get("group_id"), str), payload)

# ------------------------------------------------------------------ rendering
print("\n[report header]")
q = locate.LocateQuery(since=1000, keywords=["部署"], scan_limit=300)
rep = locate.ScanReport(scanned=812, pages=28, matched_total=4, returned=4,
                        oldest_ts=1757300000, newest_ts=1757400000, elapsed=4.2,
                        stop_reason="enough")
head = rep.header(q, "group:123")
check("header has the session", "session=group:123" in head, head)
check("header has the condition", "关键词[部署]" in head and "时间[" in head, head)
check("header has the scan depth", "扫描=812条" in head, head)
check("header has the boundary flag", "到最早=否" in head, head)
check("header has the elapsed time", "耗时=4.2s" in head, head)

rep.stop_reason = "scan_limit"
check("stop reason rendered", "scan_limit" in rep.header(q, "g"), rep.header(q, "g"))
rep.error = "翻页失败：boom"
check("error rendered", "boom" in rep.header(q, "g"), rep.header(q, "g"))

# ------------------------------------------------------------------ hit label
print("\n[hit count honesty]")
_exact = locate.ScanReport(matched_total=3, stop_reason="empty", reached_start=True)
check("a bounded scan reports an exact count", _exact.hit_label() == "命中=3",
      _exact.hit_label())
for _reason in ("enough", "scan_limit", "time_limit", "stalled"):
    _r = locate.ScanReport(matched_total=50, stop_reason=_reason)
    check("early stop (%s) marks the count as a lower bound" % _reason,
          _r.hit_label() == "命中=50+", _r.hit_label())
_err = locate.ScanReport(matched_total=0, stop_reason="error")
check("error scan reports an exact zero", _err.hit_label() == "命中=0", _err.hit_label())
check("the header uses the labelled count",
      "命中=50+" in locate.ScanReport(
          matched_total=50, stop_reason="enough").header(
              locate.LocateQuery(), "g:1"))


# ------------------------------------------------------------------- summary
print()
passed = sum(1 for _, ok in results if ok)
print("TOTAL %d/%d passed" % (passed, len(results)))
sys.exit(0 if passed == len(results) else 1)
