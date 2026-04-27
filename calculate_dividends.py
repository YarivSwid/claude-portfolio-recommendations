import json

with open('portfolio/transactions.json', 'r') as f:
    data = json.load(f)

total_dividends_usd = 0
total_dividends_ils = 0

for tx in data['transactions']:
    if tx['action'] == 'dividend':
        if tx['currency'] == 'USD':
            total_dividends_usd += tx['net']
        elif tx['currency'] == 'ILS':
            total_dividends_ils += tx['net']

print(f"Total Dividends (USD): {total_dividends_usd}")
print(f"Total Dividends (ILS): {total_dividends_ils}")
