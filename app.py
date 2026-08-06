import concurrent.futures
import json
import os
import re
import urllib.parse
import base64
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import time

import feedparser
import requests
from bs4 import BeautifulSoup
from googlenewsdecoder import gnewsdecoder
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components
from dotenv import load_dotenv
from openai import OpenAI
import pandas as pd
from plotly.subplots import make_subplots

load_dotenv()

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
CEREBRAS_API_KEY = os.getenv("CEREBRAS_API_KEY")
TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY")
COINMARKETCAP_API_KEY = os.getenv("COINMARKETCAP_API_KEY")
RSS_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
SAVED_ARTICLES_FILE = Path(__file__).with_name("saved_articles.json")

# ВАЖЛИВО: значення в "sources" — це ДОМЕНИ (не прямі RSS-адреси).
# Прямі RSS-фіди новинних агенцій (Reuters, AP, Ukrinform тощо) регулярно
# зникають/переїжджають, тому всі джерела тепер ідуть через Google News RSS
# (google_news_feed нижче), що набагато стабільніше і до того ж поважає
# фільтри теми/періоду, які раніше просто ігнорувались.
CATEGORIES = {
    "Загальні новини": {
        "topic": "новини світу | world news",
        "sources": {
            "BBC World": "bbc.com",
            "Reuters": "reuters.com",
            "AP News": "apnews.com",
            "Укрінформ": "ukrinform.ua",
            "The Guardian": "theguardian.com",
            "NPR": "npr.org",
        },
    },
    "Війна": {
        "topic": "Україна війна | Ukraine war",
        "sources": {
            "Укрінформ": "ukrinform.ua",
            "Reuters": "reuters.com",
            "Kyiv Independent": "kyivindependent.com",
            "ISW": "understandingwar.org",
            "AP News": "apnews.com",
            "The Guardian": "theguardian.com",
            "Meduza": "meduza.io",
        },
    },
    "Фінанси": {
        "topic": "stock market economy inflation earnings | фондовий ринок економіка інфляція доходи",
        "sources": {
            "Reuters Business": "reuters.com",
            "AP Business": "apnews.com",
            "BBC Business": "bbc.com",
            "CNBC": "cnbc.com",
            "Bloomberg": "bloomberg.com",
            "MarketWatch": "marketwatch.com",
        },
    },
    "Криптовалюти": {
        "topic": "Bitcoin Ethereum cryptocurrency",
        "sources": {
            "Reuters": "reuters.com",
            "CoinDesk": "coindesk.com",
            "Cointelegraph": "cointelegraph.com",
            "AP News": "apnews.com",
            "Decrypt": "decrypt.co",
            "The Block": "theblock.co",
        },
    },
    "Технології": {
        "topic": "artificial intelligence technology | технологія штучного інтелекту",
        "sources": {
            "Reuters Technology": "reuters.com",
            "BBC Technology": "bbc.com",
            "The Verge": "theverge.com",
            "TechCrunch": "techcrunch.com",
            "Ars Technica": "arstechnica.com",
            "Wired": "wired.com",
        },
    },
    "Ілон Маск / компанії": {
        "topic": "Elon Musk Tesla SpaceX X | Ілон Маск Тесла",
        "sources": {
            "Reuters": "reuters.com",
            "AP News": "apnews.com",
            "BBC": "bbc.com",
            "Tesla": "tesla.com",
            "Electrek": "electrek.co",
            "CNBC": "cnbc.com",
        },
    },
}

STOCK_ALIASES = {
    "tesla": "TSLA",
    "nvidia": "NVDA",
    "apple": "AAPL",
    "microsoft": "MSFT",
    "amazon": "AMZN",
    "google": "GOOGL",
    "alphabet": "GOOGL",
    "meta": "META",
}
CRYPTO_ALIASES = {
    "bitcoin": ("BTC", 1),
    "btc": ("BTC", 1),
    "ethereum": ("ETH", 1027),
    "eth": ("ETH", 1027),
    "solana": ("SOL", 5426),
    "sol": ("SOL", 5426),
    "xrp": ("XRP", 52),
    "dogecoin": ("DOGE", 74),
    "doge": ("DOGE", 74),
    "cardano": ("ADA", 2010),
    "ada": ("ADA", 2010),
}

CRYPTO_CHART_INTERVALS = {
    "1Д": ["1хв", "5хв", "15хв"],
    "7Д": ["15хв", "1г", "4г"],
    "30Д": ["1г", "4г", "1д"],
}

INTERVAL_MAP = {
    "1хв": "1min",
    "5хв": "5min",
    "15хв": "15min",
    "30хв": "30min",
    "1г": "1h",
    "4г": "4h",
    "1д": "1day",
}

OUTPUTSIZE_MAP = {
    ("1Д", "1хв"): 1440,
    ("1Д", "5хв"): 288,
    ("1Д", "15хв"): 96,
    ("7Д", "15хв"): 672,
    ("7Д", "1г"): 168,
    ("7Д", "4г"): 42,
    ("30Д", "1г"): 720,
    ("30Д", "4г"): 180,
    ("30Д", "1д"): 30,
}

# Дві палітри для графіка ціни/об'єму. Об'єм навмисно має ЯСКРАВІ, окремі
# від свічок кольори (не просто затемнений варіант того самого зеленого/
# червоного) — щоб стовпчики об'єму завжди чітко відрізнялись від фону,
# незалежно від теми.
CHART_THEMES = {
    "Темна": {
        "plotly_template": "plotly_dark",
        "paper_bgcolor": "#242c3d",
        "plot_bgcolor": "#242c3d",
        "grid_color": "#3a4258",
        "line_color": "#3a4258",
        "font_color": "#d1d4dc",
        "tick_color": "#9aa3b5",
        "price_line_color": "#FFFFFF",
        "candle_up": "#26a69a",
        "candle_down": "#ef5350",
        "volume_color": "#5ee6f2",
        "hover_bg": "#3a4258",
        "legend_bg": "rgba(36, 44, 61, 0.75)",
        "hline_color": "#9aa3b5",
    },
    "Світла": {
        "plotly_template": "plotly_white",
        "paper_bgcolor": "#ffffff",
        "plot_bgcolor": "#ffffff",
        "grid_color": "#e3e7ee",
        "line_color": "#d6dae3",
        "font_color": "#1f2a3b",
        "tick_color": "#5b6472",
        "price_line_color": "#1f2a3b",
        "candle_up": "#0f9d78",
        "candle_down": "#d92c4a",
        "volume_color": "#0f5c66",
        "hover_bg": "#eef1f6",
        "legend_bg": "rgba(255,255,255,0.9)",
        "hline_color": "#5b6472",
    },
}

st.set_page_config(page_title="Аналітик новин", page_icon="📰", layout="wide")

st.markdown(
    """
    <style>
        footer {visibility: hidden;}
        
        /* Компактні відступи для мобільних пристроїв */
        .block-container {
            padding-top: 1rem !important;
            padding-bottom: 1rem !important;
            padding-left: 0.5rem !important;
            padding-right: 0.5rem !important;
            max-width: 100% !important;
        }
        
        /* Забороняємо сторінці виходити за межі екрана телефона */
        html, body, .stApp {
            overflow-x: hidden !important;
            max-width: 100vw !important;
        }
        
        # Обмежуємо переповнення в колонках та радіо-кнопках
        [data-testid="stHorizontalBlock"], [data-testid="stRadio"] {
            max-width: 100% !important;
            overflow-x: hidden !important;
        }

        /* Адаптація для екранів телефонів (до 768px) */
        @media screen and (max-width: 768px) {
            /* Перетворюємо всі горизонтальні колонки у вертикальний список.
               ВАЖЛИВО: самого "width: 100%" на колонках недостатньо — без
               явного flex-direction:column батьківський блок лишався
               флекс-рядком, тому колонки й далі тиснули одна на одну в
               рядок і обрізались через overflow-x:hidden вище. Через це,
               зокрема, блок аналізу здавлювався між блоками статей. */
            [data-testid="stHorizontalBlock"] {
                flex-direction: column !important;
                height: auto !important;
                align-items: stretch !important;
            }
            [data-testid="column"] {
                width: 100% !important;
                flex: 1 1 100% !important;
                min-width: 100% !important;
                height: auto !important;
                max-height: none !important;
                overflow: visible !important;
            }

            /* Збільшуємо кнопки, щоб зручно натискати пальцем */
            .stButton > button {
                width: 100% !important;
                min-height: 44px !important;
            }

            /* Зменшуємо шрифти заголовків для економії місця */
            h1 { font-size: 1.5rem !important; }
            h2 { font-size: 1.25rem !important; }
            h3 { font-size: 1.1rem !important; }

            /* Блок аналізу має пріоритет: піднімаємо його на перше місце
               серед left/analysis/right (замість того, щоб бути затиснутим
               між двома довгими списками статей), і даємо йому власну
               прокрутку, якщо текст довгий, щоб він розкривався повністю,
               не залежачи від висоти сусідніх блоків. */
            [data-testid="stHorizontalBlock"]:has(.st-key-analysis_priority_block)
                [data-testid="column"]:has(.st-key-analysis_priority_block) {
                order: -1 !important;
            }
            .st-key-analysis_priority_block {
                border: 1px solid rgba(128, 128, 128, 0.35);
                border-radius: 10px;
                padding: 12px;
                margin-bottom: 14px;
                max-height: 75vh;
                overflow-y: auto;
                -webkit-overflow-scrolling: touch;
            }
        }
    </style>
    """,
    unsafe_allow_html=True,
)


def _news_locale(domain: str) -> tuple[str, str, str]:
    if domain.endswith(".ua") or "ukrinform" in domain:
        return "uk", "UA", "UA:uk"
    return "en-US", "US", "US:en"


def google_news_feed(domain: str, query: str, hours: int) -> str:
    """Один запит на джерело замість окремого запиту на кожен термін —
    Google News RSS помітно частіше повертає порожній фід (тихо, без HTTP-
    помилки), якщо бомбардувати його забагатьма запитами поспіль."""
    time_filter = f"when:{hours // 24}d" if hours >= 24 and hours % 24 == 0 else f"when:{hours}h"
    terms = [t.strip() for t in query.split("|") if t.strip()]
    if not terms:
        terms = [query] if query.strip() else []

    hl, gl, ceid = _news_locale(domain)

    if terms:
        term_part = " OR ".join(f'"{t}"' if " " in t else t for t in terms)
        search_query = f"site:{domain} ({term_part}) {time_filter}"
    else:
        search_query = f"site:{domain} {time_filter}"

    encoded = urllib.parse.quote(search_query)
    return f"https://news.google.com/rss/search?q={encoded}&hl={hl}&gl={gl}&ceid={ceid}"


def google_news_feed_fallback(domain: str, hours: int) -> str:
    """Запит без ключових слів — лише домен і період. Використовується,
    коли пошук за темою повертає 0 результатів (фраза не збіглась дослівно),
    щоб джерело не зникало з видачі повністю."""
    time_filter = f"when:{hours // 24}d" if hours >= 24 and hours % 24 == 0 else f"when:{hours}h"
    hl, gl, ceid = _news_locale(domain)
    search_query = f"site:{domain} {time_filter}"
    encoded = urllib.parse.quote(search_query)
    return f"https://news.google.com/rss/search?q={encoded}&hl={hl}&gl={gl}&ceid={ceid}"


def api_json(url: str, headers: dict | None = None) -> dict:
    request = Request(url, headers=headers or {"User-Agent": RSS_USER_AGENT})
    try:
        with urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Сервіс даних повернув {error.code}: {detail[:160]}") from error
    except URLError as error:
        raise RuntimeError("Не вдалося з'єднатися із сервісом даних.") from error


@st.cache_data(ttl=30, show_spinner=False)
def twelve_quote(api_key: str, symbol: str) -> dict:
    url = "https://api.twelvedata.com/quote?" + urllib.parse.urlencode({"symbol": symbol, "apikey": api_key})
    data = api_json(url)
    if data.get("status") == "error" or data.get("code"):
        raise RuntimeError(data.get("message", "Twelve Data не повернув котирування."))
    return data


@st.cache_data(ttl=60, show_spinner=False)
def cmc_history(api_key: str, crypto_id: int, count: int = 7) -> list[dict]:
    """Історія через CoinGecko (безкоштовно). crypto_id ігнорується — беремо по символу з виклику."""
    # Мапінг CMC id → CoinGecko id
    id_map = {
        1: "bitcoin",
        1027: "ethereum",
        5426: "solana",
        52: "ripple",
        74: "dogecoin",
        2010: "cardano",
    }
    gecko_id = id_map.get(crypto_id)
    if not gecko_id:
        raise RuntimeError(f"Немає мапінгу CoinGecko для id={crypto_id}")

    # days: 1, 7, 30
    days = max(count, 1)
    url = f"https://api.coingecko.com/api/v3/coins/{gecko_id}/market_chart?" + urllib.parse.urlencode(
        {"vs_currency": "usd", "days": days, "interval": "daily"}
    )
    data = api_json(url)  # CoinGecko не потребує ключа для цього ендпоінту
    prices = data.get("prices", [])
    if not prices:
        raise RuntimeError("CoinGecko не повернув ціни.")

    # Формат: [[timestamp_ms, price], ...]
    result = []
    for ts_ms, price in prices:
        dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        result.append({
            "timestamp": dt.strftime("%Y-%m-%d"),
            "quote": {
                "USD": {
                    "price": price,
                    "close": price,
                    "open": price,
                    "high": price,
                    "low": price,
                }
            },
        })
    return result


@st.cache_data(ttl=30, show_spinner=False)
def cmc_quotes(api_key: str, symbols: tuple[str, ...]) -> dict:
    url = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest?" + urllib.parse.urlencode(
        {"symbol": ",".join(symbols), "convert": "USD"}
    )
    data = api_json(url, {"Accept": "application/json", "X-CMC_PRO_API_KEY": api_key})
    if data.get("status", {}).get("error_code"):
        raise RuntimeError(data["status"].get("error_message", "CoinMarketCap не повернув дані."))
    return data.get("data", {})


@st.cache_data(ttl=60, show_spinner=False)
def twelve_history(api_key: str, symbol: str, count: int = 7, interval: str = "1day") -> list[dict]:
    url = "https://api.twelvedata.com/time_series?" + urllib.parse.urlencode(
        {
            "symbol": symbol,
            "interval": interval,
            "outputsize": max(1, count),
            "apikey": api_key,
        }
    )
    data = api_json(url)
    if data.get("status") == "error" or not data.get("values"):
        raise RuntimeError(data.get("message", "Немає історичних даних Twelve Data."))
    return list(reversed(data["values"]))


def resolve_assets(watchlist: list[str]) -> tuple[list[str], list[tuple[str, int]]]:
    stocks, cryptos = [], []
    for asset in watchlist:
        normalized = asset.casefold().strip()
        if normalized in CRYPTO_ALIASES:
            crypto = CRYPTO_ALIASES[normalized]
            if crypto not in cryptos:
                cryptos.append(crypto)
        else:
            symbol = STOCK_ALIASES.get(normalized, asset.upper().replace(" ", ""))
            if re.fullmatch(r"[A-Z.]{1,8}", symbol) and symbol not in stocks:
                stocks.append(symbol)
    return stocks, cryptos


def format_usd(value) -> str:
    value = float(value)
    if value >= 1:
        return f"${value:,.2f}"
    return f"${value:,.6f}"


def render_price_chart(
    points: list[dict],
    title: str,
    chart_type: str,
    key: str,
    interaction_mode: str = "Pan (вільно)",
    vertical_scale: float = 1.0,
    theme: str = "Темна",
) -> None:
    """Чистий професійний графік TradingView з підтримкою об'ємів для Акцій та Крипти"""
    palette = CHART_THEMES.get(theme, CHART_THEMES["Темна"])
    rows = []
    for point in points:
        quote = point.get("quote", {}).get("USD", point)
        close = quote.get("close") or quote.get("price")
        if close is None:
            continue
        try:
            close = float(close)
        except (TypeError, ValueError):
            continue

        open_price = quote.get("open") or close
        high_price = quote.get("high") or close
        low_price = quote.get("low") or close
        try:
            open_price = float(open_price)
            high_price = float(high_price)
            low_price = float(low_price)
        except (TypeError, ValueError):
            continue

        raw_date = (
            point.get("time_open")
            or point.get("time_close")
            or point.get("datetime")
            or point.get("timestamp")
            or quote.get("timestamp")
        )
        date_value = None
        if raw_date is not None:
            if isinstance(raw_date, (int, float)):
                if raw_date > 1e12:
                    date_value = datetime.fromtimestamp(
                        raw_date / 1000, tz=timezone.utc
                    )
                else:
                    date_value = datetime.fromtimestamp(
                        int(raw_date), tz=timezone.utc
                    )
            elif isinstance(raw_date, str):
                raw_value = raw_date.strip()
                if raw_value.endswith("Z"):
                    raw_value = raw_value[:-1]
                try:
                    date_value = datetime.fromisoformat(raw_value)
                except ValueError:
                    try:
                        date_value = datetime.strptime(
                            raw_value, "%Y-%m-%d %H:%M:%S"
                        )
                    except ValueError:
                        date_value = raw_value
            else:
                date_value = str(raw_date)

        # 🎯 ВИПРАВЛЕНО: Гнучкий пошук об'єму для CoinMarketCap та TwelveData
        volume_value = (
            quote.get("volume")
            or point.get("volume")
            or quote.get("volume_24h")
            or point.get("volume_24h")
        )

        if isinstance(volume_value, str):
            try:
                volume_value = float(volume_value.replace(",", ""))
            except ValueError:
                volume_value = 0.0
        elif isinstance(volume_value, (int, float)):
            volume_value = float(volume_value)
        else:
            volume_value = 0.0

        rows.append({
            "date": date_value,
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "close": close,
            "volume": volume_value,
        })

    if not rows:
        st.warning("Недостатньо даних для побудови графіка")
        return

    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"], utc=True, errors="coerce")
    df = df.sort_values("date").dropna(subset=["date"])
    if df.empty:
        st.warning(
            "Недостатньо коректних часових міток для побудови графіка"
        )
        return

    dates = df["date"]
    opens = df["open"].tolist()
    highs = df["high"].tolist()
    lows = df["low"].tolist()
    closes = df["close"].tolist()
    # Перевіряємо та підтягуємо об'єм
    volumes = []
    volume_is_estimated = []
    for _, row in df.iterrows():
        v = row.get("volume")
        # Якщо API Twelve Data не дає об'єм для крипти (або повертає 0/NaN),
        # показуємо УМОВНИЙ об'єм на основі амплітуди свічки (High - Low) —
        # це не реальні дані біржі, лише орієнтовна оцінка, тому позначаємо
        # її окремо і підписуємо на графіку як "оцінка", щоб не ввести в оману.
        if v is None or pd.isna(v) or float(v) == 0:
            spread = abs(row["high"] - row["low"])
            fake_vol = (
                spread * row["close"] * 10
                if spread > 0
                else row["close"] * 0.1
            )
            volumes.append(fake_vol)
            volume_is_estimated.append(True)
        else:
            volumes.append(float(v))
            volume_is_estimated.append(False)

    df["volume"] = volumes
    has_volume_data = any(v > 0 for v in volumes)
    has_estimated_volume = any(volume_is_estimated)

    # Якщо об'єму немає взагалі, робимо 1 ряд, якщо є — ділимо 75% / 25%
    if has_volume_data:
        fig = make_subplots(
            rows=2,
            cols=1,
            shared_xaxes=True,
            row_heights=[0.66, 0.34],
            vertical_spacing=0.04,
        )
    else:
        fig = make_subplots(rows=1, cols=1)

    if chart_type == "Свічковий":
        fig.add_trace(
            go.Candlestick(
                x=dates,
                open=opens,
                high=highs,
                low=lows,
                close=closes,
                name="Ціна",
                increasing_line_color=palette["candle_up"],
                decreasing_line_color=palette["candle_down"],
                line_width=1,
                whiskerwidth=0.5,
            ),
            row=1,
            col=1,
        )

        fig.add_trace(
            go.Scatter(
                x=dates,
                y=closes,
                mode="lines",
                name="Лінія ціни",
                line=dict(color=palette["price_line_color"], width=1.5),
                hoverinfo="skip",
            ),
            row=1,
            col=1,
        )

        ma7 = df["close"].rolling(window=min(7, len(df))).mean()
        fig.add_trace(
            go.Scatter(
                x=dates,
                y=ma7,
                name="MA7",
                line=dict(color="#2962FF", width=1.5),
                opacity=0.7,
                hoverinfo="skip",
            ),
            row=1,
            col=1,
        )
    else:
        fig.add_trace(
            go.Scatter(
                x=dates,
                y=closes,
                mode="lines",
                name="Ціна",
                line=dict(color="#2962FF", width=2.5),
                fill="tozeroy",
                fillcolor="rgba(41, 98, 255, 0.08)",
                hovertemplate="%{y:$,.2f}<br>%{x|%Y-%m-%d %H:%M}",
            ),
            row=1,
            col=1,
        )

    # 📊 Малювання стовпчиків об'єму для Крипти / Акцій
    if has_volume_data:
        volume_label = "Обʼєм (оцінка)" if has_estimated_volume else "Обʼєм"
        fig.add_trace(
            go.Bar(
                x=dates,
                y=volumes,
                marker=dict(
                    color=palette["volume_color"],
                    line=dict(color=palette["plot_bgcolor"], width=0.5),
                ),
                name=volume_label,
                opacity=1.0,
                showlegend=False,
            ),
            row=2,
            col=1,
        )
        fig.update_yaxes(
            title_text=volume_label, row=2, col=1, tickfont=dict(color=palette["tick_color"])
        )
        if has_estimated_volume:
            st.caption("⚠️ Об'єм для цього активу недоступний з API — показана орієнтовна оцінка на основі амплітуди свічки, а не реальні дані біржі.")

    if title:
        st.markdown(f"### {title}")

    layout_dragmode = "pan"
    # Мінімум піднято з 280 до 340 і зроблено окремо для графіків з об'ємом:
    # при row_heights [0.66, 0.34] і старому мінімумі 280 рядок об'єму
    # (~95px) разом з підписами осі впритул підходив до нижньої межі
    # iframe і на вузьких екранах (телефон) обрізався.
    base_min_height = 340 if has_volume_data else 280
    layout_height = max(base_min_height, min(800, int(400 * float(vertical_scale))))

    fig.update_layout(
        uirevision=key,
        dragmode=layout_dragmode,
        height=layout_height,
        margin=dict(l=5, r=5, t=25, b=30 if has_volume_data else 5),
        template=palette["plotly_template"],
        paper_bgcolor=palette["paper_bgcolor"],
        plot_bgcolor=palette["plot_bgcolor"],
        font=dict(color=palette["font_color"], size=10),
        xaxis=dict(
            showgrid=True,
            gridcolor=palette["grid_color"],
            gridwidth=0.5,
            zeroline=False,
            showline=True,
            linecolor=palette["line_color"],
            linewidth=1,
            tickfont=dict(color=palette["tick_color"], size=9),
            type="date",
            rangeslider=dict(visible=False),
            rangeselector=dict(visible=False),
        ),
        yaxis=dict(
            showgrid=True,
            gridcolor=palette["grid_color"],
            gridwidth=0.5,
            zeroline=False,
            showline=True,
            linecolor=palette["line_color"],
            linewidth=1,
            tickfont=dict(color=palette["tick_color"], size=9),
            tickprefix="$",
        ),
        showlegend=True,
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.01,
            xanchor="left",
            x=0,
            bgcolor=palette["legend_bg"],
            font=dict(color=palette["font_color"], size=9),
        ),
        hovermode="x unified",
        hoverlabel=dict(
            bgcolor=palette["hover_bg"], font_size=11, font_color=palette["font_color"]
        ),
    )

    if has_volume_data:
        fig.update_layout(
            yaxis2=dict(
                showgrid=True,
                gridcolor=palette["grid_color"],
                gridwidth=0.5,
                zeroline=False,
                showline=True,
                linecolor=palette["line_color"],
                linewidth=1,
                tickfont=dict(color=palette["tick_color"], size=9),
            )
        )

    if closes:
        last_price = closes[-1]
        fig.add_hline(
            y=last_price,
            line_dash="dash",
            line_color=palette["hline_color"],
            opacity=0.5,
            line_width=1,
            annotation_text=f"{last_price:.2f}",
            annotation_position="bottom right",
            annotation_font_color=palette["hline_color"],
        )

    st.plotly_chart(
        fig,
        use_container_width=True,
        key=f"plotly_{key}",
        config={
            "displayModeBar": True,
            "scrollZoom": True,
            "responsive": True,
        },
    )

    if closes:
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            current = closes[-1]
            change = (
                ((closes[-1] - closes[0]) / closes[0]) * 100
                if closes[0] != 0
                else 0
            )
            st.metric("Поточна", f"${current:,.2f}", f"{change:+.2f}%")
        with col2:
            st.metric("Максимум", f"${max(closes):,.2f}")
        with col3:
            st.metric("Мінімум", f"${min(closes):,.2f}")
        with col4:
            avg = sum(closes) / len(closes)
            st.metric("Середня", f"${avg:,.2f}")



