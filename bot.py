import os
import asyncio
import re
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import User, Channel
import httpx
from dotenv import load_dotenv

load_dotenv()

API_ID = int(os.getenv("TELEGRAM_API_ID"))
API_HASH = os.getenv("TELEGRAM_API_HASH")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "mistralai/mistral-7b-instruct:free")
MESSAGES_PER_CHAT = int(os.getenv("MESSAGES_PER_CHAT", "30"))
MAX_CHATS = int(os.getenv("MAX_CHATS", "10"))
SESSION_STRING = os.getenv("TELEGRAM_SESSION", "")

client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

# ─── STORES ─────────────────────────────────────────────────────────
ai_whitelist = set()
ai_reply_active = {"active": False}
autoreply_store = {"active": False, "message": "", "replied": set()}

AI_PERSONA = """You are replying on behalf of the user in a Telegram chat.
Rules:
- Be casual and friendly, plain English
- Use short forms like: irl, imo, ig, obv, fs, ngl, rn, lol, rly, tbh
- Keep replies SHORT — 1 to 3 sentences max
- Sound natural and human, never robotic or formal
- Read the conversation context and reply accordingly
- Never reveal you are an AI
- If you don't know something personal, give a vague natural response"""


# ─── OPENROUTER ─────────────────────────────────────────────────────
async def call_openrouter(prompt: str) -> str:
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/telegram-userbot",
        "X-Title": "Telegram Userbot",
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


# ─── HELP ───────────────────────────────────────────────────────────
@client.on(events.NewMessage(pattern=r"\.help", outgoing=True))
async def handle_help(event):
    await event.delete()
    help_text = """🤖 **Userbot Commands**

📋 **SUMMARISE**
`.summarise` — Summarise top 10 chats
`.summarise 20` — Summarise top N chats
`.sum John` — Summarise a specific chat
`.folder` — List your Telegram folders
`.folder Work` — Summarise a folder

🔗 **LINKS**
`.links` — Scrape links from all groups
`.links GroupName` — Scrape from one group

🤖 **AI AUTO-REPLY**
`.aiwhitelist add Name` — Add to AI reply list
`.aiwhitelist remove Name` — Remove from list
`.aiwhitelist list` — See the list
`.aireply on` — Turn on AI replies
`.aireply off` — Turn off AI replies

💬 **SIMPLE AUTO-REPLY**
`.autoreply on I'm busy!` — Auto-reply to all DMs
`.autoreply off` — Turn off

📢 **BROADCAST**
`.broadcast Hello!` — Send to 10 contacts ⚠️

Type any command in any Telegram chat!"""
    await client.send_message("me", help_text)


# ─── SUMMARISE ──────────────────────────────────────────────────────
@client.on(events.NewMessage(pattern=r"\.summarise(all)?(\s+\d+)?", outgoing=True))
async def handle_summarise(event):
    await event.delete()
    args = event.pattern_match.group(2)
    limit = int(args.strip()) if args else MAX_CHATS
    status = await client.send_message("me", f"⏳ Fetching your last {limit} active chats...")
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
        await status.edit("📭 No messages found.")
        return

    await status.delete()
    header = f"📋 Summary of {count} chats\n\n"
    body = "\n\n".join(results)
    full = header + body
    for chunk in [full[i:i+4000] for i in range(0, len(full), 4000)]:
        await client.send_message("me", chunk)


# ─── SUMMARISE ONE CHAT ─────────────────────────────────────────────
@client.on(events.NewMessage(pattern=r"\.sum (.+)", outgoing=True))
async def handle_single(event):
    await event.delete()
    query = event.pattern_match.group(1).strip()
    status = await client.send_message("me", f"🔍 Looking for '{query}'...")
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
                await status.edit(f"📭 No messages in '{dialog.name}'.")
                return
            messages.reverse()
            summary = await summarise_chat(dialog.name, messages)
            await status.edit(f"📋 {dialog.name}\n\n{summary}")
            return
    await status.edit(f"❌ No chat found matching '{query}'")


# ─── FOLDER SUMMARISE ───────────────────────────────────────────────
@client.on(events.NewMessage(pattern=r"\.folder ?(.+)?", outgoing=True))
async def handle_folder(event):
    await event.delete()
    query = (event.pattern_match.group(1) or "").strip().lower()

    from telethon.tl.functions.messages import GetDialogFiltersRequest
    filters = await client(GetDialogFiltersRequest())

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
        await client.send_message("me", f"❌ No folder matching '{query}'.\nType .folder to list all.")
        return

    status = await client.send_message("me", f"📁 Summarising folder '{query}'...")
    results = []
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
        except:
            continue

    if not results:
        await status.edit("📭 No messages found in this folder.")
        return

    await status.delete()
    header = f"📁 Folder: {query}\n\n"
    body = "\n\n".join(results)
    full = header + body
    for chunk in [full[i:i+4000] for i in range(0, len(full), 4000)]:
        await client.send_message("me", chunk)


