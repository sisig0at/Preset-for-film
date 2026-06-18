#!/usr/bin/env python3
"""
Cinema Mode Switcher for Windows 11 + Samsung Odyssey G70B + NVIDIA GPU.
Hybrid CLI approach: ColorControl.exe for NVIDIA color, PowerShell CIM for
HDR detection, Win+Alt+B for HDR toggle. .NET 8 Desktop Runtime is
bootstrapped automatically if missing.
"""

import importlib
import os
import sys
import subprocess
import json
import time
import threading
import ctypes
from pathlib import Path

# ===================================================================
# SELF-INSTALLATION BLOCK
# ===================================================================

def _ensure_dotnet_runtime():
    """Auto-install .NET 8 Desktop Runtime if missing (used by ColorControl.exe)."""
    try:
        r = subprocess.run(
            ["dotnet", "--list-runtimes"],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if "Microsoft.WindowsDesktop.App" in r.stdout and " 8." in r.stdout:
            return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    try:
        r = subprocess.run(
            ["where", "dotnet"],
            capture_output=True, text=True, timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if r.returncode == 0:
            d = subprocess.run(
                ["dotnet", "--list-runtimes"],
                capture_output=True, text=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if "Microsoft.WindowsDesktop.App" in d.stdout and " 8." in d.stdout:
                return True
    except Exception:
        pass

    print("Bootstrapping .NET 8 Desktop Runtime (silent install)...")
    try:
        subprocess.run(
            [
                "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                "iex ((New-Object System.Net.WebClient).DownloadString('https://dot.net')); dotnet-install.ps1 -Runtime windowsdesktop -Version 8.0",
            ],
            timeout=300,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        dotnet_dir = os.path.join(os.environ.get("USERPROFILE", ""), ".dotnet")
        if os.path.isdir(dotnet_dir) and dotnet_dir not in os.environ.get("PATH", ""):
            os.environ["PATH"] = dotnet_dir + os.pathsep + os.environ.get("PATH", "")
        print(".NET 8 bootstrap complete.")
        return True
    except Exception as e:
        print(f"Warning: .NET install skipped ({e})")
        return False


_ensure_dotnet_runtime()

# Python package dependencies
_DEPENDENCIES = [
    ("customtkinter", "customtkinter"),
    ("pyautogui", "pyautogui"),
    ("win32api", "pywin32"),
]

for _mod_name, _pip_name in _DEPENDENCIES:
    try:
        importlib.import_module(_mod_name)
    except ImportError:
        print(f"Installing {_pip_name}...")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", _pip_name, "--quiet"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        importlib.invalidate_caches()

# ---------------------------------------------------------------------------
import customtkinter as ctk
import win32api
import win32con
import win32gui

# ===================================================================
# CONSTANTS
# ===================================================================

CONFIG_FILE = Path(__file__).resolve().parent / "config.json"
APP_NAME = "Cinema Mode Switcher"
COLORCTRL_EXE = Path(__file__).resolve().parent / "ColorControl.exe"
RES_4K = (3840, 2160)
RES_2K = (2560, 1440)

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# ===================================================================
# DEVMODE — resolution switching
# ===================================================================

class DEVMODEW(ctypes.Structure):
    _fields_ = [
        ("dmDeviceName", ctypes.c_wchar * 32),
        ("dmSpecVersion", ctypes.c_uint16),
        ("dmDriverVersion", ctypes.c_uint16),
        ("dmSize", ctypes.c_uint16),
        ("dmDriverExtra", ctypes.c_uint16),
        ("dmFields", ctypes.c_uint32),
        ("dmOrientation", ctypes.c_int16),
        ("dmPaperSize", ctypes.c_int16),
        ("dmPaperLength", ctypes.c_int16),
        ("dmPaperWidth", ctypes.c_int16),
        ("dmScale", ctypes.c_int16),
        ("dmCopies", ctypes.c_int16),
        ("dmDefaultSource", ctypes.c_int16),
        ("dmPrintQuality", ctypes.c_int16),
        ("dmColor", ctypes.c_int16),
        ("dmDuplex", ctypes.c_int16),
        ("dmYResolution", ctypes.c_int16),
        ("dmTTOption", ctypes.c_int16),
        ("dmCollate", ctypes.c_int16),
        ("dmFormName", ctypes.c_wchar * 32),
        ("dmLogPixels", ctypes.c_uint16),
        ("dmBitsPerPel", ctypes.c_uint32),
        ("dmPelsWidth", ctypes.c_uint32),
        ("dmPelsHeight", ctypes.c_uint32),
        ("dmDisplayFlags", ctypes.c_uint32),
        ("dmDisplayFrequency", ctypes.c_uint32),
        ("dmICMMethod", ctypes.c_uint32),
        ("dmICMIntent", ctypes.c_uint32),
        ("dmMediaType", ctypes.c_uint32),
        ("dmDitherType", ctypes.c_uint32),
        ("dmReserved1", ctypes.c_uint32),
        ("dmReserved2", ctypes.c_uint32),
        ("dmPanningWidth", ctypes.c_uint32),
        ("dmPanningHeight", ctypes.c_uint32),
    ]


def _build_devmode(width, height, freq):
    dm = DEVMODEW()
    dm.dmSize = ctypes.sizeof(DEVMODEW)
    dm.dmFields = win32con.DM_PELSWIDTH | win32con.DM_PELSHEIGHT | win32con.DM_DISPLAYFREQUENCY
    dm.dmPelsWidth = width
    dm.dmPelsHeight = height
    dm.dmDisplayFrequency = freq
    return dm


def get_current_refresh_rate():
    try:
        i = 0
        while True:
            dev = win32api.EnumDisplayDevices(None, i, 0)
            settings = win32api.EnumDisplaySettings(dev.DeviceName, win32con.ENUM_CURRENT_SETTINGS)
            if settings:
                return settings.DisplayFrequency
            i += 1
    except Exception:
        pass
    return 60


def change_resolution(width, height):
    freq = get_current_refresh_rate()
    dm = _build_devmode(width, height, freq)
    ptr = ctypes.pointer(dm)
    result = user32.ChangeDisplaySettingsW(ptr, 0)
    if result != 0:
        dm.dmFields = win32con.DM_PELSWIDTH | win32con.DM_PELSHEIGHT
        result = user32.ChangeDisplaySettingsW(ptr, 0)
    return result == 0


# ===================================================================
# NVIDIA COLOR CONTROL  (ColorControl.exe primary, registry fallback)
# ===================================================================

def _colorcontrol_apply(brightness_pct, contrast_pct, gamma_val, vibrance_pct):
    """Apply colour settings via ColorControl.exe CLI."""
    if not COLORCTRL_EXE.is_file():
        return False

    gamma_raw = max(0, min(100, int(round(gamma_val * 50))))
    b = max(0, min(100, int(round(brightness_pct))))
    c = max(0, min(100, int(round(contrast_pct))))
    v = max(0, min(100, int(round(vibrance_pct))))

    try:
        subprocess.run(
            [
                str(COLORCTRL_EXE),
                "-vibrance", str(v),
                "-brightness", str(b),
                "-contrast", str(c),
                "-gamma", str(gamma_raw),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
        return True
    except Exception:
        return False


def _registry_apply(brightness_pct, contrast_pct, gamma_val, vibrance_pct):
    """Fallback: write NVIDIA colour values directly to the registry."""
    import winreg

    def pct_to_reg(pct):
        return max(0, min(255, round(max(0, min(100, pct)) / 100.0 * 255)))

    def gamma_to_reg(g):
        return max(0, min(255, round(g * 128)))

    b_val = pct_to_reg(brightness_pct)
    c_val = pct_to_reg(contrast_pct)
    g_val = gamma_to_reg(gamma_val)
    v_val = pct_to_reg(vibrance_pct)

    keys_written = False

    # HKLM driver-level
    try:
        base = r"SYSTEM\CurrentControlSet\Control\Video"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as root:
            g = 0
            while True:
                try:
                    guid = winreg.EnumKey(root, g)
                except OSError:
                    break
                try:
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, f"{base}\\{guid}\\0000") as sk:
                        try:
                            desc, _ = winreg.QueryValueEx(sk, "DriverDesc")
                        except FileNotFoundError:
                            g += 1
                            continue
                        if "nvidia" not in desc.lower():
                            g += 1
                            continue
                    with winreg.OpenKey(
                        winreg.HKEY_LOCAL_MACHINE, f"{base}\\{guid}\\0000",
                        0, winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY,
                    ) as k:
                        winreg.SetValueEx(k, "Brightness", 0, winreg.REG_DWORD, b_val)
                        winreg.SetValueEx(k, "Contrast", 0, winreg.REG_DWORD, c_val)
                        winreg.SetValueEx(k, "DigitalVibrance", 0, winreg.REG_DWORD, v_val)
                        keys_written = True
                except OSError:
                    pass
                g += 1
    except OSError:
        pass

    # HKCU control panel
    try:
        cp_base = r"Software\NVIDIA Corporation\Global\NVControlPanel\DesktopColorSettings"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, cp_base) as root:
            h = 0
            while True:
                try:
                    sub = winreg.EnumKey(root, h)
                except OSError:
                    break
                try:
                    with winreg.OpenKey(
                        winreg.HKEY_CURRENT_USER, f"{cp_base}\\{sub}",
                        0, winreg.KEY_SET_VALUE,
                    ) as k:
                        winreg.SetValueEx(k, "Brightness", 0, winreg.REG_DWORD, b_val)
                        winreg.SetValueEx(k, "Contrast", 0, winreg.REG_DWORD, c_val)
                        winreg.SetValueEx(k, "Gamma", 0, winreg.REG_DWORD, g_val)
                        winreg.SetValueEx(k, "DigitalVibrance", 0, winreg.REG_DWORD, v_val)
                        keys_written = True
                except OSError:
                    pass
                h += 1
    except OSError:
        pass

    if keys_written:
        try:
            user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Display", 0x0002, 500, None)
        except Exception:
            pass

    return keys_written


def apply_nvidia_settings(brightness_pct, contrast_pct, gamma_val, vibrance_pct):
    """Apply NVIDIA color settings (ColorControl.exe → registry fallback)."""
    if _colorcontrol_apply(brightness_pct, contrast_pct, gamma_val, vibrance_pct):
        time.sleep(0.3)
        return
    _registry_apply(brightness_pct, contrast_pct, gamma_val, vibrance_pct)
    time.sleep(0.3)


def reset_nvidia_settings():
    """Reset to neutral defaults (50%, 50%, 1.0, 50%) for cinema mode."""
    apply_nvidia_settings(50, 50, 1.0, 50)


# ===================================================================
# HDR DETECTION  (PowerShell CIM + registry)
# ===================================================================

def _powershell_hdr_status():
    """
    Query real HDR state via PowerShell.
    Returns True/False, or None if undetermined.
    """
    script = (
        "$hdr=$false; "
        "try{$v=(Get-ItemProperty -Path 'HKCU:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\HDR' "
        "-Name AllowHDR -ErrorAction Stop).AllowHDR; $hdr=($v -eq 1)}catch{}; "
        "try{$m=Get-CimInstance -Namespace 'root/wmi' -ClassName "
        "'WmiMonitorBasicDisplayParams' -ErrorAction Stop; "
        "if($null -eq $m -or $m.Count -eq 0){'N/A';exit}}catch{}; "
        "if($hdr){'ON'}else{'OFF'}"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=8,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        out = r.stdout.strip()
        if out == "ON":
            return True
        if out == "OFF":
            return False
    except Exception:
        pass
    return None


def is_hdr_enabled():
    """Return True if HDR is active on the primary display."""
    result = _powershell_hdr_status()
    if result is not None:
        return result
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"SOFTWARE\Microsoft\Windows\CurrentVersion\HDR") as k:
            return bool(winreg.QueryValueEx(k, "AllowHDR")[0])
    except Exception:
        return False


# ===================================================================
# HDR TOGGLE
# ===================================================================

def _send_win_alt_b():
    """Emulate Win+Alt+B to toggle HDR."""
    VK_LWIN, VK_LMENU, VK_B = 0x5B, 0xA4, 0x42

    def press(vk):
        user32.keybd_event(vk, 0, 0, 0)

    def release(vk):
        user32.keybd_event(vk, 0, 2, 0)

    press(VK_LWIN)
    time.sleep(0.15)
    press(VK_LMENU)
    time.sleep(0.15)
    press(VK_B)
    time.sleep(0.25)
    release(VK_B)
    time.sleep(0.15)
    release(VK_LMENU)
    time.sleep(0.15)
    release(VK_LWIN)
    time.sleep(2.0)


def toggle_hdr(enable=True):
    """Smart HDR toggle — checks current state before sending Win+Alt+B."""
    hdr_on = is_hdr_enabled()
    need = (enable and not hdr_on) or (not enable and hdr_on)
    if not need:
        return
    _send_win_alt_b()


# ===================================================================
# APP LIFECYCLE
# ===================================================================

def launch_app(path):
    if not path or not os.path.isfile(path):
        return False
    try:
        exe_path = Path(path)
        subprocess.Popen(
            [str(exe_path)],
            cwd=str(exe_path.parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.DETACHED_PROCESS,
        )
        return True
    except Exception:
        return False


def kill_app(name):
    try:
        subprocess.run(
            ["taskkill", "/F", "/IM", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        return True
    except Exception:
        return False


# ===================================================================
# CONFIG PERSISTENCE
# ===================================================================

DEFAULT_CONFIG = {
    "lampa_path": "",
    "torrserver_path": "",
    "normal_brightness": 45,
    "normal_contrast": 70,
    "normal_gamma": 0.95,
    "normal_vibrance": 66,
}


def load_config():
    try:
        if CONFIG_FILE.exists():
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            merged = dict(DEFAULT_CONFIG)
            merged.update(data)
            return merged
    except Exception:
        pass
    return dict(DEFAULT_CONFIG)


def save_config(data):
    merged = dict(DEFAULT_CONFIG)
    merged.update(data)
    tmp = CONFIG_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CONFIG_FILE)


# ===================================================================
# GUI
# ===================================================================

class CinemaModeApp:
    def __init__(self):
        self.config = load_config()
        self.active = False

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.root = ctk.CTk()
        self.root.title(APP_NAME)
        self.root.geometry("700x750")
        self.root.resizable(False, False)

        self.root.attributes("-topmost", True)
        self.root.after(500, lambda: self.root.attributes("-topmost", False))

        self._build_ui()
        self._apply_config_to_ui()
        self.root.after(100, self._refresh_slider_readouts)

    def _build_ui(self):
        self.title_label = ctk.CTkLabel(
            self.root, text=APP_NAME, font=("Segoe UI", 20, "bold")
        )
        self.title_label.pack(pady=(15, 5))

        self.toggle_btn = ctk.CTkButton(
            self.root,
            text="ВКЛЮЧИТЬ КИНОРЕЖИМ",
            font=("Segoe UI", 18, "bold"),
            fg_color="#2563EB",
            hover_color="#1D4ED8",
            height=65,
            corner_radius=12,
            command=self._on_toggle,
        )
        self.toggle_btn.pack(pady=(5, 15), padx=30, fill="x")

        ctk.CTkLabel(self.root, text="", height=2).pack(fill="x", padx=30)

        path_frame = ctk.CTkFrame(self.root)
        path_frame.pack(fill="x", padx=30, pady=(5, 10))

        ctk.CTkLabel(
            path_frame, text="Путь к Lampa.exe", font=("Segoe UI", 12, "bold")
        ).grid(row=0, column=0, sticky="w", padx=10, pady=(10, 2))

        lampa_row = ctk.CTkFrame(path_frame, fg_color="transparent")
        lampa_row.grid(row=1, column=0, columnspan=3, sticky="ew", padx=10, pady=(0, 8))
        lampa_row.grid_columnconfigure(0, weight=1)
        self.lampa_entry = ctk.CTkEntry(lampa_row, placeholder_text="Выберите Lampa.exe...")
        self.lampa_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.lampa_btn = ctk.CTkButton(
            lampa_row, text="Обзор", width=80,
            command=lambda: self._browse_file(self.lampa_entry),
        )
        self.lampa_btn.grid(row=0, column=1)

        ctk.CTkLabel(
            path_frame, text="Путь к TorrServer.exe", font=("Segoe UI", 12, "bold")
        ).grid(row=2, column=0, sticky="w", padx=10, pady=(2, 2))

        ts_row = ctk.CTkFrame(path_frame, fg_color="transparent")
        ts_row.grid(row=3, column=0, columnspan=3, sticky="ew", padx=10, pady=(0, 10))
        ts_row.grid_columnconfigure(0, weight=1)
        self.ts_entry = ctk.CTkEntry(ts_row, placeholder_text="Выберите TorrServer.exe...")
        self.ts_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.ts_btn = ctk.CTkButton(
            ts_row, text="Обзор", width=80,
            command=lambda: self._browse_file(self.ts_entry),
        )
        self.ts_btn.grid(row=0, column=1)

        slider_frame = ctk.CTkFrame(self.root)
        slider_frame.pack(fill="both", expand=True, padx=30, pady=(5, 10))

        ctk.CTkLabel(
            slider_frame,
            text="Настройки Normal Mode (NVIDIA)",
            font=("Segoe UI", 14, "bold"),
        ).pack(anchor="w", padx=10, pady=(10, 2))

        self.slider_vals = {}

        sliders_cfg = [
            ("brightness", "Яркость (Brightness)", 0, 100, "%"),
            ("contrast", "Контрастность (Contrast)", 0, 100, "%"),
            ("gamma", "Гамма (Gamma)", 50, 150, "x0.01"),
            ("vibrance", "Цифровая вибрация (Vibrance)", 0, 100, "%"),
        ]

        for key, label, lo, hi, unit in sliders_cfg:
            row_frame = ctk.CTkFrame(slider_frame, fg_color="transparent")
            row_frame.pack(fill="x", padx=10, pady=4)

            ctk.CTkLabel(row_frame, text=label, width=260, anchor="w").pack(side="left")

            slider = ctk.CTkSlider(
                row_frame, from_=lo, to=hi, number_of_steps=hi - lo,
                command=lambda _, k=key: self._on_slider(k),
            )
            slider.pack(side="left", fill="x", expand=True, padx=(10, 10))

            val_label = ctk.CTkLabel(row_frame, text="---", width=60, anchor="e")
            val_label.pack(side="right")

            self.slider_vals[key] = {
                "slider": slider,
                "label": val_label,
                "min": lo,
                "max": hi,
                "unit": unit,
            }

        self.save_btn = ctk.CTkButton(
            self.root,
            text="\U0001F4BE Сохранить настройки",
            font=("Segoe UI", 14, "bold"),
            fg_color="#059669",
            hover_color="#047857",
            height=40,
            corner_radius=10,
            command=self._on_save,
        )
        self.save_btn.pack(pady=(5, 15), padx=30, fill="x")

        self.status_label = ctk.CTkLabel(
            self.root, text="", font=("Segoe UI", 11), anchor="w"
        )
        self.status_label.pack(side="bottom", fill="x", padx=15, pady=(0, 8))

    def _on_slider(self, key):
        self._update_slider_readout(key)

    def _update_slider_readout(self, key):
        info = self.slider_vals[key]
        raw = info["slider"].get()
        unit = info["unit"]
        if unit == "x0.01":
            display = f"{raw / 100:.2f}"
        else:
            display = str(int(round(raw)))
        info["label"].configure(text=f"{display} {unit}")

    def _refresh_slider_readouts(self):
        for key in self.slider_vals:
            self._update_slider_readout(key)
        self.root.after(200, self._refresh_slider_readouts)

    def _browse_file(self, entry):
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            title="Выберите исполняемый файл",
            filetypes=[("Программы", "*.exe"), ("Все файлы", "*.*")],
        )
        if path:
            entry.delete(0, "end")
            entry.insert(0, path)
            self.config.update(self._gather_ui_values())
            save_config(self.config)

    def _set_status(self, msg, is_error=False):
        color = "#EF4444" if is_error else "#A3E635"
        self.status_label.configure(text=msg, text_color=color)
        self.root.update_idletasks()

    def _apply_config_to_ui(self):
        self.lampa_entry.delete(0, "end")
        self.lampa_entry.insert(0, self.config.get("lampa_path", ""))
        self.ts_entry.delete(0, "end")
        self.ts_entry.insert(0, self.config.get("torrserver_path", ""))

        slider_map = {
            "brightness": "normal_brightness",
            "contrast": "normal_contrast",
            "gamma": "normal_gamma",
            "vibrance": "normal_vibrance",
        }
        for sk, ck in slider_map.items():
            val = self.config.get(ck, 50)
            info = self.slider_vals[sk]
            if info["unit"] == "x0.01":
                info["slider"].set(float(val) * 100)
            else:
                info["slider"].set(float(val))
            self._update_slider_readout(sk)

    def _gather_ui_values(self):
        vals = {
            "lampa_path": self.lampa_entry.get().strip(),
            "torrserver_path": self.ts_entry.get().strip(),
        }
        slider_map = {
            "brightness": "normal_brightness",
            "contrast": "normal_contrast",
            "gamma": "normal_gamma",
            "vibrance": "normal_vibrance",
        }
        for sk, ck in slider_map.items():
            raw = self.slider_vals[sk]["slider"].get()
            if self.slider_vals[sk]["unit"] == "x0.01":
                vals[ck] = round(raw / 100, 2)
            else:
                vals[ck] = int(round(raw))
        return vals

    def _get_slider_val(self, key):
        raw = self.slider_vals[key]["slider"].get()
        if self.slider_vals[key]["unit"] == "x0.01":
            return raw / 100.0
        return raw

    def _on_toggle(self):
        self.toggle_btn.configure(state="disabled")
        self.root.update_idletasks()

        def worker():
            try:
                if not self.active:
                    self._activate_cinema_mode()
                else:
                    self._deactivate_cinema_mode()
            except Exception as e:
                err_msg = f"Ошибка: {e}"
                self.root.after(0, lambda: self._set_status(err_msg, True))
            finally:
                self.root.after(0, lambda: self.toggle_btn.configure(state="normal"))

        threading.Thread(target=worker, daemon=True).start()

    def _activate_cinema_mode(self):
        """Cinema Mode ON: neutral colors, HDR on, 4K, launch apps."""
        self._set_status("Активация кинорежима...")

        self._set_status("Проверка и включение HDR...")
        toggle_hdr(enable=True)
        time.sleep(1.5)

        self._set_status("Смена разрешения на 4K...")
        if not change_resolution(*RES_4K):
            self._set_status("Не удалось изменить разрешение на 4K", True)
            return
        time.sleep(1.5)

        lampa = self.lampa_entry.get().strip()
        torr = self.ts_entry.get().strip()
        if lampa:
            self._set_status("Запуск Lampa...")
            launch_app(lampa)
        if torr:
            self._set_status("Запуск TorrServer...")
            launch_app(torr)

        self._set_status("Сброс цветов NVIDIA (50% / 1.0 gamma)...")
        try:
            reset_nvidia_settings()
        except Exception as exc:
            print(f"Color reset error: {exc}")
            self._set_status("Предупреждение: сброс цветов не удался", True)
        time.sleep(0.5)

        self.active = True
        self.root.after(
            0,
            lambda: self.toggle_btn.configure(
                text="ВЫКЛЮЧИТЬ КИНОРЕЖИМ",
                fg_color="#DC2626",
                hover_color="#B91C1C",
            ),
        )
        self._set_status("\u2705 Кинорежим активирован!")

    def _deactivate_cinema_mode(self):
        """Normal Mode: kill apps, smart HDR off, 2K, user colour profile."""
        self._set_status("Деактивация кинорежима...")

        self._set_status("Остановка приложений...")
        kill_app("Lampa.exe")
        kill_app("TorrServer.exe")
        time.sleep(0.5)

        self._set_status("Проверка и отключение HDR...")
        toggle_hdr(enable=False)
        time.sleep(2.0)

        self._set_status("Смена разрешения на 2K...")
        if not change_resolution(*RES_2K):
            self._set_status("Не удалось изменить разрешение на 2K", True)
            return
        time.sleep(1.5)

        b = self._get_slider_val("brightness")
        c = self._get_slider_val("contrast")
        g = self._get_slider_val("gamma")
        v = self._get_slider_val("vibrance")
        self._set_status("Применение пользовательских цветов NVIDIA...")
        try:
            apply_nvidia_settings(b, c, g, v)
        except Exception as exc:
            print(f"Color apply error: {exc}")
            self._set_status("Предупреждение: настройка цветов не удалась", True)
        time.sleep(0.5)

        self.active = False
        self.root.after(
            0,
            lambda: self.toggle_btn.configure(
                text="ВКЛЮЧИТЬ КИНОРЕЖИМ",
                fg_color="#2563EB",
                hover_color="#1D4ED8",
            ),
        )
        self._set_status("\u2705 Normal Mode восстановлен!")

    def _on_save(self):
        vals = self._gather_ui_values()
        try:
            save_config(vals)
            self.config.update(vals)
            self._set_status("\u2705 Настройки сохранены в config.json")
        except Exception as e:
            self._set_status(f"Ошибка сохранения: {e}", True)

    def run(self):
        self.root.mainloop()


# ===================================================================
# ENTRY POINT
# ===================================================================

def main():
    try:
        mutex = kernel32.CreateMutexW(None, False, "CinemaModeSwitcherMutex")
        if kernel32.GetLastError() == 183:
            hwnd = user32.FindWindowW(None, APP_NAME)
            if hwnd:
                user32.SetForegroundWindow(hwnd)
            sys.exit(0)
    except Exception:
        pass

    app = CinemaModeApp()
    app.run()


if __name__ == "__main__":
    main()
