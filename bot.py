import argparse
import asyncio
import logging
import os
import re
import sys
from pathlib import Path

from openai import OpenAI
from telegram import Bot, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

# Concise standard logging setup
logging.basicConfig(
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
    level=logging.INFO,
)
logger = logging.getLogger("Coach")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

BASE_DIR = Path(__file__).parent
SKILLS_PATH = BASE_DIR / "skills" / "telegram.md"
STATE_PATH = BASE_DIR / "skills" / "state.md"

MODEL = "openai/gpt-oss-120b"


def read_file(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def save_study_state(content: str):
    logger.info("Saving updated study state to skills/state.md...")
    STATE_PATH.write_text(content, encoding="utf-8")


def build_system_prompt() -> str:
    instructions = read_file(SKILLS_PATH)
    current_state = read_file(STATE_PATH)
    return f"{instructions}\n\nCURRENT CANDIDATE STUDY STATE:\n{current_state}"


def extract_and_save_state(ai_response: str) -> str:
    pattern = r"<STUDY_STATE>(.*?)</STUDY_STATE>"
    match = re.search(pattern, ai_response, re.DOTALL)

    if match:
        updated_state = match.group(1).strip()
        save_study_state(updated_state)
        return re.sub(pattern, "", ai_response, flags=re.DOTALL).strip()

    logger.warning("No <STUDY_STATE> block found in response.")
    return ai_response


def format_for_telegram(text: str) -> str:
    if not text:
        return ""

    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    lines = text.split("\n")
    cleaned_lines = []

    for line in lines:
        stripped = line.strip()

        if stripped.startswith("#"):
            header_text = re.sub(r"^#+\s*", "", stripped)
            cleaned_lines.append(f"\n<b>{header_text}</b>")
            continue

        if "|" in stripped:
            if re.match(r"^\|?[\s\:\-\|]+\|?$", stripped):
                continue
            parts = [p.strip() for p in stripped.split("|") if p.strip()]
            if len(parts) >= 2 and parts[0].lower() not in ["topic", "concept", "item", "status"]:
                cleaned_lines.append(f"• <b>{parts[0]}:</b> {parts[1]}")
            elif len(parts) == 1:
                cleaned_lines.append(f"• {parts[0]}")
            continue

        cleaned_lines.append(line)

    text = "\n".join(cleaned_lines)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


async def send_daily_prompt():
    logger.info("Triggering daily prompt workflow...")

    if not TELEGRAM_TOKEN or not GROQ_API_KEY or not CHAT_ID:
        logger.error("Missing required environment variables (TELEGRAM_BOT_TOKEN, GROQ_API_KEY, TELEGRAM_CHAT_ID).")
        sys.exit(1)

    client = OpenAI(
        api_key=GROQ_API_KEY,
        base_url="https://api.groq.com/openai/v1",
        timeout=30.0,
    )

    logger.info("Requesting interview question from Groq API...")
    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": build_system_prompt()},
                {
                    "role": "user",
                    "content": "It is 8:00 AM. Send me today's technical interview question based on priority and review schedule in skills/state.md.",
                },
            ],
            temperature=0.5,
        )
    except Exception as e:
        logger.error(f"Groq API error: {e}")
        sys.exit(1)

    clean_message = extract_and_save_state(response.choices[0].message.content)
    formatted_message = format_for_telegram(clean_message)

    logger.info(f"Sending message to Telegram chat ID: {CHAT_ID}...")
    try:
        bot = Bot(token=TELEGRAM_TOKEN, request=HTTPXRequest(connect_timeout=30.0, read_timeout=30.0))
        await bot.send_message(
            chat_id=int(CHAT_ID),
            text=f"☀️ <b>Daily Interview Prompt</b>\n\n{formatted_message}",
            parse_mode=ParseMode.HTML,
        )
        logger.info("Done: Daily question sent successfully.")
    except Exception as e:
        logger.error(f"Telegram API error: {e}")
        sys.exit(1)


conversations = {}


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    logger.info(f"Session started for chat_id: {chat_id}")

    client = OpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1", timeout=30.0)
    conversations[chat_id] = [{"role": "system", "content": build_system_prompt()}]
    conversations[chat_id].append(
        {"role": "user", "content": "Start the interview now with highest priority question in skills/state.md."}
    )

    response = client.chat.completions.create(model=MODEL, messages=conversations[chat_id], temperature=0.5)
    conversations[chat_id].append({"role": "assistant", "content": response.choices[0].message.content})

    clean_message = extract_and_save_state(response.choices[0].message.content)
    await update.message.reply_text(format_for_telegram(clean_message), parse_mode=ParseMode.HTML)


async def done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in conversations:
        await update.message.reply_text("No active session.")
        return

    logger.info(f"Ending session for chat_id: {chat_id}")
    client = OpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1", timeout=30.0)
    conversations[chat_id].append({"role": "user", "content": "The user issued /done. Summarize and update skills/state.md."})

    response = client.chat.completions.create(model=MODEL, messages=conversations[chat_id], temperature=0.5)
    clean_message = extract_and_save_state(response.choices[0].message.content)

    await update.message.reply_text(f"🏁 <b>Session Complete!</b>\n\n{format_for_telegram(clean_message)}", parse_mode=ParseMode.HTML)
    del conversations[chat_id]


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    client = OpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1", timeout=30.0)

    if chat_id not in conversations:
        conversations[chat_id] = [{"role": "system", "content": build_system_prompt()}]

    conversations[chat_id].append({"role": "user", "content": update.message.text})
    conversations[chat_id][0] = {"role": "system", "content": build_system_prompt()}

    response = client.chat.completions.create(model=MODEL, messages=conversations[chat_id], temperature=0.5)
    conversations[chat_id].append({"role": "assistant", "content": response.choices[0].message.content})

    clean_message = extract_and_save_state(response.choices[0].message.content)
    await update.message.reply_text(format_for_telegram(clean_message), parse_mode=ParseMode.HTML)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--send-daily", action="store_true", help="Send daily interview question and update state.")
    args = parser.parse_args()

    if args.send_daily:
        asyncio.run(send_daily_prompt())
    else:
        logger.info("Starting bot in polling mode...")
        app = Application.builder().token(TELEGRAM_TOKEN).request(HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)).build()
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("done", done))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        app.run_polling()


if __name__ == "__main__":
    main()