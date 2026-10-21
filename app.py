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

LANGUAGE_MAP = {
    "zh": "繁體中文",
    "en": "English",
    "id": "Bahasa Indonesia"
}

# ==================== [輕量多國語言字典 (零 API 消耗、秒速回應、絕不爆 429)] ====================
UI_TEXTS = {
    "zh": {
        "current_lang": "🌐 目前設定的語言為：繁體中文",
        "select_lang_title": "請選擇語言 / Select Language",
        "select_lang_text": "請選擇藥物資訊與介面的顯示語言：",
        "lang_set_success": "🌐 語言已成功設定為：繁體中文",
        "med_logged": "🎉 太好了！已為您紀錄吃藥時間：",
        "select_range_title": "📊 選擇用藥記錄查詢範圍",
        "select_range_text": "請點選您想查詢的時間區間：",
        "range_7": "📅 最近 7 天",
        "range_15": "📅 最近 15 天",
        "range_30": "📅 最近 30 天",
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
        "timer_set": "⏰ 好的！已為您設定提醒。",
        "timer_push": "🔔 提醒您該吃藥囉！",
        "fail_msg": "❌ 辨識失敗。可能原因：照片過於模糊、反光、或是連線逾時。請重新拍攝並再試一次！"
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
        "timer_set": "⏰ Got it! Reminder set.",
        "timer_push": "🔔 Reminder: It's time to take your medication!",
        "fail_msg": "❌ Recognition failed. Please retake the photo and try again!"
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
        "timer_set": "⏰ Pengingat diatur.",
        "timer_push": "🔔 Pengingat: Saatnya minum obat Anda!",
        "fail_msg": "❌ Gagal mengenali. Silakan ambil foto ulang dan coba lagi!"
    }
}

app = Flask(__name__)
DB_NAME = "pharmacy.db"

# ==================== [SQLite 資料庫持久化模組] ====================
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_recognitions (
            user_id TEXT PRIMARY KEY,
            last_result TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_languages (
            user_id TEXT PRIMARY KEY,
            lang_code TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
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
    cursor.execute('INSERT OR REPLACE INTO user_languages (user_id, lang_code, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)', (user_id, lang_code))
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
    cursor.execute('INSERT OR REPLACE INTO user_recognitions (user_id, last_result, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)', (user_id, result_text))
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
    cursor.execute('SELECT logged_at FROM user_medication_logs WHERE user_id = ? AND logged_at >= ? ORDER BY logged_at DESC', (user_id, start_date))
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
except Exception as e:
    print(f"❌ 讀取 TXT 知識庫失敗: {e}")

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
GOOGLE_API_KEY = os.environ.get('GOOGLE_API_KEY')

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
ai_client = genai.Client(api_key=GOOGLE_API_KEY)

SYSTEM_INSTRUCTION = """
你是一位擁有極高辨識精準度的「慢性病藥丸與藥袋審核專家」。使用者上傳的照片可能是「藥袋」或「藥丸/藥片/膠囊」。
請嚴格執行格式規則，並全程使用繁體中文回覆以供開發者審核。
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
        return "\n".join(result) if result else "（未標示）"
    except Exception:
        return "（解析中...）"

def send_delay_reminder(user_id):
    lang = get_user_language(user_id)
    txts = UI_TEXTS.get(lang, UI_TEXTS["zh"])
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        try:
            line_bot_api.push_message(PushMessageRequest(to=user_id, messages=[TextMessage(text=txts["timer_push"])]))
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
        line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[get_language_selection_template(lang)]))

@handler.add(PostbackEvent)
def handle_postback(event):
    data = event.postback.data
    user_id = event.source.user_id
    lang = get_user_language(user_id)
    txts = UI_TEXTS.get(lang, UI_TEXTS["zh"])
    
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)

        if data.startswith("lang="):
            lang_code = data.split("=")[1]
            save_user_language(user_id, lang_code)
            new_txts = UI_TEXTS.get(lang_code, UI_TEXTS["zh"])
            line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=new_txts["lang_set_success"])]))

        elif data.startswith("range="):
            days = int(data.split("=")[1])
            logs = get_medication_logs(user_id, days)
            if not logs:
                reply_text = txts["no_med_records"]
            else:
                formatted_logs = "\n".join([f"• {log}" for log in logs])
                reply_text = f"📊 【{txts['med_record_title']} ({days} 天)】\n━━━━━━━━━━━━━━\n{txts['med_record_count']}{len(logs)} 次\n\n【詳細紀錄 / Details】：\n{formatted_logs}\n━━━━━━━━━━━━━━"

            line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=reply_text)]))

        elif data.startswith("view_bag=") or data.startswith("view_pill="):
            parts = data.split("=")
            view_type = parts[1]
            stored_data = get_user_recognition(user_id)

            if not stored_data:
                reply_msg = txts["no_pending_data"]
            else:
                if "view_bag" in parts[0]:
                    warning_info = extract_section(stored_data, "注意事項")
                    if view_type == "name":
                        val = extract_section(stored_data, "藥品名稱")
                        reply_msg = f"🏷️ 【{txts['bag_btn_name']}】：\n{val}\n\n⚠️ 【注意事項】：\n{warning_info}"
                    elif view_type == "usage":
                        val = extract_section(stored_data, "適應症/用途")
                        reply_msg = f"🎯 【{txts['bag_btn_usage']}】：\n{val}\n\n⚠️ 【注意事項】：\n{warning_info}"
                    elif view_type == "dose":
                        val = extract_section(stored_data, "用法用量")
                        reply_msg = f"⏰ 【{txts['bag_btn_dose']}】：\n{val}\n\n⚠️ 【注意事項】：\n{warning_info}"
                    elif view_type == "side_effect":
                        val = extract_section(stored_data, "副作用")
                        reply_msg = f"🧪 【{txts['bag_btn_side']}】：\n{val}\n\n⚠️ 【注意事項】：\n{warning_info}"
                    else:
                        reply_msg = stored_data
                else: # view_pill
                    warning_info = extract_section(stored_data, "服用注意事項與警語")
                    if view_type == "name":
                        val = extract_section(stored_data, "可能藥品名稱")
                        reply_msg = f"🏷️ 【{txts['pill_btn_name']}】：\n{val}\n\n⚠️ 【注意事項與警語】：\n{warning_info}"
                    elif view_type == "category":
                        val = extract_section(stored_data, "慢性病分類")
                        reply_msg = f"🩺 【{txts['pill_btn_cat']}】：\n{val}\n\n⚠️ 【注意事項與警語】：\n{warning_info}"
                    elif view_type == "usage":
                        val = extract_section(stored_data, "主要適應症/用途")
                        reply_msg = f"🎯 【{txts['pill_btn_usage']}】：\n{val}\n\n⚠️ 【注意事項與警語】：\n{warning_info}"
                    elif view_type == "dose":
                        val = extract_section(stored_data, "一般常見用法")
                        reply_msg = f"⏰ 【{txts['pill_btn_dose']}】：\n{val}\n\n⚠️ 【注意事項與警語】：\n{warning_info}"
                    else:
                        reply_msg = stored_data

            line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=reply_msg)]))

@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    user_message = event.message.text.strip()
    user_id = event.source.user_id
    lang = get_user_language(user_id)
    txts = UI_TEXTS.get(lang, UI_TEXTS["zh"])

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)

        if user_message in ["我吃藥了", "I took my medicine", "Saya sudah minum obat"]:
            log_medication_time(user_id)
            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            reply_text = f"{txts['med_logged']}\n⏱️ {current_time}"
            line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=reply_text)]))

        elif user_message in ["查看用藥紀錄", "用藥紀錄", "Medication History", "Riwayat Pengobatan"]:
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
            line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[range_menu]))

        elif user_message in ["語言翻譯", "語言設定", "重新選擇語言", "Language Settings", "Pengaturan Bahasa"]:
            current_lang_name = LANGUAGE_MAP.get(lang, "繁體中文")
            info_msg = TextMessage(text=f"{txts['current_lang']}")
            select_menu = get_language_selection_template(lang)
            line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[info_msg, select_menu]))

        else:
            timer_options = {
                "5分鐘後提醒我": 300, "10分鐘後提醒我": 600, "15分鐘後提醒我": 900
            }
            if user_message in timer_options:
                delay = timer_options[user_message]
                try:
                    line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=txts["timer_set"])]))
                    threading.Timer(delay, send_delay_reminder, args=[user_id]).start()
                except Exception as e:
                    print(f"❌ [提醒失敗] ➔ {e}")

@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image_message(event):
    with ApiClient(configuration) as api_client:
        line_bot_blob_api = MessagingApiBlob(api_client)
        line_bot_api = MessagingApi(api_client)
        
        user_id = event.source.user_id
        lang = get_user_language(user_id)
        txts = UI_TEXTS.get(lang, UI_TEXTS["zh"])
        base_result_text = ""

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
            
            prompt_content = f"""
請判斷這張照片是「藥袋」還是「藥丸/藥片/膠囊」。
【常見慢性病藥品參考知識庫 (RAG Knowledge Base)】：
{RAG_KNOWLEDGE_BASE}
【任務指示】：
1. 如果是「藥袋」：請精確萃取藥袋上的藥品名稱、適應症、用法用量與注意事項。
2. 如果是藥丸：請依據知識庫進行深度辨識。若非慢性病用藥，請回傳「此藥丸非慢性病用藥，無法偵測」。
3. 請全程使用「繁體中文」回覆。
"""
            
            response = ai_client.models.generate_content(
                model='gemini-2.5-flash',
                contents=[img, prompt_content],
                config=types.GenerateContentConfig(system_instruction=SYSTEM_INSTRUCTION, temperature=0.1)
            )
            base_result_text = response.text.strip()
            save_user_recognition(user_id, base_result_text)

            # ==================== [客觀動態計算的 RAG 評估邏輯] ====================
            similarity_score = "N/A"
            reasoning = "動態檢核中"
            metric_title = "•系統解析完整度"

            if "此藥丸非慢性病用藥，無法偵測" in base_result_text:
                similarity_score = "0%"
                reasoning = "該藥丸不在慢性病知識庫範圍內"
                metric_title = "與慢性病藥品匹配度"
            else:
                eval_prompt = f"""
你是一位極度嚴格且客觀的醫療 RAG 系統評估裁判。請依據以下內容【真實計算】其語意對應與忠實度：
【AI 辨識回答】：{base_result_text}

請依據實際準確度計算 0% 到 100% 之間的真實相似度分數，並給予具體理由。
請嚴格輸出 JSON 格式：{{"score": "真實算出的百分比%", "reason": "客觀評語"}}
"""
                try:
                    eval_response = ai_client.models.generate_content(
                        model='gemini-2.5-flash',
                        contents=[img, eval_prompt] if "📋 【藥袋辨識結果】" in base_result_text else eval_prompt,
                        config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.0)
                    )
                    eval_json = json.loads(eval_response.text)
                    similarity_score = eval_json.get("score", "N/A")
                    reasoning = eval_json.get("reason", "計算完成")
                    metric_title = "圖文忠實度與準確度" if "📋 【藥袋辨識結果】" in base_result_text else "資訊準確度與相似度"
                except Exception as eval_err:
                    print(f"⚠️ RAG 評分例外: {eval_err}")
                    similarity_score = "N/A"
                    reasoning = "動態計算未完成"

            result_text = f"{base_result_text}\n\n📊 【RAG 系統評估指標】\n━━━━━━━━━━━━━━\n• {metric_title}：{similarity_score}\n• 檢核說明：{reasoning}\n━━━━━━━━━━━━━━"

        except Exception as e:
            print(f"❌ 辨識過程錯誤: {e}")
            result_text = txts["fail_msg"]

        try:
            messages_to_send = [TextMessage(text=result_text)]
            if base_result_text and "此藥丸非慢性病用藥，無法偵測" not in base_result_text and "❌ 辨識失敗" not in result_text:
                if "📋 【藥袋辨識結果】" in base_result_text:
                    menu_template = TemplateMessage(
                        alt_text=txts["bag_menu_title"],
                        template=ButtonsTemplate(
                            title=txts["bag_menu_title"], text=txts["bag_menu_text"],
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
                            title=txts["pill_menu_title"], text=txts["pill_menu_text"],
                            actions=[
                                PostbackAction(label=txts["pill_btn_name"], data="view_pill=name"),
                                PostbackAction(label=txts["pill_btn_cat"], data="view_pill=category"),
                                PostbackAction(label=txts["pill_btn_usage"], data="view_pill=usage"),
                                PostbackAction(label=txts["pill_btn_dose"], data="view_pill=dose")
                            ]
                        )
                    )
                messages_to_send.append(menu_template)

            line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=messages_to_send))
        except Exception as reply_error:
            print(f"❌ [回傳失敗] ➔ {reply_error}")

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
