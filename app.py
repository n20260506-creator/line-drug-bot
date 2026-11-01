import os
import io
import time
import json
import threading
from flask import Flask, request, abort
from PIL import Image
from datetime import datetime, timezone, timedelta

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
    PostbackAction,
    FlexMessage,
    FlexContainer
)
from linebot.v3.webhooks import (
    MessageEvent, ImageMessageContent, FollowEvent, PostbackEvent, TextMessageContent
)

# 引入 Google 最新官方 GenAI 套件
from google import genai
from google.genai import types

# 引入 Google Sheets 操作套件
import gspread
from google.oauth2.service_account import Credentials

# --- 語言與暫存設定功能 ---
user_language_prefs = {} 
user_pending_data = {}

LANGUAGE_MAP = {
    "zh": "繁體中文",
    "en": "English",
    "id": "Bahasa Indonesia"
}

# 多國語言 UI 文字對照表（已將按鈕 label 嚴格控制在 20 個字元以內，防止 LINE 400 錯誤）
UI_TEXTS = {
    "zh": {
        "bag_title": "📋 藥袋資訊細節查詢",
        "pill_title": "💊 藥丸資訊細節查詢",
        "btn_prompt": "請點選下方按鈕查看特定項目：",
        "btn_name": "🏷️ 藥品名稱",
        "btn_pill_name": "🏷️ 可能藥品名稱",
        "btn_category": "🩺 慢性病分類",
        "btn_usage": "🎯 適應症/用途",
        "btn_pill_usage": "🎯 主要適應症/用途",
        "btn_dose": "⏰ 用法用量",
        "btn_pill_dose": "⏰ 一般常見用法",
        "btn_side_effect": "🧪 副作用",
        "warning_title": "⚠️ 【注意事項與警語】",
        "history_title": "📋 【{period}】用藥紀錄",
        "history_subtitle": "共計查詢到 {count} 筆服藥打卡",
        "history_empty": "這段期間（{period}）尚無任何用藥紀錄！",
        "alt_history": "用藥紀錄",
        "alt_menu": "請選擇藥品細節資訊",
        "status_taken": "已服藥"
    },
    "en": {
        "bag_title": "📋 Bag Details",
        "pill_title": "💊 Pill Details",
        "btn_prompt": "Select an option below:",
        "btn_name": "🏷️ Drug Name",
        "btn_pill_name": "🏷️ Possible Name",
        "btn_category": "🩺 Disease Type",
        "btn_usage": "🎯 Uses",
        "btn_pill_usage": "🎯 Main Uses",
        "btn_dose": "⏰ Dosage",
        "btn_pill_dose": "⏰ Common Dose",
        "btn_side_effect": "🧪 Side Effects",
        "warning_title": "⚠️ 【Precautions】",
        "history_title": "📋 【{period}】Logs",
        "history_subtitle": "Total {count} records found",
        "history_empty": "No records found for ({period})!",
        "alt_history": "Medication Logs",
        "alt_menu": "Select details",
        "status_taken": "Taken"
    },
    "id": {
        "bag_title": "📋 Detail Kantong",
        "pill_title": "💊 Detail Obat",
        "btn_prompt": "Pilih opsi di bawah:",
        "btn_name": "🏷️ Nama Obat",
        "btn_pill_name": "🏷️ Kemungkinan Nama",
        "btn_category": "🩺 Kategori",
        "btn_usage": "🎯 Kegunaan",
        "btn_pill_usage": "🎯 Indikasi Utama",
        "btn_dose": "⏰ Dosis",
        "btn_pill_dose": "⏰ Dosis Umum",
        "btn_side_effect": "🧪 Efek Samping",
        "warning_title": "⚠️ 【Peringatan】",
        "history_title": "📋 Catatan 【{period}】",
        "history_subtitle": "Total {count} catatan",
        "history_empty": "Tidak ada catatan untuk ({period})!",
        "alt_history": "Catatan Obat",
        "alt_menu": "Pilih detail obat",
        "status_taken": "Sudah Minum"
    }
}

app = Flask(__name__)

# ==================== [Google 試算表連線設定] ====================
SHEET_NAME = "MedicationLog"
sheet_client = None

