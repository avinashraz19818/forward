"""
Regression suite for the defects found in forward.py.

Everything runs offline: the Telegram layer is faked so each defect can be
proved (and each fix verified) deterministically, without network access or a
real session file.

Run with:  .venv/bin/python tests/repro_bugs.py
Exit code: 0 = all fixed, 1 = at least one defect still present.
"""

import asyncio
import json
import os
import signal
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

os.environ.setdefault("BOT_TOKEN", "dummy:token-for-tests")

import forward  # noqa: E402
from forward import (ForwarderBot, SKIPPED, TRANSIENT,  # noqa: E402
                     MAPPINGS_MAX, MAX_BUTTON_BYTES)

# Never write into the real data files — redirect the module's paths.
_TMP = Path(tempfile.mkdtemp(prefix="fwd-tests-"))
forward.CONFIGS_FILE = str(_TMP / "user_configs.json")
forward.FILTERS_FILE = str(_TMP / "user_filters.json")
forward.MAPS_FILE = str(_TMP / "user_msg_maps.json")
forward.PROCESSED_FILE = str(_TMP / "processed_messages.json")
forward.SESSIONS_DIR = _TMP / "sessions"
forward.MEDIA_FILES_DIR = _TMP / "media_files"
forward.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
forward.MEDIA_FILES_DIR.mkdir(parents=True, exist_ok=True)

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    mark = "FIXED" if ok else "BUG "
    print(f"[{mark}] {name}" + (f"\n        -> {detail}" if detail else ""))


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------
class FakeClient:
    """Mimics the slice of TelegramClient the forwarding worker touches.

    add/remove_event_handler reproduce Telethon's real semantics: handlers are
    stored as (builder, callback) and removal compares `cb == callback` plus
    `isinstance(builder, type(event))`.
    """

    def __init__(self, cycles=4, on_last=None):
        self.builders = []
        self.max_seen = 0          # peak number of live handlers
        self.removed_total = 0     # how many remove_event_handler calls hit
        self.sent = []
        self.cycles = 0
        self.max_cycles = cycles
        self.on_last = on_last

    # --- connection ---
    def is_connected(self):
        # Return False right away so the worker's inner loop breaks without
        # sleeping; the outer loop then re-registers handlers = one "cycle".
        self.cycles += 1
        if self.cycles >= self.max_cycles and self.on_last:
            self.on_last()
        return False

    # --- handlers ---
    def add_event_handler(self, cb, event=None):
        self.builders.append((event, cb))
        self.max_seen = max(self.max_seen, len(self.builders))

    def remove_event_handler(self, cb, event=None):
        found = 0
        i = len(self.builders)
        while i:
            i -= 1
            ev, c = self.builders[i]
            if c == cb and (not event or isinstance(ev, type(event))):
                del self.builders[i]
                found += 1
        self.removed_total += found
        return found

    # --- sending ---
    async def send_message(self, dest, text, **kw):
        if not text:
            raise RuntimeError("MESSAGE_EMPTY_NOT_ALLOWED")
        self.sent.append((dest, text))
        return type("M", (), {"id": len(self.sent)})()

    async def send_file(self, dest, file, **kw):
        self.sent.append((dest, "file"))
        return type("M", (), {"id": len(self.sent)})()

    async def forward_messages(self, *a, **k):
        self.sent.append(a)
        return type("M", (), {"id": len(self.sent)})()

    async def delete_messages(self, *a, **k):
        return None

    async def edit_message(self, *a, **k):
        return None

    async def disconnect(self):
        return None


class Ev:
    """Minimal stand-in for a Telethon message/callback event."""

    def __init__(self, sender_id=1, text=None, media=None):
        self.sender_id = sender_id
        self.edits = []
        self.responds = []
        self.answers = []

        async def _download(*a, **k):
            return None

        self.message = type("Msg", (), {
            "text": text, "raw_text": text, "media": media,
            "download_media": _download, "id": 1, "chat_id": -1001,
        })()

    async def edit(self, *a, **k):
        self.edits.append((a, k))

    async def respond(self, *a, **k):
        self.responds.append((a, k))

    async def answer(self, *a, **k):
        self.answers.append((a, k))


def make_msg(mid, chat_id=-100123, text="hello", media=None, action=None,
             grouped=None, entities=None):
    return type("Msg", (), {
        "id": mid, "chat_id": chat_id, "message": text, "media": media,
        "action": action, "grouped_id": grouped, "entities": entities,
    })()


