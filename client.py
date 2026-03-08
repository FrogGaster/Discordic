import os
import asyncio
import threading
import requests
import pyaudio
import websockets
import customtkinter as ctk
from dotenv import load_dotenv

# Импортируем исключения для обработки Бана (403) и Кика (1008)
from websockets.exceptions import InvalidStatusCode, ConnectionClosed

# === 1. АУДИО МОДУЛЬ ===
try:
    import audioop
except ImportError:
    try:
        import audioop_lts as audioop
    except ImportError:
        audioop = None
        print("Warning: audioop not found. Volume control disabled.")

# === 2. КОНФИГУРАЦИЯ ===
load_dotenv()

SERVER_IP = "127.0.0.1"
SERVER_PORT = "8000"
API_URL = f"http://{SERVER_IP}:{SERVER_PORT}"
WS_BASE_URL = f"ws://{SERVER_IP}:{SERVER_PORT}/ws"

# Настройки PyAudio
CHUNK = 1024
RATE = 44100
FORMAT = pyaudio.paInt16
CHANNELS = 1

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

# === 3. КЛАСС РАБОТЫ СО ЗВУКОМ ===
class AudioHandler:
    def __init__(self):
        self.p = pyaudio.PyAudio()
        self.in_s = self.out_s = None
        self.active = False
        self.global_vol = 1.0  # Общая громкость
        self.muted = False

    def start(self, callback):
        self.stop()
        self.active = True
        try:
            self.out_s = self.p.open(format=FORMAT, channels=CHANNELS, rate=RATE, output=True)
            self.in_s = self.p.open(format=FORMAT, channels=CHANNELS, rate=RATE, input=True, frames_per_buffer=CHUNK)
            
            def record_loop():
                while self.active:
                    try:
                        data = self.in_s.read(CHUNK, exception_on_overflow=False)
                        if not self.muted:
                            callback(data)
                    except: break
            threading.Thread(target=record_loop, daemon=True).start()
        except Exception as e:
            print(f"Audio Start Error: {e}")

    def play(self, data, vol=1.0):
        if self.out_s and self.active:
            final_vol = self.global_vol * vol
            
            if final_vol != 1.0 and audioop:
                try: data = audioop.mul(data, 2, final_vol)
                except: pass
            
            try: self.out_s.write(data)
            except: pass

    def stop(self):
        self.active = False
        if self.in_s: self.in_s.stop_stream(); self.in_s.close(); self.in_s = None
        if self.out_s: self.out_s.stop_stream(); self.out_s.close(); self.out_s = None

