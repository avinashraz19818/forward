"""
Telegram Message Forwarder Bot - COMPLETE VERSION
- Auto API (just phone number)
- One Source → Multiple Destinations
- Text & Media Filters
- Album support
- Ultra stable
"""

import os
from telethon import TelegramClient, events, Button
from telethon.tl.types import User, Chat, Channel, MessageMediaPhoto, MessageMediaDocument
from telethon import types
import asyncio
import json
import time
import traceback
import signal
import sys
from dotenv import load_dotenv

load_dotenv()

# Bot configuration
BOT_TOKEN = os.getenv('BOT_TOKEN', '7293372967:AAFvWvDLHLuCZEN7wEbiK2-PB6JR1TMlYTA')

# Auto API credentials
AUTO_API_ID = 6
AUTO_API_HASH = 'eb06d4abfb49dc3eeb1aeb98ae0f581e'

# Files
CONFIGS_FILE = 'user_configs.json'
FILTERS_FILE = 'user_filters.json'
MEDIA_FILES_DIR = 'media_files'
SESSIONS_DIR = 'sessions'
MAPS_FILE = 'user_msg_maps.json'

os.makedirs(SESSIONS_DIR, exist_ok=True)
os.makedirs(MEDIA_FILES_DIR, exist_ok=True)

