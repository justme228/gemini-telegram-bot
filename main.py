import os
import httpx
from fastapi import FastAPI, Request
from google import genai
from google.genai import types

app = FastAPI()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
# Specify any 3.x model here (defaults to gemini-3-flash)
MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-3-flash")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

client = genai.Client(api_key=GEMINI_API_KEY)

# In-memory chat storage per Telegram user: {chat_id: chat_session}
# (Keeps context while the server is active)
user_sessions = {}

@app.get("/")
def health_check():
    return {"status": "ok", "model": MODEL_NAME}

@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    
    if "message" in data and "text" in data["message"]:
        chat_id = data["message"]["chat"]["id"]
        user_text = data["message"]["text"].strip()
        
        # Reset chat session on /start or /clear
        if user_text in ["/start", "/clear", "/reset"]:
            user_sessions[chat_id] = client.chats.create(model=MODEL_NAME)
            reply_text = f"Context reset! Connected to {MODEL_NAME}. What would you like to talk about?"
        else:
            if chat_id not in user_sessions:
                user_sessions[chat_id] = client.chats.create(model=MODEL_NAME)
            
            chat = user_sessions[chat_id]
            try:
                response = chat.send_message(user_text)
                reply_text = response.text or "No response generated."
            except Exception as e:
                reply_text = f"Gemini Error: {e}"

        # Send response back to Telegram
        async with httpx.AsyncClient(timeout=60.0) as http_client:
            await http_client.post(
                f"{TELEGRAM_API_URL}/sendMessage",
                json={"chat_id": chat_id, "text": reply_text},
            )

    return {"status": "ok"}