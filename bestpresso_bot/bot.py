"""Bestpresso · Заявки — Telegram-бот (aiogram v3).
Каркас реалізує узгоджений флоу: Замовлення / Інвентаризація -> регіон ->
(Львів: підлокація) -> (замовлення: каталог / інвентаризація: автомат -> каталог) ->
формування xlsx-бланку -> надсилання отримувачу.

Стан сесії (обраний режим, локація, кошик) зберігається у FSM-контексті.
Запуск: заповніть config.py (BOT_TOKEN, отримувачі) і `python bot.py`.
"""
import asyncio
import logging
from datetime import date

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (Message, CallbackQuery, FSInputFile, ErrorEvent,
                           InlineKeyboardButton, InlineKeyboardMarkup)

import os

import config
import data_loader as dl
import blanks
import mailer

os.makedirs(config.OUTPUT_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO)

# --- завантаження довідників у памʼять (перечитуються командою /reload) ---
CATALOG = dl.load_catalog(config.CATALOG_CSV)
LOCATIONS = dl.load_locations(config.LOCATIONS_CSV)
EMPLOYEES = dl.load_employees(config.EMPLOYEES_CSV)
CODE_UNIT = dl.item_lookup(CATALOG)
DISPLAY = {n: info.get("display", n) for n, info in CODE_UNIT.items()}


def d(name):
    """Скорочена назва для показу; для логіки/файлу лишається оригінал."""
    return DISPLAY.get(name, name)


