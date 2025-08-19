from sqlmodel import SQLModel, Session, create_engine, select
from app.main import Recipe  # 既存モデルを再利用

engine = create_engine("sqlite:///app.db", echo=False)

with Session(engine) as s:
    rows = s.exec(select(Recipe).where(Recipe.main_image.is_not(None))).all()
    fixed = 0
    for r in rows:
        if r.main_image.endswith("+"):
            r.main_image = r.main_image[:-1]
            s.add(r)
            fixed += 1
    if fixed:
        s.commit()
    print("fixed:", fixed)