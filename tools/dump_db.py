"""Print row counts and the guilds table from the bot's database.

    .venv\\Scripts\\python.exe tools\\dump_db.py [db_path]
"""

import sqlite3
import sys

path = sys.argv[1] if len(sys.argv) > 1 else "data/adjutant.db"
conn = sqlite3.connect(path)
conn.row_factory = sqlite3.Row

tables = [
    r["name"]
    for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
]
print(f"database: {path}")
for table in tables:
    count = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
    print(f"  {table:<18} {count}")

print("\nguilds:")
for row in conn.execute("SELECT * FROM guilds"):
    print(f"  {dict(row)}")
