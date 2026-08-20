"""
test_series.py — the Test Series feature: a 3-level browsable menu

    Institutes  ->  Test Series  ->  Papers (PDFs)

replacing the old flat "Books" button list on /start.

Two halves live in this one file:

  USER SIDE (callback prefix "ts:")
    ts:main                        -> top-level institute list (same content
                                       as a bare /start's menu)
    ts:inst:<inst_id>              -> that institute's test-series list
    ts:series:<inst_id>:<series_id>-> that series' paper list
    ts:paper:<code>                -> delivers the PDF behind `code`
    Every submenu carries a "⬅️ Back" (previous level) and "🏠 Main Menu"
    (top level) button so navigation never dead-ends.

  ADMIN SIDE (callback prefix "tsa:", command /testseries)
    A panel in the same "tap a field, then reply with a message" style as
    /setting (see admin_settings.py). Institutes and series are created by
    name; papers are attached by pasting the code/link of a file that was
    already uploaded through the normal single-file or /batch flow (see
    upload.py) — this feature never re-implements uploading, it only
    organizes links that already exist.

Data lives in the `institutes` Mongo collection (plugins/helper/db.py):
one doc per institute, embedding its own list of series, each embedding its
own list of papers ({id, name, code}).
"""
import html as html_lib
import logging
import secrets

from pyrogram import Client, filters
from pyrogram.errors import MessageNotModified
from pyrogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

from plugins.helper.db import db
from plugins.helper.filters import admin_filter
from plugins.helper.force_sub import ensure_subscribed_for_user
from plugins.helper.photo_ref import send_photo_ref, store_photo_ref
from plugins.helper.settings import settings
from plugins.helper.start_message import _arrange_buttons, main_menu_content
from plugins.filestore.delivery import deliver
from plugins.filestore.linking import extract_code
from plugins.filestore.upload import BATCH_SESSIONS
from plugins.filestore.admin_settings import AWAITING as SETTINGS_AWAITING

logger = logging.getLogger(__name__)


def _esc(s: str) -> str:
    return html_lib.escape(s or "")


def _new_id() -> str:
    return secrets.token_hex(4)


async def _edit_menu(client: Client, query: CallbackQuery, text: str, markup: InlineKeyboardMarkup,
                      photo: str = None):
    """Renders a menu level.

    `photo` is the institute/series' own image (a Telegram file_id), or
    None if that level has no custom image. Since each level can carry a
    *different* image, this can't just be a caption edit.

    This deliberately does NOT use edit_media to swap photos in place.
    Telegram treats an edited message's media as new content and makes
    clients re-fetch it every single time, even for a photo they already
    downloaded on a previous visit — that's what was showing a download
    arrow on every click. Instead, whenever a photo is involved, this
    follows the same approach as the /start message's photo
    (start_message.py): delete the old message and send a fresh one.
    A freshly *sent* message with a given photo is cached normally by the
    client, so revisiting an institute/series you've already viewed
    doesn't re-trigger the download indicator — only the very first time
    that particular image is shown does.

    Pure text-to-text navigation (no photo at either end) is still edited
    in place, since plain text has none of this media-caching behavior.
    """
    msg = query.message

    if not photo and not msg.photo:
        try:
            await msg.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
            return
        except MessageNotModified:
            return
        except Exception as e:
            logger.info(f"In-place text edit failed for test-series menu, resending: {e}")

    try:
        await msg.delete()
    except Exception as e:
        logger.warning(f"Failed to delete old test-series menu message: {e}")
    chat_id = msg.chat.id
    if photo:
        try:
            await send_photo_ref(client, chat_id, photo, caption=text, reply_markup=markup)
            return
        except Exception as e:
            # A bad/legacy reference must not break navigation — fall back
            # to a text-only render of the same menu instead of crashing.
            logger.warning(f"Image ({photo!r}) failed to send for test-series menu: {e}")
    await client.send_message(chat_id, text, reply_markup=markup, disable_web_page_preview=True)


# ══════════════════════════════════════════════════════════════════════
# USER SIDE — browsing
# ══════════════════════════════════════════════════════════════════════

