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
from zoneinfo import ZoneInfo

FEEDS = [
    {"name": "OpenAI", "kind": "rss", "url": "https://openai.com/news/rss.xml"},
    {"name": "Anthropic", "kind": "anthropic_newsroom", "url": "https://www.anthropic.com/news"},
    {"name": "Google DeepMind", "kind": "rss", "url": "https://blog.google/technology/ai/rss/"},
    {"name": "TechCrunch AI", "kind": "rss", "url": "https://techcrunch.com/category/artificial-intelligence/feed/"},
    {"name": "Hugging Face", "kind": "rss", "url": "https://huggingface.co/blog/feed.xml"},
]

OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "digest_state.json"


@dataclass
class Config:
    telegram_bot_token: str
    telegram_chat_id: str
    openai_api_key: str
    openai_model: str
    timezone_name: str
    lookback_hours: int
    max_items: int
    state_limit: int
    force_send: bool
    mode: str


@dataclass
class NewsItem:
    source: str
    title: str
    link: str
    published_at: datetime
    snippet: str

    @property
    def fingerprint(self) -> str:
        raw = f"{self.source}|{self.link}".encode("utf-8")
        import hashlib

        return hashlib.sha256(raw).hexdigest()


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
        max_items=env_int("DIGEST_MAX_ITEMS", 8),
        state_limit=env_int("DIGEST_STATE_LIMIT", 800),
        force_send=os.getenv("DIGEST_FORCE_SEND", "").lower() in {"1", "true", "yes"},
        mode=os.getenv("DIGEST_MODE", "brief").lower(),
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


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"sent_fingerprints": []}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"sent_fingerprints": []}


def local_today(config: Config) -> str:
    return local_now(config).date().isoformat()


def local_now(config: Config) -> datetime:
    try:
        tz = ZoneInfo(config.timezone_name)
    except Exception:  # noqa: BLE001
        tz = timezone.utc
    return datetime.now(tz)


def is_scheduled_run() -> bool:
    return os.getenv("GITHUB_EVENT_NAME") == "schedule"


def is_cronjob_run() -> bool:
    return os.getenv("DIGEST_TRIGGER", "").lower() == "cronjob"


def should_send_once_per_day(config: Config) -> bool:
    return not config.force_send and (is_scheduled_run() or is_cronjob_run())


def state_date_key(config: Config) -> str:
    return {
        "brief": "last_digest_date",
        "analysis": "last_analysis_date",
        "rubric": "last_rubric_date",
    }[config.mode]


def mode_already_sent_today(config: Config) -> bool:
    return load_state().get(state_date_key(config)) == local_today(config)


