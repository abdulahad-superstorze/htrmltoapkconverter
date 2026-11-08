"""Telegram handlers: commands, buttons, the upload flow and build orchestration.

The flow is a small state machine stored in the user's session:

    name -> website -> icon -> review -> building -> (done | back to review)

Because every message is routed by the session step (instead of Telegram
"next step handlers"), commands such as /cancel and /start always work.
"""
import functools
import threading
import time

from telebot import types

from api_client import BuildCancelled, BuildError, request_build
from config import (
    BRAND_NAME,
    BUILD_COOLDOWN_SECONDS,
    BUILD_RETRIES,
    MAX_HTML_SIZE,
    MAX_ICON_SIZE,
    MAX_TOTAL_FILES,
    MAX_ZIP_SIZE,
    MAX_CONCURRENT_BUILDS,
    SESSION_TTL_SECONDS,
    STATUS_REFRESH_SECONDS,
)
from core import bot, logger
from sessions import SessionStore
from ui import (
    ABOUT_TEXT,
    BUILD_BUSY_TEXT,
    CANCELLED_BUILD_TEXT,
    CANCELLED_TEXT,
    CRASH_TEXT,
    EXPIRED_TEXT,
    HELP_TEXT,
    HOME_TEXT,
    IDLE_HINT_TEXT,
    SESSION_EXPIRED_ALERT,
    STALE_BUTTON_TEXT,
    UNKNOWN_COMMAND_TEXT,
    apk_caption,
    build_complete_text,
    build_failed_text,
    build_status_text,
    edit_quiet,
    error_screen,
    kb_back,
    kb_back_to_review,
    kb_cancel,
    kb_cancel_build,
    kb_confirm,
    kb_failed,
    kb_main_menu,
    prompt_icon,
    prompt_name,
    prompt_website,
    render,
    review_text,
    send_with_banner,
    strip_markup,
    welcome_text,
)
from validators import (
    file_extension,
    icon_filename,
    validate_app_name,
    validate_icon,
    validate_website,
)

store = SessionStore()
build_slots = threading.BoundedSemaphore(MAX_CONCURRENT_BUILDS)

MESSAGE_TYPES = ["text", "document", "photo", "sticker", "video", "audio", "voice", "animation", "video_note"]


# --------------------------------------------------------------------------
# Safety net
# --------------------------------------------------------------------------
def _chat_id_of(obj):
    if isinstance(obj, types.CallbackQuery):
        return obj.message.chat.id
    return obj.chat.id


def safe(func):
    """Log any unexpected error and tell the user instead of failing silently."""

    @functools.wraps(func)
    def wrapper(obj, *args, **kwargs):
        try:
            return func(obj, *args, **kwargs)
        except Exception:
            logger.exception("Unhandled error in %s", func.__name__)
            try:
                if isinstance(obj, types.CallbackQuery):
                    bot.answer_callback_query(obj.id)
                bot.send_message(_chat_id_of(obj), CRASH_TEXT, reply_markup=kb_main_menu())
            except Exception:
                pass

    return wrapper


def answer(call, text=None, alert=False):
    try:
        bot.answer_callback_query(call.id, text, show_alert=alert)
    except Exception:
        pass


# --------------------------------------------------------------------------
# Screen helpers
# --------------------------------------------------------------------------
def advance(session, text, markup=None):
    """Send the next step as a new message (it appears below the user's reply)."""
    previous = session.ui_message
    session.ui_message = bot.send_message(session.chat_id, text, reply_markup=markup)
    if previous is not None:
        strip_markup(previous)


def show_in_place(session, source, text, markup=None):
    session.ui_message = render(source, text, markup)


def flow_kb(session):
    return kb_back_to_review() if session.editing else kb_cancel()


def show_review(session, source=None):
    session.step = "review"
    session.editing = False
    text = review_text(session)
    if source is not None:
        show_in_place(session, source, text, kb_confirm())
    else:
        advance(session, text, kb_confirm())


# --------------------------------------------------------------------------
# Telegram file download
# --------------------------------------------------------------------------
class FileTooLarge(Exception):
    pass


class DownloadFailed(Exception):
    pass


