"""
Telegram Message Forwarder Bot - FULLY WORKING WITH FILTERS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✅ All filters working (Text & Media)
✅ No duplicate messages
✅ Instant forwarding
✅ Album support
✅ Edit/Delete mirroring
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import os
import sys
import json
import time
import asyncio
import signal
import logging
import mimetypes
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from collections import defaultdict, OrderedDict

from telethon import TelegramClient, events, Button
from telethon.tl.types import Message, InputDocument, InputPhoto
from telethon.errors import (
    FloodWaitError, ChatWriteForbiddenError,
    ChannelPrivateError, UserBannedInChannelError,
    FileReferenceExpiredError, MessageIdInvalidError,
    SessionPasswordNeededError, PhoneCodeInvalidError,
    PhoneCodeExpiredError, AuthKeyUnregisteredError,
    MessageEmptyError, MessageTooLongError
)
from dotenv import load_dotenv

load_dotenv()

# ========== CONFIGURATION ==========
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
AUTO_API_ID = 6
AUTO_API_HASH = "eb06d4abfb49dc3eeb1aeb98ae0f581e"

if not BOT_TOKEN:
    print("\n" + "="*60)
    print("❌ ERROR: BOT_TOKEN not set!")
    print("="*60)
    print("\nCreate a .env file with: BOT_TOKEN=your_bot_token_here")
    print("="*60)
    sys.exit(1)

# Paths
CONFIGS_FILE = "user_configs.json"
FILTERS_FILE = "user_filters.json"
MAPS_FILE = "user_msg_maps.json"
PROCESSED_FILE = "processed_messages.json"
MEDIA_FILES_DIR = Path("media_files")
SESSIONS_DIR = Path("sessions")

SESSIONS_DIR.mkdir(exist_ok=True)
MEDIA_FILES_DIR.mkdir(exist_ok=True)

ALBUM_WAIT = 0.35
RECONNECT_DELAY = 5

# Telegram hard limits (server-side). Exceeding them makes the whole request fail.
MAX_BUTTON_BYTES = 64        # inline keyboard button text
MAX_MESSAGE_LEN = 4096       # message / caption text
MAX_BUTTON_ROWS = 100        # buttons per message

# Dedupe cache: insertion-ordered so eviction always drops the OLDEST ids.
DEDUPE_MAX = 2000
DEDUPE_TRIM_TO = 1500

# Message-id -> forwarded-id mappings are only needed for edit/delete mirroring,
# so the map is capped to keep user_msg_maps.json from growing forever.
MAPPINGS_MAX = 20000

# Frequent files (maps / processed ids) are flushed at most this often instead of
# rewriting the whole JSON blob on every single forwarded message.
SAVE_DEBOUNCE = 3.0

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
log = logging.getLogger(__name__)
# Telethon is extremely chatty at INFO; keep our own logs readable.
logging.getLogger("telethon").setLevel(logging.WARNING)


class _Sentinel:
    """Unique marker object — compare with `is`, never `==`."""
    __slots__ = ("name",)

    def __init__(self, name: str):
        self.name = name

    def __repr__(self):
        return f"<{self.name}>"


# Nothing to deliver (service message, empty text, permanently rejected dest).
SKIPPED = _Sentinel("SKIPPED")
# Delivery failed for a reason that may succeed later -> keep the msg retryable.
TRANSIENT = _Sentinel("TRANSIENT")


def clip_bytes(text: str, limit: int = MAX_BUTTON_BYTES) -> str:
    """
    Truncate `text` so it fits Telegram's byte limit.

    Naive `text[:20]` slicing is NOT safe: channel names full of unicode
    (math-bold, emoji) cost 4 bytes per char, so 20 chars can be 80+ bytes and
    Telegram then rejects the *entire* inline keyboard.
    """
    if not text:
        return ""
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    suffix = "…"
    budget = limit - len(suffix.encode("utf-8"))
    cut = raw[:budget]
    # decode with errors="ignore" drops a trailing partial multi-byte sequence
    return cut.decode("utf-8", errors="ignore") + suffix


def clip_message(text: Optional[str]) -> str:
    """Same idea for message bodies (4096 chars) — never returns empty."""
    if not text:
        return ""
    return text[:MAX_MESSAGE_LEN - 1] if len(text) > MAX_MESSAGE_LEN else text


class UserState:
    def __init__(self, user_id: int):
        self.user_id = user_id
        self.client: Optional[TelegramClient] = None
        self.client_authorized = False
        self.client_lock = asyncio.Lock()
        self.forwarding_task: Optional[asyncio.Task] = None
        # Registered (handler, event_builder) pairs for the live user client.
        # Kept so they can actually be removed again — see B2.
        self.registered_handlers: List[tuple] = []
        self.handlers_active = False
        # OrderedDict acts as an *insertion-ordered* set. A plain set evicts in
        # arbitrary bucket order, which threw away RECENT ids and re-forwarded
        # messages that had already been delivered.
        self.processed_messages: "OrderedDict[int, None]" = OrderedDict()
        self.reconnect_attempts = 0

    def is_processed(self, msg_id: int) -> bool:
        return msg_id in self.processed_messages

    def mark_processed(self, msg_id: int) -> None:
        self.processed_messages[msg_id] = None
        self.processed_messages.move_to_end(msg_id)
        if len(self.processed_messages) > DEDUPE_MAX:
            # popitem(last=False) removes the OLDEST entry, deterministically.
            while len(self.processed_messages) > DEDUPE_TRIM_TO:
                self.processed_messages.popitem(last=False)

    def register_handler(self, cb, builder) -> None:
        self.registered_handlers.append((cb, builder))

    def clear_handlers(self, client) -> None:
        for cb, builder in self.registered_handlers:
            try:
                client.remove_event_handler(cb, builder)
            except Exception:
                pass
        self.registered_handlers.clear()
        self.handlers_active = False


class ForwarderBot:
    def __init__(self):
        self.bot: Optional[TelegramClient] = None
        self.users: Dict[int, UserState] = {}
        
        # Load data
        self.configs = self._load_json(CONFIGS_FILE)
        self.filters = self._load_json(FILTERS_FILE)
        self.mappings = self._load_json(MAPS_FILE)
        self.processed_cache = self._load_json(PROCESSED_FILE)
        
        # Album handling
        self.album_buffers: Dict[int, Dict[int, List[Message]]] = defaultdict(lambda: defaultdict(list))
        self.album_timers: Dict[int, Dict[int, asyncio.Task]] = defaultdict(dict)
        
        # Temp storage for filter creation
        self.temp_filters: Dict[int, dict] = {}

        # user_id -> (fetched_at, dialogs) so paging the chat list is cheap
        self.dialog_cache: Dict[int, tuple] = {}

        # Debounced / atomic persistence
        self._json_registry: Dict[str, dict] = {
            CONFIGS_FILE: self.configs,
            FILTERS_FILE: self.filters,
            MAPS_FILE: self.mappings,
            PROCESSED_FILE: self.processed_cache,
        }
        self._save_dirty: Dict[str, bool] = {}
        self._last_save: Dict[str, float] = {}
        self._flush_task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._shutdown_event: Optional[asyncio.Event] = None

        self.running = True
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    # ---------- graceful shutdown (B11) ----------

    def _handle_signal(self, signum, frame):
        """Flag the shutdown and wake the event loop from inside it.

        The old handler only set `self.running = False`, which nothing in
        `run_until_disconnected()` ever looked at — so SIGTERM was effectively
        ignored and `./start stop` always had to escalate to SIGKILL.
        """
        log.warning("Signal %s received - shutting down gracefully", signum)
        self.running = False
        loop = self._loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(self._request_shutdown)
            except RuntimeError:
                pass

    def _request_shutdown(self):
        if self._shutdown_event is not None:
            self._shutdown_event.set()

    async def shutdown(self):
        """Flush state and disconnect every client exactly once."""
        self.running = False
        self.flush_all(force=True)

        for state in list(self.users.values()):
            client = state.client
            if client is None:
                continue
            state.clear_handlers(client)
            try:
                await client.disconnect()
            except Exception as e:
                log.warning("Disconnect failed: %s", e)
            state.client = None

        if self.bot is not None:
            try:
                await self.bot.disconnect()
            except Exception as e:
                log.warning("Bot disconnect failed: %s", e)
        log.info("Shutdown complete.")

    # ---------- persistence (B10) ----------

    def _load_json(self, filename: str) -> dict:
        path = Path(filename)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
                if isinstance(data, dict):
                    return data
                log.error("%s did not contain a JSON object - ignoring it", filename)
            except Exception as e:
                # Quarantine the broken file instead of returning {} — the next
                # save would otherwise silently overwrite the real data with an
                # empty config.
                backup = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
                try:
                    path.rename(backup)
                    log.critical("%s is corrupt (%s); kept a copy at %s", filename, e, backup)
                except Exception:
                    log.critical("%s is corrupt (%s) and could not be backed up", filename, e)
        return {}

    def _save_json(self, filename: str, data: dict, debounce: bool = False):
        """Atomic write: serialise to a temp file, then `os.replace` it into place.

        A crash (or OOM kill) halfway through the old direct `write_text()`
        truncated user_configs.json and destroyed every user's settings.
        """
        if debounce:
            now = time.monotonic()
            if now - self._last_save.get(filename, 0.0) < SAVE_DEBOUNCE:
                self._save_dirty[filename] = True
                return
        self._save_dirty[filename] = False
        self._last_save[filename] = time.monotonic()

        path = Path(filename)
        tmp = path.with_name(path.name + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)          # atomic on POSIX
        except Exception as e:
            log.error("Failed to save %s: %s", filename, e)
            try:
                tmp.unlink()
            except Exception:
                pass

    def flush_all(self, force: bool = False):
        """Write every file that has pending (debounced) changes."""
        for filename, data in self._json_registry.items():
            if force or self._save_dirty.get(filename):
                self._save_json(filename, data)
        if force:
            self._save_dirty.clear()

    async def _flush_loop(self):
        while self.running:
            try:
                await asyncio.sleep(SAVE_DEBOUNCE)
                self.flush_all()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("Flush loop error: %s", e)

    def get_user_state(self, user_id: int) -> UserState:
        if user_id not in self.users:
            state = UserState(user_id)
            cached = self.processed_cache.get(str(user_id)) or []
            # Rebuild as an insertion-ordered set, oldest first.
            for mid in cached:
                try:
                    state.processed_messages[int(mid)] = None
                except (TypeError, ValueError):
                    continue
            self.users[user_id] = state
        return self.users[user_id]
    
    def get_config(self, user_id: int) -> dict:
        key = str(user_id)
        if key not in self.configs:
            self.configs[key] = {
                "logged_in": False, "phone": None, "api_id": None, "api_hash": None,
                "source_chat": None, "source_chat_name": "Not Set", "destinations": [],
                "forwarding_active": False, "forward_with_tag": False, "current_step": None
            }
        return self.configs[key]
    
    def save_config(self, user_id: int):
        self._save_json(CONFIGS_FILE, self.configs)
    
    def get_filters(self, user_id: int) -> dict:
        key = str(user_id)
        if key not in self.filters:
            self.filters[key] = {"text_filters": [], "media_filters": []}
        return self.filters[key]
    
    def save_filters(self, user_id: int):
        self._save_json(FILTERS_FILE, self.filters)
    
    def get_mappings(self, user_id: int) -> dict:
        key = str(user_id)
        if key not in self.mappings:
            self.mappings[key] = {}
        return self.mappings[key]

    def trim_mappings(self, user_id: int):
        """Cap the id map so user_msg_maps.json cannot grow without bound.

        Entries are only ever removed when Telegram reports a delete-event, so
        a busy channel grew this file forever. Only the oldest message ids are
        dropped - those are the ones least likely to still be edited/deleted.
        """
        maps = self.get_mappings(user_id)
        if len(maps) <= MAPPINGS_MAX:
            return
        try:
            ordered = sorted(maps.keys(), key=int)
        except (TypeError, ValueError):
            ordered = list(maps.keys())
        for stale in ordered[:len(maps) - MAPPINGS_MAX]:
            maps.pop(stale, None)
        log.info("Trimmed message map for user %s to %d entries", user_id, len(maps))

    def save_mappings(self, user_id: int):
        self.trim_mappings(user_id)
        self._save_json(MAPS_FILE, self.mappings, debounce=True)
    
    async def get_client(self, user_id: int) -> Optional[TelegramClient]:
        state = self.get_user_state(user_id)
        
        async with state.client_lock:
            if state.client and state.client_authorized and state.client.is_connected():
                return state.client
            
            config = self.get_config(user_id)
            if not config.get("logged_in"):
                return None
            
            api_id = config.get("api_id") or AUTO_API_ID
            api_hash = config.get("api_hash") or AUTO_API_HASH
            
            if state.client:
                state.clear_handlers(state.client)
                try:
                    await state.client.disconnect()
                except Exception:
                    pass
                state.client = None
                state.client_authorized = False

            try:
                api_id = int(api_id)
            except (TypeError, ValueError):
                log.error("User %s has an invalid api_id: %r", user_id, api_id)
                return None
            if not api_hash:
                log.error("User %s has no api_hash configured", user_id)
                return None

            session_file = str(SESSIONS_DIR / f"user_{user_id}")
            state.client = TelegramClient(
                session_file, api_id, api_hash,
                connection_retries=3, retry_delay=1, request_retries=2, flood_sleep_threshold=60
            )
            state.client_authorized = False

            try:
                await state.client.connect()
                if await state.client.is_user_authorized():
                    state.reconnect_attempts = 0
                    state.client_authorized = True
                    return state.client
                # Connected but not authorised: do NOT keep it, otherwise the
                # `is_connected()` fast-path above hands back a dead client
                # forever and every send silently fails.
                state.client_authorized = False
                try:
                    await state.client.disconnect()
                except Exception:
                    pass
                state.client = None
                return None
            except Exception as e:
                log.error(f"Failed to connect: {e}")
                state.reconnect_attempts += 1
                return None

    # ---------- duplicate detection (B5 / B6) ----------

    async def is_duplicate(self, user_id: int, msg_id: int) -> bool:
        """Pure check — no side effects.

        Splitting "have I seen this?" from "mark it as seen" is what stops
        undelivered messages from being permanently blacklisted.
        """
        return self.get_user_state(user_id).is_processed(msg_id)

    def mark_processed(self, user_id: int, msg_ids) -> None:
        """Record ids as delivered and persist (debounced)."""
        state = self.get_user_state(user_id)
        ids = msg_ids if isinstance(msg_ids, (list, tuple, set)) else [msg_ids]
        for mid in ids:
            state.mark_processed(mid)
        self.processed_cache[str(user_id)] = list(state.processed_messages)[-DEDUPE_MAX:]
        self._save_json(PROCESSED_FILE, self.processed_cache, debounce=True)

    # ---------- text filters (B7 / B13) ----------

    @staticmethod
    def _remap_entities(entities: list, mapping: Dict[int, int], new_len: int) -> list:
        """Rebuild formatting entities for the filtered text.

        Uses an old-index -> new-index map so every span stays inside the new
        string. The previous arithmetic (`offset=idx`) pushed offsets past the
        end of the text and Telegram rejected the message outright.
        """
        out = []
        for ent in entities or []:
            offset = getattr(ent, "offset", None)
            length = getattr(ent, "length", None)
            if offset is None or length is None:
                continue
            start = mapping.get(offset, min(offset, new_len))
            end = mapping.get(offset + length, min(offset + length, new_len))
            start = max(0, min(start, new_len))
            end = max(0, min(end, new_len))
            if end <= start:
                continue          # entity lived entirely inside replaced text
            new_ent = ForwarderBot._clone_entity(ent, start, end - start)
            if new_ent is not None:
                out.append(new_ent)
        return out

    @staticmethod
    def _clone_entity(ent, offset: int, length: int):
        try:
            kwargs = {k: v for k, v in vars(ent).items() if not k.startswith("_")}
            kwargs["offset"] = offset
            kwargs["length"] = length
            return type(ent)(**kwargs)
        except Exception:
            try:                    # fall back to in-place mutation
                ent.offset = offset
                ent.length = length
                return ent
            except Exception:
                return None

    def apply_text_filters(self, text: Optional[str], entities: Optional[list],
                           filters: Optional[list]) -> Tuple[str, list]:
        if not text:
            return (text or ""), list(entities or [])
        if not filters:
            return text, list(entities or [])

        result = text
        current_entities = list(entities or [])

        for f in filters:
            try:
                find = f["find"]
                replace = f.get("replace", "")
            except (KeyError, TypeError):
                log.warning("Skipping malformed text filter: %r", f)
                continue

            # An empty needle makes str.find() return the current position
            # forever -> infinite loop that freezes the whole event loop.
            if not find:
                log.warning("Ignoring text filter with an empty 'find' string")
                continue
            if find not in result:
                continue

            result, mapping = self._replace_all(result, find, replace)
            current_entities = self._remap_entities(current_entities, mapping, len(result))

        return result, current_entities

    @staticmethod
    def _replace_all(text: str, find: str, replace: str) -> Tuple[str, Dict[int, int]]:
        """Replace every occurrence in one pass and return an index map."""
        out: List[str] = []
        mapping: Dict[int, int] = {}
        i = 0
        n = len(text)
        flen = len(find)

        while i <= n:
            idx = text.find(find, i)
            if idx == -1:
                for j in range(i, n + 1):
                    mapping[j] = sum(len(p) for p in out) + (j - i)
                out.append(text[i:])
                break

            for j in range(i, idx + 1):
                mapping[j] = sum(len(p) for p in out) + (j - i)
            out.append(text[i:idx])

            base = sum(len(p) for p in out)
            out.append(replace)
            # every index inside the matched span collapses onto the replacement
            for j in range(idx, idx + flen + 1):
                mapping[j] = base
            mapping[idx + flen] = base + len(replace)

            i = idx + flen

        return "".join(out), mapping
    
    @staticmethod
    def get_media_id(media) -> Optional[str]:
        try:
            if hasattr(media, 'photo') and media.photo:
                return str(media.photo.id)
            if hasattr(media, 'document') and media.document:
                return str(media.document.id)
        except:
            pass
        return None
    
    @staticmethod
    def _is_service_message(msg: Message) -> bool:
        """System messages ('X joined the group', pinned-item notices, ...)."""
        return bool(getattr(msg, "action", None)) or (
            not getattr(msg, "media", None) and not getattr(msg, "message", None)
        )

    async def _refresh_message(self, client: TelegramClient, msg: Message) -> Optional[Message]:
        """Re-fetch a message to obtain a fresh file_reference."""
        try:
            fresh = await client.get_messages(msg.chat_id, ids=msg.id)
            return fresh or None
        except Exception as e:
            log.warning("Could not refresh message %s: %s", getattr(msg, "id", "?"), e)
            return None

    async def send_clean(self, client: TelegramClient, user_id: int, msg: Message,
                         dest_id: int, text_filters: list, media_filters: list,
                         _attempt: int = 1):
        """Deliver one message to one destination.

        Returns the sent Message, SKIPPED when there is nothing to deliver, or
        TRANSIENT for a failure worth retrying. Exceptions are handled here so
        the caller can decide whether the message may be re-delivered later.
        """
        if self._is_service_message(msg):
            return SKIPPED          # was sent as "" -> MESSAGE_EMPTY_NOT_ALLOWED

        caption = msg.message
        entities = list(msg.entities or [])

        if caption:
            caption, entities = self.apply_text_filters(caption, entities, text_filters)
            caption = clip_message(caption)

        try:
            if msg.media:
                media_id = self.get_media_id(msg.media)
                if media_id and media_filters:
                    for mf in media_filters:
                        if mf.get("original_id") == media_id:
                            repl = mf.get("replace_file")
                            if repl and Path(repl).exists():
                                return await client.send_file(
                                    dest_id, file=repl,
                                    caption=caption or None,
                                    formatting_entities=entities or None
                                )
                            log.error("Media filter points at a missing file: %s", repl)

                if hasattr(msg.media, 'document') and msg.media.document:
                    doc = msg.media.document
                    return await client.send_file(
                        dest_id,
                        file=InputDocument(id=doc.id, access_hash=doc.access_hash,
                                           file_reference=doc.file_reference),
                        caption=caption or None,
                        formatting_entities=entities or None,
                        attributes=doc.attributes
                    )
                elif hasattr(msg.media, 'photo') and msg.media.photo:
                    photo = msg.media.photo
                    return await client.send_file(
                        dest_id,
                        file=InputPhoto(id=photo.id, access_hash=photo.access_hash,
                                        file_reference=photo.file_reference),
                        caption=caption or None,
                        formatting_entities=entities or None
                    )
                else:
                    # Web pages, polls, contacts, venues, dice, ... — let
                    # Telethon re-send the media by reference instead of the
                    # old download-to-disk dance (which also dropped entities).
                    if caption:
                        return await client.send_message(
                            dest_id, caption, formatting_entities=entities or None,
                            link_preview=True
                        )
                    return await client.send_file(
                        dest_id, msg, caption=caption or None,
                        formatting_entities=entities or None
                    )
            else:
                if not caption:
                    return SKIPPED
                return await client.send_message(
                    dest_id, caption, formatting_entities=entities or None
                )

        except FileReferenceExpiredError:
            if _attempt < 2:
                fresh = await self._refresh_message(client, msg)
                if fresh is not None:
                    return await self.send_clean(
                        client, user_id, fresh, dest_id, text_filters, media_filters,
                        _attempt=_attempt + 1
                    )
            log.error("File reference expired for msg %s and could not be refreshed", msg.id)
            return TRANSIENT

        except FloodWaitError as e:
            wait = min(int(getattr(e, "seconds", 0) or 0), 300)
            log.warning("Flood wait %ss on dest %s", wait, dest_id)
            if _attempt < 2:
                await asyncio.sleep(wait + 1)
                return await self.send_clean(
                    client, user_id, msg, dest_id, text_filters, media_filters,
                    _attempt=_attempt + 1
                )
            return TRANSIENT

        except (ChatWriteForbiddenError, ChannelPrivateError,
                UserBannedInChannelError, MessageEmptyError, MessageTooLongError) as e:
            # Retrying these will never succeed — report loudly, don't loop.
            log.error("Permanently rejected by dest %s: %s: %s",
                      dest_id, type(e).__name__, e)
            return SKIPPED

        except AuthKeyUnregisteredError as e:
            log.error("Session for user %s is no longer authorised: %s", user_id, e)
            return TRANSIENT

        except Exception as e:
            log.error("Send error to %s: %s: %s", dest_id, type(e).__name__, e)
            return TRANSIENT

    async def forward_message(self, user_id: int, msg: Message):
        config = self.get_config(user_id)
        if not config.get("forwarding_active"):
            return

        source_id = config.get("source_chat")
        if not source_id or str(msg.chat_id) != str(source_id):
            return

        destinations = config.get("destinations", [])
        if not destinations:
            return

        if await self.is_duplicate(user_id, msg.id):
            return

        # Resolve the client BEFORE touching the dedupe cache: if we are
        # offline the message must stay retryable instead of being blacklisted.
        client = await self.get_client(user_id)
        if not client:
            log.warning("No client for user %s - message %s left unprocessed", user_id, msg.id)
            return

        use_tag = config.get("forward_with_tag", False)
        text_filters = self.get_filters(user_id).get("text_filters", [])
        media_filters = self.get_filters(user_id).get("media_filters", [])
        mappings = self.get_mappings(user_id)

        async def send_to_dest(dest: dict):
            try:
                dest_id = int(dest["id"])
            except (KeyError, TypeError, ValueError):
                log.error("Bad destination entry: %r", dest)
                return SKIPPED

            name = dest.get("name", dest_id)
            try:
                if use_tag:
                    sent = await client.forward_messages(dest_id, msg.id, int(source_id))
                    if isinstance(sent, list):
                        sent = sent[0] if sent else SKIPPED
                else:
                    sent = await self.send_clean(
                        client, user_id, msg, dest_id, text_filters, media_filters
                    )

                if sent is SKIPPED:
                    return SKIPPED
                if sent is TRANSIENT or sent is None:
                    return TRANSIENT

                msg_key = str(msg.id)
                mappings.setdefault(msg_key, []).append({"dest": dest_id, "msg_id": sent.id})
                log.info("Forwarded %s to %s", msg.id, name)
                return sent
            except Exception as e:
                log.error("Error forwarding %s to %s: %s", msg.id, name, e)
                return TRANSIENT

        results = await asyncio.gather(*[send_to_dest(d) for d in destinations])

        delivered = [r for r in results if r is not SKIPPED and r is not TRANSIENT]
        transient = [r for r in results if r is TRANSIENT]

        if delivered:
            self.save_mappings(user_id)

        if not transient:
            # Every destination reached a terminal state -> safe to remember.
            self.mark_processed(user_id, msg.id)
        else:
            log.warning(
                "Message %s failed for %d/%d destinations - left retryable",
                msg.id, len(transient), len(destinations)
            )
    
    async def handle_album(self, user_id: int, msg: Message):
        gid = msg.grouped_id

        config = self.get_config(user_id)
        if not config.get("forwarding_active"):
            return
        source_id = config.get("source_chat")
        if not source_id or str(msg.chat_id) != str(source_id):
            return
        if await self.is_duplicate(user_id, msg.id):
            return

        self.album_buffers[user_id][gid].append(msg)

        old = self.album_timers[user_id].get(gid)
        if old is not None and not old.done():
            old.cancel()

        async def flush():
            try:
                await asyncio.sleep(ALBUM_WAIT)
                await self.forward_album(user_id, gid)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error("Album flush error: %s", e)
            finally:
                # Never leave a finished task behind in the timer map.
                if self.album_timers[user_id].get(gid) is asyncio.current_task():
                    self.album_timers[user_id].pop(gid, None)

        self.album_timers[user_id][gid] = asyncio.create_task(flush())

    def clear_album_state(self, user_id: int):
        """Drop pending album buffers/timers (used when forwarding stops)."""
        for task in list(self.album_timers.get(user_id, {}).values()):
            if task is not None and not task.done():
                task.cancel()
        self.album_timers.pop(user_id, None)
        self.album_buffers.pop(user_id, None)

    async def forward_album(self, user_id: int, gid: int):
        messages = self.album_buffers[user_id].pop(gid, [])
        self.album_timers[user_id].pop(gid, None)

        if not messages:
            return

        config = self.get_config(user_id)
        if not config.get("forwarding_active"):
            return

        source_id = config.get("source_chat")
        if not source_id:
            return
        if any(str(m.chat_id) != str(source_id) for m in messages):
            return

        destinations = config.get("destinations", [])
        if not destinations:
            return

        # Same rule as single messages: nothing is remembered as "processed"
        # until it has actually been delivered somewhere.
        # NB: a generator expression containing `await` is an *async* generator,
        # which `all()` cannot consume — materialise the list first.
        already_seen = [await self.is_duplicate(user_id, m.id) for m in messages]
        if all(already_seen):
            return
        messages = [m for m, seen in zip(messages, already_seen, strict=True) if not seen]
        if not messages:
            return

        use_tag = config.get("forward_with_tag", False)
        text_filters = self.get_filters(user_id).get("text_filters", [])

        client = await self.get_client(user_id)
        if not client:
            # Put the messages back so a later flush can still deliver them.
            self.album_buffers[user_id][gid].extend(messages)
            return

        mappings = self.get_mappings(user_id)

        async def send_album_to_dest(dest: dict):
            try:
                dest_id = int(dest["id"])
            except (KeyError, TypeError, ValueError):
                log.error("Bad destination entry: %r", dest)
                return SKIPPED

            name = dest.get("name", dest_id)
            try:
                if use_tag:
                    sent = await client.forward_messages(
                        dest_id, [m.id for m in messages], int(source_id))
                    if not isinstance(sent, list):
                        sent = [sent]
                else:
                    files = []
                    caption = None
                    entities = None

                    for m in messages:
                        if caption is None and m.message:
                            caption, entities = self.apply_text_filters(
                                m.message, m.entities, text_filters)
                            caption = clip_message(caption)

                        if m.media:
                            if hasattr(m.media, 'document') and m.media.document:
                                doc = m.media.document
                                files.append(InputDocument(
                                    id=doc.id, access_hash=doc.access_hash,
                                    file_reference=doc.file_reference))
                            elif hasattr(m.media, 'photo') and m.media.photo:
                                photo = m.media.photo
                                files.append(InputPhoto(
                                    id=photo.id, access_hash=photo.access_hash,
                                    file_reference=photo.file_reference))

                    if not files:
                        return SKIPPED
                    sent = await client.send_file(
                        dest_id, files, caption=caption or None,
                        formatting_entities=entities or None)
                    if not isinstance(sent, list):
                        sent = [sent]

                # strict=False on purpose: Telegram may return fewer messages
                # than we sent (e.g. a rejected item), and we still want to
                # map the ones that did land.
                for sm, om in zip(sent, messages, strict=False):
                    mappings.setdefault(str(om.id), []).append(
                        {"dest": dest_id, "msg_id": sm.id})

                log.info("Forwarded album (%d msgs) to %s", len(messages), name)
                return sent
            except FloodWaitError as e:
                wait = min(int(getattr(e, "seconds", 0) or 0), 300)
                log.warning("Album flood wait %ss on dest %s", wait, dest_id)
                await asyncio.sleep(wait + 1)
                return TRANSIENT
            except (ChatWriteForbiddenError, ChannelPrivateError,
                    UserBannedInChannelError) as e:
                log.error("Album permanently rejected by %s: %s", name, e)
                return SKIPPED
            except Exception as e:
                log.error("Album error to %s: %s: %s", name, type(e).__name__, e)
                return TRANSIENT

        results = await asyncio.gather(*[send_album_to_dest(d) for d in destinations])
        delivered = [r for r in results if r is not SKIPPED and r is not TRANSIENT]
        transient = [r for r in results if r is TRANSIENT]

        if delivered:
            self.save_mappings(user_id)
        if not transient:
            self.mark_processed(user_id, [m.id for m in messages])
        else:
            log.warning("Album %s failed for %d/%d destinations - left retryable",
                        gid, len(transient), len(destinations))

    async def start_forwarding(self, user_id: int):
        state = self.get_user_state(user_id)
        if state.forwarding_task and not state.forwarding_task.done():
            return
        state.forwarding_task = asyncio.create_task(self._forwarding_worker(user_id))

    async def stop_forwarding(self, user_id: int):
        config = self.get_config(user_id)
        config["forwarding_active"] = False
        self.save_config(user_id)

        self.clear_album_state(user_id)

        state = self.get_user_state(user_id)
        if state.forwarding_task:
            state.forwarding_task.cancel()
            try:
                await state.forwarding_task
            except (asyncio.CancelledError, Exception):
                pass
            state.forwarding_task = None

        if state.client:
            state.clear_handlers(state.client)

    async def restart_forwarding_worker(self, user_id: int):
        """Rebind handlers without flipping `forwarding_active`.

        Used when the source chat changes mid-run: the old handlers stay bound
        to the previous chat otherwise and keep forwarding the wrong channel.
        """
        state = self.get_user_state(user_id)
        if state.client:
            state.clear_handlers(state.client)
        self.clear_album_state(user_id)
        if state.forwarding_task and not state.forwarding_task.done():
            state.forwarding_task.cancel()
            try:
                await state.forwarding_task
            except (asyncio.CancelledError, Exception):
                pass
            state.forwarding_task = None
        if self.get_config(user_id).get("forwarding_active"):
            await self.start_forwarding(user_id)

    async def _forwarding_worker(self, user_id: int):
        config = self.get_config(user_id)
        state = self.get_user_state(user_id)

        try:
            while self.running and config.get("forwarding_active"):
                try:
                    client = await self.get_client(user_id)
                    if not client:
                        await asyncio.sleep(RECONNECT_DELAY)
                        continue

                    source_id = config.get("source_chat")
                    if not source_id:
                        await asyncio.sleep(5)
                        continue

                    try:
                        source_peer = int(source_id)
                    except (TypeError, ValueError):
                        log.error("Invalid source_chat %r for user %s", source_id, user_id)
                        await asyncio.sleep(5)
                        continue

                    # Remove exactly what we registered. The old code passed the
                    # bound methods while lambdas had been added, so Telethon
                    # matched nothing and handlers piled up on every reconnect —
                    # each message ended up forwarded once per reconnect cycle.
                    state.clear_handlers(client)

                    handlers = [
                        (lambda e: self.on_new_message(user_id, e),
                         events.NewMessage(chats=source_peer)),
                        (lambda e: self.on_message_edit(user_id, e),
                         events.MessageEdited(chats=source_peer)),
                        (lambda e: self.on_message_delete(user_id, e),
                         events.MessageDeleted(chats=source_peer)),
                    ]
                    for cb, builder in handlers:
                        client.add_event_handler(cb, builder)
                        state.register_handler(cb, builder)
                    state.handlers_active = True

                    log.info("Forwarding active for user %s (source %s)", user_id, source_peer)

                    while self.running and config.get("forwarding_active"):
                        if not client.is_connected():
                            break
                        await asyncio.sleep(5)

                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.error("Worker error: %s: %s", type(e).__name__, e)
                    await asyncio.sleep(RECONNECT_DELAY)
        finally:
            if state.client:
                state.clear_handlers(state.client)
            state.handlers_active = False
            self.clear_album_state(user_id)
            state.forwarding_task = None
    
    async def on_new_message(self, user_id: int, event):
        try:
            msg = event.message
            if hasattr(msg, 'grouped_id') and msg.grouped_id:
                await self.handle_album(user_id, msg)
            else:
                await self.forward_message(user_id, msg)
        except Exception as e:
            log.error(f"New message error: {e}")
    
    async def on_message_edit(self, user_id: int, event):
        try:
            msg = event.message
            mappings = self.get_mappings(user_id)

            msg_key = str(msg.id)
            if msg_key not in mappings or not mappings[msg_key]:
                return

            # A media message whose caption was cleared must not have its media
            # wiped: Telethon treats text=None as "leave the text alone".
            if msg.message is None and not msg.media:
                return

            text_filters = self.get_filters(user_id).get("text_filters", [])
            new_text, new_entities = self.apply_text_filters(
                msg.message, msg.entities, text_filters)
            new_text = clip_message(new_text) or None

            client = await self.get_client(user_id)
            if not client:
                return

            for mapping in mappings[msg_key]:
                try:
                    await client.edit_message(
                        mapping["dest"], mapping["msg_id"], new_text,
                        formatting_entities=new_entities or None)
                except MessageIdInvalidError:
                    log.info("Mirrored message %s no longer exists in dest %s",
                             mapping["msg_id"], mapping["dest"])
                except Exception as e:
                    log.error("Edit error: %s: %s", type(e).__name__, e)
        except Exception as e:
            log.error("Edit handler error: %s: %s", type(e).__name__, e)

    async def on_message_delete(self, user_id: int, event):
        try:
            mappings = self.get_mappings(user_id)
            deleted_ids = list(getattr(event, "deleted_ids", None) or [])
            pending = [mid for mid in deleted_ids if str(mid) in mappings]
            if not pending:
                return          # nothing mirrored -> no client, no disk write

            client = await self.get_client(user_id)
            if not client:
                return

            changed = False
            for msg_id in pending:
                msg_key = str(msg_id)
                for mapping in mappings.get(msg_key, []):
                    try:
                        await client.delete_messages(mapping["dest"], mapping["msg_id"])
                        changed = True
                    except MessageIdInvalidError:
                        changed = True
                    except Exception as e:
                        log.error("Delete error: %s: %s", type(e).__name__, e)
                mappings.pop(msg_key, None)
                changed = True

            if changed:
                self.save_mappings(user_id)
        except Exception as e:
            log.error("Delete handler error: %s: %s", type(e).__name__, e)
    
    # ========== BOT COMMANDS WITH WORKING FILTERS ==========
    
    async def start_bot(self):
        log.info("Starting bot...")
        self._loop = asyncio.get_running_loop()
        self._shutdown_event = asyncio.Event()

        self.bot = TelegramClient("bot_session", AUTO_API_ID, AUTO_API_HASH)
        await self.bot.start(bot_token=BOT_TOKEN)

        # `incoming=True` matters: Telethon also delivers the bot's OWN outgoing
        # messages, and without the guard replies like "✅ Find: `x`" were fed
        # back into the login/filter state machine as if the user typed them.
        @self.bot.on(events.NewMessage(incoming=True, pattern=r'^/start(?:@\w+)?\s*$'))
        async def start_cmd(event):
            await self.show_menu(event)

        @self.bot.on(events.NewMessage(incoming=True, pattern=r'^/help(?:@\w+)?\s*$'))
        async def help_cmd(event):
            await self.show_help(event)

        @self.bot.on(events.NewMessage(incoming=True, pattern=r'^/status(?:@\w+)?\s*$'))
        async def status_cmd(event):
            await self.show_status(event)

        @self.bot.on(events.NewMessage(incoming=True, pattern=r'^/cancel(?:@\w+)?\s*$'))
        async def cancel_cmd(event):
            await self.cancel_step(event)

        @self.bot.on(events.CallbackQuery())
        async def callback_handler(event):
            await self.handle_callback(event)

        @self.bot.on(events.NewMessage(incoming=True))
        async def private_handler(event):
            if not event.is_private:
                return
            text = event.message.text
            if text and text.startswith('/'):
                return
            try:
                await self.handle_private_message(event)
            except Exception as e:
                log.error("Private message handler error: %s: %s", type(e).__name__, e)
                try:
                    await event.respond("❌ Something went wrong. Try /start again.")
                except Exception:
                    pass

        self._flush_task = asyncio.create_task(self._flush_loop())

        for user_id_str, config in list(self.configs.items()):
            if config.get("forwarding_active") and config.get("logged_in"):
                try:
                    user_id = int(user_id_str)
                except (TypeError, ValueError):
                    log.error("Skipping config with a non-numeric user id: %r", user_id_str)
                    continue
                await self.start_forwarding(user_id)
                log.info("Resumed forwarding for user %s", user_id)

        log.info("Bot is ready!")

        # Race the disconnect against our own shutdown request so SIGTERM/SIGINT
        # actually stop the process instead of being ignored until SIGKILL.
        disconnect_task = asyncio.ensure_future(self.bot.run_until_disconnected())
        shutdown_task = asyncio.ensure_future(self._shutdown_event.wait())
        try:
            await asyncio.wait({disconnect_task, shutdown_task},
                               return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (disconnect_task, shutdown_task):
                if not task.done():
                    task.cancel()
            if self._flush_task and not self._flush_task.done():
                self._flush_task.cancel()
            await self.shutdown()

    async def cancel_step(self, event):
        """/cancel — abort whatever multi-step flow the user is stuck in."""
        user_id = event.sender_id
        config = self.get_config(user_id)
        if not config.get("current_step"):
            await event.respond("Nothing to cancel.")
            return
        config["current_step"] = None
        self.save_config(user_id)
        self.temp_filters.pop(user_id, None)
        await event.respond("✅ Cancelled. Back to the menu:",
                            buttons=[Button.inline("🔙 Menu", b"back")])
    
    @staticmethod
    async def reply(event, text: str, buttons=None):
        """Edit in place for callback queries, otherwise send a new message.

        Using `respond()` inside a callback used to stack a brand-new menu
        message on every button press.
        """
        text = clip_message(text)
        buttons = ForwarderBot._safe_buttons(buttons)
        if hasattr(event, "edit"):
            try:
                return await event.edit(text, buttons=buttons)
            except Exception as e:
                log.warning("edit() failed (%s), falling back to respond()", e)
        return await event.respond(text, buttons=buttons)

    @staticmethod
    def _safe_buttons(buttons):
        """Clip button labels to Telegram's 64-byte limit and cap the row count."""
        if not buttons:
            return None

        # Accept both [[btn, btn], [btn]] and a flat [btn, btn] list.
        rows = buttons if isinstance(buttons[0], (list, tuple)) else [buttons]

        safe = []
        for row in rows[:MAX_BUTTON_ROWS]:
            new_row = []
            for btn in row:
                text = getattr(btn, "text", None)
                data = getattr(btn, "data", None)
                if text is None or data is None:
                    new_row.append(btn)          # url / switch_pm / etc.
                    continue
                new_row.append(Button.inline(clip_bytes(text), data))
            if new_row:
                safe.append(new_row)
        return safe or None

    async def show_menu(self, event):
        user_id = event.sender_id
        config = self.get_config(user_id)

        logged_in = bool(config.get("logged_in"))
        status = "✅ Logged In" if logged_in else "❌ Not Logged In"
        fwd_status = "🟢 Active" if config.get("forwarding_active") else "🔴 Stopped"
        tag_status = "✅ ON" if config.get("forward_with_tag") else "❌ OFF"

        menu_text = f"""
🤖 **Message Forwarder Bot**

📊 **Status**: {status}
📱 **Source**: {clip_message(config.get('source_chat_name') or 'Not Set')}
📤 **Destinations**: {len(config.get('destinations', []))}
🔄 **Forwarding**: {fwd_status}
🏷️ **Forward Tag**: {tag_status}

✨ **Features**:
• Zero-delay forwarding
• No tag on media (when OFF)
• Album support
• Edit/Delete mirroring
• Text & Media filters
        """

        buttons = []
        if not logged_in:
            buttons.append([Button.inline("🔐 Login", b"login")])
        else:
            buttons.append([Button.inline("📱 Set Source", b"set_source")])
            buttons.append([Button.inline("📤 Manage Destinations", b"manage_dests")])
            buttons.append([Button.inline("🔧 Filters", b"manage_filters")])
            buttons.append([Button.inline(
                f"🏷️ Tag: {'ON' if config.get('forward_with_tag') else 'OFF'}", b"toggle_tag")])

            if config.get("source_chat") and config.get("destinations"):
                if config.get("forwarding_active"):
                    buttons.append([Button.inline("⏸️ Stop", b"stop_forward")])
                else:
                    buttons.append([Button.inline("▶️ Start", b"start_forward")])

            buttons.append([Button.inline("🔄 Restart", b"restart")])
            buttons.append([Button.inline("🚪 Logout", b"logout")])

        buttons.append([Button.inline("❓ Help", b"help")])

        await self.reply(event, menu_text, buttons=buttons)

    async def show_help(self, event):
        help_text = """
📚 **Help Guide**

**Setup:**
1. Login with API ID & Hash (from https://my.telegram.org)
2. Enter phone number and verification code
3. Set source chat (where to forward from)
4. Add destination chats (where to forward to)
5. Start forwarding!

**Filters:**
• **Text Filters**: Replace words/phrases in messages
• **Media Filters**: Replace specific media with custom files

**Commands:**
/start - Main menu
/status - Show status
/cancel - Abort the current step
/help - This help
        """
        await self.reply(event, help_text, buttons=[Button.inline("🔙 Menu", b"back")])

    async def show_status(self, event):
        user_id = event.sender_id
        config = self.get_config(user_id)
        state = self.get_user_state(user_id)
        filters = self.get_filters(user_id)

        connected = bool(state.client and state.client_authorized
                         and state.client.is_connected())

        status_text = f"""
📊 **Status**

🔐 Login: {'✅ Yes' if config.get('logged_in') else '❌ No'}
📱 Source: {clip_message(config.get('source_chat_name') or 'Not Set')}
📤 Destinations: {len(config.get('destinations', []))}
🔄 Forwarding: {'🟢 Active' if config.get('forwarding_active') else '🔴 Stopped'}
🏷️ Tag Mode: {'ON' if config.get('forward_with_tag') else 'OFF'}
🔌 Connection: {'Connected' if connected else 'Disconnected'}
📨 Tracked: {len(state.processed_messages)} messages
📝 Text Filters: {len(filters.get('text_filters', []))}
🖼️ Media Filters: {len(filters.get('media_filters', []))}
        """
        await self.reply(event, status_text, buttons=[Button.inline("🔙 Menu", b"back")])
    
    async def handle_callback(self, event):
        try:
            raw = getattr(event, "data", None)
            if not raw:
                return
            data = raw.decode() if isinstance(raw, bytes) else str(raw)
        except Exception as e:
            log.error("Unreadable callback data: %s", e)
            return

        try:
            await event.answer()
            
            if data == "login":
                await self.start_login(event)
            elif data == "set_source":
                await self.select_chat(event, "source")
            elif data == "manage_dests":
                await self.show_destinations(event)
            elif data == "add_dest":
                await self.select_chat(event, "dest")
            elif data == "manage_filters":
                await self.show_filters_menu(event)
            elif data == "add_text_filter":
                await self.start_text_filter(event)
            elif data == "add_media_filter":
                await self.start_media_filter(event)
            elif data == "view_text_filters":
                await self.view_text_filters(event)
            elif data == "view_media_filters":
                await self.view_media_filters(event)
            elif data == "toggle_tag":
                await self.toggle_tag(event)
            elif data == "start_forward":
                await self.start_forwarding_callback(event)
            elif data == "stop_forward":
                await self.stop_forwarding_callback(event)
            elif data == "restart":
                await self.restart_forwarding(event)
            elif data == "logout":
                await self.logout(event)
            elif data == "help":
                await self.show_help(event)
            elif data == "back":
                await self.show_menu(event)
            elif data.startswith("del_dest_"):
                await self.remove_destination(event, data[9:])
            elif data.startswith("del_text_"):
                await self.delete_text_filter(event, int(data[9:]))
            elif data.startswith("del_media_"):
                await self.delete_media_filter(event, int(data[10:]))
            elif data.startswith("sel_src_"):
                await self.set_source(event, data[8:])
            elif data.startswith("sel_dst_"):
                await self.add_destination(event, data[8:])
            elif data.startswith("page_src_") or data.startswith("page_dst_"):
                chat_type = "source" if data.startswith("page_src_") else "dest"
                try:
                    page = int(data.rsplit("_", 1)[1])
                except ValueError:
                    page = 0
                await self.select_chat(event, chat_type, page=page)
            else:
                log.warning("Unhandled callback data: %r", data)
                
        except Exception as e:
            log.error(f"Callback error: {e}")
            await event.answer(f"Error: {str(e)[:50]}", alert=True)
    
    # ========== LOGIN HANDLERS ==========
    
    async def start_login(self, event):
        user_id = event.sender_id
        config = self.get_config(user_id)
        config["current_step"] = "api_id"
        self.save_config(user_id)
        
        await event.edit(
            "🔐 **Login - Step 1/4**\n\nSend your **API ID**\n\nGet from: https://my.telegram.org",
            buttons=[Button.inline("❌ Cancel", b"back")]
        )
    
    async def _download_filter_media(self, message, user_id: int, prefix: str,
                                     tag: str) -> Optional[str]:
        """Download a media message into MEDIA_FILES_DIR and return its real path.

        Telethon *appends* a guessed extension when the target filename has
        none, so the previously stored path never existed on disk and every
        media filter failed with FileNotFoundError at send time. We now pick
        the extension ourselves and trust the value `download_media` returns.
        """
        ext = ""
        try:
            mime = getattr(getattr(message.media, "document", None), "mime_type", None) \
                or getattr(getattr(message.media, "photo", None), "mime_type", None)
            if mime:
                ext = mimetypes.guess_extension(mime.split(";")[0].strip()) or ""
                if ext == ".jpe":      # guess_extension's odd choice for jpeg
                    ext = ".jpg"
        except Exception:
            ext = ""
        if not ext:
            ext = ".bin"

        MEDIA_FILES_DIR.mkdir(parents=True, exist_ok=True)
        target = MEDIA_FILES_DIR / f"{prefix}_{user_id}_{tag}_{int(time.time())}{ext}"
        try:
            result = await message.download_media(file=str(target))
        except Exception as e:
            log.error("Could not download filter media: %s: %s", type(e).__name__, e)
            return None

        # `download_media` returns the path it actually wrote to.
        for candidate in (result, str(target)):
            if candidate and Path(str(candidate)).exists():
                return str(candidate)
        log.error("Downloaded media vanished: %s", target)
        return None

    async def handle_private_message(self, event):
        user_id = event.sender_id
        config = self.get_config(user_id)
        step = config.get("current_step")

        if not step:
            return

        # Caption-less media gives text=None; `.strip()` on it used to raise
        # AttributeError and kill the whole media-filter flow.
        text = (event.message.text or event.message.raw_text or "").strip()
        has_media = bool(event.message.media)

        if step in ("api_id", "api_hash", "phone", "code", "2fa",
                    "text_find", "text_replace") and not text:
            await event.respond(
                "❌ Please send text for this step (or /cancel to abort).")
            return

        if step == "api_id":
            try:
                api_id = int(text)
            except ValueError:
                await event.respond("❌ API ID must be a number! e.g. `1234567`")
                return
            if api_id <= 0:
                await event.respond("❌ That API ID does not look right.")
                return
            config["api_id"] = api_id
            config["current_step"] = "api_hash"
            self.save_config(user_id)
            await event.respond(
                "✅ API ID saved!\n\n**Step 2/4:** Send your **API HASH**")

        elif step == "api_hash":
            cleaned = text.lower().replace(" ", "")
            if len(cleaned) != 32 or any(c not in "0123456789abcdef" for c in cleaned):
                await event.respond(
                    "❌ API Hash must be exactly 32 hex characters.\n"
                    "Copy it from https://my.telegram.org")
                return
            config["api_hash"] = cleaned
            config["current_step"] = "phone"
            self.save_config(user_id)
            await event.respond(
                "✅ API Hash saved!\n\n**Step 3/4:** Send your **phone number**"
                "\nExample: +911234567890")

        elif step == "phone":
            digits = text.lstrip("+").replace(" ", "").replace("-", "")
            if not digits.isdigit() or len(digits) < 7:
                await event.respond(
                    "❌ That does not look like a phone number.\n"
                    "Use the international format, e.g. +911234567890")
                return
            await self.send_verification_code(event, text)

        elif step == "code":
            await self.verify_code(event, text)

        elif step == "2fa":
            await self.verify_2fa(event, text)

        # ---------- text filter steps ----------
        elif step == "text_find":
            if len(text.encode("utf-8")) > MAX_BUTTON_BYTES * 4:
                await event.respond("❌ That text is too long to filter.")
                return
            self.temp_filters[user_id] = {"find": text}
            config["current_step"] = "text_replace"
            self.save_config(user_id)
            await event.respond(
                f"✅ Find: `{text}`\n\n**Step 2/2:** Send the **REPLACEMENT** text "
                f"(send a single `-` to delete the phrase):")

        elif step == "text_replace":
            find_text = self.temp_filters.get(user_id, {}).get("find", "")
            if not find_text:
                # Lost temp state (e.g. after a restart) — restart the flow
                # instead of saving a filter with an empty needle, which would
                # have hung the event loop forever.
                config["current_step"] = "text_find"
                self.save_config(user_id)
                await event.respond(
                    "⚠️ The previous step was lost. Please send the text to "
                    "**FIND** again:")
                return

            replacement = "" if text == "-" else text
            filters = self.get_filters(user_id)
            filters.setdefault("text_filters", []).append(
                {"find": find_text, "replace": replacement})
            self.save_filters(user_id)
            config["current_step"] = None
            self.save_config(user_id)
            self.temp_filters.pop(user_id, None)
            await event.respond(
                f"✅ **Text Filter Added!**\n\nFind: `{find_text}`\n"
                f"Replace: `{replacement or '(removed)'}`")
            await self.show_filters_menu(event)

        # ---------- media filter steps ----------
        elif step == "media_original":
            if not has_media:
                await event.respond(
                    "❌ Please send the **original** media (photo/video/sticker) "
                    "you want to replace.")
                return

            media_id = self.get_media_id(event.message.media)
            if not media_id:
                await event.respond(
                    "❌ Could not read that media's ID. Send a photo, video or "
                    "document (not a link/preview).")
                return

            filepath = await self._download_filter_media(
                event.message, user_id, "orig", media_id)
            if not filepath:
                await event.respond("❌ Download failed. Please try again.")
                return

            self.temp_filters[user_id] = {
                "original_id": media_id, "original_file": filepath}
            config["current_step"] = "media_replace"
            self.save_config(user_id)
            await event.respond("✅ Original saved!\n\n**Step 2/2:** Send the **REPLACEMENT** media:")

        elif step == "media_replace":
            if not has_media:
                await event.respond("❌ Please send the **replacement** media file.")
                return

            temp = self.temp_filters.get(user_id, {})
            orig_id = temp.get("original_id")
            orig_file = temp.get("original_file")

            if not orig_id or not orig_file or not Path(orig_file).exists():
                config["current_step"] = "media_original"
                self.temp_filters.pop(user_id, None)
                self.save_config(user_id)
                await event.respond(
                    "⚠️ The original media was lost. Please send the **ORIGINAL** "
                    "media again to restart this filter.")
                return

            filepath = await self._download_filter_media(
                event.message, user_id, "repl", orig_id)
            if not filepath:
                await event.respond("❌ Download failed. Please send the replacement again.")
                return

            filters = self.get_filters(user_id)
            filters.setdefault("media_filters", []).append({
                "original_id": orig_id,
                "original_file": orig_file,
                "replace_file": filepath,
            })
            self.save_filters(user_id)

            config["current_step"] = None
            self.save_config(user_id)
            self.temp_filters.pop(user_id, None)

            await event.respond(
                f"✅ **Media Filter Added!**\n\nOriginal ID: `{orig_id[:20]}…`")
            await self.show_filters_menu(event)

        else:
            log.warning("Unknown wizard step %r for user %s - resetting", step, user_id)
            config["current_step"] = None
            self.save_config(user_id)
    
    def _mark_logged_in(self, user_id: int, config: dict):
        config["logged_in"] = True
        config["current_step"] = None
        self.save_config(user_id)
        state = self.get_user_state(user_id)
        state.client_authorized = True
        state.reconnect_attempts = 0

    async def send_verification_code(self, event, phone: str):
        user_id = event.sender_id
        config = self.get_config(user_id)
        state = self.get_user_state(user_id)

        try:
            api_id = int(config.get("api_id"))
        except (TypeError, ValueError):
            config["current_step"] = "api_id"
            self.save_config(user_id)
            await event.respond("❌ No valid API ID stored. Please send your **API ID** again.")
            return

        api_hash = config.get("api_hash")
        if not api_hash:
            config["current_step"] = "api_hash"
            self.save_config(user_id)
            await event.respond("❌ No API Hash stored. Please send your **API HASH** again.")
            return

        client = None
        try:
            SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
            client = TelegramClient(str(SESSIONS_DIR / f"user_{user_id}"), api_id, api_hash)
            await client.connect()

            # Adopt the client first so a failure below can still clean it up.
            async with state.client_lock:
                if state.client is not None and state.client is not client:
                    try:
                        await state.client.disconnect()
                    except Exception:
                        pass
                state.client = client
                state.client_authorized = False

            if await client.is_user_authorized():
                self._mark_logged_in(user_id, config)
                await event.respond(
                    "✅ This session is already authorised!\n\nNo code needed.",
                    buttons=[Button.inline("🔙 Menu", b"back")])
                return

            await client.send_code_request(phone)

            config["phone"] = phone
            config["current_step"] = "code"
            self.save_config(user_id)
            await event.respond("📲 **Verification code sent!**\n\nEnter the code:")

        except SessionPasswordNeededError:
            config["phone"] = phone
            config["current_step"] = "2fa"
            self.save_config(user_id)
            await event.respond("🔐 **2FA enabled on this account.**\n\nEnter your password:")
        except FloodWaitError as e:
            wait = min(int(getattr(e, "seconds", 0) or 0), 3600)
            await event.respond(f"⏳ Telegram asked us to wait {wait}s. Try again later.")
        except Exception as e:
            log.error("send_verification_code failed: %s: %s", type(e).__name__, e)
            if client is not None:
                async with state.client_lock:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                    if state.client is client:
                        state.client = None
                        state.client_authorized = False
            await event.respond(
                f"❌ Could not send the code ({type(e).__name__}): {str(e)[:120]}\n"
                f"Check the API ID/Hash and the phone number, then try again.")

    async def verify_code(self, event, code: str):
        user_id = event.sender_id
        config = self.get_config(user_id)
        state = self.get_user_state(user_id)

        if not state.client:
            config["current_step"] = None
            self.save_config(user_id)
            await event.respond("❌ Session lost. Press /start and log in again.")
            return

        code = code.replace(" ", "").strip()
        if not code.isdigit():
            await event.respond("❌ The code is digits only. Try again:")
            return

        try:
            await state.client.sign_in(phone=config.get("phone"), code=code)
            self._mark_logged_in(user_id, config)
            await event.respond("✅ **Login Successful!**",
                                buttons=[Button.inline("🔙 Menu", b"back")])
        except SessionPasswordNeededError:
            config["current_step"] = "2fa"
            self.save_config(user_id)
            await event.respond("🔐 **2FA Required**\n\nEnter your password:")
        except PhoneCodeInvalidError:
            await event.respond("❌ Wrong code. Please try again (or /cancel):")
        except PhoneCodeExpiredError:
            config["current_step"] = "phone"
            self.save_config(user_id)
            await event.respond("⌛ That code expired. Please send your **phone number** again:")
        except Exception as e:
            log.error("verify_code failed: %s: %s", type(e).__name__, e)
            await event.respond(f"❌ Login failed ({type(e).__name__}): {str(e)[:120]}")

    async def verify_2fa(self, event, password: str):
        user_id = event.sender_id
        config = self.get_config(user_id)
        state = self.get_user_state(user_id)

        if not state.client:
            config["current_step"] = None
            self.save_config(user_id)
            await event.respond("❌ Session lost. Press /start and log in again.")
            return

        try:
            await state.client.sign_in(password=password)
            self._mark_logged_in(user_id, config)
            await event.respond("✅ **Login Successful!**",
                                buttons=[Button.inline("🔙 Menu", b"back")])
        except Exception as e:
            log.warning("2FA failed for user %s: %s", user_id, type(e).__name__)
            await event.respond(
                f"❌ Password not accepted ({type(e).__name__}). Try again, or /cancel:")
    
    # ========== CHAT MANAGEMENT ==========

    CHATS_PER_PAGE = 20
    DIALOG_CACHE_TTL = 120

    async def _get_dialogs(self, user_id: int, client) -> list:
        """Fetch (and briefly cache) the dialog list so paging is cheap."""
        now = time.monotonic()
        cached = self.dialog_cache.get(user_id)
        if cached and now - cached[0] < self.DIALOG_CACHE_TTL:
            return cached[1]

        dialogs = await client.get_dialogs(limit=200)
        usable = []
        for d in dialogs or []:
            if not (d.is_group or d.is_channel or d.is_user):
                continue
            entity = getattr(d, "entity", None)
            if entity is not None and getattr(entity, "deleted", False):
                continue          # deleted accounts cannot be resolved later
            usable.append(d)
        self.dialog_cache[user_id] = (now, usable)
        return usable

    @staticmethod
    def _dialog_label(dialog) -> str:
        icon = "👤" if dialog.is_user else "📢" if dialog.is_channel else "👥"
        name = dialog.name or "Unknown"
        return clip_bytes(f"{icon} {name}", MAX_BUTTON_BYTES)

    async def select_chat(self, event, chat_type: str, page: int = 0):
        user_id = event.sender_id
        client = await self.get_client(user_id)

        if not client:
            await event.answer("❌ Please login first!", alert=True)
            return

        try:
            await event.edit("⏳ Loading chats...")
            dialogs = await self._get_dialogs(user_id, client)

            per_page = self.CHATS_PER_PAGE
            total_pages = max(1, (len(dialogs) + per_page - 1) // per_page)
            page = max(0, min(page, total_pages - 1))
            window = dialogs[page * per_page:(page + 1) * per_page]

            prefix = "sel_src_" if chat_type == "source" else "sel_dst_"
            page_prefix = f"page_{'src' if chat_type == 'source' else 'dst'}_"

            buttons = []
            for dialog in window:
                buttons.append([Button.inline(
                    self._dialog_label(dialog), f"{prefix}{dialog.id}".encode())])

            if not buttons:
                buttons.append([Button.inline("No chats found", b"back")])

            nav = []
            if page > 0:
                nav.append(Button.inline("⬅️ Prev", f"{page_prefix}{page - 1}".encode()))
            nav.append(Button.inline(f"📄 {page + 1}/{total_pages}", f"{page_prefix}{page}".encode()))
            if page + 1 < total_pages:
                nav.append(Button.inline("Next ➡️", f"{page_prefix}{page + 1}".encode()))
            buttons.append(nav)
            buttons.append([Button.inline("🔙 Back", b"back")])

            title = "Source Chat" if chat_type == "source" else "Destination Chat"
            await self.reply(
                event, f"📋 **Select {title}:** ({len(dialogs)} chats)", buttons=buttons)
        except Exception as e:
            log.error("select_chat failed: %s: %s", type(e).__name__, e)
            await event.answer(f"Error: {str(e)[:60]}", alert=True)

    @staticmethod
    def _entity_name(entity) -> str:
        return (getattr(entity, "title", None)
                or getattr(entity, "first_name", None)
                or getattr(entity, "username", None)
                or "Unknown")

    async def set_source(self, event, chat_id: str):
        user_id = event.sender_id
        client = await self.get_client(user_id)

        if not client:
            await event.answer("❌ Please login first!", alert=True)
            return

        try:
            entity = await client.get_entity(int(chat_id))
            name = self._entity_name(entity)

            config = self.get_config(user_id)
            changed = str(config.get("source_chat")) != str(chat_id)
            config["source_chat"] = chat_id
            config["source_chat_name"] = name
            self.save_config(user_id)

            # The live handlers are bound to the OLD chat id; without this the
            # worker keeps forwarding the previous channel until a manual
            # restart of the whole process.
            if changed and config.get("forwarding_active"):
                await self.restart_forwarding_worker(user_id)
                log.info("Rebound handlers for user %s to new source %s", user_id, chat_id)

            await self.reply(event, f"✅ **Source Chat Set!**\n\n📱 {clip_message(name)}",
                             buttons=[Button.inline("🔙 Menu", b"back")])
        except Exception as e:
            log.error("set_source failed for %s: %s", chat_id, e)
            await event.answer(f"Error: {str(e)[:60]}", alert=True)

    async def add_destination(self, event, chat_id: str):
        user_id = event.sender_id
        client = await self.get_client(user_id)

        if not client:
            await event.answer("❌ Please login first!", alert=True)
            return

        try:
            entity = await client.get_entity(int(chat_id))
            name = self._entity_name(entity)

            config = self.get_config(user_id)
            destinations = config.get("destinations", [])

            if any(str(d.get("id")) == str(chat_id) for d in destinations):
                await event.answer("Already added!", alert=True)
                return

            destinations.append({"id": chat_id, "name": name})
            config["destinations"] = destinations
            self.save_config(user_id)

            await self.reply(
                event,
                f"✅ **Destination Added!**\n\n📤 {clip_message(name)}\nTotal: {len(destinations)}",
                buttons=[[Button.inline("➕ Add More", b"add_dest")],
                         [Button.inline("📤 View All", b"manage_dests")],
                         [Button.inline("🔙 Menu", b"back")]])
        except Exception as e:
            log.error("add_destination failed for %s: %s", chat_id, e)
            await event.answer(f"Error: {str(e)[:60]}", alert=True)

    async def show_destinations(self, event):
        user_id = event.sender_id
        config = self.get_config(user_id)
        destinations = config.get("destinations", [])

        if not destinations:
            await self.reply(event, "📤 **No destinations yet!**",
                             buttons=[[Button.inline("➕ Add Destination", b"add_dest")],
                                      [Button.inline("🔙 Menu", b"back")]])
            return

        text = f"📤 **Destinations ({len(destinations)}):**\n\n"
        buttons = []

        for dest in destinations:
            name = dest.get("name") or str(dest.get("id"))
            text += f"• {clip_message(name)}\n"
            buttons.append([Button.inline(
                f"🗑️ {clip_bytes(name, MAX_BUTTON_BYTES - 12)}",
                f"del_dest_{dest.get('id')}".encode())])

        text = clip_message(text)
        buttons.append([Button.inline("➕ Add More", b"add_dest")])
        buttons.append([Button.inline("🔙 Menu", b"back")])

        await self.reply(event, text, buttons=buttons)

    async def remove_destination(self, event, dest_id: str):
        user_id = event.sender_id
        config = self.get_config(user_id)

        before = len(config.get("destinations", []))
        config["destinations"] = [d for d in config.get("destinations", [])
                                  if str(d.get("id")) != str(dest_id)]
        self.save_config(user_id)

        if len(config["destinations"]) == before:
            await event.answer("Already removed.", alert=True)
        else:
            await event.answer("✅ Removed!")
        await self.show_destinations(event)
    
    # ========== WORKING FILTERS ==========
    
    async def show_filters_menu(self, event):
        user_id = event.sender_id
        filters = self.get_filters(user_id)
        
        text_filters = len(filters.get("text_filters", []))
        media_filters = len(filters.get("media_filters", []))
        
        text = f"""
🔧 **Filter Manager**

📝 **Text Filters**: {text_filters}
🖼️ **Media Filters**: {media_filters}

• Text filters replace words/phrases
• Media filters replace specific media
        """
        
        buttons = [
            [Button.inline("📝 Add Text Filter", b"add_text_filter")],
            [Button.inline("🖼️ Add Media Filter", b"add_media_filter")],
        ]
        
        if text_filters:
            buttons.append([Button.inline("📋 View Text Filters", b"view_text_filters")])
        if media_filters:
            buttons.append([Button.inline("🖼️ View Media Filters", b"view_media_filters")])
        
        buttons.append([Button.inline("🔙 Main Menu", b"back")])
        
        await event.edit(text, buttons=buttons)
    
    async def start_text_filter(self, event):
        user_id = event.sender_id
        config = self.get_config(user_id)
        config["current_step"] = "text_find"
        self.save_config(user_id)
        
        await event.edit(
            "📝 **Add Text Filter - Step 1/2**\n\n"
            "Send the text to **FIND** (case-sensitive):",
            buttons=[Button.inline("❌ Cancel", b"manage_filters")]
        )
    
    async def start_media_filter(self, event):
        user_id = event.sender_id
        config = self.get_config(user_id)
        config["current_step"] = "media_original"
        self.save_config(user_id)
        
        await event.edit(
            "🖼️ **Add Media Filter - Step 1/2**\n\n"
            "Send the **ORIGINAL** media (photo/video/sticker) to replace:",
            buttons=[Button.inline("❌ Cancel", b"manage_filters")]
        )
    
    async def view_text_filters(self, event):
        user_id = event.sender_id
        filters = self.get_filters(user_id).get("text_filters", [])

        if not filters:
            await event.answer("No text filters!", alert=True)
            await self.show_filters_menu(event)
            return

        text = f"📝 **Text Filters ({len(filters)}):**\n\n"
        buttons = []

        for i, f in enumerate(filters):
            if not isinstance(f, dict):
                continue
            find = f.get("find") or ""
            repl = f.get("replace") or "(removed)"
            # `.get()` — a hand-edited/older JSON without these keys used to
            # raise KeyError and freeze the whole filters screen.
            text += f"{i + 1}. `{clip_message(find)}` → `{clip_message(repl)}`\n"
            buttons.append([Button.inline(f"🗑️ Delete #{i + 1}", f"del_text_{i}".encode())])

        text = clip_message(text)
        buttons.append([Button.inline("➕ Add", b"add_text_filter")])
        buttons.append([Button.inline("🔙 Back", b"manage_filters")])

        await self.reply(event, text, buttons=buttons)

    async def view_media_filters(self, event):
        user_id = event.sender_id
        filters = self.get_filters(user_id).get("media_filters", [])

        if not filters:
            await event.answer("No media filters!", alert=True)
            await self.show_filters_menu(event)
            return

        text = f"🖼️ **Media Filters ({len(filters)}):**\n\n"
        buttons = []

        for i, f in enumerate(filters):
            if not isinstance(f, dict):
                continue
            oid = str(f.get("original_id") or "?")
            repl = f.get("replace_file") or ""
            status = "✅" if repl and Path(repl).exists() else "⚠️ missing file"
            text += f"{i + 1}. ID `{oid[:16]}…` {status}\n"
            buttons.append([Button.inline(f"🗑️ Delete #{i + 1}", f"del_media_{i}".encode())])

        text = clip_message(text)
        buttons.append([Button.inline("➕ Add", b"add_media_filter")])
        buttons.append([Button.inline("🔙 Back", b"manage_filters")])

        await self.reply(event, text, buttons=buttons)

    async def delete_text_filter(self, event, idx: int):
        user_id = event.sender_id
        filters = self.get_filters(user_id)
        text_filters = filters.setdefault("text_filters", [])

        if not (0 <= idx < len(text_filters)):
            await event.answer("Already deleted.", alert=True)
            await self.view_text_filters(event)
            return

        del text_filters[idx]
        self.save_filters(user_id)
        await event.answer("✅ Filter deleted!")
        await self.view_text_filters(event)

    async def delete_media_filter(self, event, idx: int):
        user_id = event.sender_id
        filters = self.get_filters(user_id)
        media_filters = filters.setdefault("media_filters", [])

        if not (0 <= idx < len(media_filters)):
            await event.answer("Already deleted.", alert=True)
            await self.view_media_filters(event)
            return

        entry = media_filters[idx] or {}
        for key in ("original_file", "replace_file"):
            path = entry.get(key)
            if not path:
                continue
            try:
                Path(path).unlink(missing_ok=True)
            except Exception as e:
                log.warning("Could not delete %s: %s", path, e)

        del media_filters[idx]
        self.save_filters(user_id)
        await event.answer("✅ Filter deleted!")
        await self.view_media_filters(event)
    
    # ========== FORWARDING CONTROL ==========

    async def toggle_tag(self, event):
        user_id = event.sender_id
        config = self.get_config(user_id)
        config["forward_with_tag"] = not config.get("forward_with_tag", False)
        self.save_config(user_id)
        state = "ON ✅" if config["forward_with_tag"] else "OFF ❌"
        await event.answer(f"Forward Tag: {state}", alert=True)
        await self.show_menu(event)

    async def start_forwarding_callback(self, event):
        user_id = event.sender_id
        config = self.get_config(user_id)

        if not config.get("logged_in"):
            await event.answer("❌ Please login first!", alert=True)
            return

        if not config.get("source_chat"):
            await event.answer("❌ Set source chat first!", alert=True)
            return

        if not config.get("destinations"):
            await event.answer("❌ Add destinations first!", alert=True)
            return

        config["forwarding_active"] = True
        self.save_config(user_id)
        await self.start_forwarding(user_id)

        await self.reply(event, "✅ **Forwarding Started!**",
                         buttons=[Button.inline("🔙 Menu", b"back")])

    async def stop_forwarding_callback(self, event):
        user_id = event.sender_id
        await self.stop_forwarding(user_id)
        await self.reply(event, "⏸️ **Forwarding Stopped!**",
                         buttons=[Button.inline("🔙 Menu", b"back")])

    async def restart_forwarding(self, event):
        user_id = event.sender_id
        config = self.get_config(user_id)

        # Read the flag BEFORE stopping: stop_forwarding() sets it to False, so
        # the old `if config.get("forwarding_active")` check was always False
        # and the Restart button silently did nothing at all.
        was_active = bool(config.get("forwarding_active"))

        await event.edit("🔄 Restarting...")
        await self.stop_forwarding(user_id)

        state = self.get_user_state(user_id)
        if state.client:
            state.clear_handlers(state.client)
            try:
                await state.client.disconnect()
            except Exception as e:
                log.warning("Disconnect during restart failed: %s", e)
            state.client = None
            state.client_authorized = False

        await asyncio.sleep(2)

        if was_active:
            config["forwarding_active"] = True
            self.save_config(user_id)
            await self.start_forwarding(user_id)

        outcome = "running again ✅" if was_active else "still stopped (it was off) ⏸️"
        await self.reply(event, f"✅ **Restart Complete!**\nForwarding: {outcome}",
                         buttons=[Button.inline("🔙 Menu", b"back")])

    async def logout(self, event):
        user_id = event.sender_id
        await self.stop_forwarding(user_id)

        state = self.get_user_state(user_id)
        if state.client:
            state.clear_handlers(state.client)
            try:
                await state.client.disconnect()
            except Exception as e:
                log.warning("Disconnect during logout failed: %s", e)
            state.client = None
            state.client_authorized = False

        # Delete the session file, otherwise it stays authorised on disk and the
        # next get_client() logs the user straight back in — logout was cosmetic.
        session_path = SESSIONS_DIR / f"user_{user_id}.session"
        removed = False
        try:
            if session_path.exists():
                session_path.unlink()
                removed = True
        except Exception as e:
            log.error("Could not remove session file %s: %s", session_path, e)

        config = self.get_config(user_id)
        config.update({
            "logged_in": False, "phone": None, "api_id": None, "api_hash": None,
            "source_chat": None, "source_chat_name": "Not Set", "destinations": [],
            "forwarding_active": False, "forward_with_tag": False, "current_step": None
        })
        self.save_config(user_id)

        state.processed_messages.clear()
        self.processed_cache.pop(str(user_id), None)
        self._save_json(PROCESSED_FILE, self.processed_cache)
        self.temp_filters.pop(user_id, None)
        self.dialog_cache.pop(user_id, None)
        self.clear_album_state(user_id)

        detail = "Session file deleted." if removed else "Session file was already gone."
        await self.reply(event, f"✅ **Logged Out!**\n\n{detail}",
                         buttons=[Button.inline("🔙 Menu", b"back")])


async def amain():
    bot = ForwarderBot()
    try:
        await bot.start_bot()
    finally:
        bot.flush_all(force=True)


if __name__ == "__main__":
    print("=" * 70)
    print("🚀  TELEGRAM MESSAGE FORWARDER BOT")
    print("=" * 70)

    # Create .env file if needed
    env_file = Path(".env")
    if not env_file.exists() and not os.getenv("BOT_TOKEN"):
        token = input("\nEnter your bot token: ").strip()
        if token:
            env_file.write_text(f"BOT_TOKEN={token}\n")
            print("✅ .env file created!")
        else:
            print("❌ No token provided. Exiting.")
            sys.exit(1)

    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        print("\n🛑 Bot stopped")
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
