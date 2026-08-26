"""Завантаження й структурування даних із CSV-таблиць.
Таблиці веде відділ обліку вручну (можна тримати у Google Sheets і експортувати в CSV).
"""
import csv
from collections import OrderedDict, defaultdict

TYPE_COFFEE = "Кавомат"
TYPE_SNACK = "Снеки"


def load_catalog(path):
    """Повертає впорядкований каталог: [ {group, type, subs:[{name, items:[{name,unit,articul}]}], items:[...]} ].
    type групи визначається за позиціями (усі позиції групи одного типу)."""
    groups = OrderedDict()
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            g = groups.setdefault(r["group"], {"name": r["group"], "type": r["type"],
                                               "subs": OrderedDict(), "items": []})
            item = {"name": r["name"], "unit": r.get("unit") or "шт.",
                    "articul": r.get("articul") or "", "type": r["type"],
                    "display": (r.get("display") or r["name"])}
            if r["subcat"]:
                sub = g["subs"].setdefault(r["subcat"], {"name": r["subcat"], "items": []})
                sub["items"].append(item)
            else:
                g["items"].append(item)
    # convert subs dict -> list
    out = []
    for g in groups.values():
        g["subs"] = list(g["subs"].values())
        out.append(g)
    return out


def catalog_for_type(catalog, atype):
    """Каталог, відфільтрований за типом автомата (для інвентаризації)."""
    res = []
    for g in catalog:
        items = [i for i in g["items"] if i["type"] == atype]
        subs = [{"name": s["name"], "items": [i for i in s["items"] if i["type"] == atype]}
                for s in g["subs"]]
        subs = [s for s in subs if s["items"]]
        if items or subs:
            res.append({"name": g["name"], "type": g["type"], "items": items, "subs": subs})
    return res


def flat_items(catalog):
    """Плаский список позицій (для пошуку)."""
    out = []
    for g in catalog:
        out += g["items"]
        for s in g["subs"]:
            out += s["items"]
    return out


def load_locations(path):
    """Повертає:
       regions = OrderedDict{ region -> {"has_sub": bool, "sublocs": OrderedDict, "locs": OrderedDict} }
       де для звичайного регіону використовується locs (адреса -> [apparatus...]),
       а для регіону з підлокаціями (Львів) — sublocs (підлокація -> [apparatus...]).
    """
    regions = OrderedDict()
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            reg = regions.setdefault(r["region"], {"has_sub": False,
                                                   "sublocs": OrderedDict(),
                                                   "locs": OrderedDict()})
            app = {"inv": r["inv"], "atype": r["atype"], "model": r["model"],
                   "address": r["address"]}
            if r["sublocation"]:
                reg["has_sub"] = True
                sl = reg["sublocs"].setdefault(r["sublocation"], [])
                sl.append(app)
            else:
                loc = reg["locs"].setdefault(r["address"], [])
                loc.append(app)
    return regions


def load_employees(path):
    """telegram_id -> {"name", "role"}"""
    emp = {}
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            raw = (r.get("regions") or "").strip()
            # порожньо або "*" -> доступ до всіх регіонів (None); інакше набір ключів регіонів
            regions = None if raw in ("", "*") else {x.strip() for x in raw.split(";") if x.strip()}
            emp[str(r["telegram_id"])] = {"name": r["name"], "role": r.get("role", ""), "regions": regions}
    return emp


def resolve_requester(employees, telegram_id):
    return employees.get(str(telegram_id), employees.get("0", {"name": "Невідомий", "role": ""}))["name"]


def item_lookup(catalog):
    """Плаский довідник: назва -> {'articul', 'unit'} (для підстановки коду й од. виміру)."""
    m = {}
    for g in catalog:
        for it in g["items"]:
            m[it["name"]] = {"articul": it.get("articul", ""), "unit": it.get("unit", ""), "display": it.get("display", it["name"])}
        for s in g["subs"]:
            for it in s["items"]:
                m[it["name"]] = {"articul": it.get("articul", ""), "unit": it.get("unit", ""), "display": it.get("display", it["name"])}
    return m