def _render_market_dashboard_body(category: str, watchlist: list[str]) -> None:
    stocks, cryptos = resolve_assets(watchlist)
    if category not in ("Фінанси", "Криптовалюти", "Ілон Маск / компанії"):
        return

    MARKET_REFRESH_SECONDS = 30
    if "market_last_refresh" not in st.session_state:
        st.session_state["market_last_refresh"] = time.time()
        st.session_state["market_next_refresh"] = st.session_state["market_last_refresh"] + MARKET_REFRESH_SECONDS

    now_ts = time.time()
    if now_ts >= st.session_state["market_next_refresh"]:
        st.session_state["market_last_refresh"] = now_ts
        st.session_state["market_next_refresh"] = now_ts + MARKET_REFRESH_SECONDS

    seconds_until_update = max(0, int(st.session_state["market_next_refresh"] - now_ts))

    st.subheader("📈 Ринок")
    
    timer_id = f"market_timer_{int(now_ts * 1000)}"
    timer_html = f"""
    <div style='display:flex; align-items:center; gap:10px;'>
        <div style='padding:4px 8px; border-radius:8px; background:#f4f5f7; color:#1f2a3b; font-size:12px; text-align:center; border:1px solid #d8dde6; min-width:90px;'>
            ⏱ <strong id='{timer_id}'>{seconds_until_update}</strong> сек.
        </div>
    </div>
    <script>
    (function() {{
            const deadline = {int(st.session_state['market_next_refresh'] * 1000)};
            const output = document.getElementById('{timer_id}');
            if (!output) return;
            function tick() {{
                    const diff = Math.max(0, deadline - Date.now());
                    output.innerText = Math.ceil(diff / 1000);
            }}
            tick();
            setInterval(tick, 1000);
    }})();
    </script>
    """
    st.session_state["market_refresh_seconds"] = MARKET_REFRESH_SECONDS

    # 1. Визначаємо тип активів залежно від категорії
    is_crypto_category = (category == "Криптовалюти")
    
    # 2. Отримуємо метрики цін
    asset_data = []  # [(symbol, display_price, change, extra_id)]
    
    if is_crypto_category:
        if not COINMARKETCAP_API_KEY:
            st.info("Для криптоцін додайте COINMARKETCAP_API_KEY у .env.")
            return
        if not cryptos:
            st.caption("Додайте до «Моїх активів» Bitcoin, Ethereum, Solana або тікери BTC, ETH, SOL.")
            return
        try:
            quote_data = cmc_quotes(COINMARKETCAP_API_KEY, tuple(symbol for symbol, _ in cryptos))
            for symbol, crypto_id in cryptos:
                record = quote_data.get(symbol)
                if isinstance(record, list):
                    record = record[0] if record else None
                if record:
                    usd = record["quote"]["USD"]
                    price = usd["price"]
                    change = float(usd.get('percent_change_24h') or 0)
                    volume = float(usd.get('volume_24h') or 0)  # <-- Отримуємо об'єм
                    asset_data.append((symbol, price, change, volume, crypto_id))
        except RuntimeError as error:
            st.warning(f"Дані CoinMarketCap недоступні: {error}")
            return
    else:
        if not TWELVE_DATA_API_KEY:
            st.info("Для цін акцій додайте TWELVE_DATA_API_KEY у .env.")
            return
        if not stocks:
            st.caption("Додайте до «Моїх активів» тікери, наприклад TSLA, NVDA або AAPL.")
            return
        for symbol in stocks:
            try:
                quote = twelve_quote(TWELVE_DATA_API_KEY, symbol)
                current = quote.get("close") or quote.get("price")
                previous = quote.get("previous_close") or quote.get("open")
                change = quote.get("percent_change")
                if change is None and current and previous:
                    change = (float(current) / float(previous) - 1) * 100
                asset_data.append((symbol, current, float(change or 0), None))
            except RuntimeError as error:
                st.warning(f"{symbol}: {error}")

    if not asset_data:
        return

    # Відображення карток метрик
    # Відображення карток метрик (підтримує і акції, і крипту з об'ємом)
    columns = st.columns(min(4, len(asset_data)))
    for column, item in zip(columns, asset_data):
        symbol, price, change = item[0], item[1], item[2]
        volume = item[3] if len(item) > 4 else None  # Для крипти є 4-й елемент volume
        
        with column:
            st.metric(symbol, format_usd(price), f"{change:+.2f}% за день")
            if volume:
                # Форматування великих чисел об'єму (Мільйони/Мільярди)
                if volume >= 1e9:
                    vol_str = f"${volume / 1e9:.2f}B"
                elif volume >= 1e6:
                    vol_str = f"${volume / 1e6:.2f}M"
                else:
                    vol_str = f"${volume:,.0f}"
                st.caption(f"📊 Об'єм 24г: **{vol_str}**")

    # 3. ЕДИНІ АДАПТИВНІ НАЛАШТУВАННЯ ГРАФІКА ДЛЯ ВСІХ КАТЕГОРІЙ
    row1_col1, row1_col2 = st.columns(2)
    with row1_col1:
        selected_period = st.radio(
            "Період",
            ("1Д", "7Д", "30Д"),
            horizontal=True,
            key=f"timeframe_{category}"
        )
    with row1_col2:
        interval_options = CRYPTO_CHART_INTERVALS.get(selected_period, ["1д"]) if is_crypto_category else ["15хв", "1г", "1д"]
        selected_interval = st.selectbox("Інтервал", interval_options, key=f"interval_{category}")

    row2_col1, row2_col2 = st.columns(2)
    with row2_col1:
        labels = [item[0] for item in asset_data]
        selected_asset = st.selectbox("Актив", labels, key=f"chart_asset_{category}")
    with row2_col2:
        chart_type = st.radio(
            "Тип",
            ("Лінійний", "Свічковий"),
            horizontal=True,
            key=f"chart_type_{category}",
        )

    row3_col1, row3_col2 = st.columns(2)
    with row3_col1:
        chart_theme = st.radio(
            "Тема графіка",
            ("Темна", "Світла"),
            horizontal=True,
            key=f"chart_theme_{category}",
        )
    with row3_col2:
        vertical_scale = st.slider("Вертикальний масштаб", 0.3, 3.0, 1.0, 0.1, key=f"v_scale_{category}")

    # 4. Шапка графіка з кнопкою та таймером
    with st.container(key=f"header_{category}"):
        col_title, col_pause, col_timer, col_button = st.columns([2.4, 1.3, 1, 1], gap="small")
        with col_title:
            st.markdown(f"<h3 style='margin:0'>{selected_asset} · {selected_interval} · {selected_period}</h3>", unsafe_allow_html=True)
        with col_pause:
            st.toggle(
                "⏸ Пауза",
                key="market_autorefresh_paused",
                on_change=_force_full_rerun,
            )
        with col_timer:
            try:
                components.html(timer_html, height=36, scrolling=False)
            except Exception:
                st.markdown("&nbsp;")
        with col_button:
            if st.button("Оновити", key=f"refresh_btn_{category}_{selected_asset}"):
                twelve_history.clear()
                cmc_history.clear()
                cmc_quotes.clear()
                twelve_quote.clear()
                now_local = time.time()
                st.session_state["market_last_refresh"] = now_local
                st.session_state["market_next_refresh"] = now_local + MARKET_REFRESH_SECONDS

    # 5. Отримання даних і побудова однаковим методом
    days_map = {"1Д": 1, "7Д": 7, "30Д": 30}
    selected_days = days_map[selected_period]
    interval_code = INTERVAL_MAP.get(selected_interval, "1day")
    output_count = OUTPUTSIZE_MAP.get((selected_period, selected_interval), selected_days)

    points_key = f"points_{category}_{selected_asset}"
    points = None

    try:
        # Для крипти додаємо /USD, для акцій використовуємо сам тікер
        symbol = f"{selected_asset}/USD" if is_crypto_category else selected_asset

        # Беремо дані з того самого джерела Twelve Data
        points = twelve_history(
            TWELVE_DATA_API_KEY, 
            symbol, 
            count=output_count, 
            interval=interval_code
        )
        
        st.session_state[points_key] = points
    except RuntimeError as error:
        st.warning(f"Дані недоступні: {error}")
        points = st.session_state.get(points_key)

    if points:
        render_price_chart(
            points,
            "",
            chart_type,
            key=f"chart_{category}_{selected_asset}_{selected_period}_{selected_interval}",
            interaction_mode="Pan (вільно)",
            vertical_scale=vertical_scale,
            theme=chart_theme,
        )


def _force_full_rerun() -> None:
    # Тумблер паузи живе всередині фрагмента, а обирає між
    # live/paused-фрагментом код ЗА МЕЖАМИ фрагмента (нижче) — тож без
    # примусового full-app rerun перемикання паузи не встигало б підхопитись.
    st.rerun()


@st.fragment(run_every="30s")
def _market_dashboard_live(category: str, watchlist: list[str]) -> None:
    _render_market_dashboard_body(category, watchlist)


@st.fragment
def _market_dashboard_paused(category: str, watchlist: list[str]) -> None:
    _render_market_dashboard_body(category, watchlist)


def render_market_dashboard(category: str, watchlist: list[str], force_paused: bool = False) -> None:
    if category not in ("Фінанси", "Криптовалюти", "Ілон Маск / компанії"):
        return
    # force_paused=True вимикає авто-оновлення (30s фрагмент) під час аналізу
    # статей LLM: фонове оновлення ринку посеред довгого циклу аналізу може
    # спричиняти передчасний перерендер сторінки, через що готовий текст
    # аналізу "губиться" і з'являється лише після наступного кліку.
    paused = force_paused or st.session_state.get("market_autorefresh_paused", False)
    if paused:
        _market_dashboard_paused(category, watchlist)
    else:
        _market_dashboard_live(category, watchlist)


