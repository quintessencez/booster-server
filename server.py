# server.py
"""Booster Platform API v4 — Neon + Render ready."""

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

app = FastAPI(title="Valve Games Booster API v4")

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise Exception("DATABASE_URL не задан")

ADMIN_KEY = os.environ.get("ADMIN_KEY", "TEST-KEY-FOR-ME-0001")
FACEIT_API_KEY = os.environ.get("FACEIT_API_KEY", "")
OPENDOTA_BASE = "https://api.opendota.com/api"
FACEIT_BASE = "https://open.faceit.com/data/v4"


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


# ==================== БАЗОВЫЕ ====================
@app.get("/")
def root():
    return {"status": "ok", "service": "Booster API v4"}


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
        # ключ уже привязан к этому устройству?
        if user["hwid"] != hwid_hash:
            raise HTTPException(403, "Ключ привязан к другому устройству")
        # ник привязан к ключу — менять нельзя
        if req.nickname and req.nickname != user["nickname"]:
            raise HTTPException(403, "Ник привязан к ключу и не может быть изменён")
        cur.execute("UPDATE users SET last_login = %s WHERE id = %s",
                    (datetime.datetime.now().isoformat(), user["id"]))
        conn.commit()
        return {"id": user["id"], "key": user["key"], "nickname": user["nickname"],
                "role": user["role"], "hourly_rate": user["hourly_rate"],
                "balance": float(user["balance"] or 0)}

    # первый вход — создаём
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
            "balance": float(user["balance"] or 0)}


# ==================== БУСТЫ ====================
@app.get("/api/boosts/{user_id}")
def get_boosts(user_id: int, status: str = "all", conn=Depends(get_db)):
    """Для бустера — только свои. Для админа — все."""
    cur = conn.cursor()
    cur.execute("SELECT role FROM users WHERE id = %s", (user_id,))
    row = cur.fetchone()
    is_admin = bool(row and row["role"] == "admin")

    if is_admin:
        if status == "all":
            cur.execute("""SELECT b.*, u.nickname AS booster_nickname
                           FROM boosts b JOIN users u ON u.id = b.booster_id
                           ORDER BY b.id DESC""")
        else:
            cur.execute("""SELECT b.*, u.nickname AS booster_nickname
                           FROM boosts b JOIN users u ON u.id = b.booster_id
                           WHERE b.status = %s ORDER BY b.id DESC""", (status,))
    else:
        if status == "all":
            cur.execute("SELECT * FROM boosts WHERE booster_id = %s ORDER BY id DESC",
                        (user_id,))
        else:
            cur.execute("SELECT * FROM boosts WHERE booster_id = %s AND status = %s ORDER BY id DESC",
                        (user_id, status))
    return rows_to_json(cur.fetchall())


