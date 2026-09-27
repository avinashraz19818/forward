"""
Feature tests for the requested updates:

  1. media filters work for ANY file type (apk, zip, pdf, video, ...)
  2. the replacement is delivered under the EXACT filename the user uploaded
  3. text filters also rewrite the hidden URL of hyperlink entities
  4. media filters apply inside albums too
  5. the new UI renders within Telegram's limits and every button is wired

Fully offline. Run with:  .venv/bin/python tests/test_features.py
"""

import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
os.environ.setdefault("BOT_TOKEN", "dummy:token-for-tests")

import forward  # noqa: E402
from forward import (ForwarderBot, SKIPPED, MAX_BUTTON_BYTES,  # noqa: E402
                     MAX_MESSAGE_LEN)
from telethon.tl.types import (DocumentAttributeFilename, MessageEntityTextUrl,  # noqa: E402
                               MessageEntityBold, InputMediaUploadedDocument,
                               InputMediaUploadedPhoto)

_TMP = Path(tempfile.mkdtemp(prefix="fwd-feat-"))
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

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"\n        -> {detail}" if detail else ""))


def run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------
def fake_media_message(filename, mime, media_id=111, is_photo=False, size=4242):
    """A stand-in for a Telethon message carrying a document/photo."""
    attrs = [DocumentAttributeFilename(file_name=filename)] if filename else []
    doc = type("Doc", (), {"id": media_id, "access_hash": 7, "file_reference": b"r",
                           "mime_type": mime, "attributes": attrs, "size": size})()
    photo = None if not is_photo else type("Photo", (), {
        "id": media_id, "access_hash": 7, "file_reference": b"r",
        "mime_type": mime, "size": size})()
    media = type("Media", (), {"document": None if is_photo else doc, "photo": photo})()
    return type("Msg", (), {
        "id": 1, "chat_id": -1001, "media": media, "message": None,
        "entities": None, "file": None, "download_media": None,
    })()


class UploadingClient:
    """Records what would be sent; fakes upload_file."""

    def __init__(self):
        self.uploads = []
        self.sent = []

    async def upload_file(self, path, **kw):
        self.uploads.append(str(path))
        return type("InputFile", (), {"id": len(self.uploads), "name": Path(path).name,
                                      "parts": 1, "md5": b"", "size": 1})()

    async def send_file(self, dest, file, **kw):
        self.sent.append({"dest": dest, "file": file, **kw})
        if isinstance(file, (list, tuple)):
            return [type("M", (), {"id": i + 1})() for i in range(len(file))]
        return type("M", (), {"id": len(self.sent)})()

    async def send_message(self, dest, text, **kw):
        if not text:
            raise RuntimeError("MESSAGE_EMPTY_NOT_ALLOWED")
        self.sent.append({"dest": dest, "text": text, **kw})
        return type("M", (), {"id": len(self.sent)})()


class Ev:
    def __init__(self, sender_id=1, text=None, media=None):
        self.sender_id = sender_id
        self.edits, self.responds, self.answers = [], [], []

        async def _dl(*a, **k):
            return None

        self.message = type("Msg", (), {"text": text, "raw_text": text, "media": media,
                                        "download_media": _dl, "id": 1, "chat_id": -1001})()

    async def edit(self, text=None, *, buttons=None, **kw):
        self.edits.append((text, buttons))

    async def respond(self, text=None, *, buttons=None, **kw):
        self.responds.append((text, buttons))

    async def answer(self, message=None, **kw):
        self.answers.append(message)

    def all_payloads(self):
        return self.edits + self.responds


