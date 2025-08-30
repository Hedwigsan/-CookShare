# app/main.py
from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path
from typing import Optional, List, Tuple
from PIL import Image, ImageOps
from sqlmodel import SQLModel, Field, Session, create_engine, select
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import delete, text, func
from passlib.context import CryptContext
import secrets
import uuid
import json
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

# ---------- DB ----------
DB_URL = "sqlite:///app.db"
engine = create_engine(DB_URL, echo=False)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# ---------- Models ----------
class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(index=True, unique=True)
    password_hash: str

class Recipe(SQLModel, table=True):
    __table_args__ = {"sqlite_autoincrement": True}
    id: Optional[int] = Field(default=None, primary_key=True)
    title: str
    description: str = ""
    main_image: Optional[str] = None
    author_id: Optional[int] = Field(foreign_key="user.id", default=None)
    view_count: int = Field(default=0)

class Ingredient(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    recipe_id: int = Field(foreign_key="recipe.id")
    name: str
    amount: Optional[str] = None
    unit: Optional[str] = None
    order_no: int = 0

class Step(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    recipe_id: int = Field(foreign_key="recipe.id")
    body: str
    order_no: int = 0
    image_path: Optional[str] = None

class Tag(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str  # 重複はアプリ側で防止

class RecipeTag(SQLModel, table=True):
    recipe_id: int = Field(foreign_key="recipe.id", primary_key=True)
    tag_id: int = Field(foreign_key="tag.id", primary_key=True)

# ---------- App ----------
app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key="CHANGE_ME_TO_RANDOM_AND_SECRET", same_site="lax")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.mount("/media", StaticFiles(directory="app/media"), name="media")
app.mount("/static", StaticFiles(directory="app/static/css"), name="css")
templates = Jinja2Templates(directory="app/templates")
Path("app/media").mkdir(parents=True, exist_ok=True)

# ---------- Startup (create tables + lightweight migration) ----------
@app.exception_handler(RequestValidationError)
async def ve_handler(request, exc):
    print("★422 detail:", exc.errors())
    return JSONResponse({"detail": exc.errors()}, status_code=422)

@app.on_event("startup")
def on_startup():
    SQLModel.metadata.create_all(engine)
    _migrate_sqlite()

def _migrate_sqlite():
    # 既存 recipe テーブルに必要なカラムを追加
    with engine.connect() as conn:
        rows = conn.exec_driver_sql("PRAGMA table_info(recipe);").fetchall()
        colnames = {r[1] for r in rows}
        if "author_id" not in colnames:
            conn.exec_driver_sql("ALTER TABLE recipe ADD COLUMN author_id INTEGER;")
        if "view_count" not in colnames:
            conn.exec_driver_sql("ALTER TABLE recipe ADD COLUMN view_count INTEGER DEFAULT 0;")

# ---------- Auth helpers ----------
def get_current_user(request: Request) -> Optional[User]:
    uid = request.session.get("user_id")
    if not uid:
        return None
    with Session(engine) as s:
        return s.get(User, uid)

def _ensure_csrf(request: Request):
    if not request.session.get("csrf_token"):
        request.session["csrf_token"] = secrets.token_urlsafe(16)

def verify_csrf(request: Request, token: str):
    sess_token = request.session.get("csrf_token")
    if not sess_token or not token or not secrets.compare_digest(sess_token, token):
        raise HTTPException(status_code=400, detail="Invalid CSRF token")

def _require_login(request: Request) -> User:
    user = get_current_user(request)
    if not user:
        # 画面遷移前提の保護は 303 リダイレクトで返す
        raise HTTPException(status_code=401, detail="Login required")
    return user

def _get_recipe_or_404(session: Session, rid: int) -> Recipe:
    r = session.get(Recipe, rid)
    if not r:
        raise HTTPException(status_code=404, detail="Not found")
    return r

def _require_owner(request: Request, session: Session, rid: int):
    user = _require_login(request)
    r = _get_recipe_or_404(session, rid)
    if r.author_id != user.id:
        raise HTTPException(status_code=403, detail="Forbidden")
    return r, user

def save_step_image(upload: UploadFile, crop_x=None, crop_y=None, crop_w=None, crop_h=None) -> str:
    if not upload or not upload.filename:
        return None
    fname = f"{uuid.uuid4().hex}.webp"
    out = Path("app/media") / fname
    data = upload.file.read()
    out.write_bytes(data)
    try:
        with Image.open(out) as im0:
            im = ImageOps.exif_transpose(im0)
            im.thumbnail((1600, 1600))

            # crop 指定がある場合
            if all(v is not None for v in [crop_x, crop_y, crop_w, crop_h]):
                x, y, w, h = int(crop_x), int(crop_y), int(crop_w), int(crop_h)
                w = min(w, im.width - x)
                h = min(h, im.height - y)
                im = im.crop((x, y, x + w, y + h))
                im = im.resize((1200, 675), Image.LANCZOS)

            im.save(out, format="WEBP", quality=85)
    except Exception as e:
        print("step image save error:", e)
    return f"/media/{fname}"

def _to_float_or_none(v: Optional[str]) -> Optional[float]:
    try:
        return float(v) if v not in (None, "",) else None
    except ValueError:
        return None

def _crop_tuple_at(idx: int,
                   xs: Optional[List[str]], ys: Optional[List[str]],
                   ws: Optional[List[str]], hs: Optional[List[str]]
) -> Optional[Tuple[int,int,int,int]]:
    # 各配列から idx の値を取り出して int へ。全部そろってなければ None
    vals = []
    for arr in (xs, ys, ws, hs):
        v = arr[idx] if arr and idx < len(arr) else None
        vals.append(_to_int_or_none(v))
    if all(v is not None for v in vals):
        x, y, w, h = vals
        return (max(0,x), max(0,y), max(1,w), max(1,h))
    return None

def _to_int_or_none(v) -> Optional[int]:
    try:
        return int(float(v)) if v not in (None, "",) else None
    except Exception:
        return None
# ---------- Routes ----------
# 一覧＋検索＋タグ絞り込み
@app.get("/", response_class=HTMLResponse)
def index(request: Request, q: Optional[str] = None, tag: Optional[str] = None, order: str = "new"):
    _ensure_csrf(request)
    current_user = get_current_user(request)
    with Session(engine) as s:
        stmt = select(Recipe)
        if q:
            stmt = stmt.where((Recipe.title.contains(q)) | (Recipe.description.contains(q)))
        if tag:
            t = s.exec(select(Tag).where(Tag.name == tag.strip())).first()
            if not t:
                return templates.TemplateResponse("index.html",
                    {"request": request, "recipes": [], "q": q or "", "tag": tag, "order": order, "current_user": current_user})
            rid_rows = s.exec(select(RecipeTag.recipe_id).where(RecipeTag.tag_id == t.id)).all()
            rid_list = [r for (r,) in rid_rows] if rid_rows and isinstance(rid_rows[0], tuple) else rid_rows
            if rid_list:
                stmt = stmt.where(Recipe.id.in_(rid_list))
            else:
                return templates.TemplateResponse("index.html",
                    {"request": request, "recipes": [], "q": q or "", "tag": tag, "order": order, "current_user": current_user})

        if order == "old":
            stmt = stmt.order_by(Recipe.id.asc())
        elif order == "views":
            stmt = stmt.order_by(Recipe.view_count.desc())
        else:
            stmt = stmt.order_by(Recipe.id.desc())
        recipes = s.exec(stmt).all()
    return templates.TemplateResponse("index.html",
        {"request": request, "recipes": recipes, "q": q or "", "tag": tag or "", "order": order, "current_user": current_user})

# ---------- Auth: signup/login/logout ----------
@app.get("/signup", response_class=HTMLResponse)
def signup_form(request: Request):
    _ensure_csrf(request)
    return templates.TemplateResponse("signup.html",
        {"request": request, "csrf_token": request.session.get("csrf_token"), "current_user": get_current_user(request)})

@app.post("/signup", response_class=HTMLResponse)
def signup(request: Request,
           email: str = Form(...),
           password: str = Form(...),
           csrf_token: str = Form(...)):
    verify_csrf(request, csrf_token)
    with Session(engine) as s:
        exists = s.exec(select(User).where(User.email == email)).first()
        if exists:
            _ensure_csrf(request)
            return templates.TemplateResponse("signup.html",
                {"request": request, "error": "このメールは既に登録済みです。", "csrf_token": request.session.get("csrf_token"),
                 "current_user": get_current_user(request)})
        u = User(email=email, password_hash=pwd_context.hash(password))
        s.add(u); s.commit(); s.refresh(u)
        request.session["user_id"] = u.id
    return RedirectResponse(url="/", status_code=303)

@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    _ensure_csrf(request)
    return templates.TemplateResponse("login.html",
        {"request": request, "csrf_token": request.session.get("csrf_token"), "current_user": get_current_user(request)})

@app.post("/login", response_class=HTMLResponse)
def login(request: Request,
          email: str = Form(...),
          password: str = Form(...),
          csrf_token: str = Form(...)):
    verify_csrf(request, csrf_token)
    with Session(engine) as s:
        u = s.exec(select(User).where(User.email == email)).first()
        if not u or not pwd_context.verify(password, u.password_hash):
            _ensure_csrf(request)
            return templates.TemplateResponse("login.html",
                {"request": request, "error": "メールまたはパスワードが違います。", "csrf_token": request.session.get("csrf_token"),
                 "current_user": get_current_user(request)})
        request.session["user_id"] = u.id
    return RedirectResponse(url="/", status_code=303)

@app.post("/logout")
def logout(request: Request, csrf_token: str = Form(...)):
    verify_csrf(request, csrf_token)
    request.session.clear()
    return RedirectResponse(url="/", status_code=303)

# ---------- Recipes: new/create ----------
@app.get("/recipes/new", response_class=HTMLResponse)
def recipe_new(request: Request):
    current_user = get_current_user(request)
    if not current_user:
        return RedirectResponse(url="/login", status_code=303)
    _ensure_csrf(request)
    return templates.TemplateResponse("recipe_new.html",
        {"request": request, "csrf_token": request.session.get("csrf_token"), "current_user": current_user})

@app.post("/recipes", response_class=HTMLResponse)
async def recipe_create(
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    image: UploadFile = File(None),
    ingredients_name: List[str] = Form([]),
    ingredients_amount: List[str] = Form([]),
    ingredients_unit: List[str] = Form([]),
    steps_body: List[str] = Form([]),
    steps_image: List[UploadFile] = File([]),     # ★ 手順画像
    
    steps_crop_x: Optional[List[str]] = Form(None),
    steps_crop_y: Optional[List[str]] = Form(None),
    steps_crop_w: Optional[List[str]] = Form(None),
    steps_crop_h: Optional[List[str]] = Form(None),

    tags_csv: str = Form(""),
    crop_x: Optional[str] = Form(None),
    crop_y: Optional[str] = Form(None),
    crop_w: Optional[str] = Form(None),
    crop_h: Optional[str] = Form(None),
    csrf_token: str = Form(...),
):
    current_user = get_current_user(request)
    if not current_user:
        return RedirectResponse(url="/login", status_code=303)
    verify_csrf(request, csrf_token)

    cx = _to_float_or_none(crop_x)
    cy = _to_float_or_none(crop_y)
    cw = _to_float_or_none(crop_w)
    ch = _to_float_or_none(crop_h)

    # メイン画像（16:9切り抜き対応）
    saved_path = None
    if image and getattr(image, "filename", None):
        fname = f"{uuid.uuid4().hex}.webp"
        out = Path("app/media") / fname
        data = await image.read()
        out.write_bytes(data)
        try:
            with Image.open(out) as im0:
                im = ImageOps.exif_transpose(im0)
                im.thumbnail((1600, 1600))
                # 任意の切り抜き
                if all(v is not None for v in (cx, cy, cw, ch)):
                    x = max(0, int(cx)); y = max(0, int(cy))
                    w = max(1, int(cw)); h = max(1, int(ch))
                    w = min(w, im.width - x); h = min(h, im.height - y)
                    im = im.crop((x, y, x + w, y + h))
                    im = im.resize((1200, 675), Image.LANCZOS)
                im.save(out, format="WEBP", quality=85)
        except Exception:
            pass
        saved_path = f"/media/{fname}+"

    with Session(engine) as s:
        # レシピ本体
        r = Recipe(title=title, description=description, main_image=saved_path, author_id=current_user.id)
        s.add(r); s.commit(); s.refresh(r)

        # 材料
        for idx, name in enumerate(ingredients_name or []):
            name = (name or "").strip()
            if not name: 
                continue
            amount = (ingredients_amount[idx] if idx < len(ingredients_amount) else None) or None
            unit   = (ingredients_unit[idx]   if idx < len(ingredients_unit)   else None) or None
            s.add(Ingredient(
                recipe_id=r.id, name=name,
                amount=(amount.strip() if amount else None),
                unit=(unit.strip() if unit else None),
                order_no=idx,
            ))

        # 手順（本文＋画像）※画像のみの手順もOK
        max_len = max(len(steps_body or []), len(steps_image or []))
        for idx in range(max_len):
            body = (steps_body[idx] if idx < len(steps_body) else "") or ""
            body = body.strip()
            if idx < len(steps_body):
                body = (steps_body[idx] or "").strip()
            img_path = None
            if idx < len(steps_image):
                up = steps_image[idx]
                if up and getattr(up, "filename", None):
                    crop = _crop_tuple_at(idx, steps_crop_x, steps_crop_y, steps_crop_w, steps_crop_h)
                    img_path = save_step_image(up)
            if not body and not img_path:
                continue
            s.add(Step(recipe_id=r.id, body=body or "", order_no=idx, image_path=img_path))

        # タグ
        for raw in (tags_csv or "").split(","):
            tname = raw.strip()
            if not tname: 
                continue
            t = s.exec(select(Tag).where(Tag.name == tname)).first()
            if not t:
                t = Tag(name=tname); s.add(t); s.commit(); s.refresh(t)
            exists = s.exec(select(RecipeTag).where(
                (RecipeTag.recipe_id == r.id) & (RecipeTag.tag_id == t.id)
            )).first()
            if not exists:
                s.add(RecipeTag(recipe_id=r.id, tag_id=t.id))

        s.commit()

        # 表示用
        ings  = s.exec(select(Ingredient).where(Ingredient.recipe_id == r.id).order_by(Ingredient.order_no)).all()
        steps = s.exec(select(Step).where(Step.recipe_id == r.id).order_by(Step.order_no)).all()
        tags  = s.exec(select(Tag).join(RecipeTag, RecipeTag.tag_id == Tag.id).where(RecipeTag.recipe_id == r.id)).all()

    return templates.TemplateResponse(
        "recipe_detail.html",
        {"request": request, "recipe": r, "ingredients": ings, "steps": steps, "tags": tags, "current_user": current_user},
    )


# ---------- Recipes: detail ----------
@app.get("/recipes/{rid}", response_class=HTMLResponse)
def recipe_detail(request: Request, rid: int):
    _ensure_csrf(request)
    current_user = get_current_user(request)
    with Session(engine) as s:
        r = s.get(Recipe, rid)
        if not r:
            return HTMLResponse("Not found", status_code=404)
        r.view_count = (r.view_count or 0) + 1
        s.add(r)
        s.commit()
        s.refresh(r)
        ings = s.exec(select(Ingredient).where(Ingredient.recipe_id == rid).order_by(Ingredient.order_no)).all()
        steps = s.exec(select(Step).where(Step.recipe_id == rid).order_by(Step.order_no)).all()
        tags = s.exec(select(Tag).join(RecipeTag, RecipeTag.tag_id == Tag.id).where(RecipeTag.recipe_id == rid)).all()

        fav_count = _fav_count(s, rid)
        is_fav = (current_user is not None) and _is_favorited(s, current_user.id, rid)

    return templates.TemplateResponse("recipe_detail.html", {
        "request": request, "recipe": r, "ingredients": ings, "steps": steps, "tags": tags,
        "current_user": current_user, "fav_count": fav_count, "is_fav": is_fav
    })

# ---------- Recipes: edit (GET/POST) ----------
@app.get("/recipes/{rid}/edit", response_class=HTMLResponse)
def recipe_edit_form(request: Request, rid: int):
    _ensure_csrf(request)
    with Session(engine) as s:
        r, user = _require_owner(request, s, rid)
        ings = s.exec(select(Ingredient).where(Ingredient.recipe_id == rid).order_by(Ingredient.order_no)).all()
        steps = s.exec(select(Step).where(Step.recipe_id == rid).order_by(Step.order_no)).all()
        tags = s.exec(select(Tag).join(RecipeTag, RecipeTag.tag_id == Tag.id).where(RecipeTag.recipe_id == rid)).all()
    tags_csv = ", ".join(t.name for t in tags)
    return templates.TemplateResponse("recipe_edit.html",
        {"request": request, "recipe": r, "ingredients": ings, "steps": steps, "tags_csv": tags_csv,
         "csrf_token": request.session.get("csrf_token"), "current_user": user})

@app.post("/recipes/{rid}/edit", response_class=HTMLResponse)
async def recipe_update(
    request: Request,
    rid: int,
    # ここは必須
    title: str = Form(...),
    csrf_token: str = Form(...),

    # ここからはオプショナルで受ける（空文字・未送信を許容）
    description: str = Form(""),
    image: UploadFile = File(None),

    ingredients_name: Optional[List[str]] = Form(None),
    ingredients_amount: Optional[List[str]] = Form(None),

    steps_body: Optional[List[str]] = Form(None),
    steps_existing_image: Optional[List[str]] = Form(None),
    steps_image: Optional[List[UploadFile]] = File(None),

    tags_csv: str = Form(""),

    # ← 空が来る可能性があるので str で受けて手動変換
    crop_x: Optional[str] = Form(None),
    crop_y: Optional[str] = Form(None),
    crop_w: Optional[str] = Form(None),
    crop_h: Optional[str] = Form(None),
):
    current_user = get_current_user(request)
    if not current_user:
        return RedirectResponse(url="/login", status_code=303)
    verify_csrf(request, csrf_token)

    cx = _to_float_or_none(crop_x)
    cy = _to_float_or_none(crop_y)
    cw = _to_float_or_none(crop_w)
    ch = _to_float_or_none(crop_h)

    with Session(engine) as s:
        r = s.get(Recipe, rid)
        if not r: return HTMLResponse("Not found", status_code=404)
        if r.author_id != current_user.id: return HTMLResponse("Forbidden", status_code=403)

        # メイン画像（差し替え時のみ）
        if image and getattr(image, "filename", None):
            fname = f"{uuid.uuid4().hex}.webp"
            out = Path("app/media") / fname
            data = await image.read()
            out.write_bytes(data)
            try:
                with Image.open(out) as im0:
                    im = ImageOps.exif_transpose(im0)
                    im.thumbnail((1600, 1600))
                    if all(v is not None for v in (cx, cy, cw, ch)):
                        x = max(0, int(cx)); y = max(0, int(cy))
                        w = max(1, int(cw)); h = max(1, int(ch))
                        w = min(w, im.width - x); h = min(h, im.height - y)
                        im = im.crop((x, y, x + w, y + h)).resize((1200, 675), Image.LANCZOS)
                    im.save(out, format="WEBP", quality=85)
            except Exception:
                pass
            r.main_image = f"/media/{fname}+"

        # 本文
        r.title = title
        r.description = description
        s.add(r); s.commit()

        # 材料：全削除→再作成（テンプレは unit を送っていない）
        s.exec(delete(Ingredient).where(Ingredient.recipe_id == rid))
        names   = ingredients_name  or []
        amounts = ingredients_amount or []
        for idx, name in enumerate(names):
            name = (name or "").strip()
            if not name: continue
            amount = (amounts[idx] if idx < len(amounts) else None) or None
            s.add(Ingredient(
                recipe_id=rid, name=name,
                amount=(amount.strip() if amount else None),
                unit=None, order_no=idx,
            ))

        # 手順：全削除→再作成（本文＋画像）
        s.exec(delete(Step).where(Step.recipe_id == rid))
        bodies = steps_body or []
        olds   = steps_existing_image or []
        imgs   = steps_image or []
        max_len = max(len(bodies), len(olds), len(imgs))
        for idx in range(max_len):
            body = ((bodies[idx] if idx < len(bodies) else "") or "").strip()
            keep = (olds[idx] if idx < len(olds) else None) or None

            img_path = None
            if idx < len(imgs):
                up = imgs[idx]
                if up and getattr(up, "filename", None):
                    img_path = save_step_image(up)
            if not img_path:
                img_path = keep  # 削除チェックはテンプレ未実装なので維持

            if not body and not img_path:
                continue
            s.add(Step(recipe_id=rid, body=body, order_no=idx, image_path=img_path))

        # タグ（追加的に）
        for raw in (tags_csv or "").split(","):
            tname = raw.strip()
            if not tname: continue
            t = s.exec(select(Tag).where(Tag.name == tname)).first()
            if not t:
                t = Tag(name=tname); s.add(t); s.commit(); s.refresh(t)
            exists = s.exec(select(RecipeTag).where(
                (RecipeTag.recipe_id == rid) & (RecipeTag.tag_id == t.id)
            )).first()
            if not exists:
                s.add(RecipeTag(recipe_id=rid, tag_id=t.id))

        s.commit()

    return RedirectResponse(url=f"/recipes/{rid}", status_code=303)
# ---------- Recipes: delete ----------
@app.post("/recipes/{rid}/delete")
def recipe_delete(request: Request, rid: int, csrf_token: str = Form(...)):
    verify_csrf(request, csrf_token)
    with Session(engine) as s:
        r, user = _require_owner(request, s, rid)  # 所有者チェック（自作ヘルパ）
        # 子テーブル削除（お気に入りも忘れずに）
        s.exec(delete(Ingredient).where(Ingredient.recipe_id == rid))
        s.exec(delete(Step).where(Step.recipe_id == rid))
        s.exec(delete(RecipeTag).where(RecipeTag.recipe_id == rid))
        s.exec(delete(Favorite).where(Favorite.recipe_id == rid))  # ★お気に入りの孤児防止
        s.delete(r)
        s.commit()
    return RedirectResponse(url="/", status_code=303)

# ---------- PWA manifest / SW / favicon ----------
@app.get("/manifest.json")
def manifest():
    data = {
        "name": "CookShare",
        "short_name": "CookShare",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#ffffff",
        "theme_color": "#ffffff",
        "icons": []  # 後で追加
    }
    return Response(content=json.dumps(data, ensure_ascii=False), media_type="application/manifest+json")

@app.get("/sw.js")
def sw():
    js = "self.addEventListener('install', e => { self.skipWaiting(); }); self.addEventListener('activate', e => { self.clients.claim(); });"
    return PlainTextResponse(js, media_type="application/javascript")

@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    ico_path = Path("app/static/favicon.ico")
    if ico_path.exists():
        return FileResponse(str(ico_path))
    return Response(status_code=204)

def _parse_crop(x, y, w, h):
    """空や不正値を弾いて、(x,y,w,h) を int で返す。全て揃ってなければ None を返す。"""
    vals = (x, y, w, h)
    if any(v is None or str(v).strip() == "" for v in vals):
        return None
    try:
        xi = max(0, int(float(x)))
        yi = max(0, int(float(y)))
        wi = max(1, int(float(w)))
        hi = max(1, int(float(h)))
        return (xi, yi, wi, hi)
    except Exception:
        return None
    
@app.get("/account", response_class=HTMLResponse)
def account_page(request: Request):
    _ensure_csrf(request)
    return templates.TemplateResponse("account.html", {
        "request": request,
        "current_user": get_current_user(request),
        "csrf_token": request.session.get("csrf_token"),
    })

# --- Favorite モデル（新規） ---
class Favorite(SQLModel, table=True):
    user_id: int = Field(foreign_key="user.id", primary_key=True)
    recipe_id: int = Field(foreign_key="recipe.id", primary_key=True)

def _is_favorited(session: Session, user_id: int, recipe_id: int) -> bool:
    return session.get(Favorite, (user_id, recipe_id)) is not None

def _fav_count(session: Session, recipe_id: int) -> int:
    return session.exec(select(func.count()).select_from(Favorite).where(Favorite.recipe_id == recipe_id)).one()

# 追加
@app.post("/recipes/{rid}/favorite")
def favorite_recipe(request: Request, rid: int, csrf_token: str = Form(...)):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    verify_csrf(request, csrf_token)
    with Session(engine) as s:
        exists = s.exec(select(Favorite).where(
            (Favorite.user_id == user.id) & (Favorite.recipe_id == rid)
        )).first()
        if not exists:
            s.add(Favorite(user_id=user.id, recipe_id=rid)); s.commit()
    # Ajax対応なら 204 でもOK。ここは一覧/詳細へ戻す
    return RedirectResponse(url=f"/recipes/{rid}", status_code=303)


# 解除
@app.post("/recipes/{rid}/unfavorite")
def unfavorite_recipe(request: Request, rid: int, csrf_token: str = Form(...)):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    verify_csrf(request, csrf_token)
    with Session(engine) as s:
        s.exec(delete(Favorite).where((Favorite.user_id == user.id) & (Favorite.recipe_id == rid)))
        s.commit()
    return RedirectResponse(url=f"/recipes/{rid}", status_code=303)

# お気に入り一覧ページ
@app.get("/favorites", response_class=HTMLResponse)
def favorites_page(request: Request, order: str = "new"):
    _ensure_csrf(request)
    user = _require_login(request)
    with Session(engine) as s:
        # ユーザーのお気に入り recipe を取得し並び替え
        fav_rids = s.exec(select(Favorite.recipe_id).where(Favorite.user_id == user.id)).all()
        if not fav_rids:
            recipes = []
        else:
            rid_list = [r for (r,) in fav_rids] if isinstance(fav_rids[0], tuple) else fav_rids
            stmt = select(Recipe).where(Recipe.id.in_(rid_list))
            if order == "old":
                stmt = stmt.order_by(Recipe.id.asc())
            elif order == "views":
                stmt = stmt.order_by(Recipe.view_count.desc())
            else:
                stmt = stmt.order_by(Recipe.id.desc())
            recipes = s.exec(stmt).all()
    return templates.TemplateResponse("favorites.html", {
        "request": request,
        "recipes": recipes,
        "current_user": user,
        "order": order,
    })

@app.get("/offline", response_class=HTMLResponse)
def offline_page(request: Request):
    return templates.TemplateResponse("offline.html", {
        "request": request,
        "current_user": get_current_user(request),
    })

