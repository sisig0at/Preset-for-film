#!/usr/bin/env python3
"""
Cinema Mode Switcher for Windows 11 + Samsung Odyssey G70B + NVIDIA GPU.
Supports 2K/4K resolution switching, NVIDIA color control via direct registry
access (no external tools), WinAPI-based HDR status detection and toggle
(Win+Alt+B), and app lifecycle (Lampa + TorrServer).

100% self-contained -- no ColorControl.exe or .NET dependencies.
"""

import importlib
import os
import sys
import subprocess
import json
import time
import threading
import ctypes
from ctypes import wintypes
from pathlib import Path

# ---------------------------------------------------------------------------
# Self-installation block -- runs before any third-party import
# ---------------------------------------------------------------------------
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
# Third-party imports (guaranteed available)
# ---------------------------------------------------------------------------
import customtkinter as ctk
import win32api
import win32con
import win32gui
import win32print

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CONFIG_FILE = Path(__file__).resolve().parent / "config.json"
APP_NAME = "Cinema Mode Switcher"

RES_4K = (3840, 2160)
RES_2K = (2560, 1440)

# ---------------------------------------------------------------------------
# Win32 / ctypes helpers
# ---------------------------------------------------------------------------
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

HWND_BROADCAST = 0xFFFF
WM_SETTINGCHANGE = 0x001A
SMTO_ABORTIFHUNG = 0x0002

# ---- DISPLAYCONFIG structures for HDR detection (Win10 2004+) ----
QDC_ONLY_ACTIVE_PATHS = 2
DISPLAYCONFIG_DEVICE_INFO_GET_ADVANCED_COLOR_INFO = 7


class LUID(ctypes.Structure):
    _fields_ = [
        ("LowPart", wintypes.DWORD),
        ("HighPart", wintypes.LONG),
    ]


class DISPLAYCONFIG_RATIONAL(ctypes.Structure):
    _fields_ = [
        ("Numerator", wintypes.DWORD),
        ("Denominator", wintypes.DWORD),
    ]


class DISPLAYCONFIG_PATH_SOURCE_INFO(ctypes.Structure):
    _fields_ = [
        ("adapterId", LUID),
        ("id", wintypes.DWORD),
        ("modeInfoIdx", wintypes.DWORD),
        ("cloneGroupId", wintypes.DWORD),
        ("sourceDeviceInfo", wintypes.DWORD),
    ]


class DISPLAYCONFIG_PATH_TARGET_INFO(ctypes.Structure):
    _fields_ = [
        ("adapterId", LUID),
        ("id", wintypes.DWORD),
        ("modeInfoIdx", wintypes.DWORD),
        ("outputTechnology", wintypes.DWORD),
        ("rotation", wintypes.DWORD),
        ("scaling", wintypes.DWORD),
        ("refreshRate", DISPLAYCONFIG_RATIONAL),
        ("scanLineOrdering", wintypes.DWORD),
        ("targetAvailable", wintypes.BOOL),
        ("statusFlags", wintypes.DWORD),
    ]


class DISPLAYCONFIG_PATH_INFO(ctypes.Structure):
    _fields_ = [
        ("sourceInfo", DISPLAYCONFIG_PATH_SOURCE_INFO),
        ("targetInfo", DISPLAYCONFIG_PATH_TARGET_INFO),
        ("flags", wintypes.DWORD),
    ]


class DISPLAYCONFIG_DEVICE_INFO_HEADER(ctypes.Structure):
    _fields_ = [
        ("type", wintypes.DWORD),
        ("size", wintypes.DWORD),
        ("adapterId", LUID),
        ("id", wintypes.DWORD),
    ]


class DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO(ctypes.Structure):
    _fields_ = [
        ("header", DISPLAYCONFIG_DEVICE_INFO_HEADER),
        ("value", wintypes.DWORD),
        ("colorEncoding", wintypes.DWORD),
        ("bitsPerChannel", wintypes.DWORD),
    ]