def _series_list_content(inst: dict):
    series = inst.get("series", [])
    text = f"🏫 <b>{_esc(inst['name'])}</b>\n\n"
    text += "📚 <b>Choose a test series:</b>" if series else "No test series here yet. Check back soon!"

    buttons = [
        InlineKeyboardButton(f"📘 {s['name']}", callback_data=f"ts:series:{inst['_id']}:{s['id']}")
        for s in series
    ]
    rows = _arrange_buttons(buttons)
    rows.append([
        InlineKeyboardButton("⬅️ Back", callback_data="ts:main"),
        InlineKeyboardButton("🏠 Main Menu", callback_data="ts:main"),
    ])
    return text, InlineKeyboardMarkup(rows)


def _papers_list_content(inst: dict, series: dict):
    papers = series.get("papers", [])
    text = f"📘 <b>{_esc(inst['name'])} — {_esc(series['name'])}</b>\n\n"
    text += "📝 <b>Tap a paper to get the PDF:</b>" if papers else "No papers uploaded here yet. Check back soon!"

    buttons = [
        InlineKeyboardButton(f"📄 {p['name']}", callback_data=f"ts:paper:{p['code']}")
        for p in papers
    ]
    rows = _arrange_buttons(buttons)
    rows.append([
        InlineKeyboardButton("⬅️ Back", callback_data=f"ts:inst:{inst['_id']}"),
        InlineKeyboardButton("🏠 Main Menu", callback_data="ts:main"),
    ])
    return text, InlineKeyboardMarkup(rows)


@Client.on_callback_query(filters.regex(r"^ts:main$"))
async def ts_main(client: Client, query: CallbackQuery):
    await query.answer()
    text, markup = await main_menu_content(query.from_user.mention)
    await _edit_menu(client, query, text, markup, photo=settings.get("start_photo"))


@Client.on_callback_query(filters.regex(r"^ts:inst:"))
async def ts_inst(client: Client, query: CallbackQuery):
    inst_id = query.data.split(":", 2)[2]
    inst = await db.get_institute(inst_id)
    if not inst:
        await query.answer("This institute isn't available anymore.", show_alert=True)
        return
    await query.answer()
    text, markup = _series_list_content(inst)
    await _edit_menu(client, query, text, markup, photo=inst.get("image"))


@Client.on_callback_query(filters.regex(r"^ts:series:"))
async def ts_series(client: Client, query: CallbackQuery):
    _, _, inst_id, series_id = query.data.split(":", 3)
    inst = await db.get_institute(inst_id)
    series = next((s for s in (inst or {}).get("series", []) if s["id"] == series_id), None)
    if not inst or not series:
        await query.answer("This test series isn't available anymore.", show_alert=True)
        return
    await query.answer()
    text, markup = _papers_list_content(inst, series)
    # A series' own image takes priority; fall back to its institute's image
    # if the series itself has none set.
    photo = series.get("image") or inst.get("image")
    await _edit_menu(client, query, text, markup, photo=photo)


@Client.on_callback_query(filters.regex(r"^ts:paper:"))
async def ts_paper(client: Client, query: CallbackQuery):
    code = query.data.split(":", 2)[2]
    allowed = await ensure_subscribed_for_user(
        client, query.from_user.id, query.message.chat.id, resume_payload=code
    )
    if not allowed:
        await query.answer()
        return
    await query.answer("📤 Sending...")
    await deliver(client, query.from_user.id, code)


# ══════════════════════════════════════════════════════════════════════
# ADMIN SIDE — managing institutes / series / papers
# ══════════════════════════════════════════════════════════════════════

# admin_id -> {"action": str, "inst_id": Optional[str], "series_id": Optional[str]}
AWAITING_TS: dict = {}

ADD_PAPERS_HELP = (
    "✏️ Send one paper per line, as:\n"
    "<code>Label | CODE_OR_LINK</code>\n\n"
    "Upload the PDF the normal way first (just send it to me, or use /batch) "
    "to get its code/link, then paste it here — one line per paper.\n\n"
    "<b>Example:</b>\n"
    "<code>Test 1 | https://t.me/mybot?start=abcd1234\n"
    "Test 2 | efgh5678</code>"
)


# chat_id -> message_id of the most recently shown /testseries admin
# panel screen (top-level panel, an institute, or a series). Every
# navigation or completed action re-renders through this instead of
# stacking a fresh message on top each time.
LAST_PANEL_MSG: dict = {}


