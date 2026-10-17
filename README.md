# GitHub Steam Status

Динамическая SVG-карточка со статусом Steam для вставки в `README.md` на GitHub.
Показывает аватар, ник, статус (Online / In-Game / Away / Offline) с пульсирующим индикатором,
уровень, количество игр, общее время и часы за последние 2 недели.

Стек: **FastAPI + httpx**, деплой на **Vercel (Serverless Function)**.

## Структура

```
.
├── api/index.py      # точка входа Vercel (импортирует app из main.py)
├── main.py           # FastAPI-приложение, запросы к Steam, генератор SVG
├── requirements.txt
├── vercel.json
├── .env.example
└── README.md
```

## Подготовка

1. Получите Steam Web API Key: https://steamcommunity.com/dev/apikey
2. В настройках приватности Steam поставьте **Public** для «My profile» и «Game details»
   (иначе статистика игр будет скрыта и в карточке появятся прочерки).
3. Ваш SteamID64 (17 цифр) можно узнать на https://steamid.io, либо использовать
   имя из своей кастомной ссылки профиля (`steamcommunity.com/id/<имя>`).

## Локальный запуск

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # впишите STEAM_API_KEY
uvicorn main:app --reload
```

Откройте: http://127.0.0.1:8000/api/widget?steam_id=YOUR_STEAM_ID&theme=dark

## Деплой на Vercel

```bash
npm i -g vercel
vercel login
vercel                           # первый деплой (preview)
vercel env add STEAM_API_KEY     # вставьте ключ (Production, Preview, Development)
vercel --prod
```

Либо импортируйте репозиторий на https://vercel.com/new и добавьте переменную окружения
`STEAM_API_KEY` в Settings → Environment Variables, затем сделайте Redeploy.

## Использование в README

```markdown
[![Steam Status](https://YOUR-APP.vercel.app/api/widget?steam_id=YOUR_STEAM_ID&theme=dark)](https://steamcommunity.com/profiles/YOUR_STEAM_ID)
```

## Параметры

| Параметр   | Описание                                                       | По умолчанию |
|------------|----------------------------------------------------------------|--------------|
| `steam_id` | SteamID64 (17 цифр) или имя из кастомной ссылки профиля        | обязателен   |
| `theme`    | `dark`, `dracula`, `nord`, `cyberpunk`                         | `dark`       |

## Кеширование и ошибки

- Успешный ответ: `Cache-Control: public, max-age=300, s-maxage=300` — карточка обновляется раз в 5 минут,
  Steam API не спамится.
- Ошибки (неверный ID, недоступен Steam API, не задан ключ) возвращаются как SVG со статусом HTTP 200
  и кешем 60 секунд, чтобы сообщение было видно в README, а не сломанная картинка.
- Аватар встраивается в SVG как `data:` URI, потому что GitHub не загружает внешние ресурсы из SVG.

## Эндпоинты

- `GET /api/widget` — SVG-карточка
- `GET /api/health` — проверка работоспособности
- `GET /` — краткая справка
