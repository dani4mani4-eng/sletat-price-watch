#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Слежка за ценами на авиабилеты (Магнитогорск ↔ Москва и т.п.).

Источник цен — открытый ценовой календарь Aviasales, который использует
виджет билетов на слетать.ру (endpoint apistp.com GraphQL prices_round_trip).
Работает без авторизации и без браузера: обычный POST-запрос.

Важно: календарь отдаёт минимальные цены по датам, которые недавно искали.
По строго заданным датам цена бывает не всегда — тогда замер пропускается.

Логика сигналов та же, что у трекера туров: копим историю по каждой поездке,
и если цена упала на N% от обычной (или ниже заданной суммы) — шлём в Telegram.
"""

import json
import os
import ssl
import sys
import statistics
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta, date

import tracker  # переиспользуем send_telegram, load_secrets, rub, _urlopen

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "flights.json")
DATA_DIR = os.path.join(HERE, "data")
HISTORY_PATH = os.path.join(DATA_DIR, "flights_history.jsonl")
STATE_PATH = os.path.join(DATA_DIR, "flights_state.json")
LOG_PATH = os.path.join(DATA_DIR, "flights.log")

GQL_URL = "https://api.apistp.com/whitelabels/web/flights/v1/prices/graphql/query"
GQL_QUERY = (
    "query Q($origin: String!, $destination: String!, $limit: Int!, $offset: Int!, "
    "$minDepartStart: Date!, $maxDepartStart: Date!, $minReturnStart: Date!, $maxReturnStart: Date!, "
    "$currency: String!, $withBaggage: Boolean, $directOnly: Boolean, $tripClass: TripClass) { "
    "prices_round_trip(paging: {offset: $offset, limit: $limit}, params: {origin: $origin, "
    "destination: $destination, depart_date_min: $minDepartStart, depart_date_max: $maxDepartStart, "
    "return_date_min: $minReturnStart, return_date_max: $maxReturnStart, with_baggage: $withBaggage, "
    "direct: $directOnly, trip_class: $tripClass}, currency: $currency) "
    "{ departure_at return_at value __typename } }"
)


def log(msg):
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  [билеты] {msg}"
    print(line)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def _shift(iso, days):
    y, m, d = map(int, iso.split("-"))
    return (date(y, m, d) + timedelta(days=days)).isoformat()


def query_prices(route, dep, ret, span=5):
    """Спрашиваем календарь по диапазону вокруг дат (min==max сервер не любит)."""
    variables = {
        "origin": route["откуда_код"],
        "destination": route["куда_код"],
        "limit": 365,
        "offset": 0,
        "minDepartStart": _shift(dep, -span),
        "maxDepartStart": _shift(dep, span),
        "minReturnStart": _shift(ret, -span),
        "maxReturnStart": _shift(ret, span),
        "currency": "RUB",
        "directOnly": False,
        "withBaggage": False,
        "tripClass": "Y",
    }
    body = json.dumps({"operationName": "Q", "variables": variables, "query": GQL_QUERY}).encode()
    req = urllib.request.Request(
        GQL_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Referer": "https://aviasales.sletat.ru/",
            "Origin": "https://aviasales.sletat.ru",
            "User-Agent": "Mozilla/5.0",
            "affiliate-marker": str(route["affiliate_marker"]),
        },
    )
    with tracker._urlopen(req) as r:
        data = json.loads(r.read().decode("utf-8"))
    return (data.get("data") or {}).get("prices_round_trip") or []


def best_for_trip(route, trip, flex):
    """Минимальная цена в пределах гибкости дней от заданных дат."""
    rows = query_prices(route, trip["туда"], trip["обратно"])
    cand = []
    for x in rows:
        dp = x["departure_at"][:10]
        rp = x["return_at"][:10]
        if abs((date.fromisoformat(dp) - date.fromisoformat(trip["туда"])).days) <= flex and \
           abs((date.fromisoformat(rp) - date.fromisoformat(trip["обратно"])).days) <= flex:
            cand.append({"price": int(round(x["value"])), "depart": dp, "ret": rp})
    if not cand:
        return None
    return min(cand, key=lambda c: c["price"])


def flight_link(route, depart, ret):
    d = datetime.fromisoformat(depart).strftime("%d%m")
    r = datetime.fromisoformat(ret).strftime("%d%m")
    return f"https://aviasales.sletat.ru/search/{route['откуда_код']}{d}{route['куда_код']}{r}1"


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


def ddmm(iso):
    return datetime.fromisoformat(iso).strftime("%d.%m")


def process_trip(route, trip, sig, flex, state, now):
    try:
        offer = best_for_trip(route, trip, flex)
    except Exception as e:
        log(f"{trip['id']}: ошибка запроса — {e}")
        return
    if not offer:
        log(f"{trip['id']} ({ddmm(trip['туда'])}→{ddmm(trip['обратно'])}): цены на эти даты сейчас нет в календаре.")
        return

    price = offer["price"]
    append_history({
        "ts": now.isoformat(),
        "trip": trip["id"],
        "price": price,
        "depart": offer["depart"],
        "ret": offer["ret"],
    })
    log(f"{trip['id']} ({ddmm(offer['depart'])}→{ddmm(offer['ret'])}): {tracker.rub(price)}")

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
    should = True
    if last_price is not None and last_ts:
        hours = (now - datetime.fromisoformat(last_ts)).total_seconds() / 3600
        further = price <= last_price * (1 - sig["повтор_при_доп_падении_процент"] / 100)
        if hours < sig["не_повторять_сигнал_часов"] and not further:
            should = False
    if not should:
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
        f"📅 Туда {ddmm(offer['depart'])}, обратно {ddmm(offer['ret'])}\n\n"
        f"➡️ <a href=\"{flight_link(route, offer['depart'], offer['ret'])}\">Открыть билеты на слетать.ру</a>\n\n"
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
    flex = int(cfg.get("гибкость_дней", 0))
    now = datetime.now(timezone.utc)
    state = load_state()
    for trip in cfg["поездки"]:
        process_trip(route, trip, sig, flex, state, now)
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
