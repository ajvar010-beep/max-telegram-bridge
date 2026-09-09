import argparse
import base64
import html
import json
import logging
import os
import sys
import time
import zlib

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

FILE_ATTACHMENT_TYPES = {"image", "video", "audio", "voice", "file", "sticker"}

MINI_APP_URL = os.environ.get("MINI_APP_URL") or "https://ajvar010-beep.github.io/max-telegram-bridge/"
SECRETARY_APP_URL = os.environ.get("SECRETARY_APP_URL") or "https://ajvar010-beep.github.io/max-telegram-bridge/secretary.html"

TG_ATT_TO_MAX = {
    "photo": ("image", ".jpg", "image/jpeg"),
    "video": ("video", ".mp4", "video/mp4"),
    "animation": ("video", ".mp4", "video/mp4"),
    "audio": ("audio", ".mp3", "audio/mpeg"),
    "voice": ("audio", ".ogg", "audio/ogg"),
    "document": ("file", None, "application/octet-stream"),
    "sticker": ("image", ".webp", "image/webp"),
}
MEDIA_CAPTION_LIMIT = 400

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
            cfg[key] = val if key == "max_chat_id" else (int(val) if key.endswith("chat_id") else val)
    ids = [int(x) for x in str(cfg.get("max_chat_id") or "").replace(";", ",").split(",") if x.strip()]
    cfg["max_chat_ids"] = ids
    if ids:
        cfg["max_chat_id"] = ids[0]
    missing = [k for k in ("max_token", "tg_token") if not cfg.get(k)]
    if missing:
        sys.exit(f"Не заданы {', '.join(missing)}: заполните config.json или переменные окружения MAX_TOKEN/TG_TOKEN")
    return cfg


def load_state():
    state = {"marker": None, "rules": {}, "tg_offset": None, "recent": [], "feed": [], "participants": {}, "tg_welcomed": []}
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                state.update(json.load(f))
        except (json.JSONDecodeError, OSError):
            pass
    return state


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


def max_send(session, chat_id, text=None, attachments=None, link=None):
    payload = {}
    if text:
        payload["text"] = text[:4000]
    if attachments:
        payload["attachments"] = attachments
    if link:
        payload["link"] = link
    resp = session.post(f"{MAX_API}/messages", params={"chat_id": chat_id}, json=payload, timeout=60)
    resp.raise_for_status()
    try:
        return resp.json()
    except Exception:
        return {}


def encode_snapshot(snapshot):
    raw = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(zlib.compress(raw, 9)).decode("ascii")


def tg_user_name(user):
    name = f"{user.get('first_name') or ''} {user.get('last_name') or ''}".strip()
    return name or user.get("username") or "Пользователь"


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


def tg_reply(tg_token, chat_id, text):
    try:
        tg_send(tg_token, "sendMessage", chat_id=chat_id, text=truncate(text, TG_TEXT_LIMIT))
    except Exception as e:
        log.error("Не удалось ответить в Telegram: %s", e)


def tg_admin_ids(cfg):
    return {int(i) for i in str(cfg.get("tg_admin_ids") or "1924570470").replace(",", " ").split()}


def max_admin_ids(cfg):
    return {int(i) for i in str(cfg.get("max_admin_ids") or "115694993").replace(",", " ").split()}


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


def tg_send_file(tg_token, session, method, chat_id, att, caption, parse_mode=None):
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
        if parse_mode:
            fields["parse_mode"] = parse_mode
    field_name = {"sendPhoto": "photo", "sendVideo": "video", "sendAudio": "audio", "sendVoice": "voice", "sendDocument": "document"}.get(method, "document")
    result = tg_send(tg_token, method, files={field_name: (filename, data)}, **fields)
    return result.get("message_id")