def active_config(bot, uid, source="-100123", dests=None):
    cfg = bot.get_config(uid)
    cfg.update(
        logged_in=True, forwarding_active=True, source_chat=source,
        destinations=dests if dests is not None else [{"id": "-100999", "name": "d"}],
    )
    return cfg


# ==========================================================================
# B1  `time` was used but never imported -> NameError
# ==========================================================================
def t01_time_imported():
    check("B1  `time` imported", hasattr(forward, "time"),
          "" if hasattr(forward, "time") else "NameError at 3 call sites")


# ==========================================================================
# B2  handlers leaked on every reconnect (duplicate forwarding)
# ==========================================================================
def t02_handler_leak():
    bot, uid = ForwarderBot(), 123
    active_config(bot, uid)
    client = FakeClient(cycles=4, on_last=lambda: bot.get_config(uid).update(forwarding_active=False))

    async def fake_get_client(_uid):
        bot.get_user_state(uid).client = client
        bot.get_user_state(uid).client_authorized = True
        return client

    bot.get_client = fake_get_client
    run(bot._forwarding_worker(uid))

    # The worker clears handlers in its `finally`, so 0 remain afterwards; the
    # number that matters is the PEAK while it was running. A leak would have
    # shown 3*cycles here and forwarded every message that many times.
    peak = client.max_seen
    ok = peak == 3 and client.removed_total > 0 and client.cycles >= 4
    check("B2  handlers stay at 3 across reconnect cycles", ok,
          f"peak live handlers={peak} over {client.cycles} cycles, "
          f"successful removals={client.removed_total}, "
          f"left after shutdown={len(client.builders)}")


# ==========================================================================
# B3  "Restart" button was a no-op
# ==========================================================================
def t03_restart():
    bot, uid = ForwarderBot(), 999
    active_config(bot, uid)
    restarted = {"v": False}

    async def fake_start(_uid):
        restarted["v"] = True

    bot.start_forwarding = fake_start
    run(bot.restart_forwarding(Ev(uid)))
    check("B3  Restart actually restarts forwarding", restarted["v"] is True,
          "start_forwarding() was never called")


# ==========================================================================
# B4  media-filter flow crashed on a caption-less photo
# ==========================================================================
def t04_captionless_media():
    bot, uid = ForwarderBot(), 555
    bot.get_config(uid)["current_step"] = "media_original"

    photo = type("P", (), {"id": 4242, "access_hash": 1, "file_reference": b"x",
                           "mime_type": "image/jpeg"})()
    media = type("Media", (), {"photo": photo, "document": None})()

    target = _TMP / "downloaded.jpg"

    async def _download(*a, **k):
        target.write_bytes(b"\xff\xd8fakejpeg")
        return str(target)

    ev = Ev(uid, text=None, media=media)          # caption-less
    ev.message.download_media = _download

    try:
        run(bot.handle_private_message(ev))
        crashed, err = False, ""
    except AttributeError as e:
        crashed, err = True, f"AttributeError: {e}"

    temp = bot.temp_filters.get(uid, {})
    ok = (not crashed) and temp.get("original_file") == str(target) \
        and bot.get_config(uid)["current_step"] == "media_replace"
    check("B4  caption-less media no longer crashes the wizard", ok,
          err or f"temp={temp}")


