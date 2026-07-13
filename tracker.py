#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Слежка за выгодной ценой тура на слетать.ру.

Что делает за один запуск:
  1. Спрашивает у внутреннего API слетать.ру (module.sletat.ru/GetTours) цены
     по заданному отелю, датам, составу семьи.
  2. Оставляет только предложения нужного номера и питания (из config.json).
  3. Берёт минимальную цену, дописывает её в историю (data/history.jsonl).
  4. Сравнивает с обычной ценой за последние дни. Если упала на нужный процент
     (или ниже вашей суммы) — присылает сообщение в Telegram.
  5. Если ничего не упало — молчит.

Запускается по расписанию (launchd, каждые 16 минут). Ничего вводить не нужно.
"""

import json
import os
import ssl
import sys
import time
import urllib.parse
import urllib.request
import statistics
from datetime import datetime, timezone, timedelta

_UNVERIFIED = ssl._create_unverified_context()


def _urlopen(req):
    """Открываем URL. Сначала со строгой проверкой сертификата,
    при сбое (например, корпоративный/тестовый прокси) — мягко."""
    try:
        return urllib.request.urlopen(req, timeout=60)
    except urllib.error.URLError as e:
        if isinstance(e.reason, ssl.SSLError):
            return urllib.request.urlopen(req, timeout=60, context=_UNVERIFIED)
        raise

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
DATA_DIR = os.path.join(HERE, "data")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")
STATE_PATH = os.path.join(DATA_DIR, "state.json")
LOG_PATH = os.path.join(DATA_DIR, "tracker.log")

API = "https://module.sletat.ru/slt/Main.svc/GetTours"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://sletat.ru/",
}

# Индексы колонок в строке aaData (разобраны по живому ответу API)
C_TOURID = 0
C_ROOM_RU = 9
C_MEAL = 10
C_DEPART = 12
C_RETURN = 13
C_NIGHTS = 14
C_OPERATOR = 18
C_PRICE = 42
C_ROOM_EN = 53


def log(msg):
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    print(line)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def load_secrets():
    """Читаем токен бота и chat_id из общего .secrets.env в корне проекта."""
    token = os.environ.get("SLETAT_TG_BOT_TOKEN")
    chat = os.environ.get("SLETAT_TG_CHAT_ID")
    if token and chat:
        return token, chat
    # корень проекта = на два уровня выше (dev-project/sletat-price-agent/..)
    root = os.path.abspath(os.path.join(HERE, "..", ".."))
    env_path = os.path.join(root, ".secrets.env")
    vals = {}
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln or ln.startswith("#") or "=" not in ln:
                    continue
                k, v = ln.split("=", 1)
                vals[k.strip()] = v.strip()
    return (
        token or vals.get("SLETAT_TG_BOT_TOKEN"),
        chat or vals.get("SLETAT_TG_CHAT_ID"),
    )


def build_query(cfg, request_id):
    p = cfg["поездка"]
    params = {
        "requestId": request_id,
        "pageSize": 9999,
        "pageNumber": 1,
        "countryId": p["countryId"],
        "cityFromId": p["cityFromId"],
        "cities": p["resortId"],
        "meals": "",
        "stars": "",
        "features": "",
        "s_nightsMin": p["ночей_от"],
        "s_nightsMax": p["ночей_до"],
        "currencyAlias": "RUB",
        "groupBy": "",
        "includeDescriptions": 0,
        "includeOilTaxesAndVisa": 0,
        "minHotelRating": "",
        "s_showcase": "false",
        "filterToursForType": 0,
        "excludeToursForType": 0,
        "filterToursForTransportType": 0,
        "s_hotelIsNotInStop": "true",
        "s_hasTickets": "true",
        "s_ticketsIncluded": "true",
        "updateResult": 1,
        "hotels": p["hotelId"],
        "s_adults": p["взрослых"],
        "s_kids": p["детей"],
        "s_kids_ages": p["возраст_детей"],
        "s_departFrom": p["вылет_с"],
        "s_departTo": p["вылет_по"],
        "calcFullPrice": 1,
        "showHotelFacilities": 0,
    }
    # даты и запятые нельзя ломать — quote_via с safe
    return API + "?" + urllib.parse.urlencode(params, safe="/,")


def api_get(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with _urlopen(req) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_tours(cfg):
    """Инициируем поиск и опрашиваем, пока операторы не отдадут результат."""
    init = api_get(build_query(cfg, 0))
    data = init["GetToursResult"]["Data"]
    rid = data["requestId"]
    rows = data.get("aaData") or []
    for _ in range(12):  # до ~48 сек ожидания
        time.sleep(4)
        r = api_get(build_query(cfg, rid))
        d = r["GetToursResult"]["Data"]
        rows = d.get("aaData") or []
        load = d.get("loadState") or []
        pending = [o for o in load if not o["IsProcessed"] and not o["IsSkipped"]]
        if rows and not pending:
            break
        if rows and len(pending) <= 2:
            # большинство операторов ответили — хватит ждать «долгих»
            break
    return rows


def match(row, flt):
    room = (str(row[C_ROOM_EN]) + " " + str(row[C_ROOM_RU])).lower()
    inc = [s.lower() for s in flt["номер_содержит"]]
    exc = [s.lower() for s in flt["номер_исключить"]]
    if inc and not any(s in room for s in inc):
        return False
    if any(s in room for s in exc):
        return False
    meals = [m.upper() for m in flt["питание"]]
    if meals and str(row[C_MEAL]).upper() not in meals:
        return False
    return True


def best_offer(rows, flt):
    cand = [r for r in rows if match(r, flt)]
    if not cand:
        return None, len(rows)
    best = min(cand, key=lambda r: int(r[C_PRICE]))
    offer = {
        "price": int(best[C_PRICE]),
        "room": best[C_ROOM_RU],
        "meal": best[C_MEAL],
        "depart": best[C_DEPART],
        "ret": best[C_RETURN],
        "nights": best[C_NIGHTS],
        "operator": best[C_OPERATOR],
        "matched": len(cand),
        "total": len(rows),
    }
    return offer, len(rows)


def read_history(days):
    if not os.path.exists(HISTORY_PATH):
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out = []
    with open(HISTORY_PATH, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                rec = json.loads(ln)
                ts = datetime.fromisoformat(rec["ts"])
                if ts >= cutoff:
                    out.append(rec)
            except Exception:
                continue
    return out


def append_history(rec):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(HISTORY_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(st):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


def send_telegram(token, chat, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode(
        {"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "false"}
    ).encode()
    req = urllib.request.Request(url, data=data)
    with _urlopen(req) as r:
        return json.loads(r.read().decode("utf-8")).get("ok", False)


def rub(n):
    return f"{int(n):,}".replace(",", " ") + " ₽"


def main():
    cfg = load_config()
    sig = cfg["сигнал"]
    now = datetime.now(timezone.utc)

    try:
        rows = fetch_tours(cfg)
    except Exception as e:
        log(f"ОШИБКА запроса к API: {e}")
        return 1

    offer, total = best_offer(rows, cfg["фильтр_предложений"])
    if not offer:
        log(f"Нет предложений под фильтр (всего строк: {total}). Пропуск.")
        return 0

    price = offer["price"]
    append_history({
        "ts": now.isoformat(),
        "price": price,
        "room": offer["room"],
        "meal": offer["meal"],
        "depart": offer["depart"],
        "ret": offer["ret"],
        "nights": offer["nights"],
        "operator": offer["operator"],
    })
    log(f"Текущий минимум: {rub(price)} | {offer['room']} {offer['meal']} | "
        f"{offer['depart']}-{offer['ret']} {offer['nights']}н | {offer['operator']} "
        f"(совпало {offer['matched']} из {total})")

    # база = медиана прошлых замеров (без текущего)
    hist = read_history(sig["окно_базовой_цены_дней"])
    prev = [h["price"] for h in hist[:-1]] if len(hist) >= 1 else []
    state = load_state()

    reasons = []
    baseline = None
    if len(prev) >= sig["минимум_замеров_для_базы"]:
        baseline = statistics.median(prev)
        drop_pct = (baseline - price) / baseline * 100
        if drop_pct >= sig["порог_падения_процент"]:
            reasons.append(
                f"Цена упала на {drop_pct:.0f}% относительно обычной "
                f"({rub(baseline)} → {rub(price)})"
            )
    else:
        log(f"Базы пока мало ({len(prev)} замеров, нужно "
            f"{sig['минимум_замеров_для_базы']}) — коплю историю, сигнал не шлю.")

    target = sig.get("целевая_цена")
    if target and price <= target:
        reasons.append(f"Цена ниже вашей планки {rub(target)}")

    if not reasons:
        save_state(state)
        return 0

    # анти-спам: не повторяем тот же сигнал часто, но шлём при доп. падении
    last_price = state.get("last_alert_price")
    last_ts = state.get("last_alert_ts")
    should = True
    if last_price is not None and last_ts:
        hours = (now - datetime.fromisoformat(last_ts)).total_seconds() / 3600
        further = price <= last_price * (1 - sig["повтор_при_доп_падении_процент"] / 100)
        if hours < sig["не_повторять_сигнал_часов"] and not further:
            should = False

    if not should:
        log("Выгодная цена сохраняется, но сигнал уже отправляли недавно — молчу.")
        save_state(state)
        return 0

    token, chat = load_secrets()
    if not token or not chat:
        log("НЕТ токена/chat_id Telegram — не могу отправить.")
        return 1

    base_line = f"\nОбычная цена ~{rub(baseline)}" if baseline else ""
    text = (
        f"🔥 <b>Выгодная цена!</b>\n"
        f"<b>{cfg['поездка']['название']}</b>\n\n"
        f"💰 <b>{rub(price)}</b>{base_line}\n"
        f"🛏 {offer['room']} · {offer['meal']}\n"
        f"📅 {offer['depart']} → {offer['ret']} ({offer['nights']} ночей)\n"
        f"👨‍👩‍👧‍👦 {cfg['поездка']['взрослых']} взр + дети {cfg['поездка']['возраст_детей']}\n"
        f"🏢 Оператор: {offer['operator']}\n\n"
        f"➡️ https://sletat.ru/turkey/acisu/gloria_golf_resort/\n\n"
        f"<i>{'; '.join(reasons)}</i>"
    )
    ok = send_telegram(token, chat, text)
    if ok:
        log(f"СИГНАЛ отправлен: {rub(price)} ({'; '.join(reasons)})")
        state["last_alert_price"] = price
        state["last_alert_ts"] = now.isoformat()
    else:
        log("Не удалось отправить сообщение в Telegram.")
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
