"""
Morning News Collector
毎朝ニュース・競合動向・マーケティング事例を収集し、
Claude APIで分析してGoogleスプレッドシートに記録する。
"""

import os
import json
import time
import re
import feedparser
import anthropic
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime


# ============================================================
# 設定
# ============================================================

# Google Newsで検索するキーワード（1キーワードにつき最新5件取得）
GOOGLE_NEWS_KEYWORDS = [
    "ELECOM",
    "CIO ガジェット",
    "UGREEN",
    "SONY ガジェット",
    "iRobot",
    "PLAUDE AI",
]

# マーケ系RSSフィード（ソース名: URL）
MARKETING_RSS_FEEDS = {
    "MarkeZine":  "https://markezine.jp/rss/20/index.rss",
    "AdWeek":     "https://www.adweek.com/feed/",
    "日経ニュース":  "https://news.google.com/rss/search?q=日本経済新聞+ビジネス&hl=ja&gl=JP&ceid=JP:ja",
}

# Google NewsのRSS URLテンプレート
GOOGLE_NEWS_RSS = (
    "https://news.google.com/rss/search"
    "?q={query}&hl=ja&gl=JP&ceid=JP:ja"
)

# 1キーワードあたりの最大取得記事数
MAX_ARTICLES_PER_KEYWORD = 5

# マーケRSSフィード1件あたりの最大取得記事数
MAX_ARTICLES_PER_FEED = 5

# Claude APIコール間のウェイト（秒）※レートリミット対策
API_CALL_DELAY = 1.0

# 使用するClaudeモデル（コスト効率重視）
CLAUDE_MODEL = "claude-haiku-4-5-20251001"


# ============================================================
# RSSフェッチ
# ============================================================

def fetch_google_news(keyword: str) -> list:
    """Google NewsのRSSからキーワードで記事を取得する"""
    url = GOOGLE_NEWS_RSS.format(query=keyword.replace(" ", "+"))
    try:
        feed = feedparser.parse(url)
        results = []
        for entry in feed.entries[:MAX_ARTICLES_PER_KEYWORD]:
            results.append({
                "title":       entry.get("title", ""),
                "description": _strip_html(entry.get("summary", "")),
                "url":         entry.get("link", ""),
                "source":      f"Google News / {keyword}",
            })
        return results
    except Exception as e:
        print(f"WARNING: Google News RSS取得エラー [{keyword}]: {e}")
        return []


def fetch_rss_feed(source_name: str, feed_url: str) -> list:
    """指定RSSフィードから記事を取得する"""
    try:
        feed = feedparser.parse(feed_url)
        results = []
        for entry in feed.entries[:MAX_ARTICLES_PER_FEED]:
            results.append({
                "title":       entry.get("title", ""),
                "description": _strip_html(entry.get("summary", "")),
                "url":         entry.get("link", ""),
                "source":      source_name,
            })
        return results
    except Exception as e:
        print(f"WARNING: RSS取得エラー [{source_name}]: {e}")
        return []


def _strip_html(text: str) -> str:
    """HTMLタグを除去し、テキストを最大800文字に制限する"""
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:800]


# ============================================================
# Claude API 分析
# ============================================================

def analyze_article(client, title: str, description: str):
    """
    Claude APIで記事を分析する。
    - 3行以内の要約
    - カテゴリ分類（一般ニュース / 競合動向 / マーケ事例）
    - マーケティング施策への示唆（1〜2文）
    """
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
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        raw_text = response.content[0].text.strip()

        # JSON部分を抽出
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
        print(f"WARNING: Claude APIエラー: {e}")
        return None


# ============================================================
# Google Sheets 書き込み
# ============================================================

SHEET_HEADERS = ["日付", "カテゴリ", "タイトル", "要約", "示唆", "URL", "ソース"]


def get_or_create_worksheet(gc, spreadsheet_id: str):
    """スプレッドシートを開き、ヘッダー行がなければ追加する"""
    sh = gc.open_by_key(spreadsheet_id)
    worksheet = sh.sheet1

    # ヘッダー行が未設定なら追加
    existing = worksheet.row_values(1)
    if existing != SHEET_HEADERS:
        worksheet.insert_row(SHEET_HEADERS, index=1)
        print("OK: ヘッダー行を追加しました")

    return worksheet


def get_existing_urls(worksheet) -> set:
    """既存の重複チェック用にURL列（6列目）を取得する"""
    try:
        url_col = worksheet.col_values(6)  # F列 = URL
        return set(url_col[1:])  # ヘッダー行を除く
    except Exception:
        return set()


def write_rows_to_sheet(worksheet, rows: list) -> None:
    """行をスプレッドシートに一括追加する"""
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

    # --- 環境変数チェック ---
    required_env = ["ANTHROPIC_API_KEY", "GOOGLE_CREDENTIALS_JSON", "SPREADSHEET_ID"]
    for key in required_env:
        if not os.environ.get(key):
            raise EnvironmentError(f"環境変数 {key} が設定されていません")

    # --- クライアント初期化 ---
    claude_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

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

    # --- 記事収集 ---
    articles = []

    # Google News（キーワード別）
    print("Google News RSSを取得中...")
    for keyword in GOOGLE_NEWS_KEYWORDS:
        fetched = fetch_google_news(keyword)
        articles.extend(fetched)
        print(f"  [{keyword}] {len(fetched)}件取得")

    # マーケ系RSSフィード
    print("\nマーケ系RSSを取得中...")
    for source_name, feed_url in MARKETING_RSS_FEEDS.items():
        fetched = fetch_rss_feed(source_name, feed_url)
        articles.extend(fetched)
        print(f"  [{source_name}] {len(fetched)}件取得")

    print(f"\n合計 {len(articles)} 件の記事を取得しました\n")

    # --- 重複除去 ---
    seen_urls = set(existing_urls)
    unique_articles = []
    for article in articles:
        if article["url"] and article["url"] not in seen_urls:
            seen_urls.add(article["url"])
            unique_articles.append(article)

    print(f"重複除去後: {len(unique_articles)} 件\n")

    # --- Claude APIで分析 ---
    today = datetime.now().strftime("%Y-%m-%d")
    rows_to_write = []
    success_count = 0
    error_count = 0

    print("Claude APIで記事を分析中...")
    for i, article in enumerate(unique_articles, 1):
        title = article["title"]
        description = article["description"]

        if not title:
            continue

        print(f"  ({i}/{len(unique_articles)}) {title[:60]}...")

        analysis = analyze_article(claude_client, title, description)

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

        # レートリミット対策
        time.sleep(API_CALL_DELAY)

    # --- Google Sheetsへ書き込み ---
    print(f"\nGoogleスプレッドシートへ書き込み中... ({len(rows_to_write)}件)")
    write_rows_to_sheet(worksheet, rows_to_write)

    print("\n" + "=" * 50)
    print(f"  完了! 成功: {success_count}件 / エラー: {error_count}件")
    print("=" * 50 + "\n")


if __name__ == "__main__":
    main()
