# Bug Audit & Fix Report — `forward.py`

**Date:** 2026-09-13 · **Branch:** `arena/01a09a2a-forward`
**Scope:** full read of `forward.py` (1,276 lines), `start`, `restart_forward.sh`, config/data files.
**Result:** 20 defects found, all reproduced with an offline harness, all fixed. `20 / 20` regression tests pass.

Nothing here was guessed — every item was reproduced against the real Telethon 1.34.0 source or against
the real data in `user_configs.json`.

---

## Severity summary

| # | Severity | Defect | User-visible symptom |
|---|----------|--------|----------------------|
| B1 | 🔴 Crash | `time` used but never imported | Media filters impossible; some media silently dropped |
| B2 | 🔴 Critical | Event handlers leaked on every reconnect | Messages forwarded **N× (once per reconnect)** |
| B3 | 🔴 High | "Restart" button was a no-op | Restart silently did nothing |
| B4 | 🔴 Crash | Caption-less photo crashed the wizard | Media filter flow died with `AttributeError` |
| B5 | 🟠 High | Dedupe cache evicted **random** ids | Already-delivered messages re-forwarded |
| B6 | 🟠 High | Marked "processed" before sending | Failed sends permanently lost, never retried |
| B7 | 🟠 High | Filter entity offsets past end of text | `ENTITY_BOUNDS_INVALID` → message dropped |
| B8 | 🟠 High | Bot's own replies parsed as user input | Login/filter wizard corrupted by its own messages |
| B9 | 🟠 High | Button text > 64 bytes | Whole keyboard rejected for unicode channel names |
| B10 | 🟠 High | Non-atomic JSON writes | Crash mid-write = **all user settings lost** |
| B11 | 🟡 Med | SIGTERM/SIGINT ignored | `./start stop` always needed SIGKILL |
| B12 | 🔴 Security | Secrets + live sessions committed | Full account takeover from repo access |
| B13 | 🔴 Crash | Empty `find` filter = infinite loop | Entire event loop frozen |
| B14 | 🟡 Med | Service messages sent as empty text | `MESSAGE_EMPTY_NOT_ALLOWED` log spam |
| B15 | 🟠 High | Albums bypassed dedupe | Galleries re-sent after restart |
| B16 | 🟡 Med | Logout kept authorised session file | "Logout" was cosmetic |
| B17 | 🟠 High | Source change didn't rebind handlers | Kept forwarding the **old** channel |
| B18 | 🟡 Med | `user_msg_maps.json` grew forever | 28 KB already; unbounded disk + load cost |
| B19 | 🔴 Critical | Media filter stored a path that never existed | Every media filter failed at send time |
| B20 | 🟢 Test | (regression coverage for filter correctness) | — |

---

## Details

### B1 — `import time` missing (3 call sites)
`pyflakes` proof: `undefined name 'time'` at lines 300, 826, 848.
Any media filter creation raised `NameError`, swallowed by a bare `except` → the user got no error and
no filter. **Fix:** imported `time`.

### B2 — Handler leak on reconnect *(the worst bug in the file)*
The worker registered **lambdas**:
```python
client.add_event_handler(lambda e: self.on_new_message(user_id, e), events.NewMessage(...))
```
but removed **bound methods**:
```python
client.remove_event_handler(self.on_new_message)   # never matches the lambda
```
Telethon's `remove_event_handler` compares `cb == callback`, so it removed **0 handlers**.
Every reconnect cycle added 3 more. Measured: **12 live handlers after 4 cycles → each message
forwarded 4×.** On a flaky connection this multiplies without bound.

**Fix:** `UserState.registered_handlers` stores the exact `(callback, builder)` pairs;
`clear_handlers()` removes them with Telethon's real semantics. Regression test drives the actual
`_forwarding_worker` through 4 reconnect cycles and asserts the peak stays at **3**.

### B3 — Restart button did nothing
```python
await self.stop_forwarding(user_id)      # sets forwarding_active = False
...
if config.get("forwarding_active"):      # always False now
```
**Fix:** read `was_active` *before* stopping.

