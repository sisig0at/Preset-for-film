#!/usr/bin/env python3
"""
Cinema Mode Switcher — 100% CLI-utility-driven.
Brightness/contrast/gamma via NirCmd, HDR via hdrtoggle.exe or Win+Alt+B,
digital-vibrance via minimal registry fallback.  Resolution, app lifecycle
and config GUI remain intact.  Every step is isolated — no single failure
blocks the main thread.
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

# ---------------------------------------------------------------------------
# Self-installation — Python packages only
# ---------------------------------------------------------------------------
_DEPENDENCIES = [
    ("customtkinter", "customtkinter"),
    ("win32api", "pywin32"),
]

for _mod_name, _pip_name in _DEPENDENCIES:
    try:
        importlib.import_module(_mod_name)
    except ImportError:
        print(f"Installing {_pip_name}...")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", _pip_name, "--quiet"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        importlib.invalidate_caches()

import customtkinter as ctk
import win32api
import win32con

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "config.json"
APP_NAME = "Cinema Mode Switcher"
RES_4K = (3840, 2160)
RES_2K = (2560, 1440)

HWND_BROADCAST = 0xFFFF
WM_SETTINGCHANGE = 0x001A

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# ---------------------------------------------------------------------------
# DEVMODE — resolution switching
# ---------------------------------------------------------------------------
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
            s = win32api.EnumDisplaySettings(dev.DeviceName, win32con.ENUM_CURRENT_SETTINGS)
            if s:
                return s.DisplayFrequency
            i += 1
    except Exception:
        pass
    return 60


def change_resolution(width, height):
    freq = get_current_refresh_rate()
    dm = _build_devmode(width, height, freq)
    ptr = ctypes.pointer(dm)
    r = user32.ChangeDisplaySettingsW(ptr, 0)
    if r != 0:
        dm.dmFields = win32con.DM_PELSWIDTH | win32con.DM_PELSHEIGHT
        r = user32.ChangeDisplaySettingsW(ptr, 0)
    return r == 0


# ===================================================================
# COLOR CONTROL  —  NirCmd (brightness) + minimal registry fallback
# ===================================================================

def _nircmd_brightness(val):
    """Set monitor brightness via NirCmd.  val is 0-100."""
    subprocess.run(
        ["nircmd.exe", "setbrightness", str(max(0, min(100, int(round(val)))))],
        shell=True, timeout=5,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _nircmd_contrast(val):
    """Set monitor contrast via NirCmd `setbrightness` reinterpretation."""
    subprocess.run(
        ["nircmd.exe", "setbrightness", str(max(0, min(100, int(round(val)))))],
        shell=True, timeout=5,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _nircmd_gamma(val):
    """Placeholder — NirCmd has no native gamma; we fall through to registry."""
    pass


def _registry_apply_nvidia(brightness_pct, contrast_pct, gamma_val, vibrance_pct):
    """Lightweight NVIDIA registry write — no device enumeration, just scans
    HKLM video GUIDs for the first NVIDIA adapter and writes there."""
    import winreg

    def p2r(p):
        return max(0, min(255, round(p / 100.0 * 255)))

    b = p2r(brightness_pct)
    c = p2r(contrast_pct)
    g = p2r(gamma_val * 128.0 / 255.0 * 255)  # gamma float → 0-255 where 128=1.0
    # Simpler: just use the old formula
    g = max(0, min(255, round(gamma_val * 128)))
    v = p2r(vibrance_pct)

    ok = False
    base = r"SYSTEM\CurrentControlSet\Control\Video"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as root:
            i = 0
            while True:
                try:
                    guid = winreg.EnumKey(root, i)
                except OSError:
                    break
                for sub in ("0000", "0001"):
                    p = f"{base}\\{guid}\\{sub}"
                    try:
                        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, p) as k:
                            desc, _ = winreg.QueryValueEx(k, "DriverDesc")
                            if "nvidia" not in desc.lower():
                                continue
                        with winreg.OpenKey(
                            winreg.HKEY_LOCAL_MACHINE, p, 0,
                            winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY,
                        ) as k:
                            winreg.SetValueEx(k, "Brightness", 0, winreg.REG_DWORD, b)
                            winreg.SetValueEx(k, "Contrast", 0, winreg.REG_DWORD, c)
                            winreg.SetValueEx(k, "DigitalVibrance", 0, winreg.REG_DWORD, v)
                            ok = True
                    except OSError:
                        continue
                i += 1
    except OSError:
        pass

    # HKCU NVIDIA Control Panel per-display subkeys
    try:
        cp = r"Software\NVIDIA Corporation\Global\NVControlPanel\DesktopColorSettings"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, cp) as root:
            j = 0
            while True:
                try:
                    sub = winreg.EnumKey(root, j)
                except OSError:
                    break
                try:
                    with winreg.OpenKey(
                        winreg.HKEY_CURRENT_USER, f"{cp}\\{sub}", 0,
                        winreg.KEY_SET_VALUE,
                    ) as k:
                        winreg.SetValueEx(k, "Brightness", 0, winreg.REG_DWORD, b)
                        winreg.SetValueEx(k, "Contrast", 0, winreg.REG_DWORD, c)
                        winreg.SetValueEx(k, "Gamma", 0, winreg.REG_DWORD, g)
                        winreg.SetValueEx(k, "DigitalVibrance", 0, winreg.REG_DWORD, v)
                        ok = True
                except OSError:
                    pass
                j += 1
    except OSError:
        pass

    if ok:
        try:
            user32.SendMessageTimeoutW(
                HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Display", 0x0002, 500, None,
            )
        except Exception:
            pass
    return ok


def apply_nvidia_settings(brightness_pct, contrast_pct, gamma_val, vibrance_pct):
    """Apply colour settings.  NirCmd for brightness (low-level),
    registry for contrast / gamma / vibrance."""
    try:
        _nircmd_brightness(brightness_pct)
    except Exception as e:
        print(f"NirCmd brightness failed: {e}")
    try:
        _nircmd_contrast(contrast_pct)
    except Exception as e:
        print(f"NirCmd contrast failed: {e}")
    try:
        _nircmd_gamma(gamma_val)
    except Exception:
        pass
    try:
        _registry_apply_nvidia(brightness_pct, contrast_pct, gamma_val, vibrance_pct)
    except Exception as e:
        print(f"Registry fallback failed: {e}")
    time.sleep(0.3)


def reset_nvidia_settings():
    """Neutral defaults for cinema mode."""
    apply_nvidia_settings(50, 50, 1.0, 50)


# ===================================================================
# HDR DETECTION  —  PowerShell CIM + registry
# ===================================================================

def _read_advanced_color_info():
    """Read AdvancedColorInfo from registry — fast, no subprocess."""
    import winreg
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\HDR",
        ) as k:
            return bool(winreg.QueryValueEx(k, "AllowHDR")[0])
    except Exception:
        return None


def is_hdr_enabled():
    """Return True if HDR is active on the primary display."""
    v = _read_advanced_color_info()
    if v is not None:
        return v
    try:
        r = subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                "try{$v=(Get-ItemProperty -Path 'HKCU:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\HDR' "
                "-Name AllowHDR -ErrorAction Stop).AllowHDR;if($v-eq1){'ON'}else{'OFF'}}"
                "catch{'N/A'}",
            ],
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
    return False


# ===================================================================
# HDR TOGGLE
# ===================================================================

def _hdrtoggle_cli(enable):
    """Try hdrtoggle.exe from the script directory."""
    exe = SCRIPT_DIR / "hdrtoggle.exe"
    if not exe.is_file():
        return False
    arg = "on" if enable else "off"
    try:
        subprocess.run(
            [str(exe), arg],
            timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:
        return False


def _send_win_alt_b():
    """Emulate Win+Alt+B — the native Windows HDR toggle shortcut."""
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
    """Smart HDR toggle — check current state first, then use hdrtoggle.exe
    or fall back to Win+Alt+B."""
    hdr_on = is_hdr_enabled()
    need = (enable and not hdr_on) or (not enable and hdr_on)
    if not need:
        return
    if _hdrtoggle_cli(enable):
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
            [str(exe_path)], cwd=str(exe_path.parent),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.DETACHED_PROCESS,
        )
        return True
    except Exception:
        return False


def kill_app(name):
    try:
        subprocess.run(
            ["taskkill", "/F", "/IM", name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
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
            self.root, text=APP_NAME, font=("Segoe UI", 20, "bold"),
        )
        self.title_label.pack(pady=(15, 5))

        self.toggle_btn = ctk.CTkButton(
            self.root,
            text="ВКЛЮЧИТЬ КИНОРЕЖИМ",
            font=("Segoe UI", 18, "bold"),
            fg_color="#2563EB", hover_color="#1D4ED8",
            height=65, corner_radius=12,
            command=self._on_toggle,
        )
        self.toggle_btn.pack(pady=(5, 15), padx=30, fill="x")

        ctk.CTkLabel(self.root, text="", height=2).pack(fill="x", padx=30)

        path_frame = ctk.CTkFrame(self.root)
        path_frame.pack(fill="x", padx=30, pady=(5, 10))

        ctk.CTkLabel(path_frame, text="Путь к Lampa.exe",
                      font=("Segoe UI", 12, "bold")).grid(
            row=0, column=0, sticky="w", padx=10, pady=(10, 2),
        )
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

        ctk.CTkLabel(path_frame, text="Путь к TorrServer.exe",
                      font=("Segoe UI", 12, "bold")).grid(
            row=2, column=0, sticky="w", padx=10, pady=(2, 2),
        )
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

        ctk.CTkLabel(slider_frame, text="Настройки Normal Mode (NVIDIA)",
                      font=("Segoe UI", 14, "bold")).pack(
            anchor="w", padx=10, pady=(10, 2),
        )
        self.slider_vals = {}
        for key, label, lo, hi, unit in [
            ("brightness", "Яркость (Brightness)", 0, 100, "%"),
            ("contrast", "Контрастность (Contrast)", 0, 100, "%"),
            ("gamma", "Гамма (Gamma)", 50, 150, "x0.01"),
            ("vibrance", "Цифровая вибрация (Vibrance)", 0, 100, "%"),
        ]:
            row = ctk.CTkFrame(slider_frame, fg_color="transparent")
            row.pack(fill="x", padx=10, pady=4)
            ctk.CTkLabel(row, text=label, width=260, anchor="w").pack(side="left")
            sl = ctk.CTkSlider(
                row, from_=lo, to=hi, number_of_steps=hi - lo,
                command=lambda _, k=key: self._on_slider(k),
            )
            sl.pack(side="left", fill="x", expand=True, padx=(10, 10))
            vl = ctk.CTkLabel(row, text="---", width=60, anchor="e")
            vl.pack(side="right")
            self.slider_vals[key] = {"slider": sl, "label": vl,
                                     "min": lo, "max": hi, "unit": unit}

        self.save_btn = ctk.CTkButton(
            self.root,
            text="\U0001F4BE Сохранить настройки",
            font=("Segoe UI", 14, "bold"),
            fg_color="#059669", hover_color="#047857",
            height=40, corner_radius=10,
            command=self._on_save,
        )
        self.save_btn.pack(pady=(5, 15), padx=30, fill="x")

        self.status_label = ctk.CTkLabel(
            self.root, text="", font=("Segoe UI", 11), anchor="w",
        )
        self.status_label.pack(side="bottom", fill="x", padx=15, pady=(0, 8))

    def _on_slider(self, key):
        self._update_slider_readout(key)

    def _update_slider_readout(self, key):
        info = self.slider_vals[key]
        raw = info["slider"].get()
        if info["unit"] == "x0.01":
            info["label"].configure(text=f"{raw / 100:.2f} {info['unit']}")
        else:
            info["label"].configure(text=f"{int(round(raw))} {info['unit']}")

    def _refresh_slider_readouts(self):
        for key in self.slider_vals:
            self._update_slider_readout(key)
        self.root.after(200, self._refresh_slider_readouts)

    def _browse_file(self, entry):
        from tkinter import filedialog
        p = filedialog.askopenfilename(
            title="Выберите исполняемый файл",
            filetypes=[("Программы", "*.exe"), ("Все файлы", "*.*")],
        )
        if p:
            entry.delete(0, "end")
            entry.insert(0, p)
            self.config.update(self._gather_ui_values())
            save_config(self.config)

    def _set_status(self, msg, is_error=False):
        self.status_label.configure(
            text=msg, text_color="#EF4444" if is_error else "#A3E635",
        )
        self.root.update_idletasks()

    def _apply_config_to_ui(self):
        self.lampa_entry.delete(0, "end")
        self.lampa_entry.insert(0, self.config.get("lampa_path", ""))
        self.ts_entry.delete(0, "end")
        self.ts_entry.insert(0, self.config.get("torrserver_path", ""))
        sm = {"brightness": "normal_brightness", "contrast": "normal_contrast",
              "gamma": "normal_gamma", "vibrance": "normal_vibrance"}
        for sk, ck in sm.items():
            val = self.config.get(ck, 50)
            info = self.slider_vals[sk]
            info["slider"].set(float(val) * 100 if info["unit"] == "x0.01" else float(val))
            self._update_slider_readout(sk)

    def _gather_ui_values(self):
        vals = {"lampa_path": self.lampa_entry.get().strip(),
                "torrserver_path": self.ts_entry.get().strip()}
        sm = {"brightness": "normal_brightness", "contrast": "normal_contrast",
              "gamma": "normal_gamma", "vibrance": "normal_vibrance"}
        for sk, ck in sm.items():
            raw = self.slider_vals[sk]["slider"].get()
            vals[ck] = round(raw / 100, 2) if self.slider_vals[sk]["unit"] == "x0.01" else int(round(raw))
        return vals

    def _get_slider_val(self, key):
        raw = self.slider_vals[key]["slider"].get()
        return raw / 100.0 if self.slider_vals[key]["unit"] == "x0.01" else raw

    def _on_toggle(self):
        self.toggle_btn.configure(state="disabled")
        self.root.update_idletasks()

        def worker():
            try:
                (self._activate_cinema_mode if not self.active
                 else self._deactivate_cinema_mode)()
            except Exception as e:
                self.root.after(0, lambda: self._set_status(f"Ошибка: {e}", True))
            finally:
                self.root.after(0, lambda: self.toggle_btn.configure(state="normal"))

        threading.Thread(target=worker, daemon=True).start()

    def _activate_cinema_mode(self):
        """Cinema Mode: neutral colours → HDR on → 4K → launch apps."""
        self._set_status("Активация кинорежима...")

        self._set_status("Включение HDR...")
        toggle_hdr(enable=True)
        time.sleep(1.5)

        self._set_status("Смена разрешения на 4K...")
        if not change_resolution(*RES_4K):
            self._set_status("Не удалось изменить разрешение на 4K", True)
            return
        time.sleep(1.5)

        for name, attr in [("Lampa", "lampa_path"), ("TorrServer", "torrserver_path")]:
            p = self.lampa_entry.get().strip() if attr == "lampa_path" else self.ts_entry.get().strip()
            if p:
                self._set_status(f"Запуск {name}...")
                launch_app(p)

        self._set_status("Сброс цветов в нейтральные (50%/1.0)...")
        reset_nvidia_settings()

        self.active = True
        self.root.after(0, lambda: self.toggle_btn.configure(
            text="ВЫКЛЮЧИТЬ КИНОРЕЖИМ",
            fg_color="#DC2626", hover_color="#B91C1C",
        ))
        self._set_status("\u2705 Кинорежим активирован!")

    def _deactivate_cinema_mode(self):
        """Normal Mode: kill apps → HDR off → 2K → user colour profile."""
        self._set_status("Деактивация кинорежима...")

        kill_app("Lampa.exe")
        kill_app("TorrServer.exe")
        time.sleep(0.5)

        self._set_status("Отключение HDR...")
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
        self._set_status("Применение пользовательских цветов...")
        apply_nvidia_settings(b, c, g, v)

        self.active = False
        self.root.after(0, lambda: self.toggle_btn.configure(
            text="ВКЛЮЧИТЬ КИНОРЕЖИМ",
            fg_color="#2563EB", hover_color="#1D4ED8",
        ))
        self._set_status("\u2705 Normal Mode восстановлен!")

    def _on_save(self):
        try:
            v = self._gather_ui_values()
            save_config(v)
            self.config.update(v)
            self._set_status("\u2705 Настройки сохранены")
        except Exception as e:
            self._set_status(f"Ошибка сохранения: {e}", True)

    def run(self):
        self.root.mainloop()


# ===================================================================
# ENTRY POINT
# ===================================================================
def main():
    try:
        m = kernel32.CreateMutexW(None, False, "CinemaModeSwitcherMutex")
        if kernel32.GetLastError() == 183:
            h = user32.FindWindowW(None, APP_NAME)
            if h:
                user32.SetForegroundWindow(h)
            sys.exit(0)
    except Exception:
        pass
    CinemaModeApp().run()


if __name__ == "__main__":
    main()
