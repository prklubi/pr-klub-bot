import os
import json
import time
import random
from datetime import datetime
from io import BytesIO

import telebot
from telebot import types
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from PIL import Image

# QR kod o'qish uchun pyzbar (lekin serverda zbar bo'lmasa, QR o'chiriladi)
try:
    from pyzbar.pyzbar import decode as qr_decode
    QR_AVAILABLE = True
except Exception as e:
    print("pyzbar/zbar yuklanmadi, QR o‘qish vaqtincha o‘chiriladi:", e)
    qr_decode = None
    QR_AVAILABLE = False

# =========================
# 1. SOZLAMALAR
# =========================

# TOKEN:
# - Render'da bo'lsang: TOKEN environment'dan olinadi
# - Lokal kompyuteringda: shu yerda yozilgan qiymat ishlaydi
TOKEN = os.environ.get("TOKEN", "8522650018:AAHy-X-Xisalwy0Su1Hh4QW4ItkFYF4ib-8")

SERVICE_ACCOUNT_FILE = "service-account.json"
SPREADSHEET_ID = "1DJcHKX6boO-kH9zTB63cSkRof4kemDEhZftx7AylvzA"
CHANNEL_USERNAME = "@PR_klubi"

STUDENTS_SHEET = "Students"
ACTIVITIES_SHEET = "Activities"
PHOTOS_SHEET = "Photos"
ADMINS_SHEET = "Admins"

# 🔐 Bot egasi (FAQAT shu ID broadcast va admin boshqaruvini qiladi)
OWNER_ID = 387178074  # O'zingning Telegram ID'ing shu yerda

# Activities jadvalidagi statuslar
STATUS_PENDING = "Kutilmoqda"
STATUS_APPROVED = "Tasdiqlandi"
STATUS_REJECTED = "Rad etildi"

# Qayta ishga tushirish va ortga qaytish tugmalari
RESTART_LABEL = "🔄 Botni qayta ishga tushirish"
BACK_LABEL = "⬅️ Ortga qaytish"

# =========================
# 2. Google Sheets
# =========================

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Agar SERVICE_ACCOUNT_JSON env bor bo'lsa (Render) – shundan o'qiydi
# Aks holda lokal fayl (kompyuteringdagi service-account.json) dan o'qiydi
if "SERVICE_ACCOUNT_JSON" in os.environ:
    service_account_info = json.loads(os.environ["SERVICE_ACCOUNT_JSON"])
    creds = Credentials.from_service_account_info(service_account_info, scopes=SCOPES)
else:
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)

sheets_service = build("sheets", "v4", credentials=creds)
sheet = sheets_service.spreadsheets()

# =========================
# 3. Telegram bot
# =========================

bot = telebot.TeleBot(TOKEN, parse_mode="HTML")

# Faollik qo'shish flow'i holati
activity_state = {}  # { user_id: {...} }

# Admin panel holati
admin_state = {}     # { admin_id: {"stage": "...", "queue": [...] } }

# Broadcast holati (faqat egasi uchun)
broadcast_state = {}  # { owner_id: {"stage": "wait_message"} }

# =========================
# 4. Cache lar
# =========================

students_cache = {"data": [], "loaded_at": 0}
STUDENTS_CACHE_TTL = 60  # 1 daqiqa

activities_cache = {"data": [], "loaded_at": 0}
ACTIVITIES_CACHE_TTL = 60  # 1 daqiqa

photos_cache = {"data": [], "loaded_at": 0}
PHOTOS_CACHE_TTL = 60  # 1 daqiqa

membership_cache = {}     # {user_id: {"is_member": bool, "checked_at": ts}}
MEMBERSHIP_CACHE_TTL = 24 * 60 * 60   # 1 kun

admins_cache = {"ids": set(), "loaded_at": 0}
ADMINS_CACHE_TTL = 3600  # 🔁 1 soat

# =========================
# 5. QR yordamchi
# =========================

def decode_card_from_qr_bytes(file_bytes):
    # Agar QR_AVAILABLE = False bo'lsa (Render'da libzbar yo'q bo'lsa) –
    # har doim None qaytaramiz va foydalanuvchidan karta raqamini qo'lda so'raymiz.
    if not QR_AVAILABLE:
        return None

    try:
        img = Image.open(BytesIO(file_bytes))
        codes = qr_decode(img)
        if not codes:
            return None
        return codes[0].data.decode("utf-8").strip()
    except Exception as e:
        print("QR decode error:", e)
        return None

# =========================
# 6. Adminlar
# =========================

def get_admin_ids():
    now = time.time()
    if admins_cache["ids"] and (now - admins_cache["loaded_at"] < ADMINS_CACHE_TTL):
        return admins_cache["ids"]

    res = sheet.values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{ADMINS_SHEET}!A2:A"
    ).execute()
    rows = res.get("values", [])

    ids = set()
    for r in rows:
        if not r:
            continue
        raw = str(r[0]).strip()
        if not raw:
            continue
        try:
            ids.add(int(raw))
        except ValueError:
            continue

    # OWNER_ID doim adminlar ichida bo'lsin
    ids.add(OWNER_ID)

    admins_cache["ids"] = ids
    admins_cache["loaded_at"] = now
    return ids


def is_admin(user_id: int) -> bool:
    return user_id in get_admin_ids()


def add_admin_id(new_admin_id: int) -> bool:
    """
    OWNER orqali yangi admin qo'shish.
    True -> yangi admin qo'shildi,
    False -> allaqachon bor.
    """
    ids = get_admin_ids()
    if new_admin_id in ids:
        return False

    sheet.values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{ADMINS_SHEET}!A:A",
        valueInputOption="RAW",
        body={"values": [[str(new_admin_id)]]}
    ).execute()

    ids.add(new_admin_id)
    admins_cache["ids"] = ids
    admins_cache["loaded_at"] = time.time()
    return True


