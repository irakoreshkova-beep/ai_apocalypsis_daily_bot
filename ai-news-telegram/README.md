# Telegram AI Digest

Минимальный ежедневный AI-дайджест в Telegram:

- `GitHub Actions` сейчас временно запускается каждые 5 минут для диагностики schedule
- после проверки расписание нужно вернуть на ежедневный утренний запуск
- скрипт защищен от дублей и отправляет не больше одного scheduled-дайджеста в день
- [daily_digest.py](/Users/irakoreshkova/Documents/New project/ai-news-telegram/daily_digest.py:1) собирает AI-новости из RSS/newsroom-источников
- OpenAI API делает короткий пересказ на русском
- Telegram Bot API отправляет итог в ваш чат или канал

## Что нужно в GitHub Secrets

- `OPENAI_API_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

## Как получить `TELEGRAM_CHAT_ID`

Если это личный чат с ботом:

1. Напишите что-нибудь вашему боту в Telegram.
2. Откройте в браузере:

```text
https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/getUpdates
```

3. В ответе найдите `chat` → `id`.

Если это канал:

- добавьте бота в канал как администратора
- используйте chat id канала, обычно вида `-100...`

## Ручной тест

1. Залейте [daily_digest.py](/Users/irakoreshkova/Documents/New project/ai-news-telegram/daily_digest.py:1), [digest_state.json](/Users/irakoreshkova/Documents/New project/ai-news-telegram/digest_state.json:1) и [telegram-ai-digest.yml](/Users/irakoreshkova/Documents/New project/.github/workflows/telegram-ai-digest.yml:1) в репозиторий.
2. Добавьте secrets.
3. Откройте `Actions` → `Telegram AI Digest` → `Run workflow`.
4. Проверьте, пришло ли сообщение в Telegram.

## Оценка стоимости

С учетом коротких RSS/snippet-входов и одного короткого ежедневного ответа моделью `gpt-4.1-mini`, стоимость обычно остается очень низкой — порядок центов в месяц, а не долларов в день.

Официальные материалы:

- OpenAI Responses API: [Text generation](https://platform.openai.com/docs/guides/text?api-mode=responses%5C)
- OpenAI pricing: [Pricing](https://platform.openai.com/docs/pricing/)
- GitHub Actions schedule: [Workflow syntax](https://docs.github.com/en/actions/writing-workflows/workflow-syntax-for-github-actions?source=post_page-----b008ed5f3edc--------------------------------)
