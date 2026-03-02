import os
import re
import time
import hashlib
import json
from datetime import datetime, timezone
import requests
import asyncio
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from telegram.request import HTTPXRequest
from dotenv import load_dotenv

load_dotenv()
# -------- Safe Telegram Message Sender --------
TG_MAX_LEN = 3900

def _chunk_text(text: str, limit: int = TG_MAX_LEN):
    if not text:
        return [""]

    chunks = []
    text = str(text)

    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut == -1 or cut < limit * 0.5:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()

    if text:
        chunks.append(text)
    return chunks

async def safe_send(update, context, text, **kwargs):
    for part in _chunk_text(text):
        if part:
            await safe_send(update, context,
                chat_id=update.effective_chat.id,
                text=part,
                **kwargs
            )

# — Конфигурация только из .env
BOT_TOKEN = os.getenv("TG_BOT_TOKEN")

# API key используется для получения временного session token через /apilogin
API_KEY = os.getenv("GGSEL_API_KEY")

# Идентификатор продавца для эндпоинтов продаж
SELLER_ID = os.getenv("SELLER_ID")

# Динамический токен, выдаваемый /apilogin
API_TOKEN: str | None = None
API_TOKEN_EXPIRES_AT: float = 0.0

# Заголовки для API-запросов (некоторые эндпоинты отдают HTML без этих заголовков)
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://seller.ggsel.com/",
    "Origin": "https://seller.ggsel.com",
}

# Для некоторых эндпоинтов требуется заголовок locale
HEADERS_LOCALE_RU = {**HEADERS, "locale": "ru"}

# Базовые URL официального API
BASE_API = "https://seller.ggsel.com"
DEBATES_URL = f"{BASE_API}/api_sellers/api/debates/v2"
DEBATES_CHATS_URL = f"{BASE_API}/api_sellers/api/debates/v2/chats"
LAST_SALES_URL = f"{BASE_API}/api_sellers/api/seller-last-sales"
PURCHASE_INFO_URL = f"{BASE_API}/api_sellers/api/purchase/info"
API_LOGIN_URL = f"{BASE_API}/api_sellers/api/apilogin"

# Текст кнопки в постоянном нижнем меню
BUTTON_TEXT = "💬 Проверить сообщения"
BUTTON_TEXT_ORDERS = "🧾 Проверить заказы"
BUTTON_TEXT_DEBUG = "🔍 Диагностика API"

# === Клиент официального API ===
def _json_or_error(resp: requests.Response):
    try:
        return resp.json()
    except ValueError:
        content_type = resp.headers.get("Content-Type", "")
        snippet = (resp.text or "")[:300].replace("\n", " ")
        raise RuntimeError(
            f"GGSEL API non-JSON response (status={resp.status_code}, ct={content_type}). Body: {snippet}"
        )


def _auth_headers(locale_ru: bool = False, with_bearer: bool = True) -> dict:
    # Отдаём и стандартные заголовки, и Bearer как запасной вариант авторизации
    headers = dict(HEADERS_LOCALE_RU if locale_ru else HEADERS)
    if with_bearer and API_TOKEN:
        headers.setdefault("Authorization", f"Bearer {API_TOKEN}")
    return headers


def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _ensure_api_token(force_refresh: bool = False):
    global API_TOKEN, API_TOKEN_EXPIRES_AT
    if not SELLER_ID or not str(SELLER_ID).strip():
        raise RuntimeError("Не задан SELLER_ID")
    if not API_KEY:
        raise RuntimeError("Не задан API ключ (GGSEL_API_KEY)")
    now = time.time()
    if API_TOKEN and not force_refresh and API_TOKEN_EXPIRES_AT - now > 30:
        return
    ts = str(int(now))
    sign = _sha256_hex(f"{API_KEY}{ts}")
    payload = {"seller_id": int(SELLER_ID), "timestamp": ts, "sign": sign}
    headers = _auth_headers(locale_ru=True, with_bearer=False)
    r = requests.post(API_LOGIN_URL, json=payload, headers=headers, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"apilogin HTTP {r.status_code}: {(r.text or '')[:160]}")
    data = r.json() if r.headers.get("Content-Type", "").startswith("application/json") else {}
    token = (data or {}).get("token")
    if not token:
        desc = (data or {}).get("desc") or (data or {}).get("retdesc") or "—"
        raise RuntimeError(f"apilogin вернул ошибку: {desc}")
    API_TOKEN = token
    # истечение срока
    valid_thru = (data or {}).get("valid_thru")
    expires_at = now + 1800
    if isinstance(valid_thru, str):
        try:
            dt = datetime.fromisoformat(valid_thru.replace("Z", "+00:00"))
            expires_at = dt.timestamp()
        except Exception:
            pass
    API_TOKEN_EXPIRES_AT = expires_at


