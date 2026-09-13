import asyncio
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
import modal
from fastapi import Request
from fastapi.responses import JSONResponse
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
# LOGGING & ENVIRONMENT RESOLUTION
# ============================================================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Populate os.environ from a local .env file, if one exists. This is
# intentionally NOT gated behind modal.is_local() -- that check does not
# reliably return True at this point in the import lifecycle when the
# module is loaded via `modal run`/`modal deploy`. Calling load_dotenv()
# unconditionally is safe: by default it never overrides a variable that's
# already set, so in the cloud (where Modal's secret already populated
# os.environ before this code runs) it's just a harmless no-op.
try:
    from dotenv import load_dotenv

    _dotenv_loaded = load_dotenv()
    logger.info(f"load_dotenv() ran; found a .env file: {_dotenv_loaded}")
except ImportError:
    logger.warning(
        "python-dotenv not installed; relying on already-exported "
        "environment variables."
    )


def get_modal_secrets():
    """Always resolve to the named Modal secret.

    Secret.from_name(...) is a LAZY reference: Modal's backend looks up the
    secret's current value each time a container actually starts, wherever
    that happens. This is correct for both `modal run` and `modal deploy` --
    unlike branching on modal.is_local() here, which would only reflect
    whatever machine happened to import this module (almost always your
    laptop, since that's where the CLI parses the file), not where the
    function actually executes.
    """
    return [modal.Secret.from_name("interview-coach-secrets")]


state_volume = modal.Volume.from_name("interview-coach-state", create_if_missing=True)

image = (
    modal.Image.debian_slim()
    .pip_install_from_requirements("requirements.txt")
    .pip_install("fastapi[standard]")
    .add_local_file("skills/interview.md", "/root/skills/interview.md")
    .add_local_file("skills/telegram.md", "/root/skills/telegram.md")
)

app = modal.App(name="telegram-interview-coach", image=image)

MODEL = "openai/gpt-oss-120b"

# Dynamic Storage Path: Local folder on Mac vs Modal Volume in Cloud
if modal.is_local():
    VOLUME_DIR = Path("./local_dev_state")
    INTERVIEW_SKILL_PATH = Path("./skills/interview.md")
    TELEGRAM_SKILL_PATH = Path("./skills/telegram.md")
else:
    VOLUME_DIR = Path("/root/skills/state_vol")
    INTERVIEW_SKILL_PATH = Path("/root/skills/interview.md")
    TELEGRAM_SKILL_PATH = Path("/root/skills/telegram.md")

DB_PATH = VOLUME_DIR / "state.db"
BACKUP_DIR = VOLUME_DIR / "backups"

conversations = {}


def _read_skill_file(path: Path, label: str) -> str:
    """Reads a skill markdown file for inclusion in the system prompt.
    Returns an empty string (and logs a warning) if missing, so a missing
    skill file never crashes the bot -- it just runs without that context.
    """
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        logger.warning(f"{label} not found at {path}; continuing without it.")
        return ""
    except Exception as e:
        logger.error(f"Failed to read {label} at {path}: {e}")
        return ""


def load_interview_skill() -> str:
    """Reads skills/interview.md, the interview-coaching rules."""
    return _read_skill_file(INTERVIEW_SKILL_PATH, "interview.md")


def load_telegram_skill() -> str:
    """Reads skills/telegram.md, the Telegram output-formatting rules."""
    return _read_skill_file(TELEGRAM_SKILL_PATH, "telegram.md")


def get_env_var(var_name: str) -> str:
    """Reads an environment variable from the process environment.

    Locally this comes from your shell or the .env file loaded above.
    In the cloud this comes from whatever Secret Modal injected when the
    container started. Values are stripped to avoid trailing-newline /
    whitespace issues from copy-pasted tokens causing silent auth failures.
    """
    val = os.environ.get(var_name, "").strip()
    if not val:
        logger.error(f"Missing required environment variable: {var_name}")
    return val


def require_env_var(var_name: str) -> str:
    """Like get_env_var, but raises loudly instead of returning "" ."""
    val = get_env_var(var_name)
    if not val:
        raise RuntimeError(
            f"{var_name} is empty. Locally: check your .env file. "
            f"In the cloud: check `modal secret list` / the "
            f"'interview-coach-secrets' secret in your Modal dashboard."
        )
    return val

