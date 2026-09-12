from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# Your Telegram bot token
TOKEN = "8909415161:AAFXoX96WYoOoruM6MvpJ7oz-hq_YAPXDTE"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Hi! I'm your technical interview coach."
    )


async def chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Your chat ID is: {update.effective_chat.id}"
    )


app = Application.builder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("chatid", chatid))

print("Bot is running...")

app.run_polling()