def _request_json(url: str, params: dict | None = None, locale_ru: bool = False, timeout: int = 60, _retry: bool = True) -> dict | list:
    params = dict(params or {})
    _ensure_api_token()
    headers = _auth_headers(locale_ru=locale_ru)
    if API_TOKEN and "token" not in params:
        params["token"] = API_TOKEN
    resp = requests.get(url, params=params, headers=headers, timeout=timeout)
    if resp.status_code == 401:
        redacted_url = re.sub(r"(token=)[^&]+", r"\1***", resp.url or url)
        if _retry:
            _ensure_api_token(force_refresh=True)
            return _request_json(url, params, locale_ru, timeout, _retry=False)
        raise RuntimeError(
            f"GGSEL API 401 Unauthorized: {redacted_url}. Проверьте SELLER_ID и API ключ."
        )
    resp.raise_for_status()
    return _json_or_error(resp)


 
def api_list_chats(filter_new: int | None = None, page: int = 1, pagesize: int = 20, email: str | None = None):
    if not API_KEY:
        raise RuntimeError("Не задан API ключ (GGSEL_API_KEY)")
    params = {
        "token": API_TOKEN or "",
        "page": page,
        "pagesize": pagesize,
    }
    if filter_new is not None:
        params["filter_new"] = filter_new
    if email:
        params["email"] = email
    data = _request_json(DEBATES_CHATS_URL, params=params, locale_ru=False, timeout=25) or {}
    items = data.get("items") if isinstance(data, dict) else None
    return items or []


def api_list_messages(conversation_id: int, count: int = 50, newer: int | None = None):
    if not API_KEY:
        raise RuntimeError("Не задан API ключ (GGSEL_API_KEY)")
    params = {
        "token": API_TOKEN or "",
        "id_i": conversation_id,
        "count": min(max(count, 1), 100),
    }
    if newer is not None:
        params["newer"] = newer
    data = _request_json(DEBATES_URL, params=params, locale_ru=False, timeout=25)
    return data if isinstance(data, list) else []

def api_last_sales(top: int = 4):
    if not API_KEY:
        raise RuntimeError("Не задан API ключ (GGSEL_API_KEY)")
    effective_seller_id = SELLER_ID
    if not effective_seller_id:
        raise RuntimeError("Не задан SELLER_ID")
    params = {
        "token": API_TOKEN or "",
        "seller_id": int(effective_seller_id),
        "top": max(1, min(int(top), 100)),
    }
    data = _request_json(LAST_SALES_URL, params=params, locale_ru=True, timeout=60) or {}
    return data.get("sales", [])


def api_purchase_info(invoice_id: int):
    if not API_KEY:
        raise RuntimeError("Не задан API ключ (GGSEL_API_KEY)")
    url = f"{PURCHASE_INFO_URL}/{invoice_id}"
    params = {"token": API_TOKEN or ""}
    data = _request_json(url, params=params, locale_ru=True, timeout=60) or {}
    return data.get("content") or {}


# === Логика проверки ===
def _select_last_unread_buyer_message(messages: list[dict]) -> dict | None:
    if not messages:
        return None
    # Выбираем САМУЮ ПОСЛЕДНЮЮ реплику покупателя по дате (а не по порядку массива)
    def _to_ts(s: str | None) -> float:
        if not s:
            return float("-inf")
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return dt.timestamp()
        except Exception:
            try:
                return float(s)
            except Exception:
                return float("-inf")

    latest_msg: dict | None = None
    latest_ts: float = float("-inf")
    for msg in messages:
        try:
            is_buyer = int(msg.get("buyer", 0)) == 1
            is_deleted = int(msg.get("deleted", 0)) == 1
        except Exception:
            continue
        if not is_buyer or is_deleted:
            continue
        ts = _to_ts(msg.get("date_written") or msg.get("created_at") or None)
        if ts > latest_ts:
            latest_ts = ts
            latest_msg = msg
    return latest_msg