# ==========================================================================
# F1 — any file type keeps its extension when stored
# ==========================================================================
def f1_any_file_type():
    bot = ForwarderBot()
    cases = [
        ("MyApp_v2.1.apk", "application/vnd.android.package-archive", ".apk"),
        ("archive.tar.gz", "application/gzip", ".gz"),
        ("report final.pdf", "application/pdf", ".pdf"),
        ("movie.mp4", "video/mp4", ".mp4"),
        ("song.mp3", "audio/mpeg", ".mp3"),
        ("setup.exe", "application/x-msdownload", ".exe"),
        ("data.json", "application/json", ".json"),
        ("", "image/jpeg", ".jpg"),          # no filename -> fall back to mime
        ("weirdfile", "application/octet-stream", ".bin"),
    ]
    problems = []
    for i, (fname, mime, want_ext) in enumerate(cases):
        msg = fake_media_message(fname, mime, media_id=1000 + i)
        target_dir = forward.MEDIA_FILES_DIR
        written = {}

        async def _dl(*a, file=None, **k):
            p = Path(file)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x")
            written["path"] = str(p)
            return str(p)

        msg.download_media = _dl
        got = run(bot._download_filter_media(msg, 1, "orig", str(1000 + i)))
        if not got:
            problems.append(f"{fname or '(none)'}: download returned None")
            continue
        ext = Path(got).suffix
        if ext != want_ext:
            problems.append(f"{fname or '(none)'} ({mime}): got {ext}, want {want_ext}")
        # and the mime must be recoverable from the stored path, because
        # send_file() derives it from the path, not from us
        import mimetypes
        guessed = mimetypes.guess_type(got)[0]
        if want_ext in (".apk", ".pdf", ".mp4", ".mp3", ".json", ".gz", ".jpg") \
                and guessed != mime and want_ext != ".gz":
            problems.append(f"{fname}: path mime {guessed} != original {mime}")

    check("F1  every file type stored with its real extension", not problems,
          "; ".join(problems))


# ==========================================================================
# F2 — replacement is delivered under the uploaded filename
# ==========================================================================
def f2_filename_preserved():
    bot = ForwarderBot()
    client = UploadingClient()

    stored = forward.MEDIA_FILES_DIR / "repl_9_9_1700000000.bin"   # ugly internal name
    stored.write_bytes(b"fake apk bytes")

    mf = {
        "original_id": "555",
        "replace_file": str(stored),
        "replace_name": "MyApp_v2.1.apk",          # what the user uploaded
        "replace_kind": "📦 APK",
        "replace_as_document": False,
    }

    media = run(bot.replacement_input_media(client, mf, str(stored)))

    ok_type = isinstance(media, InputMediaUploadedDocument)
    fname = None
    for attr in getattr(media, "attributes", []) or []:
        if isinstance(attr, DocumentAttributeFilename):
            fname = attr.file_name
    ok_name = fname == "MyApp_v2.1.apk"
    ok_mime = media.mime_type == "application/octet-stream" or media.mime_type

    check("F2  replacement delivered under the uploaded filename",
          ok_type and ok_name and bool(ok_mime),
          f"type={type(media).__name__}, filename={fname!r}, mime={media.mime_type!r}")


def f2b_photo_replacement():
    bot = ForwarderBot()
    client = UploadingClient()
    p = forward.MEDIA_FILES_DIR / "repl_1_1_1.jpg"
    p.write_bytes(b"\xff\xd8jpeg")

    media = run(bot.replacement_input_media(
        client, {"replace_name": "pic.jpg", "replace_as_document": False}, str(p)))
    check("F2b image replacement stays a photo (not a file)",
          isinstance(media, InputMediaUploadedPhoto), type(media).__name__)

    media2 = run(bot.replacement_input_media(
        client, {"replace_name": "pic.jpg", "replace_as_document": True}, str(p)))
    fname = next((a.file_name for a in (media2.attributes or [])
                  if isinstance(a, DocumentAttributeFilename)), None)
    check("F2c image uploaded 'as file' stays a file with its name",
          isinstance(media2, InputMediaUploadedDocument) and fname == "pic.jpg"
          and media2.force_file is True,
          f"type={type(media2).__name__}, name={fname!r}, force_file={media2.force_file}")


# ==========================================================================
# F3 — send_clean uses the filter and never leaks the original
# ==========================================================================
def f3_send_clean_replacement():
    bot, uid = ForwarderBot(), 11
    client = UploadingClient()

    repl = forward.MEDIA_FILES_DIR / "repl_11_11_1.apk"
    repl.write_bytes(b"apk")
    mf = {"original_id": "555", "replace_file": str(repl),
          "replace_name": "Clean.apk", "replace_as_document": False}

    msg = fake_media_message("Original_Ad.apk", "application/vnd.android.package-archive",
                             media_id=555)
    msg.message = "Download here"
    sent = run(bot.send_clean(client, uid, msg, -1002, [], [mf]))

    payload = client.sent[-1]
    inner = payload["file"]
    fname = next((a.file_name for a in (getattr(inner, "attributes", []) or [])
                  if isinstance(a, DocumentAttributeFilename)), None)
    ok = (sent is not None and sent is not SKIPPED
          and isinstance(inner, InputMediaUploadedDocument)
          and fname == "Clean.apk"
          and payload.get("caption") == "Download here")
    check("F3  matching media is swapped, caption kept", ok,
          f"filename={fname!r}, caption={payload.get('caption')!r}")

    # missing replacement file -> must NOT leak the original
    broken = {"original_id": "555", "replace_file": "/nonexistent/x.apk",
              "replace_name": "x.apk"}
    client2 = UploadingClient()
    res = run(bot.send_clean(client2, uid, msg, -1002, [], [broken]))
    check("F3b missing replacement never leaks the original",
          res is SKIPPED and not client2.sent, f"result={res!r}, sent={len(client2.sent)}")

    # non-matching media passes through untouched
    client3 = UploadingClient()
    other = fake_media_message("keepme.apk", "application/vnd.android.package-archive",
                               media_id=999)
    run(bot.send_clean(client3, uid, other, -1002, [], [mf]))
    passed = client3.sent and not client3.uploads
    check("F3c unrelated media is forwarded unchanged", bool(passed),
          f"uploads={client3.uploads}")


# ==========================================================================
# F4 — text filters rewrite the hidden URL of hyperlinks
# ==========================================================================
def f4_link_url_rewritten():
    bot = ForwarderBot()
    filters = [{"find": "t.me/oldchannel", "replace": "t.me/mychannel"}]
    ents = [MessageEntityTextUrl(offset=0, length=8, url="https://t.me/oldchannel/1"),
            MessageEntityBold(offset=10, length=4)]

    out, new_ents = bot.apply_text_filters("Join now bold text", ents, filters,
                                           filter_urls=True)
    url_ent = next((e for e in new_ents if isinstance(e, MessageEntityTextUrl)), None)
    bold = next((e for e in new_ents if isinstance(e, MessageEntityBold)), None)

    ok = (out == "Join now bold text"
          and url_ent is not None and url_ent.url == "https://t.me/mychannel/1"
          and url_ent.offset == 0 and url_ent.length == 8
          and bold is not None and bold.offset == 10 and bold.length == 4)
    check("F4  hyperlink URL rewritten, other entities untouched", ok,
          f"url={getattr(url_ent, 'url', None)!r}, bold={bold}")

    # and the toggle actually turns it off
    out2, ents2 = bot.apply_text_filters("Join now bold text", ents, filters,
                                         filter_urls=False)
    url2 = next((e for e in ents2 if isinstance(e, MessageEntityTextUrl)), None)
    check("F4b filter_urls=False leaves the URL alone",
          url2 is not None and url2.url == "https://t.me/oldchannel/1",
          f"url={getattr(url2, 'url', None)!r}")

    # visible text AND url filtered at the same time
    out3, ents3 = bot.apply_text_filters(
        "t.me/oldchannel is live", [MessageEntityTextUrl(offset=0, length=15,
                                                         url="https://t.me/oldchannel")],
        filters, filter_urls=True)
    url3 = next((e for e in ents3 if isinstance(e, MessageEntityTextUrl)), None)
    check("F4c visible text and hidden URL filtered together",
          out3 == "t.me/mychannel is live" and url3.url == "https://t.me/mychannel",
          f"{out3!r} / {getattr(url3, 'url', None)!r}")


# ==========================================================================
# F5 — albums apply media filters too
# ==========================================================================
def f5_album_filters():
    bot, uid = ForwarderBot(), 22
    cfg = bot.get_config(uid)
    cfg.update(logged_in=True, forwarding_active=True, source_chat="-1001",
               destinations=[{"id": "-1002", "name": "d"}], forward_with_tag=False)

    repl = forward.MEDIA_FILES_DIR / "repl_22_22_1.apk"
    repl.write_bytes(b"apk")
    mf = {"original_id": "555", "replace_file": str(repl),
          "replace_name": "Album_Replacement.apk", "replace_as_document": False}
    bot.get_filters(uid)["media_filters"] = [mf]

    client = UploadingClient()

    async def ok_client(_uid):
        return client

    bot.get_client = ok_client

    msgs = [
        fake_media_message("Ad.apk", "application/vnd.android.package-archive", media_id=555),
        fake_media_message("pic.jpg", "image/jpeg", media_id=556, is_photo=True),
    ]
    msgs[0].message = "album caption"
    msgs[0].grouped_id = 99
    msgs[1].grouped_id = 99
    for i, m in enumerate(msgs):
        m.id = 500 + i
        m.grouped_id = 99

    bot.album_buffers[uid][99].extend(msgs)
    run(bot.forward_album(uid, 99))

    payload = client.sent[-1] if client.sent else {}
    items = payload.get("file") or []
    kinds = [type(x).__name__ for x in items]
    fname = None
    for x in items:
        if isinstance(x, InputMediaUploadedDocument):
            fname = next((a.file_name for a in (x.attributes or [])
                          if isinstance(a, DocumentAttributeFilename)), None)

    ok = (len(items) == 2
          and "InputMediaUploadedDocument" in kinds      # the replacement
          and "InputMediaPhoto" in kinds                 # untouched photo
          and fname == "Album_Replacement.apk"
          and client.uploads == [str(repl)])
    check("F5  album: filtered item replaced, others untouched, still one album",
          ok, f"items={kinds}, filename={fname!r}, uploads={client.uploads}")


# ==========================================================================
# F6 — media metadata helpers
# ==========================================================================
def f6_media_metadata():
    bot = ForwarderBot()
    msg = fake_media_message("MyApp.apk", "application/vnd.android.package-archive", 5)
    name = bot.get_media_filename(msg)
    kind = bot.get_media_kind(msg.media)
    size = bot.get_media_size(msg)

    ok = name == "MyApp.apk" and "APK" in kind and size == 4242
    check("F6  filename / kind / size read from any document", ok,
          f"name={name!r}, kind={kind!r}, size={size}")

    sizes = [(0, "?"), (512, "512 B"), (2048, "2.0 KB"),
             (5 * 1024 * 1024, "5.0 MB"), (3 * 1024 ** 3, "3.0 GB")]
    bad = [(n, bot.human_size(n), w) for n, w in sizes if bot.human_size(n) != w]
    check("F6b human_size formatting", not bad, str(bad))


