"""Кістяк AI-агента в Telegram: меню, про мене, продукти, кабінет, питання власниці, розсилки, фото, подарунок,
підписка на канал (за бажанням), звіт власниці о 9:00, «Агента оновлено».
Запуск: python bot.py. Тексти в texts.py, налаштування в config.py, база в db.py.
Додаток продажів (launch.py + wfp.py + texts_launch.py) підключається сам, якщо ці файли є в папці."""
import asyncio
import html
import json
import re
import logging
import os
from collections import Counter
from datetime import datetime, timedelta

from telegram import (BotCommand, BotCommandScopeChat, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
                      ReplyKeyboardMarkup, Update, WebAppInfo)
from telegram.constants import ParseMode
import httpx
from telegram.error import BadRequest, Conflict, Forbidden, NetworkError
from telegram.ext import (Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

import calc
import kviz
import config
import db
import texts as T
import webserver

# Додаток продажів: якщо в папці є launch.py, агент уміє запуски й оплату. Помилки в ньому не ховаємо.
if os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)), "launch.py")):
    import launch
else:
    launch = None

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("agent")
HTML = ParseMode.HTML
HERE = os.path.dirname(os.path.abspath(__file__))


# ================== клавіатури ==================

def main_button():
    return T.BTN_LAUNCH if launch else T.BTN_MAIN


def menu_buttons():
    b = [main_button(), T.BTN_MAIN, T.BTN_PRODUCTS, T.BTN_CALC, T.BTN_CALC_PENSION, T.BTN_CALC_KIDS, T.BTN_CALC_SAVE,
         T.BTN_ASK, T.BTN_CABINET, T.BTN_CONTACTS, T.BTN_ADMIN]
    return set(b)


def kb_bottom(uid):
    """Нижнє меню: широка головна кнопка зверху, далі по 2 в ряд. Власниці ще «Керування»."""
    # «Розрахунки»: якщо є адреса мінізастосунку, кнопка одразу відкриває його, інакше розрахунки в чаті
    def calc_btn(text, tab):
        if not config.WEBAPP_URL:
            return text
        return KeyboardButton(text, web_app=WebAppInfo(f"{config.WEBAPP_URL}?tab={tab}"))
    rows = [[main_button()],
            [calc_btn(T.BTN_CALC_PENSION, "pension"), calc_btn(T.BTN_CALC_KIDS, "kids")],
            [calc_btn(T.BTN_CALC_SAVE, "save")],
            [T.BTN_PRODUCTS, T.BTN_ASK], [T.BTN_CABINET, T.BTN_CONTACTS]]
    if config.is_admin(uid):
        rows.append([T.BTN_ADMIN])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)


def kb_btn(t, d):
    """Кнопка під повідомленням: https:// посилання, app:вкладка (мінізастосунок розрахунків) або callback."""
    if d.startswith("http"):
        return InlineKeyboardButton(t, url=d)
    if d.startswith("app:"):
        if config.WEBAPP_URL:
            return InlineKeyboardButton(t, web_app=WebAppInfo(f"{config.WEBAPP_URL}?tab={d[4:]}"))
        return InlineKeyboardButton(t, callback_data=f"calc:{d[4:]}")  # без адреси: розрахунок у чаті
    return InlineKeyboardButton(t, callback_data=d)


def kb(rows):
    """rows: [[("Текст", "callback, app:вкладка або https://посилання"), ...], ...]"""
    return InlineKeyboardMarkup([[kb_btn(t, d) for t, d in r] for r in rows if r])


SRC_OK = re.compile(r"^[A-Za-z0-9_-]{1,64}$")  # слово після ?start=: латиниця, цифри, «_», «-», до 64 знаків


def kb_nav(back=None):
    row = [(T.BTN_BACK, back)] if back else []
    return kb([row + [(T.BTN_HOME, "home")]])


def esc(s):
    return html.escape(str(s or ""))


def who(u):
    name = esc(u.full_name if hasattr(u, "full_name") else u.get("name"))
    nick = u.username if hasattr(u, "username") else u.get("username")
    return f"{name} (@{nick})" if nick else name


# ================== стан людини (зберігається в базі, не губиться після оновлення) ==================

def state(uid, value=None):
    if value is None:
        return db.kv(f"st:{uid}") or ""
    return db.kv(f"st:{uid}", value)


# ================== надсилання з фото / відео ==================

def media_path(rel):
    p = os.path.join(HERE, rel or "")
    return p if rel and os.path.isfile(p) else ""


async def send(bot, chat_id, text, markup=None, media=""):
    """Текст, а якщо є файл у images/, то фото чи відео з текстом. Telegram дає під фото до 1024 знаків:
    довший текст іде окремим повідомленням після фото. file_id запам'ятовуємо, щоб не вантажити файл щоразу."""
    path = media_path(media)
    if path:
        fid_key = f"fid:{media}:{int(os.path.getmtime(path))}"
        fid = db.kv(fid_key)
        short = len(text) <= 1000
        is_video = path.lower().endswith((".mp4", ".mov"))
        try:
            src = fid or open(path, "rb")
            k = dict(caption=text if short else None, parse_mode=HTML, reply_markup=markup if short else None)
            m = await (bot.send_video(chat_id, src, **k) if is_video else bot.send_photo(chat_id, src, **k))
            if not fid:
                got = m.video.file_id if is_video and m.video else (m.photo[-1].file_id if m.photo else "")
                if got:
                    db.kv(fid_key, got)
            if short:
                return m
        except Exception as e:
            log.warning("media %s: %s", media, e)
    return await bot.send_message(chat_id, text, parse_mode=HTML, reply_markup=markup, disable_web_page_preview=True)


# ================== власниці ==================

async def notify_admins(bot, text, **k):
    for a in config.ADMIN_IDS:
        try:
            await bot.send_message(a, text, parse_mode=HTML, disable_web_page_preview=True, **k)
        except Exception as e:
            log.warning("admin notify %s: %s", a, e)


# ================== підписка на канал (за бажанням) ==================

