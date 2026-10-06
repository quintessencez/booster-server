# server.py
"""Booster Platform API v6 — Neon + Render ready."""

from fastapi import FastAPI, HTTPException, Depends, Header
from pydantic import BaseModel
import psycopg2
import psycopg2.extras
import os
import hashlib
import datetime
import httpx
import asyncio
import json
import re
import secrets
import string
import threading
import time
from base64 import urlsafe_b64encode
from cryptography.fernet import Fernet
from bs4 import BeautifulSoup

try:
    from FunPayAPI import Account as FunPayAccount
    from FunPayAPI import Runner as FunPayRunner
    try:
        from FunPayAPI.updater.events import (
            NewOrderEvent, OrderStatusChangedEvent, InitialChatEvent, NewMessageEvent,
            LastChatMessageChangedEvent, ChatsListChangedEvent
        )
    except Exception:
        NewOrderEvent = OrderStatusChangedEvent = InitialChatEvent = NewMessageEvent = None
        LastChatMessageChangedEvent = ChatsListChangedEvent = None
except Exception:
    FunPayAccount = FunPayRunner = None
    NewOrderEvent = OrderStatusChangedEvent = InitialChatEvent = NewMessageEvent = None
    LastChatMessageChangedEvent = ChatsListChangedEvent = None


app = FastAPI(title="Valve Games Booster API v6")

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise Exception("DATABASE_URL не задан")

ADMIN_KEY = os.environ.get("ADMIN_KEY", "TEST-KEY-FOR-ME-0001")
FACEIT_API_KEY = os.environ.get("FACEIT_API_KEY", "")
OPENDOTA_BASE = "https://api.opendota.com/api"
FACEIT_BASE = "https://open.faceit.com/data/v4"
ADVANCE_SHARE = 0.70
MASTER_ADMIN_KEY = ADMIN_KEY
FUNPAY_TOKEN_SECRET = os.environ.get("FUNPAY_TOKEN_SECRET", ADMIN_KEY)
FUNPAY_FERNET = Fernet(urlsafe_b64encode(hashlib.sha256(FUNPAY_TOKEN_SECRET.encode()).digest()))

# ===== ОБЩИЕ ЧАТЫ FUNPAY (числовые chat_id, добыты из HTML) =====
FUNPAY_PUBLIC_CHATS = {
    "dota2": {"name": "🎮 Dota 2 — общий чат", "chat_id": "3163049", "node": "game-41"},
    "cs2":   {"name": "🔫 CS2 — общий чат",    "chat_id": "79275542", "node": "game-333"},
}

LEVEL_PAYOUT = [
    (1, 5, 0.70), (6, 10, 0.73), (11, 20, 0.76), (21, 30, 0.80),
    (31, 40, 0.82), (41, 49, 0.85), (50, 50, 0.88),
]

KEY_ALPHABET = string.ascii_uppercase + string.digits


def generate_activation_key(prefix="VB"):
    parts = ["".join(secrets.choice(KEY_ALPHABET) for _ in range(5)) for _ in range(4)]
    return f"{prefix}-{parts[0]}-{parts[1]}-{parts[2]}-{parts[3]}"


def encrypt_funpay_token(token):
    return FUNPAY_FERNET.encrypt(token.encode()).decode()


def decrypt_funpay_token(token):
    return FUNPAY_FERNET.decrypt(token.encode()).decode()


def level_from_xp(xp):
    xp = max(0, int(xp or 0))
    level = 1
    spent = 0
    for lvl in range(1, 50):
        need = 100 + (lvl - 1) * 35
        if xp < spent + need:
            return lvl
        spent += need
        level = lvl + 1
    return 50


def payout_percent(level):
    level = max(1, min(50, int(level or 1)))
    for lo, hi, share in LEVEL_PAYOUT:
        if lo <= level <= hi:
            return share
    return 0.70


def effective_level(row):
    base = level_from_xp(row.get("xp", 0))
    admin_delta = int(row.get("admin_level_delta", 0) or 0)
    return max(1, min(50, base + admin_delta))


def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        yield conn
    finally:
        conn.close()


def get_hwid_hash(hwid: str, key: str) -> str:
    return hashlib.sha256(f"{key}:{hwid}".encode()).hexdigest()[:32]


def rows_to_json(rows):
    result = []
    for r in rows:
        item = dict(r)
        for k, v in item.items():
            if isinstance(v, datetime.datetime):
                item[k] = v.isoformat()
        result.append(item)
    return result


# ==================== МОДЕЛИ ====================
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


class TransferBoost(BaseModel):
    boost_id: int
    to_user_id: int
    comment: str = ""


class ChatMessage(BaseModel):
    to_user_id: int | None = None
    boost_id: int | None = None
    message: str


class FunPayConnect(BaseModel):
    golden_key: str


class FunPaySendMessage(BaseModel):
    message: str


# ==================== ГЛОБАЛЬНЫЕ ====================
FUNPAY_THREADS = {}
FUNPAY_PUBLIC_THREADS = {}
FUNPAY_STOP = {}
FUNPAY_ACCOUNTS = {}
FUNPAY_ACCOUNT_LOCK = threading.Lock()


# ==================== СТАРТ ====================
@app.on_event("startup")
async def start_funpay_workers():
    if FunPayAccount is None:
        return
    try:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            cur = conn.cursor()
            cur.execute("SELECT user_id, golden_key FROM funpay_accounts WHERE funpay_connected=TRUE")
            for row in cur.fetchall():
                uid = int(row["user_id"])
                try:
                    token = decrypt_funpay_token(row["golden_key"])
                    try:
                        account = FunPayAccount(token).get()
                        with FUNPAY_ACCOUNT_LOCK:
                            FUNPAY_ACCOUNTS[uid] = account
                    except Exception:
                        continue
                    FUNPAY_STOP[uid] = False
                    if uid not in FUNPAY_THREADS or not FUNPAY_THREADS[uid].is_alive():
                        t = threading.Thread(target=_funpay_worker, args=(uid, token), daemon=True)
                        FUNPAY_THREADS[uid] = t; t.start()
                    if uid not in FUNPAY_PUBLIC_THREADS or not FUNPAY_PUBLIC_THREADS[uid].is_alive():
                        pt = threading.Thread(target=_funpay_public_sync_loop, args=(uid,), daemon=True)
                        FUNPAY_PUBLIC_THREADS[uid] = pt; pt.start()
                except Exception:
                    continue
        finally:
            conn.close()
    except Exception:
        return


@app.get("/")
def root():
    return {"status": "ok", "service": "Booster API v6"}


# ==================== ЛОГИН ====================
@app.post("/api/login")
def login(req: LoginRequest, conn=Depends(get_db)):
    cur = conn.cursor()
    cur.execute("SELECT role, active FROM keys WHERE key = %s", (req.key.upper(),))
    key_row = cur.fetchone()
    if not key_row or not key_row["active"]:
        raise HTTPException(401, "Ключ не найден или деактивирован")
    role = key_row["role"]
    hwid_hash = get_hwid_hash(req.hwid, req.key.upper())
    cur.execute("SELECT * FROM users WHERE key = %s", (req.key.upper(),))
    user = cur.fetchone()
    if user:
        if user["hwid"] != hwid_hash:
            raise HTTPException(403, "Ключ привязан к другому устройству")
        if req.nickname and req.nickname.strip().lower() != (user["nickname"] or "").lower():
            raise HTTPException(403, "Ник привязан к ключу и не может быть изменён")
        cur.execute("UPDATE users SET last_login = %s WHERE id = %s",
                    (datetime.datetime.now().isoformat(), user["id"]))
        conn.commit()
        return {"id": user["id"], "key": user["key"], "nickname": user["nickname"],
                "role": user["role"], "hourly_rate": user["hourly_rate"],
                "balance": float(user["balance"] or 0),
                "level": effective_level(user),
                "xp": int(user.get("xp") or 0),
                "payout_percent": payout_percent(effective_level(user))}
    if not req.nickname or len(req.nickname.strip()) < 3:
        raise HTTPException(400, "Ник минимум 3 символа")
    nick = req.nickname.strip()
    cur.execute("SELECT id FROM users WHERE nickname = %s", (nick,))
    if cur.fetchone():
        raise HTTPException(400, "Ник занят")
    cur.execute("""INSERT INTO users (key, nickname, role, hwid, last_login)
                   VALUES (%s, %s, %s, %s, %s) RETURNING id""",
                (req.key.upper(), nick, role, hwid_hash,
                 datetime.datetime.now().isoformat()))
    uid = cur.fetchone()["id"]
    conn.commit()
    cur.execute("SELECT * FROM users WHERE id = %s", (uid,))
    user = cur.fetchone()
    return {"id": user["id"], "key": user["key"], "nickname": user["nickname"],
            "role": user["role"], "hourly_rate": user["hourly_rate"],
            "balance": float(user["balance"] or 0), "level": effective_level(user),
            "xp": int(user.get("xp") or 0), "payout_percent": payout_percent(effective_level(user))}


# ==================== БУСТЫ ====================
def _next_order_no(cur):
    cur.execute("SELECT COALESCE(MAX(CAST(order_no AS INTEGER)), 0) + 1 AS nxt FROM boosts")
    n = int(cur.fetchone()["nxt"])
    return f"{n:07d}"