def download_telegram_file(file_id, max_size):
    try:
        info = bot.get_file(file_id)
        if info.file_size and info.file_size > max_size:
            raise FileTooLarge()
        content = bot.download_file(info.file_path)
    except FileTooLarge:
        raise
    except Exception:
        # Never show raw exceptions to users: they can contain the bot token in URLs.
        logger.exception("Telegram file download failed")
        raise DownloadFailed()
    if len(content) > max_size:
        raise FileTooLarge()
    return content


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------
def _is_building(chat_id):
    session = store.get(chat_id)
    return bool(session and session.building)


@bot.message_handler(commands=["start", "menu"])
@safe
def cmd_start(message):
    chat_id = message.chat.id
    if _is_building(chat_id):
        bot.send_message(chat_id, BUILD_BUSY_TEXT)
        return
    session = store.discard(chat_id)
    if session is not None and session.ui_message is not None:
        strip_markup(session.ui_message)
    first_name = getattr(message.from_user, "first_name", None)
    send_with_banner(chat_id, welcome_text(first_name), kb_main_menu())


@bot.message_handler(commands=["help"])
@safe
def cmd_help(message):
    bot.send_message(message.chat.id, HELP_TEXT, reply_markup=kb_back())


@bot.message_handler(commands=["about"])
@safe
def cmd_about(message):
    bot.send_message(message.chat.id, ABOUT_TEXT, reply_markup=kb_back())


@bot.message_handler(commands=["newapk"])
@safe
def cmd_newapk(message):
    chat_id = message.chat.id
    if _is_building(chat_id):
        bot.send_message(chat_id, BUILD_BUSY_TEXT)
        return
    start_flow(chat_id)


@bot.message_handler(commands=["cancel"])
@safe
def cmd_cancel(message):
    chat_id = message.chat.id
    session = cancel_session(chat_id)
    if session is None:
        bot.send_message(chat_id, "Nothing to cancel.", reply_markup=kb_main_menu())
    elif session.building:
        bot.send_message(chat_id, "Choose an action below:", reply_markup=kb_main_menu())
    else:
        bot.send_message(chat_id, CANCELLED_TEXT, reply_markup=kb_main_menu())


def cancel_session(chat_id, source=None):
    """Cancel the user's session (and any running build). Returns the old session."""
    session = store.discard(chat_id)
    if session is None:
        return None
    with session.ui_lock:
        if source is not None:
            text = CANCELLED_BUILD_TEXT if session.building else CANCELLED_TEXT
            render(source, text, kb_main_menu())
        elif session.ui_message is not None:
            if session.building:
                render(session.ui_message, CANCELLED_BUILD_TEXT, None)
            else:
                strip_markup(session.ui_message)
    return session


# --------------------------------------------------------------------------
# Buttons
# --------------------------------------------------------------------------
def start_flow(chat_id, source=None):
    session = store.create(chat_id)
    text = prompt_name()
    if source is not None:
        show_in_place(session, source, text, kb_cancel())
    else:
        session.ui_message = bot.send_message(chat_id, text, reply_markup=kb_cancel())


def cb_home(call):
    chat_id = call.message.chat.id
    if _is_building(chat_id):
        answer(call, "A build is in progress. Use Cancel to stop it.", alert=True)
        return
    store.discard(chat_id)
    answer(call)
    render(call.message, HOME_TEXT, kb_main_menu())


def cb_help(call):
    answer(call)
    render(call.message, HELP_TEXT, kb_back())


def cb_about(call):
    answer(call)
    render(call.message, ABOUT_TEXT, kb_back())


def cb_new(call):
    chat_id = call.message.chat.id
    if _is_building(chat_id):
        answer(call, "A build is in progress. Use Cancel to stop it.", alert=True)
        return
    answer(call)
    start_flow(chat_id, source=call.message)


def cb_cancel(call):
    chat_id = call.message.chat.id
    answer(call, "Cancelled")
    session = cancel_session(chat_id, source=call.message)
    if session is None:
        render(call.message, CANCELLED_TEXT, kb_main_menu())


def _edit_item(call, step, text):
    session = store.get(call.message.chat.id)
    if session is None:
        answer(call, SESSION_EXPIRED_ALERT)
        return
    with session.lock:
        if session.step != "review" or session.building:
            answer(call, STALE_BUTTON_TEXT)
            return
        session.touch()
        session.editing = True
        session.step = step
        answer(call)
        show_in_place(session, call.message, text, kb_back_to_review())


