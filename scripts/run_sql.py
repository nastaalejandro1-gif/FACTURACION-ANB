"""Corre SQL contra Supabase usando SUPABASE_DB_URL del .env (solo uso local).

    python scripts/run_sql.py "SELECT 1"
    python scripts/run_sql.py archivo.sql
"""
import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

sql = sys.argv[1]
if sql.endswith(".sql"):
    sql = Path(sql).read_text(encoding="utf-8")

with psycopg.connect(os.environ["SUPABASE_DB_URL"]) as conn:
    cur = conn.execute(sql)
    if cur.description:
        for row in cur.fetchall():
            print(row)
    print("OK")
