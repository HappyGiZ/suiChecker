# Sui Balance Checker

Python-скрипт для проверки балансов кошельков Sui. Показывает балансы SUI, токенов (например, AFSUI) с ценами в USD через CoinGecko API, а также количество SUI в нативном стейкинге.

## Особенности
- Таблица с балансами и общей стоимостью.
- После проверки выводит список неработающих прокси.
- Логирование в `sui_checker.log`.
- Настройка через файлы `wallets.txt`, `tokens.txt`, `proxies.txt`.

## Установка
1. Установите зависимости:
   ```bash
   pip install requests tabulate tqdm

## Запуск
- В терминале python check.py