def cb_rename(call):
    _edit_item(call, "name", prompt_name(rename=True))


def cb_website(call):
    _edit_item(call, "website", prompt_website())


def cb_icon(call):
    _edit_item(call, "icon", prompt_icon())


def cb_review(call):
    session = store.get(call.message.chat.id)
    if session is None:
        answer(call, SESSION_EXPIRED_ALERT)
        return
    with session.lock:
        if session.building or not (session.app_name and session.website and session.icon):
            answer(call, STALE_BUTTON_TEXT)
            return
        session.touch()
        answer(call)
        show_review(session, source=call.message)


def cb_build(call):
    chat_id = call.message.chat.id
    session = store.get(chat_id)
    if session is None or not session.icon or not session.website:
        answer(call, SESSION_EXPIRED_ALERT, alert=True)
        return
    with session.lock:
        if session.building:
            answer(call, "A build is already running.")
            return
        if session.step != "review":
            answer(call, STALE_BUTTON_TEXT)
            return
        wait = store.cooldown_remaining(chat_id, BUILD_COOLDOWN_SECONDS)
        if wait:
            answer(call, f"Please wait {wait}s before starting another build.", alert=True)
            return
        store.mark_build(chat_id)
        session.touch()
        session.step = "building"
        session.building = True
        session.cancelled = False
        session.ui_message = call.message
    answer(call, "Build started")
    threading.Thread(target=run_build, args=(session,), daemon=True, name=f"build-{chat_id}").start()


CALLBACKS = {
    "menu:home": cb_home,
    "menu:help": cb_help,
    "menu:about": cb_about,
    "menu:new": cb_new,
    "flow:cancel": cb_cancel,
    "flow:rename": cb_rename,
    "flow:website": cb_website,
    "flow:icon": cb_icon,
    "flow:review": cb_review,
    "flow:build": cb_build,
}


@bot.callback_query_handler(func=lambda call: True)
@safe
def on_callback(call):
    # Messages older than ~48h arrive as "inaccessible" (no content) and cannot be edited.
    if getattr(call.message, "content_type", None) is None:
        answer(call, "This message is too old. Please start again.", alert=True)
        send_with_banner(call.message.chat.id, HOME_TEXT, kb_main_menu())
        return
    handler = CALLBACKS.get(call.data)
    if handler is None:
        answer(call, STALE_BUTTON_TEXT)
        return
    handler(call)


# --------------------------------------------------------------------------
# Flow steps (driven by session.step)
# --------------------------------------------------------------------------
def handle_name(session, message):
    name = (message.text or "").strip() if message.content_type == "text" else ""
    error = validate_app_name(name)
    if error:
        advance(session, error_screen(0, "Invalid app name", error) + "\n\nPlease try again:", flow_kb(session))
        return
    session.app_name = name
    if session.editing:
        show_review(session)
        return
    session.step = "website"
    advance(session, prompt_website(name), kb_cancel())


def handle_website(session, message):
    doc = message.document if message.content_type == "document" else None
    if doc is None:
        advance(session, error_screen(1, "File required", "Upload the website as a document file:"), flow_kb(session))
        return
    filename = doc.file_name or "index.html"
    ext = file_extension(filename)
    if ext not in ("html", "htm", "zip"):
        advance(session, error_screen(1, "Unsupported file", "Send only .html, .htm, or .zip:"), flow_kb(session))
        return
    limit = MAX_ZIP_SIZE if ext == "zip" else MAX_HTML_SIZE
    too_large = error_screen(1, "File is too large", f"Maximum for .{ext} is {limit:,} bytes. Send a smaller file:")
    if doc.file_size and doc.file_size > limit:
        advance(session, too_large, flow_kb(session))
        return
    try:
        content = download_telegram_file(doc.file_id, limit)
    except FileTooLarge:
        advance(session, too_large, flow_kb(session))
        return
    except DownloadFailed:
        advance(session, error_screen(1, "Download failed", "Telegram could not deliver the file. Please try again:"), flow_kb(session))
        return
    error, warning = validate_website(filename, content)
    if error:
        advance(session, error_screen(1, "Invalid website file", error + "\n\nPlease send another file:"), flow_kb(session))
        return
    icon_size = len(session.icon[1]) if session.icon else 0
    if len(content) + icon_size > MAX_TOTAL_FILES:
        advance(
            session,
            error_screen(1, "Files are too large together", "Website + icon exceed the API request limit. Send a smaller website:"),
            flow_kb(session),
        )
        return
    session.website = (filename, content)
    session.website_note = warning
    if session.editing:
        show_review(session)
        return
    session.step = "icon"
    advance(session, prompt_icon(filename), kb_cancel())