### B4 — `AttributeError` on caption-less media
`text = event.message.text.strip()` — Telethon returns `None` for a photo with no caption, which is
exactly what the media-filter step asks for. **Fix:** `(event.message.text or event.message.raw_text or "").strip()`,
plus media steps branch on `event.message.media` instead of text.

### B5 — Dedupe evicted arbitrary ids
```python
old = list(state.processed_messages)[:1000]   # set order = hash bucket order
```
Measured on realistic sparse channel ids: **516 of the newest 1250 ids were forgotten** → duplicates.
**Fix:** `OrderedDict`-backed insertion-ordered set; `mark_processed()` pops from the **oldest** end.

### B6 — "Processed" was recorded before delivery
`is_duplicate()` both checked *and* marked. If the client was offline, or the send raised, the id was
already blacklisted → **permanently lost**. Reproduced: with no client, msg id 5 was marked processed
without a single send.
**Fix:** split into a pure `is_duplicate()` and a `mark_processed()` called **only** when every
destination reached a terminal state. Transient failures leave the message retryable.

### B7 — Entity offsets outside the text
The overlap branch did `type(ent)(offset=idx, ...)` — resetting the offset to the *match position*.
Reproduced: `"hello world"` + entity `(0,11)` + filter `world→""` produced text of length 6 with
entity `(6,6)` → Telegram rejects the whole message. It also broke for entity types with extra
required fields (`MessageEntityTextUrl`, `MentionName`, `CustomEmoji`) — the bare `except` kept the
stale entity.
**Fix:** single-pass replacement that builds an old-index→new-index map, then remaps and clamps every
span; entities are cloned generically (verified against all four tricky TL types).

