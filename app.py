import os
import io
import time
import json
import sqlite3
import threading
from datetime import datetime, timedelta
from flask import Flask, request, abort
from PIL import Image

# 引入 LINE SDK
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    MessagingApiBlob,
    ReplyMessageRequest,
    PushMessageRequest,
    TextMessage,
    TemplateMessage,
    ButtonsTemplate,
    PostbackAction
)
from linebot.v3.webhooks import (
    MessageEvent, ImageMessageContent, FollowEvent, PostbackEvent, TextMessageContent
)

# 引入 Google 最新官方 GenAI 套件
from google import genai
from google.genai import types

# --- 多國語言語系字典 (系統 UI 訊息) ---
LANGUAGE_MAP = {
    "zh": "繁體中文",
    "en": "English",
    "id": "Bahasa Indonesia"
}

UI_TEXTS = {
    "zh": {
        "current_lang": "🌐 目前設定的語言為：繁體中文",
        "select_lang_title": "請選擇語言 / Select Language",
        "select_lang_text": "請選擇系統介面與提醒的顯示語言：",
        "lang_set_success": "🌐 語言已成功設定為：繁體中文",
        "med_logged": "🎉 太好了！已為您紀錄吃藥時間：",
        "select_range_title": "📊 選擇用藥記錄查詢範圍",
        "select_range_text": "請點選您想查詢的時間區間：",
        "range_7": "📅 最近 7 天",
        "range_15": "📅 最近 15 天",
        "range_30": "📅 最近 30 天 (1個月)",
        "no_med_records": "ℹ️ 在指定的區間內查無任何吃藥紀錄。",
        "med_record_title": "📋 用藥紀錄查詢結果",
        "med_record_count": "總計服藥次數：",
        "bag_menu_title": "📋 藥袋資訊細節查詢",
        "bag_menu_text": "請點選下方按鈕查看特定項目：",
        "bag_btn_name": "🏷️ 藥品名稱",
        "bag_btn_usage": "🎯 適應症/用途",
        "bag_btn_dose": "⏰ 用法用量",
        "bag_btn_side": "🧪 副作用",
        "pill_menu_title": "💊 藥丸資訊細節查詢",
        "pill_menu_text": "請點選下方按鈕查看特定項目：",
        "pill_btn_name": "🏷️ 可能藥品名稱",
        "pill_btn_cat": "🩺 慢性病分類",
        "pill_btn_usage": "🎯 主要適應症/用途",
        "pill_btn_dose": "⏰ 一般常見用法",
        "no_pending_data": "⚠️ 查無近期辨識資料，請重新上傳照片。",
        "timer_set": "⏰ 好的！已為您設定 {} 分鐘後的吃藥提醒。",
        "timer_push": "🔔 提醒您該吃藥囉！"
    },
    "en": {
        "current_lang": "🌐 Current language setting: English",
        "select_lang_title": "Select Language",
        "select_lang_text": "Please select your preferred language:",
        "lang_set_success": "🌐 Language successfully set to: English",
        "med_logged": "🎉 Great! Medication time recorded: ",
        "select_range_title": "📊 Medication History Range",
        "select_range_text": "Please select the timeframe to query:",
        "range_7": "📅 Last 7 Days",
        "range_15": "📅 Last 15 Days",
        "range_30": "📅 Last 30 Days",
        "no_med_records": "ℹ️ No medication records found for the selected timeframe.",
        "med_record_title": "📋 Medication History",
        "med_record_count": "Total doses taken: ",
        "bag_menu_title": "📋 Drug Bag Details",
        "bag_menu_text": "Select an item to view details:",
        "bag_btn_name": "🏷️ Drug Name",
        "bag_btn_usage": "🎯 Indications",
        "bag_btn_dose": "⏰ Dosage",
        "bag_btn_side": "🧪 Side Effects",
        "pill_menu_title": "💊 Pill Details",
        "pill_menu_text": "Select an item to view details:",
        "pill_btn_name": "🏷️ Possible Name",
        "pill_btn_cat": "🩺 Chronic Category",
        "pill_btn_usage": "🎯 Indications",
        "pill_btn_dose": "⏰ Common Dosage",
        "no_pending_data": "⚠️ No recent recognition data found. Please upload a photo again.",
        "timer_set": "⏰ Got it! Medication reminder set for {} minutes later.",
        "timer_push": "🔔 Reminder: It's time to take your medication!"
    },
    "id": {
        "current_lang": "🌐 Pengaturan bahasa saat ini: Bahasa Indonesia",
        "select_lang_title": "Pilih Bahasa",
        "select_lang_text": "Silakan pilih bahasa pilihan Anda:",
        "lang_set_success": "🌐 Bahasa berhasil diubah ke: Bahasa Indonesia",
        "med_logged": "🎉 Bagus! Waktu minum obat berhasil dicatat: ",
        "select_range_title": "📊 Pilih Rentang Riwayat Obat",
        "select_range_text": "Pilih rentang waktu yang ingin Anda lihat:",
        "range_7": "📅 7 Hari Terakhir",
        "range_15": "📅 15 Hari Terakhir",
        "range_30": "📅 30 Hari Terakhir",
        "no_med_records": "ℹ️ Tidak ada catatan minum obat dalam rentang waktu ini.",
        "med_record_title": "📋 Riwayat Minum Obat",
        "med_record_count": "Total minum obat: ",
        "bag_menu_title": "📋 Detail Kantong Obat",
        "bag_menu_text": "Pilih item untuk melihat detail:",
        "bag_btn_name": "🏷️ Nama Obat",
        "bag_btn_usage": "🎯 Indikasi",
        "bag_btn_dose": "⏰ Dosis",
        "bag_btn_side": "🧪 Efek Samping",
        "pill_menu_title": "💊 Detail Pil Obat",
        "pill_menu_text": "Pilih item untuk melihat detail:",
        "pill_btn_name": "🏷️ Nama Obat",
        "pill_btn_cat": "🩺 Kategori Penyakit",
        "pill_btn_usage": "🎯 Indikasi",
        "pill_btn_dose": "⏰ Dosis Umum",
        "no_pending_data": "⚠️ Data tidak ditemukan. Silakan unggah foto obat kembali.",
        "timer_set": "⏰ Pengingat pengobatan diatur untuk {} menit lagi.",
        "timer_push": "🔔 Pengingat: Saatnya minum obat Anda!"
    }
}

