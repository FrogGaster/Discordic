import os
import uvicorn
import datetime
from typing import Dict, List, Optional
from collections import defaultdict
from dotenv import load_dotenv

# FastAPI
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from pydantic import BaseModel

# SQLAlchemy (Database)
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime
from sqlalchemy.orm import sessionmaker, declarative_base

# === 1. ЗАГРУЗКА КОНФИГУРАЦИИ ИЗ .ENV ===
load_dotenv()
SERVER_HOST = os.getenv("SERVER_HOST", "127.0.0.1")
SERVER_PORT = int(os.getenv("SERVER_PORT", 8000))
DATABASE_URL = "sqlite:///./discord.db"

# === 2. НАСТРОЙКА БАЗЫ ДАННЫХ ===
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# Таблица пользователей
class UserDB(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    password = Column(String)  # В продакшене обязательно хешировать!
    status = Column(String, default="Online")
    bio = Column(Text, default="Я использую PyDiscord")

# Таблица сообщений (История чата)
class MessageDB(Base):
    __tablename__ = "messages"
    id = Column(Integer, primary_key=True, index=True)
    channel_id = Column(String, index=True)
    username = Column(String)
    content = Column(Text)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow)

# Создаем таблицы при запуске
Base.metadata.create_all(bind=engine)

# === 3. МОДЕЛИ ДАННЫХ (Pydantic) ===
class UserLogin(BaseModel):
    username: str
    password: str

class ProfileUpdate(BaseModel):
    username: str
    status: str
    bio: str

# === 4. МЕНЕДЖЕР СОЕДИНЕНИЙ WEBSOCKET ===
class ConnectionManager:
    def __init__(self):
        # channel_id -> список активных WebSocket
        self.active_connections: Dict[str, List[WebSocket]] = defaultdict(list)
        # channel_id -> множество имен пользователей в сети
        self.channel_users: Dict[str, set] = defaultdict(set)

    async def connect(self, websocket: WebSocket, channel_id: str, username: str):
        await websocket.accept()
        self.active_connections[channel_id].append(websocket)
        self.channel_users[channel_id].add(username)
        # Рассылаем обновленный список участников
        await self.broadcast_user_list(channel_id)

    async def disconnect(self, websocket: WebSocket, channel_id: str, username: str):
        if channel_id in self.active_connections:
            if websocket in self.active_connections[channel_id]:
                self.active_connections[channel_id].remove(websocket)
        
        self.channel_users[channel_id].discard(username)
        await self.broadcast_user_list(channel_id)

    async def broadcast_user_list(self, channel_id: str):
        """Отправляет всем спец-сообщение со списком участников"""
        users = ",".join(self.channel_users[channel_id])
        await self.broadcast(f"MEMBERS:{users}", channel_id)

    async def broadcast(self, message, channel_id: str, sender: Optional[WebSocket] = None):
        """Универсальная рассылка: текст или байты (звук)"""
        if channel_id not in self.active_connections:
            return

        for connection in list(self.active_connections[channel_id]):
            # Если это звук (байты), не шлем его самому себе (эхо)
            if isinstance(message, bytes) and connection == sender:
                continue
            
            try:
                if isinstance(message, str):
                    await connection.send_text(message)
                else:
                    await connection.send_bytes(message)
            except:
                # Если сокет "протух", удалим его позже при дисконнекте
                pass

manager = ConnectionManager()

# === 5. FastAPI ПРИЛОЖЕНИЕ ===
app = FastAPI(title="PyDiscord Backend")

# Эндпоинт входа/регистрации
@app.post("/login")
async def login(user_data: UserLogin):
    db = SessionLocal()
    user = db.query(UserDB).filter(UserDB.username == user_data.username).first()
    
    if not user:
        # Автоматическая регистрация
        user = UserDB(username=user_data.username, password=user_data.password)
        db.add(user)
        db.commit()
    elif user.password != user_data.password:
        db.close()
        raise HTTPException(status_code=401, detail="Неверный пароль")
    
    db.close()
    return {"status": "ok", "message": "Logged in"}

# Получение истории сообщений
@app.get("/history/{channel_id}")
async def get_history(channel_id: str):
    db = SessionLocal()
    # Берем последние 50 сообщений
    msgs = db.query(MessageDB).filter(MessageDB.channel_id == channel_id)\
             .order_by(MessageDB.id.desc()).limit(50).all()
    db.close()
    # Возвращаем в правильном порядке (от старых к новым)
    return [{"username": m.username, "content": m.content} for m in reversed(msgs)]

# Получение профиля
@app.get("/profile/{username}")
async def get_profile(username: str):
    db = SessionLocal()
    user = db.query(UserDB).filter(UserDB.username == username).first()
    db.close()
    if not user:
        return {"status": "Offline", "bio": "Пользователь не найден"}
    return {"status": user.status, "bio": user.bio}

# Обновление профиля
@app.post("/profile/update")
async def update_profile(profile: ProfileUpdate):
    db = SessionLocal()
    user = db.query(UserDB).filter(UserDB.username == profile.username).first()
    if user:
        user.status = profile.status
        user.bio = profile.bio
        db.commit()
    db.close()
    return {"status": "success"}

# === 6. WEBSOCKET ТОЧКА ВХОДА ===
@app.websocket("/ws/{channel_id}/{username}")
async def websocket_endpoint(websocket: WebSocket, channel_id: str, username: str):
    await manager.connect(websocket, channel_id, username)
    
    try:
        while True:
            # Получаем пакет (может быть текст или байты звука)
            data = await websocket.receive()
            
            if "text" in data:
                content = data["text"]
                
                # 1. Сохраняем текстовое сообщение в БД
                db = SessionLocal()
                new_msg = MessageDB(channel_id=channel_id, username=username, content=content)
                db.add(new_msg)
                db.commit()
                db.close()
                
                # 2. Рассылаем всем в канале (включая автора, чтобы он увидел сообщение)
                await manager.broadcast(f"{username}: {content}", channel_id)
            
            elif "bytes" in data:
                # Голосовые данные рассылаем всем, кроме автора
                await manager.broadcast(data["bytes"], channel_id, sender=websocket)

    except WebSocketDisconnect:
        await manager.disconnect(websocket, channel_id, username)
        await manager.broadcast(f"System: {username} покинул чат.", channel_id)
    except Exception as e:
        print(f"WS Error: {e}")
        await manager.disconnect(websocket, channel_id, username)

# === 7. ЗАПУСК ===
if __name__ == "__main__":
    print(f"🚀 Сервер запускается на http://{SERVER_HOST}:{SERVER_PORT}")
    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT)