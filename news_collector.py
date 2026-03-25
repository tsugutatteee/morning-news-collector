"""
Morning News Collector
毎朝ニュース・競合動向・マーケティング事例を収集し、
Gemini APIで分析してGoogleスプレッドシートに記録する。
"""
import os
import json
import time
import re
import feedparser
import google.generativeai as genai
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime

# ============================================================
# 設定
# ============================================================
GOOGLE_NEWS_KEYWORDS = [
    "ELECOM",
    "CIO ガジェット",
    "UGREEN",
    "SONY ガジェット",
    "iRobot",
    "PLAUDE AI",
]

MARKETING_RSS_FEEDS = {
    "MarkeZine": "https://markezine.jp/rss/20/index.rss",
    "AdWeek": "https://www.adweek.com/feed/",
    "日経ニュース": "https://news.google.com/rss/search?q=日本経済新聞+ビジネス&hl=ja&gl=JP&ceid=JP:ja",
}

GOOGLE_NEWS_RSS = (
    "https://news.google.com/rss/search"
    "?q={query}&hl=ja&gl=JP&ceid=JP:ja"
)

MAX_ARTICLES_PER_KEYWORD = 5
MAX_ARTICLES_PER_FEED = 5
API_CALL_DELAY = 0.5
GEMINI_MODEL = "gemini-1.5-flash"

# ============================================================
# RSSフェッチ
# ============================================================
def fetch_google_news(keyword: str) -> list:
    url = GOOGLE_NEWS_RSS.format(query=keyword.replace(" ", "+"))
    try:
        feed = feedparser.parse(url)
        results = []
        for entry in feed.entries[:MAX_ARTICLES_PER_KEYWORD]:
            results.append({
                "title": entry.get("title", ""),
                "description": _strip_html(entry.get("summary", "")),
                "url": entry.get("link", ""),
                "source": f"Google News / {keyword}",
            })
        return results
    except Exception as e:
        print(f"WARNING: Google News RSS取得エラー [{keyword}]: {e}")
        return []

def fetch_rss_feed(source_name: str, feed_url: str) -> list:
    try:
        feed = feedparser.parse(feed_url)
        results = []
        for entry in feed.entries[:MAX_ARTICLES_PER_FEED]:
            results.append({
                "title": entry.get("title", ""),
                "description": _strip_html(entry.get("summary", "")),
                "url": entry.get("link", ""),
                "source": source_name,
            })
        return results
    except Exception as e:
        print(f"WARNING: RSS取得エラー [{source_name}]: {e}")
        return []

def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:800]

# ============================================================
# Gemini API 分析
# ============================================================
def analyze_article(model, title: str, description: str):
    prompt = f"""以下の記事を分析し、指定のJSON形式のみで回答してください。

タイトル: {title}
内容: {description}

{{
  "summary": "3行以内で記事の要点を要約（日本語）",
  "category": "一般ニュース" または "競合動向" または "マーケ事例" のいずれか1つ,
  "insight": "この記事からマーケティング施策に活かせる示唆を1〜2文（日本語）"
}}

JSONのみ出力してください。説明文は不要です。"""

    try:
        response = model.generate_content(prompt)
        raw_text = response.text.strip()
        raw_text = re.sub(r"^```json\s*", "", raw_text)
        raw_text = re.sub(r"\s*```$", "", raw_text)
        json_match = re.search(r"\{.*\}", raw_text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group())
        else:
            print(f"WARNING: JSON解析失敗: {raw_text[:100]}")
            return None
    except json.JSONDecodeError as e:
        print(f"WARNING: JSONデコードエラー: {e}")
        return None
    except Exception as e:
        print(f"WARNING: Gemini APIエラー: {e}")
        return None

# ============================================================
# Google Sheets 書き込み
# ============================================================
SHEET_HEADERS = ["日付", "カテゴリ", "タイトル", "要約", "示唆", "URL", "ソース"]

def get_or_create_worksheet(gc, spreadsheet_id: str):
    sh = gc.open_by_key(spreadsheet_id)
    worksheet = sh.sheet1
    existing = worksheet.row_values(1)
    if existing != SHEET_HEADERS:
        worksheet.insert_row(SHEET_HEADERS, index=1)
        print("OK: ヘッダー行を追加しました")
    return worksheet

def get_existing_urls(worksheet) -> set:
    try:
        url_col = worksheet.col_values(6)
        return set(url_col[1:])
    except Exception:
        return set()

def write_rows_to_sheet(worksheet, rows: list) -> None:
    if not rows:
        return
    worksheet.append_rows(rows, value_input_option="USER_ENTERED")

# ============================================================
# メイン処理
# ============================================================
def main():
    print("\n" + "=" * 50)
    print(f"  Morning News Collector 起動 {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 50 + "\n")

    required_env = ["GEMINI_API_KEY", "GOOGLE_CREDENTIALS_JSON", "SPREADSHEET_ID"]
    for key in required_env:
        if not os.environ.get(key):
            raise EnvironmentError(f"環境変数 {key} が設定されていません")

    genai.configure(api_key=os.environ["GEMINI_API_KEY"])
    gemini_model = genai.GenerativeModel(GEMINI_MODEL)

    creds_info = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
    creds = Credentials.from_service_account_info(
        creds_info,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive.readonly",
        ],
    )
    gc = gspread.authorize(creds)
    spreadsheet_id = os.environ["SPREADSHEET_ID"]
    worksheet = get_or_create_worksheet(gc, spreadsheet_id)
    existing_urls = get_existing_urls(worksheet)
    print(f"既存記事URL数（重複除外用）: {len(existing_urls)}\n")

    articles = []
    print("Google News RSSを取得中...")
    for keyword in GOOGLE_NEWS_KEYWORDS:
        fetched = fetch_google_news(keyword)
        articles.extend(fetched)
        print(f"  [{keyword}] {len(fetched)}件取得")

    print("\nマーケ系RSSを取得中...")
    for source_name, feed_url in MARKETING_RSS_FEEDS.items():
        fetched = fetch_rss_feed(source_name, feed_url)
        articles.extend(fetched)
        print(f"  [{source_name}] {len(fetched)}件取得")

    print(f"\n合計 {len(articles)} 件の記事を取得しました\n")

    seen_urls = set(existing_urls)
    unique_articles = []
    for article in articles:
        if article["url"] and article["url"] not in seen_urls:
            seen_urls.add(article["url"])
            unique_articles.append(article)
    print(f"重複除去後: {len(unique_articles)} 件\n")

    today = datetime.now().strftime("%Y-%m-%d")
    rows_to_write = []
    success_count = 0
    error_count = 0

    print("Gemini APIで記事を分析中...")
    for i, article in enumerate(unique_articles, 1):
        title = article["title"]
        description = article["description"]
        if not title:
            continue
        print(f"  ({i}/{len(unique_articles)}) {title[:60]}...")
        analysis = analyze_article(gemini_model, title, description)
        if analysis:
            rows_to_write.append([
                today,
                analysis.get("category", "一般ニュース"),
                title,
                analysis.get("summary", ""),
                analysis.get("insight", ""),
                article["url"],
                article["source"],
            ])
            success_count += 1
        else:
            error_count += 1
        time.sleep(API_CALL_DELAY)

    print(f"\nGoogleスプレッドシートへ書き込み中... ({len(rows_to_write)}件)")
    write_rows_to_sheet(worksheet, rows_to_write)

    print("\n" + "=" * 50)
    print(f"  完了! 成功: {success_count}件 / エラー: {error_count}件")
    print("=" * 50 + "\n")

if __name__ == "__main__":
    main()