# Validate struct sizes at import time (matches MSVC x64/x86 layout)
_STRUCT_SIZES = {
    DISPLAYCONFIG_PATH_SOURCE_INFO: 24,
    DISPLAYCONFIG_PATH_TARGET_INFO: 48,
    DISPLAYCONFIG_PATH_INFO: 76,
    DISPLAYCONFIG_DEVICE_INFO_HEADER: 20,
    DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO: 32,
}
for _struct_cls, _expected in _STRUCT_SIZES.items():
    actual = ctypes.sizeof(_struct_cls)
    if actual != _expected:
        print(f"Warning: {_struct_cls.__name__} size {actual} != expected {_expected}")


# ---- DEVMODE structure for resolution switching ----
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


# ===================================================================
# RESOLUTION SWITCHING
# ===================================================================

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
# SMART HDR STATUS DETECTION (WinAPI + fallback)
# ===================================================================

def _fallback_is_hdr_enabled():
    """Fallback HDR detection via registry + PowerShell."""
    import winreg

    paths = [
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\HDR"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\HDR"),
    ]
    for hive, path in paths:
        try:
            with winreg.OpenKey(hive, path) as k:
                val, _ = winreg.QueryValueEx(k, "AllowHDR")
                return bool(val)
        except (FileNotFoundError, OSError):
            continue

    try:
        r = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "(Get-ItemProperty -Path 'HKCU:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\HDR' -Name AllowHDR -ErrorAction SilentlyContinue).AllowHDR",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip() == "1"
    except Exception:
        pass

    return False


def is_hdr_enabled():
    """
    Check the real-time HDR status of the primary display using the
    Windows DisplayConfig API (Win10 2004+).

    Uses QueryDisplayConfig to enumerate active paths, then calls
    DisplayConfigGetDeviceInfo with DISPLAYCONFIG_DEVICE_INFO_GET_ADVANCED_COLOR_INFO
    to read the advancedColorEnabled flag for each active target.

    Falls back to registry-based detection if the API is unavailable.
    """
    try:
        num_paths = wintypes.UINT32(0)
        num_modes = wintypes.UINT32(0)

        result = user32.GetDisplayConfigBufferSizes(
            QDC_ONLY_ACTIVE_PATHS,
            ctypes.byref(num_paths),
            ctypes.byref(num_modes),
        )
        if result != 0:
            return _fallback_is_hdr_enabled()

        path_array = (DISPLAYCONFIG_PATH_INFO * num_paths.value)()
        mode_array = (ctypes.c_byte * (64 * num_modes.value))()

        actual_paths = wintypes.UINT32(num_paths.value)
        actual_modes = wintypes.UINT32(num_modes.value)

        result = user32.QueryDisplayConfig(
            QDC_ONLY_ACTIVE_PATHS,
            ctypes.byref(actual_paths),
            path_array,
            ctypes.byref(actual_modes),
            mode_array,
            None,
        )
        if result != 0:
            return _fallback_is_hdr_enabled()

        for i in range(actual_paths.value):
            path = path_array[i]
            ac_info = DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO()
            ac_info.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_ADVANCED_COLOR_INFO
            ac_info.header.size = ctypes.sizeof(DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO)
            ac_info.header.adapterId = path.targetInfo.adapterId
            ac_info.header.id = path.targetInfo.id

            result = user32.DisplayConfigGetDeviceInfo(ctypes.byref(ac_info))
            if result == 0:
                if ac_info.value & 0x2:  # bit 1 = advancedColorEnabled
                    return True

        return False
    except Exception:
        return _fallback_is_hdr_enabled()


# ===================================================================
# NVIDIA COLOR CONTROL -- fully autonomous (direct registry + broadcast)
# ===================================================================

def _broadcast_display_change():
    """Notify the shell that display settings have changed."""
    try:
        user32.SendMessageTimeoutW(
            HWND_BROADCAST,
            WM_SETTINGCHANGE,
            0,
            "Display",
            SMTO_ABORTIFHUNG,
            500,
            None,
        )
    except Exception:
        pass


