#!/usr/bin/env python3
from __future__ import annotations

import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

FEEDS = [
    {"name": "OpenAI", "kind": "rss", "url": "https://openai.com/news/rss.xml"},
    {"name": "Anthropic", "kind": "anthropic_newsroom", "url": "https://www.anthropic.com/news"},
    {"name": "Google DeepMind", "kind": "rss", "url": "https://blog.google/technology/ai/rss/"},
    {"name": "TechCrunch AI", "kind": "rss", "url": "https://techcrunch.com/category/artificial-intelligence/feed/"},
    {"name": "Hugging Face", "kind": "rss", "url": "https://huggingface.co/blog/feed.xml"},
]

OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
ROOT = Path(__file__).resolve().parent


@dataclass
class Config:
    telegram_bot_token: str
    telegram_chat_id: str
    openai_api_key: str
    openai_model: str
    timezone_name: str
    lookback_hours: int
    max_items: int


@dataclass
class NewsItem:
    source: str
    title: str
    link: str
    published_at: datetime
    snippet: str


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value else default


def load_config() -> Config:
    required = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "OPENAI_API_KEY"]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise SystemExit(f"Missing required environment variables: {', '.join(missing)}")

    return Config(
        telegram_bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
        telegram_chat_id=os.environ["TELEGRAM_CHAT_ID"],
        openai_api_key=os.environ["OPENAI_API_KEY"],
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
        timezone_name=os.getenv("DIGEST_TIMEZONE", "Asia/Tbilisi"),
        lookback_hours=env_int("DIGEST_LOOKBACK_HOURS", 30),
        max_items=env_int("DIGEST_MAX_ITEMS", 12),
    )


def fetch_url(url: str, *, data: bytes | None = None, headers: dict[str, str] | None = None, method: str = "GET") -> bytes:
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "User-Agent": "ai-news-telegram/1.0 (+https://openai.com)",
            **(headers or {}),
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def clean_html(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def canonicalize_link(url: str) -> str:
    if not url.strip():
        return ""
    parts = urllib.parse.urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return url.strip()

    filtered_query = urllib.parse.urlencode(
        [
            (key, value)
            for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
            if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid"}
        ]
    )
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc.lower(), parts.path.rstrip("/") or "/", filtered_query, "")
    )


def parse_datetime(value: str) -> datetime:
    if not value:
        return datetime.now(timezone.utc)

    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        pass

    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)


def parse_rss_items(source: str, root: ET.Element) -> list[NewsItem]:
    channel = root.find("channel")
    if channel is None:
        return []

    items: list[NewsItem] = []
    for item in channel.findall("item"):
        title = clean_html(item.findtext("title", ""))
        link = canonicalize_link(item.findtext("link", ""))
        snippet = clean_html(item.findtext("description", ""))
        published = parse_datetime(item.findtext("pubDate", ""))
        if title and link:
            items.append(NewsItem(source, title, link, published, snippet))
    return items


def parse_atom_items(source: str, root: ET.Element) -> list[NewsItem]:
    namespace = root.tag.split("}", 1)[0] + "}" if root.tag.startswith("{") else ""
    items: list[NewsItem] = []
    for entry in root.findall(f"{namespace}entry"):
        title = clean_html(entry.findtext(f"{namespace}title", ""))
        snippet = clean_html(entry.findtext(f"{namespace}summary", "") or entry.findtext(f"{namespace}content", ""))
        published = parse_datetime(entry.findtext(f"{namespace}updated", "") or entry.findtext(f"{namespace}published", ""))
        link = ""
        for link_node in entry.findall(f"{namespace}link"):
            href = link_node.attrib.get("href", "")
            rel = link_node.attrib.get("rel", "alternate")
            if href and rel == "alternate":
                link = canonicalize_link(href)
                break
        if title and link:
            items.append(NewsItem(source, title, link, published, snippet))
    return items


def fetch_feed_items(source: str, url: str) -> list[NewsItem]:
    try:
        payload = fetch_url(
            url,
            headers={
                "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.8"
            },
        )
        root = ET.fromstring(payload)
    except (urllib.error.URLError, ET.ParseError) as exc:
        print(f"[warn] Failed to fetch {source}: {exc}", file=sys.stderr)
        return []

    tag = root.tag.lower()
    return parse_rss_items(source, root) if tag.endswith("rss") or tag.endswith("rdf") else parse_atom_items(source, root)


def parse_anthropic_article(article_url: str) -> NewsItem | None:
    try:
        payload = fetch_url(article_url).decode("utf-8", "ignore")
    except urllib.error.URLError as exc:
        print(f"[warn] Failed to fetch Anthropic article {article_url}: {exc}", file=sys.stderr)
        return None

    title_match = re.search(r"<title>(.*?)</title>", payload, re.IGNORECASE | re.DOTALL)
    description_match = re.search(
        r'<meta name="description" content="([^"]+)"', payload, re.IGNORECASE | re.DOTALL
    )
    date_match = re.search(r"([A-Z][a-z]{2} \d{1,2}, \d{4})", payload)
    if not title_match or not date_match:
        return None

    title = clean_html(title_match.group(1)).removesuffix(" \\ Anthropic")
    snippet = clean_html(description_match.group(1)) if description_match else ""
    published = datetime.strptime(date_match.group(1), "%b %d, %Y").replace(tzinfo=timezone.utc)
    return NewsItem("Anthropic", title, canonicalize_link(article_url), published, snippet)


