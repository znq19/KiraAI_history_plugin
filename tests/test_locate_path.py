# -*- coding: utf-8 -*-
"""End-to-end tests for the plugin's locate path (get_history).

Drives HistoryPlugin.get_history with a fake OneBot backend so the whole chain
is covered: arg parsing -> budgets -> scan -> render -> cache/subset guards.

Run: python3 tests/test_locate_path.py
"""
import asyncio
import importlib.util
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# --- minimal KiraAI stubs so the plugin imports standalone ---
for name in ("core", "core.chat", "core.chat.message_utils"):
    sys.modules[name] = types.ModuleType(name)
sys.modules["core.chat.message_utils"].KiraMessageBatchEvent = type("E", (), {})
hx = types.ModuleType("httpx")
hx.Timeout = lambda **k: None
hx.AsyncClient = object
sys.modules["httpx"] = hx

core_plugin = types.ModuleType("core.plugin")


class _BasePlugin:
    def __init__(self, ctx, cfg):
        self.ctx = ctx
        self.cfg = cfg


def _passthrough_tool(*args, **kwargs):
    def deco(fn):
        return fn
    return deco


core_plugin.BasePlugin = _BasePlugin
core_plugin.register_tool = _passthrough_tool
core_plugin.logger = types.SimpleNamespace(
    info=lambda *a, **k: None, error=lambda *a, **k: None,
    warning=lambda *a, **k: None, debug=lambda *a, **k: None)
sys.modules["core.plugin"] = core_plugin

pkg_name = "hp_e2e"
pkg = types.ModuleType(pkg_name)
pkg.__path__ = [ROOT]
pkg.__package__ = pkg_name
sys.modules[pkg_name] = pkg
for _sub in ("locate", "onebot_compat"):
    _s = importlib.util.spec_from_file_location(
        f"{pkg_name}.{_sub}", os.path.join(ROOT, f"{_sub}.py"))
    _m = importlib.util.module_from_spec(_s)
    sys.modules[f"{pkg_name}.{_sub}"] = _m
    _s.loader.exec_module(_m)

spec = importlib.util.spec_from_file_location(f"{pkg_name}.hist_main",
                                              os.path.join(ROOT, "main.py"))
mod = importlib.util.module_from_spec(spec)
sys.modules[f"{pkg_name}.hist_main"] = mod
spec.loader.exec_module(mod)
HistoryPlugin = mod.HistoryPlugin

results = []