# ==========================================================================
# F7 — new UI screens render inside Telegram's limits
# ==========================================================================
def f7_ui_screens():
    cfg = json.loads((ROOT / "user_configs.json").read_text(encoding="utf-8"))
    uid = int(next(iter(cfg)))

    bot = ForwarderBot()

    async def no_client(_uid):
        return None

    bot.get_client = no_client

    # give the user a realistic pile of filters, incl. unicode-heavy names
    tf = bot.get_filters(uid).setdefault("text_filters", [])
    for i in range(12):
        tf.append({"find": f"old{i}", "replace": f"new{i}"})
    mf = bot.get_filters(uid).setdefault("media_filters", [])
    real = forward.MEDIA_FILES_DIR / "repl_ui_1.apk"
    real.write_bytes(b"apk")
    for i in range(7):
        mf.append({"original_id": str(9000 + i), "original_file": str(real),
                   "original_name": f"🟢𝐎𝐫𝐢𝐠𝐢𝐧𝐚𝐥_{i}.apk", "original_kind": "📦 APK",
                   "original_size": 4 * 1024 * 1024,
                   "replace_file": str(real) if i % 3 else "/gone/missing.apk",
                   "replace_name": f"𝐑𝐞𝐩𝐥𝐚𝐜𝐞𝐦𝐞𝐧𝐭_{i}.apk",
                   "replace_kind": "📦 APK", "replace_size": 4 * 1024 * 1024,
                   "replace_as_document": False})

    problems = []
    screens = [
        ("show_menu", lambda e: bot.show_menu(e)),
        ("show_help", lambda e: bot.show_help(e)),
        ("show_status", lambda e: bot.show_status(e)),
        ("show_settings", lambda e: bot.show_settings(e)),
        ("show_filters_menu", lambda e: bot.show_filters_menu(e)),
        ("view_text_filters p0", lambda e: bot.view_text_filters(e, 0)),
        ("view_text_filters p2", lambda e: bot.view_text_filters(e, 2)),
        ("view_media_filters p0", lambda e: bot.view_media_filters(e, 0)),
        ("view_media_filters p1", lambda e: bot.view_media_filters(e, 1)),
        ("show_destinations", lambda e: bot.show_destinations(e)),
        ("confirm_clear_filters", lambda e: bot.confirm_clear_filters(e)),
        ("confirm_logout", lambda e: bot.confirm_logout(e)),
        ("start_text_filter", lambda e: bot.start_text_filter(e)),
        ("start_media_filter", lambda e: bot.start_media_filter(e)),
        ("start_filter_test", lambda e: bot.start_filter_test(e)),
    ]
    for label, fn in screens:
        ev = Ev(uid)
        try:
            run(fn(ev))
        except Exception as e:
            problems.append(f"{label}: {type(e).__name__}: {e}")
            continue
        payloads = ev.all_payloads()
        if not payloads:
            problems.append(f"{label}: rendered nothing")
            continue
        for text, buttons in payloads:
            if text and len(text) > MAX_MESSAGE_LEN:
                problems.append(f"{label}: message {len(text)} > {MAX_MESSAGE_LEN}")
            for row in buttons or []:
                for b in (row if isinstance(row, (list, tuple)) else [row]):
                    t = getattr(b, "text", None)
                    if t and len(t.encode("utf-8")) > MAX_BUTTON_BYTES:
                        problems.append(f"{label}: button {len(t.encode())}B > 64B {t!r}")
        print(f"        {label:<26} {len(payloads[0][0] or '')} chars, "
              f"{sum(len(r) for r in (payloads[0][1] or []))} buttons")

    check("F7  all new screens render within Telegram limits", not problems,
          "; ".join(problems))


# ==========================================================================
# F8 — filter preview flow
# ==========================================================================
def f8_preview_flow():
    bot, uid = ForwarderBot(), 33
    bot.get_filters(uid)["text_filters"] = [
        {"find": "@oldchannel", "replace": "@mychannel"},
        {"find": "t.me/bad", "replace": ""},
    ]
    bot.get_config(uid)["current_step"] = "test_preview"

    ev = Ev(uid, text="Join @oldchannel and t.me/bad now")
    run(bot.handle_private_message(ev))

    body = ev.responds[-1][0] if ev.responds else ""
    ok = ("@mychannel" in body and "Before" in body and "After" in body
          and bot.get_config(uid)["current_step"] is None)
    check("F8  🧪 Test-a-filter previews before/after", ok,
          body.replace("\n", " ")[:150])


# ==========================================================================
# F9 — every button in the source has a dispatch route
# ==========================================================================
def f9_button_wiring():
    src = (ROOT / "forward.py").read_text(encoding="utf-8")
    literals = set(re.findall(r'Button\.inline\([^,]+,\s*b"([^"]+)"', src))
    prefixes = set(re.findall(r'Button\.inline\([^,]+,\s*f"([a-z_]+)_\{', src))

    start = src.index("async def handle_callback")
    end = src.index("def _int_arg")
    body = src[start:end]

    handled = set(re.findall(r'data == "([^"]+)"', body))
    handled |= set(re.findall(r'data in \(([^)]*)\)', body)
                   and [x.strip().strip('"') for grp in
                        re.findall(r'data in \(([^)]*)\)', body)
                        for x in grp.split(",")])
    starts = set(re.findall(r'data\.startswith\("([^"]+)"\)', body))
    starts |= set(re.findall(r'data\.startswith\("([^"]+)"\) or data\.startswith\("([^"]+)"\)',
                             body) and [y for pair in
                   re.findall(r'data\.startswith\("([^"]+)"\) or data\.startswith\("([^"]+)"\)',
                              body) for y in pair])

    dead = sorted(l for l in literals
                  if l not in handled and not any(l.startswith(s.rstrip("_")) for s in starts))
    dead_dyn = sorted(p for p in prefixes if not any(s.startswith(p) for s in starts))

    check("F9  every button routes to a handler", not dead and not dead_dyn,
          f"dead={dead}, dead_prefixes={dead_dyn}, routes={len(handled) + len(starts)}")


