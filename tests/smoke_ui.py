"""
Offline smoke test: renders every bot screen against the real user_configs.json
with a faked Telegram layer. Catches attribute errors, byte-limit violations and
KeyErrors in the UI paths without touching the network.

Run with:  .venv/bin/python tests/smoke_ui.py
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
os.environ.setdefault("BOT_TOKEN", "dummy:token-for-tests")

import forward  # noqa: E402
from forward import ForwarderBot, MAX_BUTTON_BYTES, MAX_MESSAGE_LEN  # noqa: E402

# Read the REAL data files but never write back into them: copy them into a
# tempdir and point the module's paths there before constructing the bot.
_TMP = Path(tempfile.mkdtemp(prefix="fwd-smoke-"))
for name in ("user_configs.json", "user_filters.json",
             "user_msg_maps.json", "processed_messages.json"):
    src = ROOT / name
    if src.exists():
        shutil.copy2(src, _TMP / name)

forward.CONFIGS_FILE = str(_TMP / "user_configs.json")
forward.FILTERS_FILE = str(_TMP / "user_filters.json")
forward.MAPS_FILE = str(_TMP / "user_msg_maps.json")
forward.PROCESSED_FILE = str(_TMP / "processed_messages.json")
forward.SESSIONS_DIR = _TMP / "sessions"
forward.MEDIA_FILES_DIR = _TMP / "media_files"
forward.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
forward.MEDIA_FILES_DIR.mkdir(parents=True, exist_ok=True)

FAILURES = []


class RecEvent:
    """Records what the bot tried to send, and validates Telegram limits."""

    def __init__(self, sender_id, label):
        self.sender_id = sender_id
        self.label = label
        self.payloads = []

    def _validate(self, text, buttons, where):
        if text is not None:
            if len(text) > MAX_MESSAGE_LEN:
                FAILURES.append(f"{self.label}/{where}: message {len(text)} > {MAX_MESSAGE_LEN}")
        for row in buttons or []:
            for b in row if isinstance(row, (list, tuple)) else [row]:
                t = getattr(b, "text", None)
                if t is None:
                    continue
                if len(t.encode("utf-8")) > MAX_BUTTON_BYTES:
                    FAILURES.append(
                        f"{self.label}/{where}: button {len(t.encode('utf-8'))}B > "
                        f"{MAX_BUTTON_BYTES}B -> {t!r}")
        if buttons and len(buttons) > forward.MAX_BUTTON_ROWS:
            FAILURES.append(f"{self.label}/{where}: {len(buttons)} button rows > 100")

    async def edit(self, text=None, *, buttons=None, **kw):
        self.payloads.append(("edit", text, buttons))
        self._validate(text, buttons, "edit")

    async def respond(self, text=None, *, buttons=None, **kw):
        self.payloads.append(("respond", text, buttons))
        self._validate(text, buttons, "respond")

    async def answer(self, message=None, **kw):
        self.payloads.append(("answer", message, None))

    @property
    def last_text(self):
        for _kind, text, _b in reversed(self.payloads):
            if text:
                return text
        return ""


def run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def main():
    configs = json.loads((ROOT / "user_configs.json").read_text(encoding="utf-8"))
    filters = json.loads((ROOT / "user_filters.json").read_text(encoding="utf-8"))

    bot = ForwarderBot()

    # Chat-picking screens would otherwise open a real TelegramClient and try to
    # reach the network. Stub the client away so they take the "please login"
    # branch instead — we are validating rendering/limits, not connectivity.
    async def no_client(_uid):
        return None

    bot.get_client = no_client

    print(f"Loaded {len(configs)} real user config(s), {len(filters)} filter set(s)\n")

    screens = [
        ("show_menu", bot.show_menu),
        ("show_help", bot.show_help),
        ("show_status", bot.show_status),
        ("show_destinations", bot.show_destinations),
        ("show_filters_menu", bot.show_filters_menu),
        ("view_text_filters", bot.view_text_filters),
        ("view_media_filters", bot.view_media_filters),
    ]

    for uid_str, cfg in configs.items():
        uid = int(uid_str)
        print(f"--- user {uid}  ({cfg.get('source_chat_name')}) ---")
        for name, fn in screens:
            ev = RecEvent(uid, f"{uid}:{name}")
            try:
                run(fn(ev))
                kind, text, _b = ev.payloads[-1] if ev.payloads else ("none", "", None)
                first = (text or "").strip().splitlines()[0] if text else "(empty)"
                print(f"  ok   {name:<20} {kind:<8} {first[:60]}")
            except Exception as e:
                FAILURES.append(f"{uid}:{name} raised {type(e).__name__}: {e}")
                print(f"  FAIL {name:<20} {type(e).__name__}: {e}")

        # callback dispatch for every static button id in the menu
        for data in (b"back", b"help", b"manage_dests", b"manage_filters",
                     b"toggle_tag", b"set_source", b"add_dest"):
            ev = RecEvent(uid, f"{uid}:cb:{data!r}")
            ev.data = data
            try:
                run(bot.handle_callback(ev))
            except Exception as e:
                FAILURES.append(f"{uid}:cb:{data!r} raised {type(e).__name__}: {e}")
                print(f"  FAIL callback {data!r}: {type(e).__name__}: {e}")
        print()

    # unicode-heavy names: the exact case that broke the destination keyboard
    stress = [
        "🟢𝐁𝐃𝐆 𝐖𝐈𝐍 𝐌𝐀𝐗𝐈𝐄 𝟑 𝐌𝐈𝐍 𝐖𝐈𝐍𝐆𝐎🔴",
        "𝗩𝗲𝗿𝘆 𝗟𝗼𝗻𝗴 𝗨𝗻𝗶𝗰𝗼𝗱𝗲 𝗖𝗵𝗮𝗻𝗻𝗲𝗹 𝗡𝗮𝗺𝗲 𝗧𝗵𝗮𝘁 𝗚𝗼𝗲𝘀 𝗢𝗻",
        "🇮🇳" * 40,
        "a" * 300,
    ]
    uid = 999
    bot.get_config(uid)["destinations"] = [
        {"id": f"-100{i}", "name": n} for i, n in enumerate(stress)]
    ev = RecEvent(uid, "stress:destinations")
    run(bot.show_destinations(ev))
    print(f"--- stress: {len(stress)} unicode-heavy destination names ---")
    for row in ev.payloads[-1][2] or []:
        for b in row:
            print(f"  {len(b.text.encode('utf-8')):>3}B  {b.text!r}")
    print()

    if FAILURES:
        print("=" * 70)
        print(f" {len(FAILURES)} PROBLEM(S)")
        for f in FAILURES:
            print("   -", f)
        print("=" * 70)
        sys.exit(1)

    print("=" * 70)
    print(" UI smoke test passed - no crashes, no Telegram limit violations")
    print("=" * 70)


if __name__ == "__main__":
    main()