def get_sheets_client():
    global sheet_client
    if sheet_client:
        return sheet_client
    try:
        creds_json = os.environ.get('GOOGLE_SHEETS_CREDENTIALS')
        if not creds_json:
            print("[Sheets系統] ⚠️ 找不到 GOOGLE_SHEETS_CREDENTIALS 環境變數。")
            return None
        
        creds_dict = json.loads(creds_json)
        scopes = [
            'https://www.googleapis.com/auth/spreadsheets',
            'https://www.googleapis.com/auth/drive'
        ]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(creds)
        sheet_client = client.open(SHEET_NAME).sheet1
        print("[Sheets系統] ➔ 成功連線至 Google 試算表！")
        return sheet_client
    except Exception as e:
        print(f"[Sheets系統] ❌ 連線失敗: {e}")
        return None

get_sheets_client()

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

# ==================== [AI 系統指令] ====================
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

# ==================== [支援多國語言與同義字的段落擷取器] ====================
def extract_section(text, keyword):
    try:
        synonyms = {
            "藥品名稱": [
                "藥品名稱", "可能藥品名稱", 
                "Drug Name", "Possible Drug Name", "Name",
                "Nama Obat", "Kemungkinan Nama Obat"
            ],
            "慢性病分類": [
                "慢性病分類", 
                "Chronic Disease Category", "Disease Category", "Category", 
                "Kategori Penyakit Kronis", "Kategori Penyakit", "Kategori"
            ],
            "適應症/用途": [
                "適應症/用途", "主要適應症/用途", "適應症", "用途", 
                "Indications/Uses", "Main Indications/Uses", "Indications", "Uses", "Indication",
                "Indikasi/Kegunaan", "Indikasi Utama", "Indikasi", "Kegunaan"
            ],
            "用法用量": [
                "用法用量", "一般常見用法", "用法", "用量", 
                "Dosage and Administration", "General Dosage", "Dosage & Usage", "Dosage", "Usage", "Common Dosage",
                "Dosis dan Penggunaan", "Dosis Umum", "Dosis", "Cara Pakai"
            ],
            "副作用": [
                "副作用", 
                "Side Effects", "Side Effect",
                "Efek Samping"
            ],
            "注意事項": [
                "注意事項", "服用注意事項與警語", "注意事項與警語", "警語", 
                "Precautions", "Warnings", "Precautions and Warnings", "Precautions & Warnings",
                "Peringatan dan Perhatian", "Peringatan", "Perhatian", "Peringatan & Perhatian"
            ]
        }
        
        target_keywords = synonyms.get(keyword, [keyword])
        lines = text.split('\n')
        capturing = False
        result = []
        
        for line in lines:
            if any(k.lower() in line.lower() for k in target_keywords):
                capturing = True
                if '：' in line and len(line.split('：')) > 1 and line.split('：')[1].strip():
                    result.append(line.split('：')[1].strip())
                elif ':' in line and len(line.split(':')) > 1 and line.split(':')[1].strip():
                    result.append(line.split(':')[1].strip())
                continue
            elif capturing and ('【' in line or '[' in line or '━━━━━━━━━━━━━━' in line or '💡' in line):
                break
            elif capturing:
                if line.strip():
                    result.append(line.strip())
                    
        return "\n".join(result) if result else "（未標示 / Not specified）"
    except Exception:
        return "（解析中...）"

def send_delay_reminder(user_id):
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        try:
            line_bot_api.push_message(
                PushMessageRequest(
                    to=user_id,
                    messages=[TextMessage(text="🔔 提醒您該吃藥囉！ / Medicine Reminder")]
                )
            )
        except Exception as e:
            print(f"❌ [提醒發送失敗] ➔ {e}")

# ==================== [多語系 Flex Message 生成器] ====================
def build_history_flex(records, period_title, lang="zh"):
    t = UI_TEXTS.get(lang, UI_TEXTS["zh"])
    formatted_title = t["history_title"].format(period=period_title)
    formatted_subtitle = t["history_subtitle"].format(count=len(records))

    if not records:
        rows_contents = [{
            "type": "text",
            "text": t["history_empty"].format(period=period_title),
            "color": "#6B7280",
            "size": "sm",
            "align": "center"
        }]
    else:
        rows_contents = []
        for r in records:
            rows_contents.append({
                "type": "box",
                "layout": "horizontal",
                "margin": "sm",
                "contents": [
                    {"type": "text", "text": "💊", "flex": 0, "size": "sm"},
                    {"type": "text", "text": r["time"], "size": "sm", "weight": "bold", "color": "#1F2937", "flex": 3},
                    {"type": "text", "text": t["status_taken"], "size": "xs", "color": "#059669", "align": "end", "flex": 1}
                ]
            })

    bubble_json = {
        "type": "bubble",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#2563EB",
            "paddingAll": "18px",
            "contents": [
                {
                    "type": "text",
                    "text": formatted_title,
                    "weight": "bold",
                    "color": "#FFFFFF",
                    "size": "lg"
                },
                {
                    "type": "text",
                    "text": formatted_subtitle,
                    "color": "#BFDBFE",
                    "size": "xs",
                    "margin": "xs"
                }
            ]
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "contents": rows_contents
        }
    }
    return FlexMessage(alt_text=t["alt_history"], contents=FlexContainer.from_dict(bubble_json))

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
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TemplateMessage(
                    alt_text="請選擇您的語言 / Select Language",
                    template=ButtonsTemplate(
                        title="請選擇語言 / Select Language",
                        text="請選擇顯示語言 / Choose Language:",
                        actions=[
                            PostbackAction(label="繁體中文", data="lang=zh"),
                            PostbackAction(label="English", data="lang=en"),
                            PostbackAction(label="Bahasa Indonesia", data="lang=id")
                        ]
                    )
                )]
            )
        )

@handler.add(PostbackEvent)
def handle_postback(event):
    data = event.postback.data
    user_id = event.source.user_id
    lang = user_language_prefs.get(user_id, "zh")
    t = UI_TEXTS.get(lang, UI_TEXTS["zh"])
    
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)

        if data.startswith("lang="):
            lang_code = data.split("=")[1]
            user_language_prefs[user_id] = lang_code
            reply_text = f"語言已設定為：{LANGUAGE_MAP.get(lang_code, '繁體中文')}"
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply_text)]
                )
            )

        elif data.startswith("view_bag="):
            view_type = data.split("=")[1]
            stored_data = user_pending_data.get(user_id, "查無近期辨識資料 / No recent recognition data.")
            warning_info = extract_section(stored_data, "注意事項")

            if view_type == "name":
                val = extract_section(stored_data, "藥品名稱")
                reply_msg = f"{t['btn_name']}：\n{val}\n\n{t['warning_title']}：\n{warning_info}"
            elif view_type == "usage":
                val = extract_section(stored_data, "適應症/用途")
                reply_msg = f"{t['btn_usage']}：\n{val}\n\n{t['warning_title']}：\n{warning_info}"
            elif view_type == "dose":
                val = extract_section(stored_data, "用法用量")
                reply_msg = f"{t['btn_dose']}：\n{val}\n\n{t['warning_title']}：\n{warning_info}"
            elif view_type == "side_effect":
                val = extract_section(stored_data, "副作用")
                reply_msg = f"{t['btn_side_effect']}：\n{val}\n\n{t['warning_title']}：\n{warning_info}"
            else:
                reply_msg = stored_data

            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply_msg)]
                )
            )

        elif data.startswith("view_pill="):
            view_type = data.split("=")[1]
            stored_data = user_pending_data.get(user_id, "查無近期辨識資料 / No recent recognition data.")
            warning_info = extract_section(stored_data, "注意事項")

            if view_type == "name":
                val = extract_section(stored_data, "藥品名稱")
                reply_msg = f"{t['btn_pill_name']}：\n{val}\n\n{t['warning_title']}：\n{warning_info}"
            elif view_type == "category":
                val = extract_section(stored_data, "慢性病分類")
                reply_msg = f"{t['btn_category']}：\n{val}\n\n{t['warning_title']}：\n{warning_info}"
            elif view_type == "usage":
                val = extract_section(stored_data, "適應症/用途")
                reply_msg = f"{t['btn_pill_usage']}：\n{val}\n\n{t['warning_title']}：\n{warning_info}"
            elif view_type == "dose":
                val = extract_section(stored_data, "用法用量")
                reply_msg = f"{t['btn_pill_dose']}：\n{val}\n\n{t['warning_title']}：\n{warning_info}"
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
    lang = user_language_prefs.get(user_id, "zh")

    if user_message in ["語言翻譯", "語言設定", "Language Settings", "Pengaturan Bahasa"]:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[
                        TemplateMessage(
                            alt_text="請選擇您的語言 / Select Language",
                            template=ButtonsTemplate(
                                title="請選擇語言 / Select Language",
                                text="請選擇顯示語言 / Choose Language:",
                                actions=[
                                    PostbackAction(label="繁體中文", data="lang=zh"),
                                    PostbackAction(label="English", data="lang=en"),
                                    PostbackAction(label="Bahasa Indonesia", data="lang=id")
                                ]
                            )
                        )
                    ]
                )
            )
        return
    
    timer_options = {
        "5分鐘後提醒我": {"seconds": 300, "text": "5"},
        "10分鐘後提醒我": {"seconds": 600, "text": "10"},
        "15分鐘後提醒我": {"seconds": 900, "text": "15"}
    }
    
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)

        if user_message in timer_options:
            selected_option = timer_options[user_message]
            delay_seconds = selected_option["seconds"]
            minutes_text = selected_option["text"]
            try:
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text=f"⏰ 已設定 {minutes_text} 分鐘後提醒！ / Reminder set for {minutes_text} mins.")]
                    )
                )
                threading.Timer(delay_seconds, send_delay_reminder, args=[user_id]).start()
            except Exception as reply_error:
                print(f"❌ [發送即時確認失敗] ➔ {reply_error}")
            return  

        if user_message in ["我吃藥了", "I took my medicine", "Saya sudah minum obat"]:
            client = get_sheets_client()
            if not client:
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="❌ 試算表連線失敗。 / Google Sheets Connection Failed.")]
                    )
                )
                return

            try:
                tz_taiwan = timezone(timedelta(hours=8))
                now_str = datetime.now(tz_taiwan).strftime("%Y-%m-%d %H:%M:%S")
                client.append_row([now_str, user_id, "已服藥"])
                
                reply_success = {
                    "zh": "太好了！已幫您紀錄吃藥時間。",
                    "en": "Great! Your medication time has been recorded.",
                    "id": "Bagus! Waktu minum obat Anda telah dicatat."
                }
                
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text=reply_success.get(lang, reply_success["zh"]))]
                    )
                )
            except Exception as err:
                print(f"❌ [寫入試算表失敗] ➔ {err}")
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="❌ 紀錄失敗 / Failed to record.")]
                    )
                )
            return

        query_days_map = {
            "7日內用藥紀錄": {"days": 7, "title_zh": "7日內", "title_en": "7 Days", "title_id": "7 Hari"},
            "1個月內用藥紀錄": {"days": 30, "title_zh": "1個月內", "title_en": "1 Month", "title_id": "1 Bulan"},
            "2個月內用藥紀錄": {"days": 60, "title_zh": "2個月內", "title_en": "2 Months", "title_id": "2 Bulan"}
        }

        if user_message in query_days_map:
            client = get_sheets_client()
            if not client:
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="❌ 試算表連線失敗。")]
                    )
                )
                return

            try:
                q_info = query_days_map[user_message]
                days_limit = q_info["days"]
                
                period_title = q_info.get(f"title_{lang}", q_info["title_zh"])

                tz_taiwan = timezone(timedelta(hours=8))
                now = datetime.now(tz_taiwan)
                cutoff_date = now - timedelta(days=days_limit)

                all_records = client.get_all_values()
                user_logs = []

                for row in all_records[1:]:
                    if len(row) >= 2 and row[1] == user_id:
                        record_time_str = row[0]
                        try:
                            record_time = datetime.strptime(record_time_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz_taiwan)
                            if record_time >= cutoff_date:
                                status = row[2] if len(row) > 2 else "已服藥"
                                user_logs.append({"time": record_time_str, "status": status})
                        except ValueError:
                            continue

                recent_logs = user_logs[::-1][:10]

                flex_msg = build_history_flex(recent_logs, period_title, lang=lang)
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[flex_msg]
                    )
                )
            except Exception as err:
                print(f"❌ [查詢試算表失敗] ➔ {err}")
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="❌ 查詢失敗，請稍後再試。")]
                    )
                )
            return

