import argparse
import html
import json
import logging
import os
import sys
import time

import requests

if not sys.stdout.isatty():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
STATE_PATH = os.path.join(BASE_DIR, "state.json")
LOG_PATH = os.path.join(BASE_DIR, "bridge.log")
CA_BUNDLE = os.path.join(BASE_DIR, "ca_bundle.pem")

MAX_API = "https://platform-api2.max.ru"
TG_API = "https://api.telegram.org"
TG_TEXT_LIMIT = 4096
TG_CAPTION_LIMIT = 1024
SEND_INTERVAL = 0.55

MAX_ENTITY_TO_HTML = {
    "strong": "b",
    "emphasized": "i",
    "monospaced": "code",
    "strikethrough": "s",
    "underline": "u",
}

log = logging.getLogger("bridge")
_last_tg_send = 0.0


def load_config():
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    env_keys = {"max_token": "MAX_TOKEN", "tg_token": "TG_TOKEN", "max_chat_id": "MAX_CHAT_ID", "tg_chat_id": "TG_CHAT_ID"}
    for key, env in env_keys.items():
        val = os.environ.get(env)
        if val:
            cfg[key] = int(val) if key.endswith("chat_id") else val
    missing = [k for k in ("max_token", "tg_token") if not cfg.get(k)]
    if missing:
        sys.exit(f"Не заданы {', '.join(missing)}: заполните config.json или переменные окружения MAX_TOKEN/TG_TOKEN")
    return cfg


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"marker": None}


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_PATH)


def max_session(token):
    s = requests.Session()
    s.headers["Authorization"] = token
    if os.path.exists(CA_BUNDLE):
        s.verify = CA_BUNDLE
    else:
        log.warning("ca_bundle.pem не найден — запустите setup_cert.py")
    return s


def tg_send(tg_token, method, timeout=60, **data):
    global _last_tg_send
    url = f"{TG_API}/bot{tg_token}/{method}"
    files = data.pop("files", None)
    wait = SEND_INTERVAL - (time.monotonic() - _last_tg_send)
    if wait > 0:
        time.sleep(wait)
    attempts = 0
    while True:
        _last_tg_send = time.monotonic()
        attempts += 1
        try:
            if files:
                resp = requests.post(url, data=data, files=files, timeout=timeout)
            else:
                resp = requests.post(url, json=data, timeout=timeout)
        except requests.RequestException as e:
            if attempts >= 10:
                raise RuntimeError(f"Telegram {method}: сеть недоступна: {e}")
            log.warning("Telegram %s: сетевая ошибка (%s), повтор через 5 с", method, e)
            time.sleep(5)
            continue
        if resp.status_code == 429:
            try:
                retry_after = int(resp.json()["parameters"]["retry_after"])
            except Exception:
                retry_after = 5
            retry_after = min(retry_after, 30)
            log.warning("Telegram 429, ждём %s с", retry_after)
            time.sleep(retry_after)
            continue
        if resp.status_code >= 500:
            if attempts >= 10:
                raise RuntimeError(f"Telegram {method}: HTTP {resp.status_code}")
            time.sleep(5)
            continue
        result = resp.json()
        if not result.get("ok"):
            raise RuntimeError(f"Telegram {method}: {result.get('description')} (код {result.get('error_code')})")
        return result["result"]


def sender_name(msg):
    sender = msg.get("sender") or {}
    name = f"{sender.get('first_name') or ''} {sender.get('last_name') or ''}".strip()
    if not name:
        name = sender.get("username") or "Участник"
    return name


def markup_to_html(text, entities):
    try:
        escaped = html.escape(text)
        if not entities:
            return escaped
        points = []
        for ent in entities:
            start = int(ent.get("offset", ent.get("from", 0)))
            length = int(ent.get("length", 0))
            if length <= 0:
                continue
            points.append((start, start + length, ent))
        points.sort(key=lambda p: (p[0], -p[1]))
        out = []
        pos = 0
        for start, end, ent in points:
            if start < pos:
                continue
            out.append(html.escape(text[pos:start]))
            etype = ent.get("type")
            payload = ent.get("payload") or {}
            if etype == "link" and payload.get("url"):
                url = html.escape(payload["url"], quote=True)
                out.append(f'<a href="{url}">')
                out.append(html.escape(text[start:end]))
                out.append("</a>")
            elif etype == "user_mention" and payload.get("user_id"):
                url = f"max://user/{payload['user_id']}"
                out.append(f'<a href="{url}">')
                out.append(html.escape(text[start:end]))
                out.append("</a>")
            elif etype in ("heading",):
                out.append("<b>")
                out.append(html.escape(text[start:end]))
                out.append("</b>")
            elif etype in MAX_ENTITY_TO_HTML:
                tag = MAX_ENTITY_TO_HTML[etype]
                out.append(f"<{tag}>")
                out.append(html.escape(text[start:end]))
                out.append(f"</{tag}>")
            else:
                out.append(html.escape(text[start:end]))
            pos = end
        out.append(html.escape(text[pos:]))
        return "".join(out)
    except Exception:
        return html.escape(text)


def truncate(text, limit):
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def download_max_file(session, url):
    try:
        resp = session.get(url, timeout=120)
    except requests.exceptions.SSLError:
        resp = requests.get(url, timeout=120, verify=False)
    resp.raise_for_status()
    return resp.content


def tg_send_file(tg_token, session, method, chat_id, att, caption):
    payload = att.get("payload") or {}
    url = payload.get("url")
    if not url:
        log.warning("Вложение %s без payload.url — пропускаю. Структура: %s", att.get("type"), json.dumps(att, ensure_ascii=False)[:500])
        return None
    data = download_max_file(session, url)
    filename = payload.get("filename") or att.get("filename")
    if not filename:
        if att.get("type") == "sticker":
            filename = "sticker.webp"
        else:
            ext = {"sendPhoto": ".jpg", "sendVideo": ".mp4", "sendAudio": ".mp3", "sendVoice": ".ogg", "sendDocument": ".bin"}
            filename = "file" + ext.get(method, ".bin")
    fields = {"chat_id": chat_id}
    if caption:
        fields["caption"] = caption
    field_name = {"sendPhoto": "photo", "sendVideo": "video", "sendAudio": "audio", "sendVoice": "voice", "sendDocument": "document"}.get(method, "document")
    result = tg_send(tg_token, method, files={field_name: (filename, data)}, **fields)
    return result.get("message_id")


def forward_message(session, cfg, msg):
    tg_chat_id = cfg["tg_chat_id"]
    body = msg.get("body") or {}
    text = body.get("text") or ""
    entities = body.get("markup") or []
    attachments = body.get("attachments") or []
    link_msg = msg.get("link")
    prefix = f"👤 {html.escape(sender_name(msg))}:\n"
    sent_ids = []

    if text or link_msg:
        html_text = prefix + markup_to_html(text, entities) if text else prefix.rstrip("\n")
        if link_msg:
            link_body = link_msg.get("message") or {}
            link_text = link_body.get("text") or ""
            label = "↩️ Ответ на сообщение" if link_msg.get("type") == "reply" else "↪️ Пересланное сообщение"
            quote = f"{label}:\n{link_text}" if link_text else label
            html_text = f"{html_text}\n{html.escape(truncate(quote, 900))}"
        result = tg_send(cfg["tg_token"], "sendMessage", chat_id=tg_chat_id, text=truncate(html_text, TG_TEXT_LIMIT), parse_mode="HTML", disable_web_page_preview=False)
        sent_ids.append(result["message_id"])

    for att in attachments:
        att_type = att.get("type")
        payload = att.get("payload") or {}
        caption = truncate(prefix.rstrip("\n"), TG_CAPTION_LIMIT)
        try:
            if att_type == "image":
                mid = tg_send_file(cfg["tg_token"], session, "sendPhoto", tg_chat_id, att, caption)
            elif att_type == "video":
                mid = tg_send_file(cfg["tg_token"], session, "sendVideo", tg_chat_id, att, caption)
            elif att_type == "audio":
                mid = tg_send_file(cfg["tg_token"], session, "sendAudio", tg_chat_id, att, caption)
            elif att_type == "voice":
                mid = tg_send_file(cfg["tg_token"], session, "sendVoice", tg_chat_id, att, caption)
            elif att_type == "file":
                mid = tg_send_file(cfg["tg_token"], session, "sendDocument", tg_chat_id, att, caption)
            elif att_type == "sticker":
                mid = tg_send_file(cfg["tg_token"], session, "sendDocument", tg_chat_id, att, caption)
            elif att_type == "location":
                result = tg_send(cfg["tg_token"], "sendLocation", chat_id=tg_chat_id, latitude=payload.get("latitude"), longitude=payload.get("longitude"))
                mid = result.get("message_id")
            elif att_type == "share":
                parts = [prefix.rstrip("\n")]
                if att.get("title"):
                    parts.append(f"🔗 {att['title']}")
                if att.get("description"):
                    parts.append(att["description"])
                share_url = payload.get("url") or att.get("url")
                if share_url:
                    parts.append(share_url)
                result = tg_send(cfg["tg_token"], "sendMessage", chat_id=tg_chat_id, text=truncate("\n".join(html.escape(p) for p in parts), TG_TEXT_LIMIT), parse_mode="HTML")
                mid = result.get("message_id")
            elif att_type == "contact":
                contact_text = payload.get("vcf_info") or payload.get("max_info") or json.dumps(payload, ensure_ascii=False)
                result = tg_send(cfg["tg_token"], "sendMessage", chat_id=tg_chat_id, text=truncate(prefix + html.escape(str(contact_text)), TG_TEXT_LIMIT), parse_mode="HTML")
                mid = result.get("message_id")
            elif att_type == "inline_keyboard":
                buttons = payload.get("buttons") or []
                texts = []
                for row in buttons:
                    for btn in row or []:
                        label = btn.get("text") or btn.get("type") or "?"
                        burl = btn.get("url")
                        texts.append(f"[{label}]({burl})" if burl else f"🔘 {label}")
                if texts:
                    result = tg_send(cfg["tg_token"], "sendMessage", chat_id=tg_chat_id, text=truncate(prefix + html.escape("\n".join(texts)), TG_TEXT_LIMIT), parse_mode="HTML")
                    mid = result.get("message_id")
                else:
                    mid = None
            else:
                mid = tg_send_file(cfg["tg_token"], session, "sendDocument", tg_chat_id, att, caption)
            if mid:
                sent_ids.append(mid)
        except Exception as e:
            log.error("Не удалось переслать вложение %s: %s", att_type, e)

    if not sent_ids:
        result = tg_send(cfg["tg_token"], "sendMessage", chat_id=tg_chat_id, text=truncate(prefix + "[сообщение без содержимого]", TG_TEXT_LIMIT), parse_mode="HTML")
        sent_ids.append(result["message_id"])
    return sent_ids


def pin_message(cfg, message_id):
    try:
        tg_send(cfg["tg_token"], "pinChatMessage", chat_id=cfg["tg_chat_id"], message_id=message_id, disable_notification=False)
    except Exception as e:
        log.error("Не удалось закрепить сообщение %s: %s", message_id, e)


def handle_message(session, cfg, msg):
    chat_id = (msg.get("recipient") or {}).get("chat_id")
    if chat_id is None or int(chat_id) != int(cfg["max_chat_id"]):
        return
    sender = msg.get("sender") or {}
    log.info("Сообщение от %s → Telegram", sender_name(msg) if sender else "канала")
    sent_ids = forward_message(session, cfg, msg)
    pin_message(cfg, sent_ids[0])


def poll_loop(cfg, run_for=None):
    session = max_session(cfg["max_token"])
    deadline = time.monotonic() + run_for if run_for else None
    state = load_state()
    marker = state.get("marker")
    if marker is None:
        log.info("Первый запуск: инициализирую маркер (история не пересылается)")
        resp = session.get(f"{MAX_API}/updates", params={"timeout": 0, "limit": 1}, timeout=30)
        resp.raise_for_status()
        marker = resp.json().get("marker")
        state["marker"] = marker
        save_state(state)
    log.info("Polling запущен, маркер: %s", marker)
    delay = 0
    while deadline is None or time.monotonic() < deadline:
        try:
            params = {"timeout": 30, "limit": 100, "types": "message_created"}
            if marker is not None:
                params["marker"] = marker
            resp = session.get(f"{MAX_API}/updates", params=params, timeout=60)
            if resp.status_code in (401, 403):
                sys.exit(f"MAX API отклонил запрос ({resp.status_code}): {resp.text[:200]}. Проверьте токен и права администратора бота в группе.")
            resp.raise_for_status()
            data = resp.json()
            updates = data.get("updates") or []
            for upd in updates:
                msg = upd.get("message")
                if not msg:
                    continue
                try:
                    handle_message(session, cfg, msg)
                except Exception as e:
                    log.error("Ошибка обработки сообщения: %s", e)
            new_marker = data.get("marker")
            if new_marker is not None:
                marker = new_marker
                state["marker"] = marker
                save_state(state)
            delay = 0
        except KeyboardInterrupt:
            raise
        except Exception as e:
            delay = min(max(delay * 2, 5), 60)
            log.warning("Ошибка polling: %s — повтор через %s с", e, delay)
            time.sleep(delay)
    if deadline is not None:
        log.info("Отведённое время вышло, маркер сохранён")


def run_init(cfg):
    session = max_session(cfg["max_token"])

    if not cfg.get("max_chat_id"):
        print("\n=== MAX ===")
        print("Добавьте бота в группу MAX (и назначьте администратором) или напишите любое сообщение в группе, где бот уже есть.")
        print("Жду событие до 2 минут…")
        deadline = time.time() + 120
        found = None
        while time.time() < deadline and not found:
            resp = session.get(f"{MAX_API}/updates", params={"timeout": 30, "limit": 100}, timeout=60)
            resp.raise_for_status()
            for upd in resp.json().get("updates") or []:
                utype = upd.get("update_type")
                if utype == "bot_added" and upd.get("chat_id"):
                    found = upd["chat_id"]
                    break
                if utype == "message_created":
                    msg = upd.get("message") or {}
                    chat_id = (msg.get("recipient") or {}).get("chat_id")
                    if chat_id:
                        found = chat_id
                        break
        if found:
            cfg["max_chat_id"] = found
            print(f"max_chat_id найден: {found}")
        else:
            print("Не удалось найти chat_id MAX. Запустите --init ещё раз и добавьте бота в группу во время ожидания.")
    else:
        print(f"max_chat_id уже задан: {cfg['max_chat_id']}")

    if not cfg.get("tg_chat_id"):
        print("\n=== Telegram ===")
        print("Напишите любое сообщение в TG-группу, куда добавлен бот (бот — администратор с правом закрепления).")
        print("Жду до 2 минут…")
        deadline = time.time() + 120
        found = None
        while time.time() < deadline and not found:
            try:
                result = tg_send(cfg["tg_token"], "getUpdates", timeout=35)
            except Exception as e:
                print(f"Ошибка Telegram: {e}")
                time.sleep(3)
                continue
            for upd in result:
                msg = upd.get("message") or upd.get("channel_post") or upd.get("edited_message") or {}
                chat = msg.get("chat") or {}
                if chat.get("id") and int(chat["id"]) < 0:
                    found = chat["id"]
                    print(f"Группа: «{chat.get('title')}»")
                    break
            time.sleep(1)
        if found:
            cfg["tg_chat_id"] = found
            print(f"tg_chat_id найден: {found}")
        else:
            print("Не удалось найти tg_chat_id. Запустите --init ещё раз и напишите сообщение в группу.")
    else:
        print(f"tg_chat_id уже задан: {cfg['tg_chat_id']}")

    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print(f"\nКонфигурация сохранена в {CONFIG_PATH}")
    if cfg.get("max_chat_id") and cfg.get("tg_chat_id"):
        print("Всё готово. Запустите: python bridge.py")


def check_tokens(cfg):
    session = max_session(cfg["max_token"])
    try:
        resp = session.get(f"{MAX_API}/me", timeout=15)
    except requests.exceptions.SSLError:
        sys.exit("SSL-ошибка при обращении к MAX. Запустите: python setup_cert.py")
    if resp.status_code != 200:
        sys.exit(f"Токен MAX недействителен (HTTP {resp.status_code}). Проверьте токен бота на платформе dev.max.ru.")
    bot = resp.json()
    log.info("MAX-бот: @%s", bot.get("username", "?"))
    result = tg_send(cfg["tg_token"], "getMe")
    log.info("Telegram-бот: @%s", result.get("username", "?"))


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_PATH, encoding="utf-8"),
        ],
    )


def main():
    parser = argparse.ArgumentParser(description="Мост MAX → Telegram с закреплением сообщений")
    parser.add_argument("--init", action="store_true", help="определить chat_id групп MAX и Telegram")
    parser.add_argument("--run-for", type=int, metavar="СЕК", help="отработать заданное число секунд и выйти (для планировщиков)")
    args = parser.parse_args()
    setup_logging()
    cfg = load_config()
    check_tokens(cfg)
    if args.init:
        run_init(cfg)
        return
    for key in ("max_chat_id", "tg_chat_id"):
        if not cfg.get(key):
            sys.exit(f"В config.json не указан {key}. Сначала запустите: python bridge.py --init")
    try:
        poll_loop(cfg, args.run_for)
    except KeyboardInterrupt:
        log.info("Остановлено пользователем, маркер сохранён")


if __name__ == "__main__":
    main()
