import os
import asyncio
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import User, Channel
import httpx
from dotenv import load_dotenv

load_dotenv()

API_ID = int(os.getenv("TELEGRAM_API_ID"))
API_HASH = os.getenv("TELEGRAM_API_HASH")
PHONE = os.getenv("TELEGRAM_PHONE")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "mistralai/mistral-7b-instruct:free")
MESSAGES_PER_CHAT = int(os.getenv("MESSAGES_PER_CHAT", "30"))
MAX_CHATS = int(os.getenv("MAX_CHATS", "10"))

SESSION_STRING = os.getenv("TELEGRAM_SESSION", "")
client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)


async def call_openrouter(prompt: str) -> str:
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/telegram-userbot-summariser",
        "X-Title": "Telegram Userbot Summariser",
    }
    payload = {
        "model": OPENROUTER_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 512,
    }
    async with httpx.AsyncClient(timeout=60) as http:
        r = await http.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json=payload,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()


async def summarise_chat(chat_name: str, messages: list) -> str:
    if not messages:
        return None
    conversation = "\n".join(messages)
    prompt = (
        f"Summarise this Telegram conversation from '{chat_name}' in 2-4 sentences. "
        f"Focus on key topics, decisions, or anything needing attention. Be concise.\n\n"
        f"{conversation}"
    )
    try:
        return await call_openrouter(prompt)
    except Exception as e:
        return f"Error: {e}"


@client.on(events.NewMessage(pattern=r"\.summarise(all)?(\s+\d+)?", outgoing=True))
async def handle_summarise(event):
    await event.delete()
    args = event.pattern_match.group(2)
    limit = int(args.strip()) if args else MAX_CHATS
    status = await client.send_message("me", f"Fetching your last {limit} active chats...")
    results = []
    count = 0

    async for dialog in client.iter_dialogs():
        if count >= limit:
            break
        if dialog.archived:
            continue
        entity = dialog.entity
        chat_name = dialog.name or "Unknown"
        messages = []

        async for msg in client.iter_messages(entity, limit=MESSAGES_PER_CHAT):
            if msg.text:
                try:
                    if isinstance(entity, User):
                        sender = chat_name
                    else:
                        sender_entity = await msg.get_sender()
                        sender = getattr(sender_entity, "first_name", None) or "Someone"
                except:
                    sender = "Someone"
                messages.append(f"{sender}: {msg.text}")

        if not messages:
            continue

        messages.reverse()
        summary = await summarise_chat(chat_name, messages)
        if summary:
            icon = "👤" if isinstance(entity, User) else "👥"
            unread = f" • {dialog.unread_count} unread" if dialog.unread_count > 0 else ""
            results.append(f"{icon} {chat_name}{unread}\n{summary}")
            count += 1

    if not results:
        await status.edit("No messages found.")
        return

    await status.delete()
    header = f"Summary of {count} chats\n\n"
    body = "\n\n".join(results)
    full = header + body
    for chunk in [full[i:i+4000] for i in range(0, len(full), 4000)]:
        await client.send_message("me", chunk)


@client.on(events.NewMessage(pattern=r"\.links ?(.+)?", outgoing=True))
async def handle_links(event):
    """
    .links          — scrape links from all groups you're in
    .links GroupName — scrape links from a specific group
    """
    import re
    await event.delete()
    query = (event.pattern_match.group(1) or "").strip()
    URL_REGEX = re.compile(r'(https?://\S+|t\.me/\S+)', re.IGNORECASE)
    LINKS_PER_GROUP = 50

    status = await client.send_message("me", "🔍 Scraping links...")
    all_links = []

    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        # Only groups/channels, or filter by name if query given
        if isinstance(entity, User):
            continue
        if query and query.lower() not in dialog.name.lower():
            continue

        group_links = []
        async for msg in client.iter_messages(entity, limit=200):
            if not msg.text:
                continue
            found = URL_REGEX.findall(msg.text)
            for link in found:
                group_links.append(link)
            if len(group_links) >= LINKS_PER_GROUP:
                break

        if group_links:
            # Deduplicate
            unique = list(dict.fromkeys(group_links))
            all_links.append(f"📌 {dialog.name} ({len(unique)} links)\n" + "\n".join(unique))

        if query and all_links:
            break  # Found the specific group, stop

    if not all_links:
        await status.edit("📭 No links found.")
        return

    await status.delete()
    header = f"🔗 Links scraped from {'all groups' if not query else query}\n{'─'*30}\n\n"
    body = "\n\n".join(all_links)
    full = header + body
    for chunk in [full[i:i+4000] for i in range(0, len(full), 4000)]:
        await client.send_message("me", chunk)


@client.on(events.NewMessage(pattern=r"\.sum (.+)", outgoing=True))
async def handle_single(event):
    await event.delete()
    query = event.pattern_match.group(1).strip()
    status = await client.send_message("me", f"Looking for '{query}'...")
    async for dialog in client.iter_dialogs():
        if query.lower() in dialog.name.lower():
            messages = []
            async for msg in client.iter_messages(dialog.entity, limit=MESSAGES_PER_CHAT):
                if msg.text:
                    try:
                        sender_entity = await msg.get_sender()
                        sender = getattr(sender_entity, "first_name", None) or dialog.name
                    except:
                        sender = "Someone"
                    messages.append(f"{sender}: {msg.text}")
            if not messages:
                await status.edit(f"No messages in '{dialog.name}'.")
                return
            messages.reverse()
            summary = await summarise_chat(dialog.name, messages)
            await status.edit(f"{dialog.name}\n\n{summary}")
            return
    await status.edit(f"No chat found matching '{query}'")