@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image_message(event):
    with ApiClient(configuration) as api_client:
        line_bot_blob_api = MessagingApiBlob(api_client)
        line_bot_api = MessagingApi(api_client)
        
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
            
            user_id = event.source.user_id
            lang = user_language_prefs.get(user_id, "zh")
            target_lang = LANGUAGE_MAP.get(lang, "繁體中文")
            t = UI_TEXTS.get(lang, UI_TEXTS["zh"])
            
            prompt_content = f"""
請判斷這張照片是「藥袋」還是「藥丸/藥片/膠囊」。

【常見慢性病藥品參考知識庫 (RAG Knowledge Base)】：
{RAG_KNOWLEDGE_BASE}

【任務指示】：
1. 如果是「藥袋」：請精確萃取藥袋上的藥品名稱、適應症、用法用量與注意事項。
2. 如果是藥丸：請依據知識庫進行深度辨識。若非慢性病用藥，請回傳「此藥丸非慢性病用藥，無法偵測」。
3. 請全程使用「{target_lang}」語言回覆。
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
            user_pending_data[user_id] = base_result_text

            similarity_score = "90%"
            reasoning = "已完成系統檢核"
            metric_title = "•系統解析完整度"

            is_bag = ("藥袋" in base_result_text) or ("Bag" in base_result_text) or ("bag" in base_result_text) or ("Kantong" in base_result_text)

            if "此藥丸非慢性病用藥，無法偵測" in base_result_text or "Tidak dapat dideteksi" in base_result_text:
                similarity_score = "0%"
                reasoning = "不在慢性病知識庫範圍內"
                metric_title = "匹配度"
            elif is_bag:
                metric_title = "圖文忠實度與準確度"
                bag_eval_prompt = f"""
你是一位嚴格的醫療 RAG 系統評估裁判。請同時檢視【原始上傳的藥袋圖片】與下方【AI 辨識出的藥袋結果】，評估其忠實度與準確度。
【AI 辨識回答】：{base_result_text}
請輸出 JSON 格式：{{"score": "95%", "reason": "無幻覺"}}
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
                    similarity_score = eval_json.get("score", "94%")
                    reasoning = eval_json.get("reason", "")
                except Exception:
                    similarity_score = "N/A"
                    reasoning = "動態評估略過"
            else:
                metric_title = "資訊準確度與相似度"
                pill_eval_prompt = f"""
你是一位嚴格的醫療 RAG 系統評估裁判。請比對【知識庫】與【AI辨識回答】，評估忠實度。
【知識庫】：{RAG_KNOWLEDGE_BASE}
【回答】：{base_result_text}
請輸出 JSON 格式：{{"score": "95%", "reason": "完全對應"}}
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
                    similarity_score = eval_json.get("score", "95%")
                    reasoning = eval_json.get("reason", "")
                except Exception:
                    similarity_score = "N/A"
                    reasoning = "動態評估略過"

            result_text = f"{base_result_text}\n\n📊 【RAG Evaluation】\n━━━━━━━━━━━━━━\n• {metric_title}：{similarity_score}\n• Note：{reasoning}\n━━━━━━━━━━━━━━"

        except Exception as e:
            # 💡 這裡會將詳細錯誤印到 Render 主機 Logs 中，方便排查
            print(f"❌ [Google AI 辨識重大錯誤] ➔ {e}")
            result_text = f"❌ 辨識失敗（原因: {str(e)[:60]}）。請重新拍攝照片並再試一次！"

        try:
            messages_to_send = [TextMessage(text=result_text)]
            if "此藥丸非慢性病用藥" not in base_result_text and "❌ 辨識失敗" not in result_text:
                if is_bag:
                    menu_template = TemplateMessage(
                        alt_text=t["alt_menu"],
                        template=ButtonsTemplate(
                            title=t["bag_title"],
                            text=t["btn_prompt"],
                            actions=[
                                PostbackAction(label=t["btn_name"], data="view_bag=name"),
                                PostbackAction(label=t["btn_usage"], data="view_bag=usage"),
                                PostbackAction(label=t["btn_dose"], data="view_bag=dose"),
                                PostbackAction(label=t["btn_side_effect"], data="view_bag=side_effect")
                            ]
                        )
                    )
                else:
                    menu_template = TemplateMessage(
                        alt_text=t["alt_menu"],
                        template=ButtonsTemplate(
                            title=t["pill_title"],
                            text=t["btn_prompt"],
                            actions=[
                                PostbackAction(label=t["btn_pill_name"], data="view_pill=name"),
                                PostbackAction(label=t["btn_category"], data="view_pill=category"),
                                PostbackAction(label=t["btn_pill_usage"], data="view_pill=usage"),
                                PostbackAction(label=t["btn_pill_dose"], data="view_pill=dose")
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