def _find_target_display_keys():
    """
    Locate the exact HKLM registry path(s) for the primary Samsung Odyssey G70B
    (or any attached NVIDIA display as fallback).

    Returns a list of paths relative to HKEY_LOCAL_MACHINE, e.g.:
        SYSTEM\\CurrentControlSet\\Control\\Video\\{GUID}\\0000
    """
    keys = []

    # Pass 1: find the primary monitor by checking StateFlags and device name
    i = 0
    while True:
        try:
            adapter = win32api.EnumDisplayDevices(None, i, 0)
        except Exception:
            break
        if not adapter.DeviceName:
            break

        is_primary = bool(getattr(adapter, "StateFlags", 0) & 0x00000001)

        j = 0
        while True:
            try:
                monitor = win32api.EnumDisplayDevices(adapter.DeviceName, j, 0)
            except Exception:
                break
            if not monitor.DeviceString:
                break

            name = monitor.DeviceString.lower()
            is_target = ("odyssey" in name or "g70" in name)

            if is_target or (is_primary and is_target):
                raw = adapter.DeviceKey
                pfx = "\\Registry\\Machine\\"
                if raw.startswith(pfx):
                    raw = raw[len(pfx):]
                raw = raw.rstrip("\\")
                if raw:
                    keys.append(raw)
                break
            j += 1

        i += 1

    if keys:
        return keys

    # Pass 2: fallback -- scan all GUID subkeys for any NVIDIA display adapter
    import winreg

    def _val(key, name):
        try:
            v, _ = winreg.QueryValueEx(key, name)
            return v
        except FileNotFoundError:
            return None

    base = r"SYSTEM\CurrentControlSet\Control\Video"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as root:
            g = 0
            while True:
                try:
                    guid = winreg.EnumKey(root, g)
                except OSError:
                    break
                gp = f"{base}\\{guid}"
                try:
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, gp) as gk:
                        s = 0
                        while True:
                            try:
                                sub = winreg.EnumKey(gk, s)
                            except OSError:
                                break
                            sp = f"{gp}\\{sub}"
                            try:
                                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, sp) as sk:
                                    d = _val(sk, "DriverDesc")
                                    if d and "nvidia" in d.lower():
                                        b = _val(sk, "Brightness")
                                        c = _val(sk, "Contrast")
                                        v = _val(sk, "DigitalVibrance")
                                        if b is not None and c is not None and v is not None:
                                            keys.append(sp)
                            except OSError:
                                pass
                            s += 1
                except OSError:
                    pass
                g += 1
    except OSError:
        pass

    return keys


def _write_nvidia_registry(brightness_val, contrast_val, gamma_val, vibrance_val):
    """
    Write color values directly to the NVIDIA driver registry keys and the
    NVIDIA Control Panel per-display subkeys.

    Parameters are the raw DWORD values (0-255):
        128 = neutral 50% for Brightness/Contrast/Vibrance
        128 = gamma 1.0
    """
    import winreg

    if brightness_val is None:
        brightness_val = 128
    if contrast_val is None:
        contrast_val = 128
    if gamma_val is None:
        gamma_val = 128
    if vibrance_val is None:
        vibrance_val = 128

    keys_written = False

    # --- HKLM driver-level paths ---
    for key_path in _find_target_display_keys():
        try:
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                key_path,
                0,
                winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY,
            ) as k:
                winreg.SetValueEx(k, "Brightness", 0, winreg.REG_DWORD, brightness_val)
                winreg.SetValueEx(k, "Contrast", 0, winreg.REG_DWORD, contrast_val)
                winreg.SetValueEx(k, "DigitalVibrance", 0, winreg.REG_DWORD, vibrance_val)
            keys_written = True
        except (FileNotFoundError, OSError):
            continue

    # --- HKCU NVIDIA Control Panel slider paths ---
    try:
        base = r"Software\NVIDIA Corporation\Global\NVControlPanel\DesktopColorSettings"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, base) as root:
            h = 0
            while True:
                try:
                    sub = winreg.EnumKey(root, h)
                except OSError:
                    break
                sub_path = f"{base}\\{sub}"
                try:
                    with winreg.OpenKey(
                        winreg.HKEY_CURRENT_USER,
                        sub_path,
                        0,
                        winreg.KEY_SET_VALUE,
                    ) as k:
                        winreg.SetValueEx(k, "Brightness", 0, winreg.REG_DWORD, brightness_val)
                        winreg.SetValueEx(k, "Contrast", 0, winreg.REG_DWORD, contrast_val)
                        winreg.SetValueEx(k, "Gamma", 0, winreg.REG_DWORD, gamma_val)
                        winreg.SetValueEx(k, "DigitalVibrance", 0, winreg.REG_DWORD, vibrance_val)
                        keys_written = True
                except (FileNotFoundError, OSError):
                    pass
                h += 1
    except OSError:
        pass

    if not keys_written:
        raise RuntimeError("No NVIDIA display registry keys found; cannot apply color settings.")

    _broadcast_display_change()


