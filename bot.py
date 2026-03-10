import os
import asyncio
import re
import json
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import User, Channel
import httpx
from dotenv import load_dotenv
from aiohttp import web

load_dotenv()

API_ID = int(os.getenv("TELEGRAM_API_ID"))
API_HASH = os.getenv("TELEGRAM_API_HASH")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "arcee-ai/arcee-fusion")
MESSAGES_PER_CHAT = int(os.getenv("MESSAGES_PER_CHAT", "30"))
MAX_CHATS = int(os.getenv("MAX_CHATS", "10"))
SESSION_STRING = os.getenv("TELEGRAM_SESSION", "")
PORT = int(os.getenv("PORT", "8080"))

client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

# ─── GLOBAL STATE ────────────────────────────────────────────────────
state = {
    "ai_reply": True,
    "autoreply": {"active": False, "message": ""},
    "autoreply_replied": set(),
    "scrape_groups": [],   # saved group names for .scrape
    "me": None,
    "status_log": [],      # last 20 status messages
}

LINK_FILTERS = {
    "mega":      r'https?://mega\.nz/\S+',
    "drive":     r'https?://drive\.google\.com/\S+',
    "gdrive":    r'https?://drive\.google\.com/\S+',
    "youtube":   r'https?://(?:www\.)?youtu(?:be\.com|\.be)/\S+',
    "instagram": r'https?://(?:www\.)?instagram\.com/\S+',
    "twitter":   r'https?://(?:twitter|x)\.com/\S+',
    "telegram":  r't\.me/\S+',
    "github":    r'https?://github\.com/\S+',
    "all":       r'https?://\S+|t\.me/\S+',
}

def log(msg):
    print(msg)
    state["status_log"].append(msg)
    if len(state["status_log"]) > 50:
        state["status_log"].pop(0)


# ─── OPENROUTER ──────────────────────────────────────────────────────
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
        r = await http.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()


async def summarise_chat(chat_name, messages):
    if not messages:
        return None
    conversation = "\n".join(messages)
    prompt = (
        f"Summarise this Telegram conversation from '{chat_name}' in 2-4 sentences. "
        f"Focus on key topics, decisions, or anything needing attention. Be concise.\n\n{conversation}"
    )
    try:
        return await call_openrouter(prompt)
    except Exception as e:
        return f"Error: {e}"


# ─── BOT COMMANDS ────────────────────────────────────────────────────
@client.on(events.NewMessage(pattern=r"\.help", outgoing=True))
async def handle_help(event):
    await event.delete()
    await client.send_message("me", """🤖 **Userbot Commands**

📋 **SUMMARISE**
`.summarise` — Top 10 chats
`.summarise 20` — Top N chats
`.sum John` — One specific chat
`.folder` — List folders
`.folder Work` — Summarise folder

🔗 **LINKS**
`.links` — All links from all groups
`.links mega` — Only Mega links
`.links drive` — Google Drive only
`.links mega Movies` — Mega from one group
`.scrape add Movies` — Save group to scrape list
`.scrape remove Movies` — Remove from list
`.scrape list` — See saved groups
`.links mega scrape` — Scrape saved groups only

🤖 **AI REPLY**
`.aireply on` — AI replies to ALL DMs
`.aireply off` — Turn off

💬 **SIMPLE REPLY**
`.autoreply on Busy!` — Fixed reply
`.autoreply off` — Turn off

📢 `.broadcast Hello!` — Send to 10 contacts ⚠️""")


@client.on(events.NewMessage(pattern=r"\.scrape (add|remove|list)(?: (.+))?", outgoing=True))
async def handle_scrape(event):
    await event.delete()
    action = event.pattern_match.group(1)
    name = (event.pattern_match.group(2) or "").strip()
    if action == "add" and name:
        if name not in state["scrape_groups"]:
            state["scrape_groups"].append(name)
        await client.send_message("me", f"✅ Added **{name}** to scrape list.\nGroups: {', '.join(state['scrape_groups'])}")
    elif action == "remove" and name:
        state["scrape_groups"] = [g for g in state["scrape_groups"] if g.lower() != name.lower()]
        await client.send_message("me", f"🗑️ Removed **{name}**.\nGroups: {', '.join(state['scrape_groups']) or 'empty'}")
    elif action == "list":
        if state["scrape_groups"]:
            await client.send_message("me", "📋 Scrape groups:\n" + "\n".join(f"• {g}" for g in state["scrape_groups"]))
        else:
            await client.send_message("me", "📋 No saved groups. Use `.scrape add GroupName`")