@app.get("/api/boosts/{user_id}")
def get_boosts(user_id: int, status: str = "all", scope: str = "mine",
               conn=Depends(get_db)):
    cur = conn.cursor()
    cur.execute("SELECT role FROM users WHERE id = %s", (user_id,))
    row = cur.fetchone()
    is_admin = bool(row and row["role"] == "admin")
    where = []
    params = []
    if scope == "pool":
        where.append("b.booster_id IS NULL")
        where.append("b.status = 'planned'")
    elif scope == "all":
        if not is_admin:
            raise HTTPException(403, "Только для админа")
        if status != "all":
            where.append("b.status = %s"); params.append(status)
    else:
        where.append("(b.booster_id = %s OR (b.booster_id IS NULL AND b.source = 'funpay' AND b.source_owner_id = %s))")
        params.extend([user_id, user_id])
        if status != "all":
            where.append("b.status = %s"); params.append(status)
    sql = """SELECT b.*,
                    COALESCE(u.nickname, '—') AS booster_nickname,
                    so.nickname AS source_owner_nickname,
                    so.role AS source_owner_role
             FROM boosts b
             LEFT JOIN users u ON u.id = b.booster_id
             LEFT JOIN users so ON so.id = b.source_owner_id"""
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY b.id DESC"
    cur.execute(sql, params)
    result = rows_to_json(cur.fetchall())
    if scope == "pool":
        cur.execute("SELECT xp, admin_level_delta FROM users WHERE id=%s", (user_id,))
        me = cur.fetchone() or {}
        lvl = effective_level(me)
        level_share = payout_percent(lvl)
        for item in result:
            total = float(item.get("total_price") or 0)
            if item.get("source") == "funpay" and float(item.get("pool_executor_share") or 0) > 0:
                share = float(item.get("pool_executor_share") or 0)
                owner_share = float(item.get("pool_owner_share") or max(0.0, 1.0-share))
                item["payout_percent"] = share
                item["owner_payout_percent"] = owner_share
                item["potential_booster_earn"] = round(total * share, 2)
                item["platform_payout_percent"] = 0.0
            else:
                share = level_share
                item["payout_percent"] = share
                item["owner_payout_percent"] = 0.0
                item["platform_payout_percent"] = max(0.0, 1.0-share)
                item["potential_booster_earn"] = round(total * share, 2)
    return result


@app.post("/api/boosts")
def create_boost(boost: BoostCreate, x_user_id: int = Header(...), conn=Depends(get_db)):
    cur = conn.cursor()
    order_no = _next_order_no(cur)
    cur.execute("SELECT role FROM users WHERE id=%s", (x_user_id,))
    creator = cur.fetchone()
    if not creator:
        raise HTTPException(401, "Пользователь не найден")
    source = "admin" if creator["role"] == "admin" else "platform"
    source_owner_id = x_user_id
    cur.execute("""INSERT INTO boosts (order_no, booster_id, source, source_owner_id, game, client_steam_id,
                    client_nickname, start_value, target_value, total_price,
                    commission, booster_earn, estimated_hours, deadline_days,
                    options, notes, status)
                    VALUES (%s, NULL, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'planned')
                    RETURNING id, order_no""",
                (order_no, source, source_owner_id, boost.game, boost.client_steam_id, boost.client_nickname,
                 boost.start_value, boost.target_value, boost.total_price,
                 boost.commission, boost.booster_earn, boost.estimated_hours,
                 boost.deadline_days, boost.options, boost.notes))
    r = cur.fetchone()
    bid = r["id"]; ono = r["order_no"]
    conn.commit()
    cur.execute("SELECT id FROM users WHERE role = 'booster'")
    for u in cur.fetchall():
        cur.execute("""INSERT INTO notifications (user_id, type, title, body, related_id)
                       VALUES (%s, %s, %s, %s, %s)""",
                    (u["id"], "new_boost", f"Новый заказ #{ono}",
                     f"{boost.game}: {boost.start_value}→{boost.target_value}", bid))
    conn.commit()
    return {"id": bid, "order_no": ono}


@app.get("/api/boosts/detail/{boost_id}")
def get_boost_detail(boost_id: int, x_user_id: int = Header(...), conn=Depends(get_db)):
    cur = conn.cursor()
    cur.execute("""SELECT b.*, COALESCE(u.nickname, '—') AS booster_nickname,
                          so.nickname AS source_owner_nickname, so.role AS source_owner_role
                   FROM boosts b
                   LEFT JOIN users u ON u.id = b.booster_id
                   LEFT JOIN users so ON so.id = b.source_owner_id
                   WHERE b.id = %s""", (boost_id,))
    b = cur.fetchone()
    if not b:
        raise HTTPException(404, "Заказ не найден")
    cur.execute("SELECT role FROM users WHERE id = %s", (x_user_id,))
    me = cur.fetchone()
    is_admin = bool(me and me["role"] == "admin")
    if not is_admin and b["booster_id"] not in (None, x_user_id):
        raise HTTPException(403, "Нет доступа")
    cur.execute("""SELECT * FROM transactions WHERE boost_id = %s ORDER BY id DESC""", (boost_id,))
    txs = rows_to_json(cur.fetchall())
    result = dict(b)
    for k, v in result.items():
        if isinstance(v, datetime.datetime):
            result[k] = v.isoformat()
    result["transactions"] = txs
    return result


