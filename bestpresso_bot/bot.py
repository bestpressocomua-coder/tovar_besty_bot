"""Bestpresso · Заявки — Telegram-бот (aiogram v3).
Флоу: Замовлення / Інвентаризація -> регіон -> (локація) -> каталог -> xlsx -> лист.
Замовлення: звичайний філіал = на весь філіал; Львів = по локації.
Інвентаризація: завжди по локації. Один файл на заявку.
"""
import asyncio
import logging
import os
from datetime import date

from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (Message, CallbackQuery, FSInputFile, ErrorEvent,
                           InlineKeyboardButton, InlineKeyboardMarkup)

import config
import data_loader as dl
import blanks
import mailer

os.makedirs(config.OUTPUT_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO)

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


# ---------- доступ ----------
def _allowed_ids():
    return {k for k in EMPLOYEES.keys() if k not in ("0", "")}


class AccessMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is not None and str(user.id) not in _allowed_ids():
            if getattr(event, "message", None):
                await event.message.answer("⛔ Доступ до бота обмежено. Зверніться до відділу обліку, щоб вас додали.")
            elif getattr(event, "callback_query", None):
                await event.callback_query.answer("Доступ обмежено", show_alert=True)
            return
        return await handler(event, data)


dp.update.outer_middleware(AccessMiddleware())