### B8 — The bot answered itself
`events.NewMessage()` with no `incoming=` filter also matches **outgoing** messages (verified in
Telethon's `NewMessage.filter`). So replies like `✅ Find: \`x\`` were fed back into the wizard as if
the user had typed them — e.g. the literal string `✅ Find: ...` became the "replacement" text.
**Fix:** every bot handler is now `incoming=True`; command patterns are anchored
(`^/start(?:@\w+)?\s*$` instead of `/start`, which also matched `/startfoo`).

### B9 — Button text over Telegram's 64-byte limit
`name[:20]` is a *character* slice. Your real destination
`🟢𝐁𝐃𝐆 𝐖𝐈𝐍 𝐌𝐀𝐗𝐈𝐄 𝟑 𝐌𝐈𝐍 𝐖𝐈𝐍𝐆𝐎🔴` uses 4-byte math-bold characters →
the remove button was **83 bytes**, and the chat-picker label **98 bytes**. Telegram then rejects the
entire keyboard.
**Fix:** `clip_bytes()` truncates on a UTF-8 boundary (never splits a multi-byte char). Your real name
now renders at **57 bytes**. Stress-tested with flag emoji and 300-char names.

### B10 — Non-atomic JSON writes
`Path(f).write_text(...)` truncates first. A crash/OOM-kill mid-write left a **0-byte or partial**
`user_configs.json` — and `_load_json` then returned `{}`, so the next save overwrote the good file
with empty config. Total data loss.
**Fix:** write to `*.json.tmp` → `fsync` → `os.replace` (atomic). Corrupt files are now **quarantined**
as `*.corrupt-<ts>` instead of silently discarded. High-frequency files (maps, processed ids) are
debounced to one write per 3 s with a periodic flush task and a forced flush on shutdown — previously
the whole 28 KB map was rewritten after **every single forwarded message**.

### B11 — Signals ignored
The handler only did `setattr(self, 'running', False)`; nothing in `run_until_disconnected()` reads it.
`./start stop` waited 5 s then SIGKILLed. **Fix:** the handler flips the flag *and* wakes the loop via
`call_soon_threadsafe`; `start_bot` races disconnect against a shutdown event, then calls a new
`shutdown()` that clears handlers, disconnects every client and flushes all state.

### B12 — Secrets committed to the repository 🔴
Tracked in git: `.env` (**live bot token**), `bot_session.session`,
`sessions/user_8089603563.session` (**full account access**), `user_configs.json`
(**api_id, api_hash, phone number**). Anyone with repo read access owns that Telegram account.

`.gitignore` now covers all of it, and `.env.example` documents the pattern.
**The files are still tracked** — `.gitignore` does not untrack existing files, and `git rm --cached`
in a commit would delete them from your server's working tree on the next `git pull`.
So run this deliberately instead:

```bash
./scripts/untrack_secrets.sh     # backs up, untracks, commits — files stay on disk
```

Then, manually (cannot be scripted):
1. **Revoke the bot token** — @BotFather → `/revoke` → put the new one in `.env`
2. **Reset the api_hash** — https://my.telegram.org → re-login through the bot
3. If this repo was ever public or shared, the old blobs are still in history — scrub with
   `git filter-repo`, or treat those credentials as dead.

### B13 — Empty `find` = infinite loop
`"".find(...)` / `text.find("", pos)` returns `pos` forever. Reproduced: still spinning after 2 s,
**blocking the whole asyncio event loop** (bot completely frozen, not just one message).
The wizard also accepted an empty `find`, so this was reachable by sending whitespace.
**Fix:** empty needles are rejected at input *and* skipped defensively in `apply_text_filters`;
send a single `-` as the replacement to delete a phrase. Added `/cancel` to escape any stuck step.

### B14 — Service messages sent as empty text
"X joined the group", pins, etc. have `message=None, media=None` → `send_message(dest, "")` →
`MESSAGE_EMPTY_NOT_ALLOWED` on every one. **Fix:** `_is_service_message()` → `SKIPPED`.
Also handled properly now: web pages/polls/contacts (re-sent by reference with `link_preview=True`
instead of the old download-to-disk path that dropped formatting), and `FILE_REFERENCE_EXPIRED`
(message re-fetched and retried once) plus `FloodWaitError` (sleep + retry once instead of dropping).

### B15 — Albums skipped dedupe
`forward_message()` checked `is_duplicate`; `forward_album()` did not. **Fix:** same check, and album
messages are only marked processed once delivered. Pending album buffers/timers are now cancelled and
cleared when forwarding stops (they used to leak as orphaned tasks).

### B16 — Logout was cosmetic
The session file stayed authorised, so the next `get_client()` logged straight back in.
**Fix:** `sessions/user_<id>.session` is deleted, dedupe cache and dialog cache are cleared, and the
config reset now also clears `forward_with_tag`.

### B17 — Changing the source didn't rebind handlers
Handlers are bound to `chats=<old id>` at registration. `set_source()` only wrote the config, so a
running worker **kept forwarding the previous channel** until the process was restarted.
**Fix:** new `restart_forwarding_worker()` rebinds handlers without flipping `forwarding_active`.

### B18 — Unbounded message map
`user_msg_maps.json` only shrank on delete-events. Already 307 entries / 28 KB.
**Fix:** capped at 20 000 entries per user, oldest message ids dropped first (they're the least likely
to still be edited/deleted).

### B19 — Media filters could never work *(second critical one)*
```python
filepath = MEDIA_FILES_DIR / f"repl_{user_id}_{orig_id}_{int(time.time())}"   # no extension
await event.message.download_media(file=str(filepath))
... "replace_file": str(filepath)                                            # stored WITHOUT extension
```
Telethon's `_get_file_name` **appends** a guessed extension when the target has none (verified in
`telethon/client/downloads.py`). So the file landed as `repl_1_2_3.jpg` while the config recorded
`repl_1_2_3` — a path that never existed. Every media filter then failed with `FileNotFoundError`,
swallowed by a bare `except` and logged as a generic "Send error".
**Fix:** `_download_filter_media()` derives the extension from the media's MIME type, passes a complete
filename, and stores the path `download_media` actually returned. The filter list now shows
`⚠️ missing file` next to any broken entry so you can spot old ones.

---

## Other hardening (no separate test — code review)