def remove_admin_id(target_admin_id: int) -> bool:
    """
    Adminni Admins sheet'dan olib tashlaydi (katakni bo'shatadi).
    OWNER_ID ni o'chirmaydi.
    """
    if target_admin_id == OWNER_ID:
        return False

    ids = get_admin_ids()
    if target_admin_id not in ids:
        return False

    res = sheet.values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{ADMINS_SHEET}!A2:A"
    ).execute()
    rows = res.get("values", [])

    found_row = None
    for idx, r in enumerate(rows, start=2):
        if not r:
            continue
        raw = str(r[0]).strip()
        try:
            val = int(raw)
        except ValueError:
            continue
        if val == target_admin_id:
            found_row = idx
            break

    if not found_row:
        # Sheet'da topilmasa ham, keshdan o'chirib qo'yamiz
        ids.discard(target_admin_id)
        admins_cache["ids"] = ids
        admins_cache["loaded_at"] = time.time()
        return True

    # Shu katakni bo'shatamiz
    sheet.values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{ADMINS_SHEET}!A{found_row}:A{found_row}",
        valueInputOption="RAW",
        body={"values": [[""]]}
    ).execute()

    ids.discard(target_admin_id)
    admins_cache["ids"] = ids
    admins_cache["loaded_at"] = time.time()
    return True

# =========================
# 7. Students / Activities / Photos
# =========================

def invalidate_students_cache():
    students_cache["data"] = []
    students_cache["loaded_at"] = 0


def invalidate_activities_cache():
    activities_cache["data"] = []
    activities_cache["loaded_at"] = 0


def invalidate_photos_cache():
    photos_cache["data"] = []
    photos_cache["loaded_at"] = 0


def get_students_rows():
    now = time.time()
    if students_cache["data"] and (now - students_cache["loaded_at"] < STUDENTS_CACHE_TTL):
        return students_cache["data"]

    res = sheet.values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{STUDENTS_SHEET}!A2:F"
    ).execute()
    rows = res.get("values", [])

    students_cache["data"] = rows
    students_cache["loaded_at"] = now
    return rows


def get_next_student_id():
    res = sheet.values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{STUDENTS_SHEET}!A2:A"
    ).execute()
    rows = res.get("values", [])
    if not rows:
        return 1
    max_id = 0
    for r in rows:
        if r and str(r[0]).isdigit():
            max_id = max(max_id, int(r[0]))
    return max_id + 1


def ensure_student_id(row_number: int, row: list) -> str:
    while len(row) < 6:
        row.append("")
    if row[0]:
        return row[0]

    new_id = get_next_student_id()
    row[0] = str(new_id)
    sheet.values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{STUDENTS_SHEET}!A{row_number}:F{row_number}",
        valueInputOption="RAW",
        body={"values": [row]}
    ).execute()
    invalidate_students_cache()
    return row[0]


def find_student_by_telegram_id(telegram_id: int):
    rows = get_students_rows()
    for idx, row in enumerate(rows, start=2):
        while len(row) < 6:
            row.append("")
        if str(row[3]) == str(telegram_id):
            student_id = ensure_student_id(idx, row)
            return {
                "row_number": idx,
                "id": student_id,
                "full_name": row[1],
                "card_code": row[2],
                "telegram_id": row[3],
                "total_points": row[4],
                "group_number": row[5]
            }
    return None


def find_student_by_id(student_id: str):
    rows = get_students_rows()
    for idx, row in enumerate(rows, start=2):
        while len(row) < 6:
            row.append("")
        if str(row[0]) == str(student_id):
            return {
                "row_number": idx,
                "id": row[0],
                "full_name": row[1],
                "card_code": row[2],
                "telegram_id": row[3],
                "total_points": row[4],
                "group_number": row[5]
            }
    return None


def find_row_by_card_code(card_code: str):
    rows = get_students_rows()
    for idx, row in enumerate(rows, start=2):
        while len(row) < 6:
            row.append("")
        if row[2] == card_code:
            return idx, row
    return None, None


def bind_card_to_telegram(row_number: int, telegram_id: int):
    res = sheet.values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{STUDENTS_SHEET}!A{row_number}:F{row_number}"
    ).execute()
    row = res.get("values", [[]])[0]
    while len(row) < 6:
        row.append("")

    if not row[0]:
        row[0] = str(get_next_student_id())
    student_id = row[0]

    full_name = row[1]
    card_code = row[2]

    row[3] = str(telegram_id)

    if not row[4]:
        row[4] = "0"
    total_points = row[4]

    group_number = row[5]

    sheet.values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{STUDENTS_SHEET}!A{row_number}:F{row_number}",
        valueInputOption="RAW",
        body={"values": [row]}
    ).execute()

    invalidate_students_cache()

    return {
        "id": student_id,
        "full_name": full_name,
        "card_code": card_code,
        "total_points": total_points,
        "group_number": group_number
    }


def increment_student_points(student_id: str, delta: int = 1):
    rows = get_students_rows()
    for idx, row in enumerate(rows, start=2):
        while len(row) < 6:
            row.append("")
        if str(row[0]) == str(student_id):
            try:
                current = int(row[4]) if row[4] else 0
            except ValueError:
                current = 0
            new_val = current + delta
            row[4] = str(new_val)
            sheet.values().update(
                spreadsheetId=SPREADSHEET_ID,
                range=f"{STUDENTS_SHEET}!A{idx}:F{idx}",
                valueInputOption="RAW",
                body={"values": [row]}
            ).execute()
            invalidate_students_cache()
            return new_val
    return None


def get_next_id(sheet_name: str):
    res = sheet.values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{sheet_name}!A2:A"
    ).execute()
    rows = res.get("values", [])
    if not rows:
        return 1
    max_id = 0
    for r in rows:
        if r and str(r[0]).isdigit():
            max_id = max(max_id, int(r[0]))
    return max_id + 1


def get_all_activities():
    now = time.time()
    if activities_cache["data"] and (now - activities_cache["loaded_at"] < ACTIVITIES_CACHE_TTL):
        return activities_cache["data"]

    res = sheet.values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{ACTIVITIES_SHEET}!A2:H"
    ).execute()
    rows = res.get("values", [])

    activities_cache["data"] = rows
    activities_cache["loaded_at"] = now
    return rows


def create_activity(student_id: str, title: str, description: str, category: str) -> int:
    activity_id = get_next_id(ACTIVITIES_SHEET)
    date_str = datetime.now().strftime("%Y-%m-%d")

    sheet.values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{ACTIVITIES_SHEET}!A:H",
        valueInputOption="RAW",
        body={"values": [[activity_id, student_id, title, description, date_str, STATUS_PENDING, "", category]]}
    ).execute()

    invalidate_activities_cache()
    return activity_id


def set_activity_status(activity_id: str, new_status: str):
    rows = get_all_activities()
    updated = False
    student_id = None
    title = ""
    for idx, row in enumerate(rows, start=2):
        while len(row) < 8:
            row.append("")
        if str(row[0]) == str(activity_id):
            old_status = row[5]
            row[5] = new_status
            student_id = row[1]
            title = row[2]
            sheet.values().update(
                spreadsheetId=SPREADSHEET_ID,
                range=f"{ACTIVITIES_SHEET}!A{idx}:H{idx}",
                valueInputOption="RAW",
                body={"values": [row]}
            ).execute()
            updated = (old_status != new_status)
            break
    if updated:
        invalidate_activities_cache()
    return updated, student_id, title


