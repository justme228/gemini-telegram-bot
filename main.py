import html
import os
import re
import httpx
from fastapi import FastAPI, Request
from google import genai
from google.genai import types

app = FastAPI()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

client = genai.Client(api_key=GEMINI_API_KEY)
user_sessions = {}
bot_username = None

# Tell Gemini to avoid markdown headers and keep it compact
CHAT_CONFIG = types.GenerateContentConfig(
    system_instruction=(
        "You are an ultra-compact Telegram assistant. "
        "Keep answers short, concise, and dense without losing essential facts. "
        "Do NOT use markdown headers like ### or ## (use bold text instead). Keep the standard format for Telegram messages "
        "Do NOT provide sources, citations, or URLs unless explicitly asked."
    ),
    tools=[types.Tool(google_search=types.GoogleSearch())],
)


def format_for_telegram(text: str) -> str:
  """Converts Gemini's standard markdown into Telegram-safe HTML."""
  # 1. Escape HTML special characters
  text = html.escape(text, quote=False)

  # 2. Convert markdown headers (### Header) to bold
  text = re.sub(r"^(?:#{1,6})\s*(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)

  # 3. Convert **bold** to <b>bold</b>
  text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)

  # 4. Convert *italic* or _italic_ to <i>italic</i>
  text = re.sub(r"(?<!\n)\*(.+?)\*", r"<i>\1</i>", text)
  text = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"<i>\1</i>", text)

  # 5. Convert `code` to <code>code</code>
  text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)

  # 6. Convert bullet asterisks (* item) into clean bullet points (• item)
  text = re.sub(r"^\*\s+", r"• ", text, flags=re.MULTILINE)

  return text


async def get_bot_username():
  global bot_username
  if not bot_username:
    async with httpx.AsyncClient() as http_client:
      res = await http_client.get(f"{TELEGRAM_API_URL}/getMe")
      if res.status_code == 200:
        bot_username = res.json().get("result", {}).get("username", "").lower()
  return bot_username


@app.get("/")
def health_check():
  return {"status": "ok", "model": MODEL_NAME}


@app.post("/webhook")
async def telegram_webhook(request: Request):
  data = await request.json()
  message = data.get("message")

  if not message or "text" not in message:
    return {"status": "ok"}

  chat_id = message["chat"]["id"]
  chat_type = message["chat"].get("type", "private")
  user_text = message["text"].strip()

  my_username = await get_bot_username()
  mention = f"@{my_username}" if my_username else None

  # Check if someone replied to the bot
  reply_to = message.get("reply_to_message")
  is_reply_to_bot = (
      reply_to
      and reply_to.get("from", {}).get("is_bot")
      and reply_to.get("from", {}).get("username", "").lower() == my_username
  )

  # Check if bot was @tagged
  is_mentioned = mention and (mention in user_text.lower())

  # In groups, only answer when tagged or replied to
  if chat_type in ["group", "supergroup"]:
    if not (is_mentioned or is_reply_to_bot or user_text.startswith("/start")):
      return {"status": "ignored"}

  # Strip @mention from prompt
  clean_prompt = user_text
  if mention and mention in clean_prompt.lower():
    clean_prompt = re.sub(
        re.escape(mention), "", clean_prompt, flags=re.IGNORECASE
    ).strip()

  if not clean_prompt and is_mentioned:
    clean_prompt = "Hello!"

  # Reset context command
  if clean_prompt in ["/start", "/clear", "/reset"]:
    user_sessions[chat_id] = client.chats.create(
        model=MODEL_NAME, config=CHAT_CONFIG
    )
    reply_text = f"Context reset! Ready ({MODEL_NAME})."
  else:
    if chat_id not in user_sessions:
      user_sessions[chat_id] = client.chats.create(
          model=MODEL_NAME, config=CHAT_CONFIG
      )

    chat = user_sessions[chat_id]
    try:
      response = chat.send_message(clean_prompt)
      raw_text = response.text or "No response."
      reply_text = format_for_telegram(raw_text)
    except Exception as e:
      reply_text = f"Gemini Error: {e}"

  # Keep within Telegram limit
  if len(reply_text) > 4000:
    reply_text = reply_text[:4000] + "..."

  # Send response via HTML mode
  async with httpx.AsyncClient(timeout=60.0) as http_client:
    payload = {
        "chat_id": chat_id,
        "text": reply_text,
        "reply_to_message_id": message["message_id"],
        "parse_mode": "HTML",
    }
    res = await http_client.post(f"{TELEGRAM_API_URL}/sendMessage", json=payload)
    if res.status_code != 200:
      # If Telegram HTML parsing still complains, fallback to clean plain text
      payload.pop("parse_mode")
      payload["text"] = re.sub(r"<[^>]+>", "", reply_text)
      await http_client.post(f"{TELEGRAM_API_URL}/sendMessage", json=payload)

  return {"status": "ok"}