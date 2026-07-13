#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Слежка за ценами на авиабилеты на СТРОГО заданные даты (живой поиск Aviasales).

Источник — живой поиск билетов виджета слетать.ру (aviasales.sletat.ru).
Живой поиск закрыт для «безголового» робота (403), поэтому используем
настоящий браузер (Playwright, headless=False). В облаке он запускается
под виртуальным экраном xvfb.

Для каждой поездки открываем ссылку поиска, перехватываем ответы движка
(/search/wl/results) и берём минимальную цену туда-обратно на точные даты.

Логика сигналов та же, что у трекера туров: копим историю по каждой поездке,
и если цена упала на N% от обычной (или ниже суммы) — шлём в Telegram.
"""

import json
import os
import sys
import time
import statistics
from datetime import datetime, timezone, timedelta

import tracker  # send_telegram, load_secrets, rub

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "flights.json")
DATA_DIR = os.path.join(HERE, "data")
HISTORY_PATH = os.path.join(DATA_DIR, "flights_history.jsonl")
STATE_PATH = os.path.join(DATA_DIR, "flights_state.json")
LOG_PATH = os.path.join(DATA_DIR, "flights.log")


def log(msg):
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  [билеты] {msg}"
    print(line)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def ddmm(iso):
    return datetime.fromisoformat(iso).strftime("%d.%m")


def search_code(route, depart, ret):
    d = datetime.fromisoformat(depart).strftime("%d%m")
    r = datetime.fromisoformat(ret).strftime("%d%m")
    return f"{route['откуда_код']}{d}{route['куда_код']}{r}1"  # 1 пассажир, эконом


def flight_link(route, depart, ret):
    return f"https://aviasales.sletat.ru/search/{search_code(route, depart, ret)}"


def _min_price(bodies):
    """Минимальная цена туда-обратно из ответов /results."""
    prices = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ("value", "price", "unified_price") and isinstance(v, (int, float)) and v > 5000:
                    prices.append(v)
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    for b in bodies:
        walk(b)
    return int(min(prices)) if prices else None


def fetch_live_prices(route, trips, wait_s=26):
    """Открываем браузер один раз и по очереди ищем цены по каждой поездке."""
    from playwright.sync_api import sync_playwright

    out = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, args=["--no-sandbox", "--disable-dev-shm-usage"])
        for trip in trips:
            code = search_code(route, trip["туда"], trip["обратно"])
            bodies = []
            page = browser.new_page()

            def on_resp(resp, _b=bodies):
                if "/search/wl/results" in resp.url:
                    try:
                        _b.append(resp.json())
                    except Exception:
                        pass

            page.on("response", on_resp)
            try:
                page.goto(f"https://aviasales.sletat.ru/search/{code}",
                          wait_until="domcontentloaded", timeout=60000)
                waited = 0
                while waited < wait_s * 1000:
                    page.wait_for_timeout(2000)  # прокачивает цикл событий (в отличие от time.sleep)
                    waited += 2000
                    if _min_price(bodies) and len(bodies) > 4:
                        break
                out[trip["id"]] = _min_price(bodies)
            except Exception as e:
                log(f"{trip['id']}: ошибка браузера — {e}")
                out[trip["id"]] = None
            finally:
                page.close()
        browser.close()
    return out


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
                if datetime.fromisoformat(rec["ts"]) >= cutoff:
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


def evaluate(route, trip, price, sig, state, now):
    append_history({
        "ts": now.isoformat(), "trip": trip["id"], "price": price,
        "depart": trip["туда"], "ret": trip["обратно"],
    })
    log(f"{trip['id']} ({ddmm(trip['туда'])}→{ddmm(trip['обратно'])}): {tracker.rub(price)}")

    hist = [h for h in read_history(sig["окно_базовой_цены_дней"]) if h.get("trip") == trip["id"]]
    prev = [h["price"] for h in hist[:-1]]

    reasons = []
    baseline = None
    if len(prev) >= sig["минимум_замеров_для_базы"]:
        baseline = statistics.median(prev)
        drop = (baseline - price) / baseline * 100
        if drop >= sig["порог_падения_процент"]:
            reasons.append(f"Цена упала на {drop:.0f}% ({tracker.rub(baseline)} → {tracker.rub(price)})")
    target = sig.get("целевая_цена")
    if target and price <= target:
        reasons.append(f"Цена ниже вашей планки {tracker.rub(target)}")
    if not reasons:
        return

    tstate = state.get(trip["id"], {})
    last_price = tstate.get("last_alert_price")
    last_ts = tstate.get("last_alert_ts")
    if last_price is not None and last_ts:
        hours = (now - datetime.fromisoformat(last_ts)).total_seconds() / 3600
        further = price <= last_price * (1 - sig["повтор_при_доп_падении_процент"] / 100)
        if hours < sig["не_повторять_сигнал_часов"] and not further:
            log(f"{trip['id']}: выгодно, но сигнал слали недавно — молчу.")
            return

    token, chat = tracker.load_secrets()
    if not token or not chat:
        log("нет токена/chat_id — не отправить.")
        return
    base_line = f"\nОбычная цена ~{tracker.rub(baseline)}" if baseline else ""
    text = (
        f"✈️ <b>Дешёвый билет!</b>\n"
        f"<b>{route['откуда']} → {route['куда']} → {route['откуда']}</b>\n\n"
        f"💰 <b>{tracker.rub(price)}</b> за 1 пассажира, туда-обратно{base_line}\n"
        f"📅 Туда {ddmm(trip['туда'])}, обратно {ddmm(trip['обратно'])}\n\n"
        f"➡️ <a href=\"{flight_link(route, trip['туда'], trip['обратно'])}\">Открыть билеты на слетать.ру</a>\n\n"
        f"<i>{'; '.join(reasons)}</i>"
    )
    if tracker.send_telegram(token, chat, text):
        log(f"{trip['id']}: СИГНАЛ отправлен ({'; '.join(reasons)})")
        state[trip["id"]] = {"last_alert_price": price, "last_alert_ts": now.isoformat()}
    else:
        log(f"{trip['id']}: не удалось отправить.")


def main():
    cfg = load_config()
    route = cfg["маршрут"]
    sig = cfg["сигнал"]
    now = datetime.now(timezone.utc)
    state = load_state()

    prices = fetch_live_prices(route, cfg["поездки"])
    for trip in cfg["поездки"]:
        price = prices.get(trip["id"])
        if not price:
            log(f"{trip['id']} ({ddmm(trip['туда'])}→{ddmm(trip['обратно'])}): цена не получена (поиск не отдал результат).")
            continue
        evaluate(route, trip, price, sig, state, now)
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
