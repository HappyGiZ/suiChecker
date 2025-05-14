import requests
from tabulate import tabulate
from tqdm import tqdm
import sys
from time import time, sleep
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import logging

# Настройка логирования только в файл
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', filename='sui_checker.log')
logger = logging.getLogger(__name__)

# Конфигурация
SUI_RPC_URL = "https://fullnode.mainnet.sui.io:443"
COINGECKO_API_URL = "https://api.coingecko.com/api/v3/simple/price"
WALLETS_FILE = "wallets.txt"
TOKENS_FILE = "tokens.txt"
PROXIES_FILE = "proxies.txt"
PRICE_CACHE_DURATION = 300  # 5 минут в секундах
MIN_TOKEN_VALUE = 0.05  # Минимальная общая стоимость токена ($0.05)
MAX_WORKERS = 10  # Максимальное количество параллельных потоков
MAX_RETRIES = 3  # Максимальное количество попыток для запроса

# Кэш для цен и decimals
price_cache = {}
decimals_cache = {}
cache_lock = threading.Lock()

def load_file(filename):
    """Загружает строки из файла"""
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            return [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        logger.error(f"Файл {filename} не найден.")
        return []

def parse_proxies(proxy_lines):
    """Парсит строки прокси в формате host:port:username:password"""
    proxies = []
    for line in proxy_lines:
        try:
            host, port, username, password = line.split(':')
            proxy = f"http://{username}:{password}@{host}:{port}"
            proxies.append(proxy)
        except ValueError:
            logger.error(f"Неверный формат прокси: {line}. Ожидается host:port:username:password")
    return proxies

def load_proxies():
    """Загружает список прокси из файла"""
    proxy_lines = load_file(PROXIES_FILE)
    proxies = parse_proxies(proxy_lines)
    if not proxies:
        logger.warning("Файл с прокси пуст, не найден или содержит неверный формат. Работаем без прокси.")
    return proxies

def test_proxy(proxy):
    """Проверяет работоспособность прокси"""
    for attempt in range(MAX_RETRIES):
        try:
            proxies = {'http': proxy, 'https': proxy}
            response = requests.get("https://api.ipify.org", timeout=5, proxies=proxies)
            response.raise_for_status()
            logger.info(f"Прокси {proxy} работает.")
            return proxy
        except requests.exceptions.RequestException as e:
            logger.error(f"Прокси {proxy} не работает (попытка {attempt + 1}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES - 1:
                sleep(2)
    return None

def shorten_address(address, prefix_len=5, suffix_len=3):
    """Сокращает адрес в формате 0x123...456"""
    if len(address) > prefix_len + suffix_len + 3:
        return f"{address[:prefix_len]}...{address[-suffix_len:]}"
    return address

def get_token_prices(token_symbols, proxies=None):
    """Получает цены токенов из CoinGecko API с кэшированием"""
    with cache_lock:
        if "timestamp" in price_cache and time() - price_cache["timestamp"] < PRICE_CACHE_DURATION:
            return price_cache["prices"]

    symbol_to_id = {
        "SUI": "sui",
        "USDC": "usd-coin",
        "USDT": "tether",
        "BUCK": "bucket-protocol-buck-stablecoin",
        "AFSUI": "aftermath-staked-sui",
        "NS": "suins-token",
        "WAL": "walrus-2",
        "CERT": "volo-staked-sui"
    }
    ids = [symbol_to_id.get(symbol, symbol.lower()) for symbol in token_symbols]
    ids_str = ",".join(ids)
    for proxy in proxies or [None]:
        for attempt in range(MAX_RETRIES):
            try:
                proxies_dict = {'http': proxy, 'https': proxy} if proxy else None
                response = requests.get(
                    f"{COINGECKO_API_URL}?ids={ids_str}&vs_currencies=usd",
                    timeout=5,
                    proxies=proxies_dict
                )
                response.raise_for_status()
                data = response.json()
                prices = {symbol: data.get(symbol_to_id.get(symbol, symbol.lower()), {}).get("usd", 0.0)
                          for symbol in token_symbols}
                with cache_lock:
                    price_cache["prices"] = prices
                    price_cache["timestamp"] = time()
                return prices
            except requests.exceptions.HTTPError as e:
                if response.status_code == 429:
                    logger.warning("Превышен лимит запросов CoinGecko. Повтор через 60 секунд...")
                    sleep(60)
                    continue
                logger.error(f"Ошибка при получении цен через прокси {proxy}: {e}")
                return {symbol: 0.0 for symbol in token_symbols}
            except requests.exceptions.RequestException as e:
                logger.error(f"Ошибка сети при использовании прокси {proxy}: {e}")
                if attempt < MAX_RETRIES - 1:
                    sleep(2)
                    continue
                if proxy is not None:
                    break
        return {symbol: 0.0 for symbol in token_symbols}

def get_token_decimals(token_type, proxies=None):
    """Получает количество decimals для токена"""
    with cache_lock:
        if token_type in decimals_cache:
            return decimals_cache[token_type]

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "suix_getCoinMetadata",
        "params": [token_type]
    }
    for proxy in proxies or [None]:
        for attempt in range(MAX_RETRIES):
            try:
                proxies_dict = {'http': proxy, 'https': proxy} if proxy else None
                response = requests.post(SUI_RPC_URL, json=payload, timeout=5, proxies=proxies_dict)
                response.raise_for_status()
                data = response.json()
                decimals = int(data['result']['decimals']) if 'result' in data and data['result'] else 9
                with cache_lock:
                    decimals_cache[token_type] = decimals
                return decimals
            except requests.exceptions.RequestException as e:
                logger.error(f"Ошибка при получении decimals для {token_type} (прокси: {proxy}, попытка {attempt + 1}/{MAX_RETRIES}): {e}")
                if attempt < MAX_RETRIES - 1:
                    sleep(2)
                    continue
                if proxy is not None:
                    break
        logger.warning(f"Используется 9 decimals для {token_type}")
        return 9

def get_sui_balance(wallet_address, proxy=None):
    """Получает баланс SUI"""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "suix_getBalance",
        "params": [wallet_address]
    }
    for attempt in range(MAX_RETRIES):
        try:
            proxies = {'http': proxy, 'https': proxy} if proxy else None
            response = requests.post(SUI_RPC_URL, json=payload, timeout=10, proxies=proxies)
            response.raise_for_status()
            data = response.json()
            return int(data['result']['totalBalance']) / 10**9 if 'result' in data else 0.0
        except requests.exceptions.RequestException as e:
            logger.error(f"Ошибка при получении баланса SUI для {wallet_address} (прокси: {proxy}, попытка {attempt + 1}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES - 1:
                sleep(2)
                continue
            return 0.0

def get_staked_sui(wallet_address, proxy=None):
    """Получает стейкинг SUI"""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "suix_getStakes",
        "params": [wallet_address]
    }
    for attempt in range(MAX_RETRIES):
        try:
            proxies = {'http': proxy, 'https': proxy} if proxy else None
            response = requests.post(SUI_RPC_URL, json=payload, timeout=10, proxies=proxies)
            response.raise_for_status()
            data = response.json()
            return sum(int(staked_obj.get('principal', 0)) 
                       for stake in data.get('result', []) 
                       for staked_obj in stake.get('stakes', [])) / 10**9
        except requests.exceptions.RequestException as e:
            logger.error(f"Ошибка при получении стейкинга SUI для {wallet_address} (прокси: {proxy}, попытка {attempt + 1}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES - 1:
                sleep(2)
                continue
            return 0.0

def get_token_balance(wallet_address, token_type, proxy=None):
    """Получает баланс токена с учетом decimals"""
    decimals = decimals_cache.get(token_type, 9)
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "suix_getBalance",
        "params": [wallet_address, token_type]
    }
    for attempt in range(MAX_RETRIES):
        try:
            proxies = {'http': proxy, 'https': proxy} if proxy else None
            response = requests.post(SUI_RPC_URL, json=payload, timeout=5, proxies=proxies)
            response.raise_for_status()
            data = response.json()
            balance = int(data['result']['totalBalance']) if 'result' in data else 0
            return balance / 10**decimals
        except requests.exceptions.RequestException as e:
            logger.error(f"Ошибка при получении баланса токена {token_type} для {wallet_address} (прокси: {proxy}, попытка {attempt + 1}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES - 1:
                sleep(2)
                continue
            return 0.0

def get_all_balances(wallet_address, token_types, proxy=None):
    """Получает балансы SUI и токенов через отдельные запросы"""
    sui_balance = get_sui_balance(wallet_address, proxy)
    staked_sui = get_staked_sui(wallet_address, proxy)
    token_balances = {}
    for token_type in token_types:
        balance = get_token_balance(wallet_address, token_type, proxy)
        token_balances[token_type] = balance
    return sui_balance, staked_sui, token_balances

def get_token_symbol(token_type):
    """Извлекает символ токена из его типа"""
    parts = token_type.split("::")
    return parts[-1] if len(parts) > 1 else token_type

def format_balance(balance, price):
    """Форматирует баланс и стоимость в долларах"""
    if balance == 0:
        return "-"
    if price == 0.0:
        return f"{balance:,.2f} (цена недоступна)"
    value = balance * price
    return f"{balance:,.2f} (${value:,.2f})"

def process_wallet(wallet, index, token_types, proxies, prices):
    """Обрабатывает один кошелёк"""
    for attempt in range(len(proxies) if proxies else 1):
        proxy = proxies[(index - 1 + attempt) % len(proxies)] if proxies else None
        short_addr = shorten_address(wallet)
        
        sui_balance, staked_sui, token_balances = get_all_balances(wallet, token_types, proxy)
        
        if sui_balance or staked_sui or any(token_balances.values()):
            break
        logger.warning(f"Попытка {attempt + 1} не удалась для {short_addr} с прокси {proxy}. Пробуем другой прокси...")
    
    af_sui_balance = 0.0
    v_sui_balance = 0.0
    total_value = 0.0
    if prices.get("SUI", 0.0) > 0:
        total_value += (sui_balance + staked_sui) * prices["SUI"]
    
    formatted_balances = {}
    for token in token_types:
        balance = token_balances[token]
        token_symbol = get_token_symbol(token)
        if token_symbol == "AFSUI":
            af_sui_balance = balance
        elif token_symbol == "CERT":
            v_sui_balance = balance
        if prices.get(token_symbol, 0.0) > 0:
            total_value += balance * prices[token_symbol]
        formatted_balances[token] = format_balance(balance, prices.get(token_symbol, 0.0))
    
    total_sui = sui_balance + staked_sui + af_sui_balance + v_sui_balance
    total_value_str = f"${total_value:,.2f}" if total_value > 0 else "цена недоступна"
    
    return {
        "index": index,
        "address": short_addr,
        "sui_balance": sui_balance,
        "staked_sui": staked_sui,
        "af_sui": af_sui_balance,
        "v_sui": v_sui_balance,
        "token_balances": token_balances,
        "formatted_balances": formatted_balances,
        "total_sui": total_sui,
        "total_value": total_value,
        "row": [index, short_addr, format_balance(sui_balance, prices.get("SUI", 0.0)),
                format_balance(staked_sui, prices.get("SUI", 0.0)), formatted_balances,
                format_balance(total_sui, prices.get("SUI", 0.0)), total_value_str]
    }

def main():
    wallets = load_file(WALLETS_FILE)
    token_types = load_file(TOKENS_FILE)
    proxies = load_proxies()
    
    if not wallets:
        print("Ошибка: Нет кошельков для проверки.")
        logger.error("Нет кошельков для проверки.")
        return

    if proxies:
        valid_proxies = []
        failed_proxies = []
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(test_proxy, proxy): proxy for proxy in proxies}
            for future in tqdm(as_completed(futures), total=len(proxies), desc="Проверка прокси", file=sys.stdout):
                proxy = futures[future]
                result = future.result()
                if result:
                    valid_proxies.append(result)
                else:
                    failed_proxies.append(proxy)
        
        if failed_proxies:
            print("\nНеработающие прокси:")
            for proxy in failed_proxies:
                print(f"- {proxy}")
        
        if not valid_proxies:
            print("\nНет рабочих прокси. Работаем без прокси.")
            logger.warning("Нет рабочих прокси. Работаем без прокси.")
            proxies = []
        else:
            proxies = valid_proxies
            logger.info(f"Используем {len(proxies)} рабочих прокси: {proxies}")

    print("\nКэшируем decimals токенов...")
    for token in token_types:
        get_token_decimals(token, proxies)

    token_symbols = [get_token_symbol(token) for token in token_types]
    token_symbols_with_sui = ["SUI"] + token_symbols

    print("Получаем цены токенов...")
    prices = get_token_prices(token_symbols_with_sui, proxies)

    table_data = []
    token_balances_all = {token: [] for token in token_types}
    totals = {
        "sui": 0.0,
        "staked": 0.0,
        "af_sui": 0.0,
        "v_sui": 0.0,
        "tokens": {token: 0.0 for token in token_types},
        "total_value": 0.0
    }

    print(f"\nПроверяем {len(wallets)} кошельков...")
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(process_wallet, wallet, i, token_types, proxies, prices)
                   for i, wallet in enumerate(wallets, 1)]
        # Добавляем прогресс-бар, сохраняя порядок кошельков
        for i, future in tqdm(enumerate(futures), total=len(wallets), desc="Проверка кошельков", file=sys.stdout):
            try:
                result = future.result()
                table_data.append(result["row"])
                for token in token_types:
                    token_balances_all[token].append(result["token_balances"][token])
                    totals["tokens"][token] += result["token_balances"][token]
                totals["sui"] += result["sui_balance"]
                totals["staked"] += result["staked_sui"]
                totals["af_sui"] += result["af_sui"]
                totals["v_sui"] += result["v_sui"]
                totals["total_value"] += result["total_value"]
            except Exception as e:
                logger.error(f"Ошибка обработки кошелька {wallets[i]}: {e}")

    significant_tokens = []
    for token in token_types:
        total_balance = totals["tokens"][token]
        token_symbol = get_token_symbol(token)
        token_value = total_balance * prices.get(token_symbol, 0.0)
        has_non_zero_balance = any(balance > 0 for balance in token_balances_all[token])
        if has_non_zero_balance and token_value > MIN_TOKEN_VALUE:
            significant_tokens.append(token)

    significant_symbols = [get_token_symbol(token).replace("CERT", "VSUI") for token in significant_tokens]
    headers = ["#", "Адрес", "SUI", "Стейкинг"] + significant_symbols + ["Всего SUI", "Общая стоимость"]

    final_table_data = []
    for row in table_data:
        token_balances = row[4]
        new_row = row[:4]
        new_row.extend([token_balances[token] for token in significant_tokens])
        new_row.append(row[5])
        new_row.append(row[6])
        final_table_data.append(new_row)

    total_row = [
        "ИТОГО",
        f"{len(wallets)} кошельков",
        format_balance(totals["sui"], prices.get("SUI", 0.0)),
        format_balance(totals["staked"], prices.get("SUI", 0.0))
    ]
    total_row.extend([format_balance(totals["tokens"][token], prices.get(get_token_symbol(token), 0.0))
                      for token in significant_tokens])
    total_sui = totals["sui"] + totals["staked"] + totals["af_sui"] + totals["v_sui"]
    total_row.append(format_balance(total_sui, prices.get("SUI", 0.0)))
    total_row.append(f"${totals['total_value']:,.2f}" if totals['total_value'] > 0 else "цена недоступна")
    final_table_data.append(total_row)

    print("\n" + "="*120)
    print(tabulate(final_table_data, headers=headers, tablefmt="grid", numalign="right", stralign="right",
                   maxcolwidths=[None, 20] + [25] * (len(headers) - 2)))

if __name__ == "__main__":
    main()