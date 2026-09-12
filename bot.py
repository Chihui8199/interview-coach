import os
import re
from pathlib import Path

from openai import OpenAI
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ============================================================
# PATHS & CONFIGURATION
# ============================================================

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]

BASE_DIR = Path(__file__).parent

SKILLS_PATH = BASE_DIR / "skills" / "interview-coach.md"
STATE_PATH = BASE_DIR / "skills" / "state.md"

client = OpenAI(
    api_key=GROQ_API_KEY,
    base_url="https://api.groq.com/openai/v1",
)

MODEL = "openai/gpt-oss-120b"


# ============================================================
# STATE & PROMPT HELPERS
# ============================================================

def read_file(path: Path) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def save_study_state(content: str):
    STATE_PATH.write_text(content, encoding="utf-8")


def build_system_prompt() -> str:
    instructions = read_file(SKILLS_PATH)
    current_state = read_file(STATE_PATH)

    return f"""
{instructions}

============================================================
CURRENT CANDIDATE STUDY STATE (skills/state.md)
============================================================
{current_state}
"""


def extract_and_save_state(ai_response: str) -> str:
    pattern = r"<STUDY_STATE>(.*?)</STUDY_STATE>"
    match = re.search(pattern, ai_response, re.DOTALL)

    if match:
        updated_state = match.group(1).strip()
        save_study_state(updated_state)
        cleaned_text = re.sub(pattern, "", ai_response, flags=re.DOTALL).strip()
        return cleaned_text

    return ai_response


def format_for_telegram(text: str) -> str:
    """Sanitizes AI responses and converts Markdown/unsupported tags to Telegram HTML."""
    if not text:
        return ""

    # Replace HTML line break tags with actual newline characters
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)

    lines = text.split("\n")
    cleaned_lines = []

    for line in lines:
        stripped = line.strip()

        # Convert lingering ### Headers to <b>Bold Text</b>
        if stripped.startswith("#"):
            header_text = re.sub(r"^#+\s*", "", stripped)
            cleaned_lines.append(f"\n<b>{header_text}</b>")
            continue

        # Convert lingering Markdown tables to bullet points
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

    # Convert standard Markdown syntax into Telegram-compatible HTML tags
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)

    # Clean up redundant line breaks
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


# ============================================================
# IN-MEMORY CHAT CONVERSATION
# ============================================================

conversations = {}


# ============================================================
# TELEGRAM HANDLERS
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    system_prompt = build_system_prompt()
    conversations[chat_id] = [{"role": "system", "content": system_prompt}]

    initial_prompt = "Start the interview now with the highest priority question based on skills/state.md."
    conversations[chat_id].append({"role": "user", "content": initial_prompt})

    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=conversations[chat_id],
            temperature=0.5,
        )
        raw_response = response.choices[0].message.content
        conversations[chat_id].append({"role": "assistant", "content": raw_response})

        clean_message = extract_and_save_state(raw_response)
        formatted_message = format_for_telegram(clean_message)

        await update.message.reply_text(
            f"👋 <b>Interview Session Started.</b>\n\n{formatted_message}",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        print("Error during start:", e)
        await update.message.reply_text("⚠️ Failed to start interview session. Check logs.")


async def done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    if chat_id not in conversations:
        await update.message.reply_text("No active session running.")
        return

    wrap_up_prompt = (
        "The user issued /done. Summarize the session performance, update "
        "skills/state.md with all new findings, and wrap up."
    )
    conversations[chat_id].append({"role": "user", "content": wrap_up_prompt})

    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=conversations[chat_id],
            temperature=0.5,
        )
        raw_response = response.choices[0].message.content

        clean_message = extract_and_save_state(raw_response)
        formatted_message = format_for_telegram(clean_message)

        await update.message.reply_text(
            f"🏁 <b>Session Complete!</b>\n\n{formatted_message}",
            parse_mode=ParseMode.HTML,
        )

        del conversations[chat_id]

    except Exception as e:
        print("Error during /done:", e)
        await update.message.reply_text("⚠️ Could not process session completion.")


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in conversations:
        del conversations[chat_id]
    await update.message.reply_text("🔄 Local conversation memory reset.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_message = update.message.text

    if chat_id not in conversations:
        system_prompt = build_system_prompt()
        conversations[chat_id] = [{"role": "system", "content": system_prompt}]

    conversations[chat_id].append({"role": "user", "content": user_message})

    # Refresh system prompt context with updated state before calling API
    conversations[chat_id][0] = {"role": "system", "content": build_system_prompt()}

    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=conversations[chat_id],
            temperature=0.5,
        )
        raw_response = response.choices[0].message.content
        conversations[chat_id].append({"role": "assistant", "content": raw_response})

        clean_message = extract_and_save_state(raw_response)
        formatted_message = format_for_telegram(clean_message)

        await update.message.reply_text(
            formatted_message,
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        print("Error during chat execution:", e)
        await update.message.reply_text("⚠️ An error occurred processing your response.")


# ============================================================
# MAIN
# ============================================================

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("done", done))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    print("🤖 Interview Coach running with openai/gpt-oss-120b and skills/state.md...")
    app.run_polling()


if __name__ == "__main__":
    main()