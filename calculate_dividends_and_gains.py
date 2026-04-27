import json

with open('portfolio/transactions.json', 'r') as f:
    data = json.load(f)

transactions = data['transactions']

total_dividends_usd = 0
total_dividends_ils = 0

total_realized_gains_usd = 0
total_realized_gains_ils = 0

for tx in transactions:
    if tx['action'] == 'dividend':
        if tx['currency'] == 'USD':
            total_dividends_usd += tx['net']
        elif tx['currency'] == 'ILS':
            total_dividends_ils += tx['net']
    elif tx['action'] == 'sell':
        # Wait, the user wants "earnings when I sold stocks" -> this implies realized gains.
        # But wait, the transactions.json has 'gross', 'net', 'tax_il', 'commission'.
        # To calculate realized gains correctly, we need the buy price.
        pass

print(f"Dividends USD: {total_dividends_usd}")
print(f"Dividends ILS: {total_dividends_ils}")