@app.post("/api/boosts")
def create_boost(boost: BoostCreate, x_user_id: int = Header(...), conn=Depends(get_db)):
    cur = conn.cursor()
    cur.execute("""INSERT INTO boosts (booster_id, game, client_steam_id, client_nickname,
                    start_value, target_value, total_price, commission, booster_earn,
                    estimated_hours, deadline_days, options, notes)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                (x_user_id, boost.game, boost.client_steam_id, boost.client_nickname,
                 boost.start_value, boost.target_value, boost.total_price, boost.commission,
                 boost.booster_earn, boost.estimated_hours, boost.deadline_days,
                 boost.options, boost.notes))
    bid = cur.fetchone()["id"]
    conn.commit()
    cur.execute("SELECT id FROM users WHERE role = 'admin' LIMIT 1")
    admin = cur.fetchone()
    if admin:
        cur.execute("""INSERT INTO notifications (user_id, type, title, body, related_id)
                       VALUES (%s, %s, %s, %s, %s)""",
                    (admin["id"], "new_boost", f"Новый заказ #{bid}",
                     f"Игра: {boost.game}, цена: {boost.total_price} ₽", bid))
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
    cur.execute("UPDATE boosts SET status = 'completed', completed_at = %s WHERE id = %s", (now, boost_id))
    cur.execute("UPDATE users SET balance = balance + %s WHERE id = %s",
                (row["booster_earn"], row["booster_id"]))
    cur.execute("""INSERT INTO transactions (user_id, boost_id, amount, type, comment)
                   VALUES (%s, %s, %s, %s, %s)""",
                (row["booster_id"], boost_id, row["booster_earn"], "earning",
                 f"Завершён буст #{boost_id}"))
    cur.execute("SELECT id FROM users WHERE role = 'admin' LIMIT 1")
    admin = cur.fetchone()
    if admin and row["commission"] > 0:
        cur.execute("""INSERT INTO transactions (user_id, boost_id, amount, type, comment)
                       VALUES (%s, %s, %s, %s, %s)""",
                    (admin["id"], boost_id, row["commission"], "commission",
                     f"Комиссия с буста #{boost_id}"))
    conn.commit()
    return {"ok": True}


@app.post("/api/boosts/{boost_id}/refund")
def refund_boost(boost_id: int, conn=Depends(get_db)):
    cur = conn.cursor()
    cur.execute("UPDATE boosts SET status = 'refunded', completed_at = %s WHERE id = %s",
                (datetime.datetime.now().isoformat(), boost_id))
    conn.commit()
    return {"ok": True}


@app.post("/api/boosts/transfer")
def transfer_boost(req: TransferBoost, x_user_id: int = Header(...), conn=Depends(get_db)):
    cur = conn.cursor()
    cur.execute("SELECT booster_id, status FROM boosts WHERE id = %s", (req.boost_id,))
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

    cur.execute("UPDATE boosts SET booster_id = %s WHERE id = %s",
                (req.to_user_id, req.boost_id))
    cur.execute("""INSERT INTO notifications (user_id, type, title, body, related_id)
                   VALUES (%s, 'transfer', 'Вам передан заказ', %s, %s)""",
                (req.to_user_id,
                 f"Заказ #{req.boost_id} передан от {me['nickname']}",
                 req.boost_id))
    conn.commit()
    return {"ok": True}


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

    cur.execute("SELECT COALESCE(SUM(amount), 0) as earned FROM transactions WHERE user_id = %s AND type = 'earning'", (user_id,))
    total_earned = cur.fetchone()["earned"]

    today = datetime.date.today().isoformat()
    week_ago = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()
    month_ago = (datetime.date.today() - datetime.timedelta(days=30)).isoformat()

    cur.execute("""SELECT
        COALESCE(SUM(CASE WHEN date(date) = %s THEN amount END), 0) as today,
        COALESCE(SUM(CASE WHEN date(date) >= %s THEN amount END), 0) as week,
        COALESCE(SUM(CASE WHEN date(date) >= %s THEN amount END), 0) as month
        FROM transactions WHERE user_id = %s AND type = 'earning'""",
        (today, week_ago, month_ago, user_id))
    periods = cur.fetchone()

    cur.execute("""SELECT COALESCE(AVG(booster_earn), 0) as avg_check,
                   COALESCE(SUM(estimated_hours), 0) as total_hours,
                   COUNT(*) as completed_count
                   FROM boosts WHERE booster_id = %s AND status = 'completed'""", (user_id,))
    agg = cur.fetchone()
    avg_hourly = (total_earned / agg["total_hours"]) if agg["total_hours"] else 0

    cur.execute("SELECT game, COUNT(*) as c FROM boosts WHERE booster_id = %s GROUP BY game ORDER BY c DESC LIMIT 1", (user_id,))
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
                       FROM transactions WHERE user_id = %s AND type = 'earning'
                       AND date(date) = %s""", (user_id, d))
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
            "history": history,
            "daily": daily}


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
    cur.execute("UPDATE chat_messages SET is_read = 1 WHERE to_user_id = %s AND is_read = 0",
                (user_id,))
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
    cur.execute("SELECT * FROM notifications WHERE user_id = %s ORDER BY id DESC LIMIT 50",
                (user_id,))
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
    cur.execute("SELECT COUNT(*) as c FROM notifications WHERE user_id = %s AND is_read = 0",
                (user_id,))
    return {"count": cur.fetchone()["c"]}


# ==================== АНАЛИЗ КЛИЕНТА (DOTA) ====================
def _get_cache(steam32, conn):
    cur = conn.cursor()
    cur.execute("SELECT * FROM client_cache WHERE steam32_id = %s", (steam32,))
    return cur.fetchone()


def _save_cache(steam32, nick, rank_tier, lb, wr, total, age_days,
                top_heroes, score, signals, conn):
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
                 json.dumps(top_heroes), score, json.dumps(signals),
                 datetime.datetime.now()))
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
    # 1) кэш
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

    # 2) три параллельных запроса вместо пяти
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
            age_days = (datetime.datetime.now() -
                        datetime.datetime.fromtimestamp(st)).days

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

    _save_cache(steam32, nick, rank_tier, lb, wr, total, age_days,
                [], score, signals, conn)

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


# ==================== АНАЛИЗ КЛИЕНТА (CS2 / FACEIT) ====================
@app.get("/api/client/analyze-cs2/{steam_id}")
async def analyze_cs2(steam_id: str):
    """Анализ CS2 через FACEIT Data API v4."""
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

            stats_r = await c.get(f"{FACEIT_BASE}/players/{pid}/stats/cs2",
                                  headers=headers)
            stats = stats_r.json() if stats_r.status_code == 200 else {}

        games = player.get("games", {}).get("cs2", {})
        life = stats.get("lifetime", {}) if stats else {}
        return {
            "steam_id": steam_id,
            "player_id": pid,
            "nickname": player.get("nickname", "—"),
            "country": player.get("country", "—"),
            "level": games.get("skill_level"),
            "elo": games.get("faceit_elo"),
            "kd": life.get("Average K/D Ratio"),
            "hs": life.get("Average Headshots %"),
            "winrate": life.get("Win Rate %"),
            "matches": life.get("Matches"),
            "longest_win_streak": life.get("Longest Win Streak"),
        }
    except Exception as e:
        return {"error": f"FACEIT недоступен: {e}"}


