"""One-shot migration: add qwen_sentiment column to political_socials."""
from config import db_connect

conn = db_connect()
try:
    conn.execute("ALTER TABLE political_socials ADD COLUMN qwen_sentiment TEXT DEFAULT 'UNSCORED'")
    conn.commit()
    print('Column added.')
except Exception as e:
    print('Error:', e)