def handle_icon(session, message):
    if message.content_type == "document":
        doc = message.document
        original = doc.file_name or "icon.png"
        if file_extension(original) not in ("png", "jpg", "jpeg"):
            advance(session, error_screen(2, "Unsupported icon", "Please send a PNG or JPEG:"), flow_kb(session))
            return
        file_id, declared_size = doc.file_id, doc.file_size
    elif message.content_type == "photo":
        original = "icon.jpg"
        photo = message.photo[-1]
        file_id, declared_size = photo.file_id, getattr(photo, "file_size", None)
    else:
        advance(session, error_screen(2, "Icon required", "Please send a PNG or JPEG image:"), flow_kb(session))
        return
    too_large = error_screen(2, "Icon is too large", "Maximum size is 6 MiB. Send a smaller icon:")
    if declared_size and declared_size > MAX_ICON_SIZE:
        advance(session, too_large, flow_kb(session))
        return
    try:
        content = download_telegram_file(file_id, MAX_ICON_SIZE)
    except FileTooLarge:
        advance(session, too_large, flow_kb(session))
        return
    except DownloadFailed:
        advance(session, error_screen(2, "Icon download failed", "Telegram could not deliver the file. Please try again:"), flow_kb(session))
        return
    info, error = validate_icon(content)
    if error:
        advance(session, error_screen(2, "Invalid icon", error + "\n\nPlease send another PNG or JPEG:"), flow_kb(session))
        return
    website_size = len(session.website[1]) if session.website else 0
    if website_size + len(content) > MAX_TOTAL_FILES:
        advance(
            session,
            error_screen(2, "Files are too large together", "Website + icon exceed the API request limit. Send a smaller icon:"),
            flow_kb(session),
        )
        return
    session.icon = (icon_filename(original, info), content, info.mime)
    session.icon_info = (info.format, info.width, info.height)
    show_review(session)


@bot.message_handler(content_types=MESSAGE_TYPES, func=lambda message: True)
@safe
def on_message(message):
    chat_id = message.chat.id
    text = message.text if message.content_type == "text" else None
    if text and text.startswith("/"):
        bot.send_message(chat_id, UNKNOWN_COMMAND_TEXT, reply_markup=kb_main_menu())
        return
    session = store.get(chat_id)
    if session is None:
        bot.send_message(chat_id, IDLE_HINT_TEXT, reply_markup=kb_main_menu())
        return
    with session.lock:
        if session.cancelled:
            bot.send_message(chat_id, IDLE_HINT_TEXT, reply_markup=kb_main_menu())
            return
        session.touch()
        step = session.step
        if step == "name":
            handle_name(session, message)
        elif step == "website":
            handle_website(session, message)
        elif step == "icon":
            handle_icon(session, message)
        elif step == "building":
            bot.send_message(chat_id, BUILD_BUSY_TEXT)
        else:  # review
            bot.send_message(chat_id, "👆 Please use the buttons on the review screen above, or /cancel.")


# --------------------------------------------------------------------------
# Build orchestration
# --------------------------------------------------------------------------
def _animate_status(session, state, stop, started):
    """Refresh the status screen with the real current stage and elapsed time."""
    frame = 0
    total_attempts = BUILD_RETRIES + 1
    while not stop.is_set() and not session.cancelled:
        text = build_status_text(
            session.app_name, state["phase"], state["attempt"], total_attempts,
            time.monotonic() - started, frame,
        )
        with session.ui_lock:
            if session.cancelled or stop.is_set():
                return
            backoff = edit_quiet(session.ui_message, text, kb_cancel_build())
        frame += 1
        stop.wait(max(STATUS_REFRESH_SECONDS, backoff))