@client.on(events.NewMessage(pattern=r"\.summarise(all)?(\s+\d+)?", outgoing=True))
async def handle_summarise(event):
    await event.delete()
    args = event.pattern_match.group(2)
    limit = int(args.strip()) if args else MAX_CHATS
    status = await client.send_message("me", f"⏳ Summarising top {limit} chats...")
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
                    sender = chat_name if isinstance(entity, User) else (getattr(await msg.get_sender(), "first_name", None) or "Someone")
                except:
                    sender = "Someone"
                messages.append(f"{sender}: {msg.text}")
        if not messages:
            continue
        messages.reverse()
        summary = await summarise_chat(chat_name, messages)
        if summary:
            icon = "👤" if isinstance(entity, User) else "👥"
            unread = f" • 🔴 {dialog.unread_count} unread" if dialog.unread_count > 0 else ""
            results.append(f"{icon} **{chat_name}**{unread}\n{summary}")
            count += 1
        if count % 5 == 0 and count > 0:
            await status.edit(f"⏳ Done {count}/{limit}...")
    if not results:
        await status.edit("📭 No messages found.")
        return
    await status.delete()
    full = f"📋 Summary of {count} chats\n\n" + "\n\n".join(results)
    for chunk in [full[i:i+4000] for i in range(0, len(full), 4000)]:
        await client.send_message("me", chunk)


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
                        sender = getattr(await msg.get_sender(), "first_name", None) or dialog.name
                    except:
                        sender = "Someone"
                    messages.append(f"{sender}: {msg.text}")
            if not messages:
                await status.edit(f"📭 No messages in '{dialog.name}'.")
                return
            messages.reverse()
            summary = await summarise_chat(dialog.name, messages)
            await status.edit(f"📋 **{dialog.name}**\n\n{summary}")
            return
    await status.edit(f"❌ No chat matching '{query}'")


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
        await client.send_message("me", "📁 Folders:\n" + "\n".join(f"• {n}" for n in names) if names else "No folders found.")
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
        await client.send_message("me", f"❌ No folder matching '{query}'.")
        return
    status = await client.send_message("me", f"📁 Summarising '{query}'...")
    results = []
    for peer in getattr(target_filter, "include_peers", []):
        try:
            entity = await client.get_entity(peer)
            chat_name = getattr(entity, "title", None) or getattr(entity, "first_name", "Unknown")
            messages = []
            async for msg in client.iter_messages(entity, limit=MESSAGES_PER_CHAT):
                if msg.text:
                    try:
                        sender = getattr(await msg.get_sender(), "first_name", None) or chat_name
                    except:
                        sender = "Someone"
                    messages.append(f"{sender}: {msg.text}")
            if not messages:
                continue
            messages.reverse()
            summary = await summarise_chat(chat_name, messages)
            if summary:
                results.append(f"💬 **{chat_name}**\n{summary}")
        except:
            continue
    if not results:
        await status.edit("📭 No messages found.")
        return
    await status.delete()
    full = f"📁 **{query}**\n\n" + "\n\n".join(results)
    for chunk in [full[i:i+4000] for i in range(0, len(full), 4000)]:
        await client.send_message("me", chunk)