class CompleteBot:
    def __init__(self):
        self.bot = None
        self.user_clients = {}
        self.user_configs = self.load_json(CONFIGS_FILE)
        self.user_filters = self.load_json(FILTERS_FILE)
        self.forwarding_tasks = {}
        self.running = True
        self.msg_maps = self.load_json(MAPS_FILE)

        signal.signal(signal.SIGINT, self.signal_handler)
        signal.signal(signal.SIGTERM, self.signal_handler)

    def signal_handler(self, signum, frame):
        print(f"\n🛑 Shutting down...")
        self.running = False
        sys.exit(0)

    def load_json(self, filename):
        if os.path.exists(filename):
            try:
                with open(filename, 'r') as f:
                    return json.load(f)
            except:
                return {}
        return {}

    def save_json(self, filename, data):
        try:
            with open(filename, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"❌ Save error: {e}")

    def save_configs(self):
        self.save_json(CONFIGS_FILE, self.user_configs)

    def save_filters(self):
        self.save_json(FILTERS_FILE, self.user_filters)

    def save_maps(self):
        self.save_json(MAPS_FILE, self.msg_maps)

    def get_user_config(self, user_id):
        uid = str(user_id)
        if uid not in self.user_configs:
            self.user_configs[uid] = {
                'logged_in': False,
                'phone': None,
                'source_chat': None,
                'source_chat_name': 'Not Set',
                'destinations': [],
                'forwarding_active': False,
                'current_step': None,
                'forward_with_tag': False,
                'api_id': None,
                'api_hash': None
            }
        return self.user_configs[uid]

    def get_user_map(self, user_id):
        uid = str(user_id)
        if uid not in self.msg_maps:
            self.msg_maps[uid] = {}
        return self.msg_maps[uid]

    def get_user_filters(self, user_id):
        uid = str(user_id)
        if uid not in self.user_filters:
            self.user_filters[uid] = {
                'text_filters': [],
                'media_filters': []
            }
        return self.user_filters[uid]

    def get_media_id(self, media):
        """Extract media ID"""
        try:
            if hasattr(media, 'photo'):
                return str(media.photo.id)
            elif hasattr(media, 'document'):
                return str(media.document.id)
            elif hasattr(media, 'id'):
                return str(media.id)
        except:
            pass
        return None

    async def ensure_user_client(self, user_id):
        """Get or create user client"""
        try:
            if user_id in self.user_clients:
                client, lock = self.user_clients[user_id]
                if client and client.is_connected():
                    return client, lock

            config = self.get_user_config(user_id)
            api_id = int(config.get('api_id') or AUTO_API_ID)
            api_hash = config.get('api_hash') or AUTO_API_HASH

            lock = asyncio.Lock()
            client = TelegramClient(
                f'{SESSIONS_DIR}/session_{user_id}',
                api_id,
                api_hash,
                connection_retries=5,
                retry_delay=1
            )

            await client.connect()

            if await client.is_user_authorized():
                self.user_clients[user_id] = (client, lock)
                return client, lock

            return None, None

        except Exception as e:
            print(f"❌ Client error: {e}")
            return None, None

    async def start(self):
        """Start bot"""
        print("🤖 Starting Complete Bot...")

        self.bot = TelegramClient('bot_session', AUTO_API_ID, AUTO_API_HASH)
        await self.bot.start(bot_token=BOT_TOKEN)
        print("✅ Bot started!")

        @self.bot.on(events.NewMessage(pattern='/start'))
        async def start_handler(event):
            await self.show_main_menu(event)

        @self.bot.on(events.CallbackQuery())
        async def callback_handler(event):
            await self.handle_callback(event)

        @self.bot.on(events.NewMessage())
        async def message_handler(event):
            await self.handle_message(event)

        print("✅ Bot running!")
        await self.bot.run_until_disconnected()

    async def show_main_menu(self, event):
        user_id = event.sender_id
        config = self.get_user_config(user_id)

        status = "🟢 Logged In" if config['logged_in'] else "🔴 Not Logged In"
        dest_count = len(config.get('destinations', []))

        text = f"""
🤖 **Complete Forwarder Bot**

📊 Status: {status}
📱 Source: {config.get('source_chat_name', 'Not Set')}
📤 Destinations: {dest_count}
🔄 Forwarding: {'🟢 Active' if config.get('forwarding_active') else '🔴 Stopped'}
🏷️ Forward Tag: {'🟢 ON' if config.get('forward_with_tag') else '🔴 OFF'}

✨ Features:
✅ Auto API (just phone!)
✅ Multiple destinations
✅ Text & Media filters
✅ Albums preserved
"""

        buttons = []
        if not config['logged_in']:
            buttons.append([Button.inline("🔐 Login (Phone Only)", b"login")])
        else:
            buttons.append([Button.inline("📱 Set Source", b"set_source")])
            buttons.append([Button.inline("📤 Manage Destinations", b"manage_dests")])
            buttons.append([Button.inline("🔧 Filters", b"manage_filters")])
            buttons.append([Button.inline(f"🏷️ Forward Tag: {'ON' if config.get('forward_with_tag') else 'OFF'}", b"toggle_tag")])

            if config.get('source_chat') and dest_count > 0:
                if config.get('forwarding_active'):
                    buttons.append([Button.inline("⏸️ Stop Forwarding", b"stop_forward")])
                else:
                    buttons.append([Button.inline("▶️ Start Forwarding", b"start_forward")])

            buttons.append([Button.inline("🔄 Restart", b"restart")])
            buttons.append([Button.inline("🚪 Logout", b"logout")])

        buttons.append([Button.inline("ℹ️ Help", b"help")])

        await event.respond(text, buttons=buttons)

    async def handle_callback(self, event):
        user_id = event.sender_id
        data = event.data.decode('utf-8')

        try:
            await event.answer()

            if data == "login":
                await self.start_login(event)
            elif data == "set_source":
                await self.show_chat_selection(event, 'source')
            elif data == "manage_dests":
                await self.show_destinations_menu(event)
            elif data == "add_dest":
                await self.show_chat_selection(event, 'destination')
            elif data == "manage_filters":
                await self.show_filters_menu(event)
            elif data == "add_text_filter":
                await self.start_add_text_filter(event)
            elif data == "add_media_filter":
                await self.start_add_media_filter(event)
            elif data == "toggle_tag":
                await self.toggle_forward_tag(event)
            elif data == "view_text_filters":
                await self.show_text_filters(event)
            elif data == "view_media_filters":
                await self.show_media_filters(event)
            elif data.startswith("del_dest_"):
                dest_id = data.replace("del_dest_", "")
                await self.delete_destination(event, dest_id)
            elif data.startswith("del_text_"):
                filter_idx = int(data.replace("del_text_", ""))
                await self.delete_text_filter(event, filter_idx)
            elif data.startswith("del_media_"):
                filter_idx = int(data.replace("del_media_", ""))
                await self.delete_media_filter(event, filter_idx)
            elif data.startswith("select_source_"):
                chat_id = data.replace("select_source_", "")
                await self.set_source_chat(event, chat_id)
            elif data.startswith("select_dest_"):
                chat_id = data.replace("select_dest_", "")
                await self.add_destination_chat(event, chat_id)
            elif data == "start_forward":
                await self.start_forwarding(event)
            elif data == "stop_forward":
                await self.stop_forwarding(event)
            elif data == "restart":
                await self.restart_forwarding(event)
            elif data == "logout":
                await self.logout_user(event)
            elif data == "help":
                await self.show_help(event)
            elif data == "back":
                await self.show_main_menu(event)

        except Exception as e:
            print(f"❌ Callback error: {e}")
            traceback.print_exc()

    async def start_login(self, event):
        user_id = event.sender_id
        config = self.get_user_config(user_id)
        config['current_step'] = 'login_api_id'
        self.save_configs()

        await event.edit(
            "🔐 **Login**\n\n"
            "Step 1: Send your API ID\n",
            buttons=[Button.inline("❌ Cancel", b"back")]
        )

    async def handle_message(self, event):
        if not event.is_private or event.message.message.startswith('/'):
            return

        user_id = event.sender_id
        config = self.get_user_config(user_id)
        text = event.message.message.strip()
        step = config.get('current_step')

        try:
            if step == 'login_api_id':
                await self.process_api_id(event, text)
            elif step == 'login_api_hash':
                await self.process_api_hash(event, text)
            elif step == 'login_phone':
                await self.process_phone(event, text)
            elif step == 'login_code':
                await self.process_code(event, text)
            elif step == 'login_2fa':
                await self.process_2fa(event, text)
            elif step == 'text_find':
                await self.process_text_find(event, text)
            elif step == 'text_replace':
                await self.process_text_replace(event, text)
            elif step == 'media_original':
                await self.process_media_original(event)
            elif step == 'media_replace':
                await self.process_media_replace(event)
        except Exception as e:
            print(f"❌ Message error: {e}")
            await event.respond(f"Error: {str(e)[:100]}")

    async def process_api_id(self, event, api_id_text):
        user_id = event.sender_id
        config = self.get_user_config(user_id)
        try:
            api_id = int(api_id_text.strip())
            config['api_id'] = api_id
            config['current_step'] = 'login_api_hash'
            self.save_configs()
            await event.respond("Step 2: Send your API HASH")
        except Exception as e:
            await event.respond("❌ Invalid API ID. Send a number.")

    async def process_api_hash(self, event, api_hash_text):
        user_id = event.sender_id
        config = self.get_user_config(user_id)
        config['api_hash'] = api_hash_text.strip()
        config['current_step'] = 'login_phone'
        self.save_configs()
        await event.respond(
            "Step 3: Send your phone number:\nExample: +911234567890",
            buttons=[Button.inline("❌ Cancel", b"back")]
        )

    async def process_phone(self, event, phone):
        user_id = event.sender_id
        config = self.get_user_config(user_id)

        try:
            if not config.get('api_id') or not config.get('api_hash'):
                await event.respond("❌ Missing API ID/Hash. Send /start and login again.")
                return
            client = TelegramClient(
                f'{SESSIONS_DIR}/session_{user_id}',
                int(config['api_id']),
                config['api_hash']
            )

            await client.connect()
            await client.send_code_request(phone)

            lock = asyncio.Lock()
            self.user_clients[user_id] = (client, lock)

            config['phone'] = phone
            config['current_step'] = 'login_code'
            self.save_configs()

            await event.respond("✅ Code sent!\n\nEnter code:")
        except Exception as e:
            await event.respond(f"❌ Error: {str(e)[:100]}")

    async def process_code(self, event, code):
        user_id = event.sender_id
        config = self.get_user_config(user_id)
        code = code.replace(' ', '')

        try:
            client_data = self.user_clients.get(user_id)
            if not client_data:
                raise Exception("Session expired")

            client, lock = client_data
            async with lock:
                await client.sign_in(code=code)

            config['logged_in'] = True
            config['current_step'] = None
            self.save_configs()

            await event.respond("✅ Login Success!", buttons=[Button.inline("🔙 Menu", b"back")])
        except Exception as e:
            error_msg = str(e)
            if "password" in error_msg.lower():
                config['current_step'] = 'login_2fa'
                self.save_configs()
                await event.respond("🔐 2FA Required\n\nEnter password:")
            else:
                await event.respond(f"❌ Error: {error_msg[:100]}")

    async def process_2fa(self, event, password):
        user_id = event.sender_id
        config = self.get_user_config(user_id)

        try:
            client_data = self.user_clients.get(user_id)
            if not client_data:
                raise Exception("Session expired")

            client, lock = client_data
            async with lock:
                await client.sign_in(password=password)

            config['logged_in'] = True
            config['current_step'] = None
            self.save_configs()

            await event.respond("✅ Login Success!", buttons=[Button.inline("🔙 Menu", b"back")])
        except Exception as e:
            await event.respond(f"❌ Wrong password")

    async def show_chat_selection(self, event, chat_type):
        user_id = event.sender_id
        client_data = await self.ensure_user_client(user_id)

        if not client_data or client_data[0] is None:
            await event.answer("❌ Login first!", alert=True)
            return

        client, lock = client_data

        try:
            await event.edit("⏳ Loading chats...")

            async with lock:
                dialogs = await client.get_dialogs(limit=50)

            buttons = []
            for dialog in dialogs:
                name = dialog.name[:30]
                chat_id = str(dialog.id)
                emoji = "👤" if isinstance(dialog.entity, User) else "📢" if isinstance(dialog.entity, Channel) else "👥"

                if chat_type == 'source':
                    callback = f"select_source_{chat_id}"
                else:
                    callback = f"select_dest_{chat_id}"

                buttons.append([Button.inline(f"{emoji} {name}", callback.encode())])

            buttons.append([Button.inline("🔙 Back", b"back")])

            title = "Source" if chat_type == 'source' else "Destination"
            await event.edit(f"📋 Select {title}:", buttons=buttons)
        except Exception as e:
            print(f"❌ Error: {e}")
            await event.answer(f"Error: {str(e)[:50]}", alert=True)

    async def set_source_chat(self, event, chat_id):
        user_id = event.sender_id
        config = self.get_user_config(user_id)
        client_data = await self.ensure_user_client(user_id)

        if not client_data or client_data[0] is None:
            return

        client, lock = client_data

        try:
            async with lock:
                entity = await client.get_entity(int(chat_id))
            name = getattr(entity, 'title', getattr(entity, 'first_name', 'Unknown'))

            config['source_chat'] = chat_id
            config['source_chat_name'] = name
            self.save_configs()

            await event.edit(
                f"✅ Source Set!\n\n{name}",
                buttons=[Button.inline("🔙 Menu", b"back")]
            )
        except Exception as e:
            await event.answer(f"Error: {str(e)[:50]}", alert=True)

    async def add_destination_chat(self, event, chat_id):
        user_id = event.sender_id
        config = self.get_user_config(user_id)
        client_data = await self.ensure_user_client(user_id)

        if not client_data or client_data[0] is None:
            return

        client, lock = client_data

        try:
            async with lock:
                entity = await client.get_entity(int(chat_id))
            name = getattr(entity, 'title', getattr(entity, 'first_name', 'Unknown'))

            destinations = config.get('destinations', [])
            if any(d['id'] == chat_id for d in destinations):
                await event.answer("Already added!", alert=True)
                return

            destinations.append({'id': chat_id, 'name': name})
            config['destinations'] = destinations
            self.save_configs()

            await event.edit(
                f"✅ Destination Added!\n\n{name}\n\nTotal: {len(destinations)}",
                buttons=[
                    [Button.inline("➕ Add More", b"add_dest")],
                    [Button.inline("🔙 Menu", b"back")]
                ]
            )
        except Exception as e:
            await event.answer(f"Error: {str(e)[:50]}", alert=True)

    async def show_destinations_menu(self, event):
        user_id = event.sender_id
        config = self.get_user_config(user_id)
        destinations = config.get('destinations', [])

        if not destinations:
            await event.edit(
                "📤 No destinations yet!\n\nAdd some:",
                buttons=[
                    [Button.inline("➕ Add Destination", b"add_dest")],
                    [Button.inline("🔙 Back", b"back")]
                ]
            )
            return

        text = f"📤 **Destinations ({len(destinations)}):**\n\n"
        buttons = []

        for dest in destinations:
            text += f"• {dest['name']}\n"
            buttons.append([
                Button.inline(f"🗑️ {dest['name'][:20]}", f"del_dest_{dest['id']}".encode())
            ])

        buttons.append([Button.inline("➕ Add More", b"add_dest")])
        buttons.append([Button.inline("🔙 Back", b"back")])

        await event.edit(text, buttons=buttons)

    async def delete_destination(self, event, dest_id):
        user_id = event.sender_id
        config = self.get_user_config(user_id)

        destinations = config.get('destinations', [])
        config['destinations'] = [d for d in destinations if d['id'] != dest_id]
        self.save_configs()

        await event.answer("✅ Deleted!", alert=True)
        await self.show_destinations_menu(event)

    async def show_filters_menu(self, event):
        user_id = event.sender_id
        filters = self.get_user_filters(user_id)

        text_count = len(filters.get('text_filters', []))
        media_count = len(filters.get('media_filters', []))

        text = f"""
🔧 **Filters**

📝 Text Filters: {text_count}
🖼️ Media Filters: {media_count}
"""

        buttons = [
            [Button.inline("📝 Add Text Filter", b"add_text_filter")],
            [Button.inline("🖼️ Add Media Filter", b"add_media_filter")]
        ]

        if text_count > 0:
            buttons.append([Button.inline("📋 View Text Filters", b"view_text_filters")])
        if media_count > 0:
            buttons.append([Button.inline("🖼️ View Media Filters", b"view_media_filters")])

        buttons.append([Button.inline("🔙 Back", b"back")])

        await event.edit(text, buttons=buttons)

    async def start_add_text_filter(self, event):
        user_id = event.sender_id
        config = self.get_user_config(user_id)
        config['current_step'] = 'text_find'
        self.save_configs()

        await event.edit(
            "📝 **Add Text Filter**\n\n"
            "**Step 1:** Send text to FIND\n\n"
            "Example: old text",
            buttons=[Button.inline("❌ Cancel", b"manage_filters")]
        )

    async def process_text_find(self, event, text):
        user_id = event.sender_id
        config = self.get_user_config(user_id)

        config['temp_find_text'] = text
        config['current_step'] = 'text_replace'
        self.save_configs()

        await event.respond(
            f"✅ Find: {text}\n\n"
            f"**Step 2:** Send REPLACEMENT text\n\n"
            f"Example: new text",
            buttons=[Button.inline("❌ Cancel", b"manage_filters")]
        )

    async def process_text_replace(self, event, text):
        user_id = event.sender_id
        config = self.get_user_config(user_id)
        filters = self.get_user_filters(user_id)

        find_text = config.get('temp_find_text', '')

        if 'text_filters' not in filters:
            filters['text_filters'] = []

        filters['text_filters'].append({
            'find': find_text,
            'replace': text
        })

        self.save_filters()

        config['current_step'] = None
        config['temp_find_text'] = None
        self.save_configs()

        await event.respond(
            f"✅ **Text Filter Added!**\n\n"
            f"Find: {find_text}\n"
            f"Replace: {text}",
            buttons=[
                [Button.inline("➕ Add Another", b"add_text_filter")],
                [Button.inline("🔙 Menu", b"manage_filters")]
            ]
        )

    async def start_add_media_filter(self, event):
        user_id = event.sender_id
        config = self.get_user_config(user_id)
        config['current_step'] = 'media_original'
        self.save_configs()

        await event.edit(
            "🖼️ **Add Media Filter**\n\n"
            "**Step 1:** Send ORIGINAL media\n\n"
            "(Photo/Video/Document to replace)",
            buttons=[Button.inline("❌ Cancel", b"manage_filters")]
        )

    async def process_media_original(self, event):
        user_id = event.sender_id
        config = self.get_user_config(user_id)

        if not event.message.media:
            await event.respond("❌ Send media only!")
            return

        media_id = self.get_media_id(event.message.media)
        if not media_id:
            await event.respond("❌ Could not extract media ID!")
            return

        # Save original media
        filename = f"original_{user_id}_{media_id}_{int(time.time())}"
        filepath = os.path.join(MEDIA_FILES_DIR, filename)
        await event.message.download_media(file=filepath)

        config['temp_original_media_id'] = media_id
        config['temp_original_file'] = filepath
        config['current_step'] = 'media_replace'
        self.save_configs()

        await event.respond(
            f"✅ Original media saved!\n\n"
            f"**Step 2:** Send REPLACEMENT media",
            buttons=[Button.inline("❌ Cancel", b"manage_filters")]
        )

    async def process_media_replace(self, event):
        user_id = event.sender_id
        config = self.get_user_config(user_id)
        filters = self.get_user_filters(user_id)

        if not event.message.media:
            await event.respond("❌ Send media only!")
            return

        original_media_id = config.get('temp_original_media_id')
        original_file = config.get('temp_original_file')

        if not original_media_id or not original_file:
            await event.respond("❌ Original media not found!")
            return

        # Save replacement media
        filename = f"replace_{user_id}_{original_media_id}_{int(time.time())}"
        filepath = os.path.join(MEDIA_FILES_DIR, filename)
        await event.message.download_media(file=filepath)

        if 'media_filters' not in filters:
            filters['media_filters'] = []

        filters['media_filters'].append({
            'original_id': original_media_id,
            'original_file': original_file,
            'replace_file': filepath
        })

        self.save_filters()

        config['current_step'] = None
        config['temp_original_media_id'] = None
        config['temp_original_file'] = None
        self.save_configs()

        await event.respond(
            f"✅ **Media Filter Added!**\n\n"
            f"Original ID: {original_media_id[:20]}...",
            buttons=[
                [Button.inline("➕ Add Another", b"add_media_filter")],
                [Button.inline("🔙 Menu", b"manage_filters")]
            ]
        )

    async def show_text_filters(self, event):
        user_id = event.sender_id
        filters = self.get_user_filters(user_id)
        text_filters = filters.get('text_filters', [])

        if not text_filters:
            await event.answer("No text filters!", alert=True)
            return

        text = f"📝 **Text Filters ({len(text_filters)}):**\n\n"
        buttons = []

        for idx, f in enumerate(text_filters):
            text += f"{idx+1}. {f['find']} → {f['replace']}\n"
            buttons.append([Button.inline(f"🗑️ Delete #{idx+1}", f"del_text_{idx}".encode())])

        buttons.append([Button.inline("🔙 Back", b"manage_filters")])

        await event.edit(text, buttons=buttons)

    async def show_media_filters(self, event):
        user_id = event.sender_id
        filters = self.get_user_filters(user_id)
        media_filters = filters.get('media_filters', [])

        if not media_filters:
            await event.answer("No media filters!", alert=True)
            return

        text = f"🖼️ **Media Filters ({len(media_filters)}):**\n\n"
        buttons = []

        for idx, f in enumerate(media_filters):
            text += f"{idx+1}. ID: {f['original_id'][:20]}...\n"
            buttons.append([Button.inline(f"🗑️ Delete #{idx+1}", f"del_media_{idx}".encode())])

        buttons.append([Button.inline("🔙 Back", b"manage_filters")])

        await event.edit(text, buttons=buttons)

    async def delete_text_filter(self, event, idx):
        user_id = event.sender_id
        filters = self.get_user_filters(user_id)

        text_filters = filters.get('text_filters', [])
        if idx < len(text_filters):
            del text_filters[idx]
            filters['text_filters'] = text_filters
            self.save_filters()

        await event.answer("✅ Deleted!", alert=True)
        await self.show_text_filters(event)

    async def delete_media_filter(self, event, idx):
        user_id = event.sender_id
        filters = self.get_user_filters(user_id)

        media_filters = filters.get('media_filters', [])
        if idx < len(media_filters):
            del media_filters[idx]
            filters['media_filters'] = media_filters
            self.save_filters()

        await event.answer("✅ Deleted!", alert=True)
        await self.show_media_filters(event)

    def apply_text_filters(self, text, text_filters):
        """Apply text filters preserving formatting"""
        if not text or not text_filters:
            return text

        result = text
        for f in text_filters:
            result = result.replace(f['find'], f['replace'])
        return result

    def find_media_filter(self, media_id, media_filters):
        """Find matching media filter"""
        if not media_id or not media_filters:
            return None

        for f in media_filters:
            if f['original_id'] == media_id:
                return f
        return None

    async def start_forwarding(self, event):
        user_id = event.sender_id
        config = self.get_user_config(user_id)

        if config.get('forwarding_active'):
            await event.answer("Already running!", alert=True)
            return

        config['forwarding_active'] = True
        self.save_configs()

        task_key = f"{user_id}"
        task = asyncio.create_task(self.forward_messages(user_id))
        self.forwarding_tasks[task_key] = task

        await event.edit(
            "✅ Forwarding Started!",
            buttons=[Button.inline("🔙 Menu", b"back")]
        )

    async def stop_forwarding(self, event):
        user_id = event.sender_id
        config = self.get_user_config(user_id)

        config['forwarding_active'] = False
        self.save_configs()

        task_key = f"{user_id}"
        if task_key in self.forwarding_tasks:
            task = self.forwarding_tasks[task_key]
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            if task_key in self.forwarding_tasks:
                del self.forwarding_tasks[task_key]

        await event.edit(
            "⏸️ Forwarding Stopped!",
            buttons=[Button.inline("🔙 Menu", b"back")]
        )

    async def restart_forwarding(self, event):
        user_id = event.sender_id

        await event.edit("🔄 Restarting...")

        # Stop
        config = self.get_user_config(user_id)
        config['forwarding_active'] = False
        self.save_configs()

        task_key = f"{user_id}"
        if task_key in self.forwarding_tasks:
            task = self.forwarding_tasks[task_key]
            if not task.done():
                task.cancel()
                try:
                    await task
                except:
                    pass
            if task_key in self.forwarding_tasks:
                del self.forwarding_tasks[task_key]

        # Reconnect
        if user_id in self.user_clients:
            client_data = self.user_clients[user_id]
            if client_data:
                client, lock = client_data
                try:
                    async with lock:
                        if client.is_connected():
                            await client.disconnect()
                except:
                    pass
            del self.user_clients[user_id]

        await asyncio.sleep(2)
        await self.ensure_user_client(user_id)
        await asyncio.sleep(1)

        # Start
        config['forwarding_active'] = True
        self.save_configs()

        task = asyncio.create_task(self.forward_messages(user_id))
        self.forwarding_tasks[task_key] = task

        await event.edit(
            "✅ Restart Complete!",
            buttons=[Button.inline("🔙 Menu", b"back")]
        )

    async def toggle_forward_tag(self, event):
        user_id = event.sender_id
        config = self.get_user_config(user_id)
        config['forward_with_tag'] = not config.get('forward_with_tag', False)
        self.save_configs()
        await event.answer(f"Forward tag {'ON' if config['forward_with_tag'] else 'OFF'}", alert=True)
        await self.show_main_menu(event)

    async def forward_messages(self, user_id):
        """Main forwarding loop with filters and album support"""
        print(f"🚀 Starting forwarding for {user_id}")
        handlers_registered = False
        
        while self.running:
            try:
                config = self.get_user_config(user_id)
                
                if not config.get('forwarding_active'):
                    break
                
                client_data = await self.ensure_user_client(user_id)
                if not client_data or client_data[0] is None:
                    await asyncio.sleep(10)
                    continue
                
                client, lock = client_data
                source_id = int(config['source_chat'])
                destinations = config.get('destinations', [])
                dest_ids = [int(d['id']) for d in destinations]
                
                # Attach delete/edit listeners once per client
                user_map = self.get_user_map(user_id)
                if not handlers_registered:
                    async def delete_handler(ev):
                        try:
                            for src_id in ev.deleted_ids:
                                key = str(src_id)
                                if key in user_map:
                                    for mapping in user_map[key]:
                                        try:
                                            await client.delete_messages(mapping['dest'], mapping['msg_id'])
                                        except Exception as de:
                                            print(f"❌ Delete mirror error: {de}")
                                    del user_map[key]
                            self.save_maps()
                        except Exception as e:
                            print(f"❌ Delete handler error: {e}")

                    async def edit_handler(ev):
                        try:
                            msg = ev.message
                            key = str(msg.id)
                            if key not in user_map:
                                return
                            text = msg.message or None
                            for mapping in user_map[key]:
                                try:
                                    await client.edit_message(mapping['dest'], mapping['msg_id'], text)
                                except Exception as ee:
                                    print(f"❌ Edit mirror error: {ee}")
                        except Exception as e:
                            print(f"❌ Edit handler error: {e}")

                    client.add_event_handler(delete_handler, events.MessageDeleted(chats=source_id))
                    client.add_event_handler(edit_handler, events.MessageEdited(chats=source_id))
                    handlers_registered = True
                
                # Get filters
                filters = self.get_user_filters(user_id)
                text_filters = filters.get('text_filters', [])
                media_filters = filters.get('media_filters', [])
                
                print(f"✅ Forwarding: {source_id} → {dest_ids}")

                # Album tracking
                grouped_messages = {}
                group_timers = {}

                async def send_album(grouped_id):
                    """Send album with filters"""
                    if grouped_id not in grouped_messages:
                        return

                    messages = grouped_messages[grouped_id]
                    if not messages:
                        return

                    print(f"📸 Sending album ({len(messages)} items)")

                    async with lock:
                        for dest_id in dest_ids:
                            try:
                                if config.get('forward_with_tag'):
                                    ids = [m.id for m in messages]
                                    sent = await client.forward_messages(dest_id, ids, source_id)
                                    # map source grouped to dest msgs
                                    user_map = self.get_user_map(user_id)
                                    for src_id, smsg in zip(ids, sent):
                                        user_map[str(src_id)] = user_map.get(str(src_id), []) + [{'dest': dest_id, 'msg_id': smsg.id}]
                                    self.save_maps()
                                else:
                                    media_list = [msg.media for msg in messages]
                                    caption = messages[0].message if messages[0].message else None

                                    # Apply text filters to caption
                                    if caption and text_filters:
                                        caption = self.apply_text_filters(caption, text_filters)

                                    sent = await client.send_file(
                                        dest_id,
                                        file=media_list,
                                        caption=caption
                                    )
                                    # sent is a message or list; map first msg id to grouped id
                                    if sent:
                                        s_list = sent if isinstance(sent, list) else [sent]
                                        user_map = self.get_user_map(user_id)
                                        for src_msg in messages:
                                            # map each source to first dest id (best effort)
                                            user_map[str(src_msg.id)] = user_map.get(str(src_msg.id), []) + [{'dest': dest_id, 'msg_id': s_list[0].id}]
                                        self.save_maps()
                            except Exception as e:
                                print(f"❌ Album error: {e}")

                    del grouped_messages[grouped_id]
                    if grouped_id in group_timers:
                        del group_timers[grouped_id]

                @client.on(events.NewMessage(chats=source_id))
                async def handler(event):
                    try:
                        msg = event.message

                        # Check if part of album
                        if hasattr(msg, 'grouped_id') and msg.grouped_id:
                            grouped_id = msg.grouped_id

                            if grouped_id not in grouped_messages:
                                grouped_messages[grouped_id] = []
                            grouped_messages[grouped_id].append(msg)

                            if grouped_id in group_timers:
                                group_timers[grouped_id].cancel()

                            async def timer_callback():
                                await asyncio.sleep(1)
                                await send_album(grouped_id)

                            group_timers[grouped_id] = asyncio.create_task(timer_callback())
                        else:
                            # Single message
                            async with lock:
                                for dest_id in dest_ids:
                                    try:
                                        if config.get('forward_with_tag'):
                                            sent = await client.forward_messages(dest_id, msg.id, source_id)
                                            if sent:
                                                user_map = self.get_user_map(user_id)
                                                user_map[str(msg.id)] = user_map.get(str(msg.id), []) + [{'dest': dest_id, 'msg_id': sent.id if not isinstance(sent, list) else sent[0].id}]
                                                self.save_maps()
                                            continue

                                        if msg.media:
                                            # Check media filter
                                            media_id = self.get_media_id(msg.media)
                                            media_filter = self.find_media_filter(media_id, media_filters)

                                            caption = msg.message if msg.message else None
                                            if caption and text_filters:
                                                caption = self.apply_text_filters(caption, text_filters)

                                            if media_filter:
                                                # Use replacement media
                                                sent = await client.send_file(
                                                    dest_id,
                                                    file=media_filter['replace_file'],
                                                    caption=caption
                                                )
                                            else:
                                                # Use original media
                                                sent = await client.send_file(
                                                    dest_id,
                                                    file=msg.media,
                                                    caption=caption
                                                )
                                        else:
                                            # Text only
                                            text = msg.message
                                            if text and text_filters:
                                                text = self.apply_text_filters(text, text_filters)
                                            sent = await client.send_message(dest_id, text)
                                        if sent:
                                            user_map = self.get_user_map(user_id)
                                            user_map[str(msg.id)] = user_map.get(str(msg.id), []) + [{'dest': dest_id, 'msg_id': sent.id if not isinstance(sent, list) else sent[0].id}]
                                            self.save_maps()
                                    except Exception as e:
                                        print(f"❌ Send error: {e}")
                    except Exception as e:
                        print(f"❌ Handler error: {e}")

                print("✅ Handler registered")

                # Keep alive
                while self.running and config.get('forwarding_active'):
                    await asyncio.sleep(5)
                    config = self.get_user_config(user_id)

                # Cleanup
                try:
                    client.remove_event_handler(handler)
                except:
                    pass
                print(f"🛑 Stopped forwarding for {user_id}")
                break

            except Exception as e:
                print(f"❌ Forwarding error: {e}")
                traceback.print_exc()
                await asyncio.sleep(10)

        # Final cleanup
        task_key = f"{user_id}"
        if task_key in self.forwarding_tasks:
            try:
                del self.forwarding_tasks[task_key]
            except:
                pass
        print(f"✅ Task finished for {user_id}")

    async def logout_user(self, event):
        user_id = event.sender_id
        config = self.get_user_config(user_id)

        # Stop forwarding
        config['forwarding_active'] = False
        self.save_configs()

        task_key = f"{user_id}"
        if task_key in self.forwarding_tasks:
            task = self.forwarding_tasks[task_key]
            if not task.done():
                task.cancel()
            if task_key in self.forwarding_tasks:
                try:
                    del self.forwarding_tasks[task_key]
                except:
                    pass

        # Disconnect
        if user_id in self.user_clients:
            client_data = self.user_clients[user_id]
            if client_data:
                client, lock = client_data
                try:
                    async with lock:
                        if client.is_connected():
                            await client.disconnect()
                except:
                    pass
            try:
                del self.user_clients[user_id]
            except:
                pass

        # Clear config
        config['logged_in'] = False
        config['phone'] = None
        config['source_chat'] = None
        config['source_chat_name'] = 'Not Set'
        config['destinations'] = []
        config['forwarding_active'] = False
        self.save_configs()

        await event.edit(
            "✅ Logged Out!",
            buttons=[Button.inline("🔙 Menu", b"back")]
        )

    async def show_help(self, event):
        help_text = """
ℹ️ **Complete Forwarder Bot**

**Features:**
✅ Auto API - Just phone number
✅ One source → Many destinations
✅ Text filters - Replace text
✅ Media filters - Replace media
✅ Albums preserved
✅ Ultra stable

**Setup:**
1. Login with phone
2. Set source chat
3. Add destinations
4. Add filters (optional)
5. Start forwarding

**Commands:**
/start - Main menu
"""
        await event.edit(help_text, buttons=[Button.inline("🔙 Back", b"back")])

if __name__ == '__main__':
    print("=" * 60)
    print("🚀 COMPLETE FORWARDER BOT")
    print("=" * 60)
    print("✨ Auto API")
    print("✨ Multiple Destinations")
    print("✨ Text & Media Filters")
    print("✨ Album Support")
    print("\n✅ Starting...\n")

    bot = CompleteBot()
    try:
        asyncio.run(bot.start())
    except KeyboardInterrupt:
        print("\n🛑 Stopped")