# ==========================================================================
# B5  dedupe cache evicted arbitrary (recent) ids
# ==========================================================================
def t05_dedupe_order():
    bot, uid = ForwarderBot(), 777
    state = bot.get_user_state(uid)
    ids = [7000000 + i * 37 for i in range(2600)]
    for mid in ids:                       # arrival order
        state.mark_processed(mid)

    newest_half = set(ids[len(ids) // 2:])
    kept = set(state.processed_messages)
    lost_recent = len(newest_half - kept)
    evicted_are_oldest = kept == set(ids[-len(kept):])
    check("B5  dedupe evicts oldest ids, keeps recent", lost_recent == 0 and evicted_are_oldest,
          f"{lost_recent} recent ids lost; retained set is the newest suffix: {evicted_are_oldest}")


# ==========================================================================
# B6  messages were blacklisted before delivery
# ==========================================================================
def t06_processed_after_send():
    bot, uid = ForwarderBot(), 4242
    active_config(bot, uid)

    async def no_client(_uid):
        return None

    bot.get_client = no_client
    run(bot.forward_message(uid, make_msg(5)))

    offline_ok = not bot.get_user_state(uid).is_processed(5)

    # and the happy path still marks it
    bot2, uid2 = ForwarderBot(), 4243
    active_config(bot2, uid2)
    client = FakeClient()

    async def ok_client(_uid):
        return client

    bot2.get_client = ok_client
    run(bot2.forward_message(uid2, make_msg(7)))
    online_ok = bot2.get_user_state(uid2).is_processed(7) and len(client.sent) == 1

    check("B6  processed only after a real delivery attempt", offline_ok and online_ok,
          f"offline-left-retryable={offline_ok}, online-marked={online_ok}")


# ==========================================================================
# B7  filters emitted entity offsets past the end of the text
# ==========================================================================
class _Ent:
    def __init__(self, offset, length):
        self.offset, self.length = offset, length

    def __repr__(self):
        return f"Ent({self.offset},{self.length})"


def t07_entity_offsets():
    bot = ForwarderBot()
    cases = [
        ("hello world", [_Ent(0, 11)], [{"find": "world", "replace": ""}]),
        ("join t.me/abc now", [_Ent(5, 9)], [{"find": "join ", "replace": ""}]),
        ("aXbXc", [_Ent(0, 5)], [{"find": "X", "replace": "yyyy"}]),
        ("bold link", [_Ent(0, 4), _Ent(5, 4)], [{"find": "bold", "replace": "b"}]),
    ]
    problems = []
    for text, ents, filters in cases:
        out, new_ents = bot.apply_text_filters(text, ents, filters)
        for e in new_ents:
            if e.offset < 0 or e.offset + e.length > len(out):
                problems.append(f"{text!r} -> {out!r} {e}")
    check("B7  entity offsets always stay inside the text", not problems,
          "; ".join(problems))


# ==========================================================================
# B8  bot's own replies were parsed as user input
# ==========================================================================
def t08_incoming_guard():
    src = (ROOT / "forward.py").read_text(encoding="utf-8")
    start = src.index("async def start_bot")
    end = src.index("async def cancel_step")
    body = src[start:end]
    guarded = body.count("incoming=True") >= 4
    check("B8  every bot handler is incoming=True", guarded,
          f"found {body.count('incoming=True')} guarded registrations")


# ==========================================================================
# B9  inline button text exceeded Telegram's 64-byte limit
# ==========================================================================
def t09_button_bytes():
    cfg = json.loads((ROOT / "user_configs.json").read_text(encoding="utf-8"))
    names = [d["name"] for c in cfg.values() for d in c.get("destinations", [])]
    names.append("🟢𝐁𝐃𝐆 𝐖𝐈𝐍 𝐌𝐀𝐗𝐈𝐄 𝟑 𝐌𝐈𝐍 𝐖𝐈𝐍𝐆𝐎🔴")

    worst = 0
    for n in names:
        worst = max(worst, len(forward.clip_bytes(f"🗑️ {n}", MAX_BUTTON_BYTES).encode("utf-8")))

    bot, uid = ForwarderBot(), 8089603563
    bot.get_config(uid)["destinations"] = [{"id": "-1", "name": n} for n in names]
    ev = Ev(uid)
    run(bot.show_destinations(ev))
    buttons = ev.edits[-1][1].get("buttons") or []
    btn_worst = max((len(b.text.encode("utf-8")) for row in buttons for b in row), default=0)

    check("B9  all button labels <= 64 bytes", worst <= 64 and btn_worst <= 64,
          f"clip_bytes worst={worst}, rendered keyboard worst={btn_worst}")


# ==========================================================================
# B10  JSON writes were not atomic
# ==========================================================================
def t10_atomic_save():
    bot = ForwarderBot()
    bot._save_json(forward.CONFIGS_FILE, {"a": 1})
    leftovers = list(_TMP.glob("*.tmp"))
    data = json.loads(Path(forward.CONFIGS_FILE).read_text())
    src = (ROOT / "forward.py").read_text(encoding="utf-8")
    ok = ("os.replace" in src) and not leftovers and data == {"a": 1}
    check("B10  atomic save (temp file + os.replace)", ok,
          f"tmp leftovers={leftovers}, data={data}")


# ==========================================================================
# B11  SIGTERM/SIGINT did not stop the bot
# ==========================================================================
def t11_signal_shutdown():
    bot = ForwarderBot()

    async def scenario():
        bot._loop = asyncio.get_running_loop()
        bot._shutdown_event = asyncio.Event()
        handler = signal.getsignal(signal.SIGTERM)
        handler(signal.SIGTERM, None)
        await asyncio.sleep(0.05)
        return bot.running, bot._shutdown_event.is_set()

    running, event_set = run(scenario())
    has_shutdown = callable(getattr(bot, "shutdown", None))
    ok = (running is False) and event_set and has_shutdown
    check("B11  SIGTERM flips running AND wakes the loop", ok,
          f"running={running}, shutdown_event_set={event_set}, has shutdown()={has_shutdown}")


# ==========================================================================
# B12  secrets/session files were not gitignored
# ==========================================================================
def t12_gitignore():
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
    needed = (".env", "*.session", "sessions/", "user_configs.json",
              "user_msg_maps.json", "processed_messages.json", "media_files/")
    missing = [p for p in needed if p not in gi]
    check("B12  secrets + runtime state gitignored", not missing,
          "missing: " + ", ".join(missing))


# ==========================================================================
# B13  an empty "find" string hung the event loop forever
# ==========================================================================
def t13_empty_find():
    bot = ForwarderBot()
    box = {}

    def work():
        out, _ = bot.apply_text_filters("abc", [], [{"find": "", "replace": "X"}])
        box["out"] = out

    th = threading.Thread(target=work, daemon=True)
    th.start()
    th.join(2)
    check("B13  empty 'find' filter is skipped, not looped", not th.is_alive(),
          box.get("out", "still spinning after 2s — event loop blocked"))


# ==========================================================================
# B14  service/system messages were sent as empty text
# ==========================================================================
def t14_service_message():
    bot, uid = ForwarderBot(), 31337
    active_config(bot, uid)
    client = FakeClient()

    async def ok_client(_uid):
        return client

    bot.get_client = ok_client
    svc = make_msg(9, text=None, action=object())
    run(bot.forward_message(uid, svc))
    check("B14  service messages are skipped", not client.sent,
          f"sent={client.sent}")


# ==========================================================================
# B15  albums bypassed the duplicate check
# ==========================================================================
def t15_album_dedupe():
    bot, uid = ForwarderBot(), 606
    active_config(bot, uid)
    client = FakeClient()

    async def ok_client(_uid):
        return client

    bot.get_client = ok_client

    msgs = [make_msg(101, grouped=555, text="cap" if i == 0 else None,
                     media=type("M", (), {"photo": type("P", (), {
                         "id": 1000 + i, "access_hash": 1, "file_reference": b"r"})(),
                         "document": None})())
            for i in range(3)]
    for m in msgs:
        bot.get_user_state(uid).mark_processed(m.id)

    bot.album_buffers[uid][555].extend(msgs)
    run(bot.forward_album(uid, 555))
    check("B15  albums respect the duplicate check", not client.sent,
          f"re-sent {len(client.sent)} album messages")


# ==========================================================================
# B16  logout left an authorised session file on disk
# ==========================================================================
def t16_logout_session():
    bot, uid = ForwarderBot(), 8181
    forward.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    sess = forward.SESSIONS_DIR / f"user_{uid}.session"
    sess.write_bytes(b"fake")

    run(bot.logout(Ev(uid)))
    check("B16  logout deletes the session file", not sess.exists(),
          f"{sess} still present")


# ==========================================================================
# B17  changing the source chat left handlers on the old chat
# ==========================================================================
def t17_source_rebind():
    bot, uid = ForwarderBot(), 9090
    active_config(bot, uid, source="-1001")
    rebound = {"v": False}

    async def fake_rebind(_uid):
        rebound["v"] = True

    bot.restart_forwarding_worker = fake_rebind

    class FakeEntity:
        title = "New Channel"

    client = FakeClient()
    client.get_entity = lambda _id: asyncio.sleep(0, result=FakeEntity())

    async def ok_client(_uid):
        return client

    bot.get_client = ok_client
    run(bot.set_source(Ev(uid), "-1002"))

    ok = rebound["v"] and bot.get_config(uid)["source_chat"] == "-1002"
    check("B17  set_source rebinds live handlers", ok,
          f"rebound={rebound['v']}, source={bot.get_config(uid)['source_chat']}")


# ==========================================================================
# B18  user_msg_maps.json grew without bound
# ==========================================================================
def t18_mapping_trim():
    bot, uid = ForwarderBot(), 707
    maps = bot.get_mappings(uid)
    for i in range(MAPPINGS_MAX + 5000):
        maps[str(i)] = [{"dest": -1, "msg_id": i}]
    bot.trim_mappings(uid)

    size = len(maps)
    kept_newest = maps.get(str(MAPPINGS_MAX + 4999)) is not None
    dropped_oldest = maps.get("0") is None
    check("B18  message map is capped, newest kept",
          size <= MAPPINGS_MAX and kept_newest and dropped_oldest,
          f"size={size}, kept_newest={kept_newest}, dropped_oldest={dropped_oldest}")


# ==========================================================================
# B19  media filters stored a path that never existed (extension mismatch)
# ==========================================================================
def t19_media_filter_path():
    bot, uid = ForwarderBot(), 3131
    bot.get_config(uid)["current_step"] = "media_original"

    photo = type("P", (), {"id": 777, "access_hash": 1, "file_reference": b"x",
                           "mime_type": "image/jpeg"})()
    media = type("Media", (), {"photo": photo, "document": None})()

    real_path = _TMP / "telethon_would_add_an_ext.jpg"

    async def _download(*a, **k):
        real_path.write_bytes(b"\xff\xd8jpeg")
        return str(real_path)          # Telethon returns the REAL path

    ev = Ev(uid, text="caption", media=media)
    ev.message.download_media = _download
    run(bot.handle_private_message(ev))

    stored = bot.temp_filters.get(uid, {}).get("original_file")
    ok = stored is not None and Path(stored).exists()
    check("B19  stored media-filter path actually exists", ok,
          f"stored={stored}")


# ==========================================================================
# B20  text filter round-trip keeps content correct
# ==========================================================================
def t20_filter_correctness():
    bot = ForwarderBot()

    # equal-length replacement: the formatting entity must survive untouched
    out, ents = bot.apply_text_filters(
        "Join @oldchannel now! @oldchannel rocks",
        [_Ent(5, 12)],
        [{"find": "@oldchannel", "replace": "@newchannel"}])
    ok1 = (out == "Join @newchannel now! @newchannel rocks"
           and len(ents) == 1 and ents[0].offset == 5 and ents[0].length == 12)

    # plain replacement
    out2, _ = bot.apply_text_filters("abc", [], [{"find": "b", "replace": "XYZ"}])
    ok2 = out2 == "aXYZc"

    # shrinking text must clamp the entity instead of overshooting it
    out3, ents3 = bot.apply_text_filters("hello world", [_Ent(0, 11)],
                                         [{"find": "world", "replace": ""}])
    ok3 = (out3 == "hello " and len(ents3) == 1
           and ents3[0].offset + ents3[0].length <= len(out3))

    check("B20  filter replacement correct end-to-end", ok1 and ok2 and ok3,
          f"entity-kept={ok1} ({ents}), shrink={ok2} ({out2!r}), clamp={ok3} "
          f"({out3!r} {ents3})")


ALL = [t01_time_imported, t02_handler_leak, t03_restart, t04_captionless_media,
       t05_dedupe_order, t06_processed_after_send, t07_entity_offsets,
       t08_incoming_guard, t09_button_bytes, t10_atomic_save,
       t11_signal_shutdown, t12_gitignore, t13_empty_find,
       t14_service_message, t15_album_dedupe, t16_logout_session,
       t17_source_rebind, t18_mapping_trim, t19_media_filter_path,
       t20_filter_correctness]

if __name__ == "__main__":
    print("=" * 78)
    print("  forward.py  —  DEFECT REGRESSION SUITE")
    print("=" * 78)
    for fn in ALL:
        try:
            fn()
        except Exception as e:
            import traceback
            check(fn.__name__, False, f"harness error: {type(e).__name__}: {e}")
            traceback.print_exc()

    fixed = [r for r in RESULTS if r[1]]
    broken = [r for r in RESULTS if not r[1]]
    print("=" * 78)
    print(f"  {len(fixed)} / {len(RESULTS)} verified fixed")
    if broken:
        print("  STILL BROKEN:")
        for name, _, detail in broken:
            print(f"    - {name}: {detail}")
    print("=" * 78)
    sys.exit(1 if broken else 0)