def get_unread():
    """Возвращает список словарей {chat_item, last_message} по непрочитанным чатам (только по /chats)."""
    # Просим сервер сразу отдать только новые чаты
    chats = api_list_chats(filter_new=1, page=1, pagesize=20)
    result = []
    for chat in chats:
        chat_id = chat.get("id_i")
        # Тянем сообщения диалога и ищем последнее непрочитанное от покупателя
        msgs = []
        try:
            # сначала пробуем получить 1 последний элемент — если API отдаёт в порядке "последний первым"
            msgs = api_list_messages(conversation_id=int(chat_id), count=1)
        except Exception:
            msgs = []
        # если пришло не то (нет покупателя) — добираем пачку и выбираем по времени
        if not msgs or not _select_last_unread_buyer_message(msgs):
            try:
                msgs = api_list_messages(conversation_id=int(chat_id), count=100)
            except Exception:
                msgs = []
        last_buyer_msg = _select_last_unread_buyer_message(msgs) or (msgs[0] if msgs else None)
        result.append({"chat": chat, "message": last_buyer_msg})
    return result


def get_recent_orders():
    """Возвращает только оплаченные заказы по официальному API"""
    sales = api_last_sales(top=4)
    paid = []
    for sale in sales:
        invoice_id = sale.get("invoice_id")
        if invoice_id is None:
            continue
        info = api_purchase_info(int(invoice_id))
        # Признаком оплаты считаем наличие даты оплаты
        is_paid = bool(info.get("date_pay"))
        if not is_paid:
            continue
        buyer_email = (info.get("buyer_info") or {}).get("email") or "—"
        amount = info.get("amount")
        currency = info.get("currency_type") or ""
        amount_str = f"{amount} {currency}" if amount is not None else "—"
        item_name = info.get("name") or ((sale.get("product") or {}).get("name") or "—")
        created_at = info.get("purchase_date") or sale.get("date") or "—"
        paid.append({
            "number": invoice_id,
            "offer_title": item_name,
            "buyer_email": buyer_email,
            "amount": amount_str,
            "status": "paid",
            "created_at": created_at,
        })
    return paid

def format_alert(chat_and_msg: dict):
    """Форматирует сообщение для отправки. Если нет текста последнего сообщения,
    строим уведомление по cnt_new/last_message из /chats."""
    chat = chat_and_msg.get("chat", {})
    msg = chat_and_msg.get("message") or {}
    email = chat.get("email") or "—"
    conversation_id = chat.get("id_i") or "—"
    product_id = chat.get("product")
    product_label = f"product #{product_id}" if product_id is not None else "—"
    text = (msg.get("message") if isinstance(msg, dict) else None) or f"Новых сообщений: {chat.get('cnt_new') or '—'}"
    dt = (msg.get("date_written") if isinstance(msg, dict) else None) or (chat.get("last_message") or "—")
    return (
        f"💬 Новое сообщение от <b>{email}</b>\n"
        f"🗂️ Диалог ID #{conversation_id} — <i>{product_label}</i>\n"
        f"🕒 {dt}\n"
        f"💭 <code>{text}</code>"
    )

def format_order_alert(order):
    title = order.get("offer_title", "—")
    email = order.get("buyer_email", "—")
    amount = order.get("amount", 0)
    status = order.get("status", "—")
    created_at = order.get("created_at", "—")
    number = order.get("number") or order.get("id", "—")
    return (
        f"🧾 Новый заказ №<b>{number}</b>\n"
        f"📦 Товар: <i>{title}</i>\n"
        f"📧 Покупатель: <code>{email}</code>\n"
        f"💰 Сумма: <b>{amount}</b>\n"
        f"📌 Статус: <b>{status}</b>\n"
        f"🕒 {created_at}"
    )

# === Телеграм команды и логика ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Включаем постоянное нижнее меню
    keyboard = [[KeyboardButton(BUTTON_TEXT), KeyboardButton(BUTTON_TEXT_ORDERS)], [KeyboardButton(BUTTON_TEXT_DEBUG)]]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(
        "Привет! 👋\nКнопка внизу: проверяй новые сообщения на GGSel когда удобно.",
        reply_markup=reply_markup
    )

    # Инициализируем хранилище просмотренных сообщений для авто-оповещений
    chat_id = update.effective_chat.id
    seen_map = context.application.bot_data.setdefault("seen_keys", {})
    seen_map.setdefault(chat_id, set())
    orders_seen_map = context.application.bot_data.setdefault("seen_orders", {})
    orders_seen_map.setdefault(chat_id, set())

    # Настраиваем авто-проверку каждую минуту
    job_queue = getattr(context, "job_queue", None)
    if job_queue is not None:
        job_name = f"auto_check_{chat_id}"
        for job in job_queue.get_jobs_by_name(job_name):
            job.schedule_removal()
        job_queue.run_repeating(
            auto_check,
            interval=60,
            first=5,
            chat_id=chat_id,
            name=job_name,
        )
        # сразу делаем первую проверку, не дожидаясь 5 секунд
        try:
            await _auto_check_once(context.application, chat_id)
        except Exception:
            pass
        # Планируем проверку заказов каждые x минут
        orders_job_name = f"auto_orders_{chat_id}"
        for job in job_queue.get_jobs_by_name(orders_job_name):
            job.schedule_removal()
        job_queue.run_repeating(
            auto_orders_check,
            interval=60,
            first=10,
            chat_id=chat_id,
            name=orders_job_name,
        )
    else:
        # Фолбэк без JobQueue: запускаем фоновую задачу
        task_map = context.application.bot_data.setdefault("bg_tasks", {})
        # первый прогон сразу
        try:
            await _auto_check_once(context.application, chat_id)
        except Exception:
            pass
        t1 = task_map.get((chat_id, "msgs"))
        if t1 is None or t1.done():
            t1 = asyncio.create_task(_auto_check_loop(context.application, chat_id, 60))
            task_map[(chat_id, "msgs")] = t1
        t2 = task_map.get((chat_id, "orders"))
        if t2 is None or t2.done():
            t2 = asyncio.create_task(_auto_orders_loop(context.application, chat_id, 300))
            task_map[(chat_id, "orders")] = t2

async def manual_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Проверяю новые сообщения...")
    try:
        unread = get_unread()
        messages = []
        for chat in unread:
            alert = format_alert(chat)
            if alert:
                messages.append(alert)
        if messages:
            await update.message.reply_text("\n\n".join(messages), parse_mode="HTML")
        else:
            await update.message.reply_text("✅ Новых сообщений нет.")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Ошибка: {e}")

async def manual_check_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Проверяю новые заказы...")
    chat_id = update.effective_chat.id
    try:
        orders = get_recent_orders()
        if not orders:
            await update.message.reply_text("✅ Новых заказов нет.")
            return
        orders_seen_map = context.application.bot_data.setdefault("seen_orders", {})
        seen_set = orders_seen_map.setdefault(chat_id, set())

        alerts = []
        for order in orders:
            oid = order.get("id") or order.get("number")
            if oid in seen_set:
                continue
            seen_set.add(oid)
            alerts.append(format_order_alert(order))

        if alerts:
            await update.message.reply_text("\n\n".join(alerts), parse_mode="HTML")
        else:
            await update.message.reply_text("✅ Новых заказов нет.")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Ошибка: {e}")

async def _auto_check_once(app: Application, chat_id: int):
    try:
        unread = get_unread()
        alerts = []
        seen_map = app.bot_data.setdefault("seen_keys", {})
        seen_set = seen_map.setdefault(chat_id, set())

        for chat in unread:
            alert = format_alert(chat)
            if not alert:
                continue
            chat_item = chat.get("chat", {})
            msg = chat.get("message", {})
            conversation_id = chat_item.get("id_i")
            msg_id = (
                (msg.get("id") if isinstance(msg, dict) else None)
                or (msg.get("date_written") if isinstance(msg, dict) else None)
                or chat_item.get("last_message")
                or chat_item.get("cnt_new")
            )
            key = f"{conversation_id}:{msg_id}"
            if key in seen_set:
                continue
            seen_set.add(key)
            alerts.append(alert)

        if alerts:
            await app.bot.send_message(chat_id=chat_id, text="\n\n".join(alerts), parse_mode="HTML")
    except Exception as e:
        print(f"Auto-check error for {chat_id}: {e}")

