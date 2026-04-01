import os
import time
import json
import feedparser
import gspread
from datetime import datetime, timezone, timedelta
from google.oauth2.service_account import Credentials
from google import genai

# --- 設定 ---
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")

MAX_ARTICLES_PER_KEYWORD = 3
MAX_ARTICLES_PER_FEED = 3
API_CALL_DELAY = 5
GEMINI_MODEL = "gemini-2.0-flash-lite"

JST = timezone(timedelta(hours=9))

# --- シート名定義 ---
SHEET_NEWS = "ニュース"
SHEET_MARKETING = "マーケティング"
SHEET_SNS = "SNS"

# --- ニュースソース定義 ---

# [ニュースシート] Google News キーワード検索
GOOGLE_NEWS_KEYWORDS = [
    "ELECOM",
    "CIO ガジェット",
    "UGREEN",
    "SONY ガジェット",
    "iRobot",
    "PLAUDE AI",
]

# [マーケティングシート] マーケティング系 RSS フィード
MARKETING_RSS_FEEDS = {
    "MarkeZine": "https://markezine.jp/rss/20/index.rss",
    "AdWeek": "https://www.adweek.com/feed/",
    "日経ニュース": "https://news.google.com/rss/search?q=日本経済新聞+ビジネス&hl=ja&gl=JP&ceid=JP:ja",
}

# [SNSシート] PR TIMES プレスリリース
PRTIMES_FEEDS = {
    "ELECOM プレスリリース": "https://prtimes.jp/rss/companies/26881.xml",
    "CIO プレスリリース": "https://prtimes.jp/rss/companies/43212.xml",
    "UGREEN プレスリリース": "https://prtimes.jp/rss/companies/81071.xml",
}

# [SNSシート] X（Twitter）アカウント via Nitter
X_ACCOUNTS = {
    "CIO X": "cio_jp_official",
    "UGREEN X": "ugreenjapan",
    "ELECOM X": "elecom_pr",
}
NITTER_INSTANCES = [
    "https://nitter.net",
    "https://nitter.privacydev.net",
    "https://nitter.1d4.us",
]


# -------------------------------------------------------------------
# ユーティリティ
# -------------------------------------------------------------------

def get_nitter_rss_url(username: str) -> str | None:
    """複数のNitterインスタンスを試してRSS URLを返す"""
    for instance in NITTER_INSTANCES:
        url = f"{instance}/{username}/rss"
        try:
            feed = feedparser.parse(url)
            if feed.entries:
                return url
        except Exception:
            continue
    return None


def fetch_google_news(keyword: str) -> list[dict]:
    """Google NewsからキーワードでRSSを取得"""
    encoded = keyword.replace(" ", "+")
    url = f"https://news.google.com/rss/search?q={encoded}&hl=ja&gl=JP&ceid=JP:ja"
    try:
        feed = feedparser.parse(url)
        articles = []
        for entry in feed.entries[:MAX_ARTICLES_PER_KEYWORD]:
            articles.append({
                "title": entry.get("title", ""),
                "url": entry.get("link", ""),
                "source": f"Google News: {keyword}",
            })
        return articles
    except Exception as e:
        print(f"[WARN] Google News fetch failed for '{keyword}': {e}")
        return []


def fetch_rss_feed(name: str, url: str) -> list[dict]:
    """汎用RSSフィードを取得"""
    try:
        feed = feedparser.parse(url)
        articles = []
        for entry in feed.entries[:MAX_ARTICLES_PER_FEED]:
            articles.append({
                "title": entry.get("title", ""),
                "url": entry.get("link", ""),
                "source": name,
            })
        return articles
    except Exception as e:
        print(f"[WARN] RSS fetch failed for '{name}': {e}")
        return []


def analyze_with_gemini(client, title: str, url: str) -> dict | None:
    """Gemini APIで記事を分析してカテゴリ・サマリー・インサイトを返す"""
    prompt = f"""以下のニュース記事を分析してください。

タイトル: {title}
URL: {url}

以下のJSON形式で回答してください:
{{
  "category": "カテゴリ（例：製品情報、マーケティング、業界動向、プレスリリース、SNS等）",
  "summary": "記事の要約（100文字以内）",
  "insight": "ビジネスインサイト（50文字以内）"
}}

JSONのみを出力してください。"""

    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
        )
        text = response.text.strip()
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()
        return json.loads(text)
    except Exception as e:
        print(f"[WARN] Gemini analysis failed: {e}")
        return None


# -------------------------------------------------------------------
# スプレッドシート操作
# -------------------------------------------------------------------

HEADERS = ["日付", "カテゴリ", "タイトル", "サマリー", "インサイト", "URL", "ソース"]