def send_media_attachment(tg_token, session, tg_chat_id, att, caption, parse_mode=None):
    att_type = att.get("type")
    payload = att.get("payload") or {}
    if att_type == "image":
        return tg_send_file(tg_token, session, "sendPhoto", tg_chat_id, att, caption, parse_mode)
    if att_type == "video":
        return tg_send_file(tg_token, session, "sendVideo", tg_chat_id, att, caption, parse_mode)
    if att_type == "audio":
        return tg_send_file(tg_token, session, "sendAudio", tg_chat_id, att, caption, parse_mode)
    if att_type == "voice":
        return tg_send_file(tg_token, session, "sendVoice", tg_chat_id, att, caption, parse_mode)
    if att_type in ("file", "sticker"):
        return tg_send_file(tg_token, session, "sendDocument", tg_chat_id, att, caption, parse_mode)
    if att_type == "location":
        result = tg_send(tg_token, "sendLocation", chat_id=tg_chat_id, latitude=payload.get("latitude"), longitude=payload.get("longitude"))
        return result.get("message_id")
    if att_type == "share":
        parts = []
        if att.get("title"):
            parts.append(f"🔗 {att['title']}")
        if att.get("description"):
            parts.append(att["description"])
        share_url = payload.get("url") or att.get("url")
        if share_url:
            parts.append(share_url)
        if caption:
            parts.insert(0, caption)
        if not parts:
            return None
        result = tg_send(tg_token, "sendMessage", chat_id=tg_chat_id, text=truncate("\n".join(html.escape(p) for p in parts), TG_TEXT_LIMIT), parse_mode="HTML")
        return result.get("message_id")
    if att_type == "contact":
        contact_text = payload.get("vcf_info") or payload.get("max_info") or json.dumps(payload, ensure_ascii=False)
        text = f"{caption}\n{html.escape(str(contact_text))}" if caption else html.escape(str(contact_text))
        result = tg_send(tg_token, "sendMessage", chat_id=tg_chat_id, text=truncate(text, TG_TEXT_LIMIT), parse_mode="HTML")
        return result.get("message_id")
    if att_type == "inline_keyboard":
        buttons = payload.get("buttons") or []
        texts = []
        for row in buttons:
            for btn in row or []:
                label = btn.get("text") or btn.get("type") or "?"
                burl = btn.get("url")
                texts.append(f"[{label}]({burl})" if burl else f"🔘 {label}")
        if not texts:
            return None
        text = f"{caption}\n{html.escape(chr(10).join(texts))}" if caption else html.escape("\n".join(texts))
        result = tg_send(tg_token, "sendMessage", chat_id=tg_chat_id, text=truncate(text, TG_TEXT_LIMIT), parse_mode="HTML")
        return result.get("message_id")
    return tg_send_file(tg_token, session, "sendDocument", tg_chat_id, att, caption, parse_mode)


def forward_message(session, cfg, msg):
    tg_chat_id = cfg["tg_chat_id"]
    body = msg.get("body") or {}
    text = body.get("text") or ""
    entities = body.get("markup") or []
    attachments = body.get("attachments") or []
    link_msg = msg.get("link")
    link_body = (link_msg.get("message") or {}) if link_msg else {}
    link_type = (link_msg or {}).get("type")
    quote = None
    if link_type == "forward":
        link_text = link_body.get("text") or ""
        link_atts = link_body.get("attachments") or []
        link_sender = sender_name(link_body) if link_body.get("sender") else None
        label = f"↪️ Переслано от {link_sender}" if link_sender else "↪️ Пересланное сообщение"
        if link_atts:
            quote = f"{label}:\n{link_text}".rstrip(": \n") if link_text else label
            attachments = attachments + link_atts
        elif link_text:
            quote = f"{label}:\n{link_text}"
        else:
            quote = label
    elif link_type == "reply":
        link_text = link_body.get("text") or ""
        quote = f"↩️ Ответ на сообщение:\n{link_text}" if link_text else "↩️ Ответ на сообщение"

    sender_prefix = f"👤 {html.escape(sender_name(msg))}:\n"
    html_body = markup_to_html(text, entities) if text else ""
    head = sender_prefix + html_body
    if quote:
        head = f"{head}\n{html.escape(truncate(quote, 900))}"

    sent_ids = []
    file_atts = [a for a in attachments if a.get("type") in FILE_ATTACHMENT_TYPES]
    other_atts = [a for a in attachments if a.get("type") not in FILE_ATTACHMENT_TYPES]

    if file_atts:
        caption = head
        parse_mode = "HTML"
        if len(caption) > TG_CAPTION_LIMIT:
            result = tg_send(cfg["tg_token"], "sendMessage", chat_id=tg_chat_id, text=truncate(caption, TG_TEXT_LIMIT), parse_mode="HTML")
            sent_ids.append(result["message_id"])
            caption = sender_prefix.rstrip("\n")
            parse_mode = None
        first = True
        for att in file_atts:
            try:
                mid = send_media_attachment(cfg["tg_token"], session, tg_chat_id, att, caption if first else None, parse_mode if first else None)
                if mid:
                    sent_ids.append(mid)
                    first = False
            except Exception as e:
                log.error("Не удалось переслать вложение %s: %s", att.get("type"), e)
        for att in other_atts:
            try:
                mid = send_media_attachment(cfg["tg_token"], session, tg_chat_id, att, None)
                if mid:
                    sent_ids.append(mid)
            except Exception as e:
                log.error("Не удалось переслать вложение %s: %s", att.get("type"), e)
    else:
        if head.strip():
            result = tg_send(cfg["tg_token"], "sendMessage", chat_id=tg_chat_id, text=truncate(head, TG_TEXT_LIMIT), parse_mode="HTML", disable_web_page_preview=False)
            sent_ids.append(result["message_id"])
        for att in other_atts:
            try:
                mid = send_media_attachment(cfg["tg_token"], session, tg_chat_id, att, None)
                if mid:
                    sent_ids.append(mid)
            except Exception as e:
                log.error("Не удалось переслать вложение %s: %s", att.get("type"), e)

    if not sent_ids:
        result = tg_send(cfg["tg_token"], "sendMessage", chat_id=tg_chat_id, text=truncate(sender_prefix + "[сообщение без содержимого]", TG_TEXT_LIMIT), parse_mode="HTML")
        sent_ids.append(result["message_id"])
    return sent_ids