@client.on(events.NewMessage(pattern=r"\.links ?(.+)?", outgoing=True))
async def handle_links(event):
    await event.delete()
    raw = (event.pattern_match.group(1) or "").strip()
    parts = raw.split(" ", 1)
    filter_key = parts[0].lower() if parts else "all"
    group_query = parts[1].strip() if len(parts) > 1 else ""
    use_scrape_list = group_query == "scrape"
    if filter_key not in LINK_FILTERS:
        pattern = LINK_FILTERS["all"]
        filter_label = "all"
        group_query = raw
    else:
        pattern = LINK_FILTERS[filter_key]
        filter_label = filter_key
    URL_REGEX = re.compile(pattern, re.IGNORECASE)
    status = await client.send_message("me", f"🔍 Scraping {filter_label} links...")
    all_links = []
    groups_checked = 0
    start = asyncio.get_event_loop().time()
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        if isinstance(entity, User):
            continue
        name = dialog.name or ""
        if use_scrape_list:
            if not any(g.lower() in name.lower() for g in state["scrape_groups"]):
                continue
        elif group_query and group_query != "scrape":
            if group_query.lower() not in name.lower():
                continue
        groups_checked += 1
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
            all_links.append(f"📌 **{name}** ({len(unique)})\n" + "\n".join(unique))
        if groups_checked % 5 == 0:
            elapsed = asyncio.get_event_loop().time() - start
            await status.edit(f"🔍 {groups_checked} groups checked... ({elapsed:.0f}s)")
        if group_query and group_query != "scrape" and all_links:
            break
    elapsed = asyncio.get_event_loop().time() - start
    if not all_links:
        await status.edit(f"📭 No {filter_label} links found. ({elapsed:.0f}s)")
        return
    await status.delete()
    total = sum(len(g.split("\n")) - 1 for g in all_links)
    full = f"🔗 {filter_label.upper()} — {total} links, {groups_checked} groups ({elapsed:.0f}s)\n\n" + "\n\n".join(all_links)
    for chunk in [full[i:i+4000] for i in range(0, len(full), 4000)]:
        await client.send_message("me", chunk)


@client.on(events.NewMessage(pattern=r"\.aireply (on|off)", outgoing=True))
async def handle_aireply_toggle(event):
    await event.delete()
    state["ai_reply"] = event.pattern_match.group(1) == "on"
    status = "✅ ON — replying to ALL DMs" if state["ai_reply"] else "🔕 OFF"
    await client.send_message("me", f"🤖 AI reply: {status}")
    log(f"AI reply: {status}")


DEFAULT_PERSONA = """You are replying on behalf of the user named {my_name} in a Telegram chat.

About the user:
- Casual and friendly, plain English
- Uses short forms naturally: irl, imo, ig, obv, fs, ngl, rn, rly, tbh, lmao, fr
- Replies are SHORT — 1 to 3 sentences max
- No emojis unless the other person uses them first
- Sounds human and natural, never robotic or formal
- Never starts a reply with "Hey" every single time — vary the opening
- Never reveals being an AI
- If asked something personal the user hasn't shared, give a vague natural deflection
- Reads the FULL conversation history carefully before replying
- Matches the energy and tone of the conversation"""

# Editable persona stored in state
state["ai_persona"] = DEFAULT_PERSONA
state["my_name"] = "me"


@client.on(events.NewMessage(incoming=True))
async def handle_ai_autoreply(event):
    if not state["ai_reply"] or not event.is_private:
        return
    try:
        sender = await event.get_sender()
        if not sender or getattr(sender, "bot", False):
            return
        sender_name = getattr(sender, "first_name", None) or "Someone"
        my_name = state.get("my_name", "me")

        # Scrape full conversation history (up to 40 messages)
        history = []
        async for msg in client.iter_messages(event.chat_id, limit=40):
            if msg.text:
                who = my_name if msg.out else sender_name
                history.append(f"{who}: {msg.text}")
        history.reverse()
        full_convo = "\n".join(history)

        persona = state["ai_persona"].replace("{my_name}", my_name)

        prompt = f"""{persona}

Full conversation with {sender_name}:
{full_convo}

{sender_name} just sent: {event.text}

Reply as {my_name} (short, casual, no unnecessary emojis):"""

        reply = await call_openrouter(prompt)
        await event.reply(reply)
        log(f"AI replied to {sender_name}")
    except Exception as e:
        log(f"AI reply error: {e}")


@client.on(events.NewMessage(pattern=r"\.autoreply (on|off)(?: (.+))?", outgoing=True))
async def handle_autoreply_toggle(event):
    await event.delete()
    mode = event.pattern_match.group(1)
    msg = (event.pattern_match.group(2) or "").strip()
    if mode == "on":
        state["autoreply"]["active"] = True
        state["autoreply"]["message"] = msg or "Hey! Busy rn, will reply soon 👍"
        state["autoreply_replied"].clear()
        await client.send_message("me", f"✅ Auto-reply ON: {state['autoreply']['message']}")
    else:
        state["autoreply"]["active"] = False
        state["autoreply_replied"].clear()
        await client.send_message("me", "🔕 Auto-reply OFF")


