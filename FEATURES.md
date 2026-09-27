# New Features — Media Filters, Link Rewriting & UI

Built on top of the 20-defect fix pass (see `BUGFIX_REPORT.md`).
All of it is covered by `tests/test_features.py` — **19/19 passing**, fully offline.

---

## 1 · Media filters now work for ANY file type

Previously media filters could not work at all (two separate bugs — see B1/B19 in the
bug report). Now the whole path is rebuilt and type-agnostic.

| Type | Supported | Delivered as |
|------|-----------|--------------|
| APK | ✅ | document, playable name + icon |
| ZIP / RAR / 7z | ✅ | document |
| PDF, DOCX, XLSX | ✅ | document |
| EXE / any binary | ✅ | document |
| Video (mp4, mkv…) | ✅ | **playable video** with duration/dimensions |
| Audio (mp3, m4a…) | ✅ | audio with duration/performer |
| Voice note | ✅ | voice bubble |
| Photo | ✅ | inline photo |
| Image sent "as file" | ✅ | file, **not** collapsed into a photo |
| Sticker / GIF | ✅ | document / GIF |
| Link preview, poll, contact, location | ❌ | cannot be swapped (no file to match) — you get a clear message instead of a silent failure |

**How the type is preserved.** `send_file()` derives both the MIME type and the displayed
filename from the *path on disk*, which is our internal `repl_<uid>_<id>_<ts>.apk` name.
So:

* the stored file now keeps the **real extension** taken from the uploaded filename
  (`.apk`, `.gz`, `.exe`, …), falling back to the MIME type — this matters because
  `send_file` never forwards a `mime_type` argument, it guesses it from the path;
* the media is uploaded and wrapped into an `InputMediaUploadedDocument` **ourselves**, with
  `utils.get_attributes()` supplying the video/audio metadata and our
  `DocumentAttributeFilename` overriding just the name.

Building the `InputMedia` by hand (rather than passing a path) is also what lets albums mix
untouched source media with replaced files in one grouped message — see §4.

`hachoir==3.3.0` was added to `requirements.txt` (pure Python, no compiler needed) so
Telethon can read duration/dimensions/artist from your replacement files. Without it a
replacement video would be sent as a generic file. Verified both ways in `F2d`/`F2e`/`F2f`.

---

## 2 · The replacement is sent under YOUR filename

> "jis name se mai send krunga file usi name se send hona chahiye"

Exactly that. Upload `MyApp_v2.1.apk` as the replacement and your channel receives
`MyApp_v2.1.apk` — not `repl_8089603563_4242_1757740000.bin`.

The name is captured at filter-creation time from `DocumentAttributeFilename` and stored as
`replace_name`. Test `F2` asserts the delivered attribute is literally
`file_name='MyApp_v2.1.apk'`.

Old filters created before this change (none exist in your `user_filters.json` today) fall
back to the stored path's basename, so nothing breaks.

**Safety rule:** if a filter matches but its replacement file is missing from disk, the
**original is never forwarded** — the message is dropped and logged loudly, and the filter
list shows `⚠️ FILE MISSING` plus `/status` warns you. Letting the original leak through
would defeat the entire point of the filter.

---

## 3 · Text filters now rewrite hyperlink URLs too

> "text me link attached rehta h, create link karke, to usme v replace kar saku"

Telegram's *create link* formatting produces a `MessageEntityTextUrl`: the visible text says
`Join Now` while the real destination hides in the entity's `url` field. Filtering only the
visible text left those links pointing at the source channel.

Now every text filter is applied to the hidden `url` as well:

```
filter:   t.me/oldchannel  →  t.me/mychannel

before:   "Join Now"  [url = https://t.me/oldchannel/1]
after:    "Join Now"  [url = https://t.me/mychannel/1]   ✅
```

Verified in `F4` / `F4b` / `F4c`, including that other entities (bold, italic, mentions)
keep their exact offsets and that visible text + hidden URL are filtered in the same pass.

Controllable from **⚙️ Settings → 🔗 Rewrite links** (`ON` by default, stored as
`filter_urls` in `user_configs.json`). Turning it off leaves hyperlink URLs untouched.

> ⚠️ Reminder: filters only apply while **Tag mode is OFF**. With tag mode ON Telegram sends
> the original message verbatim, so there is nothing to filter. `/status` now warns you if
> you have filters configured but tag mode on.

---

## 4 · Albums (galleries) are filtered too

Previously a gallery bypassed `send_clean` completely and built its own file list — so a
filtered file inside an album went straight through unreplaced.

Now each album item is checked against your media filters and the album is still sent as
**one grouped message**: test `F5` sends a 2-item album (one matching APK + one plain photo)
and asserts the result is `['InputMediaUploadedDocument', 'InputMediaPhoto']` — replacement
in place, photo untouched, grouping preserved, only one upload performed.

Album captions go through the text filters (including link URLs) as before.

---

## 5 · New UI

Rebuilt around "what do I need to see, and what can I click".