def f2d_video_audio_keep_metadata():
    """A replacement video must stay a playable video, not a generic file."""
    bot = ForwarderBot()
    client = UploadingClient()

    from telethon.tl.types import DocumentAttributeVideo, DocumentAttributeAudio

    vid = forward.MEDIA_FILES_DIR / "repl_v_1.mp4"
    vid.write_bytes(b"\x00\x00\x00\x18ftypmp42fakevideo")
    media = run(bot.replacement_input_media(
        client, {"replace_name": "trailer.mp4", "replace_as_document": False}, str(vid)))

    attrs = [type(a).__name__ for a in (media.attributes or [])]
    fname = next((a.file_name for a in (media.attributes or [])
                  if isinstance(a, DocumentAttributeFilename)), None)
    ok = (isinstance(media, InputMediaUploadedDocument)
          and "DocumentAttributeVideo" in attrs
          and fname == "trailer.mp4"
          and media.mime_type == "video/mp4")
    check("F2d video replacement stays playable + keeps its name", ok,
          f"mime={media.mime_type!r}, attrs={attrs}, name={fname!r}")

    aud = forward.MEDIA_FILES_DIR / "repl_a_1.mp3"
    # A structurally valid MPEG1 Layer III stream (417-byte frames at
    # 128 kbps / 44.1 kHz) so hachoir can actually parse it. Random bytes are
    # not enough for Telethon to emit DocumentAttributeAudio.
    frame = bytes([0xFF, 0xFB, 0x90, 0x04]) + b"\x00" * (417 - 4)
    aud.write_bytes(frame * 40)
    m2 = run(bot.replacement_input_media(
        client, {"replace_name": "song.mp3", "replace_as_document": False}, str(aud)))
    a2 = [type(x).__name__ for x in (m2.attributes or [])]
    n2 = next((x.file_name for x in (m2.attributes or [])
               if isinstance(x, DocumentAttributeFilename)), None)
    check("F2e audio replacement keeps audio attributes + name",
          "DocumentAttributeAudio" in a2 and n2 == "song.mp3"
          and m2.mime_type == "audio/mpeg",
          f"mime={m2.mime_type!r}, attrs={a2}, name={n2!r}")

    # An unparseable file must degrade gracefully: the filename survives, it is
    # still a document, and nothing raises.
    junk = forward.MEDIA_FILES_DIR / "repl_j_1.mp3"
    junk.write_bytes(b"ID3 not really an mp3 at all")
    m3 = run(bot.replacement_input_media(
        client, {"replace_name": "broken.mp3", "replace_as_document": False}, str(junk)))
    n3 = next((x.file_name for x in (m3.attributes or [])
               if isinstance(x, DocumentAttributeFilename)), None)
    check("F2f unparseable media degrades gracefully (name kept)",
          isinstance(m3, InputMediaUploadedDocument) and n3 == "broken.mp3",
          f"type={type(m3).__name__}, name={n3!r}, "
          f"attrs={[type(x).__name__ for x in (m3.attributes or [])]}")


ALL = [f1_any_file_type, f2_filename_preserved, f2b_photo_replacement,
       f2d_video_audio_keep_metadata,
       f3_send_clean_replacement, f4_link_url_rewritten, f5_album_filters,
       f6_media_metadata, f7_ui_screens, f8_preview_flow, f9_button_wiring]

if __name__ == "__main__":
    print("=" * 78)
    print("  forward.py  —  FEATURE TESTS (media filters / links / UI)")
    print("=" * 78)
    for fn in ALL:
        try:
            fn()
        except Exception as e:
            import traceback
            traceback.print_exc()
            check(fn.__name__, False, f"{type(e).__name__}: {e}")

    passed = [r for r in RESULTS if r[1]]
    failed = [r for r in RESULTS if not r[1]]
    print("=" * 78)
    print(f"  {len(passed)} / {len(RESULTS)} passed")
    if failed:
        print("  FAILED:")
        for name, _, detail in failed:
            print(f"    - {name}: {detail}")
    print("=" * 78)
    sys.exit(1 if failed else 0)
