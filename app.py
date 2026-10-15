import threading
import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path
import webbrowser

import numpy as np
import sounddevice as sd

RATE = 48000
CHANNELS = 2
BASE_BUFFER_MS = 80
DRIFT_TOLERANCE_MS = 40
RING_SECONDS = 4
MAX_DELAY_MS = 500
QR_URL = "https://vb-audio.com/Cable/index.htm"


class DelayLine:
    def __init__(self, delay_ms):
        self.cap = RATE * RING_SECONDS
        self.buf = np.zeros((self.cap, CHANNELS), dtype=np.float32)
        self.w = 0
        self.r = 0
        self.lock = threading.Lock()
        self.target = self._ms(delay_ms)
        self.primed = False

    @staticmethod
    def _ms(ms):
        return int(RATE * ms / 1000)

    def reset(self):
        with self.lock:
            self.w = 0
            self.r = 0
            self.primed = False

    def set_delay(self, ms):
        with self.lock:
            self.target = self._ms(ms)
            if self.primed:
                self.r = max(self.w - self.target, self.w - self.cap + 4096, 0)

    def push(self, data):
        n = len(data)
        with self.lock:
            idx = (self.w + np.arange(n)) % self.cap
            self.buf[idx] = data
            self.w += n
            if self.w - self.r > self.cap:
                self.r = self.w - self.cap

    def pull(self, n):
        silence = np.zeros((n, CHANNELS), dtype=np.float32)
        with self.lock:
            fill = self.w - self.r
            if not self.primed:
                if fill >= self.target:
                    self.primed = True
                else:
                    return silence
            if fill > self.target + self._ms(DRIFT_TOLERANCE_MS):
                self.r = self.w - self.target
                fill = self.target
            if fill < n:
                self.primed = False
                return silence
            idx = (self.r + np.arange(n)) % self.cap
            out = self.buf[idx].copy()
            self.r += n
            return out


class Router:
    def __init__(self):
        self.wired = DelayLine(BASE_BUFFER_MS)
        self.bt = DelayLine(BASE_BUFFER_MS)
        self.streams = []
        self.running = False

    def start(self, in_dev, wired_dev, bt_dev, delay_ms):
        self.wired.reset()
        self.bt.reset()
        self.wired.set_delay(BASE_BUFFER_MS + delay_ms)
        self.bt.set_delay(BASE_BUFFER_MS)

        ws = sd.WasapiSettings(auto_convert=True)

        def on_input(indata, frames, t, status):
            self.wired.push(indata)
            self.bt.push(indata)

        def make_output_cb(line):
            def cb(outdata, frames, t, status):
                outdata[:] = line.pull(frames)
            return cb

        try:
            out_wired = sd.OutputStream(
                device=wired_dev, samplerate=RATE, channels=CHANNELS,
                dtype="float32", callback=make_output_cb(self.wired),
                extra_settings=ws)
            out_bt = sd.OutputStream(
                device=bt_dev, samplerate=RATE, channels=CHANNELS,
                dtype="float32", callback=make_output_cb(self.bt),
                extra_settings=ws)
            inp = sd.InputStream(
                device=in_dev, samplerate=RATE, channels=CHANNELS,
                dtype="float32", callback=on_input, extra_settings=ws)
            self.streams = [out_wired, out_bt, inp]
            for s in self.streams:
                s.start()
        except Exception:
            self.stop()
            raise
        self.running = True

    def set_wired_delay(self, delay_ms):
        self.wired.set_delay(BASE_BUFFER_MS + delay_ms)

    def stop(self):
        for s in self.streams:
            try:
                s.stop()
                s.close()
            except Exception:
                pass
        self.streams = []
        self.running = False