def pin_message(cfg, message_id):
    try:
        tg_send(cfg["tg_token"], "pinChatMessage", chat_id=cfg["tg_chat_id"], message_id=message_id, disable_notification=False)
    except Exception as e:
        log.error("Не удалось закрепить сообщение %s: %s", message_id, e)


def handle_message(session, cfg, state, msg):
    chat_id = (msg.get("recipient") or {}).get("chat_id")
    if chat_id is None:
        return
    sender = msg.get("sender") or {}
    user_id = sender.get("user_id")
    body = msg.get("body") or {}
    text = (body.get("text") or "").strip()
    if user_id is not None and int(user_id) in max_admin_ids(cfg) and text.startswith("/"):
        answer = handle_command(cfg, state, text, chat_id=int(chat_id))
        try:
            max_send(session, chat_id, answer)
        except Exception as e:
            log.error("Не удалось ответить в MAX: %s", e)
        return

    if int(chat_id) not in cfg["max_chat_ids"]:
        return
    if not sender:
        log.info("Сообщение от канала → Telegram")
        sent_ids = forward_message(session, cfg, msg)
        pin_message(cfg, sent_ids[0])
        return

    if user_id is not None:
        rule = state.get("rules", {}).get(str(user_id))
        if rule == "block":
            log.info("Сообщение от %s пропущено по правилу block", sender_name(msg))
            return
        recent = state.setdefault("recent", [])
        recent.insert(0, {"id": int(user_id), "name": sender_name(msg)})
        state["recent"] = recent[:20]

    mid = msg.get("mid") or msg.get("message_id") or msg.get("id")
    atts = [a.get("type") for a in (body.get("attachments") or []) if isinstance(a, dict) and a.get("type")]
    if mid is not None and (text or atts):
        feed = state.setdefault("feed", [])
        feed.insert(0, {
            "id": int(mid),
            "chat_id": int(chat_id),
            "name": sender_name(msg) if sender else "Канал",
            "text": text[:300],
            "atts": atts[:5],
        })
        state["feed"] = feed[:15]

    log.info("Сообщение от %s → Telegram", sender_name(msg))
    sent_ids = forward_message(session, cfg, msg)
    if user_id is not None and state.get("rules", {}).get(str(user_id)) == "nopin":
        log.info("Без закрепа по правилу nopin")
        return
    pin_message(cfg, sent_ids[0])


