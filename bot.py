            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_chat=True,
        per_user=True,
        name="button_flow",
    )


def history_import_conversation():
    return ConversationHandler(
        entry_points=[CommandHandler("importhistory", import_history_start)],
        states={
            HISTORY_IMPORT_WAIT: [
                MessageHandler(filters.Document.ALL, import_history_file),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_chat=True,
        per_user=True,
        name="history_import_flow",
    )


# ----------------------------- application -----------------------------------

async def post_init(application: Application):
    async with data_lock:
        load_data()

    commands = [
        BotCommand("start", "Admin dashboard"),
        BotCommand("admin", "Admin dashboard"),
        BotCommand("post", "Create a channel post"),
        BotCommand("setphoto", "Set default photo"),
        BotCommand("removephoto", "Remove default photo"),
        BotCommand("buttons", "Manage default buttons and URLs"),
        BotCommand("importhistory", "Import Telegram Desktop HTML history"),
        BotCommand("resetbuttons", "Reset default buttons"),
        BotCommand("status", "Check bot/channel status"),
        BotCommand("setnumber", "Set next post number"),
        BotCommand("cancel", "Cancel current action"),
    ]
    await application.bot.set_my_commands(commands)

    try:
        me = await application.bot.get_me()
        member = await application.bot.get_chat_member(CHANNEL_ID, me.id)
        log.info("Channel membership: %s", member.status)
    except TelegramError as exc:
        log.warning("Channel check failed: %s", exc)

    log.info("Bot started. Channel=%r Admins=%s", CHANNEL_ID, sorted(ADMIN_IDS))


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Unhandled exception", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ Unexpected error. Use /cancel and try again."
            )
        except TelegramError:
            pass


def build_app() -> Application:
    # PTB 22.x API. Do not create Updater manually.
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .concurrent_updates(False)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", start))
    app.add_handler(CommandHandler("removephoto", remove_photo))
    app.add_handler(CommandHandler("resetbuttons", reset_buttons))
    app.add_handler(CommandHandler("status", status))

    app.add_handler(post_conversation())
    app.add_handler(photo_conversation())
    app.add_handler(number_conversation())
    app.add_handler(button_conversation())
    app.add_handler(history_import_conversation())

    # Dashboard callbacks which are not conversation entry points.
    app.add_handler(
        CallbackQueryHandler(
            dashboard_callback,
            pattern=r"^dash:(remove_photo|status)$",
        )
    )

    app.add_handler(CommandHandler("cancel", cancel), group=10)
    app.add_error_handler(error_handler)
    return app


def main():
    app = build_app()
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