def _pct_to_reg(pct):
    """Convert percentage (0-100) to registry DWORD (0-255)."""
    return max(0, min(255, round(max(0, min(100, pct)) / 100.0 * 255)))


def _gamma_to_reg(gamma):
    """Convert gamma float (e.g. 1.0) to registry DWORD (0-255, 128 = 1.0)."""
    return max(0, min(255, round(gamma * 128)))


def apply_nvidia_settings(brightness_pct, contrast_pct, gamma_val, vibrance_pct):
    """
    Apply NVIDIA color settings by writing directly to the registry
    and broadcasting the change. No external tools required.
    """
    b_val = _pct_to_reg(brightness_pct)
    c_val = _pct_to_reg(contrast_pct)
    g_val = _gamma_to_reg(gamma_val)
    v_val = _pct_to_reg(vibrance_pct)
    _write_nvidia_registry(b_val, c_val, g_val, v_val)
    time.sleep(0.5)


def reset_nvidia_settings():
    """Reset to neutral defaults (50%, 50%, 1.0, 50%)."""
    apply_nvidia_settings(50, 50, 1.0, 50)


# ===================================================================
# HDR TOGGLE (smart state-checking before hotkey)
# ===================================================================

def _send_win_alt_b():
    """Emulate Win+Alt+B keyboard shortcut to toggle HDR."""
    VK_LWIN = 0x5B
    VK_LMENU = 0xA4
    VK_B = 0x42

    def press(vk):
        user32.keybd_event(vk, 0, 0, 0)

    def release(vk):
        user32.keybd_event(vk, 0, 2, 0)

    press(VK_LWIN)
    time.sleep(0.15)
    press(VK_LMENU)
    time.sleep(0.15)
    press(VK_B)
    time.sleep(0.20)
    release(VK_B)
    time.sleep(0.15)
    release(VK_LMENU)
    time.sleep(0.15)
    release(VK_LWIN)
    time.sleep(2.0)


def toggle_hdr(enable=True):
    """
    Toggle HDR on/off with smart state-checking.

    Before sending Win+Alt+B, checks the current HDR state via the WinAPI
    DisplayConfig path. Skips the hotkey if HDR is already in the desired
    state, preventing accidental toggles.
    """
    hdr_on = is_hdr_enabled()
    need_toggle = (enable and not hdr_on) or (not enable and hdr_on)

    if not need_toggle:
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

        # Smart HDR: only toggle if currently OFF
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

        # Neutral defaults for cinema mode
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
        """Normal Mode: kill apps, smart HDR off, 2K, user color profile."""
        self._set_status("Деактивация кинорежима...")

        self._set_status("Остановка приложений...")
        kill_app("Lampa.exe")
        kill_app("TorrServer.exe")
        time.sleep(0.5)

        # Smart HDR: only toggle if currently ON
        self._set_status("Проверка и отключение HDR...")
        toggle_hdr(enable=False)
        time.sleep(2.0)

        self._set_status("Смена разрешения на 2K...")
        if not change_resolution(*RES_2K):
            self._set_status("Не удалось изменить разрешение на 2K", True)
            return
        time.sleep(1.5)

        # Apply user's custom color profile (45%, 70%, 0.95, 66% from config)
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