HELP_TEXT = (
    "Команды:\n"
    "/list — последние отправители\n"
    "/rules — текущие правила\n"
    "/block <id> — не пересылать сообщения\n"
    "/unblock <id> — снять запрет\n"
    "/nopin <id> — пересылать без закрепа\n"
    "/pin <id> — пересылать с закрепом\n"
    "/unpin — снять последний закреп\n"
    "/unpinall — снять все закрепы\n"
    "/chats — обслуживаемые чаты MAX\n"
    "/chat add <id> — добавить чат MAX\n"
    "/chat remove <id> — убрать чат MAX\n"
    "/app — панель управления (кнопка под полем ввода)\n"
    "/app hide — убрать кнопку панели\n"
    "/help — эта справка"
)


def handle_command(cfg, state, text, chat_id=None):
    parts = text.split()
    cmd = parts[0].lower()
    rules = state.setdefault("rules", {})
    recent = state.get("recent", [])
    id_to_name = {str(r["id"]): r.get("name", "?") for r in recent}

    if cmd == "/chat":
        if chat_id is None:
            return "Управление чатами MAX доступно только из группы MAX."
        sub = parts[1].lower() if len(parts) > 1 else ""
        ids = cfg["max_chat_ids"]
        if sub == "add":
            ids = list(dict.fromkeys(ids + [chat_id]))
        elif sub == "remove":
            if len(parts) > 2 and parts[2].lstrip("-").isdigit():
                target = int(parts[2])
            else:
                target = chat_id
            if target == cfg.get("max_chat_id") and len(ids) == 1:
                return "Нельзя убрать единственный чат."
            ids = [i for i in ids if i != target]
            if cfg.get("max_chat_id") not in ids and ids:
                cfg["max_chat_id"] = ids[0]
        else:
            return "Формат: /chat add или /chat remove <id>. Текущие: /chats"
        cfg["max_chat_ids"] = ids
        state["max_chat_ids"] = ids
        save_state(state)
        return f"Обслуживаемые чаты MAX: {', '.join(str(i) for i in ids) or 'нет'}"

    if cmd == "/chats":
        ids = cfg["max_chat_ids"]
        return f"Обслуживаемые чаты MAX: {', '.join(str(i) for i in ids) or 'нет'}"

    if cmd == "/list":
        if not recent:
            return "Пока нет сохранённых отправителей."
        lines = []
        for r in recent:
            rule = rules.get(str(r["id"]))
            mark = {"block": " 🚫", "nopin": " 📌✖"}.get(rule, "")
            lines.append(f"{r['id']} — {r.get('name', '?')}{mark}")
        return "Отправители:\n" + "\n".join(lines)

    if cmd == "/rules":
        if not rules:
            return "Правил нет."
        lines = []
        for uid, rule in rules.items():
            label = {"block": "не пересылать", "nopin": "без закрепа"}.get(rule, rule)
            lines.append(f"{uid} ({id_to_name.get(uid, '?')}) — {label}")
        return "Правила:\n" + "\n".join(lines)

    if cmd in ("/block", "/unblock", "/nopin", "/pin"):
        if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
            return "Формат: " + cmd + " <id отправителя>. Список: /list"
        uid = parts[1]
        name = id_to_name.get(uid, uid)
        if cmd == "/block":
            rules[uid] = "block"
            state["recent"] = recent
            save_state(state)
            return f"Сообщения от {name} больше не пересылаются."
        if cmd == "/nopin":
            rules[uid] = "nopin"
            save_state(state)
            return f"Сообщения от {name} пересылаются без закрепа."
        rules.pop(uid, None)
        save_state(state)
        if cmd == "/unblock":
            return f"Сообщения от {name} снова пересылаются."
        return f"Сообщения от {name} снова пересылаются с закрепом."

    if cmd == "/unpin":
        try:
            tg_send(cfg["tg_token"], "unpinChatMessage", chat_id=cfg["tg_chat_id"])
            return "Последний закреп снят."
        except Exception as e:
            return f"Не удалось снять закреп: {e}"

    if cmd == "/unpinall":
        try:
            tg_send(cfg["tg_token"], "unpinAllChatMessages", chat_id=cfg["tg_chat_id"])
            return "Все закрепы сняты."
        except Exception as e:
            return f"Не удалось снять закрепы: {e}"

    if cmd == "/app":
        return (
            "Панель управления доступна в Telegram: напишите /app в TG-группе "
            "или боту в личные сообщения — под полем ввода появится кнопка «🎛 Панель управления»."
        )

    if cmd == "/help":
        return HELP_TEXT

    return "Неизвестная команда. " + HELP_TEXT