# ============================================================
# SQLITE DATABASE & BACKUP MANAGEMENT
# ============================================================
def commit_volume_if_cloud():
    """Safely commits changes to Modal Volume when running in the cloud."""
    if not modal.is_local():
        try:
            state_volume.commit()
            logger.info("Committed changes to Modal Volume.")
        except Exception as e:
            logger.error(f"Failed to commit to Modal Volume: {e}")


def init_db():
    """Initializes the SQLite database.

    Two tables, two different jobs:
    - study_state: current snapshot only. One row per topic, overwritten on
      every /done. This is the compact table sent to the AI as prompt
      context -- it should stay small.
    - study_history: append-only log. A new row every time /done records an
      assessment for a topic, timestamped, never overwritten. This is your
      actual progress-over-time record (e.g. "how has my Kafka understanding
      evolved over the last month") -- it is NOT sent to the AI on every
      message, so it can grow indefinitely without bloating prompts.
    """
    VOLUME_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS study_state (
            topic TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            notes TEXT,
            priority INTEGER DEFAULT 1,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS study_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic TEXT NOT NULL,
            status TEXT NOT NULL,
            notes TEXT,
            priority INTEGER DEFAULT 1,
            recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    conn.close()
    commit_volume_if_cloud()


def backup_sqlite_db(retention_days: int = 30):
    """Creates a timestamped backup copy of state.db and removes backups older than retention_days."""
    if not DB_PATH.exists():
        logger.warning("No database found to backup.")
        return

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    today = datetime.now().strftime("%Y-%m-%d")
    backup_file = BACKUP_DIR / f"state_backup_{today}.db"

    try:
        src = sqlite3.connect(DB_PATH)
        dst = sqlite3.connect(backup_file)

        with dst:
            src.backup(dst)

        src.close()
        dst.close()
        logger.info(f"Successfully created database backup at {backup_file}")
    except Exception as e:
        logger.error(f"Failed to create database backup: {e}")
        return

    cutoff_date = datetime.now() - timedelta(days=retention_days)
    for old_backup in BACKUP_DIR.glob("state_backup_*.db"):
        try:
            date_str = old_backup.stem.replace("state_backup_", "")
            file_date = datetime.strptime(date_str, "%Y-%m-%d")
            if file_date < cutoff_date:
                old_backup.unlink()
                logger.info(f"Pruned old backup file: {old_backup.name}")
        except ValueError:
            continue

    commit_volume_if_cloud()


# How many non-mastered topics to include in the AI's prompt context. Keeps
# the prompt small and focused even after months of accumulated topics.
DB_SUMMARY_LIMIT = 10


def get_db_summary() -> str:
    """Fetches a compact, prioritized summary of study_state for prompt
    context. Deliberately excludes 'mastered' topics from the detailed list
    (they'd just be dead weight in every future prompt) and caps the count,
    so this stays small no matter how many topics accumulate over weeks.
    Also surfaces how many days it's been since each topic was last
    reviewed, so the AI can tell "mastered last week" from "mastered two
    months ago and possibly rusty by now" instead of treating both as
    equally current.
    """
    if not DB_PATH.exists():
        init_db()

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT topic, status, notes, priority,
               CAST(julianday('now') - julianday(updated_at) AS INTEGER) AS days_ago
        FROM study_state
        WHERE status != 'mastered'
        ORDER BY priority DESC, updated_at ASC
        LIMIT ?
        """,
        (DB_SUMMARY_LIMIT,),
    )
    active_rows = cursor.fetchall()

    cursor.execute("SELECT COUNT(*) FROM study_state WHERE status = 'mastered'")
    mastered_count = cursor.fetchone()[0]
    conn.close()

    if not active_rows and mastered_count == 0:
        return "No study state topics recorded yet in the database."

    summary = ["Current Study State (SQLite):"]
    for topic, status, notes, priority, days_ago in active_rows:
        when = "today" if days_ago == 0 else f"{days_ago}d ago"
        summary.append(f"- **{topic}** | Status: {status} | Priority: {priority} | Last reviewed: {when} | Notes: {notes}")

    if mastered_count:
        summary.append(f"({mastered_count} other topic(s) already marked mastered, omitted here for brevity.)")

    return "\n".join(summary)


def save_parsed_state(state_json_str: str):
    """Parses JSON output from the AI, upserts the CURRENT snapshot into
    study_state, and additionally appends a permanent record to
    study_history so the assessment for this date is never lost even after
    study_state gets overwritten by a later session.
    """
    try:
        items = json.loads(state_json_str)
        if not isinstance(items, list):
            items = [items]

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        for item in items:
            topic = item.get("topic")
            status = item.get("status", "in_progress")
            notes = item.get("notes", "")
            priority = item.get("priority", 1)

            cursor.execute(
                """
                INSERT INTO study_state (topic, status, notes, priority, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(topic) DO UPDATE SET
                    status=excluded.status,
                    notes=excluded.notes,
                    priority=excluded.priority,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (topic, status, notes, priority),
            )
            cursor.execute(
                """
                INSERT INTO study_history (topic, status, notes, priority, recorded_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (topic, status, notes, priority),
            )

        conn.commit()
        conn.close()
        commit_volume_if_cloud()
        logger.info("Successfully updated SQLite state (snapshot + history).")
    except Exception as e:
        logger.error(f"Failed to save state to SQLite: {e}")


def extract_and_update_state(text: str) -> str:
    """Extracts a <STUDY_STATE_JSON> block from the AI's reply, writes it to
    SQLite (so the NEXT prompt to the AI has fresh context), and strips the
    tag out of the text before it's sent to Telegram.
    """
    match = re.search(r"<STUDY_STATE_JSON>(.*?)</STUDY_STATE_JSON>", text, re.DOTALL)
    if match:
        json_data = match.group(1).strip()
        save_parsed_state(json_data)
        text = re.sub(r"<STUDY_STATE_JSON>.*?</STUDY_STATE_JSON>", "", text, flags=re.DOTALL).strip()
    return text


def build_system_prompt() -> str:
    """Base system prompt used for /start, regular messages, and the daily
    8 AM reminder. Includes the interview coaching rules, Telegram
    formatting rules, and the current SQLite study-state summary as
    read-only context -- but does NOT ask the AI to emit JSON. State is
    only written on /done (see build_done_prompt_addition).
    """
    interview_context = load_interview_skill()
    telegram_context = load_telegram_skill()
    db_context = get_db_summary()

    interview_section = interview_context or "You are an elite technical interview coach."
    telegram_section = f"\n{telegram_context}\n" if telegram_context else ""

    return f"""{interview_section}
{telegram_section}
{db_context}
"""


def build_done_prompt_addition() -> str:
    """Extra instruction appended only when the user issues /done. Asks for
    a session summary AND a <STUDY_STATE_JSON> block, which is parsed and
    saved to SQLite before the summary is sent back.
    """
    return """The user issued /done. Provide a concise summary of this session's
progress, following the interview style and Telegram formatting rules above.
Then, at the very end of your response, output JSON enclosed strictly in
<STUDY_STATE_JSON> tags summarizing the status of each topic covered this
session, so it can be saved to SQLite for next time.

Example Tag Format:
<STUDY_STATE_JSON>
[
  {"topic": "Kafka Consumer Groups", "status": "needs_review", "notes": "Confused rebalancing with partition assignment", "priority": 3}
]
</STUDY_STATE_JSON>
"""


def format_for_telegram(text: str) -> str:
    """Passes the AI's text through unchanged for Telegram's HTML parse mode.

    We intentionally do NOT escape <, >, & here. telegram.md instructs the
    AI to use a small whitelist of real HTML tags (<b>, <i>, <code>, <pre>)
    for formatting, and those need to reach Telegram unescaped so they
    actually render as bold/italic/etc. instead of showing up as literal
    text. The skill file is the guardrail on which tags get used, not this
    function.
    """
    return text

# ============================================================
# TELEGRAM HANDLERS
# ============================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message:
        return

    chat_id = update.effective_chat.id
    logger.info(f"Session started for chat_id: {chat_id}")

    client = OpenAI(api_key=require_env_var("GROQ_API_KEY"), base_url="https://api.groq.com/openai/v1", timeout=30.0)
    conversations[chat_id] = [{"role": "system", "content": build_system_prompt()}]
    conversations[chat_id].append(
        {"role": "user", "content": "Start the interview now with highest priority question from SQLite state."}
    )

    response = client.chat.completions.create(model=MODEL, messages=conversations[chat_id], temperature=0.5)
    raw_content = response.choices[0].message.content
    conversations[chat_id].append({"role": "assistant", "content": raw_content})

    await message.reply_text(format_for_telegram(raw_content), parse_mode=ParseMode.HTML)


async def done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message:
        return

    chat_id = update.effective_chat.id
    if chat_id not in conversations:
        await message.reply_text("No active session.")
        return

    client = OpenAI(api_key=require_env_var("GROQ_API_KEY"), base_url="https://api.groq.com/openai/v1", timeout=30.0)
    conversations[chat_id].append({"role": "user", "content": build_done_prompt_addition()})

    response = client.chat.completions.create(model=MODEL, messages=conversations[chat_id], temperature=0.5)
    raw_content = response.choices[0].message.content
    # Only /done parses and writes the study-state JSON to SQLite.
    clean_message = extract_and_update_state(raw_content)

    await message.reply_text(f"🏁 <b>Session Complete!</b>\n\n{format_for_telegram(clean_message)}", parse_mode=ParseMode.HTML)
    # Drop the whole conversation so tomorrow's 8 AM reminder (and the next
    # /start) begins fresh instead of an ever-growing message history --
    # the SQLite state we just saved is what carries context forward.
    del conversations[chat_id]


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message or not message.text:
        return

    chat_id = update.effective_chat.id
    client = OpenAI(api_key=require_env_var("GROQ_API_KEY"), base_url="https://api.groq.com/openai/v1", timeout=30.0)

    if chat_id not in conversations:
        conversations[chat_id] = [{"role": "system", "content": build_system_prompt()}]

    conversations[chat_id].append({"role": "user", "content": message.text})
    conversations[chat_id][0] = {"role": "system", "content": build_system_prompt()}

    response = client.chat.completions.create(model=MODEL, messages=conversations[chat_id], temperature=0.5)
    raw_content = response.choices[0].message.content
    conversations[chat_id].append({"role": "assistant", "content": raw_content})

    await message.reply_text(format_for_telegram(raw_content), parse_mode=ParseMode.HTML)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception handling update:", exc_info=context.error)


def build_telegram_application(telegram_token: str) -> Application:
    """Builds a python-telegram-bot Application with all handlers attached.
    Shared by both the local-dev polling path (main()) and the production
    webhook path (telegram_webhook), so handler wiring only lives in one place.
    """
    application = Application.builder().token(telegram_token).connect_timeout(30.0).read_timeout(30.0).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("done", done))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_error_handler(error_handler)
    return application


def main():
    """Local-only dev/testing entrypoint using long-polling. NOT used in
    production -- the deployed bot uses the telegram_webhook endpoint below,
    which doesn't need a permanently-running process.
    """
    init_db()
    telegram_token = require_env_var("TELEGRAM_BOT_TOKEN")
    logger.info("Starting Telegram bot worker (local polling mode)...")

    # Guarantee a usable event loop for this thread. If something earlier
    # in this process (e.g. daily_reminder_8am's asyncio.run() call during
    # test_local()) already ran and cleared the thread's event loop,
    # ApplicationBuilder's internal asyncio.Queue() construction would
    # otherwise fail with "There is no current event loop".
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    application = build_telegram_application(telegram_token)
    application.run_polling()

# ============================================================
# PRODUCTION WEBHOOK ENDPOINT
# ============================================================
# Telegram calls this URL directly whenever a user sends a message -- no
# always-on process needed, and no 24-hour container timeout to worry about.
# Modal spins up a container on demand for each incoming update and can
# scale down to zero between messages.
@app.function(
    secrets=get_modal_secrets(),
    volumes={"/root/skills/state_vol": state_volume},
)
@modal.fastapi_endpoint(method="POST")
async def telegram_webhook(payload: dict, request: Request):
    # Verify the request actually came from Telegram, not some random caller
    # who guessed the URL. Telegram sends this header on every webhook call
    # when a secret_token was set via set_webhook (see register_webhook()).
    expected_secret = get_env_var("TELEGRAM_WEBHOOK_SECRET")
    received_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if expected_secret and received_secret != expected_secret:
        logger.warning("Rejected webhook call with invalid/missing secret token.")
        return JSONResponse(status_code=401, content={"ok": False, "error": "unauthorized"})

    init_db()
    telegram_token = require_env_var("TELEGRAM_BOT_TOKEN")
    application = build_telegram_application(telegram_token)

    await application.initialize()
    try:
        update = Update.de_json(payload, application.bot)
        await application.process_update(update)
    finally:
        await application.shutdown()

    return {"ok": True}


@app.local_entrypoint()
def register_webhook():
    """Run once after `modal deploy` to point Telegram at the deployed
    webhook URL: `modal run bot.py::register_webhook`
    """
    url = telegram_webhook.get_web_url()
    if not url:
        print("Could not resolve the webhook URL -- did you run `modal deploy bot.py` first?")
        return

    secret = get_env_var("TELEGRAM_WEBHOOK_SECRET")
    if not secret:
        print(
            "WARNING: TELEGRAM_WEBHOOK_SECRET is not set. The webhook will still "
            "work, but anyone who finds the URL could send fake updates to your "
            "bot. Set TELEGRAM_WEBHOOK_SECRET in your .env and Modal secret, then "
            "re-run this command."
        )

    from telegram import Bot

    async def _register():
        bot = Bot(token=require_env_var("TELEGRAM_BOT_TOKEN"))
        await bot.set_webhook(url=url, secret_token=secret or None)
        info = await bot.get_webhook_info()
        print(f"Webhook registered: {info.url}")
        print(f"Pending update count: {info.pending_update_count}")

    asyncio.run(_register())

# ============================================================
# MODAL CLOUD CRON SCHEDULER & WORKER
# ============================================================
@app.cls(
    secrets=get_modal_secrets(),
    volumes={"/root/skills/state_vol": state_volume},
    timeout=86400,
    scaledown_window=3600,
)
class TelegramBot:
    @modal.enter()
    def start_up(self):
        init_db()

    @modal.method()
    def run(self):
        main()


# Scheduled 8:00 AM Daily Reminder & Backup Trigger
@app.function(
    secrets=get_modal_secrets(),
    volumes={"/root/skills/state_vol": state_volume},
    # 8:00 AM SGT = 00:00 UTC (Singapore is UTC+8, no daylight saving).
    # modal.Cron schedules always run in UTC, not local time.
    schedule=modal.Cron("0 0 * * *"),
)
def daily_reminder_8am():
    """Performs daily SQLite backup and pushes 8:00 AM interview prompt to Telegram."""
    logger.info("Executing 8:00 AM daily job (Backup + Reminder)...")
    init_db()

    backup_sqlite_db(retention_days=30)

    chat_id = require_env_var("TELEGRAM_CHAT_ID")
    telegram_token = require_env_var("TELEGRAM_BOT_TOKEN")
    groq_api_key = require_env_var("GROQ_API_KEY")

    client = OpenAI(api_key=groq_api_key, base_url="https://api.groq.com/openai/v1", timeout=30.0)
    prompt = f"{build_system_prompt()}\n\nGenerate a single concise 8 AM interview warm-up question based on the highest priority topic."

    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.5,
    )
    question = response.choices[0].message.content

    bot_app = Application.builder().token(telegram_token).build()
    asyncio.run(
        bot_app.bot.send_message(
            chat_id=chat_id,
            text=f"☀️ <b>8:00 AM Interview Warm-Up</b>\n\n{format_for_telegram(question)}",
            parse_mode=ParseMode.HTML,
        )
    )
    logger.info("Sent 8:00 AM daily question successfully.")

# ============================================================
# LOCAL TESTING ENTRYPOINT
# ============================================================
@app.local_entrypoint()
def test_local():
    print("=== 1. INITIALIZING LOCAL DB ===")
    init_db()

    print("\n=== 2. TESTING 8 AM DAILY REMINDER & BACKUP ===")
    daily_reminder_8am.local()

    print("\n=== 3. CHECKING DB SUMMARY ===")
    print(get_db_summary())

    print("\n=== 4. STARTING TELEGRAM BOT LOCAL WORKER ===")
    print("Press Ctrl+C to stop testing.")
    main()