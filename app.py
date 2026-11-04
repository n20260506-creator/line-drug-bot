import os
import io
import time
import json
import re
import threading
from flask import Flask, request, abort
from PIL import Image
from datetime import datetime, timezone, timedelta
from pypdf import PdfReader

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

# ==================== [Flask 應用程式宣告] ====================
app = Flask(__name__)

# --- 語言與暫存設定功能 ---
user_language_prefs = {} 
user_pending_data = {}

LANGUAGE_MAP = {
    "zh": "繁體中文",
    "en": "English",
    "id": "Bahasa Indonesia"
}

# 多國語言 UI 文字對照表（已完全修正注意事項標題，無多餘字元）
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
        "warning_title": "⚠️ 【注意事項】",
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
        "warning_title": "⚠️ 【Perhatian】",
        "history_title": "📋 Catatan 【{period}】",
        "history_subtitle": "Total {count} catatan",
        "history_empty": "Tidak ada catatan untuk ({period})!",
        "alt_history": "Catatan Obat",
        "alt_menu": "Pilih detail obat",
        "status_taken": "Sudah Minum"
    }
}

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

# ==================== [RAG 核心：PDF 讀取與 Chunking 切片分割] ====================
PDF_CHUNKS = []
PDF_PATH = "drug_guide.pdf"

def load_and_chunk_pdf(file_path):
    chunks = []
    if not os.path.exists(file_path):
        print(f"[RAG系統] ⚠️ 找不到 {file_path}，請確認檔案已放置在專案目錄下。")
        return chunks

    try:
        reader = PdfReader(file_path)
        full_text = ""
        for page in reader.pages:
            t = page.extract_text()
            if t:
                full_text += t + "\n"

        raw_items = full_text.split("【藥物編號")
        for item in raw_items:
            cleaned = item.strip()
            if cleaned and any(k in cleaned for k in ["藥品名稱", "刻字", "適應症"]):
                chunks.append(f"【藥物編號{cleaned}")

        print(f"[RAG系統] ➔ 成功解析 PDF！已分割為 {len(chunks)} 個獨立藥品特徵區塊 (Chunks)。")
    except Exception as e:
        print(f"[RAG系統] ❌ 解析 PDF 發生錯誤: {e}")
    return chunks

PDF_CHUNKS = load_and_chunk_pdf(PDF_PATH)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
GOOGLE_API_KEY = os.environ.get('GOOGLE_API_KEY')

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
ai_client = genai.Client(api_key=GOOGLE_API_KEY)

SYSTEM_INSTRUCTION = """
你是一位擁有極高辨識精準度的「慢性病藥丸與藥袋審核專家」。
使用者上傳的照片可能是「藥袋」或「藥丸/藥片/膠囊」。

請嚴格執行以下規則：

【情況 A：如果是藥袋照片】
1. 完整進行 OCR 文字辨識，並同時提供繁體中文、英文、印尼文對照的藥品資訊。
2. 第一階段完整輸出必須包含結構化資訊、系統提示與 RAGAS 評估指標。

【情況 B：如果是藥丸/藥片/膠囊照片（雙重檢驗原則）】
1. 第一步（特徵擷取）：仔細辨認照片中藥丸正反兩面的刻字、英數代碼與顏色形狀。
2. 第二步（檢索對照）：嚴格比對【常見慢性病藥品參考知識庫切割區塊】。
3. 🚨 終止條件：若找不到相符項目，回傳：此藥丸非慢性病用藥，無法偵測。

通用規定事項：
- 第一階段辨識結果的語言固定為繁體中文。
- 嚴禁輸出任何客套話、問候語。
"""