app = Flask(__name__)
DB_NAME = "pharmacy.db"

# ==================== [SQLite 資料庫持久化模組] ====================
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    # 1. 辨識結果紀錄表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_recognitions (
            user_id TEXT PRIMARY KEY,
            last_result TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # 2. 使用者語言偏好表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_languages (
            user_id TEXT PRIMARY KEY,
            lang_code TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # 3. 我吃藥了打卡紀錄表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_medication_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()
    print("[資料庫] ➔ SQLite 資料庫與所有資料表初始化完成！")

init_db()

def save_user_language(user_id, lang_code):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO user_languages (user_id, lang_code, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
    ''', (user_id, lang_code))
    conn.commit()
    conn.close()

def get_user_language(user_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT lang_code FROM user_languages WHERE user_id = ?', (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else "zh"

def save_user_recognition(user_id, result_text):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO user_recognitions (user_id, last_result, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
    ''', (user_id, result_text))
    conn.commit()
    conn.close()

def get_user_recognition(user_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT last_result FROM user_recognitions WHERE user_id = ?', (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None

def log_medication_time(user_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('INSERT INTO user_medication_logs (user_id) VALUES (?)', (user_id,))
    conn.commit()
    conn.close()

def get_medication_logs(user_id, days):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute('''
        SELECT logged_at FROM user_medication_logs 
        WHERE user_id = ? AND logged_at >= ?
        ORDER BY logged_at DESC
    ''', (user_id, start_date))
    rows = cursor.fetchall()
    conn.close()
    return [row[0] for row in rows]

# ==================== [RAG 慢性病知識庫預載] ====================
RAG_KNOWLEDGE_BASE = ""
TXT_PATH = "drug_guide.txt"

try:
    if os.path.exists(TXT_PATH):
        with open(TXT_PATH, "r", encoding="utf-8") as f:
            RAG_KNOWLEDGE_BASE = f.read()
        print(f"[RAG系統] ➔ 成功載入慢性病藥品知識庫 TXT，共讀取 {len(RAG_KNOWLEDGE_BASE)} 字。")
    else:
        print(f"[RAG系統] ⚠️ 找不到 {TXT_PATH}，將使用通用醫學知識進行辨識。")
except Exception as e:
    print(f"[RAG系統] ❌ 讀取 TXT 知識庫失敗: {e}")

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
GOOGLE_API_KEY = os.environ.get('GOOGLE_API_KEY')

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
ai_client = genai.Client(api_key=GOOGLE_API_KEY)

# ==================== [AI 系統指令 (強制保持中文辨識結構)] ====================
SYSTEM_INSTRUCTION = """
你是一位擁有極高辨識精準度的「慢性病藥丸與藥袋審核專家」。
使用者上傳的照片可能是「藥袋」或「藥丸/藥片/膠囊」。

請嚴格執行以下規則：

【情況 A：如果是藥袋照片】
1. 完整進行 OCR 文字辨識，讀取藥袋上的藥品名稱、用法、用量與注意事項。
2. 必須嚴格遵守以下格式，標題與回答內容必須強制換行，且「回答內容與下一個標題之間必須空一行」：
📋 【藥袋辨識結果】
━━━━━━━━━━━━━━
【藥品名稱】：
[帶入藥品名稱]

【適應症/用途】：
[帶入適應症/用途]

【用法用量】：
[帶入用法用量]

【副作用】：
[帶入副作用]

【注意事項】：
[帶入注意事項]
━━━━━━━━━━━━━━
💡 提示：本系統辨識結果僅供參考，用藥前請務必再次核對藥袋，並遵照醫囑。

【情況 B：如果是藥丸/藥片/膠囊照片（雙重檢驗原則）】
1. 第一步（特徵擷取）：仔細辨認照片中藥丸正反兩面的刻字、英數代碼與顏色形狀。
2. 第二步（檢索對照）：嚴格比對【常見慢性病藥品參考知識庫】。若刻字不符絕對不可張冠李戴！
3. 🚨 終止條件：
- 若照片中的刻字或外觀在知識庫中找不到相符項目，請「嚴格且唯一」回傳以下文字，切勿輸出其他內容：
此藥丸非慢性病用藥，無法偵測
4. 成功比對輸出格式（標題與內容必須換行，且不同標題之間必須空一行，文字力求精確簡煉）：
💊 【慢性病藥丸辨識結果】
━━━━━━━━━━━━━━
【可能藥品名稱】：
[填寫精確藥名]

【外觀與刻字描述】：
[精煉描述刻字、形狀與顏色]

【慢性病分類】：
[例如：高血壓 / 高血脂 / 糖尿病]

【主要適應症/用途】：
[簡述主要用途]

【一般常見用法】：
[簡述用法]

【服用注意事項與警語】：
[精煉列出重要警語]
━━━━━━━━━━━━━━
💡 警語：單憑外觀辨識藥丸具備風險。本系統僅比對「常見慢性病指引用藥」，切勿盲目服用未知藥丸！若無法確認，請務必諮詢醫師或實體藥局。

通用規定事項：
- 嚴禁輸出任何客套話、問候語。
"""

def extract_section(text, keyword):
    try:
        lines = text.split('\n')
        capturing = False
        result = []
        for line in lines:
            if keyword in line:
                capturing = True
                if '：' in line and len(line.split('：')) > 1 and line.split('：')[1].strip():
                    result.append(line.split('：')[1].strip())
                continue
            elif capturing and ('【' in line or '━━━━━━━━━━━━━━' in line or '💡' in line):
                break
            elif capturing:
                if line.strip():
                    result.append(line.strip())
        return "\n".join(result) if result else "（未標示 / Not specified）"
    except Exception:
        return "（解析中...）"

def send_delay_reminder(user_id):
    lang = get_user_language(user_id)
    txts = UI_TEXTS.get(lang, UI_TEXTS["zh"])
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        try:
            line_bot_api.push_message(
                PushMessageRequest(
                    to=user_id,
                    messages=[TextMessage(text=txts["timer_push"])]
                )
            )
        except Exception as e:
            print(f"❌ [提醒發送失敗] ➔ {e}")

def get_language_selection_template(lang):
    txts = UI_TEXTS.get(lang, UI_TEXTS["zh"])
    return TemplateMessage(
        alt_text=txts["select_lang_title"],
        template=ButtonsTemplate(
            title=txts["select_lang_title"],
            text=txts["select_lang_text"],
            actions=[
                PostbackAction(label="繁體中文", data="lang=zh"),
                PostbackAction(label="English", data="lang=en"),
                PostbackAction(label="Bahasa Indonesia", data="lang=id")
            ]
        )
    )

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(FollowEvent)
def handle_follow(event):
    user_id = event.source.user_id
    lang = get_user_language(user_id)
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[get_language_selection_template(lang)]
            )
        )

@handler.add(PostbackEvent)
def handle_postback(event):
    data = event.postback.data
    user_id = event.source.user_id
    
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)

        # 1. 語言設定修改 (寫入 SQLite 永久紀錄)
        if data.startswith("lang="):
            lang_code = data.split("=")[1]
            save_user_language(user_id, lang_code)
            txts = UI_TEXTS.get(lang_code, UI_TEXTS["zh"])
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=txts["lang_set_success"])]
                )
            )

        # 2. 查詢用藥紀錄時間範圍選擇 (7天, 15天, 30天)
        elif data.startswith("range="):
            days = int(data.split("=")[1])
            lang = get_user_language(user_id)
            txts = UI_TEXTS.get(lang, UI_TEXTS["zh"])
            logs = get_medication_logs(user_id, days)

            if not logs:
                reply_text = txts["no_med_records"]
            else:
                formatted_logs = "\n".join([f"• {log}" for log in logs])
                reply_text = f"📊 【{txts['med_record_title']} ({days} 天)】\n━━━━━━━━━━━━━━\n{txts['med_record_count']}{len(logs)} 次\n\n【詳細紀錄 / Details】：\n{formatted_logs}\n━━━━━━━━━━━━━━"

            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply_text)]
                )
            )

        # 3. 點選藥袋細節
        elif data.startswith("view_bag="):
            view_type = data.split("=")[1]
            stored_data = get_user_recognition(user_id)
            lang = get_user_language(user_id)
            txts = UI_TEXTS.get(lang, UI_TEXTS["zh"])

            if not stored_data:
                reply_msg = txts["no_pending_data"]
            else:
                warning_info = extract_section(stored_data, "注意事項")
                if view_type == "name":
                    val = extract_section(stored_data, "藥品名稱")
                    reply_msg = f"🏷️ 【藥品名稱】：\n{val}\n\n⚠️ 【注意事項】：\n{warning_info}"
                elif view_type == "usage":
                    val = extract_section(stored_data, "適應症/用途")
                    reply_msg = f"🎯 【適應症/用途】：\n{val}\n\n⚠️ 【注意事項】：\n{warning_info}"
                elif view_type == "dose":
                    val = extract_section(stored_data, "用法用量")
                    reply_msg = f"⏰ 【用法用量】：\n{val}\n\n⚠️ 【注意事項】：\n{warning_info}"
                elif view_type == "side_effect":
                    val = extract_section(stored_data, "副作用")
                    reply_msg = f"🧪 【副作用】：\n{val}\n\n⚠️ 【注意事項】：\n{warning_info}"
                else:
                    reply_msg = stored_data

            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply_msg)]
                )
            )

        # 4. 點選藥丸細節
        elif data.startswith("view_pill="):
            view_type = data.split("=")[1]
            stored_data = get_user_recognition(user_id)
            lang = get_user_language(user_id)
            txts = UI_TEXTS.get(lang, UI_TEXTS["zh"])

            if not stored_data:
                reply_msg = txts["no_pending_data"]
            else:
                warning_info = extract_section(stored_data, "服用注意事項與警語")
                if view_type == "name":
                    val = extract_section(stored_data, "可能藥品名稱")
                    reply_msg = f"🏷️ 【可能藥品名稱】：\n{val}\n\n⚠️ 【注意事項與警語】：\n{warning_info}"
                elif view_type == "category":
                    val = extract_section(stored_data, "慢性病分類")
                    reply_msg = f"🩺 【慢性病分類】：\n{val}\n\n⚠️ 【注意事項與警語】：\n{warning_info}"
                elif view_type == "usage":
                    val = extract_section(stored_data, "主要適應症/用途")
                    reply_msg = f"🎯 【主要適應症/用途】：\n{val}\n\n⚠️ 【注意事項與警語】：\n{warning_info}"
                elif view_type == "dose":
                    val = extract_section(stored_data, "一般常見用法")
                    reply_msg = f"⏰ 【一般常見用法】：\n{val}\n\n⚠️ 【注意事項與警語】：\n{warning_info}"
                else:
                    reply_msg = stored_data

            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply_msg)]
                )
            )

@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    user_message = event.message.text.strip()
    user_id = event.source.user_id
    lang = get_user_language(user_id)
    txts = UI_TEXTS.get(lang, UI_TEXTS["zh"])

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)

        # --- A. 處理「我吃藥了」打卡功能 ---
        if user_message == "我吃藥了":
            log_medication_time(user_id)
            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            reply_text = f"{txts['med_logged']}\n⏱️ {current_time}"
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply_text)]
                )
            )

        # --- B. 處理「查看用藥紀錄」跳出 7/15/30 天按鈕選單 ---
        elif user_message in ["查看用藥紀錄"]:
            range_menu = TemplateMessage(
                alt_text=txts["select_range_title"],
                template=ButtonsTemplate(
                    title=txts["select_range_title"],
                    text=txts["select_range_text"],
                    actions=[
                        PostbackAction(label=txts["range_7"], data="range=7"),
                        PostbackAction(label=txts["range_15"], data="range=15"),
                        PostbackAction(label=txts["range_30"], data="range=30")
                    ]
                )
            )
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[range_menu]
                )
            )

        # --- C. 處理圖文選單按鈕：「語言翻譯」/「語言設定」/「重新選擇語言」---
        elif user_message in ["語言翻譯"]:
            current_lang_name = LANGUAGE_MAP.get(lang, "繁體中文")
            info_msg = TextMessage(text=f"🌐 目前設定的語言為：{current_lang_name}\nCurrent Language: {current_lang_name}")
            select_menu = get_language_selection_template(lang)
            
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[info_msg, select_menu]
                )
            )

        # --- D. 定時吃藥提醒指令 ---
        else:
            timer_options = {
                "5分鐘後提醒我": {"seconds": 300, "text": "5"},
                "10分鐘後提醒我": {"seconds": 600, "text": "10"},
                "15分鐘後提醒我": {"seconds": 900, "text": "15"}
            }
            if user_message in timer_options:
                selected_option = timer_options[user_message]
                delay_seconds = selected_option["seconds"]
                minutes_text = selected_option["text"]
                try:
                    line_bot_api.reply_message(
                        ReplyMessageRequest(
                            reply_token=event.reply_token,
                            messages=[TextMessage(text=txts["timer_set"].format(minutes_text))]
                        )
                    )
                    threading.Timer(delay_seconds, send_delay_reminder, args=[user_id]).start()
                except Exception as reply_error:
                    print(f"❌ [發送即時確認失敗] ➔ {reply_error}")

@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image_message(event):
    with ApiClient(configuration) as api_client:
        line_bot_blob_api = MessagingApiBlob(api_client)
        line_bot_api = MessagingApi(api_client)
        
        base_result_text = ""
        user_id = event.source.user_id
        lang = get_user_language(user_id)
        txts = UI_TEXTS.get(lang, UI_TEXTS["zh"])

        try:
            message_id = event.message.id
            message_content = line_bot_blob_api.get_message_content(message_id)
            image_bytes = io.BytesIO()
            if hasattr(message_content, 'iter_content'):
                for chunk in message_content.iter_content():
                    image_bytes.write(chunk)
            else:
                image_bytes.write(message_content if isinstance(message_content, bytes) else message_content.read())
            
            image_bytes.seek(0)
            raw_img = Image.open(image_bytes)
            raw_img.thumbnail((1200, 1200))
            
            compressed_io = io.BytesIO()
            raw_img.convert("RGB").save(compressed_io, format="JPEG", quality=85)
            compressed_io.seek(0)
            img = Image.open(compressed_io)
            image_bytes.close() 
            
            # 💡 注意：為保護設計者審核辨識結果，Prompt 中仍請 Gemini 回傳繁體中文
            prompt_content = f"""
請判斷這張照片是「藥袋」還是「藥丸/藥片/膠囊」。

【常見慢性病藥品參考知識庫 (RAG Knowledge Base)】：
{RAG_KNOWLEDGE_BASE}

【任務指示】：
1. 如果是「藥袋」：請精確萃取藥袋上的藥品名稱、適應症、用法用量與注意事項。
2. 如果是藥丸：請依據知識庫進行深度辨識。若非慢性病用藥，請回傳「此藥丸非慢性病用藥，無法偵測」。
3. 請全程使用「繁體中文」語言回覆。
"""
            
            candidate_models = ['gemini-2.5-flash', 'gemini-1.5-flash']
            response = None
            last_error = None

            for model_name in candidate_models:
                try:
                    response = ai_client.models.generate_content(
                        model=model_name,
                        contents=[img, prompt_content],
                        config=types.GenerateContentConfig(
                            system_instruction=SYSTEM_INSTRUCTION,
                            temperature=0.1
                        ),
                    )
                    if response:
                        break
                except Exception as err:
                    last_error = err
                    time.sleep(1)

            if not response:
                raise last_error

            base_result_text = response.text.strip()
            # 寫入 SQLite 供點選按鈕時讀取
            save_user_recognition(user_id, base_result_text)

            similarity_score = "90%"
            reasoning = "已完成系統檢核"
            metric_title = "•系統解析完整度"

            if "此藥丸非慢性病用藥，無法偵測" in base_result_text:
                similarity_score = "0% (未收錄)"
                reasoning = "該藥丸不在慢性病知識庫範圍內"
                metric_title = "與慢性病藥品匹配度"
            elif "📋 【藥袋辨識結果】" in base_result_text:
                metric_title = "圖文忠實度與準確度"
                bag_eval_prompt = f"""
你是一位嚴格的醫療 RAG 系統評估裁判。請同時檢視【原始上傳的藥袋圖片】與下方【AI 辨識出的藥袋結果】，評估其忠實度與準確度。
【AI 辨識回答】：{base_result_text}
請輸出 JSON 格式：{{"score": "60%", "reason": "部分藥品名稱與用法相符，但部分注意事項擷取模糊或有輕微遺漏"}}
"""
                try:
                    eval_response = ai_client.models.generate_content(
                        model='gemini-2.5-flash',
                        contents=[img, bag_eval_prompt], 
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json",
                            temperature=0.0
                        )
                    )
                    eval_json = json.loads(eval_response.text)
                    similarity_score = eval_json.get("score", "N/A")
                    reasoning = eval_json.get("reason", "")
                except Exception:
                    similarity_score = "N/A"
                    reasoning = "評估伺服器忙碌中，動態評估略過"
            else:
                metric_title = "資訊準確度與相似度"
                pill_eval_prompt = f"""
你是一位嚴格的醫療 RAG 系統評估裁判。請比對【知識庫】與【AI辨識回答】，評估忠實度。
【知識庫】：{RAG_KNOWLEDGE_BASE}
【回答】：{base_result_text}
請輸出 JSON 格式：{{"score": "60%", "reason": "外觀或刻字特徵僅部分吻合知識庫紀錄，存在一定程度的不確定性"}}
"""
                try:
                    eval_response = ai_client.models.generate_content(
                        model='gemini-2.5-flash',
                        contents=pill_eval_prompt,
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json",
                            temperature=0.0
                        )
                    )
                    eval_json = json.loads(eval_response.text)
                    similarity_score = eval_json.get("score", "N/A")
                    reasoning = eval_json.get("reason", "")
                except Exception:
                    similarity_score = "N/A"
                    reasoning = "評估伺服器忙碌中，無法順利比對知識庫，動態評估暫時中斷"

            result_text = f"{base_result_text}\n\n📊 【RAG 系統評估指標】\n━━━━━━━━━━━━━━\n• {metric_title}：{similarity_score}\n• 檢核說明：{reasoning}\n━━━━━━━━━━━━━━"

        except Exception as e:
            result_text = "❌ 辨識失敗。可能原因：照片過於模糊、反光、或是 Google AI 連線超時。請重新拍攝並再試一次！"

        # 💡 根據使用者的語言設定，動態生成 ButtonsTemplate 按鈕選單
        try:
            messages_to_send = [TextMessage(text=result_text)]
            if "此藥丸非慢性病用藥，無法偵測" not in base_result_text and "❌ 辨識失敗" not in result_text:
                if "📋 【藥袋辨識結果】" in base_result_text:
                    menu_template = TemplateMessage(
                        alt_text=txts["bag_menu_title"],
                        template=ButtonsTemplate(
                            title=txts["bag_menu_title"],
                            text=txts["bag_menu_text"],
                            actions=[
                                PostbackAction(label=txts["bag_btn_name"], data="view_bag=name"),
                                PostbackAction(label=txts["bag_btn_usage"], data="view_bag=usage"),
                                PostbackAction(label=txts["bag_btn_dose"], data="view_bag=dose"),
                                PostbackAction(label=txts["bag_btn_side"], data="view_bag=side_effect")
                            ]
                        )
                    )
                else:
                    menu_template = TemplateMessage(
                        alt_text=txts["pill_menu_title"],
                        template=ButtonsTemplate(
                            title=txts["pill_menu_title"],
                            text=txts["pill_menu_text"],
                            actions=[
                                PostbackAction(label=txts["pill_btn_name"], data="view_pill=name"),
                                PostbackAction(label=txts["pill_btn_cat"], data="view_pill=category"),
                                PostbackAction(label=txts["pill_btn_usage"], data="view_pill=usage"),
                                PostbackAction(label=txts["pill_btn_dose"], data="view_pill=dose")
                            ]
                        )
                    )
                messages_to_send.append(menu_template)

            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=messages_to_send
                )
            )
        except Exception as reply_error:
            print(f"❌ [回傳失敗] ➔ {reply_error}")

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
