# Kindle Telegram Bot

Telegram-бот для отправки электронных книг на Kindle через email.

## Возможности

- Регистрация пользователя с указанием Kindle email
- Приём файлов книг (PDF, EPUB, MOBI, AZW и др.)
- Отправка книг на Kindle через SMTP
- Хранение настроек пользователей в SQLite

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
| `/status` | Текущий Kindle email |
| `/help` | Справка |
| `/cancel` | Отмена регистрации |

После регистрации пользователь просто отправляет файл книги боту как документ.

## Локальная разработка

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# отредактируйте .env
python bot.py
```

## Структура проекта

```
kindle-telegram-bot/
├── bot.py              # Основная логика бота
├── config.py           # Настройки из .env
├── database.py         # SQLite: пользователи
├── email_sender.py     # Отправка книг на Kindle
├── install.sh          # Установка на Ubuntu
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
cd /path/to/kindle-telegram-bot
git pull   # или скопируйте новые файлы
sudo bash install.sh
sudo systemctl restart kindle-telegram-bot
```