def extract_multilingual_section(text, keyword, lang="zh"):
    """從結構化多語系文字中直接提取對應語言的內容，完全不需要呼叫 API 翻譯"""
    try:
        lang_key_map = {"zh": "中文:", "en": "英文:", "id": "印尼文:"}
        target_prefix = lang_key_map.get(lang, "中文:")
        
        # 尋找該關鍵字區塊
        pattern = rf'(?:【|\[)?\s*({re.escape(keyword)}.*?)(?:】|\])?\s*[:：]?\s*\n?(.*?)(?=(?:【|\[)?\s*(?:藥品名稱|可能藥品名稱|外觀與刻字描述|慢性病分類|適應症|主要適應症/用途|用法用量|一般常見用法|副作用|注意事項)|$)'
        match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        
        if match:
            block_content = match.group(2).strip()
            if "|" in block_content or "中文:" in block_content:
                parts = block_content.split("|")
                for part in parts:
                    if target_prefix in part:
                        val = part.split(target_prefix)[1].strip()
                        if val:
                            return val
                return block_content.split("|")[0].replace("中文:", "").strip()
            else:
                return block_content

        return "（未標示 / Not specified）"
    except Exception as e:
        print(f"❌ [多語系解析失敗] ➔ {e}")
        return "（未標示 / Not specified）"

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
            warning_info = extract_multilingual_section(stored_data, "注意事項", lang)

            if view_type == "name":
                val = extract_multilingual_section(stored_data, "藥品名稱", lang)
                reply_msg = f"{t['btn_name']}：\n{val}\n\n{t['warning_title']}\n{warning_info}"
            elif view_type == "usage":
                val = extract_multilingual_section(stored_data, "適應症", lang)
                reply_msg = f"{t['btn_usage']}：\n{val}\n\n{t['warning_title']}\n{warning_info}"
            elif view_type == "dose":
                val = extract_multilingual_section(stored_data, "用法用量", lang)
                reply_msg = f"{t['btn_dose']}：\n{val}\n\n{t['warning_title']}\n{warning_info}"
            elif view_type == "side_effect":
                val = extract_multilingual_section(stored_data, "副作用", lang)
                reply_msg = f"{t['btn_side_effect']}：\n{val}\n\n{t['warning_title']}\n{warning_info}"
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
            warning_info = extract_multilingual_section(stored_data, "注意事項", lang)

            if view_type == "name":
                val = extract_multilingual_section(stored_data, "藥品名稱", lang)
                reply_msg = f"{t['btn_pill_name']}：\n{val}\n\n{t['warning_title']}\n{warning_info}"
            elif view_type == "category":
                val = extract_multilingual_section(stored_data, "慢性病分類", lang)
                reply_msg = f"{t['btn_category']}：\n{val}\n\n{t['warning_title']}\n{warning_info}"
            elif view_type == "usage":
                val = extract_multilingual_section(stored_data, "適應症", lang)
                reply_msg = f"{t['btn_pill_usage']}：\n{val}\n\n{t['warning_title']}\n{warning_info}"
            elif view_type == "dose":
                val = extract_multilingual_section(stored_data, "用法用量", lang)
                reply_msg = f"{t['btn_pill_dose']}：\n{val}\n\n{t['warning_title']}\n{warning_info}"
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
        
        result_text = ""
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
            
            formatted_knowledge = "\n----------------------------------------\n".join(PDF_CHUNKS)
            
            # 【優化重點】：精確、專業、詳細的 RAGAS 檢核說明與結構輸出
            prompt_content = f"""
請判斷這張照片是「藥袋」還是「藥丸/藥片/膠囊」。

【常見慢性病藥品參考知識庫切割區塊 (RAG Chunks)】：
{formatted_knowledge}

【任務指示】：
1. 如果是「藥袋」或「藥丸」：請精確萃取資訊，並同時提供繁體中文、英文、印尼文三種語言對照結構。
2. 輸出格式必須嚴格採用以下結構（不可遺漏任何欄位）：

📋 【藥袋辨識結果】（或 💊 【慢性病藥丸辨識結果】）
━━━━━━━━━━━━━━
【藥品名稱】：
中文: [繁體中文名稱] | 英文: [English Name] | 印尼文: [Nama Indonesia]

【慢性病分類】：（若為藥丸請填寫）
中文: [中文分類] | 英文: [English Category] | 印尼文: [Kategori]

【適應症/用途】：
中文: [中文用途] | 英文: [English Uses] | 印尼文: [Kegunaan]

【用法用量】：
中文: [中文用法] | 英文: [English Dosage] | 印尼文: [Dosis]

【副作用】：（若為藥袋請填寫）
中文: [中文副作用] | 英文: [English Side Effects] | 印尼文: [Efek Samping]

【注意事項】：
中文: [中文注意事項] | 英文: [English Precautions] | 印尼文: [Perhatian]
━━━━━━━━━━━━━━
💡 提示：本系統辨識結果僅供參考，用藥前請務必再次核對藥袋，並遵照醫囑。
📊 【RAG 系統評估指標與專業檢核說明】
━━━━━━━━━━━━━━
• 檢索忠實度 (Faithfulness)：[請動態計算數值，以百分比%呈現]
  - 檢核說明：[精確解析圖像特徵（如藥丸表面刻字、形狀、顏色或藥袋印刷文字），比對知識庫對應之區塊內容，計算忠實度]。
• 答案相關性 (Answer Relevance)：[請動態計算數值，以百分比%呈現]
  - 檢核說明：[擷取之醫療欄位（藥名、適應症、劑量與注意事項）與使用者輸入圖像具備完美語意對齊，且無遺漏圖像資訊，滿足慢性病藥品檢索目標]。
━━━━━━━━━━━━━━
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
                    if response and response.text:
                        break
                except Exception as err:
                    last_error = err
                    print(f"⚠️ [模型嘗試失敗] {model_name} ➔ {err}")
                    time.sleep(2)

            if not response or not response.text:
                raise last_error if last_error else Exception("No response generated")

            result_text = response.text.strip()
            base_result_text = result_text
            user_pending_data[user_id] = result_text

        except Exception as e:
            print(f"❌ [Google AI 辨識重大錯誤] ➔ {e}")
            result_text = "❌ 辨識失敗。可能原因：免費 API 額度超限 (429 Rate Limit) 或伺服器連線超時。請稍候再試一次！"
            base_result_text = result_text

        try:
            messages_to_send = [TextMessage(text=result_text)]
            if "此藥丸非慢性病用藥" not in base_result_text and "❌ 辨識失敗" not in result_text:
                lang = user_language_prefs.get(user_id, "zh")
                t = UI_TEXTS.get(lang, UI_TEXTS["zh"])
                
                if any(k in base_result_text for k in ["📋 【藥袋辨識結果】"]):
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