async def gate_ok(bot, chat_id, uid, src=None, is_new=False):
    """True, якщо можна далі. Якщо канал не задано, людина вже підписана або агент не може перевірити, пропускаємо."""
    if not T.CHANNEL or config.is_admin(uid) or db.kv(f"gate:{uid}") == "1":
        return True
    try:
        m = await bot.get_chat_member(T.CHANNEL, uid)
        if m.status in ("member", "administrator", "creator", "restricted"):
            db.kv(f"gate:{uid}", "1")
            db.log_ev(uid, "gate_ok")
            return True
    except Exception as e:
        log.warning("gate %s: %s", T.CHANNEL, e)
        day = datetime.now(config.TZ).strftime("%Y-%m-%d")
        if db.kv("gate_warn") != day:  # власниці раз на добу
            db.kv("gate_warn", day)
            await notify_admins(bot, T.ADMIN_GATE_WARN.format(channel=esc(T.CHANNEL)))
        return True
    if src is not None:  # запам'ятати, куди людина йшла, щоб продовжити після підписки
        db.kv(f"gate_src:{uid}", src)
        db.kv(f"gate_new:{uid}", "1" if is_new else "")
    url = "https://t.me/" + T.CHANNEL.lstrip("@")
    await bot.send_message(chat_id, T.GATE, parse_mode=HTML,
                           reply_markup=kb([[(T.BTN_GATE_SUB, url)], [(T.BTN_GATE_CHECK, "gate")]]))
    return False


# ================== старт і меню ==================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    chat_id = update.effective_chat.id
    src = context.args[0] if context.args else ""
    src = src if SRC_OK.match(src) else ""
    is_new = db.touch_user(u.id, u.full_name, u.username or "", src)
    state(u.id, "")
    db.log_ev(u.id, "start", src)  # джерело: хто, яке слово, коли (для /dzherela і звіту)
    if src in T.DOORS:  # «двері»: своє привітання замість загального (в after_start)
        if is_new and not config.is_admin(u.id):
            total, _, _ = db.stats()
            await notify_admins(context.bot, T.ADMIN_NEW_USER.format(who=who(u), src=esc(src), total=total))
    elif is_new:
        await send(context.bot, chat_id, T.WELCOME.format(name=esc(u.first_name), owner=T.OWNER_ROD),
                   kb_bottom(u.id), T.PHOTOS.get("welcome"))
        total, _, _ = db.stats()
        if not config.is_admin(u.id):
            await notify_admins(context.bot, T.ADMIN_NEW_USER.format(who=who(u), src=esc(src or "напряму"), total=total))
    else:
        await update.message.reply_text(T.WELCOME_BACK.format(name=esc(u.first_name)), parse_mode=HTML,
                                        reply_markup=kb_bottom(u.id))
    if not await gate_ok(context.bot, chat_id, u.id, src, is_new):
        return
    await after_start(context, chat_id, u.id, src, is_new)


async def after_start(context, chat_id, uid, src, is_new):
    """Що робити після старту (і після підписки на канал): подарунок новим і розділ за міткою ?start=..."""
    if src in T.DOORS:
        await show_door(context, chat_id, uid, src, is_new)
        return
    if (is_new or src == "gift") and T.GIFT_FILE:
        await send_gift(context.bot, chat_id, uid)
    elif (is_new or src == "gift") and T.GIFT_CALC and src != "test":  # з тесту одразу тест, без подарунка
        # подарунок-розрахунок: якщо мінізастосунку ще немає, кнопка відкриває розрахунки в чаті
        markup = None if config.WEBAPP_URL else kb([[(T.BTN_CALC_PENSION, "calc:pension")]])
        await context.bot.send_message(chat_id, T.GIFT_CALC, parse_mode=HTML, reply_markup=markup)
        db.log_ev(uid, "gift")
    # Мітка ?start=... може одразу відкривати розділ: ?start=p1 → продукт p1, ?start=ask → питання, ?start=about
    if launch and await launch.on_start(context, chat_id, uid, src):
        pass  # продажі: картка запуску, воронка-магніт або тест за міткою
    elif src in T.PRODUCTS:
        await show_product(chat_id, context, src)
    elif src == "ask":
        await ask_start(chat_id, context, uid)
    elif src == "about":
        await show_about(chat_id, context, uid)
    elif src == "test":
        await quiz_intro(context, chat_id, uid)
    elif src == "calc":
        await show_calc_menu(chat_id, context, uid)
    elif src.startswith("calc_") and src[5:] in calc.STEPS:
        await calc_start(chat_id, context, uid, src[5:])


async def show_door(context, chat_id, uid, word, is_new):
    """Окреме привітання для посилання ?start=СЛОВО: текст (новим з фото) і 1-2 кнопки, потім нижнє меню."""
    text, rows = T.DOORS[word]
    u = db.user(uid) or {}
    name = esc((u.get("name") or "").split(" ")[0])
    db.log_ev(uid, "door", word)
    await send(context.bot, chat_id, text.format(name=name), kb(rows), T.PHOTOS.get("welcome") if is_new else "")
    await context.bot.send_message(chat_id, T.DOOR_MENU, reply_markup=kb_bottom(uid))