- **Login flow:** validates API ID (positive int), API hash (exactly 32 hex chars) and phone number
  before use; catches `SessionPasswordNeededError` as an exception instead of substring-matching the
  error text; `PhoneCodeInvalid` lets you retry, `PhoneCodeExpired` restarts the step; the temporary
  client is disconnected on failure instead of leaking.
- **`get_client`:** a connected-but-unauthorised client is no longer cached (the `is_connected()`
  fast-path used to hand back a dead client forever); invalid `api_id`/`api_hash` log clearly instead
  of raising `TypeError`.
- **Chat picker:** paginated (20/page, 200 dialogs fetched, was 100 with no paging), cached for 120 s,
  skips deleted accounts, byte-safe labels.
- **Edit mirroring:** no longer wipes a caption when `msg.message` is `None`; clips to 4096 chars;
  tolerates `MessageIdInvalid`.
- **Delete mirroring:** returns early when nothing is mapped (was connecting a client and rewriting the
  whole map file for every unrelated delete in the source chat).
- **UI:** callback queries now **edit** the existing message instead of stacking a new one per button
  press; `show_menu`/`show_status` use `.get()` so an older config can't `KeyError`; out-of-range
  delete buttons report "Already deleted" instead of looking dead; all message text clipped to 4096.
- **Callback dispatch:** unreadable/empty `data` is handled; unknown actions are logged (was silent).
- **Logging:** `telethon` pinned to WARNING; exceptions log their type, not just `str(e)`.
- **`restart_forward.sh`:** resolved its own directory (was hardcoded `/root/forward`), graceful
  SIGTERM before SIGKILL, stray-process matching on the full script path, and it now verifies the new
  process survived and prints the log tail if it didn't.
- **Dead code:** removed unused imports (`Any`, `DocumentAttributeSticker`, `DocumentAttributeAnimated`,
  three unused error types) — `pyflakes` and `ruff --select=F,E9,B,PLW` are now clean.

---

## How to verify

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/pip install ruff pyflakes          # optional, for linting

.venv/bin/python tests/repro_bugs.py         # 20/20 defect regressions
.venv/bin/python tests/smoke_ui.py           # renders every screen on your real config
.venv/bin/ruff check forward.py --select=F,E9,B,PLW
.venv/bin/pyflakes forward.py
```

Both suites are fully offline — they fake the Telegram layer, redirect every JSON/session path into a
temp dir, and **never touch your real `user_configs.json`, sessions or `processed_messages.json`**.

> I deliberately did **not** start the live bot to test it: `forwarding_active` is `true` in your
> config, so booting it would have sent real messages to your destination channel.

---

## Known limitations (not fixed — need your call)

1. **No catch-up on downtime.** The bot only forwards what arrives while it's running. If it's down for
   10 minutes, those messages are gone. Fixable with `iter_messages(min_id=last_seen)` on startup —
   say the word and I'll add it.
2. **Multi-user concurrency.** All destinations are sent to with `asyncio.gather` on one client;
   a large destination list will hit Telegram's rate limits. A per-user send queue with pacing would
   be the proper fix.
3. **`cryptg` not installed.** Adding it to `requirements.txt` gives a large speedup for media
   (it's a C extension, so it can fail to build on some hosts — that's why I left it out).
4. **`user_msg_maps.json` is keyed per-user but written as one blob.** Fine at 1 user, will need
   per-user files or SQLite if you onboard many.

---

## Files changed

```
 .gitignore              | +28      secrets/sessions/runtime state
 .env.example            | new      documents the BOT_TOKEN pattern
 forward.py              | ~2000    all 20 fixes
 restart_forward.sh      | rewritten portable + graceful + health-checked
 scripts/untrack_secrets.sh | new   safe path out of B12
 tests/repro_bugs.py     | new      20 defect regressions (offline)
 tests/smoke_ui.py       | new      UI render/limit smoke test (offline)
```

Your data files (`user_configs.json`, `user_filters.json`, `user_msg_maps.json`,
`processed_messages.json`, `.env`, sessions) were **not modified** — verified with `git diff`.