async def _render_panel(client: Client, chat_id: int, text: str, markup: InlineKeyboardMarkup):
    """Shows a /testseries admin screen, replacing whatever screen was
    last shown to this admin instead of leaving it behind. Works the same
    whether the previous screen was reached via a button tap or the admin
    just finished typing a reply (rename, add series/papers, etc.) — both
    paths funnel through here.

    Tries to edit the previous screen in place first; if that's not
    possible (first screen ever, message too old to edit, or it was
    deleted out from under us) it deletes the stale message and sends a
    fresh one instead, so exactly one panel message ends up in the chat.
    """
    prev_id = LAST_PANEL_MSG.get(chat_id)
    if prev_id:
        try:
            await client.edit_message_text(chat_id, prev_id, text, reply_markup=markup)
            return
        except MessageNotModified:
            return
        except Exception as e:
            logger.info(f"Couldn't edit previous test-series admin panel, resending: {e}")
            try:
                await client.delete_messages(chat_id, prev_id)
            except Exception:
                pass
    msg = await client.send_message(chat_id, text, reply_markup=markup)
    LAST_PANEL_MSG[chat_id] = msg.id


def _panel_text(institutes: list) -> str:
    if not institutes:
        return "🏫 <b>Test Series Manager</b>\n\nNo institutes yet. Tap ➕ below to add one."
    lines = ["🏫 <b>Test Series Manager</b>\n"]
    for inst in institutes:
        n_series = len(inst.get("series", []))
        lines.append(f"• {_esc(inst['name'])} — {n_series} series")
    lines.append("\nTap an institute to manage it.")
    return "\n".join(lines)


def _panel_buttons(institutes: list) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(f"🏫 {inst['name']}", callback_data=f"tsa:inst:{inst['_id']}")
        for inst in institutes
    ]
    rows = _arrange_buttons(buttons)
    rows.append([InlineKeyboardButton("➕ Add Institute", callback_data="tsa:addinst")])
    return InlineKeyboardMarkup(rows)


async def _send_admin_panel(client: Client, chat_id: int):
    institutes = await db.get_all_institutes()
    await _render_panel(client, chat_id, _panel_text(institutes), _panel_buttons(institutes))


def _institute_admin_text(inst: dict) -> str:
    series = inst.get("series", [])
    lines = [f"🏫 <b>{_esc(inst['name'])}</b>\n"]
    if not series:
        lines.append("No test series yet.")
    else:
        for s in series:
            n_papers = len(s.get("papers", []))
            lines.append(f"• {_esc(s['name'])} — {n_papers} paper(s)")
        lines.append("\nTap a series to manage it.")
    return "\n".join(lines)


def _institute_admin_buttons(inst: dict) -> InlineKeyboardMarkup:
    inst_id = inst["_id"]
    buttons = [
        InlineKeyboardButton(f"📘 {s['name']}", callback_data=f"tsa:series:{inst_id}:{s['id']}")
        for s in inst.get("series", [])
    ]
    rows = _arrange_buttons(buttons)
    rows.append([InlineKeyboardButton("➕ Add Series", callback_data=f"tsa:addseries:{inst_id}")])
    img_row = [InlineKeyboardButton("🖼 Set Image", callback_data=f"tsa:setinstimg:{inst_id}")]
    if inst.get("image"):
        img_row.append(InlineKeyboardButton("🗑 Remove Image", callback_data=f"tsa:delinstimg:{inst_id}"))
    rows.append(img_row)
    rows.append([
        InlineKeyboardButton("✏️ Rename", callback_data=f"tsa:renameinst:{inst_id}"),
        InlineKeyboardButton("🗑 Delete Institute", callback_data=f"tsa:delinstask:{inst_id}"),
    ])
    rows.append([InlineKeyboardButton("⬅️ Back to Panel", callback_data="tsa:panel")])
    return InlineKeyboardMarkup(rows)


async def _send_institute_admin(client: Client, chat_id: int, inst_id: str):
    inst = await db.get_institute(inst_id)
    if not inst:
        await _send_admin_panel(client, chat_id)
        return
    await _render_panel(client, chat_id, _institute_admin_text(inst), _institute_admin_buttons(inst))


