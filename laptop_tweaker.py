"""
Laptop Tweaker - a small, safe system dashboard + tweaks tool for Windows.
Setup:   pip install psutil
Run:     python laptop_tweaker.py      (run as Administrator for full cleanup/power access)
"""
import ast
import ctypes
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import urllib.request
from collections import deque
from tkinter import ttk, messagebox

import psutil

try:
    import winreg
except ImportError:  # not on Windows
    winreg = None

NO_WINDOW = 0x08000000 if os.name == "nt" else 0
VERSION = "1.2.0"


# ---------- helpers ----------
def run(cmd):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, creationflags=NO_WINDOW)
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    except Exception as e:
        return False, str(e)


def fmt_bytes(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def folder_size(path):
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda e: None):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def temp_folders():
    paths = {tempfile.gettempdir(), os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Temp")}
    return [p for p in paths if os.path.isdir(p)]


def clean_folder(path):
    freed, skipped = 0, 0
    for entry in os.scandir(path):
        try:
            size = folder_size(entry.path) if entry.is_dir(follow_symlinks=False) else entry.stat().st_size
            if entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry.path)
            else:
                os.remove(entry.path)
            freed += size
        except Exception:
            skipped += 1  # file in use or no permission
    return freed, skipped


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"


def is_enabled(hive, name):
    try:
        with winreg.OpenKey(hive, APPROVED_KEY) as key:
            data, _ = winreg.QueryValueEx(key, name)
            return not (data and data[0] % 2 == 1)  # odd first byte = disabled
    except OSError:
        return True


def set_enabled(hive, name, enabled):
    data = bytes([2 if enabled else 3]) + bytes(11)
    with winreg.CreateKeyEx(hive, APPROVED_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_BINARY, data)


def startup_items():
    items = []
    if not winreg:
        return items
    for hive, label in ((winreg.HKEY_CURRENT_USER, "Current user"), (winreg.HKEY_LOCAL_MACHINE, "All users")):
        try:
            with winreg.OpenKey(hive, RUN_KEY) as key:
                i = 0
                while True:
                    try:
                        name, value, _ = winreg.EnumValue(key, i)
                        items.append((name, value, label, hive, is_enabled(hive, name)))
                        i += 1
                    except OSError:
                        break
        except OSError:
            pass
    return items


TEMP_SCRIPT = """
$v=$null
try{$v=Get-CimInstance -Namespace root/LibreHardwareMonitor -ClassName Sensor -EA Stop | ?{$_.SensorType -eq 'Temperature' -and $_.Name -like '*CPU*'} | select -First 1 -Expand Value}catch{}
if(-not $v){try{$t=Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature -EA Stop | select -First 1 -Expand CurrentTemperature; if($t){$v=[math]::Round($t/10-273.15,1)}}catch{}}
if($v){$v}
"""


def read_temperature():
    """Returns degrees C or None. Uses LibreHardwareMonitor's WMI if it is running, else the ACPI thermal zone."""
    if os.name != "nt":
        return None
    ok, out = run(["powershell", "-NoProfile", "-Command", TEMP_SCRIPT])
    try:
        return float(out.strip().splitlines()[-1]) if ok and out.strip() else None
    except ValueError:
        return None


def set_power(setting, value):
    for flag in ("/setacvalueindex", "/setdcvalueindex"):
        run(["powercfg", flag, "SCHEME_CURRENT", "SUB_PROCESSOR", setting, str(value)])


def cool_mode(on, normal_cap=100):
    """ON: active cooling (fan first), turbo boost off, CPU capped at 80%. OFF: restore normal."""
    set_power("SYSCOOLPOL", 1)                 # 1 = active (fan before throttling)
    set_power("PERFBOOSTMODE", 0 if on else 2)  # 0 = turbo off, 2 = aggressive (Windows default)
    set_power("PROCTHROTTLEMAX", 80 if on else normal_cap)
    return run(["powercfg", "/setactive", "SCHEME_CURRENT"])


CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".laptop_tweaker.json")
PERSONALIZE = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
GAMEBAR = r"Software\Microsoft\GameBar"
PROTECTED = {"system", "registry", "smss.exe", "csrss.exe", "wininit.exe", "services.exe", "lsass.exe",
             "winlogon.exe", "explorer.exe", "dwm.exe", "svchost.exe", "python.exe", "pythonw.exe"}


def load_cfg():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cfg(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
    except OSError:
        pass


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def reg_dword(path, name, value):
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, name, 0, winreg.REG_DWORD, value)
        return True
    except Exception:
        return False


def empty_recycle():
    try:
        ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, 0x7)  # no prompt, no progress, no sound
        return True
    except Exception:
        return False


# ----- network -----
def tcp_latency(host, port=443, timeout=1.0):
    t = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except OSError:
        return None
    return (time.perf_counter() - t) * 1000


def active_iface():
    skip = ("loopback", "vethernet", "virtualbox", "vmware", "bluetooth", "vpn")
    addrs = psutil.net_if_addrs()
    for name, st in psutil.net_if_stats().items():
        if not st.isup or any(k in name.lower() for k in skip):
            continue
        for a in addrs.get(name, []):
            if a.family == socket.AF_INET and not a.address.startswith(("127.", "169.254.")):
                return name
    return None


def set_dns(kind):
    iface = active_iface()
    if not iface:
        return False, "No active network connection found."
    servers = {"cloudflare": ("1.1.1.1", "1.0.0.1"), "google": ("8.8.8.8", "8.8.4.4")}.get(kind)
    if servers is None:
        return run(["netsh", "interface", "ip", "set", "dns", f"name={iface}", "source=dhcp"])
    ok, out = run(["netsh", "interface", "ip", "set", "dns", f"name={iface}", "static", servers[0]])
    if ok:
        run(["netsh", "interface", "ip", "add", "dns", f"name={iface}", servers[1], "index=2"])
    return ok, out


def net_hogs():
    counts = {}
    try:
        for c in psutil.net_connections(kind="inet"):
            if c.pid and c.status == "ESTABLISHED":
                counts[c.pid] = counts.get(c.pid, 0) + 1
    except Exception:
        return None
    out = []
    for pid, n in sorted(counts.items(), key=lambda x: -x[1])[:6]:
        try:
            out.append((psutil.Process(pid).name(), n))
        except psutil.Error:
            pass
    return out


# ----- auto update -----
def version_tuple(v):
    return tuple(int(x) for x in v.split("."))