def set_activity_warning(activity_id: str):
    rows = get_all_activities()
    for idx, row in enumerate(rows, start=2):
        while len(row) < 8:
            row.append("")
        if str(row[0]) == str(activity_id):
            row[6] = "⚠️"
            sheet.values().update(
                spreadsheetId=SPREADSHEET_ID,
                range=f"{ACTIVITIES_SHEET}!A{idx}:H{idx}",
                valueInputOption="RAW",
                body={"values": [row]}
            ).execute()
            invalidate_activities_cache()
            break


def get_student_activities(student_id: str):
    rows = get_all_activities()
    acts = []
    for row in rows:
        while len(row) < 8:
            row.append("")
        if row[1] == str(student_id):
            acts.append({
                "id": row[0],
                "title": row[2],
                "description": row[3],
                "date": row[4],
                "status": row[5] or STATUS_PENDING,
                "warning": row[6],
                "category": row[7] or "Boshqa"
            })
    return acts


def get_pending_activity_ids():
    rows = get_all_activities()
    ids = []
    for row in rows:
        while len(row) < 8:
            row.append("")
        status = row[5]
        if not status or status == STATUS_PENDING:
            ids.append(row[0])
    return ids


def get_activity_by_id(activity_id: str):
    rows = get_all_activities()
    for row in rows:
        while len(row) < 8:
            row.append("")
        if str(row[0]) == str(activity_id):
            return {
                "id": row[0],
                "student_id": row[1],
                "title": row[2],
                "description": row[3],
                "date": row[4],
                "status": row[5] or STATUS_PENDING,
                "warning": row[6],
                "category": row[7] or "Boshqa"
            }
    return None


def get_photos_rows():
    now = time.time()
    if photos_cache["data"] and (now - photos_cache["loaded_at"] < PHOTOS_CACHE_TTL):
        return photos_cache["data"]

    res = sheet.values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{PHOTOS_SHEET}!A2:C"
    ).execute()
    rows = res.get("values", [])

    photos_cache["data"] = rows
    photos_cache["loaded_at"] = now
    return rows


def get_activity_photos(activity_id: str):
    rows = get_photos_rows()
    photos = []
    for row in rows:
        while len(row) < 3:
            row.append("")
        if row[1] == str(activity_id):
            photos.append(row[2])
    return photos


def check_photo_warning(activity_id: int, file_id: str):
    rows = get_photos_rows()
    for row in rows:
        while len(row) < 3:
            row.append("")
        old_activity_id = row[1]
        old_file_id = row[2]
        if old_file_id == file_id and old_activity_id != str(activity_id):
            set_activity_warning(activity_id)
            set_activity_warning(old_activity_id)
            break


def add_photo(activity_id: int, file_id: str):
    check_photo_warning(activity_id, file_id)
    photo_id = get_next_id(PHOTOS_SHEET)
    sheet.values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{PHOTOS_SHEET}!A:C",
        valueInputOption="RAW",
        body={"values": [[photo_id, activity_id, file_id]]}
    ).execute()
    invalidate_photos_cache()

# =========================
# 8. Reyting
# =========================

def get_top_students(limit: int = 10):
    rows = get_students_rows()
    students = []
    for row in rows:
        while len(row) < 6:
            row.append("")
        try:
            pts = int(row[4]) if row[4] else 0
        except ValueError:
            pts = 0
        students.append({
            "id": row[0],
            "full_name": row[1],
            "card_code": row[2],
            "telegram_id": row[3],
            "total_points": pts,
            "group_number": row[5]
        })
    students.sort(key=lambda x: x["total_points"], reverse=True)
    return students[:limit]

# =========================
# 9. Broadcast (faqat egasi uchun)
# =========================

def get_all_broadcast_user_ids():
    rows = get_students_rows()
    ids = set()
    for row in rows:
        while len(row) < 4:
            row.append("")
        tg = row[3]
        if tg:
            try:
                ids.add(int(tg))
            except ValueError:
                continue
    return ids


def broadcast_copy_message(source_message) -> int:
    ids = get_all_broadcast_user_ids()
    sent = 0
    for uid in ids:
        try:
            bot.copy_message(
                chat_id=uid,
                from_chat_id=source_message.chat.id,
                message_id=source_message.message_id
            )
            sent += 1
            time.sleep(0.05)
        except Exception as e:
            print("Broadcast copy error:", uid, e)
            continue
    return sent

# =========================
# 10. Kanalga a'zolik
# =========================

def is_member(user_id: int) -> bool:
    now = time.time()
    cached = membership_cache.get(user_id)
    if cached and (now - cached["checked_at"] < MEMBERSHIP_CACHE_TTL):
        return cached["is_member"]

    try:
        member = bot.get_chat_member(CHANNEL_USERNAME, user_id)
        is_mem = member.status in ["member", "administrator", "creator"]
    except Exception:
        is_mem = False

    membership_cache[user_id] = {
        "is_member": is_mem,
        "checked_at": now
    }
    return is_mem

# =========================
# 11. Klaviaturalar
# =========================

def add_restart_row(kb: types.ReplyKeyboardMarkup):
    kb.row(RESTART_LABEL)
    return kb


def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("👤 Profilim")
    kb.row("➕ Ijtimoiy faollik qo'shish")
    kb.row("📂 Faolliklarim")
    kb.row("🏆 Reyting")
    add_restart_row(kb)
    return kb


def admin_menu_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("✅ Faolliklarni tasdiqlash")
    kb.row("🔎 Talabani tekshirish")
    kb.row("📢 E’lon yuborish")
    kb.row("➕ Admin qo'shish", "🗑 Adminni olib tashlash")
    kb.row("⬅️ Admin rejimdan chiqish")
    add_restart_row(kb)
    return kb


def activity_category_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("Tadbir", "Tanlov")
    kb.row("Volontyorlik", "Boshqa")
    kb.row(BACK_LABEL)
    add_restart_row(kb)
    return kb


def restart_only_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(BACK_LABEL)
    add_restart_row(kb)
    return kb

# =========================
# 12. Ro'yxatdan o'tish yordamchi
# =========================