### Main menu
```
🤖 MESSAGE FORWARDER
━━━━━━━━━━━━━━━━━━━━
🔐 Account      ✅ +91••••••8776     ← phone number masked
📥 Source       Withdraw problem
📤 Destinations 1
🔧 Filters      3
⚡ Forwarding   🟢 ACTIVE

👉 Ready — press ▶️ to start forwarding.   ← only shows when action is needed
```
```
[ 📥 Source chat      ] [ 📤 Destinations (1) ]
[ 🔧 Filters (3)      ] [ ⚙️ Settings         ]
[        ▶️  START forwarding               ]
[ 📊 Status ] [ ❓ Help ]
```
The setup path is guided: if there is no source it tells you to pick one, if there are no
destinations it tells you to add one, otherwise it offers Start.

### ⚙️ Settings (new screen)
Tag mode and link rewriting live here with plain-English explanations of what each does,
plus Restart and Log out.

### 🔧 Filters
```
🔧 FILTERS
━━━━━━━━━━━━━━━━━━━━
📝 Text filters   3
🖼️ Media filters  1
🔗 Rewrite links  ✅ ON
```
```
[ 📝 Add text filter ]
[ 📋 Manage text (3) ]
[ 🖼️ Add media filter ]
[ 🗂️ Manage media (1) ]
[ 🔗 Rewrite links: ON ]
[ 🧪 Test a filter ]
[ 🧹 Delete all filters ]
[ 🔙 Back ]
```

**Filter lists are paginated** (5 per page) with `⬅️ 📄 1/3 ➡️` navigation — previously a
long list overflowed Telegram's 100-button / 4096-character limits and the whole screen
failed to render.

**Media filters now show what they actually do:**
```
🖼️ MEDIA FILTERS (2)
━━━━━━━━━━━━━━━━━━━━
1.
  🔎 📦 APK `Original_Ad.apk`
  ♻️ 📦 APK `MyApp_v2.1.apk` ✅
2.
  🔎 🖼️ Photo `(no filename — 🖼️ Photo)`
  ♻️ 📦 APK `Clean.apk` ⚠️ FILE MISSING
```
Kind + filename + health, instead of the old `ID: 424211873920...`.

### 🧪 Test a filter (new)
Paste any sample text and see exactly what your channel would receive:
```
🧪 FILTER PREVIEW
━━━━━━━━━━━━━━━━━━━━
Before
`Join @oldchannel and t.me/bad now`

After
`Join @mychannel and  now`

✅ Matched
• `@oldchannel` → `@mychannel`
• `t.me/bad` → `(removed)`

🔗 Link rewriting: ON — hidden hyperlink URLs are filtered too.
```

### Confirmations
Destructive actions now ask first: **remove destination**, **delete all filters** and
**log out** each show what will happen and need a second tap. Log out spells out that the
session file is deleted and a new code will be required.

### Everything is byte-safe
All labels pass through `clip_bytes()`, so unicode-heavy channel names like your
`🟢𝐁𝐃𝐆 𝐖𝐈𝐍 𝐌𝐀𝐗𝐈𝐄 𝟑 𝐌𝐈𝐍 𝐖𝐈𝐍𝐆𝐎🔴` render at 60 bytes instead of the 83 that Telegram
used to reject. `tests/smoke_ui.py` stress-tests flags, math-bold and 300-char names.

### Other UX fixes
* Callback presses **edit** the current message instead of stacking a new one per tap.
* `/cancel` aborts whatever wizard step you are stuck in (it used to be impossible to escape
  a half-finished login or filter).
* Login steps explain where to get the API ID/Hash, and validate them (API hash must be
  exactly 32 hex chars) instead of failing cryptically later.
* A wrong code says "wrong code, try again"; an expired code restarts the phone step.
* Sending a single `-` as a replacement means "delete this phrase".
* `/help` is a real guide now, including the tag-mode caveat.

---

## Verify it yourself

```bash
.venv/bin/pip install -r requirements.txt          # adds hachoir

.venv/bin/python tests/test_features.py            # 19/19  new features
.venv/bin/python tests/repro_bugs.py               # 20/20  bug regressions
.venv/bin/python tests/smoke_ui.py                 # UI limits on your real config
.venv/bin/ruff check forward.py --select=F,E9,B,PLW
```

All three suites are offline: they fake the Telegram layer, redirect every JSON/session/media
path into a temp dir, and never read or write your real `user_configs.json`,
`user_filters.json`, sessions or `processed_messages.json` (asserted with `git diff`).

> The live bot was **not** started during this work: `forwarding_active` is `true` in your
> config, so booting it would post real messages to your destination channel.

---

## Suggested next step (not built — your call)

Media filters match on the **exact media ID** of the file. That works whenever a channel
re-posts the *same* file (Telegram reuses the document ID), but a freshly uploaded APK gets a
new ID and won't match.

If you want, I can add **pattern rules** alongside ID matching, e.g.:

* any file whose name ends in `.apk`
* any file whose name contains `bet365`
* any file larger than 5 MB
* any video / any sticker

…each with its own replacement. Say the word and I'll wire it into the same UI.