def get_spreadsheet_obj(credentials_json: str, spreadsheet_id: str):
    """Google Sheetsスプレッドシートオブジェクトを返す"""
    creds_dict = json.loads(credentials_json)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)
    return gc.open_by_key(spreadsheet_id)


def get_or_create_sheet(spreadsheet, sheet_name: str):
    """指定名のシートを取得、なければ新規作成してヘッダーを追加"""
    try:
        sheet = spreadsheet.worksheet(sheet_name)
        print(f"  シート '{sheet_name}' を取得しました")
        existing = sheet.row_values(1)
        if existing != HEADERS:
            sheet.insert_row(HEADERS, 1)
            print(f"  シート '{sheet_name}' にヘッダーを追加しました")
    except gspread.exceptions.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(title=sheet_name, rows=1000, cols=10)
        sheet.append_row(HEADERS)
        print(f"  シート '{sheet_name}' を新規作成しました")
    return sheet


def write_articles_to_sheet(sheet, articles: list[dict], today: str, client):
    """記事リストをGemini分析してシートに書き込む"""
    rows_to_write = []
    success_count = 0
    error_count = 0

    for i, article in enumerate(articles):
        title = article["title"]
        url = article["url"]

        if not title or not url:
            continue

        print(f"  [{i+1}/{len(articles)}] 分析中: {title[:50]}...")

        analysis = analyze_with_gemini(client, title, url)
        time.sleep(API_CALL_DELAY)

        if analysis:
            rows_to_write.append([
                today,
                analysis.get("category", "一般ニュース"),
                title,
                analysis.get("summary", ""),
                analysis.get("insight", ""),
                url,
                article["source"],
            ])
            success_count += 1
        else:
            rows_to_write.append([
                today,
                "一般ニュース",
                title,
                "（AI分析スキップ）",
                "",
                url,
                article["source"],
            ])
            error_count += 1

    if rows_to_write:
        sheet.append_rows(rows_to_write)
        print(f"  ✅ {len(rows_to_write)}件書き込み（AI成功: {success_count}件 / スキップ: {error_count}件）")
    else:
        print(f"  ⚠️ 書き込むデータなし")

    return len(rows_to_write)


# -------------------------------------------------------------------
# メイン
# -------------------------------------------------------------------

def main():
    print("=== Morning News Collector 起動 ===")
    today = datetime.now(JST).strftime("%Y-%m-%d")
    print(f"対象日付: {today}")

    client = genai.Client(api_key=GEMINI_API_KEY)

    print("\n[スプレッドシート接続中...]")
    spreadsheet = get_spreadsheet_obj(GOOGLE_CREDENTIALS_JSON, SPREADSHEET_ID)

    sheet_news = get_or_create_sheet(spreadsheet, SHEET_NEWS)
    sheet_marketing = get_or_create_sheet(spreadsheet, SHEET_MARKETING)
    sheet_sns = get_or_create_sheet(spreadsheet, SHEET_SNS)

    total = 0

    print(f"\n== シート「{SHEET_NEWS}」: Google News ==")
    news_articles = []
    for keyword in GOOGLE_NEWS_KEYWORDS:
        articles = fetch_google_news(keyword)
        print(f"  {keyword}: {len(articles)}件取得")
        news_articles.extend(articles)
    print(f"  合計: {len(news_articles)}件 → Gemini分析 & 書き込み")
    total += write_articles_to_sheet(sheet_news, news_articles, today, client)

    print(f"\n== シート「{SHEET_MARKETING}」: マーケティング RSS ==")
    marketing_articles = []
    for name, url in MARKETING_RSS_FEEDS.items():
        articles = fetch_rss_feed(name, url)
        print(f"  {name}: {len(articles)}件取得")
        marketing_articles.extend(articles)
    print(f"  合計: {len(marketing_articles)}件 → Gemini分析 & 書き込み")
    total += write_articles_to_sheet(sheet_marketing, marketing_articles, today, client)

    print(f"\n== シート「{SHEET_SNS}」: PR TIMES + X ==")
    sns_articles = []

    for name, url in PRTIMES_FEEDS.items():
        articles = fetch_rss_feed(name, url)
        print(f"  {name}: {len(articles)}件取得")
        sns_articles.extend(articles)

    for name, username in X_ACCOUNTS.items():
        rss_url = get_nitter_rss_url(username)
        if rss_url:
            articles = fetch_rss_feed(name, rss_url)
            print(f"  {name} (@{username}): {len(articles)}件取得")
            sns_articles.extend(articles)
        else:
            print(f"  {name} (@{username}): Nitter接続失敗（スキップ）")

    print(f"  合計: {len(sns_articles)}件 → Gemini分析 & 書き込み")
    total += write_articles_to_sheet(sheet_sns, sns_articles, today, client)

    print(f"\n完了！ 合計 {total}件をスプレッドシートに書き込みました")


if __name__ == "__main__":
    main()