# ─── LINKS SCRAPER ──────────────────────────────────────────────────
@client.on(events.NewMessage(pattern=r"\.links ?(.+)?", outgoing=True))
async def handle_links(event):
    await event.delete()
    query = (event.pattern_match.group(1) or "").strip()
    URL_REGEX = re.compile(r'(https?://\S+|t\.me/\S+)', re.IGNORECASE)
    status = await client.send_message("me", "🔍 Scraping links...")
    all_links = []

    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        if isinstance(entity, User):
            continue
        if query and query.lower() not in dialog.name.lower():
            continue

        group_links = []
        async for msg in client.iter_messages(entity, limit=200):
            if not msg.text:
                continue
            found = URL_REGEX.findall(msg.text)
            group_links.extend(found)
            if len(group_links) >= 50:
                break

        if group_links:
            unique = list(dict.fromkeys(group_links))
            all_links.append(f"📌 {dialog.name} ({len(unique)} links)\n" + "\n".join(unique))

        if query and all_links:
            break

    if not all_links:
        await status.edit("📭 No links found.")
        return

    await status.delete()
    header = f"🔗 Links from {'all groups' if not query else query}\n\n"
    body = "\n\n".join(all_links)
    full = header + body
    for chunk in [full[i:i+4000] for i in range(0, len(full), 4000)]:
        await client.send_message("me", chunk)


# ─── AI WHITELIST ───────────────────────────────────────────────────
@client.on(events.NewMessage(pattern=r"\.aiwhitelist (add|remove|list)(?: (.+))?", outgoing=True))
async def handle_aiwhitelist(event):
    await event.delete()
    action = event.pattern_match.group(1)
    name = (event.pattern_match.group(2) or "").strip().lower()

    if action == "add" and name:
        ai_whitelist.add(name)
        await client.send_message("me", f"✅ Added {name} to AI reply list.\nList: {', '.join(ai_whitelist)}")
    elif action == "remove" and name:
        ai_whitelist.discard(name)
        await client.send_message("me", f"🗑️ Removed {name}.\nList: {', '.join(ai_whitelist) or 'empty'}")
    elif action == "list":
        if ai_whitelist:
            await client.send_message("me", "📋 AI whitelist:\n" + "\n".join(f"• {n}" for n in ai_whitelist))
        else:
            await client.send_message("me", "📋 AI whitelist is empty.")


@client.on(events.NewMessage(pattern=r"\.aireply (on|off)", outgoing=True))
async def handle_aireply_toggle(event):
    await event.delete()
    mode = event.pattern_match.group(1)
    ai_reply_active["active"] = mode == "on"
    status = "✅ ON" if ai_reply_active["active"] else "🔕 OFF"
    await client.send_message("me", f"🤖 AI auto-reply: {status}\nWhitelist: {', '.join(ai_whitelist) or 'empty'}")


@client.on(events.NewMessage(incoming=True))
async def handle_ai_autoreply(event):
    if not ai_reply_active["active"]:
        return
    if not event.is_private:
        return
    if not ai_whitelist:
        return
    try:
        sender = await event.get_sender()
        if not sender:
            return
        sender_name = (getattr(sender, "first_name", "") or "").lower()
        sender_username = (getattr(sender, "username", "") or "").lower()
        matched = any(w in sender_name or w in sender_username for w in ai_whitelist)
        if not matched:
            return

        history = []
        async for msg in client.iter_messages(event.chat_id, limit=10):
            if msg.text:
                who = "Me" if msg.out else sender_name.title()
                history.append(f"{who}: {msg.text}")
        history.reverse()
        context = "\n".join(history)

        prompt = f"""{AI_PERSONA}

Conversation history:
{context}

New message from {sender_name.title()}: {event.text}

Reply naturally as the user (1-3 sentences max):"""

        reply = await call_openrouter(prompt)
        await event.reply(reply)
    except Exception as e:
        print(f"AI reply error: {e}")


# ─── SIMPLE AUTO REPLY ──────────────────────────────────────────────
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
    if not event.is_private:
        return
    sender_id = event.sender_id
    if sender_id in autoreply_store["replied"]:
        return
    autoreply_store["replied"].add(sender_id)
    await event.reply(autoreply_store["message"])


# ─── BROADCAST ──────────────────────────────────────────────────────
@client.on(events.NewMessage(pattern=r"\.broadcast (.+)", outgoing=True))
async def handle_broadcast(event):
    await event.delete()
    msg = event.pattern_match.group(1).strip()
    status = await client.send_message("me", "📢 Broadcasting to up to 10 contacts...")
    sent_count = 0
    async for dialog in client.iter_dialogs():
        if sent_count >= 10:
            break
        if not isinstance(dialog.entity, User):
            continue
        if dialog.entity.bot or dialog.entity.is_self:
            continue
        try:
            await client.send_message(dialog.entity, msg)
            sent_count += 1
            await asyncio.sleep(2)
        except:
            continue
    await status.edit(f"✅ Broadcast sent to {sent_count} people.")


# ─── MAIN ───────────────────────────────────────────────────────────
async def main():
    print("Starting userbot...")
    await client.start()

    if not await client.is_user_authorized():
        print("❌ Session invalid! Generate a new session string.")
        return

    me = await client.get_me()
    print(f"✅ Logged in as {me.first_name} (@{me.username})")
    print("✅ Userbot running! Type .help in any chat.")
    await client.run_until_disconnected()


asyncio.run(main())