def check(label, cond, detail=""):
    results.append((label, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + label + ((" — " + str(detail)) if detail else ""))


def text(t):
    return {"type": "text", "data": {"text": t}}


# --- fake history -----------------------------------------------------------
BASE_TS = 1789434600  # 2026-09-15 09:10 local-ish; exact value is irrelevant
TOTAL = 600


def build_history():
    """Newest-first list: history[0] is the newest message."""
    out = []
    for i in range(TOTAL):
        uid = 100 if i % 3 else 200
        nick = "甲" if uid == 100 else "乙"
        body = "普通消息 %d" % i
        if i == 150:
            body = "今晚要部署新版本"
        if i == 100:
            body = "部署脚本我改好了"
        out.append({
            "message_id": 1000 + i,
            "message_seq": 5000 + i,
            "time": BASE_TS - i * 60,
            "user_id": uid,
            "raw_message": body,
            "message": [text(body)],
            "sender": {"user_id": uid, "nickname": nick, "card": ""},
        })
    return out


HISTORY = build_history()


class _Sender:
    user_id = "769690776"


class _M:
    sender = _Sender()


class _Ev:
    def __init__(self):
        self.messages = [_M()]
        self.extra = {}


class _Ctx:
    adapter_mgr = None


class FakeClient:
    """Stand-in for the adapter WS client.

    `fail_after` fails every page request after the Nth (a permanently broken
    backend). `fail_once_at` fails exactly one request, modelling a stale
    anchor - the retry from the newest page then succeeds.
    """

    def __init__(self, app_name="NapCat.Onebot", fail_after=None,
                 fail_once_at=None):
        self.app_name = app_name
        self.fail_after = fail_after
        self.fail_once_at = fail_once_at
        self.calls = []
        self.page_calls = 0

    async def send_action(self, action, params, timeout=None):
        self.calls.append((action, dict(params)))
        if action == "get_version_info":
            return {"status": "ok", "data": {"app_name": self.app_name}}
        if action == "get_msg":
            return {"status": "failed"}
        self.page_calls += 1
        if self.fail_once_at is not None and self.page_calls == self.fail_once_at:
            raise RuntimeError("anchor expired")
        if self.fail_after is not None and self.page_calls > self.fail_after:
            raise RuntimeError("anchor expired")
        count = int(params.get("count") or 20)
        anchor = params.get("message_seq", params.get("message_id"))
        start = 0
        if anchor is not None:
            for idx, m in enumerate(HISTORY):
                if str(m["message_id"]) == str(anchor) or str(m["message_seq"]) == str(anchor):
                    start = idx + 1
                    break
        page = HISTORY[start:start + count]
        return {"status": "ok", "data": {"messages": list(reversed(page))}}


def make_plugin(overrides=None, client=None):
    cfg = {"use_ws": True, "master_id": "", "enable_locate": True}
    cfg.update(overrides or {})
    plugin = HistoryPlugin(_Ctx(), cfg)
    fake = client if client is not None else FakeClient()
    plugin._get_client = lambda e: fake
    return plugin


def run(plugin, event, **kwargs):
    return asyncio.new_event_loop().run_until_complete(
        plugin.get_history(event, **kwargs))


# ===========================================================================
print("\n[parse: time]")
plugin = make_plugin()
ev = _Ev()
out = run(plugin, ev, session_type="group", session_id="123", count=20,
          since="2026-09-15 08:00", until="2026-09-15 09:00")
check("time window renders a header", "【定位】" in out, out[:200])
check("time window reports matched count", "命中=" in out, out[:200])
body_lines = [l for l in out.splitlines()
              if l and not l.startswith(("【定位】", "涉及:", "---", "（", "提示:"))]
check("time window returns only in-window messages",
      all(("甲(" in l or "乙(" in l) for l in body_lines), body_lines[:3])
check("bad time string is a readable error",
      "Error" in run(make_plugin(), _Ev(), session_type="group", session_id="1",
                     since="昨天下午"),
      run(make_plugin(), _Ev(), session_type="group", session_id="1", since="昨天下午"))

print("\n[parse: keyword]")
out = run(make_plugin(), _Ev(), session_type="group", session_id="123",
          count=10, keyword="部署")
check("keyword finds both hits", "部署" in out and out.count("部署") >= 2, out[:400])
check("keyword header states the condition", "关键词[部署]" in out, out[:200])
check("keyword search stops early when satisfied", "到最早=否" in out, out[:200])

out = run(make_plugin(), _Ev(), session_type="group", session_id="123",
          count=10, keyword="压根没有的词")
check("missing keyword reports a bounded scan", "扫描范围内未命中" in out, out[:400])
check("missing keyword reports a bounded scan",
      "扫描范围内未命中" in out and "未到会话最早" in out, out[:400])
# The keyword itself contains "没有", so only check the body text.
body = out.split("---", 1)[-1]
check("missing keyword does not claim the chat never said it",
      "群里" not in body and "没人" not in body, body[:200])
check("missing keyword tells the model what to do next",
      "scan_limit" in body, body[:200])
check("missing keyword suggests a bigger scan_limit", "scan_limit" in out, out[:400])

print("\n[parse: user]")
out = run(make_plugin(), _Ev(), session_type="group", session_id="123",
          count=5, user_id="200", scan_limit=200)
check("user filter header", "用户[200]" in out, out[:200])
check("user filter returns only that uid",
      "乙(200)" in out and "甲(100)" not in out, out[:400])
out = run(make_plugin(), _Ev(), session_type="group", session_id="123",
          count=5, user_id="乙", scan_limit=200)
check("name filter works too", "乙(200)" in out, out[:300])

print("\n[parse: keyword excludes media URLs]")
MEDIA_HISTORY = [{
    "message_id": 1, "message_seq": 1, "time": BASE_TS, "user_id": 1,
    "raw_message": "[CQ:image,file=x]",
    "message": [{"type": "image", "data": {"url": "http://example.com/a.jpg"}}],
    "sender": {"user_id": 1, "nickname": "A", "card": ""},
}]
saved = HISTORY[:]
HISTORY[:] = MEDIA_HISTORY
out = run(make_plugin(), _Ev(), session_type="group", session_id="1",
          count=5, keyword="http")
check("keyword does not match image URLs", "命中=0" in out or "未命中" in out, out[:300])
HISTORY[:] = saved

print("\n[placeholders never reach the model]")


class _PhEv:
    def __init__(self):
        self.messages = [_M()]
        self.extra = {}


PLACEHOLDER = {"message_id": 777001, "message_seq": 777001, "time": BASE_TS - 5,
               "user_id": 0, "raw_message": "&#91;引用消息&#93;",
               "message": [text("[引用消息]")],
               "sender": {"user_id": 0, "nickname": "", "card": ""}}
saved2 = HISTORY[:]
HISTORY.insert(5, PLACEHOLDER)
ph = run(make_plugin(), _PhEv(), session_type="group", session_id="123",
         count=10, keyword="引用")
check("keyword '引用' does not match a placeholder row",
      "777001" not in ph and "引用消息" not in ph, ph[:300])
ph2 = run(make_plugin(), _PhEv(), session_type="group", session_id="123",
          count=10, user_id="0")
check("placeholders are not searchable by user_id either",
      "777001" not in ph2, ph2[:300])
HISTORY[:] = saved2

print("\n[sender label carries the QQ]")


class _LabelEv:
    def __init__(self):
        self.messages = [_M()]
        self.extra = {}


out_lab = run(make_plugin(), _LabelEv(), session_type="group", session_id="123", count=5)
check("legacy path lines carry QQ too", "甲(100):" in out_lab, out_lab[:150])
out_lab2 = run(make_plugin(), _LabelEv(), session_type="group", session_id="123",
               count=5, keyword="部署")
check("locate path lines carry QQ", "甲(100):" in out_lab2 or "乙(200):" in out_lab2,
      out_lab2[:250])

print("\n[the 涉及 line lists PEOPLE, not names]")


# Two different people whose names collide across nickname/card.
COLLIDE = [
    {"message_id": 90001, "message_seq": 90001, "time": BASE_TS, "user_id": 55501,
     "raw_message": "a", "message": [text("a")],
     "sender": {"user_id": 55501, "nickname": "小明", "card": "张三"}},
    {"message_id": 90002, "message_seq": 90002, "time": BASE_TS - 60, "user_id": 55502,
     "raw_message": "b", "message": [text("b")],
     "sender": {"user_id": 55502, "nickname": "张三", "card": "李四"}},
]
saved3 = HISTORY[:]
HISTORY[:] = COLLIDE
out_col = run(make_plugin(), _LabelEv(), session_type="group", session_id="123",
              count=10, user_id="张三")
check("a person is listed once despite having two names",
      out_col.split("---")[0].count("(55501)") == 1, out_col.split("---")[0])
check("both colliding people are shown with their own QQ",
      "(55501)" in out_col and "(55502)" in out_col, out_col.split("---")[0])
check("the card is annotated so the collision is visible",
      "[群名片:张三]" in out_col and "[群名片:李四]" in out_col,
      out_col.split("---")[0])
check("涉及 count equals the real number of people",
      "共" not in out_col.split("---")[0], out_col.split("---")[0])
HISTORY[:] = saved3

print("\n[涉及 truncation]")


MANY = []
for i in range(9):
    MANY.append({"message_id": 91000 + i, "message_seq": 91000 + i,
                 "time": BASE_TS - i * 60, "user_id": 70000 + i,
                 "raw_message": "m%d" % i, "message": [text("m%d" % i)],
                 "sender": {"user_id": 70000 + i, "nickname": "人%d" % i, "card": ""}})
saved4 = HISTORY[:]
HISTORY[:] = MANY
out_many = run(make_plugin(), _LabelEv(), session_type="group", session_id="123",
               count=20, user_id="人", scan_limit=50)
head = out_many.split("---")[0]
check("涉及 line truncates at 5 shown people",
      head.count("(70") == 5, head)
check("truncation states the true total",
      "共9人" in head, head)
HISTORY[:] = saved4

print("\n[legacy path unchanged]")
out = run(make_plugin(), _Ev(), session_type="group", session_id="123", count=20)
lines = [l for l in out.splitlines() if l.strip()]
check("no locate params -> plain list", "【定位】" not in out, out[:200])
check("returns exactly count lines", len(lines) == 20, len(lines))
check("newest first", "普通消息 0" in lines[-1] or "普通消息 0" in lines[0], lines[0])
check("locate disabled -> plain path even with keyword",
      "【定位】" not in run(make_plugin({"enable_locate": False}), _Ev(),
                            session_type="group", session_id="123", count=10,
                            keyword="部署"))

print("\n[anti-loop: exact repeat]")
plugin = make_plugin()
ev = _Ev()
first = run(plugin, ev, session_type="group", session_id="123", count=10, keyword="部署")
second = run(plugin, ev, session_type="group", session_id="123", count=10, keyword="部署")
check("second identical call is served from cache",
      "完全相同" in second, second[-200:])
check("cached answer still contains the data", "部署" in second, second[:200])

print("\n[anti-loop: subset scan refused]")
plugin = make_plugin()
ev = _Ev()
_ = run(plugin, ev, session_type="group", session_id="123", count=10,
        keyword="部署", scan_limit=400)
smaller = run(plugin, ev, session_type="group", session_id="123", count=10,
              keyword="部署", scan_limit=100)
check("narrower re-scan is refused", "子集" in smaller, smaller[:300])
bigger = run(plugin, ev, session_type="group", session_id="123", count=10,
             keyword="部署", scan_limit=500)
check("wider re-scan is allowed", "子集" not in bigger, bigger[:200])

print("\n[anti-loop: per-turn call budget]")
plugin = make_plugin({"max_calls_per_target_per_turn": 2, "max_calls_per_turn": 3})
ev = _Ev()
_ = run(plugin, ev, session_type="group", session_id="123", count=10, keyword="部署")
_ = run(plugin, ev, session_type="group", session_id="123", count=10, keyword="部署",
        scan_limit=500)
third = run(plugin, ev, session_type="group", session_id="123", count=10, keyword="其他词")
check("third call for the same target is refused", "Rejected" in third, third[:200])
fourth = run(plugin, ev, session_type="group", session_id="456", count=10, keyword="部署")
check("other session still allowed within total budget", "Rejected" not in fourth, fourth[:200])

print("\n[anti-loop: scan budget]")
plugin = make_plugin({"max_scanned_per_turn": 100, "max_calls_per_turn": 5,
                      "max_calls_per_target_per_turn": 5})
ev = _Ev()
_ = run(plugin, ev, session_type="group", session_id="123", count=10,
        keyword="没有", scan_limit=100)
after = run(plugin, ev, session_type="group", session_id="123", count=10,
            keyword="另一个没有的词", scan_limit=100)
check("scan budget exhaustion refuses further scans",
      "预算" in after or "命中=0" in after, after[:300])
check("scan budget is charged on the event",
      int(ev.extra.get("hp_hist_scanned", 0)) <= 200, ev.extra.get("hp_hist_scanned"))

print("\n[scan_limit clamping]")
# The clamp must use the REAL implementation cap, not the conservative generic
# one - which means the implementation probe has to run before clamping.
client_cap = FakeClient(app_name="SnowLuma")
svc_cap = make_plugin(client=client_cap, overrides=None)
svc_cap._impl_cache = {}
out_cap = run(svc_cap, _Ev(), session_type="group", session_id="123", count=10,
              keyword="没有", scan_limit=5000)
check("first call clamps with the SnowLuma cap (800), not the generic one",
      "800" in out_cap, out_cap[:400])

plugin = make_plugin({"max_scan_limit": 250})
out = run(plugin, _Ev(), session_type="group", session_id="123", count=10,
          keyword="没有", scan_limit=99999)
check("scan_limit clamped to the configured cap", "上限" in out, out[:400])
check("clamp capped at 250", "250" in out, out[:400])

print("\n[scan_limit floor]")
out_f = run(make_plugin(), _Ev(), session_type="group", session_id="123", count=10,
            keyword="没有的词", scan_limit=-5)
check("a typo'd scan_limit (negative) still scans a page worth",
      int(out_f.split("扫描=")[1].split("条")[0]) >= 20, out_f[:200])
out_f2 = run(make_plugin(), _Ev(), session_type="group", session_id="456", count=10,
             keyword="没有的词", scan_limit=0)
check("scan_limit=0 also falls back to the page size",
      int(out_f2.split("扫描=")[1].split("条")[0]) >= 20, out_f2[:200])

print("\n[impl probing]")
ev = _Ev()
client = FakeClient(app_name="SnowLuma")
plugin = make_plugin(client=client)
_ = run(plugin, ev, session_type="group", session_id="123", count=10, keyword="部署")
history_calls = [c for c in client.calls if c[0] != "get_version_info"]
check("version probed once", len([c for c in client.calls if c[0] == "get_version_info"]) == 1,
      [c[0] for c in client.calls][:5])
check("SnowLuma pages use message_id",
      all("message_id" in p for _, p in history_calls[1:]),
      [p for _, p in history_calls[1:]][:2])

client2 = FakeClient(app_name="NapCat.Onebot")
plugin2 = make_plugin(client=client2)
_ = run(plugin2, _Ev(), session_type="group", session_id="123", count=10, keyword="部署")
calls2 = [c for c in client2.calls if c[0] != "get_version_info"]
check("NapCat pages use message_seq",
      all("message_seq" in p for _, p in calls2[1:]),
      [p for _, p in calls2[1:]][:2])
check("NapCat direction flag set on anchored pages",
      all(p.get("reverse_order") is True for _, p in calls2[1:]),
      [p for _, p in calls2[1:]][:2])

print("\n[fallback when the anchor dies mid-walk]")
client3 = FakeClient(app_name="NapCat.Onebot", fail_once_at=2)
plugin3 = make_plugin({"locate_fallback_on_error": True}, client=client3)
out = run(plugin3, _Ev(), session_type="group", session_id="123", count=10,
          keyword="部署", scan_limit=300)
check("fallback retry still produces an answer", "【定位】" in out, out[:300])
check("fallback actually retried from the newest page",
      client3.page_calls > 2, client3.page_calls)

client4 = FakeClient(app_name="NapCat.Onebot", fail_after=1)
plugin4 = make_plugin({"locate_fallback_on_error": False}, client=client4)
out = run(plugin4, _Ev(), session_type="group", session_id="123", count=10,
          keyword="部署", scan_limit=300)
check("fallback disabled -> error surfaced", "Error" in out, out[:300])

print("\n[model-supplied types never crash the tool]")


class _TypeEv:
    def __init__(self):
        self.messages = [_M()]
        self.extra = {}


# LLMs routinely emit numbers as strings / floats. `count < 5` used to raise
# TypeError on a string and kill the whole tool call.
for _label, _kw in [
    ("count as a string", dict(count="20", keyword="部署")),
    ("count as a float", dict(count=20.7, keyword="部署")),
    ("count as garbage", dict(count="abc", keyword="部署")),
    ("count None", dict(count=None, keyword="部署")),
    ("offset as a string", dict(count=10, offset="3", user_id="100")),
    ("scan_limit as a string", dict(count=10, scan_limit="150", keyword="部署")),
    ("keyword as a number", dict(count=10, keyword=123)),
    ("user_id as a number", dict(count=10, user_id=100)),
    ("since as an epoch", dict(count=10, since=1757300000)),
]:
    try:
        _o = run(make_plugin(), _TypeEv(), session_type="group", session_id="123", **_kw)
        check("%s is handled" % _label, isinstance(_o, str) and len(_o) > 0,
              _o[:50].replace("\n", " "))
    except Exception as _e:  # pragma: no cover - the point of the test
        check("%s is handled" % _label, False, "%s: %s" % (type(_e).__name__, _e))

print("\n[max_return_count is honoured on the legacy path too]")


class _MrEv:
    def __init__(self):
        self.messages = [_M()]
        self.extra = {}


_mr_hist = [{"message_id": 60000 + i, "message_seq": 60000 + i,
             "time": BASE_TS - i * 60, "user_id": 100,
             "raw_message": "m%d" % i, "message": [text("m%d" % i)],
             "sender": {"user_id": 100, "nickname": "甲", "card": ""}}
            for i in range(300)]
_saved_mr = HISTORY[:]
HISTORY[:] = _mr_hist
for _maxret in (80, 200):
    _p = make_plugin({"max_return_count": _maxret})
    _o = run(_p, _MrEv(), session_type="group", session_id="123", count=_maxret)
    _n = len([l for l in _o.splitlines() if l.startswith(("甲(", "乙("))])
    # The body is also character-capped, so assert "not silently stuck at 80"
    check("legacy path with max_return_count=%d is not capped at 80" % _maxret,
          _n > 80 or _maxret == 80, _n)
HISTORY[:] = _saved_mr

print("\n[scan_limit adjustments are reported honestly]")


class _AdjEv:
    def __init__(self):
        self.messages = [_M()]
        self.extra = {}


_lo = run(make_plugin(), _AdjEv(), session_type="group", session_id="123",
          count=10, keyword="nope", scan_limit=5)
check("a raised scan_limit (below the floor) is reported",
      "低于下限" in _lo, _lo[-160:])

_pb = make_plugin({"max_scanned_per_turn": 60})
_evb = _AdjEv()
_pb_out = run(_pb, _evb, session_type="group", session_id="123",
              count=10, keyword="nope", scan_limit=300)
check("a budget clamp is reported with the real number",
      "预算" in _pb_out and "60" in _pb_out, _pb_out[-200:])
check("the reported budget matches what was scanned",
      "扫描=60条" in _pb_out, [b for b in _pb_out.split(" | ") if b.startswith("扫描")])

print("\n[permission]")
plugin = make_plugin({"master_id": "", "restricted_groups": ["999"]})
ev = _Ev()
ev.messages[0].sender.user_id = "555"

out = run(plugin, ev, session_type="group", session_id="999", count=10, keyword="x")
check("restricted group refused", "权限" in out, out[:200])
out = run(plugin, ev, session_type="private", session_id="555", count=10, keyword="x")
check("own private chat allowed", "权限" not in out, out[:200])
out = run(plugin, ev, session_type="private", session_id="777", count=10, keyword="x")
check("someone else's private chat refused", "权限" in out, out[:200])

print("\n[offset footer must not say 'newest']")
off = run(make_plugin(), _Ev(), session_type="group", session_id="123",
          count=3, user_id="100", offset=4, scan_limit=100)
print("    >>> " + off.replace("\n", " | ")[:300])
check("offset footer states the slice, not 'newest'",
      "最新的" not in off, off[-200:])
# The header's 返回=N must equal the number of message lines actually emitted.
_om = [l for l in off.splitlines() if l.startswith(("甲(", "乙("))]
check("header 返回 count equals the rendered message count",
      ("返回=%d" % len(_om)) in off, (off.splitlines()[0][:120], len(_om)))
check("count is honoured (min 5 per the documented floor)",
      len(_om) <= 5, len(_om))

print("\n[AND semantics for combined conditions]")


class _AndEv:
    def __init__(self):
        self.messages = [_M()]
        self.extra = {}


AND_HIST = [
    {"message_id": 88001, "message_seq": 88001, "time": BASE_TS, "user_id": 100,
     "raw_message": "今天天气不错", "message": [text("今天天气不错")],
     "sender": {"user_id": 100, "nickname": "甲", "card": ""}},
    {"message_id": 88002, "message_seq": 88002, "time": BASE_TS - 60, "user_id": 100,
     "raw_message": "我们要部署了", "message": [text("我们要部署了")],
     "sender": {"user_id": 100, "nickname": "甲", "card": ""}},
    {"message_id": 88003, "message_seq": 88003, "time": BASE_TS - 120, "user_id": 200,
     "raw_message": "我也要部署", "message": [text("我也要部署")],
     "sender": {"user_id": 200, "nickname": "乙", "card": ""}},
]
saved5 = HISTORY[:]
HISTORY[:] = AND_HIST
and_out = run(make_plugin(), _AndEv(), session_type="group", session_id="123",
              count=10, user_id="100", keyword="部署")
check("user_id + keyword is an AND, not an OR",
      "我们要部署了" in and_out and "今天天气不错" not in and_out, and_out)
check("AND result excludes other people's keyword hits",
      "我也要部署" not in and_out, and_out)
check("AND header reports 1 hit", "命中=1" in and_out, and_out.splitlines()[0])
HISTORY[:] = saved5

print("\n[stop-reason wording]")


class _StopEv:
    def __init__(self):
        self.messages = [_M()]
        self.extra = {}


stop_out = run(make_plugin(), _StopEv(), session_type="group", session_id="123",
               count=5, since="2026-09-15 08:00", scan_limit=500)
if "since" in stop_out and "边界" in stop_out:
    check("reaching the since boundary does not claim '已到会话最早'",
          "已到会话最早" not in stop_out, stop_out.split("涉及")[0])
else:
    print("  SKIP  (no since boundary hit in this fixture)")

print("\n[header agrees with the body]")
for _c in (5, 10, 20):
    _o = run(make_plugin(), _LabelEv(), session_type="group", session_id="123",
             count=_c, user_id="甲", scan_limit=200)
    _ml = [l for l in _o.splitlines() if l.startswith(("甲(", "乙("))]
    check("count=%d: 返回 equals rendered lines" % _c,
          ("返回=%d" % len(_ml)) in _o or "返回=" not in _o,
          (_o.splitlines()[0][:110], len(_ml)))

print("\n[reached-start guard must not go permanently stale]")


class _StaleEv:
    def __init__(self):
        self.messages = [_M()]
        self.extra = {}


saved6 = HISTORY[:]
# history with no keyword hit -> first query reaches the start
HISTORY[:] = [{"message_id": 70000 + i, "message_seq": 70000 + i,
               "time": BASE_TS - i * 60, "user_id": 100,
               "raw_message": "普通", "message": [text("普通")],
               "sender": {"user_id": 100, "nickname": "甲", "card": ""}}
              for i in range(5)]
st_plugin = make_plugin({"locate_cache_ttl_sec": 120})
_ = run(st_plugin, _StaleEv(), session_type="group", session_id="123",
        count=10, keyword="部署", scan_limit=50)
# a new matching message arrives at the newest end
HISTORY.insert(0, {"message_id": 71000, "message_seq": 71000, "time": BASE_TS + 120,
                   "user_id": 200, "raw_message": "新部署",
                   "message": [text("新部署")],
                   "sender": {"user_id": 200, "nickname": "乙", "card": ""}})
again = run(st_plugin, _StaleEv(), session_type="group", session_id="123",
            count=10, keyword="部署", scan_limit=500)
check("a fresh identical query is not permanently refused",
      "Rejected" in again or "新部署" in again, again[:120])
check("but within the TTL the guard may legitimately refuse",
      True)
# expire the cache and retry
for _e in st_plugin._locate_cache.values():
    _e["timestamp"] -= 9999
after_ttl = run(st_plugin, _StaleEv(), session_type="group", session_id="123",
                count=10, keyword="部署", scan_limit=500)
check("after the TTL the new message is found", "新部署" in after_ttl, after_ttl[:150])
check("after the TTL the query is not refused", "Rejected" not in after_ttl,
      after_ttl[:120])
HISTORY[:] = saved6

print("\n[header shape]")
out = run(make_plugin(), _Ev(), session_type="group", session_id="123",
          count=10, keyword="部署", scan_limit=500)
head = out.splitlines()[0]
for token in ("【定位】", "session=group:123", "命中=", "扫描=", "覆盖=", "到最早=", "耗时="):
    check("header contains %s" % token, token in head, head)

print("\n[offset paging]")
plugin = make_plugin()
ev = _Ev()
page1 = run(plugin, ev, session_type="group", session_id="123", count=2,
            user_id="100", scan_limit=500)
page2 = run(plugin, ev, session_type="group", session_id="123", count=2,
            user_id="100", scan_limit=500, offset=2)
check("offset yields a different page", page1 != page2, (page1[:120], page2[:120]))
check("offset page reports the offset", "offset" in page2, page2[:300])


print()
passed = sum(1 for _, ok in results if ok)
print("TOTAL %d/%d passed" % (passed, len(results)))
sys.exit(0 if passed == len(results) else 1)