def fetch_update(url):
    req = urllib.request.Request(url, headers={"User-Agent": "LaptopTweaker"})
    with urllib.request.urlopen(req, timeout=10) as r:
        code = r.read().decode("utf-8")
    m = re.search(r'^VERSION\s*=\s*"([\d.]+)"', code, re.M)
    if not m:
        raise ValueError("no VERSION line found in that file")
    return m.group(1), code


HISTORY_PATH = os.path.join(os.path.expanduser("~"), ".laptop_tweaker_history.json")
RP_CMD = 'Checkpoint-Computer -Description "Laptop Tweaker" -RestorePointType MODIFY_SETTINGS -ErrorAction Stop'
RISK_COLORS = {"Safe": "#34c759", "Moderate": "#ff9f0a", "Advanced": "#ff453a"}

PS_SYSINFO = r"""
$os = Get-CimInstance Win32_OperatingSystem
$cs = Get-CimInstance Win32_ComputerSystem
$bios = Get-CimInstance Win32_BIOS
$disks = @(Get-PhysicalDisk | ForEach-Object {
  $r = $_ | Get-StorageReliabilityCounter -ErrorAction SilentlyContinue
  $w = ""
  if ($r -and $r.Wear -ne $null) { $w = ", wear " + $r.Wear + "%" }
  $_.FriendlyName + ": " + $_.MediaType + ", health " + $_.HealthStatus + ", " + [math]::Round($_.Size/1GB) + " GB" + $w
}) -join "; "
[pscustomobject]@{
  model = $cs.Manufacturer + " " + $cs.Model
  cpu = (Get-CimInstance Win32_Processor | Select-Object -First 1).Name
  gpudrv = (@(Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name + " (driver " + $_.DriverVersion + ")" }) -join "; ")
  os = $os.Caption + " (build " + $os.BuildNumber + ")"
  bios = $bios.SMBIOSBIOSVersion
  boot = $os.LastBootUpTime.ToString("yyyy-MM-dd HH:mm")
  disks = $disks
} | ConvertTo-Json -Compress
"""