def send_app_keyboard(cfg, state, chat_id, hide=False):
    if hide:
        markup = {"remove_keyboard": True}
        try:
            tg_send(cfg["tg_token"], "sendMessage", chat_id=chat_id,
                    text="Кнопка панели скрыта. Чтобы вернуть — снова /app.",
                    reply_markup=json.dumps(markup))
        except Exception as e:
            log.warning("Не удалось скрыть кнопку панели: %s", e)
        return
    participants = []
    for uid, info in state.get("participants", {}).items():
        participants.append({"id": int(uid), "name": info.get("name", "?"), "avatar": info.get("avatar") or ""})
    for r in state.get("recent", []):
        if not any(p["id"] == r.get("id") for p in participants):
            participants.append({"id": r.get("id"), "name": r.get("name", "?"), "avatar": ""})
    snapshot = {
        "participants": participants,
        "rules": state.get("rules", {}),
        "max_chat_ids": cfg["max_chat_ids"],
    }
    url = MINI_APP_URL + "?d=" + encode_snapshot(snapshot) + "#" + str(int(time.time()))
    if len(url) > 3900:
        snapshot["participants"] = participants[:120]
        url = MINI_APP_URL + "?d=" + encode_snapshot(snapshot) + "#" + str(int(time.time()))
    markup = {
        "keyboard": [[{"text": "🎛 Панель управления", "web_app": {"url": url}}]],
        "resize_keyboard": True,
    }
    try:
        tg_send(cfg["tg_token"], "sendMessage", chat_id=chat_id,
                text=f"Панель управления мостом открыта — нажмите кнопку «🎛 Панель управления» под полем ввода. Участников в списке: {len(snapshot['participants'])}.",
                reply_markup=json.dumps(markup))
    except Exception as e:
        log.warning("Не удалось отправить клавиатуру панели: %s", e)


def apply_web_app_data(cfg, state, raw):
    try:
        data = json.loads(raw)
    except Exception:
        return "Не удалось разобрать данные панели."
    if not isinstance(data, dict):
        return "Не удалось разобрать данные панели."

    unpin = data.get("unpin")
    if unpin == "all":
        try:
            tg_send(cfg["tg_token"], "unpinAllChatMessages", chat_id=cfg["tg_chat_id"])
            return "Все закрепы сняты."
        except Exception as e:
            return f"Не удалось снять закрепы: {e}"
    if unpin == "last":
        try:
            tg_send(cfg["tg_token"], "unpinChatMessage", chat_id=cfg["tg_chat_id"])
            return "Последний закреп снят."
        except Exception as e:
            return f"Не удалось снять закреп: {e}"

    changed = []
    rules = data.get("rules")
    if isinstance(rules, dict):
        clean = {}
        for uid, rule in rules.items():
            if not str(uid).lstrip("-").isdigit():
                continue
            if rule in ("block", "nopin"):
                clean[str(uid)] = rule
        state["rules"] = clean
        changed.append(f"правила: {len(clean)}")
    ids = data.get("max_chat_ids")
    if isinstance(ids, list):
        parsed = []
        for x in ids:
            try:
                parsed.append(int(str(x).strip()))
            except Exception:
                continue
        if parsed:
            parsed = list(dict.fromkeys(parsed))
            cfg["max_chat_ids"] = parsed
            if cfg.get("max_chat_id") not in parsed:
                cfg["max_chat_id"] = parsed[0]
            state["max_chat_ids"] = parsed
            changed.append(f"чаты MAX: {len(parsed)}")
        elif cfg["max_chat_ids"]:
            changed.append("список чатов MAX не изменён (пустой не принят)")
    if not changed:
        return "Панель не прислала изменений."
    save_state(state)
    return "Настройки из панели сохранены (" + ", ".join(changed) + ")."


def fetch_participants(session, cfg, state):
    participants = state.setdefault("participants", {})
    for chat_id in cfg["max_chat_ids"]:
        marker = None
        for _ in range(20):
            params = {"count": 100}
            if marker is not None:
                params["marker"] = marker
            try:
                resp = session.get(f"{MAX_API}/chats/{chat_id}/members", params=params, timeout=30)
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                log.warning("Не удалось получить участников чата %s: %s", chat_id, e)
                break
            members = data.get("members") or []
            for m in members:
                if not isinstance(m, dict):
                    continue
                if m.get("is_bot"):
                    continue
                user = m.get("user") if isinstance(m.get("user"), dict) else m
                uid = user.get("user_id") or user.get("id")
                if uid is None:
                    continue
                name = f"{user.get('first_name') or ''} {user.get('last_name') or ''}".strip()
                name = name or user.get("username") or user.get("name") or "Участник"
                info = participants.setdefault(str(uid), {})
                info["name"] = name
                avatar = user.get("avatar_url") or m.get("avatar_url")
                if avatar and not info.get("avatar"):
                    info["avatar"] = avatar
                info["chat_id"] = int(chat_id)
            marker = data.get("marker")
            if not marker or not members:
                break
    if len(participants) > 400:
        state["participants"] = dict(list(participants.items())[:400])
    return state["participants"]


def send_secretary_keyboard(cfg, state, tg_chat_id, user_name):
    snapshot = {
        "feed": state.get("feed", []),
        "me": user_name,
    }
    url = SECRETARY_APP_URL + "?d=" + encode_snapshot(snapshot) + "#" + str(int(time.time()))
    markup = {
        "keyboard": [[{"text": "📨 Секретарь", "web_app": {"url": url}}]],
        "resize_keyboard": True,
    }
    try:
        tg_send(cfg["tg_token"], "sendMessage", chat_id=tg_chat_id,
                text=(f"👋 Привет, {user_name}! Я — секретарь группы MAX.\n\n"
                      "Просто напишите мне текст или отправьте фото/видео/файл — "
                      "я передам это в группу MAX от вашего имени.\n"
                      "Кнопка «📨 Секретарь» открывает мини-приложение: там лента последних "
                      "сообщений группы и быстрые реакции.\n\n"
                      "/secretary — снова показать кнопку"),
                reply_markup=json.dumps(markup))
    except Exception as e:
        log.warning("Не удалось отправить клавиатуру секретаря: %s", e)


def relay_tg_to_max(session, cfg, state, msg):
    user = msg.get("from") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    name = tg_user_name(user)
    text = (msg.get("text") or msg.get("caption") or "").strip()
    if not cfg["max_chat_ids"]:
        tg_reply(cfg["tg_token"], chat_id, "Не настроены чаты MAX для пересылки.")
        return
    att = None
    for kind in ("photo", "video", "audio", "voice", "document", "sticker", "animation"):
        if msg.get(kind):
            att = kind
            break
    label = f"📱 {name} (Telegram)"
    if not att:
        if not text:
            tg_reply(cfg["tg_token"], chat_id, "Пришлите текст или файл — я передам его в MAX.")
            return
        head = f"{label}:\n{text}"
    else:
        items = msg[att]
        item = items[-1] if isinstance(items, list) else items
        file_id = item.get("file_id")
        try:
            info = tg_send(cfg["tg_token"], "getFile", file_id=file_id)
            resp = requests.get(f"https://api.telegram.org/file/bot{cfg['tg_token']}/{info['file_path']}", timeout=120)
            resp.raise_for_status()
            blob = resp.content
        except Exception as e:
            tg_reply(cfg["tg_token"], chat_id, f"Не удалось скачать файл из Telegram: {e}")
            return
        max_type, fallback_ext, mime = TG_ATT_TO_MAX.get(att, ("file", ".bin", "application/octet-stream"))
        fname = item.get("file_name") or os.path.basename(info.get("file_path") or "") or "file" + (fallback_ext or "")
        if len(blob) > 20 * 1024 * 1024:
            tg_reply(cfg["tg_token"], chat_id, "Файл больше 20 МБ — секретарь передаёт только файлы до 20 МБ.")
            return
        try:
            up = session.post(f"{MAX_API}/uploads", files={"data": (fname, blob, mime)}, timeout=300)
            up.raise_for_status()
            updata = up.json()
        except Exception as e:
            log.error("Ошибка загрузки в MAX: %s | %s", e, getattr(e.response, 'text', '')[:300] if hasattr(e, 'response') else '')
            tg_reply(cfg["tg_token"], chat_id, f"Не удалось загрузить файл в MAX: {e}")
            return
        token = None
        for key in ("token", "upload_token", "media_token", "file_token"):
            if updata.get(key):
                token = updata[key]
                break
        if not token:
            log.error("MAX /uploads без токена: %s", json.dumps(updata, ensure_ascii=False)[:500])
            tg_reply(cfg["tg_token"], chat_id, "MAX не выдал токен загрузки — попробуйте ещё раз.")
            return
        atts = [{"type": max_type, "payload": {"token": token}}]
        if text:
            head = f"{label}:\n{text[:MEDIA_CAPTION_LIMIT]}"
        else:
            head = label
    errors = []
    sent = 0
    for mcid in cfg["max_chat_ids"]:
        try:
            if att:
                max_send(session, mcid, head, attachments=atts)
            else:
                max_send(session, mcid, head)
            sent += 1
        except Exception as e:
            log.error("Секретарь: не удалось отправить в MAX-чат %s: %s", mcid, e)
            errors.append(f"{mcid}: {e}")
    if sent:
        tg_reply(cfg["tg_token"], chat_id, f"✅ Передано в MAX ({name}): {truncate(head, 200)}")
    else:
        tg_reply(cfg["tg_token"], chat_id, "Не удалось передать в MAX:\n" + "\n".join(errors[:3]))


def apply_secretary_data(session, cfg, state, raw, tg_chat_id, user_name):
    try:
        data = json.loads(raw)
    except Exception:
        return "Не удалось разобрать данные секретаря."
    if not isinstance(data, dict):
        return "Не удалось разобрать данные секретаря."
    if not cfg["max_chat_ids"]:
        return "Не настроены чаты MAX для пересылки."
    label = f"📱 {user_name} (Telegram)"
    react = data.get("react")
    if isinstance(react, dict):
        emoji = str(react.get("emoji") or "").strip()
        target = None
        for item in state.get("feed", []):
            if str(item.get("id")) == str(react.get("id")):
                target = item
                break
        if not emoji or not target:
            return "Реакция не найдена — обновите ленту и попробуйте снова."
        body_text = f"{label} отреагировал(а): {emoji}"
        quote_text = target.get("text")
        if quote_text:
            body_text += f"\n↩️ «{truncate(quote_text, 300)}» ({target.get('name', '?')})"
        link = {"message_id": int(target["id"]), "chat_id": int(target["chat_id"])}
        for mcid in cfg["max_chat_ids"]:
            try:
                max_send(session, mcid, body_text, link=link)
            except Exception as e:
                log.warning("Реакция в MAX-чат %s не ушла: %s", mcid, e)
        return f"{emoji} Реакция отправлена в MAX."
    text = str(data.get("send") or "").strip()
    if text:
        for mcid in cfg["max_chat_ids"]:
            try:
                max_send(session, mcid, f"{label}:\n{text}")
            except Exception as e:
                log.error("Секретарь: не удалось отправить в MAX-чат %s: %s", mcid, e)
                return f"Не удалось отправить в MAX: {e}"
        return f"✅ Отправлено в MAX от имени «{user_name}»."
    if data.get("refresh"):
        fetch_participants(session, cfg, state)
        send_secretary_keyboard(cfg, state, tg_chat_id, user_name)
        return "Лента обновлена — откройте «📨 Секретарь» снова."
    return "Секретарь не понял команду."


def poll_tg_admin(session, cfg, state):
    params = {"limit": 100, "allowed_updates": json.dumps(["message"])}
    offset = state.get("tg_offset")
    if offset:
        params["offset"] = offset
    try:
        updates = tg_send(cfg["tg_token"], "getUpdates", timeout=15, **params)
    except Exception as e:
        log.warning("Ошибка getUpdates: %s", e)
        return
    admins = tg_admin_ids(cfg)
    state_changed = False
    for upd in updates:
        state["tg_offset"] = upd["update_id"] + 1
        msg = upd.get("message") or {}
        chat = msg.get("chat") or {}
        user = msg.get("from") or {}
        text = (msg.get("text") or "").strip()
        is_admin = user.get("id") is not None and int(user["id"]) in admins
        if not is_admin:
            if chat.get("type") != "private" or user.get("is_bot"):
                continue
            user_name = tg_user_name(user)
            welcomed = state.setdefault("tg_welcomed", [])
            if user.get("id") not in welcomed:
                welcomed.append(user.get("id"))
                state["tg_welcomed"] = welcomed[-300:]
                state_changed = True
                send_secretary_keyboard(cfg, state, chat.get("id"), user_name)
                continue
            wad = msg.get("web_app_data")
            if isinstance(wad, dict) and wad.get("data"):
                log.info("Данные секретаря от %s", user_name)
                answer = apply_secretary_data(session, cfg, state, wad["data"], chat.get("id"), user_name)
                tg_reply(cfg["tg_token"], chat.get("id"), answer)
                continue
            if text.lower() in ("/secretary", "/app", "/start"):
                send_secretary_keyboard(cfg, state, chat.get("id"), user_name)
                continue
            if text.startswith("/"):
                tg_reply(cfg["tg_token"], chat.get("id"),
                         "Я — секретарь группы MAX: напишите мне обычный текст, фото, видео или файл — "
                         "передам в группу от вашего имени.\n"
                         "/secretary — кнопка мини-приложения с лентой и реакциями.")
                continue
            try:
                relay_tg_to_max(session, cfg, state, msg)
            except Exception as e:
                log.error("Ошибка секретаря: %s", e)
                tg_reply(cfg["tg_token"], chat.get("id"), f"Не удалось передать сообщение: {e}")
            continue
        wad = msg.get("web_app_data")
        if isinstance(wad, dict) and wad.get("data"):
            log.info("Данные панели получены")
            answer = apply_web_app_data(cfg, state, wad["data"])
            tg_reply(cfg["tg_token"], chat.get("id"), answer)
            continue
        if not text.startswith("/"):
            if chat.get("type") == "private":
                try:
                    relay_tg_to_max(session, cfg, state, msg)
                except Exception as e:
                    log.error("Ошибка секретаря (админ): %s", e)
            continue
        log.info("TG-команда %s", text)
        parts = text.split()
        cmd = parts[0].lower()
        if cmd == "/app":
            if chat.get("type") == "private":
                fetch_participants(session, cfg, state)
                send_app_keyboard(cfg, state, chat.get("id"), hide=len(parts) > 1 and parts[1].lower() == "hide")
            else:
                tg_reply(cfg["tg_token"], chat.get("id"),
                         "Кнопку мини-приложений Telegram показывает только в личных чатах — "
                         "напишите /app боту @zakreplatorinatorbot в личные сообщения.")
            continue
        if cmd == "/secretary":
            if chat.get("type") == "private":
                send_secretary_keyboard(cfg, state, chat.get("id"), tg_user_name(user))
            else:
                tg_reply(cfg["tg_token"], chat.get("id"),
                         "Напишите /secretary боту @zakreplatorinatorbot в личные сообщения.")
            continue
        answer = handle_command(cfg, state, text)
        tg_reply(cfg["tg_token"], chat.get("id"), answer)
    save_state(state)


def poll_loop(cfg, run_for=None):
    session = max_session(cfg["max_token"])
    deadline = time.monotonic() + run_for if run_for else None
    state = load_state()
    saved_chats = [int(c) for c in state.get("max_chat_ids") or []]
    if saved_chats:
        cfg["max_chat_ids"] = list(dict.fromkeys(saved_chats + cfg["max_chat_ids"]))
        cfg["max_chat_id"] = cfg["max_chat_ids"][0]
    marker = state.get("marker")
    if marker is None:
        log.info("Первый запуск: инициализирую маркер (история не пересылается)")
        resp = session.get(f"{MAX_API}/updates", params={"timeout": 0, "limit": 1}, timeout=30)
        resp.raise_for_status()
        marker = resp.json().get("marker")
        state["marker"] = marker
        save_state(state)
    log.info("Polling запущен, маркер: %s", marker)
    try:
        parts = fetch_participants(session, cfg, state)
        save_state(state)
        log.info("Участников MAX в ростере: %s", len(parts))
    except Exception as e:
        log.warning("Не удалось обновить ростер участников: %s", e)
    delay = 0
    while deadline is None or time.monotonic() < deadline:
        try:
            poll_tg_admin(session, cfg, state)
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
                    handle_message(session, cfg, state, msg)
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
