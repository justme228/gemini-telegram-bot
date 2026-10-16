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

# Ultra-compact, cost-efficient configuration
CHAT_CONFIG = types.GenerateContentConfig(
    system_instruction=(
        "You are an ultra-compact Telegram assistant. "
        "Keep all answers as short, concise, and dense as possible without losing any essential facts. "
        "No conversational filler, pleasantries, or preamble. "
        "Do NOT provide sources, citations, or URLs unless the user explicitly asks for them."
    ),
    tools=[types.Tool(google_search=types.GoogleSearch())],
)


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
      reply_text = response.text or "No response."
    except Exception as e:
      reply_text = f"Gemini Error: {e}"

  # Keep within Telegram's limit
  if len(reply_text) > 4000:
    reply_text = reply_text[:4000] + "..."

  # Send response (with fallback if markdown formatting has issues)
  async with httpx.AsyncClient(timeout=60.0) as http_client:
    payload = {
        "chat_id": chat_id,
        "text": reply_text,
        "reply_to_message_id": message["message_id"],
        "parse_mode": "Markdown",
    }
    res = await http_client.post(f"{TELEGRAM_API_URL}/sendMessage", json=payload)
    if res.status_code != 200:
      # Retry without markdown formatting if Telegram rejected special characters
      payload.pop("parse_mode")
      await http_client.post(f"{TELEGRAM_API_URL}/sendMessage", json=payload)

  return {"status": "ok"}