def register_by_card_code(user_id: int, chat_id: int, card_code: str):
    row_number, row = find_row_by_card_code(card_code)

    if not row:
        bot.send_message(
            chat_id,
            f"❌ {card_code} kodli karta topilmadi.\n"
            "Kod to'g'ri yozilganini tekshirib, qayta yuboring."
        )
        return

    while len(row) < 6:
        row.append("")

    existing_tg = row[3]
    if existing_tg and existing_tg != str(user_id):
        bot.send_message(
            chat_id,
            "❌ Bu karta kodi allaqachon boshqa Telegram akkauntiga bog'langan.\n"
            "Agar Telegram akkauntingiz o'zgargan bo'lsa yoki bu xato deb o'ylasangiz, iltimos admin bilan bog'laning."
        )
        return

    info = bind_card_to_telegram(row_number, user_id)

    bot.send_message(
        chat_id,
        "🎉 Tabriklayman, siz ro'yxatdan o'tdingiz!\n\n"
        f"Ism-familiya: {info['full_name']}\n"
        f"Talaba ID: {info['id']}\n"
        f"Karta: {info['card_code']}\n"
        f"Guruh: {info['group_number']}\n"
        f"Ball: {info['total_points']}",
        reply_markup=main_menu()
    )

# =========================
# 13. Admin – talabani tekshirish (karta bo'yicha)
# =========================

def admin_lookup_by_card_code(admin_id: int, chat_id: int, card_code: str):
    state = admin_state.get(admin_id)
    if not state:
        return

    row_number, row = find_row_by_card_code(card_code)
    if not row:
        bot.send_message(
            chat_id,
            f"❌ {card_code} kodli karta topilmadi.\n"
            "Kod to'g'ri yozilganini tekshirib, qayta yuboring."
        )
        return

    while len(row) < 6:
        row.append("")

    existing_tg = row[3]
    student_id = ensure_student_id(row_number, row)
    full_name = row[1]
    total_points = int(row[4]) if row[4] else 0
    group_number = row[5]

    acts = get_student_activities(student_id)
    count_acts = len(acts)

    lines = [
        "👤 <b>Talaba ma'lumotlari:</b>",
        f"Ism-familiya: {full_name}",
        f"Talaba ID: {student_id}",
        f"Karta: {card_code}",
        f"Guruh: {group_number}",
        f"Ball: {total_points}",
        f"Faolliklar soni: {count_acts}",
    ]
    if existing_tg:
        lines.append(f"Telegram ID: <code>{existing_tg}</code>")

    if acts:
        lines.append("\n📂 <b>Faolliklar:</b>")
        for a in acts:
            title_line = a["title"]
            if a["warning"]:
                title_line = "⚠️ " + title_line
            lines.append(
                f"\n#{a['id']} — {title_line}\n"
                f"Tur: {a['category']}\n"
                f"📅 {a['date']}\n"
                f"📌 Status: {a['status']}\n"
                f"📝 Tavsif: {a['description']}"
            )
    else:
        lines.append("\nBu talaba uchun hali faollik kiritilmagan.")

    text_out = "\n".join(lines)

    kb = types.InlineKeyboardMarkup()
    if acts:
        kb.add(
            types.InlineKeyboardButton(
                "🖼 Rasmlarni alohida ko‘rish",
                callback_data=f"astuphotos_{student_id}"
            )
        )

    bot.send_message(chat_id, text_out, reply_markup=kb, parse_mode="HTML")

    state["stage"] = "menu"
    bot.send_message(
        chat_id,
        "✅ Talaba ma'lumotlari chiqdi.\nAdmin menyuga qaytdingiz.",
        reply_markup=admin_menu_keyboard()
    )

# =========================
# 14. /start
# =========================

@bot.message_handler(commands=['start'])
def handle_start(message):
    user_id = message.from_user.id

    if not is_member(user_id):
        bot.send_message(
            message.chat.id,
            f"❗️ Botdan foydalanish uchun avval kanalga a'zo bo'ling:\n{CHANNEL_USERNAME}",
            reply_markup=restart_only_keyboard()
        )
        return

    student = find_student_by_telegram_id(user_id)
    if student:
        bot.send_message(
            message.chat.id,
            f"Assalomu alaykum, {student['full_name']}!\n\n"
            f"ID: {student['id']}\n"
            f"Karta: {student['card_code']}\n"
            f"Guruh: {student['group_number']}\n"
            f"Ball: {student['total_points']}",
            reply_markup=main_menu()
        )
    else:
        bot.send_message(
            message.chat.id,
            "Assalomu alaykum! 😊\n"
            "Klub a'zosi sifatida ro'yxatdan o'tish uchun kartangizdagi raqamni "
            "yoki karta ustidagi QR kod rasmini yuboring.\n"
            "Masalan: <b>A2024-001</b>",
            reply_markup=restart_only_keyboard()
        )

# =========================
# 15. /admin – admin panel
# =========================

@bot.message_handler(commands=['admin'])
def handle_admin_command(message):
    user_id = message.from_user.id

    if not is_admin(user_id):
        bot.send_message(message.chat.id, "❌ Sizda admin panelga kirish huquqi yo'q.")
        return

    if not is_member(user_id):
        bot.send_message(
            message.chat.id,
            f"❗️ Admin paneldan foydalanish uchun ham kanalga a'zo bo'ling:\n{CHANNEL_USERNAME}",
            reply_markup=restart_only_keyboard()
        )
        return

    admin_state[user_id] = {"stage": "menu", "queue": []}

    bot.send_message(
        message.chat.id,
        "🛠 Admin panelga xush kelibsiz.\nQuyidagi tugmadan birini tanlang:",
        reply_markup=admin_menu_keyboard()
    )

# =========================
# 16. /broadcast – faqat egasi uchun
# =========================

@bot.message_handler(commands=['broadcast'])
def handle_broadcast_cmd(message):
    user_id = message.from_user.id
    if user_id != OWNER_ID:
        bot.send_message(message.chat.id, "❌ Bu buyruq faqat bot egasi uchun.")
        return

    broadcast_state[user_id] = {"stage": "wait_message"}
    bot.send_message(
        message.chat.id,
        "Yubormoqchi bo'lgan xabaringizni yuboring.\n"
        "U <b>barcha ro'yxatdan o'tgan talabalar</b>ga aynan shu ko‘rinishda (matn/rasm/video/poll) jo'natiladi.",
        parse_mode="HTML",
        reply_markup=restart_only_keyboard()
    )

# =========================
# 17. Admin – faolliklarni tasdiqlash
# =========================