async def dzherela(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/dzherela (тільки власниці): скільки людей прийшло з кожного слова за сьогодні, 7 днів і всього."""
    if not config.is_admin(update.effective_user.id):
        return
    now = datetime.now(config.TZ)
    today = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    week = int((now - timedelta(days=7)).timestamp())
    with db.conn() as c:
        rows = [dict(r) for r in c.execute("SELECT user_id, extra, at FROM events WHERE ev='start'").fetchall()]
    rows = [r for r in rows if not config.is_admin(r["user_id"])]

    def block(since):
        people = {}
        for r in rows:
            if r["at"] >= since:
                people.setdefault(r["extra"] or "напряму", set()).add(r["user_id"])
        top = sorted(people.items(), key=lambda x: -len(x[1]))
        return "\n".join(f"• {esc(k)}: {len(v)}" for k, v in top) or T.SOURCES_NONE

    await update.message.reply_text(T.SOURCES.format(today=block(today), week=block(week), all=block(0)), parse_mode=HTML)


async def send_gift(bot, chat_id, uid):
    path = media_path(T.GIFT_FILE)
    if not path:
        return
    fid_key = f"fid:{T.GIFT_FILE}:{int(os.path.getmtime(path))}"
    fid = db.kv(fid_key)
    try:
        m = await bot.send_document(chat_id, fid or open(path, "rb"), caption=T.GIFT_TEXT, parse_mode=HTML)
        if not fid and m.document:
            db.kv(fid_key, m.document.file_id)
        db.log_ev(uid, "gift")
    except Exception as e:
        log.warning("gift: %s", e)


async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state(update.effective_user.id, "")
    await update.message.reply_text(T.MENU_HINT, reply_markup=kb_bottom(update.effective_user.id))


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Ваш Telegram ID: <code>{update.effective_user.id}</code>", parse_mode=HTML)


# ================== розділи ==================

async def show_about(chat_id, context, uid=0):
    db.log_ev(uid, "about")
    markup = kb([[(T.BTN_CONSULT, "write:consult")], [(T.BTN_HOME, "home")]])
    await send(context.bot, chat_id, T.ABOUT, markup, T.PHOTOS.get("about"))


async def show_products(chat_id, context, edit_msg=None, uid=0):
    db.log_ev(uid, "products")
    rows = [[(name, f"prod:{key}")] for key, (name, _) in T.PRODUCTS.items()]
    if launch:
        rows = launch.product_rows() + rows
    rows.append([(T.BTN_HOME, "home")])
    if edit_msg and edit_msg.text:
        try:
            await edit_msg.edit_text(T.PRODUCTS_TITLE, parse_mode=HTML, reply_markup=kb(rows))
            return
        except Exception:
            pass
    await context.bot.send_message(chat_id, T.PRODUCTS_TITLE, parse_mode=HTML, reply_markup=kb(rows))


async def show_product(chat_id, context, key, edit_msg=None):
    name, text = T.PRODUCTS[key]
    markup = kb([[(T.BTN_CONSULT, f"write:{key}")], [(T.BTN_BACK, "products"), (T.BTN_HOME, "home")]])
    media = T.PRODUCT_PHOTOS.get(key)
    if edit_msg and edit_msg.text and not media_path(media):
        try:
            await edit_msg.edit_text(text, parse_mode=HTML, reply_markup=markup, disable_web_page_preview=True)
            return
        except Exception:
            pass
    await send(context.bot, chat_id, text, markup, media)


async def show_cabinet(chat_id, context, uid):
    db.log_ev(uid, "cabinet")
    u = db.user(uid) or {}
    since = datetime.fromtimestamp(u.get("joined") or 0, config.TZ).strftime("%d.%m.%Y") if u.get("joined") else "сьогодні"
    extra, markup = "", None
    if launch:
        extra = launch.cabinet_line(uid)
        m = launch.member(uid)
        markup = (launch.pay_more_kb(m) if m else None) or kb([[(T.BTN_LAUNCH, "lc:card")], [(T.BTN_HOME, "home")]])
    markup = markup or kb([[(T.BTN_PRODUCTS, "products")], [(T.BTN_HOME, "home")]])
    await context.bot.send_message(chat_id, T.CABINET.format(name=esc(u.get("name")), since=since, extra=extra or T.CABINET_EMPTY_EXTRA),
                                   parse_mode=HTML, reply_markup=markup)


async def show_contacts(chat_id, context, uid=0):
    db.log_ev(uid, "contacts")
    rows = [[(T.BTN_WRITE_ME, "write:contacts")], [(T.BTN_HOME, "home")]]
    if launch:  # у агенті з продажами «Про мене» живе тут, бо головна кнопка меню зайнята запуском
        rows[1].insert(0, (T.BTN_MAIN, "about"))
    await send(context.bot, chat_id, T.CONTACTS, kb(rows), T.PHOTOS.get("contacts"))


def topic_title(topic):
    """Як назвати тему запиту для Олени: «Запис на консультацію: 👶 Для дітей» тощо."""
    title = launch.topic_title(topic) if launch else ""
    title = title or (f"Запис на консультацію: {T.PRODUCTS[topic][0]}" if topic in T.PRODUCTS else "")
    return title or T.TOPICS.get(topic) or topic


async def ask_start(chat_id, context, uid, topic=""):
    state(uid, f"ask:{topic}")
    is_consult = topic in ("consult", "tip", "quiz") or topic in T.PRODUCTS or topic.startswith("calc_")  # запис на консультацію з «Про мене» або з картки програми
    if is_consult:
        db.log_ev(uid, "consult", topic)
        # Одне натискання = запис: Олені одразу запит (на нього можна «Відповісти»), людині підтвердження
        db.kv(f"consult_sent:{uid}", topic)
        if not config.is_admin(uid):
            u = db.user(uid) or {}
            nick = u.get("username")
            contact = f"@{esc(nick)}" if nick else f'<a href="tg://user?id={uid}">{T.ADMIN_WRITE_LINK}</a>'
            text = T.ADMIN_CONSULT.format(who=who(u), uid=uid, topic=esc(topic_title(topic)), contact=contact)
            if topic.startswith("calc_") and db.kv(f"calc_res:{uid}"):
                text += "\n\n<b>Розрахунок людини:</b>\n" + db.kv(f"calc_res:{uid}")
            for a in config.ADMIN_IDS:
                try:
                    m = await context.bot.send_message(a, text, parse_mode=HTML)
                    db.kv(f"q:{m.message_id}", uid)
                except Exception as e:
                    log.warning("consult to admin: %s", e)
        await context.bot.send_message(chat_id, T.CONSULT_DONE, parse_mode=HTML,
                                       reply_markup=kb([[(T.BTN_HOME, "home")]]))
        return
    db.kv(f"consult_sent:{uid}", "")
    await context.bot.send_message(chat_id, T.ASK, parse_mode=HTML,
                                   reply_markup=kb([[(T.BTN_CANCEL, "ask_cancel")]]))


# ================== квіз «Яка програма підходить саме вам?» (?start=test) ==================

async def quiz_intro(context, chat_id, uid):
    db.log_ev(uid, "quiz_start")
    await context.bot.send_message(chat_id, T.QUIZ_INTRO, parse_mode=HTML,
                                   reply_markup=kb([[(T.BTN_QUIZ_START, "qz:start")]]))


async def quiz_question(context, chat_id, uid, i, edit_msg=None):
    text, rows = kviz.question(i)
    markup = kb(rows)
    if edit_msg and edit_msg.text:  # одне повідомлення змінюється, чат лишається чистим
        try:
            await edit_msg.edit_text(text, parse_mode=HTML, reply_markup=markup)
            return
        except Exception:
            pass
    await context.bot.send_message(chat_id, text, parse_mode=HTML, reply_markup=markup)


async def quiz_answer(context, chat_id, uid, i, key, edit_msg):
    done = kviz.take(uid, i, key)
    if done is None:
        return
    if not done:
        await quiz_question(context, chat_id, uid, i + 1, edit_msg)
        return
    text, rows, goal = kviz.result(uid)
    db.log_ev(uid, "quiz_done", json.dumps(kviz.answers(uid)))
    db.log_ev(uid, "goal", goal)  # ціль видно в «🧮 Хто рахував»
    try:
        await edit_msg.edit_text(text, parse_mode=HTML, reply_markup=kb(rows))
    except Exception:
        await context.bot.send_message(chat_id, text, parse_mode=HTML, reply_markup=kb(rows))
    if not config.is_admin(uid):
        u = db.user(uid) or {}
        nick = u.get("username")
        contact = f"@{esc(nick)}" if nick else f'<a href="tg://user?id={uid}">{T.ADMIN_WRITE_LINK}</a>'
        msg = T.ADMIN_QUIZ.format(who=who(u), summary=esc(kviz.summary(uid)),
                                  result=esc(T.QUIZ_RESULT_NAMES.get(goal, goal)), contact=contact)
        for a in config.ADMIN_IDS:
            try:
                m = await context.bot.send_message(a, msg, parse_mode=HTML)
                db.kv(f"q:{m.message_id}", uid)
            except Exception as e:
                log.warning("quiz to admin: %s", e)


async def show_calc_menu(chat_id, context, uid=0):
    db.log_ev(uid, "calc")
    markup = kb([[(T.BTN_CALC_SAVE, "calc:save")], [(T.BTN_CALC_PENSION, "calc:pension")],
                 [(T.BTN_CALC_KIDS, "calc:kids")], [(T.BTN_HOME, "home")]])
    await context.bot.send_message(chat_id, T.CALC_MENU, parse_mode=HTML, reply_markup=markup)


async def calc_start(chat_id, context, uid, kind):
    state(uid, f"calc:{kind}:0")
    db.log_ev(uid, "calc_start", kind)
    await context.bot.send_message(chat_id, calc.question(kind, 0), parse_mode=HTML,
                                   reply_markup=kb([[(T.BTN_BACK, "calc"), (T.BTN_HOME, "home")]]))


async def calc_answer(msg, context, uid, st):
    """Людина відповіла числом на питання калькулятора."""
    _, kind, step = st.split(":")
    step = int(step)
    err, done = calc.take(kind, step, msg.text, uid)
    if err:
        await msg.reply_text(err)
        return
    if not done:
        state(uid, f"calc:{kind}:{step + 1}")
        await msg.reply_text(calc.question(kind, step + 1), parse_mode=HTML,
                             reply_markup=kb([[(T.BTN_BACK, "calc"), (T.BTN_HOME, "home")]]))
        return
    await calc_finish(msg, uid, kind)


async def calc_finish(msg, uid, kind, goal=0):
    """Показує результат розрахунку з кнопкою запису (і з чату, і з мінізастосунку). goal: мета з мінізастосунку."""
    state(uid, "")
    text = calc.result(kind, uid)
    if goal:
        line = T.CALC_GOAL_PENSION if kind == "pension" else T.CALC_GOAL_SUM
        text = line.format(goal=calc.money(goal)) + "\n\n" + text
    db.kv(f"calc_res:{uid}", text)  # Олена побачить цей розрахунок, якщо людина запишеться
    db.log_ev(uid, f"calc_{kind}", json.dumps(calc.answers(uid)))  # цифри людини для «🧮 Хто рахував»
    await msg.reply_text(text, parse_mode=HTML, reply_markup=kb([
        [(T.BTN_CONSULT, f"write:calc_{kind}")],
        [(T.BTN_CALC_AGAIN, "calc"), (T.BTN_HOME, "home")]]))


async def show_admin(chat_id, context, edit_msg=None):
    total, week, src = db.stats()
    sources = "\n".join(f"• {esc(s)}: {n}" for s, n in src) or "• поки нікого"
    text = T.ADMIN_MENU.format(total=total, week=week, sources=sources)
    rows = [[(T.BTN_BROADCAST, "bc"), (T.BTN_REPORT, "report")], [(T.BTN_CALC_LIST, "calcs"), (T.BTN_TIPS, "tips")],
            [(T.BTN_STATS, "adm")]]
    if launch:
        rows = launch.admin_rows() + rows
    markup = kb(rows)
    if edit_msg:
        try:
            await edit_msg.edit_text(text, parse_mode=HTML, reply_markup=markup)
            return
        except Exception:
            pass
    await context.bot.send_message(chat_id, text, parse_mode=HTML, reply_markup=markup)


# ================== кнопки під повідомленнями ==================

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try:
        await q.answer()
    except Exception:
        pass
    uid, chat_id, data = update.effective_user.id, q.message.chat_id, q.data or ""
    if data == "gate":
        if T.CHANNEL and not config.is_admin(uid):
            db.kv(f"gate:{uid}", "")
        if await gate_ok(context.bot, chat_id, uid):
            await context.bot.send_message(chat_id, T.GATE_OK, reply_markup=kb_bottom(uid))
            src = db.kv(f"gate_src:{uid}") or ""
            fresh = db.kv(f"gate_new:{uid}") == "1"
            db.kv(f"gate_src:{uid}", "")
            db.kv(f"gate_new:{uid}", "")
            await after_start(context, chat_id, uid, src, fresh)
        else:
            await context.bot.send_message(chat_id, T.GATE_NOT_YET)
        return
    if not config.is_admin(uid) and not await gate_ok(context.bot, chat_id, uid):
        return
    if launch and data.startswith("lc:"):
        await launch.on_callback(update, context, data)
    elif data == "home":
        state(uid, "")
        await context.bot.send_message(chat_id, T.MENU_HINT, reply_markup=kb_bottom(uid))
    elif data == "about":
        await show_about(chat_id, context, uid)
    elif data == "products":
        await show_products(chat_id, context, q.message, uid)
    elif data.startswith("prod:") and data[5:] in T.PRODUCTS:
        db.log_ev(uid, "product", data[5:])
        await show_product(chat_id, context, data[5:], q.message)
    elif data == "calc":
        state(uid, "")
        await show_calc_menu(chat_id, context, uid)
    elif data.startswith("calc:") and data[5:] in calc.STEPS:
        await calc_start(chat_id, context, uid, data[5:])
    elif data.startswith("write:"):
        await ask_start(chat_id, context, uid, data[6:])
    elif data == "ask_cancel":
        state(uid, "")
        await context.bot.send_message(chat_id, T.ASK_CANCELLED, reply_markup=kb_bottom(uid))
    elif config.is_admin(uid) and data == "adm":
        await show_admin(chat_id, context, q.message)
    elif data == "qz:start":
        await quiz_question(context, chat_id, uid, 0, q.message)
    elif data.startswith("qz:"):
        parts = data.split(":")
        if len(parts) == 3 and parts[1].isdigit():
            await quiz_answer(context, chat_id, uid, int(parts[1]), parts[2], q.message)
    elif data == "tip:next":
        db.log_ev(uid, "tip_next")
        n = int(db.kv(f"tip_n:{uid}") or 0)
        if n < len(tips_all()):
            db.kv(f"tip_n:{uid}", n + 1)
            await send_tip(context.bot, chat_id, n, uid)
        else:
            await context.bot.send_message(chat_id, T.TIP_NO_MORE, reply_markup=kb_bottom(uid))
    elif data == "tip:off":
        db.kv(f"tip_off:{uid}", "1")
        db.log_ev(uid, "tip_off")
        await context.bot.send_message(chat_id, T.TIP_OFF_DONE, reply_markup=kb_bottom(uid))
    elif data.startswith("goal:") and data[5:] in T.GOALS:
        await on_goal(context, chat_id, uid, data[5:])
    elif config.is_admin(uid) and data == "tips":
        await show_tips_admin(context.bot, chat_id)
    elif config.is_admin(uid) and data == "tip_add":
        state(uid, "tip_wait")
        await context.bot.send_message(chat_id, T.TIP_ADD_ASK.format(n=len(tips_all()) + 1),
                                       reply_markup=kb([[(T.BTN_CANCEL, "bc_no")]]))
    elif config.is_admin(uid) and data.startswith("tip_show:"):
        await send_tip(context.bot, chat_id, int(data[9:]), 0)
    elif config.is_admin(uid) and data.startswith("tip_del:"):
        i = int(data[8:]) - len(T.TIPS)
        custom = tips_custom()
        if 0 <= i < len(custom):
            custom.pop(i)
            db.kv("tips_custom", json.dumps(custom))
            await context.bot.send_message(chat_id, T.TIP_DELETED.format(n=int(data[8:]) + 1))
        await show_tips_admin(context.bot, chat_id)
    elif config.is_admin(uid) and data == "calcs":
        for part in calc_list_texts():
            await context.bot.send_message(chat_id, part, parse_mode=HTML, disable_web_page_preview=True)
    elif config.is_admin(uid) and data == "report":
        await context.bot.send_message(chat_id, report_text(), parse_mode=HTML)
    elif config.is_admin(uid) and launch and data == "members":
        await context.bot.send_message(chat_id, launch.members_text(), parse_mode=HTML)
    elif config.is_admin(uid) and launch and data == "wfptest":
        await launch.wfptest(context.bot, chat_id, uid)
    elif config.is_admin(uid) and data == "bc":
        state(uid, "bc_wait")
        await context.bot.send_message(chat_id, T.BC_ASK, reply_markup=kb([[(T.BTN_CANCEL, "bc_no")]]))
    elif config.is_admin(uid) and data == "bc_go":
        await broadcast(context, chat_id, uid)
    elif config.is_admin(uid) and data == "bc_edit":
        state(uid, "bc_wait")
        await context.bot.send_message(chat_id, T.BC_ASK, reply_markup=kb([[(T.BTN_CANCEL, "bc_no")]]))
    elif config.is_admin(uid) and data == "bc_no":
        state(uid, "")
        db.kv(f"bc_src:{uid}", "")
        await context.bot.send_message(chat_id, T.BC_CANCELLED, reply_markup=kb_bottom(uid))


# ================== повідомлення людей ==================


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or update.effective_chat.type != "private":
        return
    u = update.effective_user
    uid, chat_id = u.id, msg.chat_id
    db.touch_user(uid, u.full_name, u.username or "")
    text = msg.text or ""

    # Команда, вставлена як звичайний текст (наприклад, скопійована з повідомлення): виконуємо її все одно
    cmd = text.strip().split()[0].lower().split("@")[0] if text.strip().startswith("/") else ""
    if cmd in ("/dzherela", "/myid", "/menu"):
        await {"/dzherela": dzherela, "/myid": myid, "/menu": menu_cmd}[cmd](update, context)
        return

    # 0) Цифри з мінізастосунку «Розрахунки»
    if msg.web_app_data:
        kind, action, goal = calc.from_webapp(uid, msg.web_app_data.data)
        if kind:
            await calc_finish(msg, uid, kind, goal)
            if action == "consult":  # «📞 Записатися» в мінізастосунку: одразу записуємо
                await ask_start(chat_id, context, uid, f"calc_{kind}")
        else:
            await msg.reply_text(T.CALC_NOT_NUMBER, reply_markup=kb_bottom(uid))
        return

    # 1) Кнопки нижнього меню завжди першими: людина не «застрягає» в питанні чи розсилці
    if text in menu_buttons():
        state(uid, "")
        if not await gate_ok(context.bot, chat_id, uid):
            return
        if launch and text == T.BTN_LAUNCH:
            await launch.show_card(context.bot, chat_id, uid)
        elif text == T.BTN_MAIN:
            await show_about(chat_id, context, uid)
        elif text == T.BTN_PRODUCTS:
            await show_products(chat_id, context, uid=uid)
        elif text == T.BTN_CALC:
            await show_calc_menu(chat_id, context, uid)
        elif text in (T.BTN_CALC_PENSION, T.BTN_CALC_KIDS, T.BTN_CALC_SAVE):  # якщо мінізастосунку ще немає
            kinds = {T.BTN_CALC_PENSION: "pension", T.BTN_CALC_KIDS: "kids", T.BTN_CALC_SAVE: "save"}
            await calc_start(chat_id, context, uid, kinds[text])
        elif text == T.BTN_ASK:
            await ask_start(chat_id, context, uid)
        elif text == T.BTN_CABINET:
            await show_cabinet(chat_id, context, uid)
        elif text == T.BTN_CONTACTS:
            await show_contacts(chat_id, context, uid)
        elif text == T.BTN_ADMIN and config.is_admin(uid):
            await show_admin(chat_id, context)
        return

    # 2) Власниця відповідає на питання: «Відповісти» на повідомлення з питанням
    if config.is_admin(uid) and msg.reply_to_message:
        target = db.kv(f"q:{msg.reply_to_message.message_id}")
        if target:
            try:
                await context.bot.send_message(int(target), T.ANSWER_FROM_OWNER.format(owner=T.OWNER_ROD, text=esc(text or msg.caption)),
                                               parse_mode=HTML)
                if not text:  # голосове, фото, відео: пересилаємо як є
                    await context.bot.copy_message(int(target), chat_id, msg.message_id)
                await msg.reply_text(T.ADMIN_ANSWER_OK)
            except Exception:
                await msg.reply_text(T.ADMIN_ANSWER_FAIL)
            return

    st = state(uid)

    # 3а) Власниця додає текст у серію «Корисне»
    if config.is_admin(uid) and st == "tip_wait":
        state(uid, "")
        if msg.photo:
            item = {"photo": msg.photo[-1].file_id, "text": msg.caption_html or ""}
        else:
            item = {"text": msg.text_html or ""}
        if not item["text"] and "photo" not in item:
            await msg.reply_text(T.CALC_NOT_NUMBER)
            return
        custom = tips_custom()
        custom.append(item)
        db.kv("tips_custom", json.dumps(custom))
        n = len(tips_all())
        await send_tip(context.bot, chat_id, n - 1, 0)
        await msg.reply_text(T.TIP_ADDED.format(n=n), reply_markup=kb([[(T.BTN_TIPS, "tips")]]))
        return

    # 3) Власниця готує розсилку: показуємо прев'ю
    if config.is_admin(uid) and st == "bc_wait":
        db.kv(f"bc_src:{uid}", f"{chat_id}:{msg.message_id}")
        state(uid, "bc_ready")
        await context.bot.copy_message(chat_id, chat_id, msg.message_id)
        n = len([i for i in db.active_ids() if not config.is_admin(i)])
        await context.bot.send_message(chat_id, T.BC_PREVIEW.format(n=n),
                                       reply_markup=kb([[(T.BTN_SEND, "bc_go")], [(T.BTN_EDIT, "bc_edit"), (T.BTN_NOT_NOW, "bc_no")]]))
        return

    # 4) Людина рахує в калькуляторі
    if st.startswith("calc:"):
        await calc_answer(msg, context, uid, st)
        return

    # 5) Людина ставить питання: передаємо власниці
    if st.startswith("ask"):
        topic = st.split(":", 1)[1] if ":" in st else ""
        state(uid, "")
        db.log_ev(uid, "question", topic)
        head = T.ADMIN_QUESTION.format(who=who(u), uid=uid, text=esc(text or msg.caption or "(голосове чи файл нижче)"))
        if db.kv(f"consult_sent:{uid}"):  # людина вже записалась одним натисканням, це доповнення (час, телефон)
            db.kv(f"consult_sent:{uid}", "")
            head = T.ADMIN_CONSULT_EXTRA + "\n" + head
        elif topic:
            head = f"Тема: <b>{esc(topic_title(topic))}</b>\n" + head
            if topic.startswith("calc_") and db.kv(f"calc_res:{uid}"):
                head += "\n\n<b>Розрахунок людини:</b>\n" + db.kv(f"calc_res:{uid}")
        for a in config.ADMIN_IDS:
            try:
                m = await context.bot.send_message(a, head, parse_mode=HTML)
                db.kv(f"q:{m.message_id}", uid)
                if not text:
                    m2 = await context.bot.copy_message(a, chat_id, msg.message_id)
                    db.kv(f"q:{m2.message_id}", uid)
            except Exception as e:
                log.warning("question to admin: %s", e)
        await msg.reply_text(T.ASK_SENT, reply_markup=kb_bottom(uid))
        return

    # 6) Кодові слова продажів (воронки, тест) і службові повідомлення власниці, якщо підключено систему продажів
    if launch and await launch.on_message(context, msg, u):
        return

    # 7) Усе інше: м'яко повертаємо в меню
    await msg.reply_text(T.UNKNOWN, reply_markup=kb_bottom(uid))


# ================== розсилка ==================

async def broadcast(context, chat_id, uid):
    src = db.kv(f"bc_src:{uid}") or ""
    if state(uid) != "bc_ready" or ":" not in src:
        await context.bot.send_message(chat_id, T.BC_CANCELLED)
        return
    state(uid, "")
    db.kv(f"bc_src:{uid}", "")  # одразу: повторне натискання нічого не розішле
    from_chat, mid = src.split(":")
    ok = fail = 0
    for target in db.active_ids():
        if config.is_admin(target):
            continue
        try:
            await context.bot.copy_message(target, int(from_chat), int(mid))
            ok += 1
        except Forbidden:
            db.set_blocked(target)
            fail += 1
        except Exception as e:
            log.warning("broadcast %s: %s", target, e)
            fail += 1
        await asyncio.sleep(0.06)
    await context.bot.send_message(chat_id, T.BC_DONE.format(ok=ok, fail=fail), reply_markup=kb_bottom(uid))


# ================== «📚 Корисне»: серія текстів по одному на день ==================

def tips_custom():
    try:
        return json.loads(db.kv("tips_custom") or "[]")
    except ValueError:
        return []


def tips_all():
    """Вбудовані тексти з texts.py, потім ті, що Олена додала в агенті."""
    return list(T.TIPS) + tips_custom()


def tip_markup(kind):
    off = [(T.BTN_TIP_OFF, "tip:off")]
    if kind == "next":
        return kb([[(T.BTN_TIP_NEXT, "tip:next")], off])
    if kind == "calc":
        return kb([[(T.BTN_TIP_PENSION, "calc:pension"), (T.BTN_TIP_KIDS, "calc:kids")],
                   [(T.BTN_TIP_SAVE, "calc:save")], [(T.BTN_CONSULT, "write:tip")], off])
    if kind == "channel":
        return kb([[(T.BTN_TIP_CHANNEL, T.TIP_CHANNEL_URL)], off])
    if kind == "goal":
        return kb([[(name, f"goal:{key}")] for key, name in T.GOALS.items()] + [off])
    return kb([[(T.BTN_CONSULT, "write:tip")], off])


async def send_tip(bot, chat_id, idx, uid):
    """Надсилає текст серії номер idx (з 0). uid=0: показ власниці."""
    items = tips_all()
    if not 0 <= idx < len(items):
        return
    item = items[idx]
    markup = tip_markup(item.get("kind", ""))
    text = item.get("text") or ""
    if item.get("photo"):
        short = len(text) <= 1000
        await bot.send_photo(chat_id, item["photo"], caption=text if short else None, parse_mode=HTML,
                             reply_markup=markup if short else None)
        if not short:
            await bot.send_message(chat_id, text, parse_mode=HTML, reply_markup=markup, disable_web_page_preview=True)
    else:
        await bot.send_message(chat_id, text, parse_mode=HTML, reply_markup=markup, disable_web_page_preview=True)
    if uid:
        db.log_ev(uid, "tip", str(idx + 1))


async def on_goal(context, chat_id, uid, goal):
    """Людина обрала ціль у серії: Олені одразу (можна «Відповісти»), людині подяка і запис."""
    db.log_ev(uid, "goal", goal)
    if not config.is_admin(uid):
        u = db.user(uid) or {}
        nick = u.get("username")
        contact = f"@{esc(nick)}" if nick else f'<a href="tg://user?id={uid}">{T.ADMIN_WRITE_LINK}</a>'
        for a in config.ADMIN_IDS:
            try:
                m = await context.bot.send_message(a, T.ADMIN_GOAL.format(who=who(u), goal=esc(T.GOALS[goal]),
                                                                        contact=contact), parse_mode=HTML)
                db.kv(f"q:{m.message_id}", uid)
            except Exception as e:
                log.warning("goal to admin: %s", e)
    await context.bot.send_message(chat_id, T.GOAL_THANKS, parse_mode=HTML,
                                   reply_markup=kb([[(T.BTN_CONSULT, "write:tip")], [(T.BTN_HOME, "home")]]))


async def show_tips_admin(bot, chat_id):
    items = tips_all()
    lines, btns = [], []
    for i, item in enumerate(items):
        raw = html.unescape(re.sub(r"<[^>]+>", "", item.get("text") or "")).replace("\n", " ").strip()
        preview = esc(raw[:60] + ("…" if len(raw) > 60 else "")) or "(фото)"
        lines.append(T.TIPS_ITEM.format(n=i + 1, mark="📷 " if item.get("photo") else "", preview=preview))
        btns.append((f"👁 {i + 1}", f"tip_show:{i}"))
        if i >= len(T.TIPS):
            btns.append((f"🗑 {i + 1}", f"tip_del:{i}"))
    rows = [btns[i:i + 4] for i in range(0, len(btns), 4)]
    rows.append([(T.BTN_TIP_ADD, "tip_add")])
    rows.append([(T.BTN_BACK, "adm")])
    await bot.send_message(chat_id, T.TIPS_ADMIN.format(hour=T.TIPS_HOUR, n=len(items), items="\n".join(lines),
                                                       base=len(T.TIPS)), parse_mode=HTML, reply_markup=kb(rows))


async def tips_job(context):
    """Раз на день о TIPS_HOUR за Києвом: кожному наступний текст серії (не в день «Старт», не тим, хто вимкнув)."""
    now = datetime.now(config.TZ)
    if now.hour < T.TIPS_HOUR:
        return
    day = now.strftime("%Y-%m-%d")
    if db.kv("tips_day") == day:
        return
    db.kv("tips_day", day)  # ДО відправки: після перезапуску повтору не буде
    items = tips_all()
    today = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    with db.conn() as c:
        users = [r[0] for r in c.execute(
            "SELECT user_id FROM users WHERE COALESCE(blocked,0)=0 AND joined<?", (today,)).fetchall()]
    sent = 0
    for uid in users:
        if config.is_admin(uid) or db.kv(f"tip_off:{uid}") == "1":
            continue
        n = int(db.kv(f"tip_n:{uid}") or 0)
        if n >= len(items):
            continue
        db.kv(f"tip_n:{uid}", n + 1)
        try:
            await send_tip(context.bot, uid, n, uid)
            sent += 1
        except Forbidden:
            db.set_blocked(uid)
        except Exception as e:
            log.warning("tip %s: %s", uid, e)
        await asyncio.sleep(0.06)
    log.info("tips sent: %s", sent)


# ================== «🧮 Хто рахував» для власниці ==================

def calc_list_texts(days=30, limit=40):
    """Люди, які рахували за останні days днів: що рахували, з якими цифрами і де зупинились. Частинами до 3500 знаків."""
    since = int((datetime.now(config.TZ) - timedelta(days=days)).timestamp())
    with db.conn() as c:
        evs = [dict(r) for r in c.execute(
            "SELECT user_id, ev, extra, at FROM events WHERE at>=? AND (ev LIKE 'calc_%' OR ev IN ('consult', 'goal')) "
            "ORDER BY at",
            (since,)).fetchall()]
    people = {}
    for e in evs:
        uid = e["user_id"]
        if not uid or config.is_admin(uid):
            continue
        p = people.setdefault(uid, {"kind": "", "nums": None, "done": False, "consult": False, "at": 0, "goal": ""})
        p["at"] = e["at"]
        if e["ev"] == "calc_start":
            p["kind"], p["done"] = e["extra"], False
        elif e["ev"] in ("calc_save", "calc_pension", "calc_kids"):
            p["kind"], p["done"] = e["ev"][5:], True
            try:
                p["nums"] = json.loads(e["extra"]) if e["extra"] else None
            except ValueError:
                p["nums"] = None
        elif e["ev"] == "goal":
            p["goal"] = T.GOALS.get(e["extra"], e["extra"])
        elif e["ev"] == "consult" and (e["extra"] or "").startswith("calc_"):
            p["consult"] = True
            p["kind"] = p["kind"] or e["extra"][5:]
    people = {u: p for u, p in people.items() if p["kind"] in calc.STEPS or p["goal"]}
    if not people:
        return [T.CALC_LIST_EMPTY]
    lines = []
    for n, (uid, p) in enumerate(sorted(people.items(), key=lambda x: -x[1]["at"])[:limit], 1):
        u = db.user(uid) or {}
        nick = u.get("username")
        contact = f"@{esc(nick)}" if nick else f'<a href="tg://user?id={uid}">{T.ADMIN_WRITE_LINK}</a> (без ніка)'
        day = datetime.fromtimestamp(p["at"], config.TZ).strftime("%d.%m")
        if p["kind"] not in calc.STEPS:  # тільки обрав ціль у «Корисному»
            lines.append(T.CALC_LIST_GOAL_ONLY.format(n=n, name=esc(u.get("name") or uid), contact=contact,
                                                      goal=esc(p["goal"]), day=day))
            continue
        a = p["nums"] or {}
        if p["kind"] == "pension" and a:
            nums = T.CALC_LIST_PENSION.format(age=a.get("age"), retire=a.get("retire"), m=calc.money(a.get("monthly", 0)))
        elif p["kind"] == "kids" and a:
            nums = T.CALC_LIST_KIDS.format(age=a.get("child_age"), target=a.get("target", 18), m=calc.money(a.get("monthly", 0)))
        elif p["kind"] == "save" and a:
            nums = T.CALC_LIST_SAVE.format(years=a.get("years"), m=calc.money(a.get("monthly", 0)))
        else:
            nums = ""
        if p["consult"]:
            status = T.CALC_LIST_CONSULT
        elif p["done"]:
            status = T.CALC_LIST_DONE
        else:
            st = state(uid)
            step = int(st.split(":")[2]) + 1 if st.startswith(f"calc:{p['kind']}:") else 0
            status = (T.CALC_LIST_STEP.format(step=step, total=len(calc.STEPS[p["kind"]])) if step
                      else T.CALC_LIST_STOPPED)
        item = T.CALC_LIST_ITEM.format(n=n, name=esc(u.get("name") or uid), contact=contact,
                                       title=T.CALC_LIST_TITLES[p["kind"]], nums=nums, status=status, day=day)
        if p["goal"]:
            item += "\n" + T.CALC_LIST_GOAL.format(goal=esc(p["goal"]))
        lines.append(item)
    parts, cur = [], T.CALC_LIST_HEAD.format(days=days, total=len(people), shown=len(lines))
    for line in lines:
        if len(cur) + len(line) > 3500:
            parts.append(cur)
            cur = ""
        cur += "\n\n" + line
    parts.append(cur.strip())
    return parts


# ================== звіт власниці о 9:00 ==================

def report_text(day=None):
    """Звіт за вчорашню добу (за Києвом): хто заходив, нові і звідки, що робили. Плюс рядки продажів, якщо є launch."""
    today = (day or datetime.now(config.TZ)).replace(hour=0, minute=0, second=0, microsecond=0)
    a = int((today - timedelta(days=1)).timestamp())
    b = a + 86400
    with db.conn() as c:
        new = [dict(r) for r in c.execute("SELECT src FROM users WHERE joined>=? AND joined<?", (a, b)).fetchall()]
        evs = [dict(r) for r in c.execute("SELECT user_id, ev, extra FROM events WHERE at>=? AND at<?", (a, b)).fetchall()]
    admins = set(config.ADMIN_IDS)
    evs = [e for e in evs if e["user_id"] not in admins and e["user_id"]]
    src = Counter((r["src"] if r["src"] not in ("", "direct") else "напряму") for r in new)
    acts = {}
    for e in evs:
        name = T.EV_NAMES.get(e["ev"], e["ev"])
        if e["ev"] == "product" and e["extra"] in T.PRODUCTS:
            name = f"{T.EV_NAMES['product']} «{T.PRODUCTS[e['extra']][0]}»"
        acts.setdefault(name, set()).add(e["user_id"])
    top = sorted(acts.items(), key=lambda x: -len(x[1]))[:10]
    text = T.REPORT.format(
        date=(today - timedelta(days=1)).strftime("%d.%m"), active=len({e["user_id"] for e in evs}), new=len(new),
        questions=sum(1 for e in evs if e["ev"] == "question"),
        sources="\n".join(f"• {esc(k)}: {v}" for k, v in src.most_common(8)) or T.REPORT_NONE,
        acts="\n".join(f"• {esc(k)}: {len(v)}" for k, v in top) or T.REPORT_NONE)
    if launch:
        text += "\n\n" + launch.report_lines(a, b)
    return text


async def report_job(context):
    now = datetime.now(config.TZ)
    if now.hour < T.REPORT_HOUR or not config.ADMIN_IDS:
        return
    day = now.strftime("%Y-%m-%d")
    if db.kv("report_day") == day:
        return
    db.kv("report_day", day)  # ДО відправки: після перезапуску другого звіту не буде
    await notify_admins(context.bot, report_text(now))


# ================== помилки, запуск ==================

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    if isinstance(context.error, Conflict):  # під час оновлення стара і нова копія агента кілька секунд працюють разом
        log.warning("conflict: %s", context.error)
        return
    if isinstance(context.error, (NetworkError, httpx.HTTPError)) and not isinstance(context.error, (BadRequest, Forbidden)):
        # короткий збій зв'язку з Telegram: агент сам перепідключиться, власниці не пишемо
        log.warning("network: %s", context.error)
        return
    log.exception("error", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_chat and update.effective_chat.type == "private":
            await context.bot.send_message(update.effective_chat.id, T.ERROR_USER)
    except Exception:
        pass
    await notify_admins(context.bot, T.ADMIN_ERROR.format(err=esc(str(context.error)[:300])))


async def post_init(app: Application):
    db.init()
    if launch:
        launch.setup()
    await app.bot.set_my_commands([BotCommand("start", "Почати"), BotCommand("menu", "Меню")])
    try:
        webserver.BOT_USERNAME = (await app.bot.get_me()).username or webserver.BOT_USERNAME  # для «Надіслати близьким»
    except Exception as e:
        log.warning("get_me: %s", e)
    for a in config.ADMIN_IDS:
        try:
            await app.bot.set_my_commands([BotCommand("start", "Почати"), BotCommand("menu", "Меню"),
                                           BotCommand("myid", "Мій Telegram ID"),
                                           BotCommand("dzherela", "Звідки прийшли люди")], scope=BotCommandScopeChat(a))
        except Exception as e:
            log.warning("admin commands %s: %s", a, e)
    # «Агента оновлено»: один раз на кожну нову версію з texts.VERSION
    if db.kv("version") != T.VERSION:
        db.kv("version", T.VERSION)
        await notify_admins(app.bot, T.ADMIN_UPDATED.format(version=T.VERSION, notes="\n".join(f"• {n}" for n in T.NOTES)))


def main():
    if not config.BOT_TOKEN:
        raise SystemExit("Немає BOT_TOKEN. Додайте його в Railway → Variables.")
    app = ApplicationBuilder().token(config.BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu_cmd))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("dzherela", dzherela))
    app.add_handler(CallbackQueryHandler(on_callback))
    if launch:
        launch.add_handlers(app)  # /grupa і задачі продажів за розкладом
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, on_message))
    app.job_queue.run_repeating(report_job, interval=600, first=60)  # звіт о 9:00, один раз на день
    app.job_queue.run_repeating(tips_job, interval=300, first=90)  # «Корисне» о TIPS_HOUR, один раз на день
    app.add_error_handler(on_error)
    webserver.start(config.PORT)  # мінізастосунок «Розрахунки»
    log.info("Агент запускається")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