@client.on(events.NewMessage(incoming=True))
async def handle_incoming_autoreply(event):
    if not state["autoreply"]["active"] or not event.is_private:
        return
    if event.sender_id in state["autoreply_replied"]:
        return
    state["autoreply_replied"].add(event.sender_id)
    await event.reply(state["autoreply"]["message"])


@client.on(events.NewMessage(pattern=r"\.broadcast (.+)", outgoing=True))
async def handle_broadcast(event):
    await event.delete()
    msg = event.pattern_match.group(1).strip()
    status = await client.send_message("me", "📢 Broadcasting to up to 10 contacts...")
    sent = 0
    async for dialog in client.iter_dialogs():
        if sent >= 10:
            break
        if not isinstance(dialog.entity, User) or dialog.entity.bot or dialog.entity.is_self:
            continue
        try:
            await client.send_message(dialog.entity, msg)
            sent += 1
            await asyncio.sleep(2)
        except:
            continue
    await status.edit(f"✅ Sent to {sent} people.")


# ─── WEB API ─────────────────────────────────────────────────────────
async def api_status(request):
    me = state["me"]
    return web.json_response({
        "ok": True,
        "user": {"name": me.first_name, "username": me.username, "id": me.id} if me else None,
        "ai_reply": state["ai_reply"],
        "autoreply": state["autoreply"]["active"],
        "autoreply_message": state["autoreply"]["message"],
        "scrape_groups": state["scrape_groups"],
        "model": OPENROUTER_MODEL,
        "log": state["status_log"][-10:],
        "ai_persona": state.get("ai_persona", ""),
        "my_name": state.get("my_name", "me"),
    })

async def api_update_persona(request):
    data = await request.json()
    if "persona" in data:
        state["ai_persona"] = data["persona"]
    if "my_name" in data and data["my_name"].strip():
        state["my_name"] = data["my_name"].strip()
    log(f"AI persona updated via web")
    return web.json_response({"ok": True})

async def api_toggle_ai(request):
    data = await request.json()
    state["ai_reply"] = data.get("active", False)
    log(f"AI reply {'ON' if state['ai_reply'] else 'OFF'} via web")
    return web.json_response({"ok": True, "ai_reply": state["ai_reply"]})

async def api_toggle_autoreply(request):
    data = await request.json()
    state["autoreply"]["active"] = data.get("active", False)
    state["autoreply"]["message"] = data.get("message", "Hey! Busy rn, will reply soon 👍")
    state["autoreply_replied"].clear()
    log(f"Autoreply {'ON' if state['autoreply']['active'] else 'OFF'} via web")
    return web.json_response({"ok": True})

async def api_summarise(request):
    data = await request.json()
    limit = data.get("limit", MAX_CHATS)
    asyncio.create_task(do_summarise(limit))
    return web.json_response({"ok": True, "message": f"Summarising {limit} chats — check Saved Messages!"})

async def do_summarise(limit):
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
                    sender = chat_name if isinstance(entity, User) else (getattr(await msg.get_sender(), "first_name", None) or "Someone")
                except:
                    sender = "Someone"
                messages.append(f"{sender}: {msg.text}")
        if not messages:
            continue
        messages.reverse()
        summary = await summarise_chat(chat_name, messages)
        if summary:
            icon = "👤" if isinstance(entity, User) else "👥"
            unread = f" • 🔴 {dialog.unread_count} unread" if dialog.unread_count > 0 else ""
            results.append(f"{icon} {chat_name}{unread}\n{summary}")
            count += 1
    if results:
        full = f"📋 Summary of {count} chats\n\n" + "\n\n".join(results)
        for chunk in [full[i:i+4000] for i in range(0, len(full), 4000)]:
            await client.send_message("me", chunk)

async def api_scrape_links(request):
    data = await request.json()
    filter_key = data.get("filter", "all")
    group = data.get("group", "")
    use_saved = data.get("use_saved", False)
    asyncio.create_task(do_scrape_links(filter_key, group, use_saved))
    return web.json_response({"ok": True, "message": "Scraping links — check Saved Messages!"})