bot = Bot(config.BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# --- обмеження доступу: боту відповідають лише співробітники з employees.csv ---
from aiogram import BaseMiddleware


def _allowed_ids():
    return {k for k in EMPLOYEES.keys() if k not in ("0", "")}


class AccessMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is not None and str(user.id) not in _allowed_ids():
            if getattr(event, "message", None):
                await event.message.answer(
                    "\u26d4 Доступ до бота обмежено. "
                    "Зверніться до відділу обліку, "
                    "щоб вас додали."
                )
            elif getattr(event, "callback_query", None):
                await event.callback_query.answer("Доступ обмежено", show_alert=True)
            return
        return await handler(event, data)


dp.update.outer_middleware(AccessMiddleware())


# ---------- helpers для клавіатур ----------
def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def btn(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


def paginate(items, page, per_page):
    pages = max(1, (len(items) + per_page - 1) // per_page)
    page = max(0, min(page, pages - 1))
    return items[page * per_page:(page + 1) * per_page], page, pages


def tag(atype):
    return "☕" if atype == dl.TYPE_COFFEE else "🥤"


# ---------- екран 0: вибір режиму ----------
async def show_mode(msg_or_cb):
    await _send(msg_or_cb, "Вітаю 👋 Оберіть дію:",
                kb([[btn("🛒 Замовлення", "mode:order")],
                    [btn("📋 Інвентаризація", "mode:inv")]]))


@dp.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    await show_mode(m)


@dp.message(F.text == "/id")
async def whoami(m: Message):
    await m.answer(
        f"Ваш Telegram-ID: {m.from_user.id}\n"
        f"chat_id цього чату: {m.chat.id}\n\n"
        f"Число зверху вставте у config.py (RECIPIENT_*) "
        f"та у data/employees.csv (telegram_id)."
    )


@dp.message(F.text == "/reload")
async def reload_data(m: Message):
    global CATALOG, LOCATIONS, EMPLOYEES, CODE_UNIT, DISPLAY
    CATALOG = dl.load_catalog(config.CATALOG_CSV)
    LOCATIONS = dl.load_locations(config.LOCATIONS_CSV)
    EMPLOYEES = dl.load_employees(config.EMPLOYEES_CSV)
    CODE_UNIT = dl.item_lookup(CATALOG)
    DISPLAY = {n: info.get("display", n) for n, info in CODE_UNIT.items()}
    await m.answer("Довідники перечитано ✅")


@dp.callback_query(F.data == "mode:order")
async def pick_order(cb: CallbackQuery, state: FSMContext):
    await state.update_data(mode="order", cart={})
    await show_regions(cb, state)


@dp.callback_query(F.data == "mode:inv")
async def pick_inv(cb: CallbackQuery, state: FSMContext):
    await state.update_data(mode="inv", inv_carts={}, inv_done={})
    await show_regions(cb, state)


# ---------- екран 1: регіони ----------
def visible_regions(uid):
    """Список регіонів, доступних цьому користувачу (за employees.csv)."""
    scope = EMPLOYEES.get(str(uid), {}).get("regions")
    keys = list(LOCATIONS.keys())
    return keys if scope is None else [r for r in keys if r in scope]


async def enter_region(cb, state, region):
    data = await state.get_data()
    await state.update_data(region=region, page=0)
    reg = LOCATIONS[region]
    if data["mode"] == "order" and not reg["has_sub"]:
        await state.update_data(target_label=region.replace("_", " ") + " (усі локації)",
                                subloc=None, address=None)
        await show_catalog(cb, state, top=True)
    else:
        await show_locations(cb, state)


async def show_regions(cb, state):
    vis = visible_regions(cb.from_user.id)
    if not vis:
        await cb.message.answer("Вам ще не призначено регіон. Зверніться до відділу обліку.")
        await cb.answer(); return
    if len(vis) == 1:                      # один регіон — пропускаємо вибір
        await enter_region(cb, state, vis[0]); return
    rows = [[btn(f"📍 {r.replace(chr(95),' ')}", f"reg:{i}")] for i, r in enumerate(vis)]
    await _edit(cb, "Оберіть регіон / філіал:", kb(rows))


@dp.callback_query(F.data.startswith("reg:"))
async def pick_region(cb: CallbackQuery, state: FSMContext):
    vis = visible_regions(cb.from_user.id)
    idx = int(cb.data.split(":")[1])
    if idx >= len(vis):
        await cb.answer("Регіон недоступний", show_alert=True); return
    await enter_region(cb, state, vis[idx])


# ---------- екран 2: локації / підлокації ----------
async def show_locations(cb, state):
    data = await state.get_data()
    reg = LOCATIONS[data["region"]]
    entries = list(reg["sublocs"].items()) if reg["has_sub"] else list(reg["locs"].items())
    page_items, page, pages = paginate(entries, data.get("page", 0), config.LOCS_PER_PAGE)
    rows = []
    base = page * config.LOCS_PER_PAGE
    for j, (label, apps) in enumerate(page_items):
        rows.append([btn(f"{label[:40]}  ·  {len(apps)} авт.", f"loc:{base + j}")])
    nav = []
    if pages > 1:
        nav = [[btn("◀", "locpage:-1"), btn(f"{page+1}/{pages}", "noop"), btn("▶", "locpage:1")]]
    rows += nav + [[btn("⬅️ До регіонів", "back:regions")]]
    await _edit(cb, f"{data['region'].replace('_',' ')}\nОберіть локацію:", kb(rows))


@dp.callback_query(F.data.startswith("locpage:"))
async def loc_page(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    await state.update_data(page=d.get("page", 0) + int(cb.data.split(":")[1]))
    await show_locations(cb, state)


@dp.callback_query(F.data.startswith("loc:"))
async def pick_location(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    reg = LOCATIONS[data["region"]]
    entries = list(reg["sublocs"].items()) if reg["has_sub"] else list(reg["locs"].items())
    label, apps = entries[int(cb.data.split(":")[1])]
    await state.update_data(loc_label=label, loc_apps=apps)
    if data["mode"] == "order":
        # Замовлення по локації (Львів) -> каталог
        await state.update_data(target_label=label, subloc=(label if reg["has_sub"] else None),
                                address=label)
        await show_catalog(cb, state, top=True)
    else:
        await show_apparatuses(cb, state)


# ---------- екран 3 (інвентаризація): автомати ----------
async def show_apparatuses(cb, state):
    data = await state.get_data()
    apps = data["loc_apps"]
    done = data.get("inv_done", {})
    page_items, page, pages = paginate(apps, data.get("apage", 0), config.ITEMS_PER_PAGE)
    rows = []
    base = page * config.ITEMS_PER_PAGE
    for j, a in enumerate(page_items):
        mark = "✅ " if a["inv"] in done else ""
        rows.append([btn(f"{mark}Автомат №{a['inv']} {tag(a['atype'])} {a.get('model','')}".strip(),
                         f"app:{base + j}")])
    if pages > 1:
        rows.append([btn("◀", "apage:-1"), btn(f"{page+1}/{pages}", "noop"), btn("▶", "apage:1")])
    if done:
        rows.append([btn(f"📤 Завершити ({len(done)} файл.)", "inv:finish")])
    rows.append([btn("⬅️ Інша локація", "back:locations")])
    await _edit(cb, f"{data['loc_label'][:60]}\nОберіть автомат (рахуємо залишок по кожному):", kb(rows))


@dp.callback_query(F.data.startswith("apage:"))
async def app_page(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    await state.update_data(apage=d.get("apage", 0) + int(cb.data.split(":")[1]))
    await show_apparatuses(cb, state)


@dp.callback_query(F.data.startswith("app:"))
async def pick_apparatus(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    a = data["loc_apps"][int(cb.data.split(":")[1])]
    await state.update_data(cur_app=a)
    await show_catalog(cb, state, top=True)


# ---------- екран 4: каталог (групи -> підкатегорії -> позиції) ----------
def active_catalog(data):
    if data["mode"] == "inv":
        return dl.catalog_for_type(CATALOG, data["cur_app"]["atype"])
    return CATALOG


async def show_catalog(cb, state, top=False):
    data = await state.get_data()
    cat = active_catalog(data)
    rows = [[btn(f"📂 {g['name']}", f"grp:{i}")] for i, g in enumerate(cat)]
    rows.append([btn("🔍 Пошук", "search"), btn(f"🛒 Кошик ({_cart_count(data)})", "cart")])
    end = "✅ Оформити замовлення" if data["mode"] == "order" else f"💾 Зберегти №{data['cur_app']['inv']}"
    rows.append([btn(end, "finish")])
    rows.append([[btn("⬅️ Інший автомат", "back:apps")] if data["mode"] == "inv"
                 else btn("⬅️ Змінити локацію", "back:loc_or_reg")][0]
                if False else
                (btn("⬅️ Інший автомат", "back:apps") if data["mode"] == "inv"
                 else btn("⬅️ Змінити локацію/філіал", "back:loc_or_reg")))
    # normalize last row to a list
    if not isinstance(rows[-1], list):
        rows[-1] = [rows[-1]]
    await _edit(cb, "Оберіть категорію:", kb(rows))


@dp.callback_query(F.data.startswith("grp:"))
async def pick_group(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    g = active_catalog(data)[int(cb.data.split(":")[1])]
    await state.update_data(gi=int(cb.data.split(":")[1]), page=0)
    if g["subs"]:
        rows = [[btn(f"{s['name']} ({len(s['items'])})", f"sub:{i}")] for i, s in enumerate(g["subs"])]
        rows += [[btn(i["display"][:60], f"itg:{k}")] for k, i in enumerate(g["items"])]
        rows.append([btn("⬅️ Категорії", "back:catalog")])
        await _edit(cb, g["name"], kb(rows))
    else:
        await show_items(cb, state, g["items"], g["name"])


@dp.callback_query(F.data.startswith("sub:"))
async def pick_sub(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    g = active_catalog(data)[data["gi"]]
    s = g["subs"][int(cb.data.split(":")[1])]
    await state.update_data(si=int(cb.data.split(":")[1]), page=0)
    await show_items(cb, state, s["items"], s["name"])


async def show_items(cb, state, items, title):
    data = await state.get_data()
    page_items, page, pages = paginate(items, data.get("page", 0), config.ITEMS_PER_PAGE)
    base = page * config.ITEMS_PER_PAGE
    rows = [[btn(it["display"][:60], f"it:{base + k}")] for k, it in enumerate(page_items)]
    if pages > 1:
        rows.append([btn("◀", "itpage:-1"), btn(f"{page+1}/{pages}", "noop"), btn("▶", "itpage:1")])
    rows.append([btn("⬅️ Назад", "back:catalog")])
    await state.update_data(cur_items=[it["name"] for it in items])
    await _edit(cb, f"{title}\nОберіть позицію:", kb(rows))


@dp.callback_query(F.data.startswith("itpage:"))
async def it_page(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    await state.update_data(page=d.get("page", 0) + int(cb.data.split(":")[1]))
    # re-render current items
    await _rerender_items(cb, state)


async def _rerender_items(cb, state):
    data = await state.get_data()
    g = active_catalog(data)[data["gi"]]
    if data.get("si") is not None and g["subs"]:
        s = g["subs"][data["si"]]
        await show_items(cb, state, s["items"], s["name"])
    else:
        await show_items(cb, state, g["items"], g["name"])


@dp.callback_query(F.data.startswith("it:"))
async def pick_item(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    name = data["cur_items"][int(cb.data.split(":")[1])]
    await _ask_qty(cb, state, name)


async def _ask_qty(cb, state, name):
    data = await state.get_data()
    await state.update_data(pending=name)
    prompt = ("Скільки одиниць замовити?" if data["mode"] == "order"
              else f"Який залишок на автоматі №{data['cur_app']['inv']}?")
    presets = [0, 1, 2, 3, 5, 10]
    row = [btn(str(v), f"qty:{v}") for v in presets]
    rows = [row[:3], row[3:], [btn("✏️ Інша кількість", "qty:manual")], [btn("⬅️ Скасувати", "back:catalog")]]
    await _edit(cb, f"{prompt}\n{d(name)}\n(введіть число або оберіть кнопку)", kb(rows))


@dp.callback_query(F.data.startswith("qty:"))
async def set_qty(cb: CallbackQuery, state: FSMContext):
    val = cb.data.split(":")[1]
    if val == "manual":
        await cb.message.answer("Надішліть кількість числом:")
        await cb.answer()
        return
    await _add_to_cart(state, int(val))
    await show_catalog(cb, state)


@dp.message(F.text.regexp(r"^\d+$"))
async def qty_typed(m: Message, state: FSMContext):
    data = await state.get_data()
    if not data.get("pending"):
        return
    await _add_to_cart(state, int(m.text))
    await m.answer("Записано ✅. /menu — продовжити")


async def _add_to_cart(state, qty):
    data = await state.get_data()
    name = data["pending"]
    if data["mode"] == "order":
        cart = data.get("cart", {}); cart[name] = qty
        await state.update_data(cart=cart, pending=None)
    else:
        carts = data.get("inv_carts", {})
        c = carts.setdefault(data["cur_app"]["inv"], {}); c[name] = qty
        await state.update_data(inv_carts=carts, pending=None)


def _cart_count(data):
    if data["mode"] == "order":
        return len(data.get("cart", {}))
    return len(data.get("inv_carts", {}).get(data.get("cur_app", {}).get("inv"), {}))


async def _deliver(subject, body, paths, tg_recipient):
    """Надсилає файли листом; якщо пошта недоступна — резерв у Telegram.
    Повертає 'email' / 'telegram' / 'none'."""
    if config.SEND_EMAIL:
        try:
            mailer.send_email(subject, body, config.EMAIL_TO, paths, config)
            return "email"
        except Exception as e:
            logging.exception("Email failed: %s", e)
    if config.TELEGRAM_FALLBACK:
        for pth in paths:
            await bot.send_document(tg_recipient, FSInputFile(pth), caption=subject[:1000])
        return "telegram"
    return "none"


# ---------- фініш: формування й надсилання xlsx ----------
@dp.callback_query(F.data == "finish")
async def finish(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    req = dl.resolve_requester(EMPLOYEES, cb.from_user.id)
    if data["mode"] == "order":
        cart = data.get("cart", {})
        if not cart:
            await cb.answer("Кошик порожній", show_alert=True); return
        d = date.today().strftime("%d.%m.%Y")
        place = data["target_label"].replace(" (усі локації)", "")
        subject = f"Замовлення — {place} — {d} — {req}"
        fname = subject + ".xlsx"
        path = blanks.build_blank(cart, CODE_UNIT, config.OUTPUT_DIR, fname)
        body = ("Доброго дня! У вкладенні замовлення. "
                "На лист відповідати не треба — це автоматична розсилка.")
        via = await _deliver(subject, body, [path], config.RECIPIENT_ORDERS)
        await cb.message.answer(
            "Замовлення сформовано й надіслано на пошту ✅" if via == "email"
            else "Пошта недоступна — надіслано в Telegram (резерв) ⚠️" if via == "telegram"
            else "Не вдалося надіслати ❌")
        await state.clear()
    else:
        carts = data.get("inv_carts", {})
        cur = data["cur_app"]["inv"]
        if not carts.get(cur):
            await cb.answer("Порожньо", show_alert=True); return
        done = data.get("inv_done", {}); done[cur] = data["cur_app"]
        await state.update_data(inv_done=done, cur_app=None)
        await show_apparatuses(cb, state)


@dp.callback_query(F.data == "inv:finish")
async def inv_finish(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    req = dl.resolve_requester(EMPLOYEES, cb.from_user.id)
    carts, done = data.get("inv_carts", {}), data.get("inv_done", {})
    if not done:
        await cb.answer("Немає збережених автоматів", show_alert=True); return
    d = date.today().strftime("%d.%m.%Y")
    place = data["loc_label"]
    paths = []
    for inv, app in done.items():
        fname = f"Інвентаризація — авт {inv} — {place} — {d} — {req}.xlsx"
        p = blanks.build_blank(carts.get(inv, {}), CODE_UNIT, config.OUTPUT_DIR, fname)
        paths.append(p)
    nums = ", ".join(done.keys())
    subject = f"Інвентаризація — авт {nums} — {place} — {d} — {req}"
    body = ("Доброго дня! У вкладенні інвентаризація по автоматах в Регіонах. "
            "На лист відповідати не треба — це автоматична розсилка.")
    via = await _deliver(subject, body, paths, config.RECIPIENT_INVENTORY)
    await cb.message.answer(
        f"Інвентаризацію надіслано на пошту 📧 (файлів: {len(paths)})" if via == "email"
        else f"Пошта недоступна — надіслано в Telegram (резерв) ⚠️ (файлів: {len(paths)})" if via == "telegram"
        else "Не вдалося надіслати ❌")
    await state.clear()


# ---------- навігація назад ----------
@dp.callback_query(F.data.startswith("back:"))
async def go_back(cb: CallbackQuery, state: FSMContext):
    where = cb.data.split(":")[1]
    data = await state.get_data()
    if where == "regions":
        await show_regions(cb, state)
    elif where == "locations":
        await show_locations(cb, state)
    elif where == "apps":
        await show_apparatuses(cb, state)
    elif where == "catalog":
        await show_catalog(cb, state, top=True)
    elif where == "loc_or_reg":
        reg = LOCATIONS[data["region"]]
        if data["mode"] == "order" and not reg["has_sub"]:
            await show_regions(cb, state)
        else:
            await show_locations(cb, state)


@dp.callback_query(F.data == "cart")
async def show_cart(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    items = (data.get("cart", {}) if data["mode"] == "order"
             else data.get("inv_carts", {}).get(data["cur_app"]["inv"], {}))
    if not items:
        await cb.answer("Порожньо", show_alert=True); return
    txt = "\n".join(f"• {d(n)} — {q}" for n, q in items.items())
    await cb.message.answer("Кошик:\n" + txt + "\n\n/menu — продовжити")
    await cb.answer()


@dp.callback_query(F.data == "search")
async def search_prompt(cb: CallbackQuery, state: FSMContext):
    await state.update_data(searching=True)
    await cb.message.answer("Надішліть частину назви позиції для пошуку:")
    await cb.answer()


@dp.message(F.text.len() >= 2)
async def search_run(m: Message, state: FSMContext):
    data = await state.get_data()
    if not data.get("searching"):
        return
    cat = active_catalog(data)
    q = m.text.lower()
    found = [it for it in dl.flat_items(cat) if q in it["name"].lower() or q in it["display"].lower()][:12]
    if not found:
        await m.answer("Нічого не знайдено.")
        return
    await state.update_data(cur_items=[it["name"] for it in found], searching=False)
    rows = [[btn(it["display"][:60], f"it:{k}")] for k, it in enumerate(found)]
    await m.answer("Знайдено:", reply_markup=kb(rows))


@dp.message(F.text == "/menu")
async def menu(m: Message, state: FSMContext):
    await show_catalog_msg(m, state)


async def show_catalog_msg(m, state):
    data = await state.get_data()
    cat = active_catalog(data)
    rows = [[btn(f"📂 {g['name']}", f"grp:{i}")] for i, g in enumerate(cat)]
    rows.append([btn(f"🛒 Кошик ({_cart_count(data)})", "cart")])
    end = "✅ Оформити замовлення" if data["mode"] == "order" else f"💾 Зберегти №{data['cur_app']['inv']}"
    rows.append([btn(end, "finish")])
    await m.answer("Оберіть категорію:", reply_markup=kb(rows))


@dp.callback_query(F.data == "noop")
async def noop(cb: CallbackQuery):
    await cb.answer()


# ---------- утиліти відправки ----------
async def _send(target, text, markup):
    if isinstance(target, CallbackQuery):
        await target.message.answer(text, reply_markup=markup); await target.answer()
    else:
        await target.answer(text, reply_markup=markup)


async def _edit(cb: CallbackQuery, text, markup):
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except Exception:
        await cb.message.answer(text, reply_markup=markup)
    await cb.answer()


@dp.errors()
async def on_error(event: ErrorEvent):
    """Мʼяко обробляє збої (напр. натискання старої кнопки після перезапуску,
    коли стан сесії втрачено) — просить почати заново замість падіння."""
    logging.exception("Handler error: %s", event.exception)
    upd = event.update
    try:
        if getattr(upd, "callback_query", None):
            await upd.callback_query.answer()
            await upd.callback_query.message.answer(
                "Сесія оновилась. Натисніть /start, щоб почати заново.")
        elif getattr(upd, "message", None):
            await upd.message.answer(
                "Сталася помилка. Натисніть /start, щоб почати заново.")
    except Exception:
        pass
    return True


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