async def auto_check(context: ContextTypes.DEFAULT_TYPE):
    # Callback для JobQueue
    chat_id = context.job.chat_id
    await _auto_check_once(context.application, chat_id)

async def _auto_check_loop(app: Application, chat_id: int, interval_seconds: int):
    # Фолбэк-цикл, если JobQueue недоступен
    await asyncio.sleep(5)
    while True:
        await _auto_check_once(app, chat_id)
        await asyncio.sleep(interval_seconds)

async def _auto_orders_once(app: Application, chat_id: int):
    try:
        orders = get_recent_orders()
        if not orders:
            return
        orders_seen_map = app.bot_data.setdefault("seen_orders", {})
        seen_set = orders_seen_map.setdefault(chat_id, set())
        alerts = []
        for order in orders:
            oid = order.get("id") or order.get("number")
            if oid in seen_set:
                continue
            seen_set.add(oid)
            alerts.append(format_order_alert(order))
        if alerts:
            await app.bot.send_message(chat_id=chat_id, text="\n\n".join(alerts), parse_mode="HTML")
    except Exception as e:
        print(f"Auto-orders error for {chat_id}: {e}")

async def auto_orders_check(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    await _auto_orders_once(context.application, chat_id)

async def _auto_orders_loop(app: Application, chat_id: int, interval_seconds: int):
    await asyncio.sleep(5)
    while True:
        await _auto_orders_once(app, chat_id)
        await asyncio.sleep(interval_seconds)

async def debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Мини-диагностика: apilogin + базовые GET и общий вердикт."""
    env_seller = SELLER_ID or "—"

    def probe(url: str, params: dict, ru: bool = False) -> int:
        try:
            params = dict(params)
            _ensure_api_token()
            if API_TOKEN and "token" not in params:
                params["token"] = API_TOKEN
            headers = _auth_headers(locale_ru=ru)
            r = requests.get(url, params=params, headers=headers, timeout=60)
            return r.status_code
        except Exception:
            return 0

    # apilogin напрямую
    def probe_apilogin() -> int:
        try:
            ts = str(int(time.time()))
            sign = _sha256_hex(f"{API_KEY}{ts}") if API_KEY else ""
            r = requests.post(
                API_LOGIN_URL,
                json={"seller_id": int(SELLER_ID) if SELLER_ID else 0, "timestamp": ts, "sign": sign},
                headers=_auth_headers(locale_ru=True, with_bearer=False),
                timeout=60,
            )
            return r.status_code
        except Exception:
            return 0

    login_status = probe_apilogin()
    chats_status = probe(DEBATES_CHATS_URL, {"filter_new": 1, "page": 1, "pagesize": 1})
    sales_status = probe(LAST_SALES_URL, {"seller_id": env_seller, "top": 1}, ru=True) if env_seller != "—" else 0

    ok = login_status == 200 and chats_status == 200 and sales_status == 200
    verdict = "✅ API настроен верно" if ok else "❌ API настроен неверно"
    lines = [
        f"SELLER_ID: {env_seller}",
        f"apilogin: {login_status}",
        f"chats: {chats_status}",
        f"last_sales: {sales_status}",
        verdict,
    ]

    await update.message.reply_text("\n".join(lines))

# === Запуск бота ===
def main():
    print("🚀 Бот запускается...")
    if not BOT_TOKEN or ":" not in BOT_TOKEN or len(BOT_TOKEN) < 30:
        print("❌ Не найден корректный токен Telegram. Проверь .env:")
        print("   Требуется переменная TG_BOT_TOKEN=xxxxxxxxx:YYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYY")
        print("   Текущая рабочая папка:", os.getcwd())
        raise SystemExit(1)
    # Увеличим таймауты Telegram HTTP-клиента, чтобы избежать TimedOut при отправке
    request = HTTPXRequest(
        read_timeout=30.0,
        write_timeout=30.0,
        connect_timeout=15.0,
        pool_timeout=15.0,
    )
    app = Application.builder().token(BOT_TOKEN).request(request).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(fr"^{re.escape(BUTTON_TEXT)}$"), manual_check))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^🧾 Проверить заказы$"), manual_check_orders))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^🔍 Диагностика API$"), debug))
    app.add_handler(CommandHandler("debug", debug))
    print("✅ Бот запущен. Открой в Telegram и отправь /start")
    app.run_polling()

if __name__ == "__main__":
    main()
