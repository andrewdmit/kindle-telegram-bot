# Kindle Telegram Bot

Telegram-бот для отправки электронных книг на Kindle через email.

## Возможности

- Регистрация пользователя с указанием Kindle email
- Приём файлов книг (PDF, EPUB, MOBI, AZW и др.), можно несколько сразу
- Отправка книг на Kindle через SMTP
- Хранение настроек пользователей в SQLite
- Интерфейс на HTML-виджетах Telegram (Bot API 10.4) прямо в чате

## Виджеты

С ноября 2026 года Telegram позволяет ботам показывать интерактивные HTML-виджеты
внутри сообщений ([документация](https://core.telegram.org/bots/widgets)). Бот использует три:

| Виджет | Файл | Что делает |
|--------|------|------------|
| Регистрация | `widgets/registration.html` | Шаги: Kindle email → разрешить отправителя в Amazon (копирование адреса, ссылка) → тестовый документ |
| Приём книг | `widgets/inbox.html` | Копит присланные файлы в одной карточке, показывает формат, размер и ошибки, убирает лишнее, переключает конвертацию PDF, отправляет одной кнопкой |
| Прогресс | `widgets/progress.html` | Этапы по каждому файлу (скачивание → письмо → SMTP), общий прогресс, таймер, повтор неудачных |

Ограничения платформы, которые повлияли на дизайн:

- в виджете нет полей ввода и он не принимает файлы, поэтому email и книги по-прежнему
  присылаются обычными сообщениями, а виджет показывает состояние и даёт кнопки;
- в продакшене виджеты работают только в личном чате с ботом;
- HTML виджета не больше 4096 байт после zlib (сейчас 3.7–4.0 КБ, лимит проверяет тест),
  состояние — до 1024 байт, поэтому длинные списки сокращаются автоматически.

Если виджеты недоступны (`WIDGETS_ENABLED=false` или Telegram отклонил `sendWidget`),
бот работает в прежнем текстовом режиме.

Библиотека python-telegram-bot ещё не знает о виджетах, поэтому методы вызываются
через `Bot.do_api_request` (см. `widget_api.py`), а апдейты `widget_query` берутся из `api_kwargs`.

## Требования

- Ubuntu VPS (или другой Linux)
- Токен Telegram-бота от [@BotFather](https://t.me/BotFather)
- SMTP-аккаунт для отправки писем (Gmail, Yandex, Mail.ru и т.д.)
- Kindle email пользователя (например, `name@kindle.com`)

## Настройка Amazon

Каждый пользователь должен добавить **адрес отправителя бота** в список одобренных в Amazon:

1. Откройте [Amazon Content & Devices](https://www.amazon.com/hz/mycd/digital-console/contentdevices/alldevices)
2. Перейдите в **Preferences** → **Personal Document Settings**
3. В **Approved Personal Document E-mail List** добавьте `SMTP_FROM_EMAIL` из вашего `.env`
4. Убедитесь, что **Send-to-Kindle Email** вашего устройства совпадает с тем, что указан в боте

## Установка на VPS

### Одной командой с вашего компьютера

Нужен SSH-доступ к серверу (root или пользователь с sudo):

```bash
./deploy.sh root@your-vps
```

Скрипт скопирует код, установит зависимости и systemd-сервис, при первой установке
откроет `.env` в nano прямо на сервере, затем перезапустит бота и покажет статус.
Нестандартный порт SSH: `SSH_PORT=2222 ./deploy.sh user@your-vps`.

Если бот раньше работал на другом сервере, остановите его там (или удалите старый
токен), иначе два процесса будут бороться за обновления (`Conflict: terminated by other getUpdates request`).
Чтобы сохранить пользователей, перенесите старую базу `/opt/kindle-telegram-bot/data/users.db`
на новый сервер до первого запуска: новые таблицы добавятся автоматически.

### Вручную

```bash
# Скопируйте проект на сервер
scp -r kindle-telegram-bot user@your-vps:/tmp/

# На сервере
ssh user@your-vps
cd /tmp/kindle-telegram-bot
sudo bash install.sh
```

## Конфигурация

Отредактируйте `/opt/kindle-telegram-bot/.env`:

```bash
sudo nano /opt/kindle-telegram-bot/.env
```

### Пример для Gmail

```env
TELEGRAM_BOT_TOKEN=123456789:ABC...
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=your@gmail.com
SMTP_PASSWORD=xxxx-xxxx-xxxx-xxxx
SMTP_FROM_EMAIL=your@gmail.com
SMTP_USE_TLS=true
WIDGETS_ENABLED=true
```

Для Gmail нужен [пароль приложения](https://myaccount.google.com/apppasswords), а не обычный пароль.

### Пример для Yandex

```env
SMTP_HOST=smtp.yandex.ru
SMTP_PORT=587
SMTP_USER=your@yandex.ru
SMTP_PASSWORD=your-password
SMTP_FROM_EMAIL=your@yandex.ru
SMTP_USE_TLS=true
```

## Запуск

```bash
sudo systemctl start kindle-telegram-bot
sudo systemctl status kindle-telegram-bot
sudo journalctl -u kindle-telegram-bot -f
```

## Команды бота

| Команда | Описание |
|---------|----------|
| `/start` | Приветствие |
| `/register` | Регистрация / смена Kindle email |
| `/status` | Карточка регистрации с текущим Kindle email |
| `/help` | Справка |
| `/cancel` | Отмена регистрации |

После регистрации пользователь присылает файлы книг как документы: они собираются в карточке приёма, отправка запускается кнопкой «Отправить».

## Локальная разработка

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# отредактируйте .env
python bot.py
```

Тесты (с поддельным Bot API и SMTP):

```bash
pip install -r requirements-dev.txt
pytest
```

## Структура проекта

```
kindle-telegram-bot/
├── bot.py              # Основная логика бота
├── widget_api.py       # Методы виджетов Bot API 10.4
├── widgets/            # HTML виджетов: регистрация, приём, прогресс
├── config.py           # Настройки из .env
├── database.py         # SQLite: пользователи, списки приёма, отправки
├── email_sender.py     # Отправка книг на Kindle
├── deploy.sh           # Деплой на VPS по SSH
├── install.sh          # Установка на Ubuntu
├── tests/              # pytest
├── kindle-bot.service  # systemd unit
├── requirements.txt
└── .env.example
```

## Ограничения

- Максимальный размер файла: 20 МБ (лимит Telegram для ботов)
- Amazon принимает до 50 МБ на письмо
- Некоторые форматы (например, EPUB) Amazon может конвертировать автоматически

## Обновление

```bash
./deploy.sh root@your-vps
```

или на сервере:

```bash
cd /path/to/kindle-telegram-bot
git pull   # или скопируйте новые файлы
sudo bash install.sh
sudo systemctl restart kindle-telegram-bot
```