# ---------- helpers ----------
def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def btn(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


def paginate(items, page, per_page):
    pages = max(1, (len(items) + per_page - 1) // per_page)
    page = max(0, min(page, pages - 1))
    return items[page * per_page:(page + 1) * per_page], page, pages


def _ctx(data):
    mode = "🛒 Замовлення" if data["mode"] == "order" else "📋 Інвентаризація"
    place = data.get("loc_label") or data.get("target_label", "")
    return f"{mode} · {place}\n"


def _cart_count(data):
    return len(data.get("cart", {}))


# ---------- екран 0: режим ----------
async def show_mode(target):
    await _send(target, "Вітаю 👋 Оберіть дію:",
                kb([[btn("🛒 Замовлення", "mode:order")],
                    [btn("📋 Інвентаризація", "mode:inv")]]))


@dp.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    await show_mode(m)


@dp.message(F.text == "/id")
async def whoami(m: Message):
    await m.answer(f"Ваш Telegram-ID: {m.from_user.id}\nchat_id цього чату: {m.chat.id}")


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
    await state.update_data(mode="order", cart={}, loc_label=None)
    await show_regions(cb, state)


@dp.callback_query(F.data == "mode:inv")
async def pick_inv(cb: CallbackQuery, state: FSMContext):
    await state.update_data(mode="inv", cart={}, loc_label=None)
    await show_regions(cb, state)


# ---------- екран 1: регіони ----------
def visible_regions(uid):
    scope = EMPLOYEES.get(str(uid), {}).get("regions")
    keys = list(LOCATIONS.keys())
    return keys if scope is None else [r for r in keys if r in scope]


async def enter_region(cb, state, region):
    data = await state.get_data()
    await state.update_data(region=region, lpage=0, loc_label=None)
    reg = LOCATIONS[region]
    if data["mode"] == "order" and not reg["has_sub"]:
        await state.update_data(target_label=region.replace("_", " ") + " (усі локації)")
        await show_catalog(cb, state)
    else:
        await show_locations(cb, state)


async def show_regions(cb, state):
    vis = visible_regions(cb.from_user.id)
    if not vis:
        await cb.message.answer("Вам ще не призначено регіон. Зверніться до відділу обліку.")
        await cb.answer(); return
    if len(vis) == 1:
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


# ---------- екран 2: локації ----------
async def show_locations(cb, state):
    data = await state.get_data()
    reg = LOCATIONS[data["region"]]
    entries = list(reg["sublocs"].items()) if reg["has_sub"] else list(reg["locs"].items())
    page_items, page, pages = paginate(entries, data.get("lpage", 0), config.LOCS_PER_PAGE)
    base = page * config.LOCS_PER_PAGE
    rows = [[btn(f"{label[:44]}", f"loc:{base + j}")] for j, (label, apps) in enumerate(page_items)]
    if pages > 1:
        rows.append([btn("◀", "locpage:-1"), btn(f"{page+1}/{pages}", "noop"), btn("▶", "locpage:1")])
    rows.append([btn("⬅️ До регіонів", "back:regions")])
    await _edit(cb, f"{data['region'].replace('_',' ')}\nОберіть локацію:", kb(rows))


@dp.callback_query(F.data.startswith("locpage:"))
async def loc_page(cb: CallbackQuery, state: FSMContext):
    st = await state.get_data()
    await state.update_data(lpage=st.get("lpage", 0) + int(cb.data.split(":")[1]))
    await show_locations(cb, state)


@dp.callback_query(F.data.startswith("loc:"))
async def pick_location(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    reg = LOCATIONS[data["region"]]
    entries = list(reg["sublocs"].items()) if reg["has_sub"] else list(reg["locs"].items())
    label, apps = entries[int(cb.data.split(":")[1])]
    await state.update_data(loc_label=label, target_label=label)
    await show_catalog(cb, state)


# ---------- екран 3: каталог ----------
async def show_catalog(cb, state):
    data = await state.get_data()
    rows = [[btn(f"📂 {g['name']}", f"grp:{i}")] for i, g in enumerate(CATALOG)]
    rows.append([btn("🔍 Пошук", "search"), btn(f"🛒 Кошик ({_cart_count(data)})", "cart")])
    end = "✅ Оформити замовлення" if data["mode"] == "order" else "📤 Відправити інвентаризацію"
    rows.append([btn(end, "finish")])
    rows.append([btn("⬅️ Змінити локацію/філіал", "back:loc_or_reg")])
    await _edit(cb, _ctx(data) + "Оберіть категорію:", kb(rows))


async def show_subs(cb, state):
    data = await state.get_data()
    g = CATALOG[data["gi"]]
    rows = [[btn(f"{s['name']} ({len(s['items'])})", f"sub:{i}")] for i, s in enumerate(g["subs"])]
    rows += [[btn(i["display"][:60], f"itg:{k}")] for k, i in enumerate(g["items"])]
    rows.append([btn("⬅️ Категорії", "back:catalog")])
    await _edit(cb, _ctx(data) + g["name"], kb(rows))


@dp.callback_query(F.data.startswith("grp:"))
async def pick_group(cb: CallbackQuery, state: FSMContext):
    gi = int(cb.data.split(":")[1])
    await state.update_data(gi=gi, si=None, ipage=0, from_search=False)
    if CATALOG[gi]["subs"]:
        await show_subs(cb, state)
    else:
        await render_items_edit(cb, state)


@dp.callback_query(F.data.startswith("sub:"))
async def pick_sub(cb: CallbackQuery, state: FSMContext):
    si = int(cb.data.split(":")[1])
    await state.update_data(si=si, ipage=0, from_search=False)
    await render_items_edit(cb, state)


@dp.callback_query(F.data.startswith("itg:"))
async def pick_item_group(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    g = CATALOG[data["gi"]]
    name = g["items"][int(cb.data.split(":")[1])]["name"]
    await _ask_qty(cb, state, name)


def _items_payload(data):
    """(title, rows, names) для поточного екрана позицій — з каталогу або з пошуку."""
    if data.get("from_search"):
        names = data.get("cur_items", [])
        items = [{"name": n, "display": d(n)} for n in names]
        title = "Знайдено"
        back = ("⬅️ Назад до меню", "back:catalog")
    else:
        g = CATALOG[data["gi"]]
        si = data.get("si")
        if si is None:
            items, title = g["items"], g["name"]
            back = ("⬅️ Категорії", "back:catalog")
        else:
            items, title = g["subs"][si]["items"], g["subs"][si]["name"]
            back = ("⬅️ Назад", "back:subs")
    page_items, page, pages = paginate(items, data.get("ipage", 0), config.ITEMS_PER_PAGE)
    base = page * config.ITEMS_PER_PAGE
    rows = [[btn(it["display"][:60], f"it:{base + k}")] for k, it in enumerate(page_items)]
    if pages > 1:
        rows.append([btn("◀", "itpage:-1"), btn(f"{page+1}/{pages}", "noop"), btn("▶", "itpage:1")])
    rows.append([btn(back[0], back[1])])
    return _ctx(data) + title + "\nОберіть позицію:", rows, [it["name"] for it in items]


async def render_items_edit(cb, state):
    data = await state.get_data()
    text, rows, names = _items_payload(data)
    await state.update_data(cur_items=names)
    await _edit(cb, text, kb(rows))


async def render_items_msg(m, state):
    data = await state.get_data()
    text, rows, names = _items_payload(data)
    await state.update_data(cur_items=names)
    await m.answer(text, reply_markup=kb(rows))


@dp.callback_query(F.data.startswith("itpage:"))
async def it_page(cb: CallbackQuery, state: FSMContext):
    st = await state.get_data()
    await state.update_data(ipage=st.get("ipage", 0) + int(cb.data.split(":")[1]))
    await render_items_edit(cb, state)


@dp.callback_query(F.data.startswith("it:"))
async def pick_item(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    name = data["cur_items"][int(cb.data.split(":")[1])]
    await _ask_qty(cb, state, name)


async def _ask_qty(cb, state, name):
    data = await state.get_data()
    await state.update_data(pending=name)
    prompt = "Скільки одиниць замовити?" if data["mode"] == "order" else "Який залишок?"
    presets = [0, 1, 2, 3, 5, 10]
    row = [btn(str(v), f"qty:{v}") for v in presets]
    rows = [row[:3], row[3:], [btn("✏️ Інша кількість", "qty:manual")], [btn("⬅️ Назад", "back:items")]]
    await _edit(cb, f"{prompt}\n{d(name)}\n(введіть число або оберіть кнопку)", kb(rows))


@dp.callback_query(F.data.startswith("qty:"))
async def set_qty(cb: CallbackQuery, state: FSMContext):
    val = cb.data.split(":")[1]
    if val == "manual":
        await cb.message.answer("Надішліть кількість числом:")
        await cb.answer(); return
    await _add_to_cart(state, int(val))
    await render_items_edit(cb, state)


@dp.message(F.text.regexp(r"^\d+$"))
async def qty_typed(m: Message, state: FSMContext):
    data = await state.get_data()
    if not data.get("pending"):
        return
    await _add_to_cart(state, int(m.text))
    await m.answer("Записано ✅")
    await render_items_msg(m, state)


async def _add_to_cart(state, qty):
    data = await state.get_data()
    cart = data.get("cart", {})
    cart[data["pending"]] = qty
    await state.update_data(cart=cart, pending=None)


# ---------- пошук / кошик ----------
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
    q = m.text.lower()
    found = [it for it in dl.flat_items(CATALOG)
             if q in it["name"].lower() or q in it["display"].lower()][:12]
    if not found:
        await m.answer("Нічого не знайдено.")
        return
    names = [it["name"] for it in found]
    await state.update_data(cur_items=names, from_search=True, searching=False, ipage=0)
    rows = [[btn(it["display"][:60], f"it:{k}")] for k, it in enumerate(found)]
    rows.append([btn("⬅️ Назад до меню", "back:catalog")])
    await m.answer("Знайдено:", reply_markup=kb(rows))


@dp.callback_query(F.data == "cart")
async def show_cart(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    items = data.get("cart", {})
    if not items:
        await cb.answer("Порожньо", show_alert=True); return
    txt = "\n".join(f"• {d(n)} — {q}" for n, q in items.items())
    await cb.message.answer("Кошик:\n" + txt)
    await cb.answer()


# ---------- фініш ----------
async def _deliver(subject, body, paths, tg_recipient):
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


@dp.callback_query(F.data == "finish")
async def finish(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    req = dl.resolve_requester(EMPLOYEES, cb.from_user.id)
    cart = data.get("cart", {})
    if not cart:
        await cb.answer("Список порожній", show_alert=True); return
    dd = date.today().strftime("%d.%m.%Y")
    place = (data.get("loc_label") or data.get("target_label", "")).replace(" (усі локації)", "")
    if data["mode"] == "order":
        subject = f"Замовлення — {place} — {dd} — {req}"
        body = "Доброго дня! У вкладенні замовлення. На лист відповідати не треба — це автоматична розсилка."
        recipient = config.RECIPIENT_ORDERS
        done_msg = "Замовлення сформовано й надіслано"
    else:
        subject = f"Інвентаризація — {place} — {dd} — {req}"
        body = "Доброго дня! У вкладенні інвентаризація. На лист відповідати не треба — це автоматична розсилка."
        recipient = config.RECIPIENT_INVENTORY
        done_msg = "Інвентаризацію сформовано й надіслано"
    path = blanks.build_blank(cart, CODE_UNIT, config.OUTPUT_DIR, subject + ".xlsx")
    via = await _deliver(subject, body, [path], recipient)
    await cb.message.answer(
        f"{done_msg} на пошту ✅" if via == "email"
        else "Пошта недоступна — надіслано в Telegram (резерв) ⚠️" if via == "telegram"
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
    elif where == "catalog":
        await show_catalog(cb, state)
    elif where == "subs":
        await show_subs(cb, state)
    elif where == "items":
        await render_items_edit(cb, state)
    elif where == "loc_or_reg":
        reg = LOCATIONS[data["region"]]
        if data["mode"] == "order" and not reg["has_sub"]:
            await show_regions(cb, state)
        else:
            await show_locations(cb, state)


@dp.callback_query(F.data == "noop")
async def noop(cb: CallbackQuery):
    await cb.answer()


# ---------- утиліти ----------
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
    logging.exception("Handler error: %s", event.exception)
    upd = event.update
    try:
        if getattr(upd, "callback_query", None):
            await upd.callback_query.answer()
            await upd.callback_query.message.answer("Сесія оновилась. Натисніть /start, щоб почати заново.")
        elif getattr(upd, "message", None):
            await upd.message.answer("Сталася помилка. Натисніть /start, щоб почати заново.")
    except Exception:
        pass
    return True


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