@app.get("/api/client/trust/{steam_id}")
async def check_trust(steam_id: str):
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(f"https://faceitfinder.com/profile/{steam_id}",
                            follow_redirects=True)
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
            "paid": float(fin["paid"] or 0),
            "counts": counts,
            "c_today": float(periods["today"] or 0),
            "c_week": float(periods["week"] or 0),
            "c_month": float(periods["month"] or 0),
            "boosters": boosters,
            "total_balance": float(total_balance or 0)}


@app.get("/api/admin/users")
def admin_users(_=Depends(require_admin), conn=Depends(get_db)):
    cur = conn.cursor()
    cur.execute("""SELECT id, key, nickname, role, balance, hourly_rate,
                          created_at, last_login FROM users ORDER BY id""")
    return rows_to_json(cur.fetchall())


@app.post("/api/admin/balance")
def admin_balance(req: BalanceAdjust, _=Depends(require_admin), conn=Depends(get_db)):
    cur = conn.cursor()
    cur.execute("UPDATE users SET balance = balance + %s WHERE id = %s",
                (req.delta, req.user_id))
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
    cur.execute("""SELECT u.id, u.nickname, COALESCE(SUM(t.amount), 0) as earned
                   FROM users u LEFT JOIN transactions t
                   ON t.user_id = u.id AND t.type = 'earning'
                   WHERE u.role = 'booster'
                   GROUP BY u.id ORDER BY earned DESC LIMIT 5""")
    return rows_to_json(cur.fetchall())


@app.get("/api/admin/active-boosts")
def admin_active_boosts(_=Depends(require_admin), conn=Depends(get_db)):
    cur = conn.cursor()
    cur.execute("""SELECT b.*, u.nickname AS booster_nickname
                   FROM boosts b JOIN users u ON u.id = b.booster_id
                   WHERE b.status IN ('planned','active')
                   ORDER BY b.id DESC""")
    return rows_to_json(cur.fetchall())

# ==================== ДОП. АДМИН-ЭНДПОИНТЫ ====================

@app.get("/api/boosts/detail/{boost_id}")
def get_boost_detail(boost_id: int, x_user_id: int = Header(...), conn=Depends(get_db)):
    """Детальная инфа по одному заказу + кто работает + история."""
    cur = conn.cursor()
    cur.execute("""SELECT b.*, u.nickname AS booster_nickname, u.id AS booster_user_id
                   FROM boosts b JOIN users u ON u.id = b.booster_id
                   WHERE b.id = %s""", (boost_id,))
    b = cur.fetchone()
    if not b:
        raise HTTPException(404, "Заказ не найден")

    # права: только сам бустер или админ
    cur.execute("SELECT role FROM users WHERE id = %s", (x_user_id,))
    me = cur.fetchone()
    is_admin = bool(me and me["role"] == "admin")
    if not is_admin and b["booster_id"] != x_user_id:
        raise HTTPException(403, "Нет доступа")

    # транзакции по заказу
    cur.execute("""SELECT * FROM transactions WHERE boost_id = %s
                   ORDER BY id DESC""", (boost_id,))
    txs = rows_to_json(cur.fetchall())

    result = dict(b)
    for k, v in result.items():
        if isinstance(v, datetime.datetime):
            result[k] = v.isoformat()
    result["transactions"] = txs
    return result


@app.get("/api/admin/boosters-stats")
def admin_boosters_stats(_=Depends(require_admin), conn=Depends(get_db)):
    """Сводка по каждому бустеру + админ."""
    cur = conn.cursor()
    cur.execute("""SELECT id, nickname, role, balance, hourly_rate, last_login
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
                       FROM transactions WHERE user_id = %s AND type = 'commission'""",
                    (u["id"],))
        comm = float(cur.fetchone()["commission"] or 0)
        out.append({
            "id": u["id"],
            "nickname": u["nickname"],
            "role": u["role"],
            "balance": float(u["balance"] or 0),
            "hourly_rate": u["hourly_rate"],
            "last_login": u["last_login"],
            "planned": s["planned"],
            "active": s["active"],
            "completed": s["completed"],
            "refunded": s["refunded"],
            "earned": earned,
            "avg_check": float(s["avg_check"] or 0),
            "hours": hours,
            "avg_hourly": (earned / hours) if hours else 0,
            "commission": comm,
        })
    return out
