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

st.set_page_config(page_title="Аналітик новин", page_icon="📰", layout="wide")
st.markdown(
    """
    <style>
        footer {visibility: hidden;}
        .block-container {padding-top: 1.5rem;}
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


def render_price_chart(points: list[dict], title: str, chart_type: str, key: str, interaction_mode: str = "Pan (вільно)", vertical_scale: float = 1.0) -> None:
    """Чистий професійний графік як на TradingView"""
    import pandas as pd
    
    dates, opens, highs, lows, closes = [], [], [], [], []
    
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
                    date_value = datetime.fromtimestamp(raw_date / 1000, tz=timezone.utc)
                else:
                    date_value = datetime.fromtimestamp(int(raw_date), tz=timezone.utc)
            elif isinstance(raw_date, str):
                raw_value = raw_date.strip()
                if raw_value.endswith("Z"):
                    raw_value = raw_value[:-1]
                try:
                    date_value = datetime.fromisoformat(raw_value)
                except ValueError:
                    try:
                        date_value = datetime.strptime(raw_value, "%Y-%m-%d %H:%M:%S")
                    except ValueError:
                        date_value = raw_value
            else:
                date_value = str(raw_date)

        volume_value = quote.get("volume") or point.get("volume")
        if isinstance(volume_value, str):
            try:
                volume_value = float(volume_value.replace(",", ""))
            except ValueError:
                volume_value = None
        elif isinstance(volume_value, (int, float)):
            volume_value = float(volume_value)
        else:
            volume_value = None

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
        st.warning("Недостатньо коректних часових міток для побудови графіка")
        return

    dates = df["date"]
    opens = df["open"].tolist()
    highs = df["high"].tolist()
    lows = df["low"].tolist()
    closes = df["close"].tolist()

    # Створюємо графік з обʼємом
    volume_data = df.get("volume") if "volume" in df.columns else None
    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        row_heights=[0.75, 0.25],
        vertical_spacing=0.03,
    )

    if chart_type == "Свічковий":
        # ЧИСТІ СВІЧКИ як на TradingView
        fig.add_trace(go.Candlestick(
            x=dates,
            open=opens,
            high=highs,
            low=lows,
            close=closes,
            name="Ціна",
            increasing_line_color='#26a69a',
            decreasing_line_color='#ef5350',
            line_width=1,
            whiskerwidth=0.5,
        ), row=1, col=1)
        
        # Додаємо чітку лінію фактичних закриттів для кращої видимості
        fig.add_trace(go.Scatter(
            x=dates,
            y=closes,
            mode='lines',
            name='Лінія ціни',
            line=dict(color='#FFFFFF', width=1.5),
            hoverinfo='skip',
        ), row=1, col=1)

        # Додаємо ковзну середню (опціонально, але робить графік професійнішим)
        ma7 = df['close'].rolling(window=min(7, len(df))).mean()
        fig.add_trace(go.Scatter(
            x=dates,
            y=ma7,
            name='MA7',
            line=dict(color='#2962FF', width=1.5),
            opacity=0.7,
            hoverinfo='skip'
        ), row=1, col=1)
    else:
        # ЛІНІЙНИЙ ГРАФІК - чистий і чіткий
        fig.add_trace(
            go.Scatter(
                x=dates,
                y=closes,
                mode='lines',
                name='Ціна',
                line=dict(color='#2962FF', width=2.5),
                fill='tozeroy',
                fillcolor='rgba(41, 98, 255, 0.08)',
                hovertemplate='%{y:$,.2f}<br>%{x|%Y-%m-%d %H:%M}',
            ),
            row=1,
            col=1,
        )

    if volume_data is not None:
        volume = [float(v) if v is not None else 0 for v in volume_data]
        volume_colors = ['#26a69a' if c >= o else '#ef5350' for o, c in zip(opens, closes)]
        fig.add_trace(
            go.Bar(
                x=dates,
                y=volume,
                marker_color=volume_colors,
                name='Обʼєм',
                opacity=0.75,
                showlegend=False,
            ),
            row=2,
            col=1,
        )
        fig.update_yaxes(title_text='Обʼєм', row=2, col=1, tickfont=dict(color='#787b86'))

    # Заголовок графіка малюють виклики-обгортки (крипто/акції) самі, до фетчу
    # даних — тут лишається лише запасний варіант на випадок прямого виклику.
    if title:
        st.markdown(f"### {title}")
    # НАЛАШТУВАННЯ ДЛЯ ЧИСТОГО ПРОФЕСІЙНОГО ВИГЛЯДУ

    # Налаштування взаємодії: Pan (вільно), Pan (горизонтально), Zoom (обидві осі), Zoom X, Zoom Y
    # Єдиний режим: Pan (вільно)
    layout_dragmode = "pan"
    x_fixed = False
    y_fixed = False

    layout_height = max(320, min(1600, int(560 * float(vertical_scale))))
    fig.update_layout(
        uirevision=key,
        dragmode=layout_dragmode,
        height=layout_height,
        margin=dict(l=10, r=10, t=30, b=10),
        # Темна тема як на TradingView
        template='plotly_dark',
        paper_bgcolor='#131722',
        plot_bgcolor='#131722',
        font=dict(color='#d1d4dc', size=12),
        # Сітка
            xaxis=dict(
            showgrid=True,
            gridcolor='#2a2e39',
            gridwidth=0.5,
            zeroline=False,
            showline=True,
            linecolor='#2a2e39',
            linewidth=1,
            tickfont=dict(color='#787b86'),
            type='date',
            fixedrange=x_fixed,
            rangeslider=dict(visible=False, thickness=0.05),
            rangeselector=dict(
                bgcolor='#131722',
                activecolor='#2a2e39',
                bordercolor='#2a2e39',
                borderwidth=1,
                x=0,
                xanchor='left',
                y=1.12,
                yanchor='bottom',
                buttons=[
                    dict(count=1, label='1д', step='day', stepmode='backward'),
                    dict(count=7, label='7д', step='day', stepmode='backward'),
                    dict(count=1, label='1м', step='month', stepmode='backward'),
                    dict(step='all', label='Увесь період'),
                ],
            ),
        ),
        yaxis=dict(
            showgrid=True,
            gridcolor='#2a2e39',
            gridwidth=0.5,
            zeroline=False,
            showline=True,
            linecolor='#2a2e39',
            linewidth=1,
            tickfont=dict(color='#787b86'),
            tickprefix='$',
            fixedrange=y_fixed,
        ),
        yaxis2=dict(
            showgrid=True,
            gridcolor='#2a2e39',
            gridwidth=0.5,
            zeroline=False,
            showline=True,
            linecolor='#2a2e39',
            linewidth=1,
            tickfont=dict(color='#787b86'),
            fixedrange=y_fixed,
        ),
        # Легенда
        showlegend=True,
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="right",
            x=1,
            bgcolor='rgba(19, 23, 34, 0.8)',
            font=dict(color='#d1d4dc', size=11)
        ),
        hovermode='x unified',
        hoverlabel=dict(
            bgcolor='#2a2e39',
            font_size=12,
            font_color='#d1d4dc'
        )
    )
    
    # Додаємо горизонтальну лінію останньої ціни
    if closes:
        last_price = closes[-1]
        fig.add_hline(
            y=last_price,
            line_dash="dash",
            line_color="#787b86",
            opacity=0.5,
            line_width=1,
            annotation_text=f"{last_price:.2f}",
            annotation_position="bottom right",
            annotation_font_color="#787b86"
        )

    st.plotly_chart(
        fig,
        use_container_width=True,
        key=f"plotly_{key}",
        config={"displayModeBar": True, "scrollZoom": True, "responsive": True},
    )

    # Проста статистика
    if closes:
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            current = closes[-1]
            change = ((closes[-1] - closes[0]) / closes[0]) * 100 if closes[0] != 0 else 0
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
    # Якщо час оновлення настав — оновлюємо мітки
    if now_ts >= st.session_state["market_next_refresh"]:
        st.session_state["market_last_refresh"] = now_ts
        st.session_state["market_next_refresh"] = now_ts + MARKET_REFRESH_SECONDS

    # Підготовка змінних таймера завжди — щоб timer_html був визначений
    seconds_until_update = max(0, int(st.session_state["market_next_refresh"] - now_ts))

    st.subheader("📈 Ринок")
    # Підготовка HTML для таймера — рендериться поруч із заголовком графіка
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
    # Зберігаємо інтервал у session_state, щоб кнопка у render_price_chart знала, на скільки оновлювати
    st.session_state["market_refresh_seconds"] = MARKET_REFRESH_SECONDS
    if category in ("Фінанси", "Ілон Маск / компанії"):
        if not TWELVE_DATA_API_KEY:
            st.info("Для цін акцій, валют та графіків додайте TWELVE_DATA_API_KEY у .env.")
        elif not stocks:
            st.caption("Додайте до «Моїх активів» тікери, наприклад TSLA, NVDA або AAPL.")
        else:
            stock_data = []
            for symbol in stocks:
                try:
                    quote = twelve_quote(TWELVE_DATA_API_KEY, symbol)
                    current = quote.get("close") or quote.get("price")
                    previous = quote.get("previous_close") or quote.get("open")
                    change = quote.get("percent_change")
                    if change is None and current and previous:
                        change = (float(current) / float(previous) - 1) * 100
                    stock_data.append((symbol, current, change))
                except RuntimeError as error:
                    st.warning(f"{symbol}: {error}")
            if stock_data:
                columns = st.columns(min(4, len(stock_data)))
                for column, (symbol, price, change) in zip(columns, stock_data):
                    with column:
                        st.metric(symbol, format_usd(price), f"{float(change or 0):+.2f}% за день")
                
                # ДОДАТИ ЦЕ - таймфрейми для акцій
                timeframes = st.radio(
                    "Таймфрейм",
                    ("1Д", "7Д", "30Д"),
                    horizontal=True,
                    key="timeframe_stock"
                )
                days_map = {"1Д": 1, "7Д": 7, "30Д": 30}
                selected_days = days_map[timeframes]
                
                selected_stock = st.selectbox("Графік акції", [item[0] for item in stock_data], key="stock_chart")
                stock_chart_type = st.radio("Тип графіка", ("Лінійний", "Свічковий"), horizontal=True, key="stock_chart_type")
                vertical_scale_stock = st.slider("Вертикальний масштаб (свічки)", 0.3, 3.0, 1.0, 0.1, key="vertical_scale_stock")

                # Заголовок, таймер і кнопка — рендеримо ДО запиту даних, щоб клік
                # на «Оновити» встиг очистити кеш ще в цьому ж прогоні.
                st.markdown(
                    """
                    <style>
                    .st-key-stock_header div[data-testid="stVerticalBlock"] { gap: 0.15rem; }
                    .st-key-stock_header div[data-testid="stElementContainer"] { margin: 0 !important; }
                    .st-key-stock_header iframe { display: block; }
                    </style>
                    """,
                    unsafe_allow_html=True,
                )
                with st.container(key="stock_header"):
                    col_title, col_pause, col_timer, col_button = st.columns([2.4, 1.3, 1, 1], gap="small")
                    with col_title:
                        st.markdown(f"<h3 style='margin:0'>{selected_stock} · Останні {selected_days} днів</h3>", unsafe_allow_html=True)
                    with col_pause:
                        st.toggle(
                            "⏸ Пауза",
                            key="market_autorefresh_paused",
                            help="Призупинити автооновлення кожні 30с, щоб не збивало зум/масштаб графіка.",
                            on_change=_force_full_rerun,
                        )
                    with col_timer:
                        try:
                            components.html(timer_html, height=36, scrolling=False)
                        except Exception:
                            try:
                                st.markdown(timer_html, unsafe_allow_html=True)
                            except Exception:
                                st.markdown("&nbsp;")
                    with col_button:
                        refresh_now_stock = st.button("Оновити", key=f"market_refresh_button_stock_{selected_stock}")
                        if refresh_now_stock:
                            twelve_history.clear()
                            twelve_quote.clear()
                            now_local = time.time()
                            secs = int(st.session_state.get("market_refresh_seconds", 30))
                            st.session_state["market_last_refresh"] = now_local
                            st.session_state["market_next_refresh"] = now_local + secs

                # Отримуємо або кешуємо дані для оновлення ціни без втрати виду
                stock_points_key = f"points_stock_{selected_stock}"
                points = None
                try:
                    points = twelve_history(TWELVE_DATA_API_KEY, selected_stock)
                    st.session_state[stock_points_key] = points
                except RuntimeError as error:
                    st.caption(f"Графік {selected_stock} недоступний: {error}")
                    points = st.session_state.get(stock_points_key)

                if points:
                    try:
                        render_price_chart(
                            points,
                            "",
                            stock_chart_type,
                            key=f"stock_price_chart_{selected_stock}_{timeframes}",
                            interaction_mode="Pan (вільно)",
                            vertical_scale=vertical_scale_stock,
                        )
                    except RuntimeError as error:
                        st.caption(f"Графік {selected_stock} недоступний: {error}")

    if category == "Криптовалюти":
        if not COINMARKETCAP_API_KEY:
            st.info("Для криптоцін і графіків додайте COINMARKETCAP_API_KEY у .env.")
        elif not cryptos:
            st.caption("Додайте до «Моїх активів» Bitcoin, Ethereum, Solana або тікери BTC, ETH, SOL.")
        else:
            try:
                quote_data = cmc_quotes(COINMARKETCAP_API_KEY, tuple(symbol for symbol, _ in cryptos))
                crypto_data = []
                for symbol, crypto_id in cryptos:
                    record = quote_data.get(symbol)
                    if isinstance(record, list):
                        record = record[0] if record else None
                    if record:
                        usd = record["quote"]["USD"]
                        crypto_data.append((symbol, crypto_id, usd))
                columns = st.columns(min(4, len(crypto_data)))
                for column, (symbol, _, usd) in zip(columns, crypto_data):
                    with column:
                        st.metric(symbol, format_usd(usd["price"]), f"{float(usd.get('percent_change_24h') or 0):+.2f}% за 24 год")
                        st.caption(f"7 днів: {float(usd.get('percent_change_7d') or 0):+.2f}%")
                if crypto_data:
                    labels = [item[0] for item in crypto_data]
                    
                    # Приклад більш зрозумілого блока управління
                    interval_options = CRYPTO_CHART_INTERVALS.get("1Д", ["1хв", "5хв", "15хв"])
                    col_tf, col_interval, col_crypto, col_type = st.columns([1.1, 1.1, 1.4, 1.4])
                    with col_tf:
                        timeframes_crypto = st.radio(
                            "Період",
                            ("1Д", "7Д", "30Д"),
                            horizontal=True,
                            key="timeframe_crypto"
                        )
                    with col_interval:
                        interval_options = CRYPTO_CHART_INTERVALS.get(timeframes_crypto, ["1д"])
                        selected_interval = st.selectbox("Інтервал", interval_options, key="crypto_interval")
                    with col_crypto:
                        selected_crypto = st.selectbox("Актив", labels, key="crypto_chart")
                    with col_type:
                        crypto_chart_type = st.radio(
                            "Тип",
                            ("Лінійний", "Свічковий"),
                            horizontal=True,
                            key="crypto_chart_type",
                        )
                        fast_crypto = st.checkbox(
                            "Швидкий режим",
                            value=False,
                            help="Швидкий графік без важких свічок для інтрадею.",
                            key="crypto_fast_mode",
                        )

                    days_map_crypto = {"1Д": 1, "7Д": 7, "30Д": 30}
                    selected_days_crypto = days_map_crypto[timeframes_crypto]
                    interval_code = INTERVAL_MAP.get(selected_interval, "1day")
                    output_count = OUTPUTSIZE_MAP.get((timeframes_crypto, selected_interval), selected_days_crypto)

                    if selected_interval in ("1хв", "5хв") and crypto_chart_type == "Свічковий":
                        st.caption("Увага: 1хв/5хв свічки можуть працювати повільніше. Для більш плавної роботи увімкніть Швидкий режим або оберіть 15хв.")

                    display_type = "Лінійний" if fast_crypto else crypto_chart_type
                    crypto_id = next(item[1] for item in crypto_data if item[0] == selected_crypto)
                    vertical_scale_crypto = st.slider("Вертикальний масштаб (свічки)", 0.3, 3.0, 1.0, 0.1, key="vertical_scale_crypto")

                    # Заголовок, таймер і кнопка в ОДНОМУ рядку — рендеримо ДО запиту
                    # даних, щоб клік на «Оновити» встиг очистити кеш цього ж прогону.
                    st.markdown(
                        """
                        <style>
                        .st-key-crypto_header div[data-testid="stVerticalBlock"] { gap: 0.15rem; }
                        .st-key-crypto_header div[data-testid="stElementContainer"] { margin: 0 !important; }
                        .st-key-crypto_header iframe { display: block; }
                        </style>
                        """,
                        unsafe_allow_html=True,
                    )
                    with st.container(key="crypto_header"):
                        col_title, col_pause, col_timer, col_button = st.columns([2.4, 1.3, 1, 1], gap="small")
                        with col_title:
                            st.markdown(f"<h3 style='margin:0'>{selected_crypto} · {selected_interval} · останні {timeframes_crypto}</h3>", unsafe_allow_html=True)
                        with col_pause:
                            st.toggle(
                                "⏸ Пауза",
                                key="market_autorefresh_paused",
                                help="Призупинити автооновлення кожні 30с, щоб не збивало зум/масштаб графіка.",
                                on_change=_force_full_rerun,
                            )
                        with col_timer:
                            try:
                                components.html(timer_html, height=36, scrolling=False)
                            except Exception:
                                try:
                                    st.markdown(timer_html, unsafe_allow_html=True)
                                except Exception:
                                    st.markdown("&nbsp;")
                        with col_button:
                            refresh_now_crypto = st.button("Оновити", key=f"market_refresh_button_crypto_{selected_crypto}")
                            if refresh_now_crypto:
                                twelve_history.clear()
                                cmc_history.clear()
                                cmc_quotes.clear()
                                twelve_quote.clear()
                                now_local = time.time()
                                secs = int(st.session_state.get("market_refresh_seconds", 30))
                                st.session_state["market_last_refresh"] = now_local
                                st.session_state["market_next_refresh"] = now_local + secs

                    # Отримуємо або кешуємо дані для оновлення ціни без втрати виду
                    crypto_points_key = f"points_crypto_{selected_crypto}"
                    points = None
                    try:
                        if TWELVE_DATA_API_KEY:
                            points = twelve_history(TWELVE_DATA_API_KEY, f"{selected_crypto}/USD", count=output_count, interval=interval_code)
                        else:
                            points = cmc_history(COINMARKETCAP_API_KEY, crypto_id, count=selected_days_crypto)
                        st.session_state[crypto_points_key] = points
                    except RuntimeError as error:
                        st.warning(f"Дані недоступні: {error}")
                        points = st.session_state.get(crypto_points_key)

                    if points:
                        try:
                            render_price_chart(
                                points,
                                "",
                                display_type,
                                key=f"crypto_price_chart_{selected_crypto}_{timeframes_crypto}_{selected_interval}",
                                interaction_mode="Pan (вільно)",
                                vertical_scale=vertical_scale_crypto,
                            )
                        except RuntimeError as error:
                            st.warning(f"Графік недоступний: {error}")
            except RuntimeError as error:
                st.warning(f"Дані CoinMarketCap недоступні: {error}")


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


def render_market_dashboard(category: str, watchlist: list[str]) -> None:
    if category not in ("Фінанси", "Криптовалюти", "Ілон Маск / компанії"):
        return
    paused = st.session_state.get("market_autorefresh_paused", False)
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

st.caption(f"Джерела для категорії: {', '.join(config['sources'])}")
run_analysis = st.button("🚀 Зібрати та проаналізувати", type="primary")

if run_analysis:
    with st.spinner("🚀 Завантажую статті та фотографії у кілька потоків..."):
        all_articles, problems = collect_articles(config["sources"], topic, hours)

    if not all_articles:
        st.session_state.pop("result", None)
        st.error("Не знайдено жодної новини. Спробуйте уточнити або змінити тему.")
        st.stop()

    # Обрізаємо кількість статей у промпті, щоб не впиратись у ліміт токенів
    # на хвилину навіть після виправлення квоти ключа.
    MAX_ARTICLES_FOR_ANALYSIS = 30
    raw_text = "\n---\n".join(
        f"Джерело: {article['source']}\nЗаголовок: {article['title']}\nОпис: {article['summary']}"
        for article in all_articles[:MAX_ARTICLES_FOR_ANALYSIS]
    )
    market_note = "\n4. **Можливий вплив на ринок** — коротко поясни можливий зв’язок новин із цінами чи настроями інвесторів." if category in ("Фінанси", "Криптовалюти") else ""

    analysis_text = None
    analysis_error = None
    with st.spinner("Створюю український аналіз..."):
        try:
            if not OPENROUTER_API_KEY:
                analysis_error = "Не знайдено OPENROUTER_API_KEY у файлі .env"
            else:
                client = OpenAI(
                    base_url="https://openrouter.ai/api/v1",
                    api_key=OPENROUTER_API_KEY,
                )

                prompt_text = f"""Ти — старший политичний та економічний аналітик. 
Категорія: {category}. Тема: {topic}. Відстежувані активи/компанії: {', '.join(watchlist) or 'не вказано'}.

Проведи максимально глибокий, критичний та розгорнутий аналіз наданих матеріалів українською мовою.

Матеріали:
{raw_text}

Надай детальний звіт за такою структурою:
1. **Глибокий контекст та Головна суть** — що саме сталося, передумови події та чому це важливо зараз (5–7 речень).
2. **Порівняльний аналіз та достовірність джерел** — як кожне джерело висвітлює подію, на чому робить акценти та які маніпуляції чи нарративи спостерігаються; для кожного джерела коротко оціни його достовірність і можливу упередженість (репутація видання, ознаки однобічної подачі саме в цих матеріалах).
3. **Причинно-наслідкові зв'язки** — як ця подія впливає на суміжні сфери (геополітику, економіку, ринки чи технологічний сектор).{market_note}
4. **Сліпі плями та приховані ризики** — що джерела замалчують або які питання залишаються без відповідей.
5. **Стратегічний прогноз** — короткострокові та довгострокові наслідки, 4–5 конкретних маркерів для подальшого спостереження.
"""
                completion = client.chat.completions.create(
                    model="openrouter/free",
                    messages=[{"role": "user", "content": prompt_text}],
                    max_tokens=3500,
                    extra_body={
                        "models": [
                            "qwen/qwen3-next-80b-a3b-instruct:free",
                            "openai/gpt-oss-20b:free"
                        ]
                    }
                )
                analysis_text = completion.choices[0].message.content
        except Exception as error:
            analysis_error = str(error)

    # Зберігаємо все у session_state: перемикання графіка, автооновлення
    # цін чи будь-який інший віджет перезапускає скрипт Streamlit «з нуля»,
    # а st.button() на такому перезапуску знову False — без цього все
    # зібране (статті й готовий аналіз) просто зникало б з екрана.
    st.session_state["result"] = {
        "category": category,
        "watchlist": watchlist,
        "all_articles": all_articles,
        "problems": problems,
        "analysis_text": analysis_text,
        "analysis_error": analysis_error,
    }

if "result" in st.session_state:
    result = st.session_state["result"]
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

    render_market_dashboard(r_category, result["watchlist"])

    left_articles, analysis_column, right_articles = st.columns((1.6, 2, 1.6), gap="large")
    with left_articles:
        st.subheader(f"📚 Статті ({len(r_articles[::2])})")
        for index, article in enumerate(r_articles[::2]):
            render_article_card(article, r_category, f"left_{index}_{article['link']}")

    with right_articles:
        st.subheader(f"📚 Статті ({len(r_articles[1::2])})")
        for index, article in enumerate(r_articles[1::2]):
            render_article_card(article, r_category, f"right_{index}_{article['link']}")

    with analysis_column:
        if result["analysis_error"]:
            st.error(f"Помилка аналізу: {result['analysis_error']}")
        elif result["analysis_text"]:
            st.success("Аналіз готовий!")
            st.markdown(result["analysis_text"])
            if r_category in ("Фінанси", "Криптовалюти"):
                st.info("Це аналіз новин, а не інвестиційна порада.")