def send_next_pending_activity(admin_id: int, chat_id: int):
    state = admin_state.get(admin_id)
    if not state:
        return

    queue = state.get("queue", [])
    if not queue:
        pending_ids = get_pending_activity_ids()
        random.shuffle(pending_ids)
        state["queue"] = pending_ids
        queue = state["queue"]

    if not queue:
        bot.send_message(
            chat_id,
            "⏱ Hozircha tasdiqlanishi kerak bo'lgan faolliklar yo'q.",
            reply_markup=admin_menu_keyboard()
        )
        state["stage"] = "menu"
        return

    activity_id = queue.pop(0)
    activity = get_activity_by_id(activity_id)
    if not activity:
        return send_next_pending_activity(admin_id, chat_id)

    student = find_student_by_id(activity["student_id"])

    title_line = activity["title"]
    if activity["warning"]:
        title_line = "⚠️ " + title_line

    lines = [
        f"📝 <b>Faollik ID:</b> {activity['id']}",
        f"<b>Tadbir nomi:</b> {title_line}",
        f"Tur: {activity['category']}",
        f"📅 Sana: {activity['date']}",
        f"📌 Status: {activity['status']}",
        "",
    ]

    if student:
        lines += [
            "👤 <b>Talaba:</b>",
            f"Ismi: {student['full_name']}",
            f"Guruh: {student['group_number']}",
            f"Karta: {student['card_code']}",
            f"Jami ball: {student['total_points']}",
            "",
        ]

    lines += [
        "📝 <b>Tavsif:</b>",
        activity["description"]
    ]

    caption = "\n".join(lines)

    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("🖼 Rasm(lar)ni ko‘rish", callback_data=f"aphotos_{activity_id}")
    )
    kb.row(
        types.InlineKeyboardButton("✅ Tasdiqlash", callback_data=f"approve_{activity_id}"),
        types.InlineKeyboardButton("❌ Rad etish", callback_data=f"reject_{activity_id}")
    )

    bot.send_message(chat_id, caption, reply_markup=kb, parse_mode="HTML")

# =========================
# 18. Admin flow (menu, talaba, tasdiq, admin qo'shish/olib tashlash)
# =========================

def handle_admin_flow(message):
    user_id = message.from_user.id
    state = admin_state.get(user_id)
    if not state:
        return

    text = message.text.strip()
    stage = state["stage"]

    if stage == "menu":
        if text == "✅ Faolliklarni tasdiqlash":
            state["stage"] = "review"
            state["queue"] = []
            bot.send_message(
                message.chat.id,
                "Tasdiqlanishi kerak bo'lgan faolliklar tasodifiy tartibda chiqariladi.",
                reply_markup=admin_menu_keyboard()
            )
            send_next_pending_activity(user_id, message.chat.id)
            return

        if text == "🔎 Talabani tekshirish":
            state["stage"] = "wait_card"
            bot.send_message(
                message.chat.id,
                "HEMIS loginingizni kiriting",
                reply_markup=restart_only_keyboard()
            )
            return

        if text == "📢 E’lon yuborish":
            if user_id != OWNER_ID:
                bot.send_message(
                    message.chat.id,
                    "❌ E’lon yuborish faqat bot egasiga ruxsat etilgan.",
                    reply_markup=admin_menu_keyboard()
                )
                return
            broadcast_state[user_id] = {"stage": "wait_message"}
            bot.send_message(
                message.chat.id,
                "E’lon sifatida yubormoqchi bo'lgan xabaringizni yuboring.\n"
                "Masalan: matn, rasm + matn, video, poll va h.k.",
                parse_mode="HTML",
                reply_markup=restart_only_keyboard()
            )
            return

        if text == "➕ Admin qo'shish":
            if user_id != OWNER_ID:
                bot.send_message(
                    message.chat.id,
                    "❌ Yangi admin qo'shish faqat bot egasiga ruxsat etilgan.",
                    reply_markup=admin_menu_keyboard()
                )
                return
            state["stage"] = "wait_new_admin"
            bot.send_message(
                message.chat.id,
                "Yangi adminni qo'shish uchun:\n"
                "• Admin bo'lishi kerak bo'lgan foydalanuvchining xabarini forward qiling, yoki\n"
                "• Uning <b>Telegram ID</b> sini raqam ko'rinishida yuboring.\n\n"
                "Masalan: <code>123456789</code>",
                parse_mode="HTML",
                reply_markup=restart_only_keyboard()
            )
            return

        if text == "🗑 Adminni olib tashlash":
            if user_id != OWNER_ID:
                bot.send_message(
                    message.chat.id,
                    "❌ Adminni olib tashlash faqat bot egasiga ruxsat etilgan.",
                    reply_markup=admin_menu_keyboard()
                )
                return
            state["stage"] = "wait_remove_admin"
            bot.send_message(
                message.chat.id,
                "Olib tashlamoqchi bo'lgan adminning xabarini forward qiling yoki "
                "uning <b>Telegram ID</b> sini yuboring.\n\n"
                "Eslatma: o'zingizni (bot egasini) o'chira olmaysiz.",
                parse_mode="HTML",
                reply_markup=restart_only_keyboard()
            )
            return

        if text == "⬅️ Admin rejimdan chiqish":
            del admin_state[user_id]
            bot.send_message(
                message.chat.id,
                "Admin rejimdan chiqdingiz.",
                reply_markup=main_menu()
            )
            return

        bot.send_message(
            message.chat.id,
            "Iltimos, admin menyudagi tugmalardan birini tanlang.",
            reply_markup=admin_menu_keyboard()
        )
        return

    if stage == "wait_card":
        card_code = text
        admin_lookup_by_card_code(user_id, message.chat.id, card_code)
        return

    if stage == "review":
        bot.send_message(
            message.chat.id,
            "Faolliklarni tasdiqlash uchun pastdagi tugmalardan foydalaning.",
            reply_markup=admin_menu_keyboard()
        )
        return

    if stage == "wait_new_admin":
        if user_id != OWNER_ID:
            state["stage"] = "menu"
            bot.send_message(
                message.chat.id,
                "❌ Bu bo'lim faqat bot egasi uchun.",
                reply_markup=admin_menu_keyboard()
            )
            return

        new_admin_id = None

        if message.forward_from:
            new_admin_id = message.forward_from.id
        else:
            try:
                new_admin_id = int(text)
            except ValueError:
                bot.send_message(
                    message.chat.id,
                    "❌ ID faqat raqamlardan iborat bo‘lishi kerak.\n"
                    "Yoki foydalanuvchi xabarini forward qilib yuboring.",
                    reply_markup=restart_only_keyboard()
                )
                return

        if new_admin_id == OWNER_ID:
            bot.send_message(
                message.chat.id,
                "Bu ID allaqachon bot egasi sifatida ro'yxatda 😊",
                reply_markup=admin_menu_keyboard()
            )
            state["stage"] = "menu"
            return

        added = add_admin_id(new_admin_id)
        if not added:
            bot.send_message(
                message.chat.id,
                "ℹ️ Bu ID allaqachon adminlar ro'yxatida mavjud.",
                reply_markup=admin_menu_keyboard()
            )
        else:
            bot.send_message(
                message.chat.id,
                f"✅ <code>{new_admin_id}</code> ID yangi admin sifatida qo'shildi.",
                parse_mode="HTML",
                reply_markup=admin_menu_keyboard()
            )

        state["stage"] = "menu"
        return

    if stage == "wait_remove_admin":
        if user_id != OWNER_ID:
            state["stage"] = "menu"
            bot.send_message(
                message.chat.id,
                "❌ Bu bo'lim faqat bot egasi uchun.",
                reply_markup=admin_menu_keyboard()
            )
            return

        target_id = None

        if message.forward_from:
            target_id = message.forward_from.id
        else:
            try:
                target_id = int(text)
            except ValueError:
                bot.send_message(
                    message.chat.id,
                    "❌ ID faqat raqamlardan iborat bo‘lishi kerak.\n"
                    "Yoki olib tashlamoqchi bo'lgan adminning xabarini forward qiling.",
                    reply_markup=restart_only_keyboard()
                )
                return

        if target_id == OWNER_ID:
            bot.send_message(
                message.chat.id,
                "❌ Bot egasini adminlar ro'yxatidan olib tashlab bo'lmaydi.",
                reply_markup=admin_menu_keyboard()
            )
            state["stage"] = "menu"
            return

        removed = remove_admin_id(target_id)
        if not removed:
            bot.send_message(
                message.chat.id,
                "ℹ️ Bu ID adminlar ro'yxatida topilmadi yoki allaqachon olib tashlangan.",
                reply_markup=admin_menu_keyboard()
            )
        else:
            bot.send_message(
                message.chat.id,
                f"✅ <code>{target_id}</code> ID adminlar ro'yxatidan olib tashlandi.",
                parse_mode="HTML",
                reply_markup=admin_menu_keyboard()
            )
        state["stage"] = "menu"
        return