def plain_text(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", unescape(value or ""))).strip()


def extract_image_from_rss(entry: dict) -> str | None:
    """Витягує картинку з RSS без HTTP-запитів."""
    for field in ("media_content", "media_thumbnail", "enclosures"):
        items = entry.get(field, []) or []
        if isinstance(items, dict):
            items = [items]
        for item in items:
            if not isinstance(item, dict):
                continue
            url = item.get("url") or item.get("href") or item.get("link")
            if url:
                return url
    return None


def extract_real_link(entry: dict) -> tuple[str | None, str]:
    """Швидка спроба без мережевого запиту: інколи <description> містить
    пряме посилання на видання. Ненадійно з 2024 року (Google змінив
    формат), тому це лише допоміжний, не основний спосіб — основний
    нижче, у resolve_article_page (реальний HTTP-редирект)."""
    raw_html = entry.get("summary") or entry.get("description") or ""
    if not raw_html:
        return None, "no_description_field"
    try:
        soup = BeautifulSoup(raw_html, "html.parser")
        a_tag = soup.find("a", href=True)
        if a_tag:
            href = unescape(a_tag["href"])
            if "news.google.com" in href:
                return None, "description_link_is_google_too"
            return href, "ok"
        return None, "no_href_in_description"
    except Exception as error:
        return None, f"parse_error:{error}"


def get_original_url(google_news_url: str, entry: dict = None) -> str:
    """Отримує оригінальний URL: спершу з <description>, потім з entry.links."""
    if entry:
        real, _ = extract_real_link(entry)
        if real:
            return real
    try:
        if entry and 'links' in entry:
            for link in entry['links']:
                if isinstance(link, dict) and link.get('href'):
                    href = link['href']
                    if href and 'news.google.com' not in href:
                        return href
        return google_news_url
    except Exception:
        return google_news_url


def _find_page_image(soup: BeautifulSoup) -> str | None:
    og_image = soup.find("meta", property="og:image")
    if og_image and og_image.get("content"):
        img_url = og_image["content"].strip()
        if img_url.startswith("//"):
            img_url = "https:" + img_url
        if 'lh3.googleusercontent.com' not in img_url:
            return img_url

    twitter_image = soup.find("meta", attrs={"name": "twitter:image"})
    if twitter_image and twitter_image.get("content"):
        img_url = twitter_image["content"].strip()
        if img_url.startswith("//"):
            img_url = "https:" + img_url
        if 'lh3.googleusercontent.com' not in img_url:
            return img_url

    article = soup.find("article")
    if article:
        for img in article.find_all("img"):
            src = img.get("src") or img.get("data-src")
            if src:
                if src.startswith("//"):
                    src = "https:" + src
                if 'lh3.googleusercontent.com' not in src and 'icon' not in src.lower() and 'logo' not in src.lower():
                    return src

    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src")
        if src and not src.endswith(".gif") and not src.endswith(".svg"):
            if src.startswith("//"):
                src = "https:" + src
            if 'lh3.googleusercontent.com' not in src and 'icon' not in src.lower() and 'logo' not in src.lower() and 'avatar' not in src.lower():
                return src
    return None


@st.cache_data(ttl=3600, show_spinner=False)
def resolve_article_page(google_link: str) -> tuple[str, str | None, str]:
    """Спочатку декодуємо справжній URL видання через googlenewsdecoder —
    з 2024 р. простий HTTP-редирект більше НЕ доводить до сайту видання
    (Google змінив механізм на підпис+timestamp через внутрішній ендпоінт),
    тому це єдиний надійний спосіб. Потім одним GET-запитом на вже
    декодований URL парсимо og:image з реальної сторінки видання.
    Повертає (фінальний_url, картинка_або_None, причина)."""
    if not google_link:
        return google_link, None, "no_link"

    final_url = google_link
    try:
        decoded = gnewsdecoder(google_link)
        if decoded.get("status") and decoded.get("decoded_url"):
            final_url = decoded["decoded_url"]
    except Exception:
        pass  # декодер впав — пробуємо звичайний редирект нижче як запасний варіант

    try:
        headers = {
            "User-Agent": RSS_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,uk;q=0.8",
            "Referer": "https://news.google.com/",
        }
        with requests.Session() as session:
            # Обходимо cookie-стіну згоди Google (актуально лише якщо
            # декодер не спрацював і final_url досі веде на google.com).
            session.cookies.set("CONSENT", "YES+", domain=".google.com")
            res = session.get(final_url, headers=headers, timeout=12.0, allow_redirects=True)

            if res.status_code == 401:
                # Деякі видання (напр. Reuters) блокують "холодний" запит
                # без сесії — спершу заходимо на головну сторінку домену,
                # щоб отримати cookies, і повторюємо запит на статтю.
                try:
                    parsed = urllib.parse.urlparse(final_url)
                    homepage = f"{parsed.scheme}://{parsed.netloc}/"
                    session.get(homepage, headers=headers, timeout=10.0)
                    res = session.get(final_url, headers=headers, timeout=12.0, allow_redirects=True)
                except Exception:
                    pass
        if res.status_code != 200:
            return final_url, None, f"http_{res.status_code}"

        final_url = res.url
        if "news.google.com" in final_url or "consent.google.com" in final_url:
            return final_url, None, "redirect_stuck_on_google"

        soup = BeautifulSoup(res.text, "html.parser")
        image = _find_page_image(soup)
        return final_url, image, ("ok" if image else "no_image_meta_found")
    except requests.exceptions.Timeout:
        return final_url, None, "timeout"
    except requests.exceptions.SSLError:
        return final_url, None, "ssl_error"
    except Exception as error:
        return final_url, None, f"exception:{type(error).__name__}"


@st.cache_data(ttl=3600, show_spinner=False)
def extract_article_text(url: str, max_chars: int = 1800) -> str:
    """Витягує основний текст статті зі сторінки видання (а не куций
    RSS-тізер у 1-2 речення) — без цього LLM просто нема з чого робити
    «глибокий» аналіз і вона змушена узагальнювати/фантазувати."""
    if not url:
        return ""
    try:
        headers = {
            "User-Agent": RSS_USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9,uk;q=0.8",
        }
        res = requests.get(url, headers=headers, timeout=8.0)
        if res.status_code != 200:
            return ""
        soup = BeautifulSoup(res.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "figure"]):
            tag.decompose()

        container = soup.find("article") or soup
        paragraphs = [p.get_text(" ", strip=True) for p in container.find_all("p")]
        paragraphs = [p for p in paragraphs if len(p) > 40]  # відкидаємо підписи/меню
        text = " ".join(paragraphs)
        return text[:max_chars]
    except Exception:
        return ""


def process_entry_to_article(source: str, entry: dict) -> dict:
    """Обробляє запис RSS і отримує картинку."""
    google_link = entry.get("link", "")

    rss_image = extract_image_from_rss(entry)
    if rss_image:
        return {
            "source": source,
            "title": plain_text(entry.get("title", "Без назви")),
            "summary": plain_text(entry.get("summary", "")),
            "link": get_original_url(google_link, entry),
            "image": rss_image,
            "image_source": "rss",
            "image_debug": "ok",
        }

    real_link, image_url, debug_reason = resolve_article_page(google_link)

    return {
        "source": source,
        "title": plain_text(entry.get("title", "Без назви")),
        "summary": plain_text(entry.get("summary", "")),
        "link": real_link,
        "image": image_url,
        "image_source": "scrape" if image_url else None,
        "image_debug": debug_reason,
    }


def collect_articles(sources: dict, topic: str, hours: int) -> tuple[list, list]:
    """sources: {display_name: domain}. Будує запити через Google News RSS,
    бо прямі RSS-адреси окремих видань (Reuters/AP/Ukrinform тощо) часто
    відмирають або переїжджають без попередження."""
    raw_entries = []
    problems = []
    seen_links = set()

    def _fetch(feed_url: str):
        feed = feedparser.parse(feed_url, agent=RSS_USER_AGENT)
        return feed

    def _fetch_source(source: str, domain: str):
        found = []
        local_problems = []
        try:
            feed = _fetch(google_news_feed(domain, topic, hours))
            if getattr(feed, "bozo", 0):
                local_problems.append(f"{source}: RSS з помилкою — {feed.get('bozo_exception', 'невідома причина')}")
            for entry in feed.entries[:10]:
                link = entry.get("link", "")
                if link:
                    found.append((source, entry, link))
        except Exception as error:
            local_problems.append(f"{source}: помилка RSS — {error}")

        # Якщо пошук за темою не дав жодного дослівного збігу (Google шукає
        # буквальні слова, а не тему), пробуємо загальний фід джерела за той
        # самий період — це набагато краще, ніж просто пропустити джерело.
        if not found:
            try:
                feed = _fetch(google_news_feed_fallback(domain, hours))
                if getattr(feed, "bozo", 0) and not feed.entries:
                    local_problems.append(
                        f"{source}: резервний запит теж не спрацював — {feed.get('bozo_exception', 'невідома причина')}"
                    )
                for entry in feed.entries[:10]:
                    link = entry.get("link", "")
                    if link:
                        found.append((source, entry, link))
            except Exception as error:
                local_problems.append(f"{source}: помилка резервного RSS — {error}")

        if not found:
            local_problems.append(f"{source}: нічого не знайдено за темою «{topic}» (і без теми теж)")
        return found, local_problems

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as feed_executor:
        feed_futures = {
            feed_executor.submit(_fetch_source, source, domain): source
            for source, domain in sources.items()
        }
        for future in concurrent.futures.as_completed(feed_futures):
            found, local_problems = future.result()
            problems.extend(local_problems)
            for source, entry, link in found:
                if link not in seen_links:
                    seen_links.add(link)
                    raw_entries.append((source, entry))

    articles = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = []
        for source, entry in raw_entries:
            future = executor.submit(process_entry_to_article, source, entry)
            futures.append(future)

        for future in concurrent.futures.as_completed(futures):
            try:
                article = future.result(timeout=10.0)
                if article:
                    articles.append(article)
            except Exception as e:
                print(f"Помилка обробки: {e}")

    return articles, problems