# ─── FOLDER SUMMARISE ───────────────────────────────────────────────
# Usage: .folder FolderName
# Summarises all chats inside a Telegram folder (saved filter)
@client.on(events.NewMessage(pattern=r"\.folder ?(.+)?", outgoing=True))
async def handle_folder(event):
    await event.delete()
    query = (event.pattern_match.group(1) or "").strip().lower()

    from telethon.tl.functions.messages import GetDialogFiltersRequest
    filters = await client(GetDialogFiltersRequest())

    # List folders if no name given
    if not query:
        names = []
        for f in filters.filters:
            title = getattr(f, "title", None)
            if title:
                t = title if isinstance(title, str) else getattr(title, "text", str(title))
                names.append(t)
        if names:
            await client.send_message("me", "📁 Your folders:\n" + "\n".join(f"• {n}" for n in names))
        else:
            await client.send_message("me", "No folders found.")
        return

    # Find matching folder
    target_filter = None
    for f in filters.filters:
        title = getattr(f, "title", None)
        if not title:
            continue
        t = title if isinstance(title, str) else getattr(title, "text", str(title))
        if query in t.lower():
            target_filter = f
            break

    if not target_filter:
        await client.send_message("me", f"❌ No folder matching '{query}' found.\nType .folder to list all folders.")
        return

    status = await client.send_message("me", f"📁 Summarising folder '{query}'...")
    results = []

    # Get peers from folder
    peers = getattr(target_filter, "include_peers", [])
    if not peers:
        await status.edit("📭 This folder has no chats.")
        return

    for peer in peers:
        try:
            entity = await client.get_entity(peer)
            chat_name = getattr(entity, "title", None) or getattr(entity, "first_name", "Unknown")
            messages = []
            async for msg in client.iter_messages(entity, limit=MESSAGES_PER_CHAT):
                if msg.text:
                    try:
                        sender_entity = await msg.get_sender()
                        sender = getattr(sender_entity, "first_name", None) or chat_name
                    except:
                        sender = "Someone"
                    messages.append(f"{sender}: {msg.text}")
            if not messages:
                continue
            messages.reverse()
            summary = await summarise_chat(chat_name, messages)
            if summary:
                results.append(f"💬 {chat_name}\n{summary}")
        except Exception as e:
            continue

    if not results:
        await status.edit("📭 No messages found in this folder.")
        return

    await status.delete()
    header = f"📁 Folder: {query}\n{'─'*30}\n\n"
    body = "\n\n".join(results)
    full = header + body
    for chunk in [full[i:i+4000] for i in range(0, len(full), 4000)]:
        await client.send_message("me", chunk)


# ─── AUTO REPLY ─────────────────────────────────────────────────────
# .autoreply on I'm busy, will reply soon!  — turn on auto reply
# .autoreply off                             — turn off auto reply
autoreply_store = {"active": False, "message": "", "replied": set()}

@client.on(events.NewMessage(pattern=r"\.autoreply (on|off)(?: (.+))?", outgoing=True))
async def handle_autoreply_toggle(event):
    await event.delete()
    mode = event.pattern_match.group(1)
    msg = (event.pattern_match.group(2) or "").strip()

    if mode == "on":
        autoreply_store["active"] = True
        autoreply_store["message"] = msg or "Hey! I'm busy right now, will reply soon 👍"
        autoreply_store["replied"].clear()
        await client.send_message("me", f"✅ Auto-reply ON\nMessage: {autoreply_store['message']}")
    else:
        autoreply_store["active"] = False
        autoreply_store["replied"].clear()
        await client.send_message("me", "🔕 Auto-reply OFF")

@client.on(events.NewMessage(incoming=True))
async def handle_incoming_autoreply(event):
    if not autoreply_store["active"]:
        return
    # Only reply to private DMs
    if not event.is_private:
        return
    sender_id = event.sender_id
    if sender_id in autoreply_store["replied"]:
        return  # Don't spam same person
    autoreply_store["replied"].add(sender_id)
    await event.reply(autoreply_store["message"])


# ─── BROADCAST ──────────────────────────────────────────────────────
# .broadcast Hello everyone!  — sends a message to all your DM contacts
# ⚠️ Use sparingly — max 10 people to avoid ban
@client.on(events.NewMessage(pattern=r"\.broadcast (.+)", outgoing=True))
async def handle_broadcast(event):
    await event.delete()
    msg = event.pattern_match.group(1).strip()
    MAX_BROADCAST = 10  # Safety limit

    status = await client.send_message("me", f"📢 Broadcasting to up to {MAX_BROADCAST} contacts...")
    sent_count = 0

    async for dialog in client.iter_dialogs():
        if sent_count >= MAX_BROADCAST:
            break
        if not isinstance(dialog.entity, User):
            continue
        if dialog.entity.bot or dialog.entity.is_self:
            continue
        try:
            await client.send_message(dialog.entity, msg)
            sent_count += 1
            await asyncio.sleep(2)  # Delay to avoid spam detection
        except Exception:
            continue

    await status.edit(f"✅ Broadcast sent to {sent_count} people.")


async def main():
    print("Starting userbot...")
    await client.start()

    if not await client.is_user_authorized():
        print("❌ Session invalid! Generate a new session string.")
        return

    me = await client.get_me()
    print(f"✅ Logged in as {me.first_name} (@{me.username})")
    print("✅ Userbot running! Type .summarise in any Telegram chat.")
    await client.run_until_disconnected()


asyncio.run(main())