def run_build(session):
    chat_id = session.chat_id
    app_name, website, icon = session.app_name, session.website, session.icon
    started = time.monotonic()
    state = {"phase": "queued", "attempt": 1}
    stop = threading.Event()
    animator = threading.Thread(target=_animate_status, args=(session, state, stop, started), daemon=True)
    animator.start()

    def on_status(phase, attempt):
        state["phase"] = phase
        state["attempt"] = attempt

    acquired = False
    result = None
    elapsed = 0
    outcome = ("crash", None)
    try:
        while not acquired:
            if session.cancelled:
                raise BuildCancelled()
            acquired = build_slots.acquire(timeout=1)
        state["phase"] = "building"
        logger.info("Build started for chat %s (%s)", chat_id, website[0])
        result = request_build(app_name, website, icon, on_status, lambda: session.cancelled)
        elapsed = time.monotonic() - started
        if session.cancelled:
            raise BuildCancelled()
        state["phase"] = "delivering"
        bot.send_document(
            chat_id,
            (result.filename, result.content),
            caption=apk_caption(app_name, result.filename, len(result.content), elapsed),
            reply_markup=kb_main_menu(),
        )
        outcome = ("ok", None)
        logger.info("Build delivered to chat %s (%d bytes)", chat_id, len(result.content))
    except BuildCancelled:
        outcome = ("cancelled", None)
    except BuildError as exc:
        logger.warning("Build failed for chat %s: status=%s detail=%s", chat_id, exc.status, exc.detail)
        outcome = ("error", exc)
    except Exception:
        logger.exception("Unexpected build error for chat %s", chat_id)
        outcome = ("delivery" if result is not None else "crash", None)
    finally:
        if acquired:
            build_slots.release()
        stop.set()
        animator.join(timeout=10)

    kind, error = outcome
    if kind == "cancelled" or session.cancelled:
        return  # the cancel handler already updated the screen

    with session.ui_lock:
        ui = session.ui_message
        if kind == "ok":
            session.ui_message = render(ui, build_complete_text(app_name, result.filename, len(result.content), elapsed), None)
            store.discard_if(session)
            return
        if kind == "error":
            text = build_failed_text("BUILD FAILED", error.detail, error.hint, error.status)
        elif kind == "delivery":
            text = build_failed_text(
                "COULD NOT SEND THE APK",
                "The APK was built but Telegram did not accept the upload.",
                "Please try again.",
            )
        else:
            text = build_failed_text("BUILD FAILED", "An unexpected error occurred.", "Please try again.")
        with session.lock:
            session.building = False
            session.step = "review"
            session.touch()
        session.ui_message = render(ui, text, kb_failed())


# --------------------------------------------------------------------------
# Background maintenance and bot profile
# --------------------------------------------------------------------------
def _janitor_loop():
    while True:
        time.sleep(60)
        try:
            for session in store.pop_expired(SESSION_TTL_SECONDS):
                if session.ui_message is not None:
                    with session.ui_lock:
                        edit_quiet(session.ui_message, EXPIRED_TEXT, kb_main_menu())
            store.prune_cooldowns()
        except Exception:
            logger.exception("Session cleanup failed")


def start_janitor():
    threading.Thread(target=_janitor_loop, daemon=True, name="session-janitor").start()


def configure_bot_profile():
    """Best-effort: command menu and profile texts. Failures never stop the bot."""
    try:
        bot.set_my_commands([
            types.BotCommand("start", "Open the main menu"),
            types.BotCommand("newapk", "Create a new APK"),
            types.BotCommand("help", "How it works"),
            types.BotCommand("about", f"About {BRAND_NAME}"),
            types.BotCommand("cancel", "Cancel the current action"),
        ])
    except Exception:
        logger.warning("Could not set the command menu")
    try:
        bot.set_my_short_description("Convert your HTML website or ZIP project into an Android APK — right inside Telegram.")
        bot.set_my_description(
            f"{BRAND_NAME}\n\n"
            "Turn your HTML website or ZIP project into an installable Android APK in a few simple steps.\n\n"
            "• Your own app name and icon\n"
            "• Live build status\n"
            "• Ready-to-install test APK\n\n"
            "Tap START to begin."
        )
    except Exception:
        logger.warning("Could not set the bot description")
