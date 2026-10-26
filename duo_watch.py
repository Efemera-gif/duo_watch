#!/usr/bin/env python3
"""
iPhone Duo — наличие для Pickup в Apple Store ОАЭ -> уведомление в Telegram.

Запуск на Windows: двойной клик по start.bat.
При первом запуске скрипт попросит токен бота и сохранит его в token.txt.

Скрипт не использует ваш Apple ID и не трогает приложение Apple Store:
он опрашивает тот же публичный JSON, из которого сайт и приложение
берут статус «Available today / Unavailable» по каждому магазину.
"""
import os
import random
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(HERE, "token.txt")

import requests

# ================== НАСТРОЙКИ ==================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")  # в облаке берётся из секрета; локально спросится при запуске
CHAT_ID = os.environ.get("CHAT_ID", "")  # пусто — возьмётся из последнего сообщения боту
MAX_RUNTIME = int(os.environ.get("MAX_RUNTIME", "0"))  # секунд; 0 = работать бесконечно

# Ближневосточные артикулы (AH/A). Если при старте бот напишет, что Apple
# не знает артикул — откройте страницу покупки на apple.com/ae, выберите
# модель и подставьте артикул оттуда.
PARTS = {
    "MK264AH/A": "iPhone Duo 512GB Star White",
    "MK274AH/A": "iPhone Duo 512GB Night Sky",
}

LOCATION = "Dubai"     # поиск ближайших магазинов; ОАЭ маленькие, попадут все
INTERVAL = 4.0         # секунд между проверками (ниже 3 — риск временного бана)
JITTER = 1.5           # случайная добавка, чтобы запросы не шли ровно по таймеру
REPEAT_ALERTS = 3      # сколько раз подряд «долбить» уведомлением при появлении
# ===============================================

COUNTRY = "ae"
BUY_URL = f"https://www.apple.com/{COUNTRY}/shop/buy-iphone/iphone-duo"
API_URL = f"https://www.apple.com/{COUNTRY}/shop/fulfillment-messages"
TG_URL = ""

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-AE,en;q=0.9",
    "Referer": BUY_URL,
}

session = requests.Session()
session.headers.update(HEADERS)