def save_state(config: Config, items: list[NewsItem], *, mark_sent: bool = False) -> None:
    state = load_state()
    existing = state.get("sent_fingerprints", [])
    merged = existing + ([item.fingerprint for item in items] if config.mode == "rubric" else [])
    payload = {
        **state,
        "sent_fingerprints": merged[-config.state_limit :],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if mark_sent:
        today = local_today(config)
        payload[state_date_key(config)] = today
        if config.mode == "brief":
            payload["last_scheduled_digest_date"] = today
    STATE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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
    seen = set(load_state().get("sent_fingerprints", []))
    recent = [item for item in deduped if item.published_at >= cutoff]
    # The analysis intentionally revisits the strongest news from the morning brief.
    fresh = recent if config.mode == "analysis" else [item for item in recent if item.fingerprint not in seen]
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


def bonus_rubric_for_today(config: Config) -> str:
    forced = os.getenv("DIGEST_BONUS_RUBRIC")
    if forced:
        return forced
    weekday = local_now(config).weekday()
    schedule = {
        0: "SIGNAL",
        2: "DEAD INTERNET REPORT",
        4: "FUTURE JOBS / EXTINCT JOBS",
    }
    return schedule.get(weekday, "NONE")


def build_model_input(items: list[NewsItem], bonus_rubric: str) -> str:
    payload = {
        "bonus_rubric": bonus_rubric,
        "mode": os.getenv("DIGEST_MODE", "brief").lower(),
        "items": [
            {
                "source": item.source,
                "title": item.title,
                "snippet": item.snippet[:700],
                "published_at": item.published_at.isoformat(),
            }
            for item in items
        ],
    }
    return json.dumps(payload, ensure_ascii=False)


def pick_section_emoji(title: str) -> str:
    normalized = title.lower()
    if "model" in normalized or "модел" in normalized or "релиз" in normalized:
        return "🧠"
    if "creative" in normalized or "design" in normalized or "дизайн" in normalized or "креатив" in normalized or "визуал" in normalized:
        return "🎨"
    if "product" in normalized or "agent" in normalized or "продукт" in normalized or "агент" in normalized:
        return "🛠️"
    if "business" in normalized or "money" in normalized or "рын" in normalized or "бизнес" in normalized or "сделк" in normalized:
        return "💼"
    if "research" in normalized or "медицин" in normalized or "наук" in normalized or "исслед" in normalized:
        return "🧬"
    if "policy" in normalized or "society" in normalized or "регули" in normalized or "обще" in normalized:
        return "⚖️"
    return "✨"


def format_bonus_post(parsed: dict) -> str:
    bonus = parsed.get("bonus_post", {})
    if not bonus.get("enabled"):
        return ""

    rubric = re.sub(r"\s+", " ", bonus.get("rubric", "")).strip()
    title = re.sub(r"\s+", " ", bonus.get("title", "")).strip()
    body = bonus.get("body", [])
    humanity_status = re.sub(r"\s+", " ", bonus.get("humanity_status", "")).strip()
    if not rubric or not title or not body:
        return ""

    lines = [f"<b>{html.escape(rubric)} // {html.escape(title)}</b>"]
    for paragraph in body[:4]:
        text = re.sub(r"\s+", " ", paragraph).strip()
        if text:
            lines.extend(["", html.escape(text)])
    if humanity_status:
        humanity_status = re.sub(r"^HUMANITY STATUS:\s*", "", humanity_status, flags=re.IGNORECASE)
        lines.extend(["", f"<b>HUMANITY STATUS:</b> {html.escape(humanity_status)}"])
    return "\n".join(lines).strip()[:3900]


def format_analysis_post(parsed: dict) -> str:
    analysis = parsed.get("analysis_post", {})
    if not analysis.get("enabled"):
        return ""

    title = re.sub(r"\s+", " ", analysis.get("title", "")).strip()
    lead = re.sub(r"\s+", " ", analysis.get("lead", "")).strip()
    if not title or not lead:
        return ""

    lines = [f"<b>РАЗБОР ДНЯ // {html.escape(title)}</b>", "", html.escape(lead)]
    fields = [
        ("Что произошло", "what_happened"),
        ("Почему это важно сейчас", "why_it_matters"),
        ("Кого это касается", "who_it_affects"),
        ("Что дальше", "what_next"),
        ("Без хайпа", "reality_check"),
    ]
    for label, key in fields:
        text = re.sub(r"\s+", " ", analysis.get(key, "")).strip()
        if text:
            lines.extend(["", f"<b>{label}:</b> {html.escape(text)}"])

    humanity_status = re.sub(r"\s+", " ", analysis.get("humanity_status", "")).strip()
    if humanity_status:
        humanity_status = re.sub(r"^HUMANITY STATUS:\s*", "", humanity_status, flags=re.IGNORECASE)
        lines.extend(["", f"<b>HUMANITY STATUS:</b> {html.escape(humanity_status)}"])
    return "\n".join(lines).strip()[:3900]


def generate_message_payload(items: list[NewsItem], config: Config) -> dict | None:
    try:
        today = local_now(config).strftime("%d.%m.%Y")
    except Exception:  # noqa: BLE001
        today = datetime.now().strftime("%d.%m.%Y")
    if not items:
        return None

    bonus_rubric = bonus_rubric_for_today(config) if config.mode == "rubric" else "NONE"

    schema = {
        "type": "object",
        "properties": {
            "headline": {"type": "string"},
            "intro": {"type": "string"},
            "sections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "items": {"type": "array", "items": {"type": "string"}},
                        "takeaway": {"type": "string"},
                    },
                    "required": ["title", "items", "takeaway"],
                    "additionalProperties": False,
                },
            },
            "closing": {"type": "string"},
            "humanity_status": {"type": "string"},
            "bonus_post": {
                "type": "object",
                "properties": {
                    "enabled": {"type": "boolean"},
                    "rubric": {"type": "string"},
                    "title": {"type": "string"},
                    "body": {"type": "array", "items": {"type": "string"}},
                    "humanity_status": {"type": "string"},
                },
                "required": ["enabled", "rubric", "title", "body", "humanity_status"],
                "additionalProperties": False,
            },
            "analysis_post": {
                "type": "object",
                "properties": {
                    "enabled": {"type": "boolean"},
                    "title": {"type": "string"},
                    "lead": {"type": "string"},
                    "what_happened": {"type": "string"},
                    "why_it_matters": {"type": "string"},
                    "who_it_affects": {"type": "string"},
                    "what_next": {"type": "string"},
                    "reality_check": {"type": "string"},
                    "humanity_status": {"type": "string"},
                },
                "required": [
                    "enabled",
                    "title",
                    "lead",
                    "what_happened",
                    "why_it_matters",
                    "who_it_affects",
                    "what_next",
                    "reality_check",
                    "humanity_status",
                ],
                "additionalProperties": False,
            },
        },
        "required": [
            "headline",
            "intro",
            "sections",
            "closing",
            "humanity_status",
            "bonus_post",
            "analysis_post",
        ],
        "additionalProperties": False,
    }

    body = json.dumps(
        {
            "model": config.openai_model,
            "instructions": (
                "Ты редактор Telegram-канала AI Apocalypse Daily / Goodbye, человечество. "
                "Тон: умный друг рассказывает AI-новости за кофе. Живо, понятно, с лёгкой иронией и чёрным юмором, но без клоунады. "
                "Не звучать как пресс-релиз, корпоративный отчёт, лекция, аналитическая записка или техно-бро тред. "
                "Пиши простыми словами и короткими фразами. Если предложение можно сделать короче, сделай короче. "
                "На входе список свежих AI-новостей и поле mode. mode может быть brief, analysis или rubric. "
                "Не пиши от лица Ирины и не рассказывай про автоматизацию канала. Пиши как живой наблюдатель, которому не всё равно. "
                "Если mode = brief, собери короткий DAILY AI BRIEFING на русском языке. "
                "Отбери до 8 главных новостей, сожми повторы и объясни, что произошло человеческим языком. "
                "Используй только эти секции, если для них реально есть новости: Models, Products / Agents, Creative AI, Business / Money, Research, Policy / Society. "
                "Не используй Scary / Weird Future как секцию ежедневного выпуска; странные AI-кейсы оставь для отдельной рубрики и упоминай здесь только если это важная новость в Policy / Society. "
                "Сделай 2-5 секций максимум. В каждой секции 1-3 пункта. Каждый пункт максимум 1 короткое предложение, без длинных объяснений. "
                "Для каждой секции заполни takeaway: 1-2 коротких предложения с анализом, что меняется, на кого это влияет и чего ждать. Не повторяй новости из пунктов. "
                "Headline должен быть ровно в формате: AI APOCALYPSE DAILY // утренняя сводка — " + today + ". "
                "Intro сделай одной короткой дружелюбной строкой с настроением дня, без приветствий и пафоса. "
                "Closing оставь пустым, если нет очень короткой смешной финальной фразы. "
                "В humanity_status дай острый короткий панчлайн про людей, работу, технологии, интернет, дизайн или будущее. "
                "Это должна быть настоящая шутка с неожиданным поворотом, а не милое наблюдение и не отчёт. Максимум 140 символов. "
                "Допустим лёгкий чёрный юмор про AI-индустрию, карьеру, капитализм, контроль и человеческое отрицание. Не шути про реальные трагедии и конкретных уязвимых людей. "
                "Примеры направления, не копируй их дословно: 'ИИ пока не отнял вашу работу. Он просто участвует в собеседовании на неё'; "
                "'мы всё ещё принимаем решения сами. Просто после рекомендации алгоритма'; "
                "'люди создали машину, чтобы экономить время, и теперь круглосуточно читают её обновления'. "
                "Если mode не brief, верни пустые headline, intro, sections, closing и humanity_status. "
                "Если mode = analysis, выбери одну самую важную новость дня и создай analysis_post. "
                "analysis_post должен быть отдельным понятным разбором: что произошло; почему это важно именно сейчас; на кого повлияет; что может произойти дальше; где реальное изменение, а где маркетинговый шум. "
                "Не пересказывай пресс-релиз и не раздувай значение новости. Пиши как умный друг, который прочитал всё и теперь объясняет главное. "
                "Каждое поле analysis_post — один короткий абзац. Заголовок конкретный и цепкий. reality_check — честный вывод без хайпа. "
                "Для analysis_post тоже создай новый острый humanity_status. Если mode не analysis, верни analysis_post.enabled=false и пустые строки во всех остальных полях analysis_post. "
                "В поле bonus_post создай отдельный короткий пост, только если bonus_rubric не NONE. "
                "Сегодня bonus_rubric: " + bonus_rubric + ". "
                "Если mode не rubric или bonus_rubric = NONE, верни bonus_post.enabled=false и пустые строки/массив в остальных полях bonus_post. "
                "Если bonus_rubric = SIGNAL, сделай короткое наблюдение о том, что новости дня значат для культуры, дизайна, профессий, small business или интернета. Не объясняй как учебник, формулируй как один острый вывод. "
                "Если bonus_rubric = DEAD INTERNET REPORT, сделай отдельный пост про странные проявления AI-интернета, синтетический контент, автоматизированные медиа, дипфейки или ощущение, что интернет всё меньше похож на человеческое место. "
                "Если bonus_rubric = FUTURE JOBS / EXTINCT JOBS, сделай отдельный пост о том, какие задачи или профессии меняются из-за новостей дня: что исчезает, что остаётся человеческим, какие новые задачи появляются. "
                "bonus_post должен быть коротким: 2-3 абзаца, живым языком, без списка инструментов и без советов 'как пользоваться'. Если по новостям дня рубрику невозможно сделать честно, поставь enabled=false. "
                "Запрещённые скучные слова и обороты: 'важный шаг', 'новый этап', 'на фоне', 'экосистема', 'вызовы', 'трансформация', 'революция', 'изменит всё'. "
                "Не добавляй ссылки и не выдумывай факты."
            ),
            "input": build_model_input(items, bonus_rubric),
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
    return parsed


def format_brief_message(parsed: dict) -> str:

    lines = [f"<b>{html.escape(parsed['headline'].strip())}</b>"]
    intro = re.sub(r"\s+", " ", parsed["intro"]).strip()
    if intro:
        lines.extend(["", html.escape(intro)])
    for section in parsed["sections"][:5]:
        title = re.sub(r"\s+", " ", section["title"]).strip()
        items_in_section = section.get("items", [])[:3]
        if not title or not items_in_section:
            continue
        emoji = pick_section_emoji(title)
        lines.extend(["", f"<b>{emoji} {html.escape(title)}</b>"])
        for bullet in items_in_section:
            text = re.sub(r"\s+", " ", bullet).strip()
            if text:
                lines.append(f"▪️ {html.escape(text)}")
        takeaway = re.sub(r"\s+", " ", section.get("takeaway", "")).strip()
        if takeaway:
            lines.extend(["", f"<b>Что это меняет:</b> {html.escape(takeaway)}"])
    closing = re.sub(r"\s+", " ", parsed["closing"]).strip()
    if closing:
        lines.extend(["", html.escape(closing)])
    humanity_status = re.sub(r"\s+", " ", parsed["humanity_status"]).strip()
    if humanity_status:
        humanity_status = re.sub(r"^HUMANITY STATUS:\s*", "", humanity_status, flags=re.IGNORECASE)
        lines.extend(["", f"<b>HUMANITY STATUS:</b> {html.escape(humanity_status)}"])

    return "\n".join(lines).strip()[:3900]


def send_telegram_message(config: Config, message: str) -> None:
    url = f"https://api.telegram.org/bot{config.telegram_bot_token}/sendMessage"
    payload = json.dumps(
        {
            "chat_id": config.telegram_chat_id,
            "text": message,
            "disable_web_page_preview": True,
            "parse_mode": "HTML",
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
    mark_sent = should_send_once_per_day(config)
    if config.mode not in {"brief", "analysis", "rubric"}:
        raise SystemExit("DIGEST_MODE must be 'brief', 'analysis', or 'rubric'.")
    if mark_sent and mode_already_sent_today(config):
        print(f"[info] {config.mode} already sent today. Skipping.", file=sys.stderr)
        return

    items = collect_items(config)
    parsed = generate_message_payload(items, config)
    if not parsed:
        print("[info] No fresh items. Nothing to send.", file=sys.stderr)
        return

    formatters = {
        "brief": format_brief_message,
        "analysis": format_analysis_post,
        "rubric": format_bonus_post,
    }
    message = formatters[config.mode](parsed)
    if not message:
        print(f"[info] No {config.mode} message generated. Nothing to send.", file=sys.stderr)
        return

    send_telegram_message(config, message)
    if items or mark_sent:
        save_state(config, items, mark_sent=mark_sent)
    print(f"[info] Sent Telegram {config.mode} with {len(items)} source item(s).", file=sys.stderr)


if __name__ == "__main__":
    main()
