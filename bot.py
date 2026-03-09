import os
import asyncio
from telethon import TelegramClient, events
from telethon.tl.types import User, Chat, Channel
import httpx
from dotenv import load_dotenv

load_dotenv()

API_ID = int(os.getenv("TELEGRAM_API_ID"))
API_HASH = os.getenv("TELEGRAM_API_HASH")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "mistralai/mistral-7b-instruct:free")

# How many recent messages to fetch per chat
MESSAGES_PER_CHAT = int(os.getenv("MESSAGES_PER_CHAT", "30"))
# How many chats to summarise at once
MAX_CHATS = int(os.getenv("MAX_CHATS", "10"))

client = TelegramClient("userbot_session", API_ID, API_HASH)


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


async def summarise_chat(chat_name: str, messages: list[str]) -> str:
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
        return f"⚠️ Error summarising: {e}"


@client.on(events.NewMessage(pattern=r"\.summarise(all)?(\s+\d+)?", outgoing=True))
async def handle_summarise(event):
    """
    Trigger by typing:
      .summarise        — summarise top 10 chats (unread first)
      .summariseall     — summarise all chats (up to MAX_CHATS)
      .summarise 20     — summarise top 20 chats
    """
    await event.delete()  # Clean up your command message

    args = event.pattern_match.group(2)
    limit = int(args.strip()) if args else MAX_CHATS

    status = await client.send_message("me", f"⏳ Fetching your last {limit} active chats...")

    results = []
    count = 0

    async for dialog in client.iter_dialogs():
        if count >= limit:
            break

        # Skip archived
        if dialog.archived:
            continue

        entity = dialog.entity
        chat_name = dialog.name or "Unknown"

        # Get recent messages
        messages = []
        async for msg in client.iter_messages(entity, limit=MESSAGES_PER_CHAT):
            if msg.text:
                sender = ""
                try:
                    if isinstance(entity, User):
                        sender = chat_name
                    else:
                        sender_entity = await msg.get_sender()
                        sender = getattr(sender_entity, "first_name", None) or getattr(sender_entity, "title", "Unknown")
                except:
                    sender = "Someone"
                messages.append(f"{sender}: {msg.text}")

        if not messages:
            continue

        messages.reverse()  # Chronological order
        summary = await summarise_chat(chat_name, messages)

        if summary:
            # Label the chat type
            if isinstance(entity, User):
                icon = "👤"
            elif isinstance(entity, Channel) and entity.megagroup:
                icon = "👥"
            elif isinstance(entity, Channel):
                icon = "📢"
            else:
                icon = "💬"

            unread = f" • 🔴 {dialog.unread_count} unread" if dialog.unread_count > 0 else ""
            results.append(f"{icon} **{chat_name}**{unread}\n{summary}")
            count += 1

        # Update status every 5 chats
        if count % 5 == 0 and count > 0:
            await status.edit(f"⏳ Processed {count}/{limit} chats...")

    if not results:
        await status.edit("📭 No messages found to summarise.")
        return

    # Send results in chunks (Telegram 4096 char limit)
    header = f"📋 **Summary of {count} chats** (last {MESSAGES_PER_CHAT} msgs each)\n{'─'*30}\n\n"
    body = "\n\n".join(results)
    full = header + body

    await status.delete()

    # Split into chunks if needed
    chunk_size = 4000
    chunks = [full[i:i+chunk_size] for i in range(0, len(full), chunk_size)]
    for chunk in chunks:
        await client.send_message("me", chunk, parse_mode="md")


@client.on(events.NewMessage(pattern=r"\.sum (.+)", outgoing=True))
async def handle_single(event):
    """
    Summarise a specific chat by name:
      .sum John
      .sum My Group Name
    """
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
                await status.edit(f"📭 No messages found in '{dialog.name}'.")
                return

            messages.reverse()
            summary = await summarise_chat(dialog.name, messages)
            await status.edit(
                f"📋 **{dialog.name}**\n{'─'*20}\n{summary}",
                parse_mode="md"
            )
            return

    await status.edit(f"❌ No chat found matching '{query}'")


print("✅ Userbot running! Commands:")
print("  .summarise     — summarise top 10 chats")
print("  .summarise 20  — summarise top 20 chats")
print("  .sum John      — summarise specific chat")

client.start()
client.run_until_disconnected()
