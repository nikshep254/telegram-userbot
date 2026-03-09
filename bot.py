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

client = TelegramClient(StringSession(), API_ID, API_HASH)


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


async def main():
    print("Starting userbot...")
    await client.connect()

    if not await client.is_user_authorized():
        code = os.getenv("TELEGRAM_CODE", "")
        phone_code_hash = os.getenv("TELEGRAM_CODE_HASH", "")

        if not code:
            # Step 1: Request code and print hash
            print(f"Requesting login code for {PHONE}...")
            sent = await client.send_code_request(PHONE)
            print(f"CODE_HASH={sent.phone_code_hash}")
            print("Check Telegram for your login code!")
            print("Now add TELEGRAM_CODE and TELEGRAM_CODE_HASH to Railway variables and redeploy.")
            return

        # Step 2: Sign in with code
        print("Signing in with code...")
        await client.sign_in(PHONE, code, phone_code_hash=phone_code_hash)
        print("Logged in successfully!")

    print("Userbot running! Type .summarise in any Telegram chat.")
    await client.run_until_disconnected()


asyncio.run(main())
