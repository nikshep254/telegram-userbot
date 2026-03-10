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
    "scrape_groups": [],
    "me": None,
    "status_log": [],
    "chat_contexts": {},
    "results": [],
    "task_status": "idle",
    "messages_today": 0,
    "goodbye_senders": set(),
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
- Matches the energy and tone of the conversation

CRITICAL RULES ABOUT CONTINUING THE CONVERSATION:
- NEVER treat "ok", "okay", "hm", "lol", "haha", "nice", "cool", "k" as conversation-enders
- These are just acknowledgements — always keep the conversation going naturally
- Ask a follow-up question, share a thought, or react to what they said
- Only end the conversation if they explicitly say a FINAL goodbye like "bye", "gotta go", "ttyl", "talk later", "gtg", "see you", "good night"
- Even then, send ONE warm closing message like "buh bye!", "talk soon!", "catch you later!" — then stop replying
- If unsure whether it's goodbye, keep chatting"""

# Editable persona stored in state
state["ai_persona"] = DEFAULT_PERSONA
state["my_name"] = "me"

# Track goodbye state per sender so AI stops after final bye
goodbye_senders = set()

FINAL_GOODBYE_WORDS = ["bye", "goodbye", "gotta go", "gtg", "ttyl", "talk later", "see you", "see ya", "good night", "gn", "take care", "cya"]

def is_final_goodbye(text: str) -> bool:
    t = text.lower().strip()
    return any(w in t for w in FINAL_GOODBYE_WORDS)

def is_just_acknowledgement(text: str) -> bool:
    t = text.lower().strip()
    acks = ["ok", "okay", "k", "lol", "haha", "hah", "hm", "hmm", "nice", "cool", "wow", "oh", "ah", "yeah", "yep", "yup", "sure", "alright", "aight", "ikr", "ik", "true", "facts", "fr", "lmao", "lmfao", "😂", "😅", "👍", "🙏"]
    return t in acks


@client.on(events.NewMessage(incoming=True))
async def handle_ai_autoreply(event):
    if not state["ai_reply"] or not event.is_private:
        return
    try:
        sender = await event.get_sender()
        if not sender or getattr(sender, "bot", False):
            return
        sender_name = getattr(sender, "first_name", None) or "Someone"
        sender_id = event.sender_id
        my_name = state.get("my_name", "me")

        # If sender already said final goodbye, stop replying
        if sender_id in goodbye_senders:
            return

        # Scrape full conversation history (up to 40 messages)
        history = []
        async for msg in client.iter_messages(event.chat_id, limit=40):
            if msg.text:
                who = my_name if msg.out else sender_name
                history.append(f"{who}: {msg.text}")
        history.reverse()
        full_convo = "\n".join(history)

        persona = state["ai_persona"].replace("{my_name}", my_name)
        chat_context = state["chat_contexts"].get(sender_name, "")
        extra = f"\nExtra context about {sender_name}:\n{chat_context}\n" if chat_context else ""

        # Detect final goodbye
        if is_final_goodbye(event.text):
            goodbye_senders.add(sender_id)
            farewell_prompt = f"""{persona}
{extra}
Conversation:
{full_convo}

{sender_name} just said goodbye: "{event.text}"

Send ONE short warm farewell. Like "buh bye!", "talk soon!", "catch you later!" — keep it natural and brief:"""
            reply = await call_openrouter(farewell_prompt)
            await event.reply(reply)
            state["messages_today"] = state.get("messages_today", 0) + 1
            log(f"AI sent farewell to {sender_name}, stopping replies")
            return

        # Build prompt — explicitly tell AI to keep convo going
        convo_note = ""
        if is_just_acknowledgement(event.text):
            convo_note = f'\nNOTE: "{event.text}" is just an acknowledgement, NOT a goodbye. Keep the conversation going — ask something, share a thought, react naturally.\n'

        prompt = f"""{persona}
{extra}{convo_note}
Full conversation with {sender_name}:
{full_convo}

{sender_name} just sent: {event.text}

Reply as {my_name} (short, casual, keep conversation going, no unnecessary emojis):"""

        reply = await call_openrouter(prompt)
        await event.reply(reply)
        state["messages_today"] = state.get("messages_today", 0) + 1
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
        "task_status": state["task_status"],
        "results": state["results"],
        "messages_today": state.get("messages_today", 0),
    })

async def api_clear_results(request):
    state["results"] = []
    state["task_status"] = "idle"
    return web.json_response({"ok": True})

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
    state["results"] = []
    state["task_status"] = "running"
    asyncio.create_task(do_summarise(limit))
    return web.json_response({"ok": True, "message": f"Summarising {limit} chats..."})

async def do_summarise(limit):
    try:
        results = []
        count = 0
        async for dialog in client.iter_dialogs():
            if count >= limit:
                break
            if dialog.archived:
                continue
            entity = dialog.entity
            chat_name = dialog.name or "Unknown"
            state["results"].append({"type": "status", "content": f"Processing {chat_name}..."})
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
                unread = f" • {dialog.unread_count} unread" if dialog.unread_count > 0 else ""
                state["results"].append({"type": "summary", "name": f"{icon} {chat_name}{unread}", "content": summary})
                results.append(f"{icon} {chat_name}{unread}\n{summary}")
                count += 1
        state["task_status"] = "done"
        if results:
            full = f"📋 Summary of {count} chats\n\n" + "\n\n".join(results)
            for chunk in [full[i:i+4000] for i in range(0, len(full), 4000)]:
                await client.send_message("me", chunk)
    except Exception as e:
        state["results"].append({"type": "error", "content": str(e)})
        state["task_status"] = "done"

