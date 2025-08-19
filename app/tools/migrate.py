from sqlmodel import create_engine

engine = create_engine("sqlite:///app.db")

with engine.connect() as conn:
    conn.exec_driver_sql("""
        CREATE TABLE recipe_new (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          title TEXT NOT NULL,
          description TEXT NOT NULL DEFAULT '',
          main_image TEXT,
          author_id INTEGER
        );
    """)
    conn.exec_driver_sql("""
        INSERT INTO recipe_new (id, title, description, main_image, author_id)
        SELECT id, title, description, main_image, author_id FROM recipe;
    """)
    conn.exec_driver_sql("DROP TABLE recipe;")
    conn.exec_driver_sql("ALTER TABLE recipe_new RENAME TO recipe;")

print("✅ Migration done!")