# ---------------- Telegram ----------------
def tg_send(text: str) -> None:
    try:
        requests.post(
            f"{TG_URL}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
    except requests.RequestException as e:
        print(f"[tg] send failed: {e}")


def load_token() -> str:
    tok = BOT_TOKEN.strip()
    if not tok and os.path.exists(TOKEN_FILE):
        tok = open(TOKEN_FILE, encoding="utf-8").read().strip()
    if (not tok or ":" not in tok) and not sys.stdin.isatty():
        sys.exit("BOT_TOKEN не задан.")
    while not tok or ":" not in tok:
        tok = input("Вставьте токен бота от @BotFather и нажмите Enter: ").strip()
    r = requests.get(f"https://api.telegram.org/bot{tok}/getMe", timeout=10).json()
    if not r.get("ok"):
        if os.path.exists(TOKEN_FILE):
            os.remove(TOKEN_FILE)
        sys.exit("Telegram не принял токен. Запустите снова и вставьте токен заново.")
    if not BOT_TOKEN.strip():
        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            f.write(tok)
    print(f"Бот: @{r['result']['username']}")
    return tok


def keep_awake() -> None:
    """Windows: не даём компьютеру уснуть, пока бот работает."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)
        except Exception:
            pass


def resolve_chat_id() -> str:
    r = requests.get(f"{TG_URL}/getUpdates", timeout=10).json()
    if not r.get("ok"):
        sys.exit(f"Telegram отклонил токен: {r}")
    for upd in reversed(r.get("result", [])):
        msg = upd.get("message") or upd.get("channel_post")
        if msg:
            return str(msg["chat"]["id"])
    input("Откройте своего бота в Telegram, отправьте ему любое сообщение и нажмите здесь Enter...")
    return resolve_chat_id()


# ---------------- Apple ----------------
class Blocked(Exception):
    pass


def fetch():
    params = [("pl", "true"), ("mts.0", "regular"), ("searchNearby", "true"),
              ("location", LOCATION)]
    for i, part in enumerate(PARTS):
        params.append((f"parts.{i}", part))
    r = session.get(API_URL, params=params, timeout=8)
    if r.status_code in (403, 429, 503, 541):
        raise Blocked(f"HTTP {r.status_code}")
    r.raise_for_status()
    return r.json()["body"]["content"]["pickupMessage"]


def parse(pm):
    """-> (available{(store, part): (storeName, quote)}, stores[], known_parts set)"""
    available, stores, known = {}, [], set()
    for s in pm.get("stores", []):
        stores.append(s.get("storeName", s.get("storeNumber", "?")))
        for part, info in (s.get("partsAvailability") or {}).items():
            known.add(part)
            if info.get("pickupDisplay") == "available":
                quote = info.get("storePickupQuote") or info.get("pickupSearchQuote") or ""
                available[(s.get("storeNumber"), part)] = (s.get("storeName", "?"), quote)
    return available, stores, known


# ---------------- main loop ----------------
def main():
    global CHAT_ID, TG_URL
    TG_URL = f"https://api.telegram.org/bot{load_token()}"
    keep_awake()
    if not CHAT_ID:
        CHAT_ID = resolve_chat_id()
        print(f"CHAT_ID = {CHAT_ID}")

    prev = {}
    first = True
    errors = 0
    warned = False

    deadline = time.time() + MAX_RUNTIME if MAX_RUNTIME else None
    while deadline is None or time.time() < deadline:
        try:
            pm = fetch()
            now, stores, known = parse(pm)

            if first:
                first = False
                missing = [p for p in PARTS if p not in known]
                msg = ("✅ Мониторинг запущен.\nМагазины: " + (", ".join(stores) or "—") +
                       f"\nИнтервал: ~{INTERVAL:.0f}с")
                if not stores:
                    msg += "\n⚠️ Apple не вернул магазины — проверьте LOCATION."
                if missing:
                    msg += "\n⚠️ Apple не знает артикулы: " + ", ".join(missing)
                tg_send(msg)
                print(msg)

            if warned:
                tg_send("🟢 Связь с Apple восстановлена, мониторинг продолжается.")
                warned = False
            errors = 0

            for key, (store, quote) in now.items():
                if key not in prev:
                    text = (f"🚨 <b>{PARTS.get(key[1], key[1])}</b>\n"
                            f"📍 {store}\n🕒 {quote}\n\n👉 {BUY_URL}")
                    for _ in range(REPEAT_ALERTS):
                        tg_send(text)
                        time.sleep(0.7)
            for key, (store, _) in prev.items():
                if key not in now:
                    tg_send(f"⚪️ Закончилось: {PARTS.get(key[1], key[1])} — {store}")
            prev = now

            stamp = time.strftime("%H:%M:%S")
            print(f"{stamp} ok, available: {len(now)}")
            time.sleep(INTERVAL + random.uniform(0, JITTER))

        except Blocked as e:
            errors += 1
            wait = min(300, 30 * 2 ** (errors - 1))
            print(f"{time.strftime('%H:%M:%S')} {e} -> пауза {wait}с")
            if errors >= 2 and not warned:
                tg_send(f"⚠️ Apple временно ограничил запросы ({e}). Жду и продолжаю.")
                warned = True
            time.sleep(wait)
        except (requests.RequestException, ValueError, KeyError) as e:
            errors += 1
            print(f"{time.strftime('%H:%M:%S')} error: {e}")
            if errors == 5 and not warned:
                tg_send(f"⚠️ Ошибки при проверке: {e}")
                warned = True
            time.sleep(min(60, 5 * errors))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nОстановлено.")
