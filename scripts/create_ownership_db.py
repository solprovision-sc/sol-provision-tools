"""One-shot helper to create an empty blueprint_ownership.db with the right
schema. Run once locally, then SCP the resulting file to the VPS at
/var/www/sol-provision-tools/blueprint_ownership.db.
"""
import sqlite3
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "blueprint_ownership.db"

conn = sqlite3.connect(OUT)
conn.execute("""
    CREATE TABLE IF NOT EXISTS blueprint_ownership (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        discord_id TEXT NOT NULL,
        blueprint_uuid TEXT NOT NULL,
        blueprint_name TEXT NOT NULL,
        patch_version TEXT NOT NULL,
        claimed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        env TEXT NOT NULL CHECK(env IN ('prod', 'dev')),
        notes TEXT,
        UNIQUE(discord_id, blueprint_uuid, patch_version)
    )
""")
conn.commit()
n = conn.execute("SELECT COUNT(*) FROM blueprint_ownership").fetchone()[0]
conn.close()
print(f"Created {OUT}  (rows: {n})")