# === 4. ОКНО АДМИНА ===
class AdminWindow(ctk.CTkToplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.title("ADMIN PANEL")
        self.geometry("300x350")
        self.app = parent
        self.attributes("-topmost", True)
        
        ctk.CTkLabel(self, text="УПРАВЛЕНИЕ", font=("Arial", 16, "bold"), text_color="red").pack(pady=10)
        
        self.target = ctk.CTkEntry(self, placeholder_text="Nick to Ban/Mute")
        self.target.pack(pady=5, padx=20, fill="x")
        
        self.info = ctk.CTkLabel(self, text="")
        self.info.pack(pady=5)

        ctk.CTkButton(self, text="BAN / UNBAN", fg_color="#800", hover_color="#A00", 
                      command=lambda: self.act("ban")).pack(pady=10, padx=20, fill="x")
        
        ctk.CTkButton(self, text="MUTE / UNMUTE", fg_color="#880", hover_color="#AA0", text_color="black",
                      command=lambda: self.act("mute")).pack(pady=5, padx=20, fill="x")

    def act(self, action):
        t = self.target.get().strip()
        if not t: return self.info.configure(text="Введите ник!", text_color="yellow")
        
        try:
            r = requests.post(f"{API_URL}/admin/{action}", json={
                "target_username": t, "admin_password": self.app.password
            })
            if r.status_code == 200:
                self.info.configure(text=f"Success: {r.json().get('new_state')}", text_color="green")
            else:
                self.info.configure(text=f"Error: {r.json().get('detail')}", text_color="red")
        except:
            self.info.configure(text="Network Error", text_color="red")

# === 5. ОКНО НАСТРОЕК ===
class SettingsWindow(ctk.CTkToplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.title("Settings")
        self.geometry("350x400")
        self.app = parent
        self.attributes("-topmost", True)

        self.tab = ctk.CTkTabview(self)
        self.tab.pack(fill="both", expand=True, padx=10, pady=10)
        self.tab.add("Profile"); self.tab.add("Audio")

        # Profile
        ctk.CTkLabel(self.tab.tab("Profile"), text="Status").pack()
        self.st = ctk.CTkEntry(self.tab.tab("Profile")); self.st.pack(pady=5)
        ctk.CTkLabel(self.tab.tab("Profile"), text="Bio").pack()
        self.bio = ctk.CTkEntry(self.tab.tab("Profile")); self.bio.pack(pady=5)
        ctk.CTkButton(self.tab.tab("Profile"), text="Save", command=self.save).pack(pady=10)

        # Audio
        ctk.CTkLabel(self.tab.tab("Audio"), text="Global Volume").pack()
        self.sl = ctk.CTkSlider(self.tab.tab("Audio"), from_=0, to=2, command=self.set_vol)
        self.sl.set(self.app.audio.global_vol); self.sl.pack(pady=5)
        self.sw = ctk.CTkSwitch(self.tab.tab("Audio"), text="Mute Mic", command=self.set_mute)
        if self.app.audio.muted: self.sw.select()
        self.sw.pack(pady=20)
        
        self.load()

    def set_vol(self, v): self.app.audio.global_vol = float(v)
    def set_mute(self): self.app.audio.muted = (self.sw.get() == 1)
    
    def load(self):
        try:
            r = requests.get(f"{API_URL}/profile/{self.app.username}").json()
            self.st.insert(0, r.get("status","")); self.bio.insert(0, r.get("bio",""))
        except: pass

    def save(self):
        try: requests.post(f"{API_URL}/profile/update", json={"username": self.app.username, "status": self.st.get(), "bio": self.bio.get()}); self.destroy()
        except: pass

# === 6. ГЛАВНОЕ ПРИЛОЖЕНИЕ ===
class DiscordApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("PyDiscord v2")
        self.geometry("1100x700")

        # Данные
        self.username = ""
        self.password = ""
        self.is_admin = False
        
        # Состояние
        self.curr_txt = "general"
        self.curr_voice = None
        self.txt_ws = None
        self.voice_ws = None
        self.user_vols = {}

        self.loop = asyncio.new_event_loop()
        self.audio = AudioHandler()

        # --- ЭКРАН ВХОДА ---
        self.log_fr = ctk.CTkFrame(self)
        self.log_fr.place(relx=0.5, rely=0.5, anchor="center")
        
        ctk.CTkLabel(self.log_fr, text="PyDiscord Login", font=("Arial", 20, "bold")).pack(pady=20, padx=40)
        self.err = ctk.CTkLabel(self.log_fr, text="", text_color="red")
        self.err.pack()
        
        self.u_e = ctk.CTkEntry(self.log_fr, placeholder_text="Username"); self.u_e.pack(pady=5)
        self.p_e = ctk.CTkEntry(self.log_fr, placeholder_text="Password", show="*"); self.p_e.pack(pady=5)
        
        bf = ctk.CTkFrame(self.log_fr, fg_color="transparent"); bf.pack(pady=20)
        ctk.CTkButton(bf, text="Login", width=90, command=self.login).pack(side="left", padx=5)
        ctk.CTkButton(bf, text="Register", width=90, fg_color="#444", command=self.reg).pack(side="right", padx=5)

        # --- ОСНОВНОЙ ЭКРАН (Инит, но не показ) ---
        self.main_fr = ctk.CTkFrame(self)
        
        # Сайдбар
        self.sidebar = ctk.CTkFrame(self.main_fr, width=220, corner_radius=0)
        self.sidebar.pack(side="left", fill="y")
        
        # Текстовые каналы
        ctk.CTkLabel(self.sidebar, text="TEXT", text_color="gray", font=("Arial", 10, "bold")).pack(pady=(15,5))
        for c in ["general", "dev", "offtopic"]:
            ctk.CTkButton(self.sidebar, text=f"# {c}", fg_color="transparent", anchor="w", command=lambda x=c: self.sw_txt(x)).pack(fill="x", padx=10)
        
        # Голосовые каналы
        ctk.CTkLabel(self.sidebar, text="VOICE", text_color="gray", font=("Arial", 10, "bold")).pack(pady=(20,5))
        for c in ["voice1", "voice2"]:
            ctk.CTkButton(self.sidebar, text=f"🔊 {c}", fg_color="#333", anchor="w", command=lambda x=c: self.join_v(x)).pack(fill="x", padx=10, pady=2)
            
        # Кнопка Админа (скрыта)
        self.admin_btn = ctk.CTkButton(self.sidebar, text="⚠️ ADMIN PANEL", fg_color="#900", hover_color="#B00", command=lambda: AdminWindow(self))
        
        # Панель юзера
        self.u_pan = ctk.CTkFrame(self.sidebar, fg_color="#222")
        self.u_pan.pack(side="bottom", fill="x", padx=5, pady=5)
        self.v_lbl = ctk.CTkLabel(self.u_pan, text="Voice: Off", text_color="gray")
        self.v_lbl.pack()
        bpf = ctk.CTkFrame(self.u_pan, fg_color="transparent"); bpf.pack(fill="x")
        ctk.CTkButton(bpf, text="⚙", width=30, command=lambda: SettingsWindow(self)).pack(side="left", padx=5, pady=5)
        self.lv_btn = ctk.CTkButton(bpf, text="Exit", width=60, fg_color="#833", command=self.leave_v)

        # Чат зона
        self.chat = ctk.CTkFrame(self.main_fr, fg_color="#1e1e1e"); self.chat.pack(side="right", fill="both", expand=True)
        self.head = ctk.CTkLabel(self.chat, text="# general", font=("Arial", 16, "bold")); self.head.pack(pady=10)
        
        # Список участников голоса
        self.v_lst_fr = ctk.CTkFrame(self.chat, height=0, fg_color="#2b2b2b")
        self.v_lst = ctk.CTkFrame(self.v_lst_fr, fg_color="transparent"); self.v_lst.pack(pady=5)
        
        # Скролл чата
        self.scr = ctk.CTkScrollableFrame(self.chat, fg_color="transparent"); self.scr.pack(fill="both", expand=True, padx=10)
        
        # Ввод
        inp = ctk.CTkFrame(self.chat, fg_color="transparent"); inp.pack(fill="x", padx=10, pady=10)
        self.msg_e = ctk.CTkEntry(inp, placeholder_text="Message..."); self.msg_e.pack(side="left", fill="x", expand=True)
        self.msg_e.bind("<Return>", lambda e: self.send()); ctk.CTkButton(inp, text=">", width=40, command=self.send).pack(side="right", padx=5)

    # === ЛОГИКА ===
    def login(self):
        u, p = self.u_e.get(), self.p_e.get()
        try:
            r = requests.post(f"{API_URL}/login", json={"username":u, "password":p})
            d = r.json()
            if r.status_code == 200:
                self.username = u; self.password = p; self.is_admin = d.get("is_admin", False)
                self.log_fr.destroy(); self.main_fr.pack(fill="both", expand=True)
                if self.is_admin: self.admin_btn.pack(side="bottom", fill="x", padx=10, pady=10)
                threading.Thread(target=self.run_async, daemon=True).start()
            else: self.err.configure(text=d.get("detail"))
        except: self.err.configure(text="Server Offline")

    def reg(self):
        try:
            r = requests.post(f"{API_URL}/register", json={"username":self.u_e.get(), "password":self.p_e.get()})
            if r.status_code == 200: self.err.configure(text="Registered! Log in.", text_color="green")
            else: self.err.configure(text=r.json().get("detail"))
        except: self.err.configure(text="Server Offline")

    # === СЕТЬ ===
    def run_async(self):
        asyncio.set_event_loop(self.loop)
        self.loop.create_task(self.txt_loop())
        self.loop.create_task(self.voice_loop())
        self.loop.run_forever()

    async def txt_loop(self):
        while True:
            self.load_hist(self.curr_txt)
            try:
                uri = f"{WS_BASE_URL}/{self.curr_txt}/{self.username}"
                async with websockets.connect(uri) as ws:
                    self.txt_ws = ws
                    while True:
                        msg = await ws.recv()
                        if not msg.startswith("MEMBERS:"): self.add_msg(msg)
            
            # Если 403 (БАН) или 1008 (КИК) - пишем и реконнектимся реже
            except InvalidStatusCode as e:
                if e.status_code == 403: self.add_msg("🔴 SYSTEM: YOU ARE BANNED.")
                self.txt_ws = None; await asyncio.sleep(10)
            except ConnectionClosed as e:
                self.txt_ws = None; await asyncio.sleep(2)
            except Exception:
                self.txt_ws = None; await asyncio.sleep(2)

    async def voice_loop(self):
        while True:
            # Если канал не выбран - просто ждем
            if not self.curr_voice: await asyncio.sleep(0.5); continue
            
            try:
                uri = f"{WS_BASE_URL}/{self.curr_voice}/{self.username}"
                async with websockets.connect(uri) as ws:
                    self.voice_ws = ws
                    # Успешное подключение - обновляем UI
                    self.v_lbl.configure(text=f"Voice: {self.curr_voice}", text_color="green")
                    self.lv_btn.pack(side="right", padx=5)
                    self.v_lst_fr.pack(fill="x", padx=10, pady=5)
                    
                    # Запуск отправки аудио
                    async def safe_send(d):
                        try: await ws.send(d)
                        except: pass
                    self.audio.start(lambda d: self.loop.call_soon_threadsafe(lambda: asyncio.create_task(safe_send(d))))
                    
                    # Цикл приема
                    while self.curr_voice:
                        msg = await ws.recv()
                        if isinstance(msg, bytes):
                            try:
                                nl = msg[0]; sender = msg[1:1+nl].decode()
                                self.audio.play(msg[1+nl:], self.user_vols.get(sender, 1.0))
                            except: pass
                        elif isinstance(msg, str) and msg.startswith("MEMBERS:"):
                            self.upd_mems(msg[8:].split(","))

            # === ОБРАБОТКА ОШИБОК ПОДКЛЮЧЕНИЯ ===
            except InvalidStatusCode as e:
                if e.status_code == 403: # Бан
                    self.curr_voice = None # СБРАСЫВАЕМ КАНАЛ, ЧТОБЫ ПРЕКРАТИТЬ ДОЛБИТЬСЯ
                    self.v_lbl.configure(text="BANNED", text_color="red")
            
            except ConnectionClosed as e:
                if e.code == 1008: # Кик
                    self.curr_voice = None # СБРАСЫВАЕМ КАНАЛ
                    self.v_lbl.configure(text="KICKED", text_color="red")
            
            except Exception as e:
                print(f"Voice err: {e}")
            
            finally:
                self.voice_ws = None; self.audio.stop()
                if self.curr_voice: # Если мы не сами нажали выход
                     self.v_lbl.configure(text="Off", text_color="gray")
                self.lv_btn.pack_forget(); self.v_lst_fr.pack_forget()
                self.upd_mems([])
                await asyncio.sleep(1)

    # === UI HELPER ===
    def load_hist(self, c):
        for w in self.scr.winfo_children(): w.destroy()
        try:
            r = requests.get(f"{API_URL}/history/{c}").json()
            for m in r: self.add_msg(f"{m['username']}: {m['content']}")
        except: pass

    def sw_txt(self, c): self.curr_txt = c; self.head.configure(text=f"# {c}")
    def add_msg(self, t): self.scr.after(0, lambda: ctk.CTkLabel(self.scr, text=t, anchor="w", justify="left", wraplength=600).pack(fill="x", pady=2))
    def send(self):
        if self.txt_ws: asyncio.run_coroutine_threadsafe(self.txt_ws.send(self.msg_e.get()), self.loop); self.msg_e.delete(0, "end")
    
    def join_v(self, c): self.curr_voice = c
    def leave_v(self): self.curr_voice = None

    def upd_mems(self, m): self.v_lst.after(0, lambda: self._dr_m(m))
    def _dr_m(self, m):
        for w in self.v_lst.winfo_children(): w.destroy()
        for u in m:
            if not u: continue
            f = ctk.CTkFrame(self.v_lst, fg_color="#3B3B3B"); f.pack(side="left", padx=5)
            ctk.CTkLabel(f, text=f"🎙 {u}", font=("Arial",12,"bold")).pack(padx=10, pady=(5,0))
            if u != self.username:
                s = ctk.CTkSlider(f, from_=0, to=3, width=80, height=15)
                s.set(self.user_vols.get(u, 1.0)); s.pack(padx=10, pady=5)
                s.configure(command=lambda v, usr=u: self.user_vols.update({usr:v}))
            else: ctk.CTkLabel(f, text="(You)", text_color="gray").pack()

if __name__ == "__main__":
    app = DiscordApp()
    app.mainloop()