def _series_admin_text(inst: dict, series: dict) -> str:
    papers = series.get("papers", [])
    lines = [f"📘 <b>{_esc(inst['name'])} — {_esc(series['name'])}</b>\n"]
    lines.append("No papers yet." if not papers else "Tap 🗑 next to a paper to remove it.")
    return "\n".join(lines)


def _series_admin_buttons(inst: dict, series: dict) -> InlineKeyboardMarkup:
    inst_id, series_id = inst["_id"], series["id"]
    rows = [
        [InlineKeyboardButton(
            f"🗑 {p['name']}", callback_data=f"tsa:delpaperask:{inst_id}:{series_id}:{p['id']}"
        )]
        for p in series.get("papers", [])
    ]
    rows.append([InlineKeyboardButton("➕ Add Papers", callback_data=f"tsa:addpapers:{inst_id}:{series_id}")])
    img_row = [InlineKeyboardButton("🖼 Set Image", callback_data=f"tsa:setseriesimg:{inst_id}:{series_id}")]
    if series.get("image"):
        img_row.append(
            InlineKeyboardButton("🗑 Remove Image", callback_data=f"tsa:delseriesimg:{inst_id}:{series_id}")
        )
    rows.append(img_row)
    rows.append([
        InlineKeyboardButton("✏️ Rename", callback_data=f"tsa:renameseries:{inst_id}:{series_id}"),
        InlineKeyboardButton("🗑 Delete Series", callback_data=f"tsa:delseriesask:{inst_id}:{series_id}"),
    ])
    rows.append([InlineKeyboardButton("⬅️ Back to Institute", callback_data=f"tsa:inst:{inst_id}")])
    return InlineKeyboardMarkup(rows)


async def _send_series_admin(client: Client, chat_id: int, inst_id: str, series_id: str):
    inst = await db.get_institute(inst_id)
    series = next((s for s in (inst or {}).get("series", []) if s["id"] == series_id), None)
    if not inst or not series:
        await _send_institute_admin(client, chat_id, inst_id)
        return
    await _render_panel(client, chat_id, _series_admin_text(inst, series), _series_admin_buttons(inst, series))


@Client.on_message(filters.command("testseries") & filters.private & admin_filter)
async def testseries_panel(client: Client, message: Message):
    await _send_admin_panel(client, message.chat.id)


@Client.on_callback_query(filters.regex(r"^tsa:panel$") & admin_filter)
async def tsa_panel(client: Client, query: CallbackQuery):
    await query.answer()
    await _send_admin_panel(client, query.message.chat.id)


@Client.on_callback_query(filters.regex(r"^tsa:inst:") & admin_filter)
async def tsa_inst(client: Client, query: CallbackQuery):
    inst_id = query.data.split(":", 2)[2]
    await query.answer()
    await _send_institute_admin(client, query.message.chat.id, inst_id)


@Client.on_callback_query(filters.regex(r"^tsa:series:") & admin_filter)
async def tsa_series(client: Client, query: CallbackQuery):
    _, _, inst_id, series_id = query.data.split(":", 3)
    await query.answer()
    await _send_series_admin(client, query.message.chat.id, inst_id, series_id)


# ── Prompts that arm AWAITING_TS ────────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^tsa:addinst$") & admin_filter)
async def tsa_addinst(client: Client, query: CallbackQuery):
    AWAITING_TS[query.from_user.id] = {"action": "addinst"}
    await query.answer()
    await query.message.reply_text("✏️ Send the institute's name.")


@Client.on_callback_query(filters.regex(r"^tsa:renameinst:") & admin_filter)
async def tsa_renameinst(client: Client, query: CallbackQuery):
    inst_id = query.data.split(":", 2)[2]
    AWAITING_TS[query.from_user.id] = {"action": "renameinst", "inst_id": inst_id}
    await query.answer()
    await query.message.reply_text("✏️ Send the new name for this institute.")


@Client.on_callback_query(filters.regex(r"^tsa:addseries:") & admin_filter)
async def tsa_addseries(client: Client, query: CallbackQuery):
    inst_id = query.data.split(":", 2)[2]
    AWAITING_TS[query.from_user.id] = {"action": "addseries", "inst_id": inst_id}
    await query.answer()
    await query.message.reply_text("✏️ Send the test series' name (e.g. \"Weekly Test — 19 Aug\").")


