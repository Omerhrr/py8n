"""Generate sales_history.csv (60 rows) for the v28 E2E analytics demo."""
import csv
import random

rnd = random.Random(42)
regions = ["Lagos", "Berlin", "Seoul", "Paris", "Osaka", "Milan", "Dakar", "Sao Paulo"]
products = ["Pro Plan", "Enterprise", "Starter", "Add-on Pack"]

rows = []
for i in range(60):
    region = rnd.choice(regions)
    product = rnd.choice(products)
    units = rnd.randint(1, 40)
    price = round(rnd.uniform(15, 220), 2)
    cost = round(price * rnd.uniform(0.35, 0.7), 2)
    revenue = round(units * price, 2)
    month = rnd.randint(1, 12)
    rows.append({
        "order_id": f"SO-{1000 + i}",
        "month": f"2026-{month:02d}",
        "region": region,
        "product": product,
        "units": units,
        "price": price,
        "cost": cost,
        "revenue": revenue,
    })

with open("/home/z/my-project/scripts/e2e-sales.csv", "w", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)

print(f"wrote e2e-sales.csv with {len(rows)} rows")
