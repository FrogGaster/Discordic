import os
import asyncio
import threading
import requests
import pyaudio
import websockets
import customtkinter as ctk
from dotenv import load_dotenv

# Исправление для Python 3.13 (audioop удален)
try:
    import audioop
except ImportError:
    try:
        import audioop_lts as audioop
    except ImportError:
        audioop = None

# === КОНСТАНТЫ ===
load_dotenv()
SERVER_HOST = os.getenv("SERVER_HOST", "127.0.0.1")
SERVER_PORT = int(os.getenv("SERVER_PORT", 8000))
WS_BASE_URL = f"ws://{SERVER_HOST}:{SERVER_PORT}/ws"
API_URL = f"http://{SERVER_HOST}:{SERVER_PORT}"

CHUNK, RATE = 1024, 44100
FORMAT, CHANNELS = pyaudio.paInt16, 1

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

class AudioHandler:
    def __init__(self):
        self.p = pyaudio.PyAudio()
        self.in_s = self.out_s = None
        self.active = False
        self.volume = 1.0
        self.muted = False

    def start(self, send_cb):
        self.stop()
        self.active = True
        try:
            self.out_s = self.p.open(format=FORMAT, channels=CHANNELS, rate=RATE, output=True)
            self.in_s = self.p.open(format=FORMAT, channels=CHANNELS, rate=RATE, input=True, frames_per_buffer=CHUNK)
            def record_loop():
                while self.active:
                    try:
                        data = self.in_s.read(CHUNK, exception_on_overflow=False)
                        if not self.muted: send_cb(data)
                    except: break
            threading.Thread(target=record_loop, daemon=True).start()
        except: pass

    def play(self, data):
        if self.out_s and self.active:
            if self.volume != 1.0 and audioop:
                data = audioop.mul(data, 2, self.volume)
            try: self.out_s.write(data)
            except: pass

    def stop(self):
        self.active = False
        if self.in_s: self.in_s.stop_stream(); self.in_s.close(); self.in_s = None
        if self.out_s: self.out_s.stop_stream(); self.out_s.close(); self.out_s = None