async def do_scrape_links(filter_key, group_query, use_saved):
    pattern = LINK_FILTERS.get(filter_key, LINK_FILTERS["all"])
    URL_REGEX = re.compile(pattern, re.IGNORECASE)
    all_links = []
    groups_checked = 0
    start = asyncio.get_event_loop().time()
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        if isinstance(entity, User):
            continue
        name = dialog.name or ""
        if use_saved:
            if not any(g.lower() in name.lower() for g in state["scrape_groups"]):
                continue
        elif group_query:
            if group_query.lower() not in name.lower():
                continue
        groups_checked += 1
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
            all_links.append(f"📌 {name} ({len(unique)})\n" + "\n".join(unique))
        if group_query and all_links:
            break
    elapsed = asyncio.get_event_loop().time() - start
    if all_links:
        total = sum(len(g.split("\n")) - 1 for g in all_links)
        full = f"🔗 {filter_key.upper()} — {total} links, {groups_checked} groups ({elapsed:.0f}s)\n\n" + "\n\n".join(all_links)
        for chunk in [full[i:i+4000] for i in range(0, len(full), 4000)]:
            await client.send_message("me", chunk)
    else:
        await client.send_message("me", f"📭 No {filter_key} links found. ({elapsed:.0f}s)")

async def api_scrape_groups(request):
    data = await request.json()
    action = data.get("action")
    name = data.get("name", "")
    if action == "add" and name:
        if name not in state["scrape_groups"]:
            state["scrape_groups"].append(name)
    elif action == "remove":
        state["scrape_groups"] = [g for g in state["scrape_groups"] if g != name]
    return web.json_response({"ok": True, "groups": state["scrape_groups"]})

async def api_dialogs(request):
    dialogs = []
    count = 0
    async for dialog in client.iter_dialogs():
        if count >= 30:
            break
        if dialog.archived:
            continue
        entity = dialog.entity
        dialogs.append({
            "name": dialog.name or "Unknown",
            "type": "user" if isinstance(entity, User) else "group",
            "unread": dialog.unread_count,
            "id": str(dialog.id),
        })
        count += 1
    return web.json_response({"ok": True, "dialogs": dialogs})

async def api_summarise_chat(request):
    data = await request.json()
    chat_name = data.get("name", "")
    asyncio.create_task(do_summarise_single(chat_name))
    return web.json_response({"ok": True, "message": f"Summarising {chat_name} — check Saved Messages!"})

async def do_summarise_single(query):
    async for dialog in client.iter_dialogs():
        if query.lower() in (dialog.name or "").lower():
            messages = []
            async for msg in client.iter_messages(dialog.entity, limit=MESSAGES_PER_CHAT):
                if msg.text:
                    try:
                        sender = getattr(await msg.get_sender(), "first_name", None) or dialog.name
                    except:
                        sender = "Someone"
                    messages.append(f"{sender}: {msg.text}")
            if not messages:
                await client.send_message("me", f"📭 No messages in '{dialog.name}'.")
                return
            messages.reverse()
            summary = await summarise_chat(dialog.name, messages)
            await client.send_message("me", f"📋 **{dialog.name}**\n\n{summary}")
            return
    await client.send_message("me", f"❌ No chat matching '{query}'")

# Serve frontend
async def serve_index(request):
    html_path = os.path.join(os.path.dirname(__file__), "index.html")
    return web.FileResponse(html_path)

def make_app():
    app = web.Application()
    app.router.add_get("/", serve_index)
    app.router.add_get("/api/status", api_status)
    app.router.add_post("/api/ai-reply", api_toggle_ai)
    app.router.add_post("/api/autoreply", api_toggle_autoreply)
    app.router.add_post("/api/summarise", api_summarise)
    app.router.add_post("/api/summarise-chat", api_summarise_chat)
    app.router.add_post("/api/links", api_scrape_links)
    app.router.add_post("/api/scrape-groups", api_scrape_groups)
    app.router.add_post("/api/persona", api_update_persona)
    return app

# ─── MAIN ────────────────────────────────────────────────────────────
async def main():
    log("Starting userbot...")
    await client.start()
    if not await client.is_user_authorized():
        log("❌ Session invalid!")
        return
    me = await client.get_me()
    state["me"] = me
    log(f"✅ Logged in as {me.first_name} (@{me.username})")
    log(f"✅ Model: {OPENROUTER_MODEL}")

    app = make_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log(f"✅ Web dashboard running on port {PORT}")

    await client.run_until_disconnected()

asyncio.run(main())