def fetch_anthropic_newsroom(url: str) -> list[NewsItem]:
    try:
        payload = fetch_url(url).decode("utf-8", "ignore")
    except urllib.error.URLError as exc:
        print(f"[warn] Failed to fetch Anthropic newsroom: {exc}", file=sys.stderr)
        return []

    seen_links: set[str] = set()
    article_urls: list[str] = []
    for match in re.finditer(r'href="(/news/[^"]+)"', payload):
        link = canonicalize_link(f"https://www.anthropic.com{match.group(1)}")
        if link in seen_links:
            continue
        seen_links.add(link)
        article_urls.append(link)
        if len(article_urls) >= 10:
            break

    items: list[NewsItem] = []
    for article_url in article_urls:
        item = parse_anthropic_article(article_url)
        if item is not None:
            items.append(item)
    return items


def dedupe_items(items: list[NewsItem]) -> list[NewsItem]:
    deduped: dict[str, NewsItem] = {}
    for item in items:
        current = deduped.get(item.link)
        if current is None or item.published_at > current.published_at:
            deduped[item.link] = item
    return sorted(deduped.values(), key=lambda item: item.published_at, reverse=True)


def collect_items(config: Config) -> list[NewsItem]:
    now_utc = datetime.now(timezone.utc)
    cutoff = now_utc - timedelta(hours=config.lookback_hours)
    items: list[NewsItem] = []

    for feed in FEEDS:
        if feed["kind"] == "rss":
            items.extend(fetch_feed_items(feed["name"], feed["url"]))
        elif feed["kind"] == "anthropic_newsroom":
            items.extend(fetch_anthropic_newsroom(feed["url"]))

    deduped = dedupe_items(items)
    fresh = [item for item in deduped if item.published_at >= cutoff]
    return fresh[: config.max_items]


def extract_response_text(payload: dict) -> str:
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text

    chunks: list[str] = []
    for item in payload.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"} and content.get("text"):
                chunks.append(content["text"])
    return "\n".join(chunks).strip()


def build_model_input(items: list[NewsItem]) -> str:
    payload = [
        {
            "source": item.source,
            "title": item.title,
            "snippet": item.snippet[:700],
            "published_at": item.published_at.isoformat(),
        }
        for item in items
    ]
    return json.dumps(payload, ensure_ascii=False)


def generate_digest(items: list[NewsItem], config: Config) -> str:
    today = datetime.now().strftime("%d.%m.%Y")
    if not items:
        return f"AI дайджест — {today}\n\nСегодня в отслеживаемых источниках заметных новых обновлений не нашлось."

    schema = {
        "type": "object",
        "properties": {
            "headline": {"type": "string"},
            "bullets": {"type": "array", "items": {"type": "string"}},
            "closing": {"type": "string"},
        },
        "required": ["headline", "bullets", "closing"],
        "additionalProperties": False,
    }

    body = json.dumps(
        {
            "model": config.openai_model,
            "instructions": (
                "Ты редактор ежедневного AI-дайджеста на русском языке. "
                "На входе список новостей из индустрии ИИ. "
                "Собери из них короткий, живой, информативный пересказ для Telegram. "
                "Не переводи дословно. Сожми повторы. Выдели главное: релизы моделей, продуктовые апдейты, сделки, исследования, увольнения, рынок, дизайн и dev-tools. "
                "Пиши просто и по делу, без воды. Не добавляй ссылки, не выдумывай факты. "
                "Сделай 5-8 пунктов максимум. Каждый пункт 1-2 предложения."
            ),
            "input": build_model_input(items),
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "telegram_digest",
                    "schema": schema,
                    "strict": True,
                }
            },
        },
        ensure_ascii=False,
    ).encode("utf-8")

    response_payload = json.loads(
        fetch_url(
            OPENAI_RESPONSES_URL,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {config.openai_api_key}",
                "Content-Type": "application/json",
            },
        ).decode("utf-8")
    )
    parsed = json.loads(extract_response_text(response_payload))

    lines = [parsed["headline"].strip(), ""]
    for bullet in parsed["bullets"][:8]:
        text = re.sub(r"\s+", " ", bullet).strip()
        if text:
            lines.append(f"• {text}")
    closing = re.sub(r"\s+", " ", parsed["closing"]).strip()
    if closing:
        lines.extend(["", closing])

    message = "\n".join(lines).strip()
    return message[:3900]


def send_telegram_message(config: Config, message: str) -> None:
    url = f"https://api.telegram.org/bot{config.telegram_bot_token}/sendMessage"
    payload = json.dumps(
        {
            "chat_id": config.telegram_chat_id,
            "text": message,
            "disable_web_page_preview": True,
        },
        ensure_ascii=False,
    ).encode("utf-8")

    response = json.loads(
        fetch_url(
            url,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        ).decode("utf-8")
    )
    if not response.get("ok"):
        raise RuntimeError(f"Telegram API error: {response}")


def main() -> None:
    config = load_config()
    items = collect_items(config)
    message = generate_digest(items, config)
    send_telegram_message(config, message)
    print(f"[info] Sent Telegram digest with {len(items)} source item(s).", file=sys.stderr)


if __name__ == "__main__":
    main()
