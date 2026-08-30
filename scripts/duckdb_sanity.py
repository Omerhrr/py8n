"""Sanity: duckdb parquet write/read WITHOUT pyarrow (v27 foundation check)."""
import duckdb, pandas as pd, json, os

path = "/tmp/v27_sanity.parquet"
if os.path.exists(path):
    os.remove(path)

df = pd.DataFrame([
    {"name": "Ada", "age": 36, "score": 9.5, "joined": "2024-01-02"},
    {"name": "Grace", "age": 45, "score": 8.1, "joined": "2024-02-09"},
    {"name": "Linus", "age": 28, "score": 7.7, "joined": "2024-03-11"},
])

con = duckdb.connect()
con.register("df_view", df)
con.execute(f"COPY df_view TO '{path}' (FORMAT PARQUET)")

back = con.execute(f"SELECT * FROM read_parquet('{path}') WHERE age > 30 ORDER BY score DESC")
rows = json.loads(back.df().to_json(orient="records", date_format="iso"))
print("roundtrip rows:", rows)
print("schema:", con.execute(f"DESCRIBE SELECT * FROM read_parquet('{path}')").fetchall())

# aggregate over parquet directly
agg = con.execute(f"SELECT count(*) n, avg(age) avg_age FROM read_parquet('{path}')").df()
print("agg:", json.loads(agg.to_json(orient="records")))
print("OK: duckdb parquet works without pyarrow")