class SettingsWindow(ctk.CTkToplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.title("Настройки")
        self.geometry("450x400")
        self.app = parent
        self.attributes("-topmost", True)

        self.tabview = ctk.CTkTabview(self)
        self.tabview.pack(fill="both", expand=True, padx=10, pady=10)
        self.tabview.add("Профиль")
        self.tabview.add("Голос")

        # Профиль
        ctk.CTkLabel(self.tabview.tab("Профиль"), text="Статус:").pack(pady=(10,0))
        self.st_e = ctk.CTkEntry(self.tabview.tab("Профиль"), width=300)
        self.st_e.pack(pady=5)
        ctk.CTkLabel(self.tabview.tab("Профиль"), text="О себе:").pack(pady=(10,0))
        self.bio_e = ctk.CTkEntry(self.tabview.tab("Профиль"), width=300)
        self.bio_e.pack(pady=5)
        ctk.CTkButton(self.tabview.tab("Профиль"), text="Сохранить", command=self.save_p).pack(pady=20)

        # Голос
        ctk.CTkLabel(self.tabview.tab("Голос"), text="Громкость динамиков:").pack(pady=(10,0))
        self.vol_slider = ctk.CTkSlider(self.tabview.tab("Голос"), from_=0, to=2, command=self.set_v)
        self.vol_slider.set(self.app.audio.volume)
        self.vol_slider.pack(pady=5)
        self.mute_sw = ctk.CTkSwitch(self.tabview.tab("Голос"), text="Выключить микрофон", command=self.set_m)
        if self.app.audio.muted: self.mute_sw.select()
        self.mute_sw.pack(pady=20)
        self.load_p()

    def set_v(self, v): self.app.audio.volume = float(v)
    def set_m(self): self.app.audio.muted = self.mute_sw.get() == 1
    def load_p(self):
        try:
            r = requests.get(f"{API_URL}/profile/{self.app.username}").json()
            self.st_e.insert(0, r.get("status", ""))
            self.bio_e.insert(0, r.get("bio", ""))
        except: pass
    def save_p(self):
        p = {"username": self.app.username, "status": self.st_e.get(), "bio": self.bio_e.get()}
        requests.post(f"{API_URL}/profile/update", json=p)
        self.destroy()

class DiscordApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("PyDiscord")
        self.geometry("1100x750")

        self.username = ""
        self.current_txt_ch = "general"
        self.current_voice_ch = None
        self.txt_ws = None
        self.voice_ws = None
        self.loop = asyncio.new_event_loop()
        self.audio = AudioHandler()

        # ЭКРАН ЛОГИНА
        self.login_f = ctk.CTkFrame(self)
        self.login_f.place(relx=0.5, rely=0.5, anchor="center")
        ctk.CTkLabel(self.login_f, text="PyDiscord Login", font=("Arial", 20, "bold")).pack(pady=20, padx=30)
        self.u_e = ctk.CTkEntry(self.login_f, placeholder_text="Никнейм", width=250)
        self.u_e.pack(pady=5)
        self.p_e = ctk.CTkEntry(self.login_f, placeholder_text="Пароль", show="*", width=250)
        self.p_e.pack(pady=5)
        ctk.CTkButton(self.login_f, text="Войти", command=self.login_process).pack(pady=20)

        # ГЛАВНЫЙ ИНТЕРФЕЙС
        self.main_f = ctk.CTkFrame(self)
        self.sidebar = ctk.CTkFrame(self.main_f, width=220, corner_radius=0)
        self.sidebar.pack(side="left", fill="y")
        
        ctk.CTkLabel(self.sidebar, text="ТЕКСТОВЫЕ КАНАЛЫ", text_color="gray", font=("Arial", 10, "bold")).pack(pady=(15, 5))
        for n, cid in [("# болталка", "general"), ("# оффтоп", "offtopic")]:
            ctk.CTkButton(self.sidebar, text=n, anchor="w", fg_color="transparent", command=lambda c=cid: self.switch_txt(c)).pack(fill="x", padx=10)

        ctk.CTkLabel(self.sidebar, text="ГОЛОСОВЫЕ КАНАЛЫ", text_color="gray", font=("Arial", 10, "bold")).pack(pady=(20, 5))
        for n, cid in [("🔊 Голос 1", "voice1"), ("🔊 Голос 2", "voice2")]:
            ctk.CTkButton(self.sidebar, text=n, anchor="w", fg_color="#333", command=lambda c=cid: self.join_voice(c)).pack(fill="x", padx=10, pady=2)

        self.user_p = ctk.CTkFrame(self.sidebar, fg_color="#222")
        self.user_p.pack(side="bottom", fill="x", padx=5, pady=5)
        self.v_lbl = ctk.CTkLabel(self.user_p, text="Голос: нет", text_color="gray", font=("Arial", 11))
        self.v_lbl.pack(pady=2)
        btns_f = ctk.CTkFrame(self.user_p, fg_color="transparent")
        btns_f.pack(fill="x")
        ctk.CTkButton(btns_f, text="⚙", width=35, command=lambda: SettingsWindow(self)).pack(side="left", padx=5, pady=5)
        self.v_disc_btn = ctk.CTkButton(btns_f, text="Выйти", fg_color="#633", width=80, height=25, command=self.leave_voice)

        # ЧАТ
        self.chat_area = ctk.CTkFrame(self.main_f, fg_color="#1e1e1e", corner_radius=0)
        self.chat_area.pack(side="right", fill="both", expand=True)
        self.header = ctk.CTkLabel(self.chat_area, text="# general", font=("Arial", 16, "bold"))
        self.header.pack(pady=10)

        # Панель участников (создаем, но не пакуем)
        self.m_frame = ctk.CTkFrame(self.chat_area, fg_color="#252525", height=45)
        self.m_list_f = ctk.CTkFrame(self.m_frame, fg_color="transparent")
        self.m_list_f.pack(pady=5)

        self.scroll = ctk.CTkScrollableFrame(self.chat_area, fg_color="transparent")
        self.scroll.pack(fill="both", expand=True, padx=10)

        self.in_f = ctk.CTkFrame(self.chat_area, fg_color="transparent")
        self.in_f.pack(fill="x", padx=10, pady=10)
        self.msg_e = ctk.CTkEntry(self.in_f, placeholder_text="Написать сообщение...")
        self.msg_e.pack(side="left", fill="x", expand=True)
        self.msg_e.bind("<Return>", lambda e: self.send_txt())
        ctk.CTkButton(self.in_f, text="Send", width=60, command=self.send_txt).pack(side="right", padx=5)

    def login_process(self):
        self.username, pwd = self.u_e.get(), self.p_e.get()
        if not self.username or not pwd: return
        try:
            res = requests.post(f"{API_URL}/login", json={"username":self.username, "password":pwd})
            if res.status_code == 200:
                self.login_f.destroy(); self.main_f.pack(fill="both", expand=True)
                threading.Thread(target=self.start_async, daemon=True).start()
        except: pass

    def start_async(self):
        asyncio.set_event_loop(self.loop)
        self.loop.create_task(self.net_text_loop())
        self.loop.create_task(self.net_voice_loop())
        self.loop.run_forever()

    async def net_text_loop(self):
        while True:
            self.load_history(self.current_txt_ch)
            try:
                async with websockets.connect(f"{WS_BASE_URL}/{self.current_txt_ch}/{self.username}") as ws:
                    self.txt_ws = ws
                    while True:
                        msg = await ws.recv()
                        if not msg.startswith("MEMBERS:"): self.add_ui(msg)
            except: self.txt_ws = None; await asyncio.sleep(1)

    async def net_voice_loop(self):
        while True:
            if not self.current_voice_ch: await asyncio.sleep(0.2); continue
            try:
                async with websockets.connect(f"{WS_BASE_URL}/{self.current_voice_ch}/{self.username}") as ws:
                    self.voice_ws = ws
                    self.v_lbl.configure(text=f"Голос: {self.current_voice_ch}", text_color="green")
                    self.v_disc_btn.pack(side="right", padx=5)
                    self.audio.start(lambda d: self.loop.call_soon_threadsafe(lambda: asyncio.create_task(ws.send(d))))
                    while self.current_voice_ch:
                        msg = await ws.recv()
                        if isinstance(msg, bytes): self.audio.play(msg)
                        elif msg.startswith("MEMBERS:"): self.upd_members(msg[8:].split(","))
            except: pass
            finally:
                self.voice_ws = None; self.audio.stop()
                self.v_lbl.configure(text="Голос: нет", text_color="gray")
                self.v_disc_btn.pack_forget()
                self.upd_members([]); await asyncio.sleep(1)

    def load_history(self, cid):
        for w in self.scroll.winfo_children(): w.destroy()
        try:
            h = requests.get(f"{API_URL}/history/{cid}").json()
            for m in h: self.add_ui(f"{m['username']}: {m['content']}")
        except: pass

    def switch_txt(self, cid):
        self.current_txt_ch = cid
        self.header.configure(text=f"# {cid}")
        if self.txt_ws: asyncio.run_coroutine_threadsafe(self.txt_ws.close(), self.loop)

    def join_voice(self, cid):
        """Исправленный метод: переупаковка для соблюдения порядка"""
        if self.current_voice_ch == cid: return
        self.current_voice_ch = cid
        
        # Переупаковываем элементы чата, чтобы вставить панель участников сверху
        self.scroll.pack_forget()
        self.in_f.pack_forget()
        self.m_frame.pack(fill="x", padx=10, pady=5)
        self.scroll.pack(fill="both", expand=True, padx=10)
        self.in_f.pack(fill="x", padx=10, pady=10)
        
        if self.voice_ws: asyncio.run_coroutine_threadsafe(self.voice_ws.close(), self.loop)

    def leave_voice(self):
        self.current_voice_ch = None
        self.m_frame.pack_forget()
        if self.voice_ws: asyncio.run_coroutine_threadsafe(self.voice_ws.close(), self.loop)

    def upd_members(self, users):
        self.m_list_f.after(0, lambda: self._render_members(users))

    def _render_members(self, users):
        for w in self.m_list_f.winfo_children(): w.destroy()
        for u in users:
            if u: ctk.CTkLabel(self.m_list_f, text=f"🎙 {u}", fg_color="#333", corner_radius=6, padx=10).pack(side="left", padx=3)

    def send_txt(self):
        t = self.msg_e.get()
        if t and self.txt_ws:
            asyncio.run_coroutine_threadsafe(self.txt_ws.send(t), self.loop)
            self.msg_e.delete(0, 'end')

    def add_ui(self, m):
        self.scroll.after(0, lambda: ctk.CTkLabel(self.scroll, text=m, wraplength=800, justify="left", anchor="w").pack(fill="x"))

if __name__ == "__main__":
    DiscordApp().mainloop()