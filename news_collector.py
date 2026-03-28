"""
Morning News Collector
æ¯æãã¥ã¼ã¹ã»ç«¶åååã»ãã¼ã±ãã£ã³ã°äºä¾ãåéãã
Gemini APIã§åæãã¦Googleã¹ãã¬ããã·ã¼ãã«è¨é²ããã
"""
import os
import json
import time
import re
import feedparser
from google import genai
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime

# ============================================================
# è¨­å®
# ============================================================
GOOGLE_NEWS_KEYWORDS = [
    "ELECOM",
    "CIO ã¬ã¸ã§ãã",
    "UGREEN",
    "SONY ã¬ã¸ã§ãã",
    "iRobot",
    "PLAUDE AI",
]

MARKETING_RSS_FEEDS = {
    "MarkeZine": "https://markezine.jp/rss/20/index.rss",
    "AdWeek": "https://www.adweek.com/feed/",
    "æ¥çµãã¥ã¼ã¹": "https://news.google.com/rss/search?q=æ¥æ¬çµæ¸æ°è+ãã¸ãã¹&hl=ja&gl=JP&ceid=JP:ja",
}

GOOGLE_NEWS_RSS = (
    "https://news.google.com/rss/search"
    "?q={query}&hl=ja&gl=JP&ceid=JP:ja"
)

MAX_ARTICLES_PER_KEYWORD = 3
MAX_ARTICLES_PER_FEED = 3
API_CALL_DELAY = 5
GEMINI_MODEL = "gemini-2.0-flash-lite"


# ============================================================
# RSSãã§ãã
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
        print(f"WARNING: Google News RSSåå¾ã¨ã©ã¼ [{keyword}]: {e}")
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
        print(f"WARNING: RSSåå¾ã¨ã©ã¼ [{source_name}]: {e}")
        return []


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:800]


# ============================================================
# Gemini API åæ
# ============================================================
def analyze_article(client, title: str, description: str):
    prompt = f"""ä»¥ä¸ã®è¨äºãåæããæå®ã®JSONå½¢å¼ã®ã¿ã§åç­ãã¦ãã ããã

ã¿ã¤ãã«: {title}
åå®¹: {description}

{{
  "summary": "3è¡ä»¥åã§è¨äºã®è¦ç¹ãè¦ç´ï¼æ¥æ¬èªï¼",
  "category": "ä¸è¬ãã¥ã¼ã¹" ã¾ãã¯ "ç«¶ååå" ã¾ãã¯ "ãã¼ã±äºä¾" ã®ãããã1ã¤,
  "insight": "ãã®è¨äºãããã¼ã±ãã£ã³ã°æ½ç­ã«æ´»ãããç¤ºåã1ã2æï¼æ¥æ¬èªï¼"
}}

JSONã®ã¿åºåãã¦ãã ãããèª¬ææã¯ä¸è¦ã§ãã"""

    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
        )
        raw_text = response.text.strip()
        raw_text = re.sub(r"^```json\s*", "", raw_text)
        raw_text = re.sub(r"\s*```$", "", raw_text)
        json_match = re.search(r"\{.*\}", raw_text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group())
        else:
            print(f"WARNING: JSONè§£æå¤±æ: {raw_text[:100]}")
            return None
    except json.JSONDecodeError as e:
        print(f"WARNING: JSONãã³ã¼ãã¨ã©ã¼: {e}")
        return None
    except Exception as e:
        print(f"WARNING: Gemini APIã¨ã©ã¼: {e}")
        return None


# ============================================================
# Google Sheets æ¸ãè¾¼ã¿
# ============================================================
SHEET_HEADERS = ["æ¥ä»", "ã«ãã´ãª", "ã¿ã¤ãã«", "è¦ç´", "ç¤ºå", "URL", "ã½ã¼ã¹"]


def get_or_create_worksheet(gc, spreadsheet_id: str):
    sh = gc.open_by_key(spreadsheet_id)
    worksheet = sh.sheet1
    existing = worksheet.row_values(1)
    if existing != SHEET_HEADERS:
        worksheet.insert_row(SHEET_HEADERS, index=1)
        print("OK: ãããã¼è¡ãè¿½å ãã¾ãã")
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
# ã¡ã¤ã³å¦ç
# ============================================================
def main():
    print("\n" + "=" * 50)
    print(f" Morning News Collector èµ·å {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 50 + "\n")

    required_env = ["GEMINI_API_KEY", "GOOGLE_CREDENTIALS_JSON", "SPREADSHEET_ID"]
    for key in required_env:
        if not os.environ.get(key):
            raise EnvironmentError(f"ç°å¢å¤æ° {key} ãè¨­å®ããã¦ãã¾ãã")

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

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
    print(f"æ¢å­è¨äºURLæ°ï¼éè¤é¤å¤ç¨ï¼: {len(existing_urls)}\n")

    articles = []

    print("Google News RSSãåå¾ä¸­...")
    for keyword in GOOGLE_NEWS_KEYWORDS:
        fetched = fetch_google_news(keyword)
        articles.extend(fetched)
        print(f"  [{keyword}] {len(fetched)}ä»¶åå¾")

    print("\nãã¼ã±ç³»RSSãåå¾ä¸­...")
    for source_name, feed_url in MARKETING_RSS_FEEDS.items():
        fetched = fetch_rss_feed(source_name, feed_url)
        articles.extend(fetched)
        print(f"  [{source_name}] {len(fetched)}ä»¶åå¾")

    print(f"\nåè¨ {len(articles)} ä»¶ã®è¨äºãåå¾ãã¾ãã\n")

    seen_urls = set(existing_urls)
    unique_articles = []
    for article in articles:
        if article["url"] and article["url"] not in seen_urls:
            seen_urls.add(article["url"])
            unique_articles.append(article)

    print(f"éè¤é¤å»å¾: {len(unique_articles)} ä»¶\n")

    today = datetime.now().strftime("%Y-%m-%d")
    rows_to_write = []
    success_count = 0
    error_count = 0

    print("Gemini APIã§è¨äºãåæä¸­...")
    for i, article in enumerate(unique_articles, 1):
        title = article["title"]
        description = article["description"]
        if not title:
            continue

        print(f"  ({i}/{len(unique_articles)}) {title[:60]}...")
        analysis = analyze_article(client, title, description)
        if analysis:
            rows_to_write.append([
                today,
                analysis.get("category", "ä¸è¬ãã¥ã¼ã¹"),
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

    print(f"\nGoogleã¹ãã¬ããã·ã¼ãã¸æ¸ãè¾¼ã¿ä¸­... ({len(rows_to_write)}ä»¶)")
    write_rows_to_sheet(worksheet, rows_to_write)

    print("\n" + "=" * 50)
    print(f" å®äº! æå: {success_count}ä»¶ / ã¨ã©ã¼: {error_count}ä»¶")
    print("=" * 50 + "\n")


if __name__ == "__main__":
    main()