def load_saved_articles() -> list:
    try:
        return json.loads(SAVED_ARTICLES_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_article(article: dict) -> bool:
    saved = load_saved_articles()
    if any(item.get("link") == article["link"] for item in saved):
        return False
    saved.append(
        {
            "source": article["source"],
            "title": article["title"],
            "link": article["link"],
            "saved_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        }
    )
    SAVED_ARTICLES_FILE.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def importance(article: dict, category: str) -> str:
    text = f"{article['title']} {article['summary']}".lower()
    urgent_words = ("breaking", "urgent", "attack", "war", "санкц", "атака", "терміново")
    market_words = ("earnings", "market", "stock", "bitcoin", "crypto", "inflation", "ціна", "акці", "ринок")
    if any(word in text for word in urgent_words):
        return "🔴 Термінова новина"
    if category in ("Фінанси", "Криптовалюти") and any(word in text for word in market_words):
        return "🟠 Можливий вплив на ринок"
    return "🔵 Оглядова новина"


def render_article_card(article: dict, category: str, key: str) -> None:
    with st.expander(f"{article['source']} — {article['title']}"):
        st.caption(importance(article, category))
        if article.get("image"):
            try:
                st.image(article["image"], width="stretch")
            except Exception:
                pass
        if article["link"]:
            st.markdown(f"### [{article['title']}]({article['link']})")
        else:
            st.markdown(f"### {article['title']}")
        if article["summary"]:
            st.write(article["summary"])
        if article["link"] and st.button("🔖 Зберегти", key=f"save_{key}"):
            if save_article(article):
                st.toast("Статтю збережено")
            else:
                st.info("Цю статтю вже збережено.")


# --- ГОЛОВНИЙ ІНТЕРФЕЙС STREAMLIT ---
st.title("📰 Персональний аналітик новин")
st.caption("Оберіть тему — і отримайте новини, джерела та український аналіз без зайвого шуму.")

with st.sidebar:
    st.header("⚙️ Мої налаштування")
    watchlist_text = st.text_area(
        "Мої активи та компанії",
        value="Tesla, Nvidia, Apple, Bitcoin, Ethereum, Solana",
        help="Через кому: акції, криптовалюти, компанії або люди.",
    )
    with st.expander("🔖 Збережені статті"):
        saved_articles = load_saved_articles()
        if not saved_articles:
            st.caption("Тут з’являться статті, які ви збережете.")
        for article in reversed(saved_articles):
            st.markdown(f"[{article['title']}]({article['link']})")
            st.caption(f"{article['source']} · {article['saved_at']}")

category = st.radio("Категорія", tuple(CATEGORIES), horizontal=True)
config = CATEGORIES[category]

top_left, top_right = st.columns((2, 1))
with top_left:
    topic = st.text_input(
        "Що саме шукати?",
        value=config["topic"],
        key=f"topic_{category}",
    )
with top_right:
    period = st.selectbox("Період", ("За 24 години", "За 7 днів", "За 30 днів", "Власний період"))

hours = {"За 24 години": 24, "За 7 днів": 168, "За 30 днів": 720}.get(period)
if hours is None:
    hours = st.slider("Останні години", min_value=6, max_value=720, value=24, step=6)

watchlist = [item.strip() for item in watchlist_text.split(",") if item.strip()]

def render_top(result, force_paused: bool = False):
    """Малює блок статей і ринковий дашборд. Викликається одразу після
    збору статей (аналіз ще не готовий) і повторно на звичайних rerun'ах
    (перемикання активу тощо) — з result, узятого із session_state.
    force_paused=True — під час фонового аналізу LLM, щоб авто-оновлення
    ринку не заважало відображенню готового тексту аналізу."""
    r_category = result["category"]
    r_articles = result["all_articles"]

    if result["problems"]:
        with st.expander("⚠️ Деякі джерела мали проблеми"):
            for problem in result["problems"]:
                st.write("-", problem)

    image_counts = {"rss": 0, "scrape": 0, "none": 0}
    reason_counts: dict[str, int] = {}
    for article in r_articles:
        image_counts[article.get("image_source") or "none"] += 1
        if not article.get("image"):
            reason = article.get("image_debug") or "unknown"
            reason_counts[reason] = reason_counts.get(reason, 0) + 1

    with st.sidebar.expander("🖼️ Діагностика картинок", expanded=False):
        st.write(
            f"З RSS: {image_counts['rss']} · "
            f"Знайдено на сторінці статті: {image_counts['scrape']} · "
            f"Без картинки: {image_counts['none']}"
        )
        if reason_counts:
            st.caption("Причини відсутності картинки:")
            for reason, count in sorted(reason_counts.items(), key=lambda x: -x[1]):
                explain = {
                    "redirect_stuck_on_google": "редирект не довів до сайту видання (завис на google/consent)",
                    "no_image_meta_found": "сторінку відкрито, але на ній немає og:image/twitter:image",
                    "timeout": "сайт не відповів вчасно (timeout)",
                    "ssl_error": "проблема з SSL-сертифікатом сайту",
                    "no_link": "у записі взагалі немає посилання",
                }.get(reason, reason if reason.startswith("http_") else f"технічна помилка: {reason}")
                st.write(f"- {explain}: {count}")
            st.caption("Приклади статей без картинки:")
            shown = 0
            for article in r_articles:
                if not article.get("image") and shown < 3:
                    st.write(f"«{article['title'][:70]}» → {article['link']}")
                    shown += 1

    render_market_dashboard(r_category, result["watchlist"], force_paused=force_paused)

    left_articles, analysis_col_outer, right_articles = st.columns((1.6, 2, 1.6), gap="large")
    with left_articles:
        st.subheader(f"📚 Статті ({len(r_articles[::2])})")
        for index, article in enumerate(r_articles[::2]):
            render_article_card(article, r_category, f"left_{index}_{article['link']}")

    with analysis_col_outer:
        # key дає стабільний CSS-клас (.st-key-analysis_priority_block),
        # за яким мобільний @media-блок вище піднімає цей контейнер над
        # списками статей і додає йому прокрутку.
        analysis_column = st.container(key="analysis_priority_block")

    with right_articles:
        st.subheader(f"📚 Статті ({len(r_articles[1::2])})")
        for index, article in enumerate(r_articles[1::2]):
            render_article_card(article, r_category, f"right_{index}_{article['link']}")

    return analysis_column


def render_text_diagnostics(text_diagnostics):
    if not text_diagnostics:
        return
    got_full_text = sum(1 for d in text_diagnostics if not d["used_fallback"])
    fell_back = len(text_diagnostics) - got_full_text
    avg_len = sum(d["full_text_len"] for d in text_diagnostics) // max(len(text_diagnostics), 1)
    with st.sidebar.expander("📄 Діагностика текстів для аналізу", expanded=False):
        st.write(
            f"Повний текст отримано: {got_full_text} · "
            f"Відкат на короткий тізер: {fell_back} · "
            f"Середня довжина тексту: {avg_len} символів"
        )
        st.caption("Деталі по кожній статті, що йшла в промпт:")
        for d in text_diagnostics:
            status = "✅ повний текст" if not d["used_fallback"] else "⚠️ лише тізер (RSS)"
            st.write(f"- {status}, {d['full_text_len']} симв. — «{d['title']}»")


def render_analysis(container, result):
    """container — або колонка (перший прохід), або st.empty()-плейсхолдер
    (оновлення тим самим блоком після готовності аналізу)."""
    r_category = result["category"]
    with container:
        if result["analysis_error"]:
            st.error(f"Помилка аналізу: {result['analysis_error']}")
        elif result["analysis_text"]:
            meta = result.get("analysis_meta")
            if meta:
                st.success(
                    f"Аналіз готовий! Модель: **{meta['provider']} / {meta['model']}** "
                    f"({meta['article_count']} статей, до {meta['char_cap']} симв. кожна)"
                )
            else:
                st.success("Аналіз готовий!")

            attempts = result.get("analysis_attempts") or []
            if attempts:
                with st.expander(f"⚠️ Невдалі спроби перед успіхом ({len(attempts)})"):
                    # height=140 обмежує висоту блоку до 2-3 рядків із внутрішньою прокруткою
                    with st.container(height=140):
                        for attempt in attempts:
                            st.caption(format_error_compact(attempt))

            st.markdown(result["analysis_text"])
            if r_category in ("Фінанси", "Криптовалюти"):
                st.info("Це аналіз новин, а не інвестиційна порада.")
        else:
            # Якщо під час обробки передано поточну модель, показуємо її
            current_provider = result.get("current_provider")
            current_model = result.get("current_model")
            
            if current_provider and current_model:
                st.info(f"🧠 Аналізую статті за допомогою **{current_provider} / {current_model}**...")
            else:
                st.info("🧠 Підготовлюю джерела та запускаю модель аналізу...")
                
def format_error_compact(err_text: str) -> str:
    """Перетворює довгу помилку з JSON у компактний рядок в один рядок."""
    if "429" in err_text or "RESOURCE_EXHAUSTED" in err_text or "quota" in err_text.lower():
        # Витягуємо назву провайдера/моделі до першої двокрапки
        model_info = err_text.split("):")[0] + ")" if "):" in err_text else err_text.split(":")[0]
        return f"🔴 **{model_info}**: Перевищено ліміт запитів (429 Rate Limit)"
    elif "413" in err_text or "context_length" in err_text.lower():
        model_info = err_text.split("):")[0] + ")" if "):" in err_text else err_text.split(":")[0]
        return f"🟡 **{model_info}**: Занадто великий текст (413 Context Exceeded)"
    else:
        # Для інших помилок обрізаємо довжину до 120 символів
        return err_text[:120] + ("..." if len(err_text) > 120 else "")                


st.caption(f"Джерела для категорії: {', '.join(config['sources'])}")
run_analysis = st.button("🚀 Зібрати та проаналізувати", type="primary")

if run_analysis:
    with st.spinner("🚀 Завантажую статті та фотографії у кілька потоків..."):
        all_articles, problems = collect_articles(config["sources"], topic, hours)

    if not all_articles:
        st.session_state.pop("result", None)
        st.error("Не знайдено жодної новини. Спробуйте уточнити або змінити тему.")
        st.stop()

    # Одразу зберігаємо і малюємо статті — аналіз ще не готовий, але
    # користувач вже може їх читати, поки LLM працює у фоні нижче.
    st.session_state["result"] = {
        "category": category,
        "watchlist": watchlist,
        "all_articles": all_articles,
        "problems": problems,
        "analysis_text": None,
        "analysis_error": None,
        "analysis_meta": None,
        "analysis_attempts": [],
        "text_diagnostics": [],
    }
    analysis_column = render_top(st.session_state["result"], force_paused=True)
    with analysis_column:
        analysis_placeholder = st.empty()
    render_analysis(analysis_placeholder, st.session_state["result"])

    # Обрізаємо кількість статей у промпті, щоб не впиратись у ліміт токенів
    # на хвилину навіть після виправлення квоти ключа. Реальний запас під
    # ліміт визначаємо адаптивно нижче (build_material_text + retry-цикл),
    # тут беремо із запасом «зверху», а звужуємо вже за фактом відповіді
    # сервера — точно передбачити кількість токенів наперед неможливо
    # (токенізатор по-різному «важить» кирилицю й латиницю).
    MAX_ARTICLES_FOR_ANALYSIS = 12
    articles_for_analysis = all_articles[:MAX_ARTICLES_FOR_ANALYSIS]

    # RSS-тізер — це 1-2 речення, з нього неможливо зробити глибокий аналіз.
    # Довантажуємо основний текст статей паралельно (лише для тих, що йдуть
    # у промпт), і використовуємо його замість/на додачу до тізера.
    with st.spinner("Дочитую повні тексти статей для глибшого аналізу..."):
        full_texts: dict[int, str] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            future_to_index = {
                executor.submit(extract_article_text, article["link"]): index
                for index, article in enumerate(articles_for_analysis)
                if article.get("link")
            }
            for future in concurrent.futures.as_completed(future_to_index):
                index = future_to_index[future]
                try:
                    full_texts[index] = future.result(timeout=9.0)
                except Exception:
                    full_texts[index] = ""

    # Діагностика: скільки статей реально дали повний текст, а скільки
    # відкотилось на короткий RSS-тізер (і чому) — щоб було видно, чи
    # довантаження тексту взагалі спрацьовує, а не просто вірити на слово.
    text_diagnostics = []
    for index, article in enumerate(articles_for_analysis):
        body = full_texts.get(index, "")
        text_diagnostics.append({
            "title": article["title"][:70],
            "link": article.get("link", ""),
            "full_text_len": len(body),
            "used_fallback": len(body) <= 200,
        })

    market_note = "\n4. **Можливий вплив на ринок** — коротко поясни можливий зв’язок новин із цінами чи настроями інвесторів." if category in ("Фінанси", "Криптовалюти") else ""

    def build_prompt(articles: list, char_cap: int) -> str:
        """Будує промпт із обмеженням довжини тексту КОЖНОЇ статті —
        char_cap дозволяє звужувати розмір запиту на льоту, коли сервер
        каже, що токенів забагато, без повного переписування логіки."""
        parts = []
        for index, article in enumerate(articles):
            body = full_texts.get(index, "")
            if len(body) <= 200:
                body = article["summary"]  # відкат на RSS-тізер
            parts.append(
                f"Джерело: {article['source']}\nЗаголовок: {article['title']}\nТекст: {body[:char_cap]}"
            )
        raw_text = "\n---\n".join(parts)

        return f"""Ти — старший політичний та економічний аналітик з 15-річним досвідом, який пише для професійної аудиторії. Твій стиль — конкретний, з фактами та цифрами з матеріалів, без води і загальних фраз на кшталт «ситуація складна» чи «потрібно уважно стежити».

Категорія: {category}. Тема: {topic}. Відстежувані активи/компанії: {', '.join(watchlist) or 'не вказано'}.


ЖОРСТКІ ПРАВИЛА ТА ОБМЕЖЕННЯ (NO OUTER KNOWLEDGE):
1. Аналізуй ВИКЛЮЧНО надані матеріали. Заборонено додавати факти, події або контекст, яких немає в тексті (наприклад, вигадувати ракетні випробування чи футбольні турніри).
2. КАТЕГОРИЧНО ЗАБОРОНЕНО згадувати будь-які сторонні компанії, активи чи технології (наприклад: Tesla, Nvidia, Apple, Bitcoin, Ethereum, Solana тощо), якщо вони прямо не згадуються у вхідних статтях. Навіть у розділі "Сліпі плями" не вигадуй відсутні ринки чи тікери, якщо їх немає в тексті новин.
3. ВИМОГА ПОКРИТТЯ ВСІХ РЕГІОНІВ: Переконайся, що у висновках збалансовано враховано всі ключові регіональні блоки з новинного корпусу (включаючи події в Україні/Європі, Близькому Сході та Азії). Не ігноруй регіональні джерела новин.
4. Посилайся на джерела за назвою (наприклад, «за даними Reuters...», «як повідомляє Укрінформ...»).
5. Дотримуйся чіткої, стислої та аналітичної мови. Кожен із 5 пунктів структури має бути обсягом 60–90 слів.
6. Виводь ТІЛЬКИ готовий текст аналітичного звіту. Жодних приміток, вступів чи службових коментарів.
7. КАТЕГОРИЧНО ЗАБОРОНЕНО додавати після звіту будь-яку самоперевірку, підсумок дотримання вимог, підрахунок слів, список використаних джерел окремим блоком чи мета-коментарі на кшталт "Length per section", "Sources referenced", "Structure:", "PERFECT", "Check range". Відповідь має закінчуватись останнім реченням пункту 5 і нічим більше.

НАДАНИЙ НОВИННИЙ КОРПУС СТАТЕЙ:
{raw_text}

СТРУКТУРА ЗВІТУ (Виведи строго ці 5 пунктів):
1. **Глибокий контекст та Головна суть** — що саме сталося, передумови та важливість подій (мінімум 2 конкретні деталі з тексту).
2. **Порівняльний аналіз та достовірність джерел** — акценти різних джерел та рівень довіри до них у межах наданих текстів.
3. **Причинно-наслідкові зв'язки** — реальний вплив подій на суміжні сфери, які чітко описані в статтях.
4. **Сліпі плями та приховані ризики** — що саме надані джерела залишають без відповіді, замалчують або недоговорюють у межах цих тем. Якщо інформації про якісь сфери (ринки, активи тощо) взагалі немає — прямо вкажи це ("інформація про фінансові ринки/технології відсутня у тексті"), а не додумуй.
5. **Стратегічний прогноз** — короткострокові та довгострокові наслідки, а також 3–4 конкретні маркери для подальшого спостереження, які випливають із текстів.
"""

    SYSTEM_INSTRUCTION_UK = (
        "Ти відповідаєш ВИКЛЮЧНО українською мовою. Це правило абсолютне: "
        "жодного слова, фрази чи заголовка англійською чи будь-якою іншою мовою "
        "в тексті відповіді, навіть якщо матеріали для аналізу — англомовні. "
        "Назви джерел (Reuters, AP News тощо) і власні назви можна лишати як є, "
        "решта тексту — тільки українською."
    )

    def call_gemini_native(model_name: str, prompt_text: str, api_key: str, max_tokens: int, temperature: float) -> str:
        """Нові ключі Google (формат `AQ.Ab...`, т.зв. auth keys) мають
        відомий баг у зв'язці з OpenAI-сумісним ендпоінтом Gemini —
        сервер повертає 'Multiple authentication credentials received',
        навіть якщо ключ повністю робочий. Тому для Gemini йдемо напряму
        через рідний REST-ендпоінт з єдиним заголовком x-goog-api-key,
        де цей баг не відтворюється, замість client.chat.completions.create."""
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"
        headers = {
            "x-goog-api-key": api_key,
            "Content-Type": "application/json",
        }
        payload = {
            "systemInstruction": {"parts": [{"text": SYSTEM_INSTRUCTION_UK}]},
            "contents": [{"parts": [{"text": prompt_text}]}],
            "generationConfig": {
                "maxOutputTokens": max_tokens,
                "temperature": temperature,
            },
        }
        response = requests.post(url, headers=headers, json=payload, timeout=60)
        if response.status_code != 200:
            raise RuntimeError(f"Error code: {response.status_code} - {response.text[:500]}")
        data = response.json()
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as exc:
            raise RuntimeError(f"Неочікувана відповідь Gemini: {data}") from exc

    def _is_size_error(error: Exception) -> bool:
        msg = str(error).lower()
        return "request too large" in msg or "tokens per minute" in msg or (
            "413" in msg and "token" in msg
        )

    def _strip_meta_tail(text: str) -> str:
        """Страховка на випадок, якщо модель все ж таки дописала службову
        самоперевірку після звіту (спостерігалось у gemini-3.6-flash) —
        всупереч прямій забороні в промпті. Обрізаємо текст по першому
        входженню характерних маркерів такого блоку."""
        markers = [
            "\nLength per section",
            "\nSources referenced",
            "\nStructure:",
            "\nWord count",
            "\n* Length per section",
            "\n* Sources referenced",
            "\n* Structure:",
        ]
        cut_at = len(text)
        for marker in markers:
            idx = text.find(marker)
            if idx != -1:
                cut_at = min(cut_at, idx)
        return text[:cut_at].rstrip()

    analysis_text = None
    analysis_error = None
    analysis_meta = None  # який провайдер/модель/розмір реально спрацював
    with st.spinner("Створюю український аналіз..."):
        # Пробуємо провайдерів по черзі: Gemini має набагато щедріший
        # безкоштовний ліміт токенів (до 1M TPM), тож ставимо його першим —
        # найменше шансів впертися в "забагато токенів". Groq — швидкий і
        # надійний запасний варіант. OpenRouter — останній резерв (там
        # ліміт добовий, а не хвилинний, тож раз вичерпаний — довго не
        # відновиться).
        # ---------------------------------------------------------
        # 🔹 ДОПОМІЖНА ФУНКЦІЯ БЕЗПЕЧНОГО ЧИТАННЯ СЕКРЕТІВ
        # ---------------------------------------------------------
        def _fetch_secret(key_name: str):
            try:
                if key_name in st.secrets and st.secrets[key_name]:
                    val = str(st.secrets[key_name]).strip()
                    if val:
                        return val
            except Exception:
                pass
            val_env = os.getenv(key_name)
            return val_env.strip() if val_env else None

        # ---------------------------------------------------------
        # 🔹 1. ДИНАМІЧНИЙ ЗБІР УСІХ КЛЮЧІВ GEMINI
        # ---------------------------------------------------------
        gemini_candidates = []

        # а) Збираємо з os.environ (.env)
        for env_key, env_val in os.environ.items():
            if env_key.startswith("GEMINI_API_KEY") and env_val and env_val.strip():
                gemini_candidates.append(env_val.strip())

        # б) Збираємо з st.secrets (Streamlit Cloud / secrets.toml)
        try:
            for sec_key in list(st.secrets.keys()):
                if sec_key.startswith("GEMINI_API_KEY"):
                    sec_val = st.secrets[sec_key]
                    if sec_val and isinstance(sec_val, str) and sec_val.strip():
                        gemini_candidates.append(sec_val.strip())
        except Exception:
            pass

        # в) Видаляємо дублікати
        gemini_keys = list(dict.fromkeys(gemini_candidates))

        providers = []

        # 🔹 2. Актуальні моделі Gemini (станом на зараз).
        # gemini-2.0-flash / gemini-2.0-flash-lite / gemini-1.5-flash-8b
        # прибрано зі списку — Google вже вивів їх з експлуатації, тож
        # кожна спроба аналізу спершу марно "билась" об ці мертві моделі
        # (чекаючи таймаут/помилку), перш ніж дійти до робочих, — це і
        # було однією з причин, чому результат довго не з'являвся.
        gemini_models = [
            "gemini-3.6-flash",
            "gemini-3.5-flash-lite",
            "gemini-2.5-pro",
            "gemini-2.5-flash",
        ]

        for i, key in enumerate(gemini_keys, 1):
            providers.append({
                "name": f"Gemini #{i}" if len(gemini_keys) > 1 else "Gemini",
                "native": True,
                "api_key": key,
                "models": gemini_models,
            })
            
        # 2. ДОДАЄМО CEREBRAS (Працює через стандартну бібліотеку OpenAI)
        cerebras_key = _fetch_secret("CEREBRAS_API_KEY")
        if cerebras_key:
            providers.append({
                "name": "Cerebras",
                "native": False,
                "base_url": "https://api.cerebras.ai/v1",
                "api_key": cerebras_key,
                "models": [
                    "llama-3.3-70b",
                    "llama3.1-8b",
                ],
            })    

        # 🔹 3. Groq (з резервною 8b-моделлю)
        groq_key = _fetch_secret("GROQ_API_KEY")
        if groq_key:
            providers.append({
                "name": "Groq",
                "native": False,
                "base_url": "https://api.groq.com/openai/v1",
                "api_key": groq_key,
                "models": [
                    "llama-3.3-70b-versatile",
                    "llama-3.1-8b-instant",
                ],
            })

        # 🔹 4. OpenRouter (актуальні безкоштовні слаги)
        openrouter_key = _fetch_secret("OPENROUTER_API_KEY")
        if openrouter_key:
            providers.append({
                "name": "OpenRouter",
                "native": False,
                "base_url": "https://openrouter.ai/api/v1",
                "api_key": openrouter_key,
                "models": [
                    "qwen/qwen-2.5-72b-instruct:free",
                    "deepseek/deepseek-r1:free",
                ],
            })

        errors = []
        analysis_text = None
        analysis_meta = None
        analysis_error = None
        current_diagnostics = text_diagnostics

        # 🔹 Перевірка 1: Чи знайшовся хоча б один провайдер
        if not providers:
            analysis_error = "Не знайдено жодного API-ключа у Secrets чи змінних середовища (.env)"
            st.session_state["result"].update({
                "analysis_text": None,
                "analysis_error": analysis_error,
                "analysis_meta": None,
                "analysis_attempts": [],
                "text_diagnostics": current_diagnostics,
            })
            render_analysis(analysis_placeholder, st.session_state["result"])

        # 🔹 Перевірка 2: Чи є статті для аналізу
        elif not articles_for_analysis:
            analysis_error = "Список статей порожній або не сформований. Немає даних для аналізу."
            st.session_state["result"].update({
                "analysis_text": None,
                "analysis_error": analysis_error,
                "analysis_meta": None,
                "analysis_attempts": [],
                "text_diagnostics": current_diagnostics,
            })
            render_analysis(analysis_placeholder, st.session_state["result"])

        else:
            articles_list = articles_for_analysis
            size_steps = [
                (len(articles_list), 1600),
                (min(10, len(articles_list)), 900),
                (min(7, len(articles_list)), 600),
                (min(5, len(articles_list)), 400),
                (min(3, len(articles_list)), 300),
            ]

            for provider in providers:
                if analysis_text is not None:
                    break

                client = None
                if not provider.get("native"):
                    try:
                        client = OpenAI(
                            base_url=provider["base_url"],
                            api_key=provider["api_key"],
                        )
                    except Exception as e:
                        errors.append(f"{provider['name']} Init Error: {e}")
                        continue

                for model_name in provider["models"]:
                    if analysis_text is not None:
                        break

                    for article_count, char_cap in size_steps:
                        try:
                            prompt_text = build_prompt(articles_list[:article_count], char_cap)
                        except Exception as p_err:
                            errors.append(f"Prompt Build Error: {p_err}")
                            break

                        max_out_tokens = 2048 if provider["name"] == "Groq" else 3600
                        temperature = 0.15  # 👈 Додай це тут

                        analysis_placeholder.info(
                            f"🧠 Аналізую статті за допомогою **{provider['name']} / {model_name}** "
                            f"({article_count} ст., до {char_cap} симв.)..."
                        )

                        try:
                            if provider.get("native"):
                                current_text = call_gemini_native(
                                    model_name,
                                    prompt_text,
                                    provider["api_key"],
                                    max_tokens=max_out_tokens,
                                    temperature=temperature,  # 👈 А сюди передай змінну
                                )
                            else:
                                if not client:
                                    raise RuntimeError("OpenAI client не ініціалізовано")

                                completion = client.chat.completions.create(  # 👈 ТУТ МАЄ БУТИ client.chat.completions.create (без додаткового .client)
                                    model=model_name,
                                    messages=[{"role": "user", "content": prompt_text}],
                                    max_tokens=max_out_tokens,
                                    temperature=temperature,
                                    extra_body=provider.get("extra_body"),
                                )
                                raw_content = completion.choices[0].message.content or ""
                                # Очищення від процесів міркування (DeepSeek-R1)
                                if "<thought>" in raw_content and "</thought>" in raw_content:
                                    raw_content = raw_content.split("</thought>")[-1]
                                current_text = raw_content.strip()

                            if not current_text:
                                raise RuntimeError("Модель повернула порожню відповідь (0 символів)")

                            current_text = _strip_meta_tail(current_text)

                            analysis_text = current_text
                            analysis_error = None
                            analysis_meta = {
                                "provider": provider["name"],
                                "model": model_name,
                                "article_count": article_count,
                                "char_cap": char_cap,
                            }

                            st.session_state["result"].update({
                                "analysis_text": analysis_text,
                                "analysis_error": None,
                                "analysis_meta": analysis_meta,
                                "analysis_attempts": errors,
                                "text_diagnostics": current_diagnostics,
                            })
                            render_analysis(analysis_placeholder, st.session_state["result"])
                            break

                        except Exception as error:
                            err_msg = str(error).lower()
                            errors.append(f"{provider['name']}/{model_name} ({article_count} ст., {char_cap} симв.): {error}")

                            if "429" in err_msg or "quota" in err_msg or "rate limit" in err_msg or "resource_exhausted" in err_msg:
                                break

                            if "context_length_exceeded" in err_msg or "too long" in err_msg or "413" in err_msg or _is_size_error(error):
                                continue

                            break

            if analysis_text is None and errors:
                analysis_error = " | ".join(errors)

    # Фінальне оновлення стану
    st.session_state["result"].update({
        "analysis_text": analysis_text,
        "analysis_error": analysis_error,
        "analysis_meta": analysis_meta,
        "analysis_attempts": errors if providers else [],
        "text_diagnostics": current_diagnostics,
    })

    render_text_diagnostics(st.session_state["result"]["text_diagnostics"])
    render_analysis(analysis_placeholder, st.session_state["result"])

    # Аналіз повністю готовий і вже збережений у session_state. Форсуємо
    # один чистий rerun: далі сторінка малюється простим і надійним шляхом
    # (гілка "elif result in session_state" нижче) замість того, щоб
    # покладатись на живе оновлення плейсхолдера посеред довгого циклу —
    # саме це раніше іноді призводило до того, що готовий текст аналізу
    # не з'являвся сам, поки користувач щось не натисне.
    st.rerun()

elif "result" in st.session_state:
    result = st.session_state["result"]
    analysis_column = render_top(result)
    render_text_diagnostics(result.get("text_diagnostics", []))
    render_analysis(analysis_column, result)