@Client.on_callback_query(filters.regex(r"^tsa:renameseries:") & admin_filter)
async def tsa_renameseries(client: Client, query: CallbackQuery):
    _, _, inst_id, series_id = query.data.split(":", 3)
    AWAITING_TS[query.from_user.id] = {"action": "renameseries", "inst_id": inst_id, "series_id": series_id}
    await query.answer()
    await query.message.reply_text("✏️ Send the new name for this test series.")


@Client.on_callback_query(filters.regex(r"^tsa:setinstimg:") & admin_filter)
async def tsa_setinstimg(client: Client, query: CallbackQuery):
    inst_id = query.data.split(":", 2)[2]
    AWAITING_TS[query.from_user.id] = {"action": "setinstimage", "inst_id": inst_id}
    await query.answer()
    await query.message.reply_text(
        "🖼 Send the photo to use as this institute's image (shown when users open it).\n"
        "Send /cancel to leave it as is."
    )


@Client.on_callback_query(filters.regex(r"^tsa:delinstimg:") & admin_filter)
async def tsa_delinstimg(client: Client, query: CallbackQuery):
    inst_id = query.data.split(":", 2)[2]
    await db.set_institute_image(inst_id, None)
    await query.answer("Image removed.")
    await _send_institute_admin(client, query.message.chat.id, inst_id)


@Client.on_callback_query(filters.regex(r"^tsa:setseriesimg:") & admin_filter)
async def tsa_setseriesimg(client: Client, query: CallbackQuery):
    _, _, inst_id, series_id = query.data.split(":", 3)
    AWAITING_TS[query.from_user.id] = {
        "action": "setseriesimage", "inst_id": inst_id, "series_id": series_id
    }
    await query.answer()
    await query.message.reply_text(
        "🖼 Send the photo to use as this test series' image (shown when users open it).\n"
        "Send /cancel to leave it as is."
    )


@Client.on_callback_query(filters.regex(r"^tsa:delseriesimg:") & admin_filter)
async def tsa_delseriesimg(client: Client, query: CallbackQuery):
    _, _, inst_id, series_id = query.data.split(":", 3)
    await db.set_series_image(inst_id, series_id, None)
    await query.answer("Image removed.")
    await _send_series_admin(client, query.message.chat.id, inst_id, series_id)


@Client.on_callback_query(filters.regex(r"^tsa:addpapers:") & admin_filter)
async def tsa_addpapers(client: Client, query: CallbackQuery):
    _, _, inst_id, series_id = query.data.split(":", 3)
    AWAITING_TS[query.from_user.id] = {"action": "addpapers", "inst_id": inst_id, "series_id": series_id}
    await query.answer()
    await query.message.reply_text(ADD_PAPERS_HELP)


# ── Deletes (with confirmation) ─────────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^tsa:delinstask:") & admin_filter)
async def tsa_delinstask(client: Client, query: CallbackQuery):
    inst_id = query.data.split(":", 2)[2]
    await query.answer()
    await query.message.reply_text(
        "⚠️ <b>Delete this institute and every test series/paper inside it?</b>\n"
        "This can't be undone. The uploaded PDFs themselves are untouched — "
        "only their listing here is removed.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Yes, delete", callback_data=f"tsa:delinstyes:{inst_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"tsa:delinstno:{inst_id}"),
        ]]),
    )


@Client.on_callback_query(filters.regex(r"^tsa:delinstyes:") & admin_filter)
async def tsa_delinstyes(client: Client, query: CallbackQuery):
    inst_id = query.data.split(":", 2)[2]
    await db.delete_institute(inst_id)
    await query.answer("Deleted.")
    await query.message.edit_text("🗑 Institute deleted.")
    await _send_admin_panel(client, query.message.chat.id)


@Client.on_callback_query(filters.regex(r"^tsa:delinstno:") & admin_filter)
async def tsa_delinstno(client: Client, query: CallbackQuery):
    await query.answer("Cancelled.")
    await query.message.edit_text("❎ Cancelled — nothing was deleted.")