def list_wasapi_devices():
    wasapi = next((i for i, h in enumerate(sd.query_hostapis())
                   if "WASAPI" in h["name"]), None)
    if wasapi is None:
        raise RuntimeError("WASAPI not found. This app needs Windows.")
    inputs, outputs = [], []
    for i, d in enumerate(sd.query_devices()):
        if d["hostapi"] != wasapi:
            continue
        label = f'{d["name"]}  [{i}]'
        if d["max_input_channels"] >= CHANNELS:
            inputs.append((label, i))
        if d["max_output_channels"] >= CHANNELS and "CABLE" not in d["name"].upper():
            outputs.append((label, i))
    return inputs, outputs


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Dual Headphone Router")
        self.resizable(False, False)
        self.configure(bg="white")
        self.geometry("560x800")
        self.router = Router()
        self.in_map, self.out_map = {}, {}

        style = ttk.Style(self)
        style.configure("App.TFrame", background="white")
        style.configure("App.TLabel", background="white", foreground="#222222")
        style.configure("Status.TLabel", background="white")

        pad = {"padx": 12, "pady": 5}
        frm = ttk.Frame(self, padding=16, style="App.TFrame")
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(0, weight=1)

        ttk.Label(
            frm, text="Dual Headphone Router", font=("Segoe UI", 18, "bold"),
            style="App.TLabel"
        ).grid(row=0, column=0, columnspan=3, pady=(0, 8))
        ttk.Label(
            frm, text="Choose your audio devices, then press Play.",
            style="App.TLabel"
        ).grid(row=1, column=0, columnspan=3, pady=(0, 12))

        ttk.Label(frm, text="System audio source (CABLE Output):",
                  style="App.TLabel").grid(
            row=2, column=0, sticky="w", **pad)
        self.cb_in = ttk.Combobox(frm, width=55, state="readonly")
        self.cb_in.grid(row=3, column=0, columnspan=3, **pad)

        ttk.Label(frm, text="Wired headphones:", style="App.TLabel").grid(
            row=4, column=0, sticky="w", **pad)
        self.cb_wired = ttk.Combobox(frm, width=55, state="readonly")
        self.cb_wired.grid(row=5, column=0, columnspan=3, **pad)

        ttk.Label(frm, text="Bluetooth headphones:", style="App.TLabel").grid(
            row=6, column=0, sticky="w", **pad)
        self.cb_bt = ttk.Combobox(frm, width=55, state="readonly")
        self.cb_bt.grid(row=7, column=0, columnspan=3, **pad)

        ttk.Button(frm, text="Refresh devices", command=self.load_devices).grid(
            row=8, column=0, sticky="w", **pad)

        self.btn = tk.Button(
            frm, text="▶  PLAY", command=self.toggle,
            font=("Segoe UI", 18, "bold"), bg="#e53935", fg="white",
            activebackground="#c62828", activeforeground="white",
            relief="flat", cursor="hand2", padx=36, pady=12,
            borderwidth=0
        )
        self.btn.grid(row=9, column=0, columnspan=3, pady=(16, 18))

        self.delay_label = ttk.Label(
            frm, text="Wired delay: 150 ms", style="App.TLabel")
        self.delay_label.grid(row=10, column=0, sticky="w", **pad)
        self.delay = tk.Scale(frm, from_=0, to=MAX_DELAY_MS, resolution=5,
                              orient="horizontal", length=420, showvalue=False,
                              command=self.on_delay, bg="white",
                              highlightthickness=0, troughcolor="#eeeeee")
        self.delay.set(150)
        self.delay.grid(row=11, column=0, columnspan=3, **pad)

        nudge = ttk.Frame(frm)
        nudge.grid(row=12, column=0, columnspan=3, pady=(0, 12))
        for text, step in (("-10", -10), ("-1", -1), ("+1", 1), ("+10", 10)):
            ttk.Button(nudge, text=text, width=5,
                       command=lambda s=step: self.nudge(s)).pack(side="left", padx=3)

        self.status = ttk.Label(
            frm, text="Stopped", foreground="gray", style="Status.TLabel")
        self.status.grid(row=13, column=0, columnspan=3, pady=(0, 6))

        ttk.Label(
            frm, text="VB-Audio Virtual Cable", style="App.TLabel"
        ).grid(row=14, column=0, columnspan=3, pady=(8, 4))
        qr_path = Path(__file__).resolve().parent / "assets" / "qr_code.png"
        self.qr_image = tk.PhotoImage(file=str(qr_path)).subsample(3, 3)
        ttk.Label(frm, image=self.qr_image).grid(
            row=15, column=0, columnspan=3, pady=(0, 4))
        self.qr_link = tk.Label(
            frm, text=QR_URL, bg="white", fg="#1769aa",
            font=("Segoe UI", 9, "underline"), cursor="hand2",
            takefocus=True
        )
        self.qr_link.grid(row=16, column=0, columnspan=3, pady=(0, 8))
        self.qr_link.bind("<Button-1>", self.open_qr_link)
        self.qr_link.bind("<Return>", self.open_qr_link)
        self.qr_link.bind("<space>", self.open_qr_link)

        self.load_devices()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def open_qr_link(self, _event=None):
        if not webbrowser.open(QR_URL):
            messagebox.showerror("Could not open link", QR_URL)

    def load_devices(self):
        try:
            ins, outs = list_wasapi_devices()
        except Exception as e:
            messagebox.showerror("Audio error", str(e))
            return
        self.in_map = dict(ins)
        self.out_map = dict(outs)
        self.cb_in["values"] = [l for l, _ in ins]
        self.cb_wired["values"] = [l for l, _ in outs]
        self.cb_bt["values"] = [l for l, _ in outs]
        for label, _ in ins:
            if "CABLE OUTPUT" in label.upper():
                self.cb_in.set(label)
                break

    def on_delay(self, _=None):
        ms = int(self.delay.get())
        self.delay_label.config(text=f"Wired delay: {ms} ms")
        if self.router.running:
            self.router.set_wired_delay(ms)

    def nudge(self, step):
        self.delay.set(max(0, min(MAX_DELAY_MS, self.delay.get() + step)))
        self.on_delay()

    def toggle(self):
        if self.router.running:
            self.router.stop()
            self.btn.config(text="▶  PLAY")
            self.status.config(text="Stopped", foreground="gray")
            return

        if not (self.cb_in.get() and self.cb_wired.get() and self.cb_bt.get()):
            messagebox.showwarning("Missing device", "Pick all three devices first.")
            return
        in_dev = self.in_map[self.cb_in.get()]
        wired_dev = self.out_map[self.cb_wired.get()]
        bt_dev = self.out_map[self.cb_bt.get()]
        if wired_dev == bt_dev:
            messagebox.showwarning("Same device", "Wired and Bluetooth must be different.")
            return
        try:
            self.router.start(in_dev, wired_dev, bt_dev, int(self.delay.get()))
        except Exception as e:
            messagebox.showerror("Could not start", str(e))
            return
        self.btn.config(text="■  STOP")
        self.status.config(text="Running - play some audio", foreground="green")

    def on_close(self):
        self.router.stop()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()