async def api_scrape_links(request):
    data = await request.json()
    filter_key = data.get("filter", "all")
    group = data.get("group", "").strip()
    use_saved = data.get("use_saved", False)
    state["results"] = []
    state["task_status"] = "running"
    asyncio.create_task(do_scrape_links(filter_key, group, use_saved))
    return web.json_response({"ok": True, "message": "Scraping started..."})

async def do_scrape_links(filter_key, group_query, use_saved):
    def push(item_type, content):
        state["results"].append({"type": item_type, "content": content})
        log(content)

    try:
        pattern = LINK_FILTERS.get(filter_key, LINK_FILTERS["all"])
        URL_REGEX = re.compile(pattern, re.IGNORECASE)
        all_links = []
        groups_checked = 0
        start = asyncio.get_event_loop().time()

        # Support t.me/ links directly
        tme_match = re.match(r'(?:https?://)?t\.me/([a-zA-Z0-9_]+)', group_query or "")
        if tme_match:
            username = tme_match.group(1)
            push("status", f"Resolving t.me/{username}...")
            try:
                entity = await client.get_entity(username)
                name = getattr(entity, "title", None) or getattr(entity, "username", username)
                push("status", f"Scraping {name}...")
                group_links = []
                async for msg in client.iter_messages(entity, limit=200):
                    if not msg.text:
                        continue
                    found = URL_REGEX.findall(msg.text)
                    group_links.extend(found)
                elapsed = asyncio.get_event_loop().time() - start
                if group_links:
                    unique = list(dict.fromkeys(group_links))
                    push("result", f"📌 {name} — {len(unique)} {filter_key} links found ({elapsed:.0f}s)")
                    for link in unique:
                        push("link", link)
                    await client.send_message("me", f"🔗 {filter_key.upper()} from {name} ({len(unique)} links)\n\n" + "\n".join(unique))
                else:
                    push("empty", f"No {filter_key} links found in {name} ({elapsed:.0f}s)")
                    await client.send_message("me", f"📭 No {filter_key} links found in {name}")
            except Exception as e:
                push("error", f"Could not access t.me/{username}: {e}")
            state["task_status"] = "done"
            return

        # Normal group name / saved groups scrape
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
            push("status", f"Checking {name}...")
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
                all_links.append((name, unique))
                push("result", f"📌 {name} — {len(unique)} links")
                for link in unique:
                    push("link", link)
            else:
                push("empty", f"No {filter_key} links in {name}")
            if group_query and all_links:
                break

        elapsed = asyncio.get_event_loop().time() - start
        if all_links:
            total = sum(len(l) for _, l in all_links)
            push("done", f"Done — {total} {filter_key} links from {groups_checked} groups ({elapsed:.0f}s)")
            full = f"🔗 {filter_key.upper()} — {total} links, {groups_checked} groups ({elapsed:.0f}s)\n\n"
            full += "\n\n".join(f"📌 {n} ({len(l)})\n" + "\n".join(l) for n, l in all_links)
            for chunk in [full[i:i+4000] for i in range(0, len(full), 4000)]:
                await client.send_message("me", chunk)
        else:
            push("empty", f"No {filter_key} links found in any group ({elapsed:.0f}s)")
            await client.send_message("me", f"📭 No {filter_key} links found ({elapsed:.0f}s)")

    except Exception as e:
        state["results"].append({"type": "error", "content": str(e)})
        log(f"Scrape error: {e}")
    finally:
        state["task_status"] = "done"

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
    try:
        dms = []
        groups = []
        count = 0
        async for dialog in client.iter_dialogs():
            if count >= 60:
                break
            if dialog.archived:
                continue
            try:
                entity = dialog.entity
                name = dialog.name or "Unknown"
                is_user = isinstance(entity, User) and not getattr(entity, "bot", False) and not getattr(entity, "is_self", False)
                item = {
                    "name": name,
                    "type": "dm" if is_user else "group",
                    "unread": dialog.unread_count or 0,
                    "id": str(dialog.id),
                    "context": state["chat_contexts"].get(name, ""),
                }
                if is_user:
                    dms.append(item)
                else:
                    groups.append(item)
                count += 1
            except:
                continue
        return web.json_response({"ok": True, "dms": dms, "groups": groups})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)

async def api_set_context(request):
    data = await request.json()
    name = data.get("name", "")
    context = data.get("context", "")
    if name:
        state["chat_contexts"][name] = context
        log(f"Context set for {name}")
    return web.json_response({"ok": True})

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
    app.router.add_get("/api/dialogs", api_dialogs)
    app.router.add_post("/api/persona", api_update_persona)
    app.router.add_post("/api/chat-context", api_set_context)
    app.router.add_post("/api/clear-results", api_clear_results)
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

    async def midnight_reset():
        while True:
            await asyncio.sleep(86400)
            state["messages_today"] = 0
            goodbye_senders.clear()
            log("Daily counters reset")

    asyncio.create_task(midnight_reset())

    app = make_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log(f"✅ Web dashboard running on port {PORT}")

    await client.run_until_disconnected()

asyncio.run(main())