# =========================
# 19. Callback – approve / reject / rasmlar
# =========================

@bot.callback_query_handler(
    func=lambda call: call.data.startswith(
        ("approve_", "reject_", "aphotos_", "myphotos_", "astuphotos_")
    )
)
def handle_callbacks(call):
    user_id = call.from_user.id
    data = call.data

    if data.startswith("approve_") or data.startswith("reject_"):
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "Siz admin emassiz.")
            return

        if data.startswith("approve_"):
            activity_id = data.split("_", 1)[1]
            new_status = STATUS_APPROVED
        else:
            activity_id = data.split("_", 1)[1]
            new_status = STATUS_REJECTED

        changed, student_id, title = set_activity_status(activity_id, new_status)

        if not student_id:
            bot.answer_callback_query(call.id, "Faollik topilmadi.")
            return

        if changed and new_status == STATUS_APPROVED:
            increment_student_points(student_id, 1)

        student = find_student_by_id(student_id)
        if student and student["telegram_id"]:
            try:
                if new_status == STATUS_APPROVED:
                    text = (
                        f"✅ Sizning <b>{title}</b> nomli faolligingiz tasdiqlandi.\n"
                        f"Sizga 1 ball qo'shildi."
                    )
                else:
                    text = f"❌ Sizning <b>{title}</b> nomli faolligingiz rad etildi."
                bot.send_message(int(student["telegram_id"]), text, parse_mode="HTML")
            except Exception:
                pass

        bot.answer_callback_query(
            call.id,
            "Faollik tasdiqlandi." if new_status == STATUS_APPROVED else "Faollik rad etildi."
        )

        state = admin_state.get(user_id)
        if state and state.get("stage") == "review":
            send_next_pending_activity(user_id, call.message.chat.id)
        return

    if data.startswith("aphotos_"):
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "Siz admin emassiz.")
            return
        activity_id = data.split("_", 1)[1]
        activity = get_activity_by_id(activity_id)
        if not activity:
            bot.answer_callback_query(call.id, "Faollik topilmadi.")
            return
        photos = get_activity_photos(activity_id)
        title_line = activity["title"]
        if activity["warning"]:
            title_line = "⚠️ " + title_line
        if not photos:
            bot.send_message(call.message.chat.id, "Bu faollik uchun rasm yuklanmagan.")
        else:
            caption = (
                f"<b>{title_line}</b>\n"
                f"{activity['description']}\n"
                f"Tur: {activity['category']}\n"
                f"📅 {activity['date']}"
            )
            media = []
            for idx, f_id in enumerate(photos):
                if idx == 0:
                    media.append(types.InputMediaPhoto(f_id, caption=caption, parse_mode="HTML"))
                else:
                    media.append(types.InputMediaPhoto(f_id))
            bot.send_media_group(call.message.chat.id, media)

            kb = types.InlineKeyboardMarkup()
            kb.row(
                types.InlineKeyboardButton("✅ Tasdiqlash", callback_data=f"approve_{activity_id}"),
                types.InlineKeyboardButton("❌ Rad etish", callback_data=f"reject_{activity_id}")
            )
            bot.send_message(
                call.message.chat.id,
                "Ushbu faollikni tasdiqlaysizmi yoki rad etasizmi?",
                reply_markup=kb
            )

        bot.answer_callback_query(call.id, "Rasmlar ko'rsatildi.")
        return

    if data.startswith("myphotos_"):
        student_id = data.split("_", 1)[1]
        acts = get_student_activities(student_id)
        if not acts:
            bot.send_message(call.message.chat.id, "Sizda faolliklar mavjud emas.")
            bot.answer_callback_query(call.id)
            return

        for a in acts:
            photos = get_activity_photos(a["id"])
            if not photos:
                continue
            title_line = a["title"]
            if a["warning"]:
                title_line = "⚠️ " + title_line
            caption = (
                f"<b>{title_line}</b>\n"
                f"{a['description']}\n"
                f"Tur: {a['category']}\n"
                f"📅 {a['date']}\n"
                f"📌 Status: {a['status']}"
            )
            media = []
            for idx, f_id in enumerate(photos):
                if idx == 0:
                    media.append(types.InputMediaPhoto(f_id, caption=caption, parse_mode="HTML"))
                else:
                    media.append(types.InputMediaPhoto(f_id))
            bot.send_media_group(call.message.chat.id, media)

        bot.answer_callback_query(call.id, "Rasmlar alohida ko'rsatildi.")
        return

    if data.startswith("astuphotos_"):
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "Siz admin emassiz.")
            return
        student_id = data.split("_", 1)[1]
        acts = get_student_activities(student_id)
        if not acts:
            bot.send_message(call.message.chat.id, "Bu talaba uchun faolliklar mavjud emas.")
            bot.answer_callback_query(call.id)
            return

        for a in acts:
            photos = get_activity_photos(a["id"])
            if not photos:
                continue
            title_line = a["title"]
            if a["warning"]:
                title_line = "⚠️ " + title_line
            caption = (
                f"<b>{title_line}</b>\n"
                f"{a['description']}\n"
                f"Tur: {a['category']}\n"
                f"📅 {a['date']}\n"
                f"📌 Status: {a['status']}"
            )
            media = []
            for idx, f_id in enumerate(photos):
                if idx == 0:
                    media.append(types.InputMediaPhoto(f_id, caption=caption, parse_mode="HTML"))
                else:
                    media.append(types.InputMediaPhoto(f_id))
            bot.send_media_group(call.message.chat.id, media)

        bot.answer_callback_query(call.id, "Talaba faolliklarining rasmlari ko'rsatildi.")
        return