def load_hist():
    try:
        with open(HISTORY_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_hist(h):
    try:
        with open(HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(h, f)
    except OSError:
        pass


def reg_get(path, name):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
            return winreg.QueryValueEx(k, name)[0]
    except Exception:
        return None


def reg_delete(path, name):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, name)
        return True
    except FileNotFoundError:
        return True
    except Exception:
        return False


def plan_guid():
    ok, out = run(["powercfg", "/getactivescheme"])
    m = re.search(r"([0-9a-fA-F]{8}-[0-9a-fA-F-]{27})", out) if ok else None
    return m.group(1) if m else None


def run_undo(u):
    """Reverse a recorded change. u is one undo spec or a list of them."""
    if isinstance(u, list):
        return all([run_undo(x) for x in reversed(u)])
    t = u.get("t")
    if t == "reg":
        if u["old"] is None:
            return reg_delete(u["path"], u["name"])
        return reg_dword(u["path"], u["name"], u["old"])
    if t == "cmd":
        return all([run(c)[0] for c in u["cmds"]])
    if t == "plan":
        return run(["powercfg", "/setactive", u["guid"]])[0]
    if t == "cool_off":
        return cool_mode(False)[0]
    if t == "dns_auto":
        return set_dns("auto")[0]
    return False


class Tooltip:
    def __init__(self, widget, text):
        self.w, self.text, self.tip = widget, text, None
        widget.bind("<Enter>", self.show)
        widget.bind("<Leave>", self.hide)

    def show(self, _e=None):
        if self.tip:
            return
        x, y = self.w.winfo_rootx() + 20, self.w.winfo_rooty() + 24
        self.tip = tk.Toplevel(self.w)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self.tip, text=self.text, justify="left", wraplength=340, bg="#2b2d31", fg="#e6e6e6",
                 relief="solid", borderwidth=1, padx=8, pady=6).pack()

    def hide(self, _e=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None


class Spark(tk.Canvas):
    """Tiny live line graph for the last 60 samples."""
    def __init__(self, master, color, **kw):
        super().__init__(master, height=70, highlightthickness=0, **kw)
        self.color = color

    def draw(self, data, maxv=100):
        self.delete("all")
        data = list(data)
        n = len(data)
        if n < 2:
            return
        w, h = max(self.winfo_width(), 50), max(self.winfo_height(), 20)
        step = w / 59
        pts = []
        for i, v in enumerate(data):
            v = maxv if v is None else min(v, maxv)
            pts += [w - (n - 1 - i) * step, h - 3 - (v / maxv) * (h - 6)]
        self.create_line(*pts, fill=self.color, width=2)


# ---------- app ----------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Laptop Tweaker")
        self.geometry("780x760")
        self.minsize(660, 600)

        self.style = ttk.Style(self)
        self.style.theme_use("clam")
        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=(8, 0))
        self.theme_btn = ttk.Button(top, command=lambda: self.apply_theme(
            "light" if self.theme_name == "dark" else "dark"))
        self.theme_btn.pack(side="right")
        self.apply_theme("dark")

        tabs = ttk.Notebook(self)
        tabs.pack(fill="both", expand=True, padx=8, pady=8)
        names = ("Home", "Network", "Optimize", "Power", "Cleanup", "Startup", "System", "History", "Details", "Updates")
        fr = {n: ttk.Frame(tabs, padding=12) for n in names}
        for n in names:
            tabs.add(fr[n], text=n)
        self.home, self.net, self.opt, self.power = fr["Home"], fr["Network"], fr["Optimize"], fr["Power"]
        self.clean, self.startup, self.dash, self.upd = fr["Cleanup"], fr["Startup"], fr["Details"], fr["Updates"]
        self.sysf, self.hist = fr["System"], fr["History"]

        self.sparks = []
        self.ui_queue = []
        self.hist_data = load_hist()
        self.rp_done = False
        self.cfg = load_cfg()
        self.cpu_hist, self.ram_hist, self.ping_hist = deque(maxlen=60), deque(maxlen=60), deque(maxlen=60)
        self.ping_target = "1.1.1.1"
        self.pending_update, self.update_msg = None, ""

        self.build_home()
        self.build_network()
        self.build_optimize()
        self.build_dashboard()
        self.build_power()
        self.build_cleanup()
        self.build_startup()
        self.build_updates()
        self.build_system()
        self.build_history()

        psutil.cpu_percent(None)
        self.tick = 0
        self.temp_text = "CPU temperature: checking..."
        self.temp_val = None
        self.cooling = False
        threading.Thread(target=self.temp_loop, daemon=True).start()
        threading.Thread(target=self.ping_loop, daemon=True).start()
        if self.cfg.get("auto_update"):
            self.check_updates()
        self.update_stats()

    # ----- Theme -----
    THEMES = {
        "dark": dict(bg="#1e1f22", fg="#e6e6e6", field="#2b2d31", accent="#4f8cff", btn="#3a3d42"),
        "light": dict(bg="#f3f3f3", fg="#1b1b1b", field="#ffffff", accent="#2563eb", btn="#dcdcdc"),
    }

    def apply_theme(self, name):
        self.theme_name = name
        c, s = self.THEMES[name], self.style
        self.configure(bg=c["bg"])
        s.configure(".", background=c["bg"], foreground=c["fg"], fieldbackground=c["field"])
        s.configure("TFrame", background=c["bg"])
        s.configure("TLabel", background=c["bg"], foreground=c["fg"])
        s.configure("TCheckbutton", background=c["bg"], foreground=c["fg"])
        s.configure("TButton", background=c["btn"], foreground=c["fg"], borderwidth=0, padding=6)
        s.map("TButton", background=[("active", c["accent"])], foreground=[("active", "#ffffff")])
        s.configure("TNotebook", background=c["bg"], borderwidth=0)
        s.configure("TNotebook.Tab", background=c["btn"], foreground=c["fg"], padding=(7, 6))
        s.map("TNotebook.Tab", background=[("selected", c["accent"])], foreground=[("selected", "#ffffff")])
        s.configure("Treeview", background=c["field"], foreground=c["fg"], fieldbackground=c["field"], borderwidth=0)
        s.configure("Treeview.Heading", background=c["btn"], foreground=c["fg"])
        s.map("Treeview", background=[("selected", c["accent"])], foreground=[("selected", "#ffffff")])
        s.configure("Horizontal.TProgressbar", background=c["accent"], troughcolor=c["field"], borderwidth=0)
        s.configure("Horizontal.TScale", background=c["bg"], troughcolor=c["field"])
        self.theme_btn.config(text="Light theme" if name == "dark" else "Dark theme")
        for w in ("TEntry", "TCombobox", "TSpinbox"):
            s.configure(w, fieldbackground=c["field"], foreground=c["fg"], insertcolor=c["fg"])
        for sp in getattr(self, "sparks", []):
            sp.config(bg=c["field"])

    # ----- Temperature (background thread so the UI never freezes) -----
    def temp_loop(self):
        import time
        while True:
            t = read_temperature()
            self.temp_val = t
            self.temp_text = (f"CPU temperature: {t:.0f} °C" if t is not None else
                              "CPU temperature: not available (run as Administrator, or start LibreHardwareMonitor)")
            time.sleep(5)

    # ----- Dashboard -----
    def build_dashboard(self):
        self.bars = {}
        for key in ("CPU", "RAM", "Disk (C:)", "Battery"):
            row = ttk.Frame(self.dash)
            row.pack(fill="x", pady=6)
            lbl = ttk.Label(row, text=key, width=22)
            lbl.pack(side="left")
            bar = ttk.Progressbar(row, maximum=100, length=250)
            bar.pack(side="left", fill="x", expand=True)
            self.bars[key] = (lbl, bar)

        self.clock_lbl = ttk.Label(self.dash, text="")
        self.clock_lbl.pack(anchor="w", pady=(8, 2))
        self.temp_lbl = ttk.Label(self.dash, text="")
        self.temp_lbl.pack(anchor="w", pady=2)
        ttk.Label(self.dash, text="Top 5 apps by memory:", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(10, 2))
        self.proc_lbl = ttk.Label(self.dash, text="", justify="left")
        self.proc_lbl.pack(anchor="w")

    def update_stats(self):
        cpu = psutil.cpu_percent(None)
        lbl, bar = self.bars["CPU"]
        lbl.config(text=f"CPU  {cpu:.0f}%")
        bar["value"] = cpu

        vm = psutil.virtual_memory()
        lbl, bar = self.bars["RAM"]
        lbl.config(text=f"RAM  {fmt_bytes(vm.used)} / {fmt_bytes(vm.total)}")
        bar["value"] = vm.percent

        try:
            du = shutil.disk_usage(os.environ.get("SystemDrive", "C:") + "\\")
            lbl, bar = self.bars["Disk (C:)"]
            lbl.config(text=f"Disk  {fmt_bytes(du.used)} / {fmt_bytes(du.total)}")
            bar["value"] = du.used / du.total * 100
        except OSError:
            pass

        lbl, bar = self.bars["Battery"]
        batt = psutil.sensors_battery()
        if batt:
            lbl.config(text=f"Battery  {batt.percent:.0f}% ({'charging' if batt.power_plugged else 'on battery'})")
            bar["value"] = batt.percent
        else:
            lbl.config(text="Battery  n/a")

        freq = psutil.cpu_freq()
        if freq:
            self.clock_lbl.config(text=f"CPU clock: {freq.current:.0f} MHz (max {freq.max:.0f} MHz)")

        self.temp_lbl.config(text=self.temp_text)
        self.refresh_home(cpu, vm.percent)
        while self.ui_queue:
            self.ui_queue.pop(0)()
        if self.update_msg:
            self.upd_lbl.config(text=self.update_msg)
            self.update_msg = ""
        if self.pending_update:
            ver, code = self.pending_update
            self.pending_update = None
            if messagebox.askyesno("Update available", f"Version {ver} is available (you have {VERSION}).\nInstall now and restart?"):
                self.install_update(code)

        try:
            limit = self.cool_limit.get()
        except tk.TclError:
            limit = 85
        if self.auto_cool.get() and self.temp_val is not None:
            if self.temp_val >= limit and not self.cooling:
                self.cooling = True
                threading.Thread(target=cool_mode, args=(True,), daemon=True).start()
            elif self.temp_val <= limit - 10 and self.cooling:
                self.cooling = False
                threading.Thread(target=cool_mode, args=(False, int(self.cpu_cap.get())), daemon=True).start()

        if self.tick % 3 == 0:  # refresh process list every ~3 cycles (lighter)
            procs = []
            for p in psutil.process_iter(["name", "memory_info"]):
                try:
                    procs.append((p.info["memory_info"].rss, p.info["name"]))
                except (psutil.NoSuchProcess, psutil.AccessDenied, TypeError):
                    pass
            procs.sort(reverse=True)
            self.proc_lbl.config(text="\n".join(f"{n}  -  {fmt_bytes(m)}" for m, n in procs[:5]))
        self.tick += 1
        self.after(1500, self.update_stats)

    # ----- Power -----
    def build_power(self):
        ttk.Label(self.power, text="Power plan", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        row = ttk.Frame(self.power)
        row.pack(fill="x", pady=6)
        ttk.Button(row, text="Battery saver", command=lambda: self.set_plan("SCHEME_MAX")).pack(side="left", padx=2)
        ttk.Button(row, text="Balanced", command=lambda: self.set_plan("SCHEME_BALANCED")).pack(side="left", padx=2)
        ttk.Button(row, text="High performance", command=lambda: self.set_plan("SCHEME_MIN")).pack(side="left", padx=2)
        self.plan_lbl = ttk.Label(self.power, text="")
        self.plan_lbl.pack(anchor="w", pady=4)

        ttk.Label(self.power, text="Max processor state", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(16, 0))
        ttk.Label(self.power, text="Lower = cooler, quieter, longer battery. 100 = full speed.").pack(anchor="w")
        self.cpu_cap = tk.IntVar(value=100)
        ttk.Scale(self.power, from_=50, to=100, variable=self.cpu_cap, command=lambda v: self.cap_lbl.config(
            text=f"{int(float(v))}%")).pack(fill="x", pady=6)
        self.cap_lbl = ttk.Label(self.power, text="100%")
        self.cap_lbl.pack(anchor="w")
        ttk.Button(self.power, text="Apply CPU cap", command=self.apply_cap).pack(anchor="w", pady=6)

        ttk.Label(self.power, text="Cooling", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(16, 0))
        ttk.Label(self.power, text="Cool mode turns off turbo boost, caps the CPU at 80%, and tells Windows "
                  "to spin the fan up before throttling.", wraplength=540, justify="left").pack(anchor="w")
        row = ttk.Frame(self.power)
        row.pack(anchor="w", pady=6)
        ttk.Button(row, text="Cool mode ON", command=lambda: self.manual_cool(True)).pack(side="left", padx=2)
        ttk.Button(row, text="Back to normal", command=lambda: self.manual_cool(False)).pack(side="left", padx=2)
        auto = ttk.Frame(self.power)
        auto.pack(anchor="w", pady=4)
        self.auto_cool = tk.BooleanVar(value=False)
        self.cool_limit = tk.IntVar(value=85)
        ttk.Checkbutton(auto, text="Switch on Cool mode automatically at", variable=self.auto_cool).pack(side="left")
        ttk.Spinbox(auto, from_=60, to=95, width=4, textvariable=self.cool_limit).pack(side="left", padx=4)
        ttk.Label(auto, text="°C (turns off again 10° lower)").pack(side="left")
        ttk.Label(self.power, text="The app can't control the fan directly; laptop fans are run by the BIOS. "
                  "Auto cool needs the temperature reading to work.", wraplength=540, justify="left").pack(anchor="w", pady=4)
        self.refresh_plan()

    def refresh_plan(self):
        ok, out = run(["powercfg", "/getactivescheme"])
        self.plan_lbl.config(text=out if ok else "Could not read power plan.")

    def set_plan(self, alias):
        old = plan_guid()
        ok, out = run(["powercfg", "/setactive", alias])
        if ok and old:
            self.log("Power plan: " + alias, {"t": "plan", "guid": old})
        if not ok:
            messagebox.showwarning("Power plan", "Couldn't switch to that plan. Some laptops hide plans "
                                   "(especially High performance).\n\n" + out)
        self.refresh_plan()

    def manual_cool(self, on):
        ok, out = cool_mode(on, int(self.cpu_cap.get()))
        self.cooling = on
        if ok and on:
            self.log("Cool mode ON", {"t": "cool_off"})
        messagebox.showinfo("Cooling", ("Cool mode is ON." if on else "Back to normal.") if ok else out)

    def apply_cap(self):
        v = str(int(self.cpu_cap.get()))
        for flag in ("/setacvalueindex", "/setdcvalueindex"):
            run(["powercfg", flag, "SCHEME_CURRENT", "SUB_PROCESSOR", "PROCTHROTTLEMAX", v])
        ok, out = run(["powercfg", "/setactive", "SCHEME_CURRENT"])
        messagebox.showinfo("CPU cap", f"Max processor state set to {v}%." if ok else out)

    # ----- Cleanup -----
    def build_cleanup(self):
        ttk.Label(self.clean, text="Temporary files", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.clean_lbl = ttk.Label(self.clean, text="Click Scan to see how much space can be freed.")
        self.clean_lbl.pack(anchor="w", pady=6)
        row = ttk.Frame(self.clean)
        row.pack(anchor="w")
        ttk.Button(row, text="Scan", command=self.scan_temp).pack(side="left", padx=2)
        ttk.Button(row, text="Clean temp files", command=self.do_clean).pack(side="left", padx=2)
        ttk.Button(row, text="Open Windows Storage settings",
                   command=lambda: os.startfile("ms-settings:storagesense")
                   if os.name == "nt" else None).pack(side="left", padx=2)

    def scan_temp(self):
        total = sum(folder_size(p) for p in temp_folders())
        self.clean_lbl.config(text=f"Temp files found: {fmt_bytes(total)}")

    def do_clean(self):
        if not messagebox.askyesno("Clean temp files", "Delete temporary files? Files in use are skipped."):
            return
        freed = skipped = 0
        for p in temp_folders():
            f, s = clean_folder(p)
            freed += f
            skipped += s
        self.clean_lbl.config(text=f"Freed {fmt_bytes(freed)} ({skipped} items skipped, in use or protected).")

    # ----- Startup -----
    def build_startup(self):
        ttk.Label(self.startup, text="Apps that launch at login", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.meta = {}
        self.tree = ttk.Treeview(self.startup, columns=("status", "scope", "cmd"), show="tree headings", height=12)
        self.tree.heading("#0", text="Name")
        self.tree.heading("status", text="Status")
        self.tree.heading("scope", text="Scope")
        self.tree.heading("cmd", text="Command")
        self.tree.column("#0", width=140)
        self.tree.column("status", width=55, anchor="center")
        self.tree.column("scope", width=85)
        self.tree.column("cmd", width=240)
        self.tree.pack(fill="both", expand=True, pady=6)
        row = ttk.Frame(self.startup)
        row.pack(anchor="w")
        ttk.Button(row, text="Refresh", command=self.load_startup).pack(side="left", padx=2)
        ttk.Button(row, text="Enable / Disable selected", command=self.toggle_startup).pack(side="left", padx=2)
        ttk.Button(row, text="Open Task Manager",
                   command=lambda: run(["taskmgr", "/0", "/startup"])).pack(side="left", padx=2)
        self.load_startup()

    def load_startup(self):
        self.tree.delete(*self.tree.get_children())
        self.meta = {}
        for name, cmd, scope, hive, enabled in startup_items():
            iid = self.tree.insert("", "end", text=name, values=("On" if enabled else "Off", scope, cmd))
            self.meta[iid] = (name, hive, enabled)

    def toggle_startup(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Startup", "Select an item in the list first.")
            return
        name, hive, enabled = self.meta[sel[0]]
        try:
            set_enabled(hive, name, not enabled)
        except OSError as e:
            messagebox.showwarning("Startup", "Couldn't change this one. 'All users' items need Administrator.\n\n" + str(e))
            return
        self.load_startup()

    # ----- Home (new dashboard) -----
    def build_home(self):
        self.home_vals = {}
        cards = ttk.Frame(self.home)
        cards.pack(fill="x")
        for i, (key, color) in enumerate((("CPU", "#4f8cff"), ("RAM", "#34c759"), ("Ping", "#ff9f0a"))):
            card = ttk.Frame(cards, padding=6)
            card.grid(row=0, column=i, sticky="nsew", padx=4)
            cards.columnconfigure(i, weight=1, uniform="c")
            ttk.Label(card, text=key).pack(anchor="w")
            v = ttk.Label(card, text="--", font=("Segoe UI", 20, "bold"))
            v.pack(anchor="w")
            sp = Spark(card, color, bg=self.THEMES[self.theme_name]["field"])
            sp.pack(fill="x", pady=4)
            self.home_vals[key] = v
            self.sparks.append(sp)
            setattr(self, "spark_" + key.lower(), sp)
        self.home_sub = ttk.Label(self.home, text="")
        self.home_sub.pack(anchor="w", pady=(8, 2))
        self.home_status = ttk.Label(self.home, text="Running as Administrator" if is_admin() else
                                     "Not running as Administrator: temperature, cleanup and some tweaks may be limited")
        self.home_status.pack(anchor="w", pady=2)

        ttk.Label(self.home, text="Quick actions", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(18, 4))
        grid = ttk.Frame(self.home)
        grid.pack(anchor="w")
        actions = (("Game Boost", self.game_boost), ("Cool mode ON", lambda: self.manual_cool(True)),
                   ("Clean temp files", self.quick_clean), ("Flush DNS (ping)", self.flush_dns))
        for i, (txt, fn) in enumerate(actions):
            ttk.Button(grid, text=txt, command=fn, width=20).grid(row=i // 2, column=i % 2, padx=4, pady=4)
        ttk.Label(self.home, text="Game Boost = High performance plan + Game Mode + no transparency + DNS flush. "
                  "It runs hotter, so use Cool mode if the laptop heats up.", wraplength=640,
                  justify="left").pack(anchor="w", pady=8)

    def ping_loop(self):
        while True:
            self.ping_hist.append(tcp_latency(self.ping_target))
            time.sleep(1)

    def ping_summary(self):
        try:
            h = list(self.ping_hist)
        except RuntimeError:
            return None
        if not h:
            return None
        vals = [x for x in h if x is not None]
        loss = 100 * (len(h) - len(vals)) / len(h)
        if not vals:
            return dict(avg=None, jitter=None, loss=loss), h
        jit = sum(abs(a - b) for a, b in zip(vals, vals[1:])) / max(len(vals) - 1, 1)
        return dict(avg=sum(vals) / len(vals), jitter=jit, loss=loss), h

    def refresh_home(self, cpu, ram):
        self.cpu_hist.append(cpu)
        self.ram_hist.append(ram)
        self.home_vals["CPU"].config(text=f"{cpu:.0f}%")
        self.home_vals["RAM"].config(text=f"{ram:.0f}%")
        self.spark_cpu.draw(self.cpu_hist, 100)
        self.spark_ram.draw(self.ram_hist, 100)
        big, sub = "--", "measuring..."
        summ = self.ping_summary()
        if summ:
            st, hist = summ
            if st["avg"] is None:
                big, sub = "offline", f"no response ({st['loss']:.0f}% loss)"
            else:
                big = f"{st['avg']:.0f} ms"
                sub = f"avg {st['avg']:.0f} ms, jitter {st['jitter']:.0f} ms, loss {st['loss']:.0f}%"
            self.spark_ping.draw(hist, max([100] + [x for x in hist if x is not None]))
        self.home_vals["Ping"].config(text=big)
        self.home_sub.config(text=f"Ping: {sub}    |    {self.temp_text}")
        self.net_lbl.config(text=f"Latency to {self.ping_target}: {sub}")

    def game_boost(self):
        done, undo = [], []
        old = plan_guid()
        if run(["powercfg", "/setactive", "SCHEME_MIN"])[0]:
            done.append("High performance power plan")
            if old:
                undo.append({"t": "plan", "guid": old})
        for label, path, name, val in (("Game Mode on", GAMEBAR, "AutoGameModeEnabled", 1),
                                       ("Transparency effects off", PERSONALIZE, "EnableTransparency", 0)):
            u = self.reg_tweak(path, name, val)
            if u:
                done.append(label)
                undo.append(u)
        if run(["ipconfig", "/flushdns"])[0]:
            done.append("DNS cache flushed")
        if undo:
            self.log("Game Boost", undo)
        self.refresh_plan()
        messagebox.showinfo("Game Boost", ("Applied:\n- " + "\n- ".join(done) + "\n\nUndo it any time on the History tab.")
                            if done else "Nothing could be applied.")

    def quick_clean(self):
        if not messagebox.askyesno("Clean temp files", "Delete temporary files? Files in use are skipped."):
            return
        freed = sum(clean_folder(p)[0] for p in temp_folders())
        messagebox.showinfo("Cleanup", f"Freed {fmt_bytes(freed)}.")

    # ----- Network / low ping -----
    def build_network(self):
        bold = ("Segoe UI", 10, "bold")
        ttk.Label(self.net, text="Connection quality", font=bold).pack(anchor="w")
        self.net_lbl = ttk.Label(self.net, text="Measuring...")
        self.net_lbl.pack(anchor="w", pady=4)
        row = ttk.Frame(self.net)
        row.pack(anchor="w", pady=4)
        ttk.Label(row, text="Test server:").pack(side="left", padx=(0, 6))
        cb = ttk.Combobox(row, values=("Cloudflare (1.1.1.1)", "Google (8.8.8.8)"), state="readonly", width=22)
        cb.current(0)
        cb.bind("<<ComboboxSelected>>", lambda e: self.set_target("1.1.1.1" if cb.current() == 0 else "8.8.8.8"))
        cb.pack(side="left")

        ttk.Label(self.net, text="DNS (can speed up lookups and fix slow loading)", font=bold).pack(anchor="w", pady=(16, 4))
        row = ttk.Frame(self.net)
        row.pack(anchor="w")
        for txt, fn in (("Flush DNS cache", self.flush_dns), ("Cloudflare DNS", lambda: self.dns("cloudflare")),
                        ("Google DNS", lambda: self.dns("google")), ("Automatic (default)", lambda: self.dns("auto"))):
            ttk.Button(row, text=txt, command=fn).pack(side="left", padx=2)

        ttk.Label(self.net, text="What is using my network?", font=bold).pack(anchor="w", pady=(16, 4))
        ttk.Button(self.net, text="Show top apps by connections", command=self.show_hogs).pack(anchor="w")
        self.hog_lbl = ttk.Label(self.net, text="", justify="left")
        self.hog_lbl.pack(anchor="w", pady=6)
        ttk.Label(self.net, text="Honest tips: software can't make your internet provider faster. These tools "
                  "cut local delays. The biggest wins are a cable (Ethernet) or the 5 GHz Wi-Fi band, sitting "
                  "closer to the router, pausing downloads and cloud syncing, and picking the nearest game server.",
                  wraplength=640, justify="left").pack(anchor="w", pady=8)

    def set_target(self, host):
        self.ping_target = host
        self.ping_hist.clear()

    def flush_dns(self):
        ok, out = run(["ipconfig", "/flushdns"])
        messagebox.showinfo("DNS", "DNS cache flushed." if ok else out)

    def dns(self, kind):
        ok, out = set_dns(kind)
        if ok and kind != "auto":
            self.log("DNS: " + kind, {"t": "dns_auto"})
        run(["ipconfig", "/flushdns"])
        messagebox.showinfo("DNS", ("DNS updated." if kind != "auto" else "DNS set to automatic.") if ok
                            else "Couldn't change DNS (run as Administrator).\n\n" + out)

    def show_hogs(self):
        hogs = net_hogs()
        if hogs is None:
            self.hog_lbl.config(text="Couldn't read connections (run as Administrator).")
        else:
            self.hog_lbl.config(text="\n".join(f"{n}  -  {c} connections" for n, c in hogs) or "Nothing active.")

    # ----- Optimize -----
    def tweak_defs(self):
        return [
            dict(name="Game Boost", risk="Safe", direct=True, fn=self.game_boost,
                 why="Switches to the High performance plan, turns on Game Mode and turns off transparency. "
                     "Good for games, but the laptop runs hotter and uses more battery. Fully undoable."),
            dict(name="Transparency effects off", risk="Safe", reg=(PERSONALIZE, "EnableTransparency", 0),
                 why="Removes see-through window effects. Saves a little graphics power and memory on a "
                     "low-end integrated GPU."),
            dict(name="Game Mode on", risk="Safe", reg=(GAMEBAR, "AutoGameModeEnabled", 1),
                 why="Tells Windows to pause background tasks like updates while a game is running."),
            dict(name="Hibernation off (frees disk)", risk="Moderate",
                 cmd=(["powercfg", "/h", "off"], ["powercfg", "/h", "on"]),
                 why="Deletes the hibernation file, freeing several GB. You lose Hibernate and Fast Startup. "
                     "Undo turns it back on."),
            dict(name="Empty Recycle Bin", risk="Moderate", rp=False, fn=empty_recycle,
                 why="Permanently deletes everything in the Recycle Bin. This can't be undone."),
        ]

    def build_optimize(self):
        bold = ("Segoe UI", 10, "bold")
        ttk.Label(self.opt, text="Tweaks (hover the ? for why it matters)", font=bold).pack(anchor="w")
        grid = ttk.Frame(self.opt)
        grid.pack(anchor="w", pady=6)
        for i, t in enumerate(self.tweak_defs()):
            ttk.Button(grid, text=t["name"], width=30, command=lambda t=t: self.apply_tweak(t)).grid(
                row=i, column=0, padx=3, pady=3, sticky="w")
            ttk.Label(grid, text=t["risk"], foreground=RISK_COLORS[t["risk"]], width=10).grid(row=i, column=1)
            q = ttk.Label(grid, text="?", font=bold, width=3)
            q.grid(row=i, column=2)
            Tooltip(q, t["why"])
        ttk.Label(self.opt, text="Every change is logged on the History tab, where it can be undone.").pack(anchor="w")

        ttk.Label(self.opt, text="Heaviest apps right now (memory)", font=bold).pack(anchor="w", pady=(14, 4))
        self.heavy = ttk.Treeview(self.opt, columns=("pid", "mem"), show="tree headings", height=9)
        self.heavy.heading("#0", text="App")
        self.heavy.heading("pid", text="PID")
        self.heavy.heading("mem", text="Memory")
        self.heavy.column("#0", width=260)
        self.heavy.column("pid", width=70, anchor="center")
        self.heavy.column("mem", width=90, anchor="e")
        self.heavy.pack(fill="x")
        row = ttk.Frame(self.opt)
        row.pack(anchor="w", pady=6)
        ttk.Button(row, text="Refresh", command=self.load_heavy).pack(side="left", padx=2)
        ttk.Button(row, text="End selected app", command=self.end_heavy).pack(side="left", padx=2)
        self.load_heavy()

    def reg_tweak(self, path, name, value):
        old = reg_get(path, name)
        if reg_dword(path, name, value):
            return {"t": "reg", "path": path, "name": name, "old": old}
        return None

    def apply_tweak(self, t):
        if t.get("direct"):
            return t["fn"]()
        if not self.safety(t["name"], t["risk"], t.get("rp", True)):
            return
        undo, ok = None, False
        if "reg" in t:
            undo = self.reg_tweak(*t["reg"])
            ok = undo is not None
        elif "cmd" in t:
            ok = run(t["cmd"][0])[0]
            undo = {"t": "cmd", "cmds": [t["cmd"][1]]}
        elif "fn" in t:
            ok = bool(t["fn"]())
        if ok:
            self.log(t["name"], undo)
            messagebox.showinfo("Optimize", f"Done: {t['name']}.")
        else:
            messagebox.showwarning("Optimize", "That didn't work (try running as Administrator).")

    def safety(self, name, risk, rp=True):
        if risk == "Safe" or not rp or not self.rp_var.get() or self.rp_done:
            return True
        if self.make_restore_point():
            return True
        return messagebox.askyesno("No restore point", f'Couldn\'t create a restore point. Apply "{name}" anyway?')

    def load_heavy(self):
        self.heavy.delete(*self.heavy.get_children())
        rows = []
        for p in psutil.process_iter(["name", "memory_info"]):
            try:
                rows.append((p.info["memory_info"].rss, p.info["name"], p.pid))
            except (psutil.Error, TypeError, AttributeError):
                pass
        rows.sort(reverse=True)
        for m, n, pid in rows[:10]:
            self.heavy.insert("", "end", iid=str(pid), text=n, values=(pid, f"{m / 1048576:.0f} MB"))

    def end_heavy(self):
        sel = self.heavy.selection()
        if not sel:
            messagebox.showinfo("End app", "Select an app first.")
            return
        try:
            pid = int(sel[0])
            proc = psutil.Process(pid)
            name = proc.name()
            if pid == os.getpid() or name.lower() in PROTECTED:
                messagebox.showwarning("End app", f"{name} is a protected system/own process and won't be closed.")
                return
            if not messagebox.askyesno("End app", f"End {name}? Unsaved work in it will be lost."):
                return
            proc.terminate()
        except psutil.Error as e:
            messagebox.showwarning("End app", str(e))
        self.after(800, self.load_heavy)

    # ----- Updates -----
    def build_updates(self):
        bold = ("Segoe UI", 10, "bold")
        ttk.Label(self.upd, text=f"Laptop Tweaker v{VERSION}", font=bold).pack(anchor="w")
        ttk.Label(self.upd, text="Update link (a direct https link to your laptop_tweaker.py, e.g. a GitHub 'Raw' link):",
                  wraplength=640).pack(anchor="w", pady=(10, 2))
        self.url_var = tk.StringVar(value=self.cfg.get("update_url", ""))
        ttk.Entry(self.upd, textvariable=self.url_var, width=80).pack(fill="x")
        self.auto_var = tk.BooleanVar(value=bool(self.cfg.get("auto_update", False)))
        ttk.Checkbutton(self.upd, text="Check for updates automatically when the app starts",
                        variable=self.auto_var).pack(anchor="w", pady=8)
        row = ttk.Frame(self.upd)
        row.pack(anchor="w")
        ttk.Button(row, text="Save settings", command=self.save_settings).pack(side="left", padx=2)
        ttk.Button(row, text="Check for updates now", command=lambda: self.check_updates(True)).pack(side="left", padx=2)
        self.upd_lbl = ttk.Label(self.upd, text="")
        self.upd_lbl.pack(anchor="w", pady=8)
        ttk.Label(self.upd, text="Only use a link you control. An update is code that runs on your PC with "
                  "your permissions, so anyone who can change that file can run code on your laptop. "
                  "A backup (.bak) of the old version is kept next to the app.", wraplength=640,
                  justify="left").pack(anchor="w", pady=8)

    def save_settings(self):
        self.cfg["update_url"] = self.url_var.get().strip()
        self.cfg["auto_update"] = bool(self.auto_var.get())
        save_cfg(self.cfg)
        self.upd_lbl.config(text="Saved.")

    def check_updates(self, manual=False):
        url = (self.url_var.get().strip() if hasattr(self, "url_var") else self.cfg.get("update_url", ""))
        if not url.startswith("https://"):
            if manual:
                self.upd_lbl.config(text="Paste an https:// link first.")
            return
        if manual:
            self.upd_lbl.config(text="Checking...")
        threading.Thread(target=self._update_worker, args=(url, manual), daemon=True).start()

    def _update_worker(self, url, manual):
        try:
            ver, code = fetch_update(url)
            if version_tuple(ver) > version_tuple(VERSION):
                self.pending_update = (ver, code)
            elif manual:
                self.update_msg = f"You're up to date (v{VERSION})."
        except Exception as e:
            if manual:
                self.update_msg = f"Update check failed: {e}"

    def install_update(self, code):
        try:
            ast.parse(code)  # refuse anything that isn't valid Python
            path = os.path.abspath(__file__)
            shutil.copy2(path, path + ".bak")
            tmp = path + ".new"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(code)
            os.replace(tmp, path)
            subprocess.Popen([sys.executable, path])
        except Exception as e:
            messagebox.showwarning("Update", f"Update failed, nothing was changed.\n\n{e}")
            return
        self.destroy()
        os._exit(0)

    # ----- System info + battery -----
    def build_system(self):
        bold = ("Segoe UI", 10, "bold")
        ttk.Label(self.sysf, text="System information", font=bold).pack(anchor="w")
        self.sys_lbl = ttk.Label(self.sysf, text="Loading...", justify="left", wraplength=700)
        self.sys_lbl.pack(anchor="w", pady=6)
        ttk.Button(self.sysf, text="Refresh", command=self.load_sysinfo).pack(anchor="w")

        ttk.Label(self.sysf, text="Battery health", font=bold).pack(anchor="w", pady=(18, 4))
        self.batt_lbl = ttk.Label(self.sysf, text="Click the button to measure how much charge your battery "
                                  "still holds compared with when it was new.", wraplength=700, justify="left")
        self.batt_lbl.pack(anchor="w")
        row = ttk.Frame(self.sysf)
        row.pack(anchor="w", pady=6)
        ttk.Button(row, text="Check battery health", command=self.battery_report).pack(side="left", padx=2)
        ttk.Button(row, text="Open full report", command=self.open_battery_report).pack(side="left", padx=2)
        self.load_sysinfo()

    def load_sysinfo(self):
        self.sys_lbl.config(text="Loading...")

        def work():
            ok, out = run(["powershell", "-NoProfile", "-Command", PS_SYSINFO])
            try:
                d = json.loads(out.strip().splitlines()[-1])
            except Exception:
                d = None
            if d:
                up = time.time() - psutil.boot_time()
                text = "\n".join([
                    f"Model:  {d['model']}",
                    f"CPU:  {d['cpu']}  ({psutil.cpu_count(logical=False)} cores / {psutil.cpu_count()} threads)",
                    f"RAM:  {fmt_bytes(psutil.virtual_memory().total)}",
                    f"Graphics:  {d['gpudrv'].replace('; ', chr(10) + '    ')}",
                    f"Windows:  {d['os']}",
                    f"BIOS:  {d['bios']}",
                    f"Last boot:  {d['boot']}   (up {int(up // 3600)}h {int(up % 3600 // 60)}m)",
                    "Drives (health from Windows' storage data):",
                    "    " + str(d["disks"]).replace("; ", "\n    "),
                ])
            else:
                text = "Couldn't read system info (PowerShell may be blocked)."
            self.ui_queue.append(lambda: self.sys_lbl.config(text=text))
        threading.Thread(target=work, daemon=True).start()

    def battery_path(self):
        return os.path.join(tempfile.gettempdir(), "battery-report.html")

    def battery_report(self):
        self.batt_lbl.config(text="Measuring...")
        path = self.battery_path()

        def work():
            ok, out = run(["powercfg", "/batteryreport", "/output", path])
            msg = "Couldn't create a battery report (this PC may have no battery, or try Administrator)."
            if ok and os.path.exists(path):
                data = open(path, "rb").read()
                html = data.decode("utf-16" if data[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8", "ignore")
                d = re.search(r"DESIGN CAPACITY.*?([\d,]+)\s*mWh", html, re.S | re.I)
                f = re.search(r"FULL CHARGE CAPACITY.*?([\d,]+)\s*mWh", html, re.S | re.I)
                if d and f:
                    dv, fv = int(d.group(1).replace(",", "")), int(f.group(1).replace(",", ""))
                    pct = fv / dv * 100 if dv else 0
                    verdict = ("Healthy." if pct >= 80 else "Worn: expect shorter battery life." if pct >= 60
                               else "Badly worn: a replacement battery is worth considering.")
                    msg = (f"Built to hold {dv:,} mWh, now holds {fv:,} mWh = {pct:.0f}% of original. {verdict}")
                else:
                    msg = "Report created. Click 'Open full report' to read it."
            self.ui_queue.append(lambda: self.batt_lbl.config(text=msg))
        threading.Thread(target=work, daemon=True).start()

    def open_battery_report(self):
        path = self.battery_path()
        if os.path.exists(path):
            os.startfile(path)
        else:
            messagebox.showinfo("Battery", "Run 'Check battery health' first.")

    # ----- History, undo and restore points -----
    def build_history(self):
        bold = ("Segoe UI", 10, "bold")
        ttk.Label(self.hist, text="Safety net", font=bold).pack(anchor="w")
        ttk.Label(self.hist, text="A restore point lets you roll Windows back (search 'Create a restore point' "
                  "in Start) if a tweak causes trouble. Windows normally allows one per 24 hours.",
                  wraplength=700, justify="left").pack(anchor="w", pady=4)
        self.rp_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(self.hist, text="Make a restore point before Moderate/Advanced tweaks (once per session)",
                        variable=self.rp_var).pack(anchor="w")
        ttk.Button(self.hist, text="Create restore point now", command=lambda: self.make_restore_point(True)).pack(
            anchor="w", pady=6)

        ttk.Label(self.hist, text="Change history", font=bold).pack(anchor="w", pady=(14, 4))
        self.hist_tree = ttk.Treeview(self.hist, columns=("time", "name", "status"), show="headings", height=12)
        for col, txt, w in (("time", "When", 130), ("name", "Change", 330), ("status", "Status", 120)):
            self.hist_tree.heading(col, text=txt)
            self.hist_tree.column(col, width=w)
        self.hist_tree.pack(fill="x")
        row = ttk.Frame(self.hist)
        row.pack(anchor="w", pady=6)
        ttk.Button(row, text="Undo selected", command=self.undo_selected).pack(side="left", padx=2)
        ttk.Button(row, text="Undo last change", command=self.undo_last).pack(side="left", padx=2)
        self.refresh_history()

    def refresh_history(self):
        self.hist_tree.delete(*self.hist_tree.get_children())
        for i, e in enumerate(self.hist_data):
            status = "undone" if e.get("undone") else ("applied" if e.get("undo") else "can't undo")
            self.hist_tree.insert("", 0, iid=str(i), values=(e["time"], e["name"], status))

    def log(self, name, undo):
        self.hist_data.append({"time": time.strftime("%Y-%m-%d %H:%M"), "name": name, "undo": undo, "undone": False})
        del self.hist_data[:-100]
        save_hist(self.hist_data)
        self.refresh_history()

    def _undo_index(self, i):
        e = self.hist_data[i]
        if e.get("undone"):
            messagebox.showinfo("Undo", "That change was already undone.")
        elif not e.get("undo"):
            messagebox.showinfo("Undo", "This change can't be undone automatically.")
        elif run_undo(e["undo"]):
            e["undone"] = True
            save_hist(self.hist_data)
            self.refresh_history()
            self.refresh_plan()
            messagebox.showinfo("Undo", f"Undone: {e['name']}.")
        else:
            messagebox.showwarning("Undo", "Couldn't undo that (try running as Administrator).")

    def undo_selected(self):
        sel = self.hist_tree.selection()
        if sel:
            self._undo_index(int(sel[0]))
        else:
            messagebox.showinfo("Undo", "Select a change in the list first.")

    def undo_last(self):
        for i in range(len(self.hist_data) - 1, -1, -1):
            if self.hist_data[i].get("undo") and not self.hist_data[i].get("undone"):
                return self._undo_index(i)
        messagebox.showinfo("Undo", "Nothing to undo.")

    def make_restore_point(self, manual=False):
        self.config(cursor="watch")
        self.update_idletasks()
        ok, out = run(["powershell", "-NoProfile", "-Command", RP_CMD])
        self.config(cursor="")
        if ok:
            self.rp_done = True
        if manual:
            if ok:
                messagebox.showinfo("Restore point", "Restore point requested. (Windows skips it if one was already "
                                    "made in the last 24 hours.)")
            else:
                messagebox.showwarning("Restore point", "Couldn't create a restore point. Make sure System "
                                       "Protection is on for drive C: and run as Administrator.\n\n" + out[:300])
        return ok


if __name__ == "__main__":
    if os.name == "nt" and not is_admin() and "--no-admin" not in sys.argv:
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable,
                                                 f'"{os.path.abspath(__file__)}"', None, 1)
        if rc > 32:  # UAC accepted, elevated copy is starting
            sys.exit()
    App().mainloop()