@Client.on_callback_query(filters.regex(r"^tsa:delseriesask:") & admin_filter)
async def tsa_delseriesask(client: Client, query: CallbackQuery):
    _, _, inst_id, series_id = query.data.split(":", 3)
    await query.answer()
    await query.message.reply_text(
        "⚠️ <b>Delete this test series and all its papers?</b>\nThis can't be undone.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Yes, delete", callback_data=f"tsa:delseriesyes:{inst_id}:{series_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"tsa:delseriesno:{inst_id}:{series_id}"),
        ]]),
    )


@Client.on_callback_query(filters.regex(r"^tsa:delseriesyes:") & admin_filter)
async def tsa_delseriesyes(client: Client, query: CallbackQuery):
    _, _, inst_id, series_id = query.data.split(":", 3)
    await db.delete_series(inst_id, series_id)
    await query.answer("Deleted.")
    await query.message.edit_text("🗑 Test series deleted.")
    await _send_institute_admin(client, query.message.chat.id, inst_id)


@Client.on_callback_query(filters.regex(r"^tsa:delseriesno:") & admin_filter)
async def tsa_delseriesno(client: Client, query: CallbackQuery):
    await query.answer("Cancelled.")
    await query.message.edit_text("❎ Cancelled — nothing was deleted.")


@Client.on_callback_query(filters.regex(r"^tsa:delpaperask:") & admin_filter)
async def tsa_delpaperask(client: Client, query: CallbackQuery):
    _, _, inst_id, series_id, paper_id = query.data.split(":", 4)
    await query.answer()
    await query.message.reply_text(
        "⚠️ <b>Remove this paper from the series?</b>\n"
        "The uploaded file/link itself is untouched, only its listing here is removed.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Yes, remove", callback_data=f"tsa:delpaperyes:{inst_id}:{series_id}:{paper_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"tsa:delpaperno:{inst_id}:{series_id}:{paper_id}"),
        ]]),
    )


@Client.on_callback_query(filters.regex(r"^tsa:delpaperyes:") & admin_filter)
async def tsa_delpaperyes(client: Client, query: CallbackQuery):
    _, _, inst_id, series_id, paper_id = query.data.split(":", 4)
    await db.delete_paper(inst_id, series_id, paper_id)
    await query.answer("Removed.")
    await query.message.edit_text("🗑 Paper removed.")
    await _send_series_admin(client, query.message.chat.id, inst_id, series_id)


@Client.on_callback_query(filters.regex(r"^tsa:delpaperno:") & admin_filter)
async def tsa_delpaperno(client: Client, query: CallbackQuery):
    await query.answer("Cancelled.")
    await query.message.edit_text("❎ Cancelled — nothing was removed.")


# ── Capturing the reply to a prompt above ───────────────────────────────

def _has_pending_ts(_, __, message: Message) -> bool:
    if not message.from_user or message.from_user.id not in AWAITING_TS:
        return False
    admin_id = message.from_user.id
    # Don't steal a text message actually meant for /setting or an open
    # /batch session — those flows take priority if somehow both are
    # active for the same admin at once.
    if admin_id in SETTINGS_AWAITING or admin_id in BATCH_SESSIONS:
        return False
    # setinstimage/setseriesimage are handled by ts_apply_image (below) and
    # expect a photo, not text — leave that state alone here so a stray
    # text message doesn't get eaten with no matching branch in
    # ts_apply_text (which would silently pop the pending state).
    if AWAITING_TS[admin_id]["action"] in ("setinstimage", "setseriesimage"):
        return False
    return not (message.text or "").startswith("/")  # let /cancel etc. through untouched


def _has_pending_ts_image(_, __, message: Message) -> bool:
    if not message.from_user or message.from_user.id not in AWAITING_TS:
        return False
    return AWAITING_TS[message.from_user.id]["action"] in ("setinstimage", "setseriesimage")