# =========================
# 20. Faollik qo'shish flow'i
# =========================

def handle_activity_flow(message):
    user_id = message.from_user.id
    data = activity_state.get(user_id)
    if not data:
        return

    stage = data["stage"]

    if stage == "category" and message.content_type == "text":
        cat = message.text.strip()
        if cat not in ["Tadbir", "Tanlov", "Volontyorlik", "Boshqa"]:
            cat = "Boshqa"
        data["category"] = cat
        data["stage"] = "title"
        bot.send_message(
            message.chat.id,
            "Faollik nomini yuboring (masalan: 'Kiberxavfsizlik konferensiyasi qatnashchisi').",
            reply_markup=restart_only_keyboard()
        )
        return

    if stage == "title" and message.content_type == "text":
        data["title"] = message.text.strip()
        data["stage"] = "description"
        bot.send_message(
            message.chat.id,
            "Endi faollik tavsifini yuboring (qaerda, qachon, nimalar qildingiz...).",
            reply_markup=restart_only_keyboard()
        )
        return

    if stage == "description" and message.content_type == "text":
        data["description"] = message.text.strip()
        data["stage"] = "photos"

        kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
        kb.row("✅ Yakunlash")
        kb.row(BACK_LABEL)
        add_restart_row(kb)
        bot.send_message(
            message.chat.id,
            "Endi tasdiqlovchi rasmlarni yuboring.\n"
            "Tugatgandan so'ng <b>✅ Yakunlash</b> tugmasini bosing.",
            reply_markup=kb
        )
        return

    if stage == "photos":
        if message.content_type == "text" and message.text and message.text.strip() == "✅ Yakunlash":
            photos = data["photos"]
            student_id = data["student_id"]
            title = data["title"]
            description = data["description"]
            category = data.get("category", "Boshqa")

            activity_id = create_activity(student_id, title, description, category)
            for f_id in photos:
                add_photo(activity_id, f_id)

            bot.send_message(
                message.chat.id,
                f"Faolligingiz saqlandi! 🎉\nID: {activity_id}\nNomi: {title}\nTur: {category}\nStatus: {STATUS_PENDING}",
                reply_markup=main_menu()
            )
            del activity_state[user_id]
            return

        if message.content_type == "photo":
            file_id = message.photo[-1].file_id
            data["photos"].append(file_id)
            bot.send_message(
                message.chat.id,
                "Rasm qabul qilindi. Yana yuborishingiz yoki '✅ Yakunlash' ni bosishingiz mumkin."
            )
        else:
            bot.send_message(
                message.chat.id,
                "Iltimos, rasm yuboring yoki '✅ Yakunlash' ni bosing."
            )

# =========================
# 21. Text handler
# =========================