@app.post("/api/boosts/{boost_id}/take")
def take_boost(boost_id: int, confirm_terms: bool = False, x_user_id: int = Header(...),
               conn=Depends(get_db)):
    cur = conn.cursor()
    cur.execute("SELECT * FROM boosts WHERE id = %s FOR UPDATE", (boost_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(404, "Заказ не найден")
    if row["booster_id"] is not None or row["status"] != "planned":
        raise HTTPException(400, "Заказ уже взят или недоступен")
    cur.execute("SELECT role, level, xp, admin_level_delta FROM users WHERE id = %s", (x_user_id,))
    booster = cur.fetchone()
    if not booster or booster["role"] != "booster":
        raise HTTPException(403, "Только бустер может взять заказ")
    is_funpay = row.get("source") == "funpay"
    is_owner_self_take = is_funpay and row.get("source_owner_id") == x_user_id and float(row.get("pool_executor_share") or 0) <= 0
    if is_funpay and row.get("source_owner_id") == x_user_id and float(row.get("pool_executor_share") or 0) > 0:
        raise HTTPException(403, "Этот FunPay-заказ уже выставлен в пул по заданным условиям.")
    if is_owner_self_take:
        executor_share = 1.0; owner_share = 0.0; platform_share = 0.0
        terms_text = f"Это ваш FunPay-заказ #{row['order_no']}. При взятии себе вы получаете 100% суммы заказа."
    elif is_funpay and float(row.get("pool_executor_share") or 0) > 0:
        executor_share = float(row.get("pool_executor_share") or 0)
        owner_share = float(row.get("pool_owner_share") or max(0.0, 1.0-executor_share))
        platform_share = 0.0
        terms_text = f"FunPay-заказ #{row['order_no']}: исполнителю {executor_share:.0%}, владельцу заказа {owner_share:.0%}, платформе 0%."
    elif is_funpay:
        raise HTTPException(409, "Владелец FunPay-заказа должен сначала задать процент для исполнителя и положить заказ в пул")
    else:
        level = effective_level(booster)
        executor_share = payout_percent(level); owner_share = 0.0
        platform_share = max(0.0, 1.0 - executor_share)
        terms_text = f"Обычный заказ: ваш процент как исполнителя {executor_share:.0%}, платформа {platform_share:.0%}."
    if not confirm_terms:
        raise HTTPException(409, detail={
            "code": "terms_confirmation_required",
            "message": "Подтвердите условия заказа перед взятием",
            "order_no": row["order_no"],
            "total_price": float(row["total_price"] or 0),
            "executor_share": executor_share,
            "owner_share": owner_share,
            "platform_share": platform_share,
            "terms_text": terms_text,
        })
    total = float(row["total_price"] or 0)
    booster_earn = round(total * executor_share, 2)
    owner_earn = round(total * owner_share, 2)
    platform_earn = round(total * platform_share, 2)
    advance = round(booster_earn * ADVANCE_SHARE, 2)
    cur.execute("""UPDATE boosts SET booster_id=%s, status='active', started_at=%s,
                   booster_earn=%s, commission=%s, booster_share=%s, owner_share=%s,
                   platform_share=%s WHERE id=%s""",
                (x_user_id, datetime.datetime.now().isoformat(), booster_earn, platform_earn,
                 executor_share, owner_share, platform_share, boost_id))
    cur.execute("UPDATE users SET balance = balance + %s WHERE id = %s", (advance, x_user_id))
    cur.execute("""INSERT INTO transactions (user_id, boost_id, amount, type, comment)
                   VALUES (%s,%s,%s,'advance',%s)""",
                (x_user_id, boost_id, advance, f"Аванс {executor_share:.0%} за заказ #{row['order_no']}"))
    cur.execute("SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1")
    admin = cur.fetchone()
    if admin and platform_earn > 0:
        cur.execute("""INSERT INTO transactions (user_id, boost_id, amount, type, comment)
                       VALUES (%s,%s,%s,'commission',%s)""",
                    (admin["id"], boost_id, platform_earn, f"Доля платформы с заказа #{row['order_no']}"))
    conn.commit()
    return {"ok": True, "advance": advance, "booster_earn": booster_earn,
            "level": effective_level(booster),
            "booster_share": executor_share, "owner_share": owner_share,
            "owner_earn": owner_earn, "platform_earn": platform_earn,
            "terms_text": terms_text}


@app.post("/api/boosts/{boost_id}/complete")
def complete_boost(boost_id: int, x_user_id: int = Header(...), conn=Depends(get_db)):
    cur = conn.cursor()
    cur.execute("SELECT * FROM boosts WHERE id=%s FOR UPDATE", (boost_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(404, "Заказ не найден")
    if row["booster_id"] != x_user_id or row["status"] != "active":
        raise HTTPException(403, "Это не ваш активный заказ")
    total_earn = float(row["booster_earn"] or 0)
    advance = round(total_earn * ADVANCE_SHARE, 2)
    remainder = round(total_earn - advance, 2)
    now = datetime.datetime.now().isoformat()
    cur.execute("UPDATE boosts SET status='completed', completed_at=%s WHERE id=%s", (now, boost_id))
    owner_share = float(row.get("owner_share") or 0)
    owner_id = row.get("source_owner_id")
    owner_earn = round(float(row.get("total_price") or 0) * owner_share, 2)
    if owner_id and owner_earn > 0 and owner_id != x_user_id:
        cur.execute("UPDATE users SET balance = balance + %s WHERE id=%s", (owner_earn, owner_id))
        cur.execute("""INSERT INTO transactions (user_id, boost_id, amount, type, comment)
                       VALUES (%s,%s,%s,'funpay_owner',%s)""",
                    (owner_id, boost_id, owner_earn, f"{owner_share:.0%} владельцу FunPay-заказа #{row['order_no']}"))
    if remainder > 0:
        cur.execute("UPDATE users SET balance=balance+%s WHERE id=%s", (remainder, x_user_id))
        cur.execute("""INSERT INTO transactions (user_id,boost_id,amount,type,comment)
                       VALUES (%s,%s,%s,'earning',%s)""",
                    (x_user_id, boost_id, remainder, f"Остаток за заказ #{row['order_no']}"))
    xp_gain = max(50, int(float(row.get("total_price") or 0) / 10) + int(float(row.get("estimated_hours") or 0) * 20))
    cur.execute("SELECT xp, admin_level_delta FROM users WHERE id=%s FOR UPDATE", (x_user_id,))
    current_user = cur.fetchone()
    new_xp = int(current_user.get("xp", 0) or 0) + xp_gain
    new_base_level = level_from_xp(new_xp)
    cur.execute("UPDATE users SET xp=%s, level=%s WHERE id=%s", (new_xp, new_base_level, x_user_id))
    cur.execute("""INSERT INTO transactions (user_id,boost_id,amount,type,comment)
                   VALUES (%s,%s,%s,'xp',%s)""",
                (x_user_id, boost_id, xp_gain, f"XP за выполнение заказа #{row['order_no']}"))
    conn.commit()
    cur.execute("SELECT xp,level,admin_level_delta FROM users WHERE id=%s", (x_user_id,))
    u = cur.fetchone()
    final = effective_level(u)
    return {"ok": True, "remainder": remainder, "xp_gain": xp_gain,
            "level": final, "payout_percent": payout_percent(final)}


@app.post("/api/boosts/{boost_id}/refund")
def refund_boost(boost_id: int, x_user_id: int = Header(...), conn=Depends(get_db)):
    cur = conn.cursor()
    cur.execute("""SELECT booster_id, booster_earn, status, order_no
                   FROM boosts WHERE id = %s""", (boost_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(404, "Заказ не найден")
    if row["booster_id"] != x_user_id:
        raise HTTPException(403, "Это не ваш заказ")
    if row["status"] != "active":
        raise HTTPException(400, "Нельзя вернуть этот заказ")
    advance = round(float(row["booster_earn"] or 0) * ADVANCE_SHARE, 2)
    now = datetime.datetime.now().isoformat()
    cur.execute("UPDATE boosts SET status='refunded', completed_at=%s WHERE id=%s", (now, boost_id))
    cur.execute("UPDATE users SET balance = balance - %s WHERE id = %s", (advance, x_user_id))
    cur.execute("""INSERT INTO transactions (user_id, boost_id, amount, type, comment)
                   VALUES (%s, %s, %s, %s, %s)""",
                (x_user_id, boost_id, -advance, "refund", f"Возврат аванса за заказ #{row['order_no']}"))
    cur.execute("SELECT id FROM users WHERE role = 'admin' LIMIT 1")
    admin = cur.fetchone()
    if admin:
        cur.execute("UPDATE users SET balance = balance + %s WHERE id = %s", (advance, admin["id"]))
        cur.execute("""INSERT INTO transactions (user_id, boost_id, amount, type, comment)
                       VALUES (%s, %s, %s, %s, %s)""",
                    (admin["id"], boost_id, advance, "refund_in", f"Возврат с заказа #{row['order_no']}"))
    conn.commit()
    return {"ok": True, "refunded": advance}


@app.delete("/api/boosts/{boost_id}")
def delete_boost(boost_id: int, x_user_id: int = Header(...), conn=Depends(get_db)):
    cur = conn.cursor()
    cur.execute("SELECT booster_id, status, order_no FROM boosts WHERE id = %s", (boost_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(404, "Заказ не найден")
    if row["booster_id"] is not None or row["status"] != "planned":
        raise HTTPException(400, "Нельзя удалить взятый заказ")
    cur.execute("SELECT role FROM users WHERE id = %s", (x_user_id,))
    me = cur.fetchone()
    is_admin = bool(me and me["role"] == "admin")
    if not is_admin:
        raise HTTPException(403, "Только админ может удалять заказы")
    cur.execute("DELETE FROM boosts WHERE id = %s", (boost_id,))
    conn.commit()
    return {"ok": True, "order_no": row["order_no"]}


@app.post("/api/boosts/transfer")
def transfer_boost(req: TransferBoost, x_user_id: int = Header(...), conn=Depends(get_db)):
    cur = conn.cursor()
    cur.execute("SELECT booster_id, status, order_no FROM boosts WHERE id = %s", (req.boost_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(404, "Заказ не найден")
    if row["status"] in ("completed", "refunded"):
        raise HTTPException(400, "Нельзя передать закрытый заказ")
    cur.execute("SELECT role, nickname FROM users WHERE id = %s", (x_user_id,))
    me = cur.fetchone()
    is_admin = bool(me and me["role"] == "admin")
    if not is_admin and row["booster_id"] != x_user_id:
        raise HTTPException(403, "Можно передавать только свои заказы")
    cur.execute("SELECT nickname FROM users WHERE id = %s", (req.to_user_id,))
    target = cur.fetchone()
    if not target:
        raise HTTPException(404, "Получатель не найден")
    old_booster = row["booster_id"]
    if old_booster and row["status"] == "active":
        cur.execute("SELECT booster_earn FROM boosts WHERE id = %s", (req.boost_id,))
        earn = float(cur.fetchone()["booster_earn"] or 0)
        advance = round(earn * ADVANCE_SHARE, 2)
        cur.execute("UPDATE users SET balance = balance - %s WHERE id = %s", (advance, old_booster))
        cur.execute("""INSERT INTO transactions (user_id, boost_id, amount, type, comment)
                       VALUES (%s, %s, %s, %s, %s)""",
                    (old_booster, req.boost_id, -advance, "transfer_out", f"Передача заказа #{row['order_no']}"))
        cur.execute("UPDATE users SET balance = balance + %s WHERE id = %s", (advance, req.to_user_id))
        cur.execute("""INSERT INTO transactions (user_id, boost_id, amount, type, comment)
                       VALUES (%s, %s, %s, %s, %s)""",
                    (req.to_user_id, req.boost_id, advance, "transfer_in", f"Принят заказ #{row['order_no']}"))
    cur.execute("UPDATE boosts SET booster_id = %s WHERE id = %s", (req.to_user_id, req.boost_id))
    cur.execute("""INSERT INTO notifications (user_id, type, title, body, related_id)
                   VALUES (%s, 'transfer', 'Вам передан заказ', %s, %s)""",
                (req.to_user_id, f"Заказ #{row['order_no']} передан от {me['nickname']}", req.boost_id))
    conn.commit()
    return {"ok": True}


# ==================== FUNPAY ====================
def _funpay_public_config():
    return [{"key": k, "chat_id": c["chat_id"], "chat_name": c["name"],
             "configured": bool(c["chat_id"])} for k, c in FUNPAY_PUBLIC_CHATS.items()]


def _funpay_public_by_key(key):
    cfg = FUNPAY_PUBLIC_CHATS.get(str(key))
    if not cfg or not cfg.get("chat_id"):
        raise HTTPException(503, f"Публичный чат {key} не настроен")
    return cfg


def _funpay_public_db_id(key):
    return f"public:{key}"


def _funpay_public_key_from_db_id(value):
    text = str(value)
    return text.split(":", 1)[1] if text.startswith("public:") else None


def _funpay_get_session(account):
    """Пробуем достать requests.Session из Account FunPayAPI (атрибут меняется между версиями)."""
    for attr in ("session", "_FunPayAccount__session", "_session", "runner"):
        obj = getattr(account, attr, None)
        if obj is None:
            continue
        sess = getattr(obj, "session", obj)
        if sess is not None and hasattr(sess, "get") and hasattr(sess, "post"):
            return sess
    # Фолбэк — перебор всех атрибутов
    for name in dir(account):
        try:
            obj = getattr(account, name)
            if hasattr(obj, "get") and hasattr(obj, "post") and hasattr(obj, "cookies"):
                return obj
        except Exception:
            continue
    return None


# ---------- HTML-ПАРСЕР ЛИЧНЫХ КОНТАКТОВ ----------
def _funpay_sync_contacts_html(user_id, account):
    session = _funpay_get_session(account)
    if not session:
        print("[funpay_sync_contacts] session not found in account object")
        return 0
    try:
        r = session.get("https://funpay.com/chat/", timeout=15)
    except Exception as e:
        print(f"[funpay_sync_contacts] request failed: {e}")
        return 0
    if r.status_code != 200:
        print(f"[funpay_sync_contacts] HTTP {r.status_code}")
        return 0
    soup = BeautifulSoup(r.text, "lxml")
    contacts = soup.select("a.contact-item")
    count = 0
    for a in contacts:
        try:
            chat_id = a.get("data-id")
            if not chat_id:
                continue
            name_el = a.select_one(".media-user-name")
            name = name_el.get_text(strip=True) if name_el else "FunPay"
            last_el = a.select_one(".contact-item-message")
            last_msg = last_el.get_text(" ", strip=True) if last_el else ""
            _funpay_save_chat(user_id, str(chat_id), name, last_msg, False)
            count += 1
        except Exception:
            continue
    print(f"[funpay_sync_contacts] synced {count} chats")
    return count


# ---------- HTML-ПАРСЕР ОБЩИХ ЧАТОВ ----------
def _funpay_sync_public_chat(user_id, account, key):
    """Общие чаты парсим через HTML, /runner/ у FunPay сейчас отдаёт 400."""
    cfg = _funpay_public_by_key(key)
    node = cfg.get("node")
    if not node:
        return False
    session = _funpay_get_session(account)
    if not session:
        return False
    url = f"https://funpay.com/chat/?node={node}"
    try:
        r = session.get(url, timeout=15)
    except Exception as e:
        print(f"[funpay_public_sync] {key}: request failed: {e}")
        return False
    if r.status_code != 200:
        print(f"[funpay_public_sync] {key}: HTTP {r.status_code}")
        return False
    soup = BeautifulSoup(r.text, "lxml")
    items = soup.select("div.chat-msg-item")
    last_text = ""
    saved = 0
    for item in items:
        try:
            msg_id = (item.get("id") or "").replace("message-", "") or None
            author_el = item.select_one(".chat-msg-author-link")
            author_name = author_el.get_text(strip=True) if author_el else "FunPay"
            author_id = None
            if author_el and author_el.get("href"):
                mm = re.search(r"/users/(\d+)", author_el.get("href"))
                if mm:
                    author_id = mm.group(1)
            date_el = item.select_one(".chat-msg-date")
            created_at = None
            if date_el:
                try:
                    title = date_el.get("title") or ""
                    created_at = datetime.datetime.strptime(title, "%d %B, %H:%M:%S")
                except Exception:
                    created_at = datetime.datetime.utcnow()
            text_el = item.select_one(".chat-msg-text")
            text = text_el.get_text(" ", strip=True) if text_el else ""
            if text:
                last_text = text
            _funpay_save_message(user_id, _funpay_public_db_id(key), msg_id,
                                 author_id, author_name, text, False, created_at)
            saved += 1
        except Exception:
            continue
    _funpay_save_chat(user_id, _funpay_public_db_id(key), cfg["name"], last_text, False)
    print(f"[funpay_public_sync] {key}: synced {saved} messages")
    return True


def _funpay_public_sync_loop(user_id):
    while not FUNPAY_STOP.get(user_id):
        try:
            with FUNPAY_ACCOUNT_LOCK:
                account = FUNPAY_ACCOUNTS.get(user_id)
            if account:
                for key in FUNPAY_PUBLIC_CHATS.keys():
                    try:
                        _funpay_sync_public_chat(user_id, account, key)
                    except Exception as e:
                        print(f"[funpay_public_sync] {key}: {e}")
        except Exception:
            pass
        for _ in range(30):
            if FUNPAY_STOP.get(user_id):
                return
            time.sleep(1)


# ---------- БД-ХЕЛПЕРЫ ----------
def _funpay_order_to_boost(order, owner_id):
    oid = str(getattr(order, "id", ""))
    buyer = str(getattr(order, "buyer_username", ""))
    price = float(getattr(order, "price", 0) or 0)
    desc = str(getattr(order, "description", "") or "")
    title = str(getattr(order, "title", "") or "")
    text = (title + " " + desc).strip()
    game = "Dota 2" if re.search(r"dota|mmr", text, re.I) else (
        "CS2" if re.search(r"cs2|faceit|premier", text, re.I) else "FunPay")
    return oid, buyer, price, text, game


def _funpay_save_chat(user_id, chat_id, chat_name, last_message_text=None, unread=False):
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur = conn.cursor()
        cur.execute("""INSERT INTO funpay_chats(user_id,chat_id,chat_name,last_message_text,unread,last_synced_at)
                       VALUES(%s,%s,%s,%s,%s,NOW())
                       ON CONFLICT(user_id,chat_id) DO UPDATE SET
                       chat_name=COALESCE(EXCLUDED.chat_name,funpay_chats.chat_name),
                       last_message_text=COALESCE(EXCLUDED.last_message_text,funpay_chats.last_message_text),
                       unread=EXCLUDED.unread,last_synced_at=NOW()""",
                    (user_id, str(chat_id), chat_name or "FunPay",
                     last_message_text or "", bool(unread)))
        conn.commit()
    finally:
        conn.close()


def _funpay_save_message(user_id, chat_id, message_id, author_id, author_name,
                         text, by_bot=False, created_at=None):
    if not message_id:
        return
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur = conn.cursor()
        cur.execute("""INSERT INTO funpay_messages(user_id,chat_id,message_id,author_id,author_name,message,by_bot,created_at)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT(user_id,chat_id,message_id) DO UPDATE SET
                       message=EXCLUDED.message,author_name=EXCLUDED.author_name,by_bot=EXCLUDED.by_bot""",
                    (user_id, str(chat_id), str(message_id), str(author_id or ""),
                     author_name or "FunPay", text or "", bool(by_bot),
                     created_at or datetime.datetime.utcnow()))
        cur.execute("""UPDATE funpay_chats SET last_message_text=%s, unread=%s, last_synced_at=NOW()
                       WHERE user_id=%s AND chat_id=%s""",
                    (text or "", not bool(by_bot), user_id, str(chat_id)))
        conn.commit()
    finally:
        conn.close()


# ---------- ФОНОВЫЙ ВОРКЕР ----------
def _funpay_worker(user_id, golden_key):
    if FunPayAccount is None:
        return
    try:
        account = FunPayAccount(golden_key).get()
        with FUNPAY_ACCOUNT_LOCK:
            FUNPAY_ACCOUNTS[user_id] = account

        # Первичная синхронизация
        try:
            _funpay_sync_contacts_html(user_id, account)
        except Exception as e:
            print(f"[funpay_worker.initial_html] {e}")

        # Периодический поллинг контактов каждые 30 секунд
        def _poll_contacts():
            while not FUNPAY_STOP.get(user_id):
                try:
                    with FUNPAY_ACCOUNT_LOCK:
                        acc = FUNPAY_ACCOUNTS.get(user_id)
                    if acc:
                        _funpay_sync_contacts_html(user_id, acc)
                except Exception:
                    pass
                time.sleep(30)

        threading.Thread(target=_poll_contacts, daemon=True).start()

        # /runner/ у FunPay отдаёт 400 — не используем. Просто живём.
        while not FUNPAY_STOP.get(user_id):
            time.sleep(10)
    except Exception as e:
        print(f"[funpay_worker] {e}")
        return


def _import_funpay_order(owner_id, external_id, buyer, price, description, game):
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM boosts WHERE source='funpay' AND external_order_id=%s", (external_id,))
        if cur.fetchone():
            return
        cur.execute("SELECT role FROM users WHERE id=%s", (owner_id,))
        owner = cur.fetchone()
        if not owner:
            return
        cur.execute("SELECT COALESCE(MAX(CAST(order_no AS INTEGER)),0)+1 AS nxt FROM boosts")
        order_no = f"{int(cur.fetchone()['nxt']):07d}"
        cur.execute("""INSERT INTO boosts(order_no,booster_id,source,source_owner_id,external_order_id,
                       source_meta,game,client_steam_id,client_nickname,start_value,target_value,total_price,
                       commission,booster_earn,estimated_hours,deadline_days,options,notes,status)
                       VALUES(%s,NULL,'funpay',%s,%s,%s,%s,'',%s,0,0,%s,0,0,0,7,%s,%s,'planned')
                       RETURNING id""",
                    (order_no, owner_id, external_id, description, game, buyer, price,
                     json.dumps({"funpay": True}), "FunPay-заказ"))
        bid = cur.fetchone()["id"]
        cur.execute("""INSERT INTO notifications(user_id,type,title,body,related_id)
                       VALUES(%s,'funpay','🟡 Новый FunPay-заказ',%s,%s)""",
                    (owner_id, f"#{order_no} • {price:.0f} ₽ • ваш заказ", bid))
        conn.commit()
    finally:
        conn.close()


# ---------- ПРОФИЛИ ----------
def _funpay_get_account_for_user(user_id, conn):
    with FUNPAY_ACCOUNT_LOCK:
        account = FUNPAY_ACCOUNTS.get(user_id)
    if account:
        return account
    cur = conn.cursor()
    cur.execute("SELECT golden_key FROM funpay_accounts WHERE user_id=%s AND funpay_connected=TRUE", (user_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(409, "FunPay не подключён")
    try:
        account = FunPayAccount(decrypt_funpay_token(row["golden_key"])).get()
        with FUNPAY_ACCOUNT_LOCK:
            FUNPAY_ACCOUNTS[user_id] = account
        return account
    except Exception:
        raise HTTPException(502, "Не удалось подключиться к FunPay")


def _num_from_text(text):
    try:
        return float(str(text).replace(" ", "").replace(",", "."))
    except Exception:
        return None


def _parse_funpay_reviews(html):
    soup = BeautifulSoup(html or "", "lxml")
    reviews = []
    nodes = soup.select("div.review, div.review-item, div.tc-review, div.media-review, li.review")
    for i, node in enumerate(nodes[:100]):
        text_node = node.select_one(".review-text, .review-message, .review-content, .tc-review-text, .media-body")
        text = (text_node.get_text(" ", strip=True) if text_node else node.get_text(" ", strip=True))
        if not text:
            continue
        author_node = node.select_one(".media-user-name, .review-author, .user-link-name, a[href*='/users/']")
        author = author_node.get_text(" ", strip=True) if author_node else "Покупатель"
        stars = None
        cls = " ".join(node.get("class", [])) + " " + node.get_text(" ", strip=True)
        m = re.search(r"([1-5])\s*(?:/\s*5|звезд|stars?)", cls, re.I)
        if m:
            stars = int(m.group(1))
        if stars is None:
            star_nodes = node.select(".rating, .stars, [class*='star']")
            for st in star_nodes:
                mm = re.search(r"([1-5])", st.get_text(" ", strip=True))
                if mm:
                    stars = int(mm.group(1)); break
        href = author_node.get("href", "") if author_node else ""
        aid = None
        mm = re.search(r"/users/(\d+)", href)
        if mm:
            aid = mm.group(1)
        reviews.append({
            "review_id": f"html-{i}-{hashlib.sha1((author+'|'+text).encode()).hexdigest()[:12]}",
            "author_id": aid, "author_name": author, "rating": stars,
            "text": text, "reply": "", "created_at": None,
        })
    return reviews


def _lot_to_dict(lot):
    sub = getattr(lot, "subcategory", None)
    subname = str(getattr(sub, "name", "") or getattr(sub, "title", "") or "")
    title = str(getattr(lot, "title", None) or getattr(lot, "description", None) or "")
    server_name = str(getattr(lot, "server", None) or "")
    price = float(getattr(lot, "price", 0) or 0)
    lid = str(getattr(lot, "id", ""))
    link = getattr(lot, "public_link", None) or (f"https://funpay.com/lots/offer?id={lid}" if lid else "")
    text = (subname + " " + title + " " + server_name).strip()
    game = "Dota 2" if re.search(r"dota|ммр|mmr", text, re.I) else (
        "CS2" if re.search(r"cs2|counter.?strike|faceit|premier", text, re.I) else None)
    boost = bool(game and re.search(r"буст|boost|mmr|ммр|рейтинг|rank|калибр|calibr", text, re.I))
    return {"lot_id": lid, "game": game, "title": title or subname,
            "price": price, "url": link, "text": text, "boost": boost}


def _funpay_profile_payload(profile):
    html = getattr(profile, "html", "") or ""
    soup = BeautifulSoup(html, "lxml")
    username = str(getattr(profile, "username", "") or "")
    avatar = str(getattr(profile, "profile_photo", "") or "")
    if not avatar:
        av = soup.select_one(".avatar-photo")
        if av:
            style = av.get("style", "")
            mm = re.search(r"url\\?\(['\"]?([^'\")]+)", style)
            if mm:
                avatar = mm.group(1)
    status = "Онлайн" if bool(getattr(profile, "online", False)) else "Оффлайн"
    if getattr(profile, "banned", False):
        status = "Заблокирован"
    text = soup.get_text(" ", strip=True)
    rating = None
    for sel in [".rating-value", ".user-rating", ".rating", "[class*='rating']"]:
        n = soup.select_one(sel)
        if n:
            mm = re.search(r"(?:^|\s)([0-5](?:[\.,]\d+)?)\s*(?:/\s*5)?", n.get_text(" ", strip=True))
            if mm:
                rating = _num_from_text(mm.group(1)); break
    if rating is None:
        mm = re.search(r"(?:рейтинг|rating)\s*[:\-]?\s*([0-5](?:[\.,]\d+)?)", text, re.I)
        if mm:
            rating = _num_from_text(mm.group(1))
    reviews = _parse_funpay_reviews(html)
    review_count = len(reviews)
    mm = re.search(r"(?:отзыв(?:ов|а)?|reviews?)\s*[:\-]?\s*(\d+)", text, re.I)
    if mm:
        review_count = max(review_count, int(mm.group(1)))
    return {"id": str(getattr(profile, "id", "")), "username": username,
            "avatar_url": avatar, "status": status, "rating": rating,
            "review_count": review_count,
            "profile_url": f"https://funpay.com/users/{getattr(profile,'id','')}/",
            "reviews": reviews, "html": html}


def _cache_funpay_profile(conn, data):
    cur = conn.cursor()
    cur.execute("""INSERT INTO funpay_profiles(funpay_user_id,username,avatar_url,rating,review_count,status,profile_url,raw_profile,last_synced_at)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                   ON CONFLICT(funpay_user_id) DO UPDATE SET
                   username=EXCLUDED.username,avatar_url=EXCLUDED.avatar_url,rating=EXCLUDED.rating,
                   review_count=EXCLUDED.review_count,status=EXCLUDED.status,profile_url=EXCLUDED.profile_url,
                   raw_profile=EXCLUDED.raw_profile,last_synced_at=NOW()""",
                (data["id"], data["username"], data.get("avatar_url"), data.get("rating"),
                 data.get("review_count", 0), data.get("status", ""), data.get("profile_url"),
                 json.dumps({k: v for k, v in data.items() if k not in ("html", "reviews")},
                            ensure_ascii=False)))
    cur.execute("DELETE FROM funpay_profile_reviews WHERE funpay_user_id=%s", (data["id"],))
    for r in data.get("reviews", [])[:100]:
        cur.execute("""INSERT INTO funpay_profile_reviews(funpay_user_id,review_id,author_id,author_name,rating,text,reply,created_at)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT(funpay_user_id,review_id) DO UPDATE SET
                       text=EXCLUDED.text,rating=EXCLUDED.rating""",
                    (data["id"], r["review_id"], r.get("author_id"),
                     r.get("author_name", ""), r.get("rating"), r.get("text", ""),
                     r.get("reply", ""), None))
    conn.commit()


def _funpay_public_profile(user_id, target_id, conn, force=False):
    target_id = str(target_id)
    cur = conn.cursor()
    cur.execute("SELECT * FROM funpay_profiles WHERE funpay_user_id=%s", (target_id,))
    cached = cur.fetchone()
    fresh = False
    if cached and cached.get("last_synced_at"):
        fresh = (datetime.datetime.now(datetime.timezone.utc) -
                 (cached["last_synced_at"].replace(tzinfo=datetime.timezone.utc)
                  if cached["last_synced_at"].tzinfo is None else cached["last_synced_at"])
                 ).total_seconds() < 600
    if not cached or force or not fresh:
        account = _funpay_get_account_for_user(user_id, conn)
        try:
            profile = account.get_user(int(target_id))
        except Exception:
            raise HTTPException(404, "Профиль FunPay не найден")
        data = _funpay_profile_payload(profile)
        _cache_funpay_profile(conn, data)
    cur.execute("SELECT * FROM funpay_profiles WHERE funpay_user_id=%s", (target_id,))
    p = cur.fetchone()
    cur.execute("""SELECT review_id,author_id,author_name,rating,text,reply,created_at
                   FROM funpay_profile_reviews WHERE funpay_user_id=%s
                   ORDER BY created_at DESC NULLS LAST LIMIT 50""", (target_id,))
    reviews = rows_to_json(cur.fetchall())
    lots = []
    account = _funpay_get_account_for_user(user_id, conn)
    try:
        profile = account.get_user(int(target_id))
        raw_lots = getattr(profile, "get_lots", lambda: [])() or []
    except Exception:
        raw_lots = []
    for lot in raw_lots:
        d = _lot_to_dict(lot)
        if d["boost"]:
            lots.append(d)
    cur.execute("DELETE FROM funpay_profile_lots WHERE funpay_user_id=%s", (target_id,))
    for l in lots:
        cur.execute("""INSERT INTO funpay_profile_lots(funpay_user_id,lot_id,game,title,price,url,raw_lot,updated_at)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,NOW())
                       ON CONFLICT(funpay_user_id,lot_id) DO UPDATE SET
                       price=EXCLUDED.price,title=EXCLUDED.title,raw_lot=EXCLUDED.raw_lot,updated_at=NOW()""",
                    (target_id, l["lot_id"], l["game"], l["title"], l["price"], l["url"],
                     json.dumps(l, ensure_ascii=False)))
    conn.commit()
    return {"profile": dict(p) if p else {}, "reviews": reviews, "lots": lots,
            "source": "funpay", "cached_for_seconds": 600}


@app.get("/api/funpay/profile/me")
def funpay_profile_me(x_user_id: int = Header(...), conn=Depends(get_db)):
    cur = conn.cursor()
    cur.execute("SELECT funpay_connected FROM funpay_accounts WHERE user_id=%s", (x_user_id,))
    row = cur.fetchone()
    if not row or not row["funpay_connected"]:
        raise HTTPException(409, "Сначала подключи FunPay")
    account = _funpay_get_account_for_user(x_user_id, conn)
    fid = getattr(account, "id", None)
    if not fid:
        raise HTTPException(502, "FunPay не вернул ID профиля")
    return _funpay_public_profile(x_user_id, fid, conn, force=True)


@app.get("/api/funpay/profile/{target_id}")
def funpay_profile(target_id: str, x_user_id: int = Header(...), conn=Depends(get_db)):
    if not str(target_id).isdigit():
        raise HTTPException(400, "Некорректный FunPay ID")
    return _funpay_public_profile(x_user_id, target_id, conn, force=False)


@app.get("/api/funpay/pricing")
def funpay_pricing(current_mmr: int, target_mmr: int = 0, game: str = "Dota 2",
                   x_user_id: int = Header(...), conn=Depends(get_db)):
    if current_mmr < 0:
        raise HTTPException(400, "Некорректный текущий MMR")
    data = _funpay_public_profile(
        x_user_id, getattr(_funpay_get_account_for_user(x_user_id, conn), "id", 0), conn, force=False)
    lots = [x for x in data["lots"] if x.get("game") == game]

    def score(lot):
        text = (lot.get("title", "") + " " + lot.get("text", "")).lower()
        nums = [int(x) for x in re.findall(r"(?<!\d)(\d{3,5})(?!\d)", text)]
        in_range = 0
        if len(nums) >= 2:
            lo, hi = sorted(nums[:2])
            in_range = 1 if lo <= current_mmr <= hi else 0
        dist = min([abs(current_mmr - n) for n in nums], default=999999)
        return (in_range, -dist, lot.get("price", 0))

    lots = sorted(lots, key=score, reverse=True)
    selected = lots[0] if lots else None
    if not selected:
        return {"source": "fallback", "matched_lot": None, "price_per_win": None, "lots": lots}
    text = selected.get("title", "")
    nums = [int(x) for x in re.findall(r"(?<!\d)(\d{2,5})\s*(?:mmr|ммр)", text, re.I)]
    unit = nums[0] if nums else 25
    price = float(selected.get("price") or 0)
    ppw = price / (unit / 25) if unit > 25 else price
    return {"source": "funpay_lot", "matched_lot": selected,
            "price_per_win": round(ppw, 2), "unit_mmr": unit, "lots": lots}


@app.get("/api/funpay")
def funpay_status(x_user_id: int = Header(...), conn=Depends(get_db)):
    cur = conn.cursor()
    cur.execute("""SELECT funpay_username, funpay_connected, funpay_connected_at
                   FROM funpay_accounts WHERE user_id = %s""", (x_user_id,))
    row = cur.fetchone()
    if not row or not row.get("funpay_connected"):
        return {"connected": False}
    connected_at = row.get("funpay_connected_at")
    return {"connected": True,
            "funpay_username": row.get("funpay_username") or "FunPay",
            "funpay_connected_at": connected_at.isoformat() if connected_at else None}


@app.post("/api/funpay/connect")
def funpay_connect(req: FunPayConnect, x_user_id: int = Header(...), conn=Depends(get_db)):
    if FunPayAccount is None:
        raise HTTPException(503, "FunPayAPI не установлена на сервере")
    token = req.golden_key.strip()
    if len(token) < 16:
        raise HTTPException(400, "Некорректный golden_key")
    try:
        account = FunPayAccount(token).get()
        with FUNPAY_ACCOUNT_LOCK:
            FUNPAY_ACCOUNTS[x_user_id] = account
        username = str(getattr(account, "username", "FunPay"))
    except Exception:
        raise HTTPException(400, "Не удалось авторизовать FunPay")
    cur = conn.cursor()
    cur.execute("""INSERT INTO funpay_accounts(user_id,golden_key,funpay_username,funpay_connected,funpay_connected_at)
                   VALUES(%s,%s,%s,TRUE,NOW())
                   ON CONFLICT(user_id) DO UPDATE SET
                   golden_key=EXCLUDED.golden_key,funpay_username=EXCLUDED.funpay_username,
                   funpay_connected=TRUE,funpay_connected_at=NOW()""",
                (x_user_id, encrypt_funpay_token(token), username))
    conn.commit()
    FUNPAY_STOP[x_user_id] = False
    if x_user_id not in FUNPAY_THREADS or not FUNPAY_THREADS[x_user_id].is_alive():
        t = threading.Thread(target=_funpay_worker, args=(x_user_id, token), daemon=True)
        FUNPAY_THREADS[x_user_id] = t; t.start()
    if x_user_id not in FUNPAY_PUBLIC_THREADS or not FUNPAY_PUBLIC_THREADS[x_user_id].is_alive():
        pt = threading.Thread(target=_funpay_public_sync_loop, args=(x_user_id,), daemon=True)
        FUNPAY_PUBLIC_THREADS[x_user_id] = pt; pt.start()
    return {"ok": True, "username": username}


@app.delete("/api/funpay")
def funpay_disconnect(x_user_id: int = Header(...), conn=Depends(get_db)):
    FUNPAY_STOP[x_user_id] = True
    FUNPAY_PUBLIC_THREADS.pop(x_user_id, None)
    with FUNPAY_ACCOUNT_LOCK:
        FUNPAY_ACCOUNTS.pop(x_user_id, None)
    cur = conn.cursor()
    cur.execute("UPDATE funpay_accounts SET funpay_connected=FALSE WHERE user_id=%s", (x_user_id,))
    conn.commit()
    return {"ok": True}


@app.get("/api/funpay/public-chats")
def funpay_public_chats(x_user_id: int = Header(...), conn=Depends(get_db)):
    return [{"chat_id": _funpay_public_db_id(item["key"]),
             "chat_name": item["chat_name"], "configured": item["configured"],
             "public": True} for item in _funpay_public_config()]


@app.get("/api/funpay/chats")
def funpay_chats(x_user_id: int = Header(...), conn=Depends(get_db)):
    cur = conn.cursor()
    cur.execute("""SELECT chat_id,chat_name,last_message_text,unread,last_synced_at
                   FROM funpay_chats WHERE user_id=%s
                   ORDER BY unread DESC, last_synced_at DESC""", (x_user_id,))
    return rows_to_json(cur.fetchall())


@app.get("/api/funpay/chats/{chat_id}/messages")
def funpay_chat_messages(chat_id: str, x_user_id: int = Header(...), conn=Depends(get_db)):
    public_key = _funpay_public_key_from_db_id(chat_id)
    if public_key:
        with FUNPAY_ACCOUNT_LOCK:
            account = FUNPAY_ACCOUNTS.get(x_user_id)
        if not account:
            account = _funpay_get_account_for_user(x_user_id, conn)
        try:
            _funpay_sync_public_chat(x_user_id, account, public_key)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"Не удалось получить историю публичного чата: {e}")
    cur = conn.cursor()
    cur.execute("SELECT chat_id,chat_name FROM funpay_chats WHERE user_id=%s AND chat_id=%s",
                (x_user_id, chat_id))
    chat = cur.fetchone()
    if not chat:
        raise HTTPException(404, "Чат FunPay не найден")
    cur.execute("""SELECT message_id,author_id,author_name,message,by_bot,created_at
                   FROM funpay_messages WHERE user_id=%s AND chat_id=%s
                   ORDER BY created_at ASC LIMIT 200""", (x_user_id, chat_id))
    return {"chat": dict(chat), "messages": rows_to_json(cur.fetchall())}


@app.post("/api/funpay/chats/{chat_id}/messages")
def funpay_send_message(chat_id: str, req: FunPaySendMessage, x_user_id: int = Header(...),
                        conn=Depends(get_db)):
    text = req.message.strip()
    if not text:
        raise HTTPException(400, "Пустое сообщение")
    if len(text) > 4000:
        raise HTTPException(400, "Сообщение слишком длинное")
    cur = conn.cursor()
    public_key = _funpay_public_key_from_db_id(chat_id)
    if public_key:
        chat = {"chat_name": _funpay_public_by_key(public_key)["name"]}
    else:
        cur.execute("SELECT chat_name FROM funpay_chats WHERE user_id=%s AND chat_id=%s",
                    (x_user_id, chat_id))
        chat = cur.fetchone()
        if not chat:
            raise HTTPException(404, "Чат FunPay не найден")
    account = None
    with FUNPAY_ACCOUNT_LOCK:
        account = FUNPAY_ACCOUNTS.get(x_user_id)
    if account is None:
        cur.execute("SELECT golden_key FROM funpay_accounts WHERE user_id=%s AND funpay_connected=TRUE",
                    (x_user_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(409, "FunPay не подключён")
        try:
            account = FunPayAccount(decrypt_funpay_token(row["golden_key"])).get()
            with FUNPAY_ACCOUNT_LOCK:
                FUNPAY_ACCOUNTS[x_user_id] = account
        except Exception:
            raise HTTPException(502, "Не удалось подключиться к FunPay")
    try:
        raw_chat_id = _funpay_public_by_key(public_key)["chat_id"] if public_key else chat_id
        sent = account.send_message(int(raw_chat_id) if str(raw_chat_id).isdigit() else raw_chat_id, text=text)
    except Exception as ex:
        raise HTTPException(502, f"FunPay не принял сообщение: {ex}")
    message_id = str(getattr(sent, "id", f"local-{int(time.time()*1000)}"))
    author_id = getattr(sent, "author_id", None)
    author_name = getattr(sent, "author", None) or getattr(account, "username", "Вы")
    _funpay_save_message(x_user_id, chat_id, message_id, author_id, author_name, text, True)
    return {"ok": True, "message_id": message_id}


@app.post("/api/funpay/chats/{chat_id}/read")
def funpay_mark_chat_read(chat_id: str, x_user_id: int = Header(...), conn=Depends(get_db)):
    cur = conn.cursor()
    cur.execute("UPDATE funpay_chats SET unread=FALSE WHERE user_id=%s AND chat_id=%s",
                (x_user_id, chat_id))
    conn.commit()
    return {"ok": True}


@app.post("/api/funpay/{boost_id}/pool")
def funpay_put_to_pool(boost_id: int, executor_percent: float, x_user_id: int = Header(...),
                       conn=Depends(get_db)):
    cur = conn.cursor()
    cur.execute("SELECT source,source_owner_id,booster_id,status FROM boosts WHERE id=%s FOR UPDATE",
                (boost_id,))
    b = cur.fetchone()
    if not b:
        raise HTTPException(404, "Заказ не найден")
    if b["source"] != "funpay" or b["source_owner_id"] != x_user_id:
        raise HTTPException(403, "Это не ваш FunPay-заказ")
    if b["booster_id"] is not None or b["status"] != "planned":
        raise HTTPException(400, "Заказ уже недоступен")
    pct = float(executor_percent)
    if pct < 1 or pct > 100:
        raise HTTPException(400, "Процент исполнителю должен быть от 1 до 100")
    ex = pct / 100.0
    owner = max(0.0, 1.0 - ex)
    cur.execute("""UPDATE boosts SET pool_executor_share=%s,pool_owner_share=%s,
                   booster_share=%s,owner_share=%s,platform_share=0 WHERE id=%s""",
                (ex, owner, ex, owner, boost_id))
    cur.execute("SELECT id FROM users WHERE role='booster' AND id<>%s", (x_user_id,))
    for u in cur.fetchall():
        cur.execute("""INSERT INTO notifications(user_id,type,title,body,related_id)
                       VALUES(%s,'new_boost','🌌 FunPay-заказ в пул',%s,%s)""",
                    (u["id"], f"#{boost_id} • исполнителю {pct:.0f}% • владельцу {100-pct:.0f}%", boost_id))
    conn.commit()
    return {"ok": True, "executor_percent": pct, "owner_percent": 100 - pct,
            "message": "Заказ помещён в общий пул"}


# ==================== ПРОФИЛЬ ====================
@app.get("/api/profile/{user_id}")
def get_profile(user_id: int, conn=Depends(get_db)):
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
    user = cur.fetchone()
    if not user:
        raise HTTPException(404, "Пользователь не найден")
    cur.execute("""SELECT
        COALESCE(SUM(CASE WHEN status='planned' THEN 1 END), 0) as planned,
        COALESCE(SUM(CASE WHEN status='active' THEN 1 END), 0) as active,
        COALESCE(SUM(CASE WHEN status='completed' THEN 1 END), 0) as completed,
        COALESCE(SUM(CASE WHEN status='refunded' THEN 1 END), 0) as refunded
        FROM boosts WHERE booster_id = %s""", (user_id,))
    counts = cur.fetchone()
    cur.execute("""SELECT COALESCE(SUM(amount), 0) AS earned FROM transactions
                   WHERE user_id = %s AND type IN ('earning','advance')""", (user_id,))
    total_earned = cur.fetchone()["earned"]
    today = datetime.date.today().isoformat()
    week_ago = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()
    month_ago = (datetime.date.today() - datetime.timedelta(days=30)).isoformat()
    cur.execute("""SELECT
        COALESCE(SUM(CASE WHEN date(date) = %s THEN amount END), 0) as today,
        COALESCE(SUM(CASE WHEN date(date) >= %s THEN amount END), 0) as week,
        COALESCE(SUM(CASE WHEN date(date) >= %s THEN amount END), 0) as month
        FROM transactions WHERE user_id = %s AND type IN ('earning','advance')""",
        (today, week_ago, month_ago, user_id))
    periods = cur.fetchone()
    cur.execute("""SELECT COALESCE(AVG(booster_earn), 0) as avg_check,
                   COALESCE(SUM(estimated_hours), 0) as total_hours,
                   COUNT(*) as completed_count
                   FROM boosts WHERE booster_id = %s AND status = 'completed'""", (user_id,))
    agg = cur.fetchone()
    avg_hourly = (total_earned / agg["total_hours"]) if agg["total_hours"] else 0
    cur.execute("SELECT game, COUNT(*) as c FROM boosts WHERE booster_id = %s GROUP BY game ORDER BY c DESC LIMIT 1",
                (user_id,))
    fav = cur.fetchone()
    favorite_game = fav["game"] if fav else "—"
    cur.execute("SELECT * FROM transactions WHERE user_id = %s ORDER BY id DESC LIMIT 30", (user_id,))
    history = cur.fetchall()
    for h in history:
        if isinstance(h["date"], datetime.datetime):
            h["date"] = h["date"].isoformat()
    daily = []
    for i in range(13, -1, -1):
        d = (datetime.date.today() - datetime.timedelta(days=i)).isoformat()
        cur.execute("""SELECT COALESCE(SUM(amount), 0) as s
                       FROM transactions WHERE user_id = %s
                       AND type IN ('earning','advance') AND date(date) = %s""", (user_id, d))
        daily.append({"date": d, "amount": float(cur.fetchone()["s"] or 0)})
    return {"user": {"id": user["id"], "key": user["key"], "nickname": user["nickname"],
                     "role": user["role"], "balance": float(user["balance"] or 0),
                     "hourly_rate": user["hourly_rate"]},
            "statuses": {"planned": counts["planned"], "active": counts["active"],
                         "completed": counts["completed"], "refunded": counts["refunded"]},
            "total_earned": float(total_earned or 0),
            "earned_today": float(periods["today"] or 0),
            "earned_week": float(periods["week"] or 0),
            "earned_month": float(periods["month"] or 0),
            "avg_check": float(agg["avg_check"] or 0),
            "total_hours": float(agg["total_hours"] or 0),
            "completed_count": agg["completed_count"],
            "avg_hourly": float(avg_hourly),
            "favorite_game": favorite_game,
            "level": effective_level(user),
            "xp": int(user.get("xp") or 0),
            "admin_level_delta": int(user.get("admin_level_delta") or 0),
            "payout_percent": payout_percent(effective_level(user)),
            "history": history, "daily": daily}


@app.post("/api/profile/rate")
def update_rate(user_id: int = Header(...), rate: int = Header(...), conn=Depends(get_db)):
    cur = conn.cursor()
    cur.execute("UPDATE users SET hourly_rate = %s WHERE id = %s", (rate, user_id))
    conn.commit()
    return {"ok": True}


# ==================== ЧАТ ====================
@app.get("/api/chat/{user_id}")
def get_chat(user_id: int, with_user: int = 0, conn=Depends(get_db)):
    cur = conn.cursor()
    if with_user == 0:
        cur.execute("""SELECT cm.*, u.nickname as from_nickname
                       FROM chat_messages cm JOIN users u ON u.id = cm.from_user_id
                       WHERE cm.to_user_id IS NULL ORDER BY cm.id DESC LIMIT 100""")
    else:
        cur.execute("""SELECT cm.*, u.nickname as from_nickname
                       FROM chat_messages cm JOIN users u ON u.id = cm.from_user_id
                       WHERE (cm.from_user_id = %s AND cm.to_user_id = %s)
                          OR (cm.from_user_id = %s AND cm.to_user_id = %s)
                       ORDER BY cm.id ASC LIMIT 200""",
                    (user_id, with_user, with_user, user_id))
    rows = cur.fetchall()
    cur.execute("UPDATE chat_messages SET is_read = 1 WHERE to_user_id = %s AND is_read = 0", (user_id,))
    conn.commit()
    return rows_to_json(rows)


@app.post("/api/chat")
def send_chat(msg: ChatMessage, x_user_id: int = Header(...), conn=Depends(get_db)):
    cur = conn.cursor()
    cur.execute("""INSERT INTO chat_messages (from_user_id, to_user_id, boost_id, message)
                   VALUES (%s, %s, %s, %s) RETURNING id""",
                (x_user_id, msg.to_user_id, msg.boost_id, msg.message))
    mid = cur.fetchone()["id"]
    if msg.to_user_id:
        cur.execute("""INSERT INTO notifications (user_id, type, title, body, related_id)
                       VALUES (%s, %s, %s, %s, %s)""",
                    (msg.to_user_id, "chat", "Новое сообщение", msg.message[:100], mid))
    else:
        cur.execute("SELECT id FROM users WHERE id != %s", (x_user_id,))
        for u in cur.fetchall():
            cur.execute("""INSERT INTO notifications (user_id, type, title, body, related_id)
                           VALUES (%s, %s, %s, %s, %s)""",
                        (u["id"], "chat", "Новое сообщение в чате", msg.message[:100], mid))
    conn.commit()
    return {"id": mid}


# ==================== УВЕДОМЛЕНИЯ ====================
@app.get("/api/notifications/{user_id}")
def get_notifications(user_id: int, conn=Depends(get_db)):
    cur = conn.cursor()
    cur.execute("SELECT * FROM notifications WHERE user_id = %s ORDER BY id DESC LIMIT 50", (user_id,))
    return rows_to_json(cur.fetchall())


@app.post("/api/notifications/read-all/{user_id}")
def mark_all_read(user_id: int, conn=Depends(get_db)):
    cur = conn.cursor()
    cur.execute("UPDATE notifications SET is_read = 1 WHERE user_id = %s", (user_id,))
    conn.commit()
    return {"ok": True}


@app.get("/api/notifications/unread/{user_id}")
def unread_count(user_id: int, conn=Depends(get_db)):
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as c FROM notifications WHERE user_id = %s AND is_read = 0", (user_id,))
    return {"count": cur.fetchone()["c"]}


# ==================== АНАЛИЗ (DOTA) ====================
def _get_cache(steam32, conn):
    cur = conn.cursor()
    cur.execute("SELECT * FROM client_cache WHERE steam32_id = %s", (steam32,))
    return cur.fetchone()


def _save_cache(steam32, nick, rank_tier, lb, wr, total, age_days, top_heroes, score, signals, conn):
    cur = conn.cursor()
    cur.execute("""INSERT INTO client_cache (steam32_id, nickname, rank_tier, leaderboard_rank,
                    winrate, total_games, account_age_days, top_heroes, smurf_score,
                    smurf_signals, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (steam32_id) DO UPDATE SET
                    nickname = EXCLUDED.nickname, rank_tier = EXCLUDED.rank_tier,
                    leaderboard_rank = EXCLUDED.leaderboard_rank, winrate = EXCLUDED.winrate,
                    total_games = EXCLUDED.total_games, account_age_days = EXCLUDED.account_age_days,
                    top_heroes = EXCLUDED.top_heroes, smurf_score = EXCLUDED.smurf_score,
                    smurf_signals = EXCLUDED.smurf_signals, updated_at = EXCLUDED.updated_at""",
                (steam32, nick, rank_tier, lb, wr, total, age_days,
                 json.dumps(top_heroes), score, json.dumps(signals), datetime.datetime.now()))
    conn.commit()


async def _http_get(url, timeout=6):
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.get(url)
            if r.status_code == 200:
                return r.json()
    except Exception:
        pass
    return None


async def _analyze_dota_internal(steam32, conn):
    cached = _get_cache(steam32, conn)
    if cached:
        age = (datetime.datetime.now() - cached["updated_at"]).total_seconds()
        if age < 1800:
            result = dict(cached)
            result["top_heroes"] = json.loads(result["top_heroes"] or "[]")
            result["smurf_signals"] = json.loads(result["smurf_signals"] or "[]")
            if isinstance(result["updated_at"], datetime.datetime):
                result["updated_at"] = result["updated_at"].isoformat()
            result["from_cache"] = True
            return result
    profile, recent, first = await asyncio.gather(
        _http_get(f"{OPENDOTA_BASE}/players/{steam32}"),
        _http_get(f"{OPENDOTA_BASE}/players/{steam32}/recentMatches"),
        _http_get(f"{OPENDOTA_BASE}/players/{steam32}/matches?limit=1&sort=asc"),
    )
    if not profile:
        raise HTTPException(404, "Профиль не найден в OpenDota")
    nick = profile.get("profile", {}).get("personaname", "—")
    rank_tier = profile.get("rank_tier")
    lb = profile.get("leaderboard_rank")
    wins = profile.get("win", 0) or 0
    losses = profile.get("lose", 0) or 0
    total = wins + losses
    wr = (wins / total) if total else 0
    age_days = None
    if first and isinstance(first, list) and first:
        st = first[0].get("start_time")
        if st:
            age_days = (datetime.datetime.now() - datetime.datetime.fromtimestamp(st)).days
    score = 0
    signals = []
    if age_days is not None and age_days < 180:
        score += 25; signals.append(f"Аккаунт молодой: {age_days} дней")
    elif age_days is not None and age_days < 365:
        score += 15; signals.append(f"Аккаунт до года: {age_days} дней")
    if recent and isinstance(recent, list) and recent:
        rw = 0
        n = min(len(recent), 50)
        for m in recent[:n]:
            slot = m.get("player_slot", 0)
            is_rad = slot < 128
            radiant_win = m.get("radiant_win")
            if radiant_win is None:
                continue
            won = (radiant_win and is_rad) or (not radiant_win and not is_rad)
            if won:
                rw += 1
        recent_wr = rw / n if n else 0
        if recent_wr > 0.70:
            score += 30; signals.append(f"Винрейт последних игр: {recent_wr*100:.0f}%")
        elif recent_wr > 0.60:
            score += 15; signals.append(f"Повышенный винрейт: {recent_wr*100:.0f}%")
    if rank_tier and total < 300:
        medal = rank_tier // 10
        if medal >= 7 and total < 500:
            score += 25; signals.append(f"Высокий ранг ({medal}) при {total} играх")
    score = min(100, score)
    _save_cache(steam32, nick, rank_tier, lb, wr, total, age_days, [], score, signals, conn)
    return {"steam32_id": steam32, "nickname": nick, "rank_tier": rank_tier,
            "leaderboard_rank": lb, "winrate": wr, "total_games": total,
            "account_age_days": age_days, "top_heroes": [],
            "smurf_score": score, "smurf_signals": signals, "from_cache": False}


@app.get("/api/client/analyze/{steam32}")
async def analyze_client(steam32: str, conn=Depends(get_db)):
    try:
        return await asyncio.wait_for(_analyze_dota_internal(steam32, conn), timeout=8.0)
    except asyncio.TimeoutError:
        return {"error": "OpenDota не ответил за 8 секунд"}


# ==================== АНАЛИЗ (CS2 / FACEIT) ====================
@app.get("/api/client/analyze-cs2/{steam_id}")
async def analyze_cs2(steam_id: str):
    if not FACEIT_API_KEY:
        return {"error": "FACEIT_API_KEY не настроен"}
    headers = {"Authorization": f"Bearer {FACEIT_API_KEY}"}
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(f"{FACEIT_BASE}/players",
                            params={"game": "cs2", "game_player_id": steam_id},
                            headers=headers)
            if r.status_code != 200:
                return {"error": "Не удалось найти игрока на FACEIT"}
            player = r.json()
            pid = player.get("player_id")
            if not pid:
                return {"error": "Не удалось найти игрока на FACEIT"}
            stats_r = await c.get(f"{FACEIT_BASE}/players/{pid}/stats/cs2", headers=headers)
            stats = stats_r.json() if stats_r.status_code == 200 else {}
        games = player.get("games", {}).get("cs2", {})
        life = stats.get("lifetime", {}) if stats else {}
        return {"steam_id": steam_id, "player_id": pid,
                "nickname": player.get("nickname", "—"),
                "country": player.get("country", "—"),
                "level": games.get("skill_level"), "elo": games.get("faceit_elo"),
                "kd": life.get("Average K/D Ratio"),
                "hs": life.get("Average Headshots %"),
                "winrate": life.get("Win Rate %"),
                "matches": life.get("Matches"),
                "longest_win_streak": life.get("Longest Win Streak")}
    except Exception as e:
        return {"error": f"FACEIT недоступен: {e}"}


@app.get("/api/client/trust/{steam_id}")
async def check_trust(steam_id: str):
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(f"https://faceitfinder.com/profile/{steam_id}", follow_redirects=True)
        if r.status_code != 200:
            return {"trust_factor": None, "error": "Нет данных"}
        m = re.search(r"Trust\s*Factor[:\s]*(\d+)", r.text, re.IGNORECASE)
        if m:
            tf = int(m.group(1))
            level = "ЗЕЛЁНЫЙ" if tf >= 80 else ("ЖЁЛТЫЙ" if tf >= 50 else "КРАСНЫЙ")
            return {"trust_factor": tf, "level": level}
        return {"trust_factor": None, "error": "Не найдено"}
    except Exception as e:
        return {"trust_factor": None, "error": str(e)}


# ==================== АДМИН ====================
def require_admin(x_admin_key: str = Header(...)):
    if x_admin_key != ADMIN_KEY:
        raise HTTPException(403, "Доступ запрещён")


@app.get("/api/admin/stats")
def admin_stats(_=Depends(require_admin), conn=Depends(get_db)):
    cur = conn.cursor()
    cur.execute("""SELECT COALESCE(SUM(total_price),0) as turnover,
                          COALESCE(SUM(commission),0) as commission,
                          COALESCE(SUM(booster_earn),0) as paid
                   FROM boosts WHERE status = 'completed'""")
    fin = cur.fetchone()
    cur.execute("""SELECT
        COALESCE(SUM(CASE WHEN status='planned' THEN 1 END), 0) as planned,
        COALESCE(SUM(CASE WHEN status='active' THEN 1 END), 0) as active,
        COALESCE(SUM(CASE WHEN status='completed' THEN 1 END), 0) as completed,
        COALESCE(SUM(CASE WHEN status='refunded' THEN 1 END), 0) as refunded FROM boosts""")
    counts = cur.fetchone()
    today = datetime.date.today().isoformat()
    week_ago = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()
    month_ago = (datetime.date.today() - datetime.timedelta(days=30)).isoformat()
    cur.execute("""SELECT
        COALESCE(SUM(CASE WHEN date(date) = %s THEN amount END), 0) as today,
        COALESCE(SUM(CASE WHEN date(date) >= %s THEN amount END), 0) as week,
        COALESCE(SUM(CASE WHEN date(date) >= %s THEN amount END), 0) as month
        FROM transactions WHERE type = 'commission'""", (today, week_ago, month_ago))
    periods = cur.fetchone()
    cur.execute("SELECT COUNT(*) as c FROM users WHERE role = 'booster'")
    boosters = cur.fetchone()["c"]
    cur.execute("SELECT COALESCE(SUM(balance),0) as b FROM users WHERE role = 'booster'")
    total_balance = cur.fetchone()["b"]
    return {"turnover": float(fin["turnover"] or 0),
            "commission": float(fin["commission"] or 0),
            "paid": float(fin["paid"] or 0), "counts": counts,
            "c_today": float(periods["today"] or 0),
            "c_week": float(periods["week"] or 0),
            "c_month": float(periods["month"] or 0),
            "boosters": boosters,
            "total_balance": float(total_balance or 0)}


@app.get("/api/admin/users")
def admin_users(_=Depends(require_admin), conn=Depends(get_db)):
    cur = conn.cursor()
    cur.execute("""SELECT id, key, nickname, role, balance, hourly_rate, level, xp, admin_level_delta,
                          created_at, last_login FROM users ORDER BY id""")
    return rows_to_json(cur.fetchall())


@app.post("/api/admin/booster-level")
def admin_booster_level(user_id: int, delta: int, reason: str = "",
                        _=Depends(require_admin), conn=Depends(get_db)):
    if delta == 0 or abs(delta) > 50:
        raise HTTPException(400, "delta должен быть от -50 до 50 и не равен 0")
    cur = conn.cursor()
    cur.execute("SELECT role, level, xp, admin_level_delta, nickname FROM users WHERE id=%s FOR UPDATE",
                (user_id,))
    u = cur.fetchone()
    if not u or u["role"] != "booster":
        raise HTTPException(404, "Бустер не найден")
    old = effective_level(u)
    new = max(1, min(50, old + delta))
    actual_delta = new - old
    cur.execute("UPDATE users SET admin_level_delta=COALESCE(admin_level_delta,0)+%s WHERE id=%s",
                (actual_delta, user_id))
    cur.execute("""INSERT INTO level_audit(user_id,delta,old_level,new_level,reason,admin_id)
                   VALUES (%s,%s,%s,%s,%s,(SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1))""",
                (user_id, actual_delta, old, new, reason or "Ручная корректировка уровня"))
    cur.execute("""INSERT INTO notifications(user_id,type,title,body)
                   VALUES(%s,'level','Уровень изменён',%s)""",
                (user_id, f"LEVEL {old} → {new}. {reason or ''}"))
    conn.commit()
    return {"ok": True, "old_level": old, "level": new, "delta": actual_delta,
            "payout_percent": payout_percent(new)}


@app.post("/api/admin/balance")
def admin_balance(req: BalanceAdjust, _=Depends(require_admin), conn=Depends(get_db)):
    cur = conn.cursor()
    cur.execute("UPDATE users SET balance = balance + %s WHERE id = %s", (req.delta, req.user_id))
    cur.execute("""INSERT INTO transactions (user_id, amount, type, comment)
                   VALUES (%s, %s, %s, %s)""",
                (req.user_id, req.delta, "admin_adjust", req.comment))
    cur.execute("""INSERT INTO notifications (user_id, type, title, body)
                   VALUES (%s, %s, %s, %s)""",
                (req.user_id, "admin_message", "Изменение баланса",
                 f"{req.delta:+.0f} ₽ — {req.comment}"))
    conn.commit()
    return {"ok": True}


@app.get("/api/admin/keys")
def admin_keys(_=Depends(require_admin), conn=Depends(get_db)):
    cur = conn.cursor()
    cur.execute("SELECT * FROM keys ORDER BY created_at DESC")
    return rows_to_json(cur.fetchall())


@app.post("/api/admin/keys")
def admin_add_key(key: str = "", role: str = "booster", note: str = "",
                  _=Depends(require_admin), conn=Depends(get_db)):
    cur = conn.cursor()
    try:
        final_key = key.upper() if key else generate_activation_key()
        if role == "admin" and final_key != MASTER_ADMIN_KEY:
            raise HTTPException(403, "Дополнительные админские ключи отключены")
        cur.execute("INSERT INTO keys (key, role, note) VALUES (%s, %s, %s)",
                    (final_key, role, note))
        conn.commit()
        return {"ok": True, "key": final_key}
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
    cur.execute("""SELECT u.id, u.nickname, COALESCE(SUM(t.amount), 0) as earned
                   FROM users u LEFT JOIN transactions t
                   ON t.user_id = u.id AND t.type IN ('earning','advance')
                   WHERE u.role = 'booster'
                   GROUP BY u.id ORDER BY earned DESC LIMIT 5""")
    return rows_to_json(cur.fetchall())


@app.get("/api/admin/active-boosts")
def admin_active_boosts(_=Depends(require_admin), conn=Depends(get_db)):
    cur = conn.cursor()
    cur.execute("""SELECT b.*, COALESCE(u.nickname, '—') AS booster_nickname
                   FROM boosts b LEFT JOIN users u ON u.id = b.booster_id
                   WHERE b.status IN ('planned','active') ORDER BY b.id DESC""")
    return rows_to_json(cur.fetchall())


@app.get("/api/admin/boosters-stats")
def admin_boosters_stats(_=Depends(require_admin), conn=Depends(get_db)):
    cur = conn.cursor()
    cur.execute("""SELECT id, nickname, role, balance, hourly_rate, level, xp, admin_level_delta, last_login
                   FROM users ORDER BY role DESC, id ASC""")
    users = cur.fetchall()
    out = []
    for u in users:
        cur.execute("""SELECT
            COUNT(*) FILTER (WHERE status='planned')   AS planned,
            COUNT(*) FILTER (WHERE status='active')    AS active,
            COUNT(*) FILTER (WHERE status='completed') AS completed,
            COUNT(*) FILTER (WHERE status='refunded')  AS refunded,
            COALESCE(SUM(booster_earn) FILTER (WHERE status='completed'), 0) AS earned,
            COALESCE(AVG(booster_earn) FILTER (WHERE status='completed'), 0) AS avg_check,
            COALESCE(SUM(estimated_hours) FILTER (WHERE status='completed'), 0) AS hours
            FROM boosts WHERE booster_id = %s""", (u["id"],))
        s = cur.fetchone()
        hours = float(s["hours"] or 0)
        earned = float(s["earned"] or 0)
        cur.execute("""SELECT COALESCE(SUM(amount), 0) AS commission
                       FROM transactions WHERE user_id = %s AND type = 'commission'""", (u["id"],))
        comm = float(cur.fetchone()["commission"] or 0)
        out.append({
            "id": u["id"], "nickname": u["nickname"], "role": u["role"],
            "balance": float(u["balance"] or 0), "hourly_rate": u["hourly_rate"],
            "level": effective_level(u), "xp": int(u.get("xp") or 0),
            "admin_level_delta": int(u.get("admin_level_delta") or 0),
            "payout_percent": payout_percent(effective_level(u)),
            "last_login": u["last_login"],
            "planned": s["planned"], "active": s["active"],
            "completed": s["completed"], "refunded": s["refunded"],
            "earned": earned, "avg_check": float(s["avg_check"] or 0),
            "hours": hours, "avg_hourly": (earned / hours) if hours else 0,
            "commission": comm,
        })
    return out