@Client.on_message(filters.private & filters.photo & admin_filter & filters.create(_has_pending_ts_image))
async def ts_apply_image(client: Client, message: Message):
    admin_id = message.from_user.id
    state = AWAITING_TS.pop(admin_id)

    # Reposting into BACKUP_CHANNEL (rather than saving message.photo.file_id
    # directly) is what makes this image survive a future BOT_TOKEN swap —
    # see plugins/helper/photo_ref.py. It also doubles as validation: a
    # photo forwarded from a "restrict saving content" chat fails right
    # here, instead of silently breaking every time this menu is opened.
    try:
        ref = await store_photo_ref(client, message.photo.file_id)
    except Exception:
        await message.reply_text(
            "❌ Telegram won't let me reuse that image (this usually happens with "
            "photos forwarded from a channel that has 'restrict saving content' "
            "enabled).\n\nPlease send the image directly — e.g. save it to your "
            "device and upload it fresh, rather than forwarding it."
        )
        AWAITING_TS[admin_id] = state  # let them retry
        return

    if state["action"] == "setinstimage":
        await db.set_institute_image(state["inst_id"], ref)
        await message.reply_text("✅ Institute image set.")
        await _send_institute_admin(client, message.chat.id, state["inst_id"])
    else:
        await db.set_series_image(state["inst_id"], state["series_id"], ref)
        await message.reply_text("✅ Test series image set.")
        await _send_series_admin(client, message.chat.id, state["inst_id"], state["series_id"])


async def _apply_add_papers(client: Client, message: Message, inst_id: str, series_id: str):
    papers, errors = [], []
    for lineno, line in enumerate(message.text.strip().splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        if "|" not in line:
            errors.append(f"Line {lineno}: missing '|' separator — expected \"Label | CODE_OR_LINK\"")
            continue
        label, raw_code = line.split("|", 1)
        label, raw_code = label.strip(), raw_code.strip()
        if not label or not raw_code:
            errors.append(f"Line {lineno}: empty label or code")
            continue
        code = extract_code(raw_code)
        if not await db.get_file_link(code):
            errors.append(f"Line {lineno}: no uploaded file found for code '{_esc(code)}'")
            continue
        papers.append({"id": _new_id(), "name": label, "code": code})

    if papers:
        await db.add_papers(inst_id, series_id, papers)

    report = f"✅ Added {len(papers)} paper(s)." if papers else "❌ No papers were added."
    if errors:
        report += "\n\n⚠️ <b>Skipped:</b>\n" + "\n".join(_esc(e) for e in errors)
    await message.reply_text(report)


@Client.on_message(filters.private & filters.text & admin_filter & filters.create(_has_pending_ts))
async def ts_apply_text(client: Client, message: Message):
    admin_id = message.from_user.id
    state = AWAITING_TS.pop(admin_id)
    action = state["action"]
    raw = message.text.strip()

    if action == "addinst":
        if not raw:
            AWAITING_TS[admin_id] = state
            await message.reply_text("❌ Name can't be empty. Send the institute's name.")
            return
        inst_id = _new_id()
        await db.create_institute(inst_id, raw)
        await message.reply_text(f"✅ Institute \"{_esc(raw)}\" added.")
        await _send_admin_panel(client, message.chat.id)

    elif action == "renameinst":
        if not raw:
            AWAITING_TS[admin_id] = state
            await message.reply_text("❌ Name can't be empty. Send the new name.")
            return
        await db.rename_institute(state["inst_id"], raw)
        await message.reply_text("✅ Renamed.")
        await _send_institute_admin(client, message.chat.id, state["inst_id"])

    elif action == "addseries":
        if not raw:
            AWAITING_TS[admin_id] = state
            await message.reply_text("❌ Name can't be empty. Send the test series' name.")
            return
        series_id = _new_id()
        await db.add_series(state["inst_id"], {"id": series_id, "name": raw, "papers": []})
        await message.reply_text(f"✅ Test series \"{_esc(raw)}\" added.")
        await _send_institute_admin(client, message.chat.id, state["inst_id"])

    elif action == "renameseries":
        if not raw:
            AWAITING_TS[admin_id] = state
            await message.reply_text("❌ Name can't be empty. Send the new name.")
            return
        await db.rename_series(state["inst_id"], state["series_id"], raw)
        await message.reply_text("✅ Renamed.")
        await _send_series_admin(client, message.chat.id, state["inst_id"], state["series_id"])

    elif action == "addpapers":
        await _apply_add_papers(client, message, state["inst_id"], state["series_id"])
        await _send_series_admin(client, message.chat.id, state["inst_id"], state["series_id"])
