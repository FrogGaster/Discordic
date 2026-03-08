import os
import uvicorn
import datetime
from typing import Dict, List, Optional, Set
from collections import defaultdict
from dotenv import load_dotenv

# FastAPI & Pydantic
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends
from pydantic import BaseModel

# SQLAlchemy (База данных)
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Boolean
from sqlalchemy.orm import sessionmaker, declarative_base, Session

# Безопасность
import bcrypt

# === 1. ЗАГРУЗКА КОНФИГУРАЦИИ ===
load_dotenv()

SERVER_HOST = os.getenv("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.getenv("SERVER_PORT", 8000))
DATABASE_URL = "sqlite:///./discord.db"

# Данные СУПЕР-АДМИНА
ADMIN_USER = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASSWORD", "admin123")

# === 2. БАЗА ДАННЫХ ===
# check_same_thread=False нужен для SQLite при работе с FastAPI
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class UserDB(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    password = Column(String)
    status = Column(String, default="Online")
    bio = Column(Text, default="Я использую PyDiscord")
    is_banned = Column(Boolean, default=False)
    is_muted = Column(Boolean, default=False)

class MessageDB(Base):
    __tablename__ = "messages"
    id = Column(Integer, primary_key=True, index=True)
    channel_id = Column(String, index=True)
    username = Column(String)
    content = Column(Text)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow)

Base.metadata.create_all(bind=engine)

# Dependency для получения сессии БД (Best Practice)
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# === 3. УТИЛИТЫ ===
def get_password_hash(password: str) -> str:
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return bcrypt.checkpw(plain_password.encode('utf-8'), hashed_password.encode('utf-8'))

# === 4. DTO МОДЕЛИ ===
class UserAuth(BaseModel):
    username: str
    password: str

class AdminAction(BaseModel):
    target_username: str
    admin_password: str

class ProfileUpdate(BaseModel):
    username: str
    status: str
    bio: str

# === 5. МЕНЕДЖЕР СОЕДИНЕНИЙ (С КЕШИРОВАНИЕМ) ===
class ConnectionManager:
    def __init__(self):
        # channel_id -> список сокетов
        self.active_connections: Dict[str, List[WebSocket]] = defaultdict(list)
        # channel_id -> список ников
        self.channel_users: Dict[str, set] = defaultdict(set)
        # username -> список сокетов (для кика)
        self.user_sockets: Dict[str, List[WebSocket]] = defaultdict(list)
        
        # КЕШ ЗАМУЧЕННЫХ ПОЛЬЗОВАТЕЛЕЙ (Optimization)
        # Храним множество ников тех, кто в муте, чтобы не лезть в БД при каждом пакете
        self.muted_cache: Set[str] = set()

    async def connect(self, websocket: WebSocket, channel_id: str, username: str, is_muted: bool):
        await websocket.accept()
        self.active_connections[channel_id].append(websocket)
        self.channel_users[channel_id].add(username)
        self.user_sockets[username].append(websocket)
        
        # Обновляем кеш мута при входе
        if is_muted:
            self.muted_cache.add(username)
        elif username in self.muted_cache:
            self.muted_cache.discard(username)
            
        await self.broadcast_user_list(channel_id)

    async def disconnect(self, websocket: WebSocket, channel_id: str, username: str):
        if channel_id in self.active_connections and websocket in self.active_connections[channel_id]:
            self.active_connections[channel_id].remove(websocket)
        
        self.channel_users[channel_id].discard(username)
        
        if websocket in self.user_sockets[username]:
            self.user_sockets[username].remove(websocket)
            
        await self.broadcast_user_list(channel_id)

    async def kick_user(self, username: str):
        """Моментально закрывает сокеты пользователя"""
        if username in self.user_sockets:
            sockets = list(self.user_sockets[username]) # Копия списка
            for ws in sockets:
                try:
                    await ws.close(code=1008, reason="Banned")
                except: pass
            self.user_sockets[username] = []

    def update_mute_status(self, username: str, is_muted: bool):
        """Обновляет кеш мута без перезагрузки"""
        if is_muted:
            self.muted_cache.add(username)
        else:
            self.muted_cache.discard(username)

    async def broadcast_user_list(self, channel_id: str):
        users = ",".join(self.channel_users[channel_id])
        await self.broadcast(f"MEMBERS:{users}", channel_id)

    async def broadcast(self, message, channel_id: str, sender: Optional[WebSocket] = None):
        if channel_id not in self.active_connections: return
        
        # Копируем список, чтобы избежать ошибки изменения во время итерации
        # Но делаем это только если нужно (тут оптимизация python list)
        connections = self.active_connections[channel_id]
        
        for connection in connections:
            if isinstance(message, bytes) and connection == sender:
                continue
            try:
                if isinstance(message, str): await connection.send_text(message)
                else: await connection.send_bytes(message)
            except:
                # Если сокет мертв, он удалится через disconnect
                pass

manager = ConnectionManager()

# === 6. FASTAPI ENDPOINTS ===
app = FastAPI(title="PyDiscord Optimized")

@app.post("/register")
async def register(user_data: UserAuth, db: Session = Depends(get_db)):
    if user_data.username == ADMIN_USER:
        raise HTTPException(status_code=400, detail="Никнейм зарезервирован")
    
    if db.query(UserDB).filter(UserDB.username == user_data.username).first():
        raise HTTPException(status_code=400, detail="Никнейм занят")
    
    new_user = UserDB(username=user_data.username, password=get_password_hash(user_data.password))
    db.add(new_user)
    db.commit()
    return {"status": "ok"}

@app.post("/login")
async def login(user_data: UserAuth, db: Session = Depends(get_db)):
    # Админ
    if user_data.username == ADMIN_USER and user_data.password == ADMIN_PASS:
        return {"status": "ok", "is_admin": True}

    # Юзер
    user = db.query(UserDB).filter(UserDB.username == user_data.username).first()
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    
    if user.is_banned:
        raise HTTPException(status_code=403, detail="ВЫ ЗАБАНЕНЫ")

    if not verify_password(user_data.password, user.password):
        raise HTTPException(status_code=401, detail="Неверный пароль")
    
    return {"status": "ok", "is_admin": False}

# --- АДМИНКА ---

@app.post("/admin/ban")
async def admin_ban(action: AdminAction, db: Session = Depends(get_db)):
    if action.admin_password != ADMIN_PASS:
        raise HTTPException(status_code=403, detail="Wrong password")
    
    user = db.query(UserDB).filter(UserDB.username == action.target_username).first()
    if not user: return {"status": "error", "message": "Not found"}
    
    user.is_banned = not user.is_banned
    db.commit()
    
    status = "BANNED" if user.is_banned else "UNBANNED"
    
    # Моментальное применение
    if user.is_banned:
        await manager.kick_user(user.username)
        
    return {"status": "ok", "new_state": status}

@app.post("/admin/mute")
async def admin_mute(action: AdminAction, db: Session = Depends(get_db)):
    if action.admin_password != ADMIN_PASS:
        raise HTTPException(status_code=403, detail="Wrong password")
    
    user = db.query(UserDB).filter(UserDB.username == action.target_username).first()
    if not user: return {"status": "error", "message": "Not found"}
    
    user.is_muted = not user.is_muted
    db.commit()
    
    # Обновляем кеш менеджера, чтобы применить мут без перезахода юзера
    manager.update_mute_status(user.username, user.is_muted)
    
    return {"status": "ok", "new_state": "MUTED" if user.is_muted else "UNMUTED"}

# --- ИНФО ---

@app.get("/history/{channel_id}")
async def get_history(channel_id: str, db: Session = Depends(get_db)):
    msgs = db.query(MessageDB).filter(MessageDB.channel_id == channel_id)\
             .order_by(MessageDB.id.desc()).limit(50).all()
    return [{"username": m.username, "content": m.content} for m in reversed(msgs)]

@app.get("/profile/{username}")
async def get_profile(username: str, db: Session = Depends(get_db)):
    if username == ADMIN_USER: return {"status": "ADMIN", "bio": "System"}
    
    user = db.query(UserDB).filter(UserDB.username == username).first()
    if not user: return {"status": "Offline", "bio": "Not found"}
    
    p = ""
    if user.is_banned: p = "[BANNED] "
    elif user.is_muted: p = "[MUTED] "
    return {"status": p + user.status, "bio": user.bio}

@app.post("/profile/update")
async def update_profile(profile: ProfileUpdate, db: Session = Depends(get_db)):
    user = db.query(UserDB).filter(UserDB.username == profile.username).first()
    if user:
        user.status = profile.status
        user.bio = profile.bio
        db.commit()
    return {"status": "success"}

# --- WEBSOCKET (Optimized) ---

@app.websocket("/ws/{channel_id}/{username}")
async def websocket_endpoint(websocket: WebSocket, channel_id: str, username: str):
    # Предварительная проверка (однократная при входе)
    is_muted_init = False
    
    if username != ADMIN_USER:
        # Используем отдельную сессию для handshake, так как Depends тут не работает напрямую
        db = SessionLocal()
        user = db.query(UserDB).filter(UserDB.username == username).first()
        if user:
            if user.is_banned:
                db.close()
                await websocket.close(code=1008)
                return
            is_muted_init = user.is_muted
        db.close()

    # Подключаем и заносим в кеш
    await manager.connect(websocket, channel_id, username, is_muted_init)
    
    try:
        while True:
            data = await websocket.receive()
            
            # 1. ТЕКСТОВЫЕ СООБЩЕНИЯ (Сохраняем в БД)
            if "text" in data:
                content = data["text"]
                
                # Создаем сессию только для записи сообщения (редкая операция)
                db = SessionLocal()
                msg = MessageDB(channel_id=channel_id, username=username, content=content)
                db.add(msg)
                db.commit()
                db.close()
                
                await manager.broadcast(f"{username}: {content}", channel_id)
            
            # 2. ГОЛОСОВЫЕ ДАННЫЕ (БЕЗ БД - только RAM кеш)
            elif "bytes" in data:
                # Проверка мута через кеш (Быстро!)
                if username in manager.muted_cache:
                    continue
                
                # Упаковка: [Len][Name][Audio]
                u_bytes = username.encode('utf-8')
                packet = bytes([len(u_bytes)]) + u_bytes + data["bytes"]
                
                await manager.broadcast(packet, channel_id, sender=websocket)

    except WebSocketDisconnect:
        await manager.disconnect(websocket, channel_id, username)
    except Exception:
        await manager.disconnect(websocket, channel_id, username)

if __name__ == "__main__":
    print(f"🚀 Optimized Server running. Admin: {ADMIN_USER}")
    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT)