# server.py
"""Booster Platform API для мульти-пользовательского режима."""

from fastapi import FastAPI, HTTPException, Depends, Header
from pydantic import BaseModel
import psycopg2
import psycopg2.extras
import os
import hashlib
import datetime

app = FastAPI(title="Valve Games Booster API")

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise Exception("DATABASE_URL не задан")

ADMIN_KEY = "TEST-KEY-FOR-ME-0001"


def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        yield conn
    finally:
        conn.close()


def get_hwid_hash(hwid: str) -> str:
    return hashlib.sha256(hwid.encode()).hexdigest()[:32]


class LoginRequest(BaseModel):
    key: str
    nickname: str | None = None
    hwid: str


class BoostCreate(BaseModel):
    game: str
    client_steam_id: str
    client_nickname: str = ""
    start_value: int
    target_value: int
    total_price: float
    commission: float
    booster_earn: float
    estimated_hours: float
    deadline_days: int = 7
    options: str = "{}"
    notes: str = ""


class BalanceAdjust(BaseModel):
    user_id: int
    delta: float
    comment: str = "Ручная корректировка"


@app.get("/")
def root():
    return {"status": "ok", "service": "Booster API"}


@app.post("/api/login")
def login(req: LoginRequest, conn=Depends(get_db)):
    cur = conn.cursor()
    cur.execute("SELECT role, active FROM keys WHERE key = %s", (req.key.upper(),))
    key_row = cur.fetchone()
    if not key_row or not key_row["active"]:
        raise HTTPException(401, "Ключ не найден или деактивирован")

    role = key_row["role"]
    hwid_hash = get_hwid_hash(req.hwid)

    cur.execute("SELECT * FROM users WHERE key = %s", (req.key.upper(),))
    user = cur.fetchone()

    if user:
        if user["hwid"] != hwid_hash:
            raise HTTPException(403, "Ключ привязан к другому устройству")
        cur.execute("UPDATE users SET last_login = %s WHERE id = %s",
                    (datetime.datetime.now().isoformat(), user["id"]))
        conn.commit()
        return {
            "id": user["id"], "key": user["key"], "nickname": user["nickname"],
            "role": user["role"], "hourly_rate": user["hourly_rate"],
            "balance": float(user["balance"] or 0),
        }

    if not req.nickname or len(req.nickname) < 3:
        raise HTTPException(400, "Ник минимум 3 символа")

    cur.execute("SELECT id FROM users WHERE nickname = %s", (req.nickname,))
    if cur.fetchone():
        raise HTTPException(400, "Ник занят")

    cur.execute(
        "INSERT INTO users (key, nickname, role, hwid, last_login) VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (req.key.upper(), req.nickname, role, hwid_hash, datetime.datetime.now().isoformat())
    )
    uid = cur.fetchone()["id"]
    conn.commit()
    cur.execute("SELECT * FROM users WHERE id = %s", (uid,))
    user = cur.fetchone()
    return {
        "id": user["id"], "key": user["key"], "nickname": user["nickname"],
        "role": user["role"], "hourly_rate": user["hourly_rate"],
        "balance": float(user["balance"] or 0),
    }


@app.get("/api/boosts/{user_id}")
def get_boosts(user_id: int, status: str = "all", conn=Depends(get_db)):
    cur = conn.cursor()
    if status == "all":
        cur.execute("SELECT * FROM boosts WHERE booster_id = %s ORDER BY id DESC", (user_id,))
    else:
        cur.execute("SELECT * FROM boosts WHERE booster_id = %s AND status = %s ORDER BY id DESC",
                    (user_id, status))
    rows = cur.fetchall()
    for r in rows:
        for k, v in r.items():
            if isinstance(v, datetime.datetime):
                r[k] = v.isoformat()
    return rows


