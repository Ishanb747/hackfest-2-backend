import duckdb
conn = duckdb.connect('data/aml.db', read_only=True)

# Test SET search_path then unqualified
tests = [
    ("SET search_path=main", None),
    ("SELECT COUNT(*) FROM transactions", "plain"),
    ("SELECT COUNT(*) FROM aml.main.transactions", "3-part"),
    ("SET CATALOG aml", None),
    ("SELECT COUNT(*) FROM transactions", "after SET CATALOG"),
]
for sql, label in tests:
    try:
        r = conn.execute(sql)
        if label:
            print(f"OK [{label}] -> {r.fetchone()}")
        else:
            print(f"OK SET: {sql}")
    except Exception as e:
        print(f"FAIL [{label or sql}] -> {str(e)[:150]}")

conn.close()