@bot.message_handler(content_types=['text'])
def handle_text(message):
    user_id = message.from_user.id
    text = message.text.strip()

    if text == BACK_LABEL:
        if user_id in activity_state:
            del activity_state[user_id]
        if user_id in broadcast_state:
            del broadcast_state[user_id]

        if is_admin(user_id) and user_id in admin_state:
            admin_state[user_id]["stage"] = "menu"
            bot.send_message(
                message.chat.id,
                "⬅️ Ortga qaytdingiz. Admin menyu:",
                reply_markup=admin_menu_keyboard()
            )
            return

        bot.send_message(
            message.chat.id,
            "⬅️ Ortga qaytdingiz. Bosh menyu:",
            reply_markup=main_menu()
        )
        return

    if text == RESTART_LABEL:
        if user_id in activity_state:
            del activity_state[user_id]
        if user_id in admin_state:
            del admin_state[user_id]
        if user_id in broadcast_state:
            del broadcast_state[user_id]

        bot.send_message(
            message.chat.id,
            "♻️ Bot qayta ishga tushirildi.",
        )
        handle_start(message)
        return

    if user_id == OWNER_ID and user_id in broadcast_state and broadcast_state[user_id]["stage"] == "wait_message":
        sent = broadcast_copy_message(message)
        del broadcast_state[user_id]
        bot.send_message(
            message.chat.id,
            f"📢 Xabar {sent} ta foydalanuvchiga yuborildi.",
            reply_markup=main_menu() if not is_admin(user_id) else admin_menu_keyboard()
        )
        return

    if user_id in activity_state and activity_state[user_id]["stage"] in ["category", "title", "description", "photos"]:
        return handle_activity_flow(message)

    if is_admin(user_id) and user_id in admin_state:
        return handle_admin_flow(message)

    if not is_member(user_id):
        bot.send_message(
            message.chat.id,
            f"❗️ Botdan foydalanish uchun avval kanalga a'zo bo'ling:\n{CHANNEL_USERNAME}",
            reply_markup=restart_only_keyboard()
        )
        return

    student = find_student_by_telegram_id(user_id)

    if text == "👤 Profilim":
        if not student:
            bot.send_message(
                message.chat.id,
                "Avval kartangizdagi kodni yoki karta ustidagi QR kodni yuborib ro'yxatdan o'ting.",
                reply_markup=restart_only_keyboard()
            )
            return
        bot.send_message(
            message.chat.id,
            f"Profilingiz:\n\n"
            f"Ism: {student['full_name']}\n"
            f"ID: {student['id']}\n"
            f"Karta: {student['card_code']}\n"
            f"Guruh: {student['group_number']}\n"
            f"Ball: {student['total_points']}",
            reply_markup=main_menu()
        )
        return

    if text == "➕ Ijtimoiy faollik qo'shish":
        if not student:
            bot.send_message(
                message.chat.id,
                "Avval kartangiz kodini yoki karta ustidagi QR kodni yuborib ro'yxatdan o'ting.",
                reply_markup=restart_only_keyboard()
            )
            return
        activity_state[user_id] = {
            "stage": "category",
            "student_id": student["id"],
            "category": "",
            "title": "",
            "description": "",
            "photos": []
        }
        bot.send_message(
            message.chat.id,
            "Faollik turini tanlang:",
            reply_markup=activity_category_keyboard()
        )
        return

    if text == "📂 Faolliklarim":
        if not student:
            bot.send_message(message.chat.id, "Avval ro'yxatdan o'ting.", reply_markup=restart_only_keyboard())
            return
        acts = get_student_activities(student["id"])
        if not acts:
            bot.send_message(message.chat.id, "Siz hali hech qanday faollik qo'shmagansiz.", reply_markup=main_menu())
            return

        lines = ["📂 Sizning faolliklaringiz:"]
        for a in acts:
            title_line = a["title"]
            if a["warning"]:
                title_line = "⚠️ " + title_line
            lines.append(
                f"\n#{a['id']} — {title_line}\n"
                f"Tur: {a['category']}\n"
                f"📅 {a['date']}\n"
                f"📌 Status: {a['status']}\n"
                f"📝 Tavsif: {a['description']}"
            )
        text_out = "\n".join(lines)

        kb = types.InlineKeyboardMarkup()
        kb.add(
            types.InlineKeyboardButton(
                "🖼 Rasmlarni alohida ko‘rish",
                callback_data=f"myphotos_{student['id']}"
            )
        )

        bot.send_message(message.chat.id, text_out, reply_markup=kb, parse_mode="HTML")
        return

    if text == "🏆 Reyting":
        top = get_top_students(limit=10)
        if not top:
            bot.send_message(message.chat.id, "Hozircha reyting uchun ma'lumot yo'q.", reply_markup=main_menu())
            return
        lines = ["🏆 Top 10 talabalar reytingi:"]
        for i, st in enumerate(top, start=1):
            lines.append(
                f"{i}. {st['full_name']} ({st['group_number']}) — {st['total_points']} ball"
            )
        bot.send_message(message.chat.id, "\n".join(lines), reply_markup=main_menu())
        return

    if student:
        bot.send_message(
            message.chat.id,
            "Menyudan birini tanlang yoki ortga qaytish/qayta ishga tushirish tugmasidan foydalaning.",
            reply_markup=main_menu()
        )
        return

    card_code = text
    register_by_card_code(user_id, message.chat.id, card_code)

# =========================
# 22. Photo handler
# =========================

@bot.message_handler(content_types=['photo'])
def handle_photo_message(message):
    user_id = message.from_user.id

    if user_id == OWNER_ID and user_id in broadcast_state and broadcast_state[user_id]["stage"] == "wait_message":
        sent = broadcast_copy_message(message)
        del broadcast_state[user_id]
        bot.send_message(
            message.chat.id,
            f"📢 Xabar {sent} ta foydalanuvchiga yuborildi.",
            reply_markup=main_menu() if not is_admin(user_id) else admin_menu_keyboard()
        )
        return

    if user_id in activity_state and activity_state[user_id]["stage"] == "photos":
        return handle_activity_flow(message)

    if is_admin(user_id) and user_id in admin_state and admin_state[user_id]["stage"] == "wait_card":
        try:
            file_info = bot.get_file(message.photo[-1].file_id)
            downloaded_file = bot.download_file(file_info.file_path)
        except Exception as e:
            print("Admin QR fayl xatosi:", e)
            bot.send_message(message.chat.id, "QR rasmni o'qishda xato yuz berdi. Qayta urinib ko'ring.")
            return

        card_code = decode_card_from_qr_bytes(downloaded_file)
        if not card_code:
            bot.send_message(
                message.chat.id,
                "❌ QR kodni o'qib bo'lmadi.\n"
                "Karta raqamini matn ko'rinishida yuboring yoki yanada aniqroq QR rasm yuboring."
            )
            return

        admin_lookup_by_card_code(user_id, message.chat.id, card_code)
        return

    student = find_student_by_telegram_id(user_id)
    if student:
        return

    try:
        file_info = bot.get_file(message.photo[-1].file_id)
        downloaded_file = bot.download_file(file_info.file_path)
    except Exception as e:
        print("QR fayl xatosi:", e)
        bot.send_message(message.chat.id, "QR rasmni o'qishda xato yuz berdi. Qayta urinib ko'ring.")
        return

    card_code = decode_card_from_qr_bytes(downloaded_file)
    if not card_code:
        bot.send_message(
            message.chat.id,
            "❌ QR kodni o'qib bo'lmadi.\n"
            "Kartadagi raqamni matn ko'rinishida yuboring yoki yanada aniqroq QR rasm yuboring."
        )
        return

    register_by_card_code(user_id, message.chat.id, card_code)

# =========================
# 23. Boshqa media turlari uchun broadcast handler
# =========================

@bot.message_handler(content_types=['video', 'document', 'animation', 'audio', 'voice', 'poll'])
def handle_broadcast_media(message):
    user_id = message.from_user.id

    if user_id == OWNER_ID and user_id in broadcast_state and broadcast_state[user_id]["stage"] == "wait_message":
        sent = broadcast_copy_message(message)
        del broadcast_state[user_id]
        bot.send_message(
            message.chat.id,
            f"📢 Xabar {sent} ta foydalanuvchiga yuborildi.",
            reply_markup=main_menu() if not is_admin(user_id) else admin_menu_keyboard()
        )
        return

# =========================
# 24. Botni ishga tushirish
# =========================

print("Bot ishga tushdi...")

while True:
    try:
        bot.infinity_polling(timeout=60, long_polling_timeout=40)
    except Exception as e:
        print("Polling error:", e)
        time.sleep(3)