@app.post("/api/boosts")
def create_boost(boost: BoostCreate, x_user_id: int = Header(...), conn=Depends(get_db)):
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO boosts (booster_id, game, client_steam_id, client_nickname,
                            start_value, target_value, total_price, commission,
                            booster_earn, estimated_hours, deadline_days, options, notes)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
    """, (x_user_id, boost.game, boost.client_steam_id, boost.client_nickname,
          boost.start_value, boost.target_value, boost.total_price, boost.commission,
          boost.booster_earn, boost.estimated_hours, boost.deadline_days,
          boost.options, boost.notes))
    bid = cur.fetchone()["id"]
    conn.commit()
    return {"id": bid}


@app.post("/api/boosts/{boost_id}/start")
def start_boost(boost_id: int, conn=Depends(get_db)):
    cur = conn.cursor()
    cur.execute("UPDATE boosts SET status = 'active', started_at = %s WHERE id = %s",
                (datetime.datetime.now().isoformat(), boost_id))
    conn.commit()
    return {"ok": True}


@app.post("/api/boosts/{boost_id}/complete")
def complete_boost(boost_id: int, conn=Depends(get_db)):
    cur = conn.cursor()
    cur.execute("SELECT booster_id, booster_earn, commission FROM boosts WHERE id = %s", (boost_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(404, "Заказ не найден")
    now = datetime.datetime.now().isoformat()
    cur.execute("UPDATE boosts SET status = 'completed', completed_at = %s WHERE id = %s",
                (now, boost_id))
    cur.execute("UPDATE users SET balance = balance + %s WHERE id = %s",
                (row["booster_earn"], row["booster_id"]))
    cur.execute(
        "INSERT INTO transactions (user_id, boost_id, amount, type, comment) VALUES (%s, %s, %s, %s, %s)",
        (row["booster_id"], boost_id, row["booster_earn"], "earning", f"Завершён буст #{boost_id}")
    )
    cur.execute("SELECT id FROM users WHERE role = 'admin' LIMIT 1")
    admin = cur.fetchone()
    if admin and row["commission"] > 0:
        cur.execute(
            "INSERT INTO transactions (user_id, boost_id, amount, type, comment) VALUES (%s, %s, %s, %s, %s)",
            (admin["id"], boost_id, row["commission"], "commission", f"Комиссия с буста #{boost_id}")
        )
    conn.commit()
    return {"ok": True}


@app.post("/api/boosts/{boost_id}/refund")
def refund_boost(boost_id: int, conn=Depends(get_db)):
    cur = conn.cursor()
    cur.execute("UPDATE boosts SET status = 'refunded', completed_at = %s WHERE id = %s",
                (datetime.datetime.now().isoformat(), boost_id))
    conn.commit()
    return {"ok": True}


@app.get("/api/profile/{user_id}")
def get_profile(user_id: int, conn=Depends(get_db)):
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
    user = cur.fetchone()
    if not user:
        raise HTTPException(404, "Пользователь не найден")

    cur.execute("""
        SELECT
            COALESCE(SUM(CASE WHEN status='planned' THEN 1 END), 0) as planned,
            COALESCE(SUM(CASE WHEN status='active' THEN 1 END), 0) as active,
            COALESCE(SUM(CASE WHEN status='completed' THEN 1 END), 0) as completed,
            COALESCE(SUM(CASE WHEN status='refunded' THEN 1 END), 0) as refunded
        FROM boosts WHERE booster_id = %s
    """, (user_id,))
    counts = cur.fetchone()

    cur.execute("SELECT COALESCE(SUM(amount), 0) as earned FROM transactions WHERE user_id = %s AND type = 'earning'",
                (user_id,))
    total_earned = cur.fetchone()["earned"]

    today = datetime.date.today().isoformat()
    week_ago = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()
    month_ago = (datetime.date.today() - datetime.timedelta(days=30)).isoformat()

    cur.execute("""
        SELECT
            COALESCE(SUM(CASE WHEN date(date) = %s THEN amount END), 0) as today,
            COALESCE(SUM(CASE WHEN date(date) >= %s THEN amount END), 0) as week,
            COALESCE(SUM(CASE WHEN date(date) >= %s THEN amount END), 0) as month
        FROM transactions WHERE user_id = %s AND type = 'earning'
    """, (today, week_ago, month_ago, user_id))
    periods = cur.fetchone()

    cur.execute("SELECT * FROM transactions WHERE user_id = %s ORDER BY id DESC LIMIT 30", (user_id,))
    history = cur.fetchall()
    for h in history:
        if isinstance(h["date"], datetime.datetime):
            h["date"] = h["date"].isoformat()

    return {
        "user": {
            "id": user["id"], "key": user["key"], "nickname": user["nickname"],
            "role": user["role"], "balance": float(user["balance"] or 0),
            "hourly_rate": user["hourly_rate"],
        },
        "statuses": {
            "planned": counts["planned"], "active": counts["active"],
            "completed": counts["completed"], "refunded": counts["refunded"],
        },
        "total_earned": float(total_earned or 0),
        "earned_today": float(periods["today"] or 0),
        "earned_week": float(periods["week"] or 0),
        "earned_month": float(periods["month"] or 0),
        "history": history,
    }


@app.post("/api/profile/rate")
def update_rate(user_id: int = Header(...), rate: int = Header(...), conn=Depends(get_db)):
    cur = conn.cursor()
    cur.execute("UPDATE users SET hourly_rate = %s WHERE id = %s", (rate, user_id))
    conn.commit()
    return {"ok": True}


def require_admin(x_admin_key: str = Header(...)):
    if x_admin_key != ADMIN_KEY:
        raise HTTPException(403, "Доступ запрещён")


@app.get("/api/admin/stats")
def admin_stats(_=Depends(require_admin), conn=Depends(get_db)):
    cur = conn.cursor()
    cur.execute("SELECT COALESCE(SUM(total_price),0) as turnover, COALESCE(SUM(commission),0) as commission, COALESCE(SUM(booster_earn),0) as paid FROM boosts WHERE status = 'completed'")
    fin = cur.fetchone()

    cur.execute("""SELECT
        COALESCE(SUM(CASE WHEN status='planned' THEN 1 END), 0) as planned,
        COALESCE(SUM(CASE WHEN status='active' THEN 1 END), 0) as active,
        COALESCE(SUM(CASE WHEN status='completed' THEN 1 END), 0) as completed,
        COALESCE(SUM(CASE WHEN status='refunded' THEN 1 END), 0) as refunded
        FROM boosts""")
    counts = cur.fetchone()

    today = datetime.date.today().isoformat()
    week_ago = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()
    month_ago = (datetime.date.today() - datetime.timedelta(days=30)).isoformat()

    cur.execute("""
        SELECT
            COALESCE(SUM(CASE WHEN date(date) = %s THEN amount END), 0) as today,
            COALESCE(SUM(CASE WHEN date(date) >= %s THEN amount END), 0) as week,
            COALESCE(SUM(CASE WHEN date(date) >= %s THEN amount END), 0) as month
        FROM transactions WHERE type = 'commission'
    """, (today, week_ago, month_ago))
    periods = cur.fetchone()

    cur.execute("SELECT COUNT(*) as c FROM users WHERE role = 'booster'")
    boosters = cur.fetchone()["c"]
    cur.execute("SELECT COALESCE(SUM(balance),0) as b FROM users WHERE role = 'booster'")
    total_balance = cur.fetchone()["b"]

    return {
        "turnover": float(fin["turnover"] or 0), "commission": float(fin["commission"] or 0),
        "paid": float(fin["paid"] or 0), "counts": counts,
        "c_today": float(periods["today"] or 0), "c_week": float(periods["week"] or 0),
        "c_month": float(periods["month"] or 0),
        "boosters": boosters, "total_balance": float(total_balance or 0),
    }


@app.get("/api/admin/users")
def admin_users(_=Depends(require_admin), conn=Depends(get_db)):
    cur = conn.cursor()
    cur.execute("SELECT id, key, nickname, role, balance, hourly_rate, created_at, last_login FROM users ORDER BY id")
    rows = cur.fetchall()
    for r in rows:
        for k, v in r.items():
            if isinstance(v, datetime.datetime):
                r[k] = v.isoformat()
    return rows


@app.post("/api/admin/balance")
def admin_balance(req: BalanceAdjust, _=Depends(require_admin), conn=Depends(get_db)):
    cur = conn.cursor()
    cur.execute("UPDATE users SET balance = balance + %s WHERE id = %s", (req.delta, req.user_id))
    cur.execute("INSERT INTO transactions (user_id, amount, type, comment) VALUES (%s, %s, %s, %s)",
                (req.user_id, req.delta, "admin_adjust", req.comment))
    conn.commit()
    return {"ok": True}


@app.get("/api/admin/keys")
def admin_keys(_=Depends(require_admin), conn=Depends(get_db)):
    cur = conn.cursor()
    cur.execute("SELECT * FROM keys ORDER BY created_at DESC")
    rows = cur.fetchall()
    for r in rows:
        if isinstance(r["created_at"], datetime.datetime):
            r["created_at"] = r["created_at"].isoformat()
    return rows


@app.post("/api/admin/keys")
def admin_add_key(key: str, role: str = "booster", note: str = "",
                   _=Depends(require_admin), conn=Depends(get_db)):
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO keys (key, role, note) VALUES (%s, %s, %s)",
                    (key.upper(), role, note))
        conn.commit()
        return {"ok": True}
    except psycopg2.IntegrityError:
        raise HTTPException(400, "Ключ уже существует")


@app.post("/api/admin/keys/{key}/toggle")
def admin_toggle_key(key: str, _=Depends(require_admin), conn=Depends(get_db)):
    cur = conn.cursor()
    cur.execute("SELECT active FROM keys WHERE key = %s", (key,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(404, "Ключ не найден")
    new_val = not row["active"]
    cur.execute("UPDATE keys SET active = %s WHERE key = %s", (new_val, key))
    conn.commit()
    return {"ok": True, "active": new_val}


@app.get("/api/admin/top")
def admin_top(_=Depends(require_admin), conn=Depends(get_db)):
    cur = conn.cursor()
    cur.execute("""
        SELECT u.id, u.nickname, COALESCE(SUM(t.amount), 0) as earned
        FROM users u LEFT JOIN transactions t ON t.user_id = u.id AND t.type = 'earning'
        WHERE u.role = 'booster' GROUP BY u.id ORDER BY earned DESC LIMIT 5
    """)
    return cur.fetchall()


@app.get("/api/admin/daily")
def admin_daily(days: int = 7, _=Depends(require_admin), conn=Depends(get_db)):
    cur = conn.cursor()
    result = []
    for i in range(days - 1, -1, -1):
        d = (datetime.date.today() - datetime.timedelta(days=i)).isoformat()
        cur.execute("SELECT COALESCE(SUM(amount), 0) as s FROM transactions WHERE type='commission' AND date(date)=%s", (d,))
        result.append({"date": d, "amount": float(cur.fetchone()["s"] or 0)})
    return result
