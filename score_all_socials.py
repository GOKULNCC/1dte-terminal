"""Score all remaining unscored political socials in batches of 15."""
from qwen_analyzer import score_socials
import sqlite3
from config import db_connect

while True:
    conn = db_connect(row_factory=True)
    remaining = conn.execute(
        "SELECT COUNT(*) FROM political_socials WHERE qwen_sentiment = 'UNSCORED'"
    ).fetchone()[0]
    conn.close()
    print(f"  Remaining unscored: {remaining}")
    if remaining == 0:
        break
    score_socials()

# Show final distribution
conn = db_connect(row_factory=True)
rows = conn.execute(
    "SELECT qwen_sentiment, COUNT(*) as c FROM political_socials GROUP BY qwen_sentiment"
).fetchall()
print("\nFinal sentiment distribution:")
for r in rows:
    print(f"  {dict(r)}")
conn.close()
