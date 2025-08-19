# add_column.py
from sqlmodel import create_engine
from sqlalchemy import text

eng = create_engine("sqlite:///app.db")

with eng.begin() as conn:
    cols = [r[1] for r in conn.exec_driver_sql("PRAGMA table_info(step);").fetchall()]
    if "image_path" not in cols:
        conn.exec_driver_sql("ALTER TABLE step ADD COLUMN image_path TEXT;")
        print("✅ step.image_path を追加しました")
    else:
        print("✅ すでに image_path カラムがあります")