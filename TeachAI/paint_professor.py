#!/usr/bin/env python3
r"""
PAINT PROFESSOR — DeepSeek + Microsoft Paint + voz full-duplex (Windows)

Un único archivo ejecutable que convierte Microsoft Paint en una pizarra viva:
DeepSeek/LM Studio deciden qué explicar y dibujar; un lápiz virtual independiente
humaniza los trazos, habla, escucha permanentemente y acepta interrupciones.

PRIMER USO (PowerShell):
    py paint_professor.py --install
    $env:DEEPSEEK_API_KEY="sk-..."
    py paint_professor.py

Opcionales:
    $env:DEEPSEEK_MODEL="deepseek-flash"
    $env:PAINT_PROFESSOR_STT_MODEL="base"       # tiny/base/small/medium
    $env:PAINT_PROFESSOR_STT_DEVICE="cpu"       # cpu/cuda
    $env:PAINT_PROFESSOR_VOICE="es-ES-ElviraNeural"
    $env:PAINT_PROFESSOR_VISION="1"             # adjuntar captura de Paint
    $env:PAINT_PROFESSOR_VISUAL_MONITOR="1"      # auditar lienzo tras cada acción
    $env:PAINT_PROFESSOR_BOARD="native"           # native (recomendado) / paint
    $env:PAINT_PROFESSOR_PROVIDER="auto"         # auto/deepseek/lmstudio
    $env:PAINT_PROFESSOR_LOCAL_MODEL=""          # opcional: model_key de lms
    $env:PAINT_PROFESSOR_SYNTHETIC_PEN="1"       # no mover el ratón físico
    $env:PAINT_PROFESSOR_MOUSE_FALLBACK="0"      # 1 permite usarlo si falla el lápiz
    $env:PAINT_PROFESSOR_CUSTOM_COLORS="0"       # 1: editor RGB experimental de Paint
    $env:PAINT_PROFESSOR_REQUIRE_ADMIN="1"        # solicita UAC al arrancar (0 lo desactiva)

La calibración se guarda en:
    %LOCALAPPDATA%\PaintProfessor\config.json

Recomendación: usa auriculares. El micrófono permanece abierto durante la voz de
la profesora para permitir barge-in; con altavoces, su propia voz puede volver a
entrar por el micrófono pese al filtro de eco textual.
"""

from __future__ import annotations

import argparse
import asyncio
import ast
import base64
from collections import deque
import copy
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from difflib import SequenceMatcher
import io
import json
import math
import os
from pathlib import Path
import queue
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import unicodedata
import urllib.error
import urllib.request
from typing import Any, Callable, Iterable


APP_NAME = "Paint Professor"
APP_VERSION = "0.26.2"
VIRTUAL_W, VIRTUAL_H = 1000.0, 700.0
IS_WINDOWS = sys.platform == "win32"
DEFAULT_BOARD_BACKEND = os.getenv("PAINT_PROFESSOR_BOARD", "native").strip().lower()
if DEFAULT_BOARD_BACKEND not in {"native", "paint"}:
    DEFAULT_BOARD_BACKEND = "native"

REQUIRED_PACKAGES = [
    "openai>=1.40",
    "numpy>=1.26",
    "sounddevice>=0.5",
    "webrtcvad-wheels>=2.0.14",
    "faster-whisper>=1.1",
    "edge-tts>=7.0",
    "kokoro-onnx>=0.4.9",
    "soundfile>=0.12",
    "pygame>=2.6",
    "Pillow>=10.4",
    "matplotlib>=3.9",
    "tiktoken>=0.8",
    "discord.py[voice]>=2.5",
    "keyring>=25.0",
    "imageio-ffmpeg>=0.5",
    "pywinauto>=0.6.9",
    "pywebview>=5.4",
]


def _environment_python(environment: Path) -> Path:
    return environment / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")


def _write_launcher(python: Path, script: Path) -> Path | None:
    if not IS_WINDOWS:
        return None
    launcher = script.parent / "Iniciar Paint Professor.cmd"
    try:
        python_command = f'%~dp0{python.relative_to(script.parent)}'
    except ValueError:
        python_command = str(python)
    launcher.write_text(
        "@echo off\r\n"
        "cd /d \"%~dp0\"\r\n"
        f'"{python_command}" "%~dp0{script.name}" %*\r\n'
        "if errorlevel 1 pause\r\n",
        encoding="utf-8",
    )
    return launcher


def install_dependencies(inner: bool = False) -> None:
    """Crea/repara un entorno aislado y verifica la instalación completa."""
    if sys.version_info < (3, 11):
        raise RuntimeError("Paint Professor necesita Python 3.11 o 3.12 de 64 bits")
    script = Path(__file__).resolve()
    current_prefix = Path(sys.prefix).resolve()
    current_is_project_env = (
        (current_prefix / "pyvenv.cfg").exists()
        and current_prefix.parent == script.parent
    )

    if not inner and not current_is_project_env:
        environment = script.parent / ".paint-professor-env"
        python = _environment_python(environment)
        print(f"[1/4] Preparando entorno aislado: {environment}")
        if not python.exists():
            subprocess.check_call([sys.executable, "-m", "venv", str(environment)])
        try:
            subprocess.check_call([str(python), "-m", "ensurepip", "--upgrade"])
        except subprocess.CalledProcessError as exc:
            uv = shutil.which("uv")
            if not uv:
                raise RuntimeError(
                    "No pude instalar pip dentro del entorno. Repara Python 3.12 "
                    "marcando pip y venv en su instalador."
                ) from exc
            subprocess.check_call([uv, "pip", "install", "--python", str(python), "pip", "setuptools", "wheel"])
        subprocess.check_call([str(python), str(script), "--install-inner"])
        launcher = _write_launcher(python, script)
        print("\nINSTALACIÓN VERIFICADA")
        if launcher:
            print(f"Abre desde ahora: {launcher}")
        else:
            print(f"Ejecuta: {python} {script}")
        return

    print(f"[2/4] Reparando pip en {sys.executable}")
    subprocess.run([sys.executable, "-m", "ensurepip", "--upgrade"], check=False)
    pip_probe = subprocess.run(
        [sys.executable, "-m", "pip", "--version"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )
    if pip_probe.returncode:
        uv = shutil.which("uv")
        if not uv:
            raise RuntimeError("Este entorno no contiene pip ni encuentro uv para repararlo")
        subprocess.check_call([uv, "pip", "install", "--python", sys.executable, "pip", "setuptools", "wheel"])
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"])
    print("[3/4] Instalando dependencias de Paint Professor…")
    command = [sys.executable, "-m", "pip", "install", "--upgrade", *REQUIRED_PACKAGES]
    try:
        subprocess.check_call(command)
    except subprocess.CalledProcessError:
        uv = shutil.which("uv")
        if not uv:
            raise
        print("pip falló; segundo intento mediante uv pip…")
        subprocess.check_call([uv, "pip", "install", "--python", sys.executable, *REQUIRED_PACKAGES])

    print("[4/4] Verificando imports…")
    missing = dependency_error()
    if missing:
        raise RuntimeError("La instalación terminó, pero siguen faltando: " + ", ".join(missing))
    launcher = _write_launcher(Path(sys.executable).resolve(), script)
    print("Todas las dependencias se importan correctamente.")
    if IS_WINDOWS:
        try:
            print("LM Studio CLI detectado:", LMStudioManager._find_cli())
        except Exception as exc:
            print("AVISO:", exc)
    if launcher:
        print(f"Lanzador creado: {launcher}")


def dependency_error() -> list[str]:
    checks = {
        "openai": "openai",
        "numpy": "numpy",
        "sounddevice": "sounddevice",
        "webrtcvad": "webrtcvad-wheels",
        "faster_whisper": "faster-whisper",
        "edge_tts": "edge-tts",
        "kokoro_onnx": "kokoro-onnx",
        "soundfile": "soundfile",
        "pygame": "pygame",
        "PIL": "Pillow",
        "matplotlib": "matplotlib",
        "tiktoken": "tiktoken",
        "discord": "discord.py[voice]",
        "nacl": "PyNaCl",
        "keyring": "keyring",
        "imageio_ffmpeg": "imageio-ffmpeg",
        # El paquete se distribuye como pywebview, pero se importa como webview.
        # Sin esta comprobación la app parecía arrancar correctamente y acababa
        # degradándose silenciosamente a una pestaña del navegador.
        "webview": "pywebview",
    }
    missing = []
    for module, package in checks.items():
        try:
            __import__(module)
        except ImportError:
            missing.append(package)
    return missing


def app_data_dir() -> Path:
    root = Path(os.getenv("LOCALAPPDATA", Path.home()))
    path = root / "PaintProfessor"
    path.mkdir(parents=True, exist_ok=True)
    return path


CONFIG_PATH = app_data_dir() / "config.json"


DEFAULT_CONFIG: dict[str, Any] = {
    "voice": os.getenv("PAINT_PROFESSOR_VOICE", "es-ES-ElviraNeural"),
    "tts_engine": os.getenv("PAINT_PROFESSOR_TTS", "kokoro").lower(),
    "kokoro_voice": os.getenv("PAINT_PROFESSOR_KOKORO_VOICE", "ef_dora"),
    "microphone_device": None,
    "discord_enabled": False,
    "discord_read_chat": False,
    "discord_snapshot_interval": 8.0,
    "model": os.getenv("DEEPSEEK_MODEL", "deepseek-flash"),
    "stt_model": os.getenv("PAINT_PROFESSOR_STT_MODEL", "base"),
    "stt_device": os.getenv("PAINT_PROFESSOR_STT_DEVICE", "cpu"),
    "vision": os.getenv("PAINT_PROFESSOR_VISION", "1") != "0",
    # Devuelve al modelo una captura real tras cada bloque de acciones visuales.
    "visual_monitor": os.getenv("PAINT_PROFESSOR_VISUAL_MONITOR", "1") != "0",
    "board_backend": DEFAULT_BOARD_BACKEND,
    "provider": os.getenv("PAINT_PROFESSOR_PROVIDER", "auto").lower(),
    "lmstudio_url": os.getenv("PAINT_PROFESSOR_LMSTUDIO_URL", "http://127.0.0.1:1234/v1"),
    "local_model": os.getenv("PAINT_PROFESSOR_LOCAL_MODEL", ""),
    "local_identifier": "paint-professor-local",
    "local_context": 32768,
    "local_auto_download": True,
    "synthetic_pen": os.getenv("PAINT_PROFESSOR_SYNTHETIC_PEN", "1") != "0",
    "allow_physical_mouse_fallback": os.getenv("PAINT_PROFESSOR_MOUSE_FALLBACK", "0") == "1",
    # El editor RGB de Paint moderno reconstruye parte del árbol WinUI y algunas
    # versiones de Paint se cierran si UI Automation lo manipula durante ese
    # cambio. La paleta normal es estable; el editor queda como opción avanzada.
    "custom_colors": os.getenv("PAINT_PROFESSOR_CUSTOM_COLORS", "0") == "1",
    "require_admin": os.getenv("PAINT_PROFESSOR_REQUIRE_ADMIN", "0" if DEFAULT_BOARD_BACKEND == "native" else "1") != "0",
    "language": "es",
    "paint": {
        "canvas": [0.025, 0.19, 0.975, 0.94],
        "controls": {},
    },
}


def load_config() -> dict[str, Any]:
    data = json.loads(json.dumps(DEFAULT_CONFIG))
    if CONFIG_PATH.exists():
        try:
            saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            for key, value in saved.items():
                if key == "paint" and isinstance(value, dict):
                    data["paint"].update(value)
                else:
                    data[key] = value
        except Exception:
            pass
    return data


def save_config(config: dict[str, Any]) -> None:
    CONFIG_PATH.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def hex_rgb(value: str) -> tuple[int, int, int]:
    value = value.strip().lstrip("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    if not re.fullmatch(r"[0-9A-Fa-f]{6}", value):
        raise ValueError("El color debe tener formato #RRGGBB")
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))


if IS_WINDOWS:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    shell32 = ctypes.windll.shell32

    INPUT_MOUSE = 0
    MOUSEEVENTF_MOVE = 0x0001
    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004
    MOUSEEVENTF_ABSOLUTE = 0x8000
    MOUSEEVENTF_VIRTUALDESK = 0x4000
    WM_LBUTTONDOWN, WM_LBUTTONUP, MK_LBUTTON = 0x0201, 0x0202, 0x0001
    SW_RESTORE = 9
    KEYEVENTF_KEYUP = 0x0002
    VK_CONTROL, VK_SHIFT, VK_MENU = 0x11, 0x10, 0x12
    VK_ADD, VK_SUBTRACT, VK_PRIOR, VK_NEXT = 0x6B, 0x6D, 0x21, 0x22
    PT_PEN = 3
    POINTER_FEEDBACK_DEFAULT = 1
    POINTER_FLAG_NEW = 0x00000001
    POINTER_FLAG_INRANGE = 0x00000002
    POINTER_FLAG_INCONTACT = 0x00000004
    POINTER_FLAG_FIRSTBUTTON = 0x00000010
    POINTER_FLAG_DOWN = 0x00010000
    POINTER_FLAG_UPDATE = 0x00020000
    POINTER_FLAG_UP = 0x00040000
    PEN_MASK_PRESSURE = 0x00000001

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG), ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
        ]

    class INPUT_UNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", INPUT_UNION)]

    class RECT(ctypes.Structure):
        _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                    ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

    class POINT(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

    class POINTER_INFO(ctypes.Structure):
        _fields_ = [
            ("pointerType", wintypes.DWORD), ("pointerId", wintypes.UINT),
            ("frameId", wintypes.UINT), ("pointerFlags", wintypes.DWORD),
            ("sourceDevice", wintypes.HANDLE), ("hwndTarget", wintypes.HWND),
            ("ptPixelLocation", POINT), ("ptHimetricLocation", POINT),
            ("ptPixelLocationRaw", POINT), ("ptHimetricLocationRaw", POINT),
            ("dwTime", wintypes.DWORD), ("historyCount", wintypes.UINT),
            ("InputData", ctypes.c_int32), ("dwKeyStates", wintypes.DWORD),
            ("PerformanceCount", ctypes.c_uint64),
            ("ButtonChangeType", wintypes.DWORD),
        ]

    class POINTER_TOUCH_INFO(ctypes.Structure):
        _fields_ = [
            ("pointerInfo", POINTER_INFO), ("touchFlags", wintypes.DWORD),
            ("touchMask", wintypes.DWORD), ("rcContact", RECT),
            ("rcContactRaw", RECT), ("orientation", wintypes.UINT),
            ("pressure", wintypes.UINT),
        ]

    class POINTER_PEN_INFO(ctypes.Structure):
        _fields_ = [
            ("pointerInfo", POINTER_INFO), ("penFlags", wintypes.DWORD),
            ("penMask", wintypes.DWORD), ("pressure", wintypes.UINT),
            ("rotation", wintypes.UINT), ("tiltX", ctypes.c_int32),
            ("tiltY", ctypes.c_int32),
        ]

    class POINTER_TYPE_UNION(ctypes.Union):
        _fields_ = [
            ("pointerInfo", POINTER_INFO), ("touchInfo", POINTER_TOUCH_INFO),
            ("penInfo", POINTER_PEN_INFO),
        ]

    class POINTER_TYPE_INFO(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("data", POINTER_TYPE_UNION)]

    user32.WindowFromPoint.argtypes = [POINT]
    user32.WindowFromPoint.restype = wintypes.HWND
    user32.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(POINT)]
    user32.ScreenToClient.restype = wintypes.BOOL
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.PostMessageW.restype = wintypes.BOOL

    shell32.ShellExecuteW.argtypes = [
        wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
        wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int,
    ]
    shell32.ShellExecuteW.restype = wintypes.HINSTANCE

    if hasattr(user32, "CreateSyntheticPointerDevice"):
        user32.CreateSyntheticPointerDevice.argtypes = [wintypes.DWORD, wintypes.ULONG, wintypes.DWORD]
        user32.CreateSyntheticPointerDevice.restype = wintypes.HANDLE
        user32.InjectSyntheticPointerInput.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(POINTER_TYPE_INFO), wintypes.UINT,
        ]
        user32.InjectSyntheticPointerInput.restype = wintypes.BOOL
        user32.DestroySyntheticPointerDevice.argtypes = [wintypes.HANDLE]
        user32.DestroySyntheticPointerDevice.restype = None


class WinInput:
    """Movimiento nativo continuo mediante SendInput."""

    @staticmethod
    def cursor() -> tuple[int, int]:
        point = POINT()
        user32.GetCursorPos(ctypes.byref(point))
        return point.x, point.y

    @staticmethod
    def move(x: float, y: float) -> None:
        vx = user32.GetSystemMetrics(76)
        vy = user32.GetSystemMetrics(77)
        vw = user32.GetSystemMetrics(78)
        vh = user32.GetSystemMetrics(79)
        ax = int((x - vx) * 65535 / max(1, vw - 1))
        ay = int((y - vy) * 65535 / max(1, vh - 1))
        mi = MOUSEINPUT(ax, ay, 0, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, 0, None)
        packet = INPUT(INPUT_MOUSE, INPUT_UNION(mi=mi))
        user32.SendInput(1, ctypes.byref(packet), ctypes.sizeof(INPUT))

    @staticmethod
    def button(down: bool) -> None:
        flag = MOUSEEVENTF_LEFTDOWN if down else MOUSEEVENTF_LEFTUP
        packet = INPUT(INPUT_MOUSE, INPUT_UNION(mi=MOUSEINPUT(0, 0, 0, flag, 0, None)))
        user32.SendInput(1, ctypes.byref(packet), ctypes.sizeof(INPUT))

    @classmethod
    def click(cls, x: float, y: float) -> None:
        cls.move(x, y)
        time.sleep(0.08)
        cls.button(True)
        time.sleep(0.05)
        cls.button(False)

    @staticmethod
    def background_click(x: float, y: float) -> bool:
        """Prueba un clic por mensajes Win32 sin mover ni pulsar el mouse real."""
        point = POINT(int(round(x)), int(round(y)))
        target = user32.WindowFromPoint(point)
        if not target:
            return False
        client = POINT(point.x, point.y)
        if not user32.ScreenToClient(target, ctypes.byref(client)):
            return False
        packed = (client.x & 0xFFFF) | ((client.y & 0xFFFF) << 16)
        down = user32.PostMessageW(target, WM_LBUTTONDOWN, MK_LBUTTON, packed)
        up = user32.PostMessageW(target, WM_LBUTTONUP, 0, packed)
        return bool(down and up)

    @staticmethod
    def chord(*keys: int) -> None:
        for key in keys:
            user32.keybd_event(key, 0, 0, 0)
        for key in reversed(keys):
            user32.keybd_event(key, 0, KEYEVENTF_KEYUP, 0)

    @staticmethod
    def key(vk: int) -> None:
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


class SyntheticPen:
    """Dispositivo de lápiz Win32 independiente del cursor físico del usuario."""

    def __init__(self, enabled: bool = True):
        self.handle = None
        self.available = False
        self.contact = False
        self.position = WinInput.cursor() if IS_WINDOWS else (0, 0)
        self.error = ""
        if not enabled or not IS_WINDOWS:
            self.error = "desactivado"
            return
        create = getattr(user32, "CreateSyntheticPointerDevice", None)
        if not create:
            self.error = "Windows no ofrece CreateSyntheticPointerDevice"
            return
        try:
            self.handle = create(PT_PEN, 1, POINTER_FEEDBACK_DEFAULT)
            if not self.handle:
                raise ctypes.WinError(kernel32.GetLastError())
            self.available = True
        except Exception as exc:
            self.error = str(exc)
            self.handle = None

    def _inject(self, x: float, y: float, flags: int, pressure: int = 512) -> None:
        if not self.available or not self.handle:
            raise RuntimeError(self.error or "El lápiz sintético no está disponible")
        packet = POINTER_TYPE_INFO()
        packet.type = PT_PEN
        pen = packet.data.penInfo
        pen.pointerInfo.pointerType = PT_PEN
        pen.pointerInfo.pointerId = 0
        pen.pointerInfo.pointerFlags = flags
        pen.pointerInfo.ptPixelLocation = POINT(int(round(x)), int(round(y)))
        pen.pointerInfo.ptPixelLocationRaw = POINT(int(round(x)), int(round(y)))
        pen.penMask = PEN_MASK_PRESSURE
        pen.pressure = max(1, min(1024, int(pressure)))
        if not user32.InjectSyntheticPointerInput(self.handle, ctypes.byref(packet), 1):
            raise ctypes.WinError(kernel32.GetLastError())
        self.position = (int(round(x)), int(round(y)))

    def begin(self, x: float, y: float, pressure: int = 512) -> None:
        self._inject(
            x, y,
            POINTER_FLAG_NEW | POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT
            | POINTER_FLAG_FIRSTBUTTON | POINTER_FLAG_DOWN,
            pressure,
        )
        self.contact = True

    def move(self, x: float, y: float, pressure: int = 512) -> None:
        self._inject(
            x, y,
            POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT
            | POINTER_FLAG_FIRSTBUTTON | POINTER_FLAG_UPDATE,
            pressure,
        )

    def end(self, x: float | None = None, y: float | None = None) -> None:
        if not self.contact:
            return
        px, py = self.position if x is None or y is None else (x, y)
        try:
            self._inject(px, py, POINTER_FLAG_UP, 1)
        finally:
            self.contact = False

    def disable(self, reason: str) -> None:
        try:
            self.end()
        except Exception:
            pass
        self.available = False
        self.error = reason

    def close(self) -> None:
        try:
            self.end()
        except Exception:
            pass
        if self.handle and IS_WINDOWS:
            try:
                user32.DestroySyntheticPointerDevice(self.handle)
            except Exception:
                pass
        self.handle = None
        self.available = False


def find_paint_window() -> int | None:
    if not IS_WINDOWS:
        return None
    candidates: list[tuple[int, str]] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if not length:
            return True
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, length + 1)
        text = title.value.strip()
        if re.search(r"(^|[-—]\s)(paint|pintura)$", text, re.I) or text.lower() in {"paint", "pintura"}:
            candidates.append((hwnd, text))
        return True

    user32.EnumWindows(callback_type(callback), 0)
    return candidates[0][0] if candidates else None


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise RuntimeError("No se pudo leer la ventana de Paint")
    return rect.left, rect.top, rect.right, rect.bottom


def focus_window(hwnd: int) -> None:
    user32.ShowWindow(hwnd, SW_RESTORE)
    if user32.GetForegroundWindow() != hwnd:
        # Un toque de Alt permite que SetForegroundWindow funcione desde otro proceso.
        WinInput.key(VK_MENU)
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.12)


@dataclass
class Style:
    # Vacíos al arrancar: no suponemos que Paint conserve nuestro último estado.
    color: str = ""
    width: int | None = None
    speed: float = 1.0


class CancelledDrawing(RuntimeError):
    pass


class HumanMotion:
    def __init__(self, cancel: threading.Event, log: Callable[[str], None],
                 synthetic_pen: bool = True, allow_mouse_fallback: bool = False):
        self.cancel = cancel
        self.log = log
        self.fps = 120
        self.pen = SyntheticPen(synthetic_pen)
        self.allow_mouse_fallback = allow_mouse_fallback
        self.mouse_contact = False
        if self.pen.available:
            self.log("Lápiz virtual independiente preparado — el ratón físico queda libre")
        elif synthetic_pen:
            self.log(f"Lápiz virtual no disponible ({self.pen.error}); usaré SendInput como respaldo")

    def _check(self) -> None:
        if self.cancel.is_set():
            if IS_WINDOWS:
                try:
                    self.pen.end()
                except Exception:
                    pass
                if self.mouse_contact:
                    WinInput.button(False)
                    self.mouse_contact = False
            raise CancelledDrawing("Interrumpido por el alumno")

    def travel(self, target: tuple[float, float], speed: float = 1.0) -> None:
        start = self.pen.position if self.pen.available else WinInput.cursor()
        distance = math.dist(start, target)
        duration = max(0.10, min(1.4, distance / (850.0 * max(0.3, speed))))
        steps = max(8, int(duration * self.fps))
        angle = random.uniform(0, math.tau)
        bend = min(14.0, distance * 0.025)
        for i in range(1, steps + 1):
            self._check()
            t = i / steps
            eased = 10 * t**3 - 15 * t**4 + 6 * t**5
            wiggle = math.sin(math.pi * t) * math.sin(angle + t * math.pi) * bend
            dx, dy = target[0] - start[0], target[1] - start[1]
            norm = max(1.0, math.hypot(dx, dy))
            x = start[0] + dx * eased + (-dy / norm) * wiggle
            y = start[1] + dy * eased + (dx / norm) * wiggle
            if self.pen.available:
                # El dispositivo conserva su propia posición; no tocamos el cursor
                # del usuario durante el recorrido entre trazos.
                self.pen.position = (int(round(x)), int(round(y)))
            else:
                WinInput.move(x, y)
            time.sleep(duration / steps)

    def stroke(self, points: list[tuple[float, float]], speed: float = 1.0) -> None:
        if len(points) < 2:
            return
        if not self.pen.available and not self.allow_mouse_fallback:
            raise RuntimeError(
                "El lápiz virtual de Windows no está disponible y el respaldo con el mouse físico "
                "está desactivado. Define PAINT_PROFESSOR_MOUSE_FALLBACK=1 solo si quieres permitirlo."
            )
        self.travel(points[0], speed * 1.2)
        self._check()
        using_pen = self.pen.available
        if using_pen:
            try:
                self.pen.begin(*points[0], pressure=random.randint(470, 560))
            except Exception as exc:
                self.pen.disable(str(exc))
                if not self.allow_mouse_fallback:
                    raise RuntimeError(f"Falló el lápiz virtual de Windows: {exc}") from exc
                using_pen = False
                self.log(f"El lápiz virtual falló ({exc}); activo respaldo con ratón")
                self.travel(points[0], speed * 1.2)
                WinInput.button(True)
                self.mouse_contact = True
        else:
            WinInput.button(True)
            self.mouse_contact = True
        try:
            # Recorremos toda la curva por longitud acumulada. La versión
            # anterior aceleraba y frenaba en CADA punto de control, por lo que
            # una silueta compleja parecía una colección de segmentos. Ahora
            # solo acelera al apoyar el lápiz y desacelera al levantarlo.
            clean_points = [points[0]]
            for point in points[1:]:
                if math.dist(clean_points[-1], point) >= .15:
                    clean_points.append(point)
            if len(clean_points) < 2:
                return
            cumulative = [0.0]
            for a, b in zip(clean_points, clean_points[1:]):
                cumulative.append(cumulative[-1] + math.dist(a, b))
            total = cumulative[-1]
            duration = max(.06, total / (315.0 * max(.3, speed)))
            steps = max(3, int(duration * self.fps))
            segment = 0
            jitter_phase = random.random() * math.tau
            for i in range(1, steps + 1):
                self._check()
                u = i / steps
                eased = 10*u**3 - 15*u**4 + 6*u**5
                travelled = total * eased
                while segment + 1 < len(cumulative) - 1 and cumulative[segment + 1] < travelled:
                    segment += 1
                a, b = clean_points[segment], clean_points[segment + 1]
                span = max(1e-6, cumulative[segment + 1] - cumulative[segment])
                t = min(1.0, max(0.0, (travelled - cumulative[segment]) / span))
                dx, dy = b[0] - a[0], b[1] - a[1]
                norm = max(1e-6, math.hypot(dx, dy))
                fade = math.sin(math.pi * u)
                jitter = .24 * fade * math.sin(jitter_phase + i * .43)
                x = a[0] + dx*t + (-dy/norm)*jitter
                y = a[1] + dy*t + (dx/norm)*jitter
                if using_pen:
                    try:
                        pressure = 515 + int(32 * math.sin(i*.19 + jitter_phase))
                        self.pen.move(x, y, pressure)
                    except Exception as exc:
                        self.pen.disable(str(exc))
                        if not self.allow_mouse_fallback:
                            raise RuntimeError(f"Falló el lápiz virtual de Windows: {exc}") from exc
                        using_pen = False
                        self.log(f"El lápiz virtual se interrumpió ({exc}); continúo con ratón")
                        WinInput.move(x, y)
                        WinInput.button(True)
                        self.mouse_contact = True
                else:
                    WinInput.move(x, y)
                time.sleep(duration / steps)
        finally:
            if using_pen:
                try:
                    self.pen.end(*points[-1])
                except Exception as exc:
                    self.pen.disable(str(exc))
                    self.log(f"No pude levantar el lápiz virtual limpiamente: {exc}")
            else:
                WinInput.button(False)
                self.mouse_contact = False
        time.sleep(random.uniform(0.025, 0.075))

    def close(self) -> None:
        self.pen.close()

    def reset_for_window(self) -> None:
        """Suelta cualquier contacto viejo al cambiar el proceso/ventana de Paint."""
        try:
            self.pen.end()
        except Exception:
            pass
        if self.mouse_contact and IS_WINDOWS:
            WinInput.button(False)
        self.mouse_contact = False
        if IS_WINDOWS:
            self.pen.position = WinInput.cursor()


# Fuente monolineal. Cada glifo contiene trazos en una cuadrícula 0..1.
G: dict[str, list[list[tuple[float, float]]]] = {
    "A": [[(.05,1),(.5,0),(.95,1)],[(.22,.6),(.78,.6)]],
    "B": [[(.1,0),(.1,1)],[(.1,0),(.62,0),(.86,.18),(.62,.48),(.1,.48)],[(.1,.48),(.66,.48),(.9,.72),(.66,1),(.1,1)]],
    "C": [[(.9,.12),(.72,0),(.25,.02),(.05,.28),(.05,.74),(.28,1),(.75,.98),(.92,.85)]],
    "D": [[(.1,0),(.1,1)],[(.1,0),(.58,0),(.9,.25),(.9,.75),(.58,1),(.1,1)]],
    "E": [[(.88,0),(.1,0),(.1,1),(.9,1)],[(.1,.48),(.7,.48)]],
    "F": [[(.1,1),(.1,0),(.9,0)],[(.1,.48),(.72,.48)]],
    "G": [[(.9,.16),(.72,.02),(.25,.02),(.05,.28),(.05,.74),(.28,1),(.78,.98),(.92,.76),(.92,.58),(.56,.58)]],
    "H": [[(.1,0),(.1,1)],[(.9,0),(.9,1)],[(.1,.5),(.9,.5)]],
    "I": [[(.18,0),(.82,0)],[(.5,0),(.5,1)],[(.18,1),(.82,1)]],
    "J": [[(.18,0),(.88,0),(.88,.78),(.7,1),(.3,1),(.1,.8)]],
    "K": [[(.1,0),(.1,1)],[(.9,0),(.1,.58),(.92,1)]],
    "L": [[(.1,0),(.1,1),(.9,1)]],
    "M": [[(.06,1),(.06,0),(.5,.52),(.94,0),(.94,1)]],
    "N": [[(.08,1),(.08,0),(.92,1),(.92,0)]],
    "O": [[(.5,0),(.22,.03),(.05,.28),(.05,.74),(.25,.98),(.72,.98),(.95,.72),(.95,.26),(.75,.02),(.5,0)]],
    "P": [[(.1,1),(.1,0),(.65,0),(.9,.22),(.65,.5),(.1,.5)]],
    "Q": [[(.5,0),(.22,.03),(.05,.28),(.05,.74),(.25,.98),(.72,.98),(.95,.72),(.95,.26),(.75,.02),(.5,0)],[(.58,.68),(1.0,1.08)]],
    "R": [[(.1,1),(.1,0),(.65,0),(.9,.22),(.65,.5),(.1,.5)],[(.55,.5),(.94,1)]],
    "S": [[(.9,.14),(.72,.02),(.25,.02),(.05,.22),(.18,.45),(.76,.56),(.94,.76),(.78,.98),(.25,.98),(.06,.84)]],
    "T": [[(.05,0),(.95,0)],[(.5,0),(.5,1)]],
    "U": [[(.08,0),(.08,.74),(.28,.98),(.7,.98),(.92,.74),(.92,0)]],
    "V": [[(.04,0),(.5,1),(.96,0)]],
    "W": [[(.03,0),(.22,1),(.5,.58),(.78,1),(.97,0)]],
    "X": [[(.05,0),(.95,1)],[(.95,0),(.05,1)]],
    "Y": [[(.04,0),(.5,.5),(.96,0)],[(.5,.5),(.5,1)]],
    "Z": [[(.05,0),(.95,0),(.05,1),(.95,1)]],
    "0": [[(.5,0),(.2,.05),(.05,.3),(.05,.72),(.22,.98),(.72,.98),(.95,.7),(.95,.25),(.75,.02),(.5,0)],[(.2,.85),(.78,.14)]],
    "1": [[(.28,.22),(.5,0),(.5,1)],[(.25,1),(.78,1)]],
    "2": [[(.08,.22),(.28,.02),(.72,.02),(.92,.24),(.82,.46),(.08,1),(.94,1)]],
    "3": [[(.08,.14),(.3,.02),(.7,.02),(.92,.22),(.72,.48),(.42,.5)],[(.72,.48),(.95,.72),(.74,.98),(.3,.98),(.06,.82)]],
    "4": [[(.76,1),(.76,0),(.08,.7),(.94,.7)]],
    "5": [[(.9,0),(.16,0),(.1,.48),(.67,.48),(.92,.68),(.78,.96),(.3,1),(.06,.84)]],
    "6": [[(.86,.12),(.67,.02),(.25,.14),(.06,.5),(.1,.84),(.32,1),(.72,.96),(.92,.72),(.75,.5),(.1,.5)]],
    "7": [[(.06,0),(.94,0),(.35,1)]],
    "8": [[(.5,0),(.18,.08),(.12,.34),(.5,.5),(.86,.34),(.8,.08),(.5,0)],[(.5,.5),(.12,.68),(.16,.94),(.5,1),(.86,.94),(.9,.68),(.5,.5)]],
    "9": [[(.88,.5),(.22,.5),(.06,.28),(.25,.02),(.7,.04),(.92,.3),(.86,.75),(.62,.98),(.18,.9)]],
    "-": [[(.12,.53),(.88,.53)]], "+": [[(.1,.52),(.9,.52)],[(.5,.15),(.5,.9)]],
    "=": [[(.1,.36),(.9,.36)],[(.1,.68),(.9,.68)]], "/": [[(.08,1),(.92,0)]],
    "<": [[(.86,.12),(.12,.5),(.86,.88)]], ">": [[(.14,.12),(.88,.5),(.14,.88)]],
    "?": [[(.08,.2),(.28,.02),(.7,.02),(.9,.22),(.78,.43),(.52,.57),(.52,.7)],[(.52,.94),(.52,.98)]],
    "!": [[(.5,0),(.5,.7)],[(.5,.94),(.5,.98)]], ":": [[(.5,.3),(.5,.34)],[(.5,.78),(.5,.82)]],
    ".": [[(.5,.94),(.5,1)]], ",": [[(.55,.9),(.42,1.1)]],
    "(": [[(.7,0),(.42,.18),(.3,.5),(.42,.82),(.7,1)]], ")": [[(.3,0),(.58,.18),(.7,.5),(.58,.82),(.3,1)]],
    "[": [[(.72,0),(.32,0),(.32,1),(.72,1)]], "]": [[(.28,0),(.68,0),(.68,1),(.28,1)]],
    "%": [[(.08,1),(.92,0)],[(.22,.12),(.18,.22),(.28,.3),(.38,.2),(.32,.1),(.22,.12)],[(.68,.7),(.62,.8),(.72,.9),(.84,.8),(.78,.7),(.68,.7)]],
    "*": [[(.5,.15),(.5,.85)],[(.16,.32),(.84,.68)],[(.84,.32),(.16,.68)]],
}


class PaintController:
    def __init__(self, config: dict[str, Any], cancel: threading.Event, log: Callable[[str], None]):
        self.config = config
        self.cancel = cancel
        self.log = log
        self.hwnd: int | None = None
        self.motion = HumanMotion(
            cancel, log,
            bool(config.get("synthetic_pen", True)),
            bool(config.get("allow_physical_mouse_fallback", False)),
        )
        self.style = Style()
        self.active_tool = ""
        self.palette_control_available: bool | None = None
        self.color_control_available: bool | None = None
        self._palette_warning_shown = False
        self._color_warning_shown = False
        self._toolbar_expanded = False
        self.eraser_size: int | None = None

    def _reset_paint_session(self) -> None:
        """Invalida todo lo que pertenecía a otra instancia de Paint."""
        self.motion.reset_for_window()
        self.style = Style()
        self.active_tool = ""
        self.palette_control_available = None
        self.color_control_available = None
        self._palette_warning_shown = False
        self._color_warning_shown = False
        self._toolbar_expanded = False
        self.eraser_size = None

    def attach(self, launch: bool = True) -> None:
        previous_hwnd = self.hwnd
        previous_was_valid = bool(previous_hwnd and user32.IsWindow(previous_hwnd))
        found_hwnd = find_paint_window()
        if not found_hwnd and launch:
            subprocess.Popen(["mspaint.exe"])
            for _ in range(40):
                time.sleep(0.25)
                found_hwnd = find_paint_window()
                if found_hwnd:
                    break
        if not found_hwnd:
            raise RuntimeError("No encuentro Microsoft Paint. Ábrelo y vuelve a pulsar Conectar.")
        self.hwnd = found_hwnd
        if found_hwnd != previous_hwnd or not previous_was_valid:
            self._reset_paint_session()
            # Una ventana recién creada todavía está componiendo la barra WinUI.
            # Esperar aquí evita consultar controles a medio inicializar.
            time.sleep(.65)
        focus_window(self.hwnd)
        self.log("Paint conectado")

    def rect(self) -> tuple[int, int, int, int]:
        if not self.hwnd or not user32.IsWindow(self.hwnd):
            self.attach()
        return window_rect(self.hwnd)

    def ensure_attached(self) -> None:
        if not self.hwnd or not user32.IsWindow(self.hwnd):
            self.attach()
        else:
            focus_window(self.hwnd)

    def canvas_rect(self) -> tuple[float, float, float, float]:
        left, top, right, bottom = self.rect()
        cw = right - left
        ch = bottom - top
        a, b, c, d = self.config["paint"]["canvas"]
        return left + a * cw, top + b * ch, left + c * cw, top + d * ch

    def board_to_screen(self, x: float, y: float) -> tuple[float, float]:
        l, t, r, b = self.canvas_rect()
        x = min(VIRTUAL_W, max(0.0, x))
        y = min(VIRTUAL_H, max(0.0, y))
        return l + (x / VIRTUAL_W) * (r - l), t + (y / VIRTUAL_H) * (b - t)

    def screen_point(self, key: str) -> tuple[float, float] | None:
        value = self.config["paint"].get("controls", {}).get(key)
        if not value:
            return None
        l, t, r, b = self.rect()
        return l + value[0] * (r - l), t + value[1] * (b - t)

    def _click_control(self, key: str, focus: bool = True) -> bool:
        point = self.screen_point(key)
        if point:
            if focus:
                focus_window(self.hwnd)
            if not WinInput.background_click(*point):
                if not self.config.get("allow_physical_mouse_fallback", False):
                    return False
                WinInput.click(*point)
            time.sleep(0.16)
            return True
        return False

    def _invoke_uia(self, control: Any) -> bool:
        """Prueba todos los patrones accesibles de WinUI sin tocar el mouse físico."""
        actions = (
            lambda: control.invoke(),
            lambda: control.iface_invoke.Invoke(),
            lambda: control.iface_selection_item.Select(),
            lambda: control.select(),
            lambda: control.iface_toggle.Toggle(),
            lambda: control.toggle(),
            lambda: control.iface_legacy_iaccessible.DoDefaultAction(),
            lambda: control.iface_expand_collapse.Expand(),
            lambda: control.expand(),
        )
        for action in actions:
            try:
                action()
                return True
            except Exception:
                continue

        # Muchos controles WinUI de Paint se pueden enfocar, aunque no publiquen
        # InvokePattern. Espacio ejecuta el control enfocado sin mover el cursor.
        try:
            focus_window(self.hwnd)
            control.set_focus()
            WinInput.key(0x20)  # Space
            return True
        except Exception:
            pass

        # Si WinUI oculta todos los patrones, enviamos el clic directamente al
        # HWND bajo el centro del control. Tampoco altera la posición del mouse.
        try:
            rect = control.rectangle()
            if WinInput.background_click((rect.left + rect.right) / 2, (rect.top + rect.bottom) / 2):
                return True
        except Exception:
            pass

        try:
            control.click()  # Mensaje de control, no click_input.
            return True
        except Exception:
            pass

        if self.config.get("allow_physical_mouse_fallback", False):
            try:
                control.click_input()
                return True
            except Exception:
                pass
        return False

    def _uia_click(self, patterns: list[str]) -> bool:
        try:
            from pywinauto import Desktop
            window = Desktop(backend="uia").window(handle=self.hwnd)
            candidates = window.descendants()
            priority = {
                "Button": 0, "RadioButton": 0, "CheckBox": 0,
                "MenuItem": 0, "SplitButton": 0, "TabItem": 1,
            }
            candidates.sort(key=lambda c: priority.get(c.element_info.control_type or "", 5))
            matched = False
            for pattern in patterns:
                rx = re.compile(pattern, re.I)
                for control in candidates:
                    name = control.element_info.name or ""
                    if rx.search(name) and control.is_visible() and control.is_enabled():
                        matched = True
                        if not self._invoke_uia(control):
                            continue
                        time.sleep(0.18)
                        return True
            if matched:
                self.log("Paint expone el control solicitado, pero rechazó todos sus métodos de activación sin mouse")
        except Exception as exc:
            self.log(f"UI Automation: {exc}")
        return False

    def ensure_toolbar(self) -> None:
        """Despliega la barra compacta de Paint si actualmente está oculta."""
        state, control = self._toolbar_state()
        if state == "expanded":
            self._toolbar_expanded = True
            return
        if state == "collapsed" and control is not None:
            if not self._invoke_uia(control):
                self.log("Encontré Mostrar barra de herramientas, pero Paint rechazó su activación")
                return
            time.sleep(.4)
            self._toolbar_expanded = True
            self.active_tool = ""
            self.style.width = None
            self.log("Barra de herramientas de Paint desplegada")

    def _toolbar_state(self, attempts: int = 4) -> tuple[str, Any | None]:
        """Devuelve el estado observando la poscondición; tolera errores COM transitorios."""
        show_patterns = [r"^show.*toolbars?$", r"^mostrar.*barra.*herramientas$"]
        hide_patterns = [r"^hide.*toolbars?$", r"^ocultar.*barra.*herramientas$"]
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                from pywinauto import Desktop
                window = Desktop(backend="uia").window(handle=self.hwnd)
                for control in window.descendants(control_type="Button"):
                    if not control.is_visible():
                        continue
                    name = control.element_info.name or ""
                    if any(re.search(pattern, name, re.I) for pattern in show_patterns):
                        return "collapsed", control
                    if any(re.search(pattern, name, re.I) for pattern in hide_patterns):
                        return "expanded", control
                return "fixed", None
            except Exception as exc:
                last_error = exc
                time.sleep(.12 * (attempt + 1))
        if last_error:
            self.log(f"Paint UIA tardó en responder: {last_error}")
        return "unknown", None

    def collapse_toolbar(self) -> bool:
        """Contrae la barra flotante antes de usar coordenadas del lienzo."""
        state, control = self._toolbar_state()
        if state in {"collapsed", "fixed"}:
            self._toolbar_expanded = False
            return True
        if state == "expanded" and control is not None:
            invoked = self._invoke_uia(control)
            time.sleep(.45)
            verified, _ = self._toolbar_state(attempts=5)
            if verified in {"collapsed", "fixed"}:
                self._toolbar_expanded = False
                self.log("Barra de herramientas de Paint contraída")
                return True
            if not invoked:
                self.log("Paint rechazó el botón Ocultar; pruebo Escape")

        # Escape cierra la superficie flotante de Paint sin usar el mouse. El
        # error COM de eventos puede aparecer incluso cuando el panel sí cerró.
        focus_window(self.hwnd)
        WinInput.key(0x1B)
        time.sleep(.45)
        verified, _ = self._toolbar_state(attempts=5)
        if verified in {"collapsed", "fixed"}:
            self._toolbar_expanded = False
            self.log("Barra de herramientas de Paint contraída")
            return True
        if verified == "unknown":
            # No convertimos un fallo del observador UIA en un falso fallo de la
            # acción. Escape ya fue enviado y damos tiempo a que WinUI se estabilice.
            self._toolbar_expanded = False
            self.log("Paint no confirmó la barra por UIA; continúo tras cerrarla con Escape")
            return True
        self.log("La barra de herramientas continúa visible después de dos métodos de cierre")
        return False

    def select_tool(self, tool: str) -> None:
        if tool == self.active_tool:
            return
        self.ensure_toolbar()
        names = {
            "brush": [r"^pencil$", r"^l[aá]piz$", r"^brush(es)?$", r"^pincel(es)?$"],
            "eraser": [r"eraser", r"borrador", r"goma"],
        }
        ok = self._uia_click(names[tool]) or self._click_control(f"tool_{tool}")
        if not ok:
            raise RuntimeError(f"No pude seleccionar {tool}. Ejecuta Calibrar Paint (completo).")
        self.active_tool = tool
        # Paint puede conservar un grosor distinto por herramienta.
        self.style.width = None

    def set_width(self, width: int) -> None:
        width = int(min(4, max(1, width)))
        if width == self.style.width:
            return
        focus_window(self.hwnd)
        # Ctrl+- reduce un píxel y Ctrl++ aumenta uno. Bajamos sobradamente hasta
        # el mínimo y subimos a 1/3/5/8 px: determinista incluso si Paint arrancó
        # con un grosor desconocido.
        for _ in range(48):
            WinInput.chord(VK_CONTROL, VK_SUBTRACT)
            time.sleep(.004)
        target_px = {1: 1, 2: 3, 3: 5, 4: 8}[width]
        for _ in range(target_px - 1):
            WinInput.chord(VK_CONTROL, VK_ADD)
            time.sleep(.012)
        self.style.width = width

    def set_eraser_size(self, size: int) -> int:
        """Fija de forma determinista el diámetro de la goma en píxeles de Paint."""
        size = int(min(64, max(6, size)))
        if self.active_tool != "eraser":
            raise RuntimeError("Selecciona la goma antes de configurar su tamaño")
        if size == self.eraser_size:
            return size
        focus_window(self.hwnd)
        # Reinicia al mínimo conocido y crece hasta el tamaño solicitado. Paint
        # no expone este valor de forma fiable mediante UI Automation.
        for _ in range(80):
            WinInput.chord(VK_CONTROL, VK_SUBTRACT)
            time.sleep(.003)
        for _ in range(size - 1):
            WinInput.chord(VK_CONTROL, VK_ADD)
            time.sleep(.006)
        self.eraser_size = size
        self.log(f"Goma configurada a {size} px")
        return size

    def _select_palette_color(self, requested: str, rgb: tuple[int, int, int]) -> bool:
        """Selecciona el tono normal más cercano sin abrir el editor RGB de WinUI."""
        if self.palette_control_available is False:
            return False

        # Los nombres accesibles cambian con el idioma, pero los botones de la
        # paleta conservan nombres humanos. Aproximar aquí es preferible a abrir
        # el editor personalizado: para una pizarra prima que Paint no se caiga.
        palette: list[tuple[tuple[int, int, int], tuple[str, ...]]] = [
            ((0, 0, 0), ("black", "negro")),
            ((128, 128, 128), ("gray", "grey", "gris")),
            ((128, 0, 0), ("dark red", "rojo oscuro")),
            ((255, 0, 0), ("red", "rojo")),
            ((255, 128, 0), ("orange", "naranja")),
            ((255, 255, 0), ("yellow", "amarillo")),
            ((0, 128, 0), ("green", "verde")),
            ((0, 255, 255), ("turquoise", "turquesa", "cyan", "cian")),
            ((0, 0, 255), ("blue", "azul")),
            ((128, 0, 128), ("purple", "morado", "purpura", "violeta")),
            ((128, 64, 0), ("brown", "marron", "cafe")),
            ((255, 128, 192), ("pink", "rosa")),
        ]
        chosen_rgb, aliases = min(
            palette,
            key=lambda item: sum((a - b) ** 2 for a, b in zip(rgb, item[0])),
        )
        chosen_hex = "".join(f"{channel:02x}" for channel in chosen_rgb)

        try:
            from pywinauto import Desktop
            window = Desktop(backend="uia").window(handle=self.hwnd)
            scored: list[tuple[int, Any]] = []
            for control in window.descendants():
                if control.element_info.control_type not in {"Button", "RadioButton"}:
                    continue
                if not control.is_visible() or not control.is_enabled():
                    continue
                name = normalize_text(control.element_info.name or "")
                if not name or any(word in name.split() for word in ("edit", "editar", "custom", "personalizado")):
                    continue
                words = set(name.split())
                score: int | None = None
                if chosen_hex in name.replace(" ", ""):
                    score = 0
                for alias in aliases:
                    normalized_alias = normalize_text(alias)
                    if name == normalized_alias:
                        candidate_score = 1
                    elif normalized_alias in name:
                        candidate_score = 6 + len(words)
                    else:
                        continue
                    score = candidate_score if score is None else min(score, candidate_score)
                if score is None:
                    continue
                # Evita escoger "azul claro" cuando existe el azul estándar.
                qualifiers = {"light", "claro", "clara", "dark", "oscuro", "oscura"}
                if words & qualifiers and not any(normalize_text(a) == name for a in aliases):
                    score += 20
                scored.append((score, control))

            for _score, control in sorted(scored, key=lambda item: item[0]):
                if self._invoke_uia(control):
                    time.sleep(.22)
                    self.palette_control_available = True
                    self.style.color = requested
                    return True
        except Exception as exc:
            # Un árbol UIA transitorio no debe matar el turno de dibujo.
            if not self._palette_warning_shown:
                self.log(f"La paleta de Paint todavía no respondió ({exc}); mantengo su color actual.")
                self._palette_warning_shown = True

        self.palette_control_available = False
        return False

    def set_color(self, color: str) -> None:
        color = "#" + color.strip().lstrip("#").upper()
        rgb = hex_rgb(color)
        # Nunca permitimos blanco/casi blanco sobre el lienzo blanco.
        if min(rgb) > 235:
            color, rgb = "#1F2937", (31, 41, 55)
        if color == self.style.color.upper() and self.active_tool:
            return
        self.ensure_toolbar()
        if self._select_palette_color(color, rgb):
            return

        if not self.config.get("custom_colors", False):
            if not self._palette_warning_shown:
                self.log(
                    "Paint no publicó los botones de su paleta por accesibilidad; "
                    "dibujo con el color que ya esté seleccionado."
                )
                self._palette_warning_shown = True
            # Recordamos que ya intentamos aplicar este estilo. El color físico
            # sigue siendo el actual de Paint, pero evitamos desplegar la paleta
            # otra vez para cada trazo consecutivo del mismo color solicitado.
            self.style.color = color
            return
        if self.color_control_available is False:
            self.style.color = color
            return

        # Ruta avanzada/experimental. Está desactivada por defecto porque el
        # editor de color de algunas versiones modernas de Paint se cierra o
        # incluso derriba Paint cuando se automatiza durante su animación WinUI.
        opened = self._uia_click([r"edit.*colou?r", r"editar.*color"]) or self._click_control("edit_colors")
        if not opened:
            self.color_control_available = False
            if not self._color_warning_shown:
                self.log("Paint no expone Editar colores; continúo dibujando con su color actual.")
                self._color_warning_shown = True
            return
        try:
            from pywinauto import Desktop
            deadline = time.time() + 3
            surface = None
            while time.time() < deadline:
                for candidate in Desktop(backend="uia").windows():
                    title = candidate.element_info.name or ""
                    if re.search(r"edit.*colou?r|editar.*color", title, re.I):
                        surface = candidate
                        break
                if surface:
                    break
                # Paint moderno abre el editor como panel hijo, no como diálogo.
                main = Desktop(backend="uia").window(handle=self.hwnd)
                visible_edits = [e for e in main.descendants(control_type="Edit") if e.is_enabled() and e.is_visible()]
                if visible_edits:
                    surface = main
                    break
                time.sleep(0.1)
            if surface is None:
                raise RuntimeError("No apareció el diálogo Editar colores")
            edits = [e for e in surface.descendants(control_type="Edit") if e.is_enabled() and e.is_visible()]
            if len(edits) >= 3:
                # En Paint clásico, R/G/B son los tres últimos campos.
                for edit, value in zip(edits[-3:], rgb):
                    edit.set_edit_text(str(value))
            elif len(edits) == 1:
                # Paint moderno suele exponer un único campo hexadecimal.
                edits[0].set_edit_text(color)
            else:
                raise RuntimeError("Paint no expuso ningún campo de color editable")
            buttons = [b for b in surface.descendants(control_type="Button") if b.is_visible()]
            ok = next((b for b in buttons if re.search(r"^(ok|aceptar)$", b.element_info.name or "", re.I)), None)
            if not ok:
                raise RuntimeError("No encontré Aceptar en Editar colores")
            if not self._invoke_uia(ok):
                raise RuntimeError("Paint rechazó el botón Aceptar del editor de colores")
            time.sleep(0.15)
            self.style.color = color
            self.color_control_available = True
        except Exception as exc:
            WinInput.key(0x1B)  # Escape
            self.color_control_available = False
            if not self._color_warning_shown:
                self.log(f"Color personalizado no disponible ({exc}); dibujo con el color actual de Paint.")
                self._color_warning_shown = True

    def prepare(self, color: str, width: int, tool: str = "brush") -> None:
        self.ensure_attached()
        try:
            self.select_tool(tool)
            self.set_color(color)
            self.set_width(width)
        finally:
            collapsed = self.collapse_toolbar()
        if not collapsed:
            raise RuntimeError("La barra de herramientas sigue cubriendo el lienzo; dibujo cancelado")
        focus_window(self.hwnd)

    def _screen_points(self, points: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
        return [self.board_to_screen(x, y) for x, y in points]

    def line(self, x1: float, y1: float, x2: float, y2: float, color: str, width: int, speed: float) -> None:
        self.prepare(color, width)
        points = []
        n = max(2, int(math.dist((x1, y1), (x2, y2)) / 18))
        bow = random.uniform(-1.8, 1.8)
        for i in range(n + 1):
            u = i / n
            points.append((x1 + (x2-x1)*u, y1 + (y2-y1)*u + math.sin(math.pi*u)*bow))
        self.motion.stroke(self._screen_points(points), speed)

    def polyline(self, points: list[tuple[float, float]], color: str, width: int, speed: float) -> None:
        self.prepare(color, width)
        self.motion.stroke(self._screen_points(points), speed)

    @staticmethod
    def _smooth_freehand(points: list[tuple[float, float]], closed: bool) -> list[tuple[float, float]]:
        """Interpola anclas de la IA con Catmull–Rom sin cambiar su intención."""
        if len(points) < 2:
            return points
        anchors = [(min(VIRTUAL_W, max(0.0, x)), min(VIRTUAL_H, max(0.0, y))) for x, y in points]
        if closed and math.dist(anchors[0], anchors[-1]) > .5:
            anchors.append(anchors[0])
        result: list[tuple[float, float]] = [anchors[0]]
        count = len(anchors)
        for index in range(count - 1):
            p0 = anchors[index - 1] if index > 0 else (anchors[-2] if closed and count > 3 else anchors[index])
            p1 = anchors[index]
            p2 = anchors[index + 1]
            p3 = anchors[index + 2] if index + 2 < count else (anchors[1] if closed and count > 3 else p2)
            samples = max(3, min(36, int(math.dist(p1, p2) / 5.0) + 1))
            for sample in range(1, samples + 1):
                t = sample / samples
                t2, t3 = t*t, t*t*t
                x = .5 * (
                    2*p1[0] + (-p0[0] + p2[0])*t
                    + (2*p0[0] - 5*p1[0] + 4*p2[0] - p3[0])*t2
                    + (-p0[0] + 3*p1[0] - 3*p2[0] + p3[0])*t3
                )
                y = .5 * (
                    2*p1[1] + (-p0[1] + p2[1])*t
                    + (2*p0[1] - 5*p1[1] + 4*p2[1] - p3[1])*t2
                    + (-p0[1] + 3*p1[1] - 3*p2[1] + p3[1])*t3
                )
                result.append((min(VIRTUAL_W, max(0.0, x)), min(VIRTUAL_H, max(0.0, y))))
        return result

    def freehand(self, strokes: list[dict[str, Any]], color: str, width: int, speed: float) -> None:
        """Ejecuta los trazos ordenados que diseñó el modelo con una sola preparación."""
        if not isinstance(strokes, list) or not 1 <= len(strokes) <= 16:
            raise ValueError("freehand necesita entre 1 y 16 trazos")
        planned: list[list[tuple[float, float]]] = []
        anchor_count = 0
        for stroke in strokes:
            raw_points = stroke.get("points", []) if isinstance(stroke, dict) else []
            if not isinstance(raw_points, list) or not 2 <= len(raw_points) <= 64:
                raise ValueError("cada trazo libre necesita entre 2 y 64 puntos")
            anchors: list[tuple[float, float]] = []
            for point in raw_points:
                if not isinstance(point, dict) or "x" not in point or "y" not in point:
                    raise ValueError("cada punto debe contener x e y")
                anchors.append((float(point["x"]), float(point["y"])))
            anchor_count += len(anchors)
            planned.append(self._smooth_freehand(anchors, bool(stroke.get("closed", False))))
        self.log(f"Trayectoria IA: {len(planned)} trazos, {anchor_count} anclas; ejecución continua a 120 Hz")
        self.prepare(color, width)
        for points in planned:
            self.motion.stroke(self._screen_points(points), speed)

    def arrow(self, x1: float, y1: float, x2: float, y2: float, color: str, width: int, speed: float) -> None:
        self.line(x1, y1, x2, y2, color, width, speed)
        angle = math.atan2(y2-y1, x2-x1)
        size = 24 + width * 3
        for delta in (2.55, -2.55):
            self.line(x2, y2, x2 + math.cos(angle+delta)*size, y2 + math.sin(angle+delta)*size, color, width, speed)

    def rectangle(self, x: float, y: float, w: float, h: float, color: str, width: int, speed: float) -> None:
        self.polyline([(x,y),(x+w,y),(x+w,y+h),(x,y+h),(x,y)], color, width, speed)

    def ellipse(self, x: float, y: float, w: float, h: float, color: str, width: int, speed: float) -> None:
        pts = []
        for i in range(81):
            a = math.tau * i / 80
            pts.append((x+w/2 + math.cos(a)*w/2, y+h/2 + math.sin(a)*h/2))
        self.polyline(pts, color, width, speed)

    def point(self, x: float, y: float, color: str, width: int) -> None:
        radius = 8 + width * 2
        self.ellipse(x-radius, y-radius, radius*2, radius*2, color, width, 1.25)

    def write(self, text: str, x: float, y: float, height: float, max_width: float,
              color: str, width: int, speed: float) -> None:
        # La fuente vectorial no contiene emojis. Antes se convertían en '?',
        # ensuciando la pizarra con símbolos que el modelo creía haber dibujado.
        replacements = {
            "→": "->", "←": "<-", "⇒": "=>", "⇐": "<=",
            "–": "-", "—": "-", "“": '"', "”": '"', "’": "'",
        }
        for source, target in replacements.items():
            text = text.replace(source, target)
        normalized = unicodedata.normalize("NFD", text)
        normalized = "".join(c for c in normalized if unicodedata.category(c) != "Mn")
        supported = "".join(c for c in normalized if c in " \n\t" or c.upper() in G)
        if supported != normalized:
            self.log("Texto de pizarra: omití emojis o símbolos que la fuente no puede dibujar")
        text = supported
        if not text.strip():
            raise ValueError("El texto no contiene caracteres compatibles con la fuente de pizarra")
        self.prepare(color, width)
        height = min(90.0, max(14.0, height))
        advance = height * 0.72
        origin_x, cursor_x, cursor_y = x, x, y
        for char in text.upper():
            if self.cancel.is_set():
                raise CancelledDrawing()
            if char == "\n" or (cursor_x + advance > origin_x + max_width and char == " "):
                cursor_x = origin_x
                cursor_y += height * 1.42
                continue
            if cursor_x + advance > origin_x + max_width:
                cursor_x = origin_x
                cursor_y += height * 1.42
            if char == " ":
                cursor_x += advance * .72
                continue
            glyph = G.get(char, G.get("?", []))
            slant = random.uniform(-0.035, 0.055)
            sx = height * .62 * random.uniform(.96, 1.04)
            sy = height * random.uniform(.97, 1.03)
            for stroke in glyph:
                pts = []
                for gx, gy in stroke:
                    px = cursor_x + gx*sx + gy*height*slant + random.uniform(-.35,.35)
                    py = cursor_y + gy*sy + random.uniform(-.3,.3)
                    pts.append(self.board_to_screen(px, py))
                self.motion.stroke(pts, speed * random.uniform(.92, 1.07))
            cursor_x += advance * random.uniform(.96, 1.04)

    def erase(self, x: float, y: float, w: float, h: float, size: int,
              speed: float, reason: str) -> None:
        x, y, w, h = map(float, (x, y, w, h))
        if w <= 0 or h <= 0:
            raise ValueError("La región de borrado debe tener ancho y alto positivos")
        x = min(VIRTUAL_W, max(0.0, x))
        y = min(VIRTUAL_H, max(0.0, y))
        w = min(w, VIRTUAL_W - x)
        h = min(h, VIRTUAL_H - y)
        # erase es para cirugía local, no para reiniciar una página. Esta barrera
        # evita que una alucinación del auditor barra toda la explicación.
        if w > 500 or h > 220 or w * h > VIRTUAL_W * VIRTUAL_H * .12:
            raise ValueError(
                "Borrado demasiado grande: erase solo corrige una zona local; "
                "usa next_page para continuar o clear si el alumno pidió limpiar todo"
            )
        size = int(min(64, max(6, size)))
        self.log(f"Corrección con goma: {reason.strip()[:120]} ({w:.0f}x{h:.0f}, {size}px)")
        try:
            self.select_tool("eraser")
            self.set_eraser_size(size)
        finally:
            collapsed = self.collapse_toolbar()
        if not collapsed:
            raise RuntimeError("La barra de herramientas sigue cubriendo el lienzo; borrado cancelado")
        canvas_l, canvas_t, canvas_r, canvas_b = self.canvas_rect()
        scale_x = max(.1, (canvas_r - canvas_l) / VIRTUAL_W)
        scale_y = max(.1, (canvas_b - canvas_t) / VIRTUAL_H)
        footprint_x = size / scale_x
        footprint_y = size / scale_y
        left = x + min(w / 2, footprint_x * .46)
        right = x + w - min(w / 2, footprint_x * .46)
        top = y + min(h / 2, footprint_y * .46)
        bottom = y + h - min(h / 2, footprint_y * .46)
        spacing = max(2.0, footprint_y * .42)
        lines = max(1, int(math.ceil(max(0.0, bottom - top) / spacing)))
        points = []
        for i in range(lines + 1):
            yy = top + (bottom - top) * i / lines
            points.append((left if i % 2 == 0 else right, yy))
            points.append((right if i % 2 == 0 else left, yy))
        self.motion.stroke(self._screen_points(points), speed * 1.8)
        self.active_tool = "eraser"
        # Nunca dejamos Paint aparcado en la goma: el siguiente gesto manual o
        # automático vuelve a partir del lápiz.
        try:
            self.select_tool("brush")
        except Exception as exc:
            self.log(f"No pude restaurar el lápiz tras borrar: {exc}")
        finally:
            self.collapse_toolbar()

    def clear(self) -> None:
        self.ensure_attached()
        focus_window(self.hwnd)
        WinInput.chord(VK_CONTROL, ord("A"))
        time.sleep(.12)
        WinInput.key(0x2E)  # Delete
        WinInput.key(0x1B)
        time.sleep(.15)

    def next_page(self, label: str) -> str:
        """Archiva el lienzo actual y abre una página limpia para una clase larga."""
        self.ensure_attached()
        from PIL import ImageGrab
        folder = app_data_dir() / "lesson_pages" / time.strftime("%Y-%m-%d")
        folder.mkdir(parents=True, exist_ok=True)
        safe_label = re.sub(r"[^A-Za-z0-9_-]+", "-", label.strip()).strip("-") or "lesson"
        stamp = time.strftime("%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
        path = folder / f"{stamp}-{safe_label[:48]}.png"
        image = ImageGrab.grab(bbox=tuple(map(int, self.canvas_rect())), all_screens=True)
        image.save(path, format="PNG")
        self.clear()
        self.log(f"Página anterior archivada: {path}")
        return str(path)

    def undo(self) -> None:
        self.ensure_attached()
        focus_window(self.hwnd)
        WinInput.chord(VK_CONTROL, ord("Z"))
        time.sleep(.12)

    def redo(self) -> None:
        self.ensure_attached()
        WinInput.chord(VK_CONTROL, ord("Y"))
        time.sleep(.12)

    def zoom(self, direction: str, steps: int) -> None:
        self.ensure_attached()
        key = VK_PRIOR if direction == "in" else VK_NEXT
        for _ in range(min(8, max(1, int(steps)))):
            WinInput.chord(VK_CONTROL, key)
            time.sleep(.08)

    def toggle_grid(self) -> None:
        self.ensure_attached()
        WinInput.chord(VK_CONTROL, ord("G"))
        time.sleep(.12)

    def screenshot_data_url(self) -> str | None:
        try:
            from PIL import ImageGrab
            image = ImageGrab.grab(bbox=tuple(map(int, self.canvas_rect())), all_screens=True)
            # Conserva la letra fina y los cruces de trazos para el auditor visual.
            image.thumbnail((1400, 900))
            stream = io.BytesIO()
            image.convert("RGB").save(stream, format="JPEG", quality=88, optimize=True)
            return "data:image/jpeg;base64," + base64.b64encode(stream.getvalue()).decode("ascii")
        except Exception as exc:
            self.log(f"Captura omitida: {exc}")
            return None

    def diagnose(self) -> Path:
        """Vuelca el árbol UIA de Paint para adaptar automáticamente otras versiones."""
        self.ensure_attached()
        self.ensure_toolbar()
        from pywinauto import Desktop
        window = Desktop(backend="uia").window(handle=self.hwnd)
        rows = [
            f"Paint Professor UIA — {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"window={self.rect()}",
            f"canvas={tuple(round(v, 1) for v in self.canvas_rect())}",
            f"synthetic_pen={self.motion.pen.available} error={self.motion.pen.error!r}",
            f"palette_control_available={self.palette_control_available}",
            f"custom_colors_enabled={bool(self.config.get('custom_colors', False))}",
            "",
            "TYPE\tNAME\tAUTOMATION_ID\tRECT\tENABLED\tVISIBLE",
        ]
        for control in window.descendants():
            try:
                info = control.element_info
                rect = control.rectangle()
                rows.append(
                    f"{info.control_type}\t{(info.name or '').replace(chr(9), ' ')}\t"
                    f"{info.automation_id or ''}\t"
                    f"({rect.left},{rect.top},{rect.right},{rect.bottom})\t"
                    f"{control.is_enabled()}\t{control.is_visible()}"
                )
            except Exception as exc:
                rows.append(f"<unreadable>\t{exc}")
        path = app_data_dir() / "paint_uia_diagnostic.txt"
        path.write_text("\n".join(rows), encoding="utf-8")
        self.collapse_toolbar()
        self.log(f"Diagnóstico de Paint guardado: {path}")
        return path

    def close(self) -> None:
        self.motion.close()


class NativeBoardController:
    """Pizarra vectorial propia: no usa Paint, SendInput ni el cursor del sistema."""

    backend_name = "native"

    def __init__(self, config: dict[str, Any], cancel: threading.Event,
                 log: Callable[[str], None], root: Any = None,
                 dispatch: Callable[[Callable[[], None]], None] | None = None):
        from PIL import Image
        self.config, self.cancel, self.log = config, cancel, log
        self.root, self.dispatch = root, dispatch or (lambda fn: fn())
        self.ui_thread_id = threading.get_ident()
        self.lock = threading.RLock()
        self.image = Image.new("RGB", (int(VIRTUAL_W), int(VIRTUAL_H)), "white")
        self.objects: list[dict[str, Any]] = []
        self.undo_stack: list[tuple[Any, list[dict[str, Any]]]] = []
        self.redo_stack: list[tuple[Any, list[dict[str, Any]]]] = []
        self.archived_pages: list[str] = []
        self.page_number = 1
        self.next_object_id = 1
        self.last_report: dict[str, Any] = {
            "backend": "native", "status": "ready", "object_count": 0,
        }
        self.cursor: tuple[float, float] | None = None
        self.dirty = True
        self.closed = False
        self.window = None
        self.canvas = None
        self.photo = None
        self.page_var = None
        self.object_var = None
        self.grid_visible = False
        self.zoom_level = 1.0
        self.on_change: Callable[[], None] | None = None
        self.on_frame: Callable[[], None] | None = None
        if self.root is not None:
            self._ui_sync(self._create_window)

    def _ui_sync(self, fn: Callable[[], Any]) -> Any:
        if threading.get_ident() == self.ui_thread_id:
            return fn()
        done = threading.Event()
        result: dict[str, Any] = {}
        def wrapped() -> None:
            try:
                result["value"] = fn()
            except Exception as exc:
                result["error"] = exc
            finally:
                done.set()
        self.dispatch(wrapped)
        while not done.wait(.05):
            if self.closed:
                raise RuntimeError("La pizarra nativa está cerrada")
        if "error" in result:
            raise result["error"]
        return result.get("value")

    def _create_window(self) -> None:
        import tkinter as tk
        top = tk.Toplevel(self.root)
        top.title("TeachAI — Pizarra nativa")
        top.geometry("1240x900")
        top.minsize(760, 560)
        top.configure(bg="#111827")
        top.protocol("WM_DELETE_WINDOW", top.withdraw)
        header = tk.Frame(top, bg="#111827", padx=18, pady=10)
        header.pack(fill="x")
        tk.Label(header, text="TEACHAI BOARD", bg="#111827", fg="#8EABFF",
                 font=("Segoe UI Semibold", 11)).pack(side="left")
        self.page_var = tk.StringVar(value="Página 1")
        self.object_var = tk.StringVar(value="0 objetos")
        tk.Label(header, textvariable=self.page_var, bg="#111827", fg="#F8FAFC",
                 font=("Segoe UI Semibold", 10)).pack(side="left", padx=(24, 10))
        tk.Label(header, textvariable=self.object_var, bg="#111827", fg="#94A3B8",
                 font=("Segoe UI", 10)).pack(side="left")
        tk.Label(header, text="Cursor IA independiente", bg="#111827", fg="#53D69D",
                 font=("Segoe UI", 9)).pack(side="right")
        self.canvas = tk.Canvas(top, bg="#1F2937", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=18, pady=(0, 18))
        self.window = top
        top.after(16, self._refresh)

    def _refresh(self) -> None:
        if self.closed or not self.window:
            return
        try:
            if self.dirty and self.canvas:
                from PIL import Image, ImageTk
                with self.lock:
                    source = self.image.copy()
                    cursor = self.cursor
                    page, count = self.page_number, len(self.objects)
                    self.dirty = False
                cw, ch = max(40, self.canvas.winfo_width()), max(40, self.canvas.winfo_height())
                ratio = min(cw / VIRTUAL_W, ch / VIRTUAL_H) * self.zoom_level
                dw, dh = max(1, int(VIRTUAL_W * ratio)), max(1, int(VIRTUAL_H * ratio))
                resized = source.resize((dw, dh), Image.Resampling.LANCZOS)
                self.photo = ImageTk.PhotoImage(resized)
                ox, oy = (cw - dw) / 2, (ch - dh) / 2
                self.canvas.delete("board-frame")
                self.canvas.create_image(ox, oy, image=self.photo, anchor="nw", tags="board-frame")
                if self.grid_visible:
                    for gx in range(0, 1001, 50):
                        x = ox + gx * ratio
                        self.canvas.create_line(x, oy, x, oy + dh, fill="#E5E7EB", tags="board-frame")
                    for gy in range(0, 701, 50):
                        y = oy + gy * ratio
                        self.canvas.create_line(ox, y, ox + dw, y, fill="#E5E7EB", tags="board-frame")
                if cursor is not None:
                    cx, cy = ox + cursor[0] * ratio, oy + cursor[1] * ratio
                    radius = 7
                    self.canvas.create_oval(cx-radius, cy-radius, cx+radius, cy+radius,
                                            fill="#4F7CFF", outline="white", width=2,
                                            tags="board-frame")
                    self.canvas.create_line(cx+5, cy+5, cx+15, cy+15, fill="#4F7CFF",
                                            width=3, tags="board-frame")
                self.page_var.set(f"Página {page}")
                self.object_var.set(f"{count} objetos")
            self.window.after(16, self._refresh)
        except Exception:
            if self.window:
                self.window.after(80, self._refresh)

    def _mark_dirty(self) -> None:
        self.dirty = True

    def attach(self, launch: bool = True) -> None:
        del launch
        if self.window is not None:
            self._ui_sync(lambda: (self.window.deiconify(), self.window.lift()))
        self.log("Pizarra nativa conectada — ningún cursor del sistema será utilizado")

    def _snapshot(self) -> None:
        with self.lock:
            self.undo_stack.append((self.image.copy(), copy.deepcopy(self.objects)))
            self.undo_stack = self.undo_stack[-50:]
            self.redo_stack.clear()

    @staticmethod
    def _bounds(points: Iterable[tuple[float, float]], padding: float = 0.0) -> dict[str, float]:
        pts = list(points)
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        return {
            "x": round(max(0.0, min(xs) - padding), 1),
            "y": round(max(0.0, min(ys) - padding), 1),
            "w": round(min(VIRTUAL_W, max(xs) + padding) - max(0.0, min(xs) - padding), 1),
            "h": round(min(VIRTUAL_H, max(ys) + padding) - max(0.0, min(ys) - padding), 1),
        }

    @staticmethod
    def _intersects(a: dict[str, float], b: dict[str, float], margin: float = 2.0) -> bool:
        return not (
            a["x"] + a["w"] + margin <= b["x"] or b["x"] + b["w"] + margin <= a["x"]
            or a["y"] + a["h"] + margin <= b["y"] or b["y"] + b["h"] + margin <= a["y"]
        )

    def _warnings_for(self, kind: str, bounds: dict[str, float]) -> list[str]:
        warnings: list[str] = []
        if bounds["x"] < 4 or bounds["y"] < 4 or bounds["x"] + bounds["w"] > 996 or bounds["y"] + bounds["h"] > 696:
            warnings.append("El objeto toca el margen de seguridad")
        if kind == "write":
            overlaps = [obj["id"] for obj in self.objects if obj.get("type") == "write"
                        and self._intersects(bounds, obj["bounds"], margin=4)]
            if overlaps:
                warnings.append("El texto se solapa con texto existente: " + ", ".join(overlaps))
        return warnings

    def _record(self, kind: str, bounds: dict[str, float], render: dict[str, Any],
                **metadata: Any) -> None:
        object_id = f"obj_{self.next_object_id}"
        self.next_object_id += 1
        warnings = self._warnings_for(kind, bounds)
        item = {"id": object_id, "type": kind, "bounds": bounds,
                "render": render, "page": self.page_number, **metadata}
        self.objects.append(item)
        self.last_report = {
            "backend": "native", "status": "ok" if not warnings else "warning",
            "object_id": object_id, "type": kind, "actual_bounds": bounds,
            "warnings": warnings, "object_count": len(self.objects),
        }
        self._mark_dirty()
        if self.on_change:
            try: self.on_change()
            except Exception: pass

    @staticmethod
    def _pixel_width(width: int) -> int:
        return {1: 2, 2: 3, 3: 5, 4: 8}.get(int(width), 3)

    def _draw_segment(self, start: tuple[float, float], end: tuple[float, float],
                      color: str, width: int) -> None:
        from PIL import ImageDraw
        with self.lock:
            ImageDraw.Draw(self.image).line([start, end], fill=color, width=width)
            self.cursor = end
            self.dirty = True
        if self.on_frame:
            try: self.on_frame()
            except Exception: pass

    def _animate_path(self, points: list[tuple[float, float]], color: str,
                      width: int, speed: float) -> None:
        if len(points) < 2:
            return
        pixel_width = self._pixel_width(width) if color.lower() != "#ffffff" else int(width)
        previous = points[0]
        self.cursor = previous
        for target in points[1:]:
            distance = max(.01, math.dist(previous, target))
            samples = max(1, int(distance / 3.2))
            origin = previous
            for index in range(1, samples + 1):
                if self.cancel.is_set():
                    raise CancelledDrawing()
                u = index / samples
                eased = u * u * (3 - 2 * u)
                current = (origin[0] + (target[0]-origin[0])*eased,
                           origin[1] + (target[1]-origin[1])*eased)
                self._draw_segment(previous, current, color, pixel_width)
                previous = current
                time.sleep(max(.0015, 1.0 / (150.0 * max(.4, speed))))
        self.cursor = None
        self._mark_dirty()

    def _animate_paths(self, paths: list[list[tuple[float, float]]], color: str,
                       width: int, speed: float) -> None:
        for path in paths:
            self._animate_path(path, color, width, speed)

    @staticmethod
    def _clean_text(text: str) -> str:
        replacements = {"→":"->", "←":"<-", "⇒":"=>", "⇐":"<=", "–":"-", "—":"-"}
        for source, target in replacements.items():
            text = text.replace(source, target)
        text = unicodedata.normalize("NFD", text)
        text = "".join(c for c in text if unicodedata.category(c) != "Mn")
        return "".join(c for c in text if c in " \n\t" or c.upper() in G)

    @staticmethod
    def _wrap_text(text: str, capacity: int) -> list[str]:
        lines: list[str] = []
        for paragraph in text.splitlines() or [text]:
            words = paragraph.split()
            current = ""
            for word in words:
                chunks = [word[i:i+capacity] for i in range(0, len(word), capacity)] or [""]
                for chunk in chunks:
                    candidate = chunk if not current else current + " " + chunk
                    if len(candidate) <= capacity:
                        current = candidate
                    else:
                        if current:
                            lines.append(current)
                        current = chunk
            lines.append(current)
        return lines or [""]

    def write(self, text: str, x: float, y: float, height: float, max_width: float,
              color: str, width: int, speed: float) -> None:
        text = self._clean_text(text).strip()
        if not text:
            raise ValueError("El texto no contiene caracteres compatibles")
        x, y = max(8.0, float(x)), max(8.0, float(y))
        available = min(max(80.0, float(max_width)), VIRTUAL_W - x - 8)
        requested_height = min(90.0, max(14.0, float(height)))
        longest_word = max((len(word) for word in text.split()), default=1)
        # Evita apilar una palabra larga en fragmentos verticales ilegibles.
        fit_height = available / max(1.0, longest_word * .72)
        actual_height = max(14.0, min(requested_height, fit_height))
        while True:
            capacity = max(2, int(available / (actual_height * .72)))
            lines = self._wrap_text(text, capacity)
            total_height = actual_height + max(0, len(lines)-1) * actual_height * 1.42
            if y + total_height <= VIRTUAL_H - 8 or actual_height <= 14:
                break
            actual_height = max(14.0, actual_height - 1)
        if y + total_height > VIRTUAL_H - 4:
            raise ValueError("El texto no cabe verticalmente; usa otra posición o next_page")
        paths: list[list[tuple[float, float]]] = []
        max_line_chars = 0
        for row, line_text in enumerate(lines):
            cursor_x = x
            cursor_y = y + row * actual_height * 1.42
            max_line_chars = max(max_line_chars, len(line_text))
            for char in line_text.upper():
                advance = actual_height * .72
                if char == " ":
                    cursor_x += advance * .72
                    continue
                glyph = G.get(char, [])
                slant = random.uniform(-.025, .035)
                sx, sy = actual_height * .62, actual_height
                for stroke in glyph:
                    paths.append([
                        (cursor_x + gx*sx + gy*actual_height*slant, cursor_y + gy*sy)
                        for gx, gy in stroke
                    ])
                cursor_x += advance
        bounds = {"x":round(x,1), "y":round(y,1),
                  "w":round(min(available, max_line_chars * actual_height * .72),1),
                  "h":round(total_height,1)}
        self._snapshot()
        self._animate_paths(paths, color, width, speed)
        self._record("write", bounds, {"paths":paths, "color":color, "width":width},
                     text=text, lines=lines, requested_height=requested_height,
                     actual_height=actual_height,
                     auto_fitted=abs(actual_height - requested_height) > .01)

    def line(self, x1: float, y1: float, x2: float, y2: float,
             color: str, width: int, speed: float) -> None:
        points = []
        bow = random.uniform(-1.4, 1.4)
        count = max(2, int(math.dist((x1,y1),(x2,y2))/16))
        for i in range(count + 1):
            u = i / count
            points.append((x1+(x2-x1)*u, y1+(y2-y1)*u + math.sin(math.pi*u)*bow))
        self._snapshot()
        self._animate_path(points, color, width, speed)
        self._record("line", self._bounds(points, width+2),
                     {"paths":[points], "color":color, "width":width})

    def arrow(self, x1: float, y1: float, x2: float, y2: float,
              color: str, width: int, speed: float) -> None:
        angle, size = math.atan2(y2-y1, x2-x1), 22 + width*3
        paths = [[(x1,y1),(x2,y2)]] + [
            [(x2,y2),(x2+math.cos(angle+d)*size, y2+math.sin(angle+d)*size)]
            for d in (2.55, -2.55)
        ]
        self._snapshot()
        self._animate_paths(paths, color, width, speed)
        self._record("arrow", self._bounds([p for path in paths for p in path], width+2),
                     {"paths":paths, "color":color, "width":width})

    def rectangle(self, x: float, y: float, w: float, h: float,
                  color: str, width: int, speed: float) -> None:
        if w <= 0 or h <= 0 or x < 0 or y < 0 or x+w > VIRTUAL_W or y+h > VIRTUAL_H:
            raise ValueError("El rectángulo debe caber dentro de 1000x700")
        path = [(x,y),(x+w,y),(x+w,y+h),(x,y+h),(x,y)]
        self._snapshot()
        self._animate_path(path, color, width, speed)
        self._record("rectangle", {"x":x,"y":y,"w":w,"h":h},
                     {"paths":[path], "color":color, "width":width})

    def ellipse(self, x: float, y: float, w: float, h: float,
                color: str, width: int, speed: float) -> None:
        if w <= 0 or h <= 0 or x < 0 or y < 0 or x+w > VIRTUAL_W or y+h > VIRTUAL_H:
            raise ValueError("La elipse debe caber dentro de 1000x700")
        path = [(x+w/2+math.cos(math.tau*i/96)*w/2,
                 y+h/2+math.sin(math.tau*i/96)*h/2) for i in range(97)]
        self._snapshot()
        self._animate_path(path, color, width, speed)
        self._record("ellipse", {"x":x,"y":y,"w":w,"h":h},
                     {"paths":[path], "color":color, "width":width})

    def point(self, x: float, y: float, color: str, width: int) -> None:
        radius = 10 + width*2
        self.ellipse(x-radius, y-radius, radius*2, radius*2, color, width, 1.4)

    def freehand(self, strokes: list[dict[str, Any]], color: str,
                 width: int, speed: float) -> None:
        if not 1 <= len(strokes) <= 16:
            raise ValueError("freehand necesita entre 1 y 16 trazos")
        paths: list[list[tuple[float, float]]] = []
        for stroke in strokes:
            anchors = [(float(p["x"]), float(p["y"])) for p in stroke.get("points", [])]
            if len(anchors) < 2:
                raise ValueError("Cada trazo necesita al menos dos anclas")
            paths.append(PaintController._smooth_freehand(anchors, bool(stroke.get("closed", False))))
        self._snapshot()
        self._animate_paths(paths, color, width, speed)
        self._record("freehand", self._bounds([p for path in paths for p in path], width+2),
                     {"paths":paths, "color":color, "width":width},
                     stroke_count=len(paths), anchor_count=sum(len(s.get("points",[])) for s in strokes))

    def math_formula(self, latex: str, x: float, y: float, max_width: float,
                     max_height: float, color: str) -> None:
        """Compone notación matemática real (fracciones, raíces, matrices, integrales…)."""
        latex = str(latex).strip()
        if not latex or len(latex) > 500:
            raise ValueError("La fórmula está vacía o es demasiado larga")
        x, y = float(x), float(y)
        max_width, max_height = float(max_width), float(max_height)
        if x < 4 or y < 4 or max_width < 30 or max_height < 20:
            raise ValueError("Región de fórmula inválida")
        from matplotlib.mathtext import math_to_image
        from PIL import Image
        stream = io.BytesIO()
        expression = latex if latex.startswith("$") and latex.endswith("$") else f"${latex}$"
        try:
            math_to_image(expression, stream, dpi=220, format="png", color=color)
        except Exception as exc:
            raise ValueError(f"LaTeX matemático no válido: {exc}") from exc
        stream.seek(0)
        rendered = Image.open(stream).convert("RGBA")
        scale = min(max_width/rendered.width, max_height/rendered.height, 1.0)
        size = (max(1,int(rendered.width*scale)),max(1,int(rendered.height*scale)))
        rendered = rendered.resize(size, Image.Resampling.LANCZOS)
        if x+size[0] > VIRTUAL_W-4 or y+size[1] > VIRTUAL_H-4:
            raise ValueError("La fórmula no cabe en el espacio indicado")
        self._snapshot()
        with self.lock:
            base = self.image.convert("RGBA")
            base.alpha_composite(rendered,(int(x),int(y)))
            self.image = base.convert("RGB")
        bounds={"x":x,"y":y,"w":float(size[0]),"h":float(size[1])}
        self._record("math_formula",bounds,{"latex":latex,"color":color},latex=latex)

    def math_matrix(self, rows: list[list[str]], x: float, y: float, cell_width: float,
                    cell_height: float, brackets: str, color: str) -> None:
        from PIL import ImageDraw
        if not rows or len(rows)>8 or any(not row or len(row)>8 for row in rows):
            raise ValueError("La matriz debe tener entre 1x1 y 8x8 elementos")
        columns=max(len(row) for row in rows)
        if any(len(row)!=columns for row in rows): raise ValueError("Todas las filas deben tener igual longitud")
        x,y,cw,ch=float(x),float(y),float(cell_width),float(cell_height)
        w,h=columns*cw+34,len(rows)*ch+12
        if cw<28 or ch<24 or x<4 or y<4 or x+w>996 or y+h>696: raise ValueError("La matriz no cabe o sus celdas son demasiado pequeñas")
        self._snapshot()
        with self.lock:
            draw=ImageDraw.Draw(self.image);font=self._technical_font(max(14,int(ch*.48)))
            for row_i,row in enumerate(rows):
                for col_i,value in enumerate(row):
                    cx=x+17+col_i*cw+cw/2;cy=y+6+row_i*ch+ch/2
                    draw.text((cx,cy),str(value),fill=color,font=font,anchor="mm")
            left,right=x+9,x+w-9;top,bottom=y+4,y+h-4
            if brackets=="parentheses":
                draw.arc((left-8,top,left+15,bottom),90,270,fill=color,width=3)
                draw.arc((right-15,top,right+8,bottom),270,90,fill=color,width=3)
            else:
                draw.line((left+8,top,left,top,left,bottom,left+8,bottom),fill=color,width=3)
                draw.line((right-8,top,right,top,right,bottom,right-8,bottom),fill=color,width=3)
        bounds={"x":x,"y":y,"w":w,"h":h}
        self._record("math_matrix",bounds,{"color":color},rows=copy.deepcopy(rows),brackets=brackets)

    @staticmethod
    def _safe_math_values(expression: str, xs: Any) -> Any:
        import numpy as np
        expression=expression.replace("^","**")
        tree=ast.parse(expression,mode="eval")
        allowed_nodes=(ast.Expression,ast.BinOp,ast.UnaryOp,ast.Call,ast.Name,ast.Load,
                       ast.Constant,ast.Add,ast.Sub,ast.Mult,ast.Div,ast.Pow,ast.Mod,
                       ast.USub,ast.UAdd)
        allowed_names={"x","sin","cos","tan","exp","log","sqrt","abs","pi","e"}
        for node in ast.walk(tree):
            if not isinstance(node,allowed_nodes):
                raise ValueError(f"Operación no permitida en la función: {type(node).__name__}")
            if isinstance(node,ast.Name) and node.id not in allowed_names:
                raise ValueError(f"Nombre matemático no permitido: {node.id}")
            if isinstance(node,ast.Call) and (not isinstance(node.func,ast.Name)
                                              or node.func.id not in allowed_names-{"x","pi","e"}):
                raise ValueError("Función matemática no permitida")
        scope={"x":xs,"sin":np.sin,"cos":np.cos,"tan":np.tan,"exp":np.exp,
               "log":np.log,"sqrt":np.sqrt,"abs":np.abs,"pi":np.pi,"e":np.e}
        return eval(compile(tree,"<plot>","eval"),{"__builtins__":{}},scope)

    @staticmethod
    def _technical_font(size: int = 15) -> Any:
        from PIL import ImageFont
        candidates=[Path(os.environ.get("WINDIR","C:/Windows"))/"Fonts"/"segoeui.ttf",
                    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")]
        for candidate in candidates:
            try:
                if candidate.exists(): return ImageFont.truetype(str(candidate),size)
            except OSError: pass
        return ImageFont.load_default()

    def plot_function(self, expression: str, x_min: float, x_max: float,
                      y_min: float, y_max: float, x: float, y: float, w: float,
                      h: float, color: str, title: str) -> None:
        """Dibuja una gráfica cartesiana escalada, con rejilla, ticks y función segura."""
        import numpy as np
        from PIL import ImageDraw
        x_min,x_max,y_min,y_max=map(float,(x_min,x_max,y_min,y_max))
        x,y,w,h=map(float,(x,y,w,h))
        if x_max<=x_min or y_max<=y_min or w<180 or h<140 or x<4 or y<4 or x+w>996 or y+h>696:
            raise ValueError("Dominio, rango o región de gráfica inválidos")
        xs=np.linspace(x_min,x_max,900)
        with np.errstate(all="ignore"):
            ys=np.asarray(self._safe_math_values(expression,xs),dtype=float)
        if ys.ndim==0: ys=np.full_like(xs,float(ys))
        self._snapshot()
        with self.lock:
            draw=ImageDraw.Draw(self.image)
            draw.rectangle((x,y,x+w,y+h),fill="#FFFFFF",outline="#94A3B8",width=2)
            font=self._technical_font(13); title_font=self._technical_font(16)
            for i in range(1,10):
                gx=x+w*i/10; gy=y+h*i/10
                draw.line((gx,y,gx,y+h),fill="#E2E8F0",width=1)
                draw.line((x,gy,x+w,gy),fill="#E2E8F0",width=1)
            px=lambda value:x+(value-x_min)/(x_max-x_min)*w
            py=lambda value:y+h-(value-y_min)/(y_max-y_min)*h
            if x_min<=0<=x_max: draw.line((px(0),y,px(0),y+h),fill="#334155",width=2)
            if y_min<=0<=y_max: draw.line((x,py(0),x+w,py(0)),fill="#334155",width=2)
            for i in range(6):
                xv=x_min+(x_max-x_min)*i/5; yv=y_min+(y_max-y_min)*i/5
                draw.text((px(xv)-10,y+h+2),f"{xv:g}",fill="#475569",font=font)
                draw.text((x-42,py(yv)-7),f"{yv:g}",fill="#475569",font=font)
            finite=np.isfinite(ys)&(ys>=y_min)&(ys<=y_max)
            segment=[]
            for xv,yv,valid in zip(xs,ys,finite):
                if valid: segment.append((px(float(xv)),py(float(yv))))
                elif len(segment)>1:
                    draw.line(segment,fill=color,width=4); segment=[]
            if len(segment)>1: draw.line(segment,fill=color,width=4)
            if title: draw.text((x+8,y+7),title,fill="#0F172A",font=title_font)
        bounds={"x":x,"y":y,"w":w,"h":h}
        self._record("plot_function",bounds,{"expression":expression,"color":color},
                     expression=expression,title=title,domain=[x_min,x_max],range=[y_min,y_max])

    def render_equation(self, latex: str, x: float, y: float, max_width: float,
                        max_height: float, color: str) -> None:
        """Nombre público inequívoco para fórmulas; conserva math_formula por compatibilidad."""
        self.math_formula(latex, x, y, max_width, max_height, color)
        if self.objects:
            self.objects[-1]["type"] = "render_equation"
            self.last_report["type"] = "render_equation"

    @staticmethod
    def _fit_label(draw: Any, text: str, box: tuple[float, float, float, float],
                   color: str = "#0F172A", maximum: int = 19) -> None:
        """Texto tipográfico legible dentro de una caja; nunca apila caracteres."""
        x, y, w, h = box
        clean = normalize_text(str(text)).strip()
        size = maximum
        while size > 10:
            font = NativeBoardController._technical_font(size)
            bbox = draw.multiline_textbbox((0, 0), clean, font=font, spacing=3, align="center")
            if bbox[2] <= w-12 and bbox[3] <= h-10:
                break
            size -= 1
        font = NativeBoardController._technical_font(size)
        # Ajuste por palabras; las palabras imposibles se elipsizan, no se parten verticalmente.
        words, lines, current = clean.split(), [], ""
        for word in words:
            candidate = word if not current else current+" "+word
            if draw.textbbox((0,0), candidate, font=font)[2] <= w-12:
                current = candidate
            else:
                if current: lines.append(current)
                current = word
        if current: lines.append(current)
        while len(lines)*max(12,size+4) > h-8 and len(lines)>1:
            lines[-2] += " " + lines.pop()
        value = "\n".join(lines) or clean
        draw.multiline_text((x+w/2,y+h/2),value,fill=color,font=font,
                            anchor="mm",align="center",spacing=3)

    def draw_diagram(self, diagram_type: str, nodes: list[dict[str, Any]],
                     edges: list[dict[str, Any]], x: float, y: float, w: float,
                     h: float, direction: str, title: str) -> dict[str, Any]:
        """Diagrama semántico con layout determinista y conectores que evitan el texto."""
        from PIL import ImageDraw
        diagram_type = str(diagram_type).lower()
        if diagram_type not in {"flowchart","decision_tree","tree","neural_network","architecture"}:
            raise ValueError("tipo de diagrama no compatible")
        if not 2 <= len(nodes) <= 24:
            raise ValueError("draw_diagram requiere entre 2 y 24 nodos")
        x,y,w,h=map(float,(x,y,w,h))
        if w<260 or h<180 or x<4 or y<4 or x+w>996 or y+h>696:
            raise ValueError("La región del diagrama no cabe en la pizarra")
        ids=[str(n.get("id","")).strip() for n in nodes]
        if any(not value for value in ids) or len(set(ids))!=len(ids):
            raise ValueError("Cada nodo necesita un id único")
        known=set(ids)
        for edge in edges:
            if str(edge.get("from")) not in known or str(edge.get("to")) not in known:
                raise ValueError("Una arista referencia un nodo inexistente")
        # Capas por topología; los ciclos restantes se colocan en una última capa.
        incoming={key:0 for key in ids}; outgoing={key:[] for key in ids}
        for edge in edges:
            a,b=str(edge["from"]),str(edge["to"]); incoming[b]+=1; outgoing[a].append(b)
        frontier=[key for key in ids if incoming[key]==0] or [ids[0]]
        layers: list[list[str]]=[]; placed:set[str]=set()
        while frontier:
            layer=[key for key in frontier if key not in placed]
            if not layer: break
            layers.append(layer); placed.update(layer); following=[]
            for key in layer:
                for target in outgoing[key]:
                    incoming[target]-=1
                    if incoming[target]<=0: following.append(target)
            frontier=following
        if placed!=known: layers.append([key for key in ids if key not in placed])
        horizontal = direction == "horizontal"
        inner_y=y+(34 if title else 10); inner_h=h-(44 if title else 20)
        positions: dict[str,dict[str,float]]={}
        node_map={str(n["id"]):n for n in nodes}
        for li,layer in enumerate(layers):
            for ri,key in enumerate(layer):
                if horizontal:
                    cell_w=w/max(1,len(layers)); cell_h=inner_h/max(1,len(layer))
                    nw=min(170,cell_w*.72); nh=min(72,cell_h*.62)
                    nx=x+cell_w*(li+.5)-nw/2; ny=inner_y+cell_h*(ri+.5)-nh/2
                else:
                    cell_h=inner_h/max(1,len(layers)); cell_w=w/max(1,len(layer))
                    nw=min(180,cell_w*.72); nh=min(72,cell_h*.62)
                    nx=x+cell_w*(ri+.5)-nw/2; ny=inner_y+cell_h*(li+.5)-nh/2
                positions[key]={"x":nx,"y":ny,"w":nw,"h":nh}
        self._snapshot()
        with self.lock:
            draw=ImageDraw.Draw(self.image,"RGBA")
            draw.rounded_rectangle((x,y,x+w,y+h),radius=14,fill="#F8FAFC",outline="#CBD5E1",width=2)
            if title:
                draw.text((x+14,y+9),title,fill="#0F172A",font=self._technical_font(18))
            # Aristas primero: de borde a borde, antes de pintar los nodos.
            for edge in edges:
                a,b=positions[str(edge["from"])],positions[str(edge["to"])]
                if horizontal:
                    p1=(a["x"]+a["w"],a["y"]+a["h"]/2); p2=(b["x"],b["y"]+b["h"]/2)
                else:
                    p1=(a["x"]+a["w"]/2,a["y"]+a["h"]); p2=(b["x"]+b["w"]/2,b["y"])
                draw.line((*p1,*p2),fill="#64748B",width=3)
                angle=math.atan2(p2[1]-p1[1],p2[0]-p1[0]); size=10
                draw.polygon([p2,(p2[0]-math.cos(angle-.5)*size,p2[1]-math.sin(angle-.5)*size),
                              (p2[0]-math.cos(angle+.5)*size,p2[1]-math.sin(angle+.5)*size)],fill="#64748B")
                label=str(edge.get("label","")).strip()
                if label:
                    mid=((p1[0]+p2[0])/2,(p1[1]+p2[1])/2-9)
                    draw.text(mid,label,fill="#475569",font=self._technical_font(11),anchor="ms")
            palette=["#DBEAFE","#DCFCE7","#FEF3C7","#F3E8FF","#FFE4E6"]
            for index,key in enumerate(ids):
                node=node_map[key]; box=positions[key]
                fill=str(node.get("color") or palette[index%len(palette)])
                xy=(box["x"],box["y"],box["x"]+box["w"],box["y"]+box["h"])
                if diagram_type=="neural_network" or str(node.get("kind"))=="circle":
                    diameter=min(box["w"],box["h"]); cx=box["x"]+box["w"]/2;cy=box["y"]+box["h"]/2
                    xy=(cx-diameter/2,cy-diameter/2,cx+diameter/2,cy+diameter/2)
                    draw.ellipse(xy,fill=fill,outline="#2563EB",width=3)
                else:
                    draw.rounded_rectangle(xy,radius=10,fill=fill,outline="#2563EB",width=3)
                self._fit_label(draw,str(node.get("label",key)),(xy[0],xy[1],xy[2]-xy[0],xy[3]-xy[1]))
        bounds={"x":x,"y":y,"w":w,"h":h}
        self._record("draw_diagram",bounds,{"diagram_type":diagram_type},diagram_type=diagram_type,
                     title=title,nodes=copy.deepcopy(nodes),edges=copy.deepcopy(edges),layout=positions)
        return {"nodes":positions,"edge_count":len(edges)}

    def highlight_region(self, x: float, y: float, w: float, h: float,
                         color: str, label: str) -> None:
        from PIL import Image, ImageColor, ImageDraw
        x,y,w,h=map(float,(x,y,w,h))
        if w<8 or h<8 or x<0 or y<0 or x+w>1000 or y+h>700: raise ValueError("Región inválida")
        self._snapshot()
        with self.lock:
            overlay=Image.new("RGBA",self.image.size,(0,0,0,0));draw=ImageDraw.Draw(overlay)
            rgb=ImageColor.getrgb(color);draw.rounded_rectangle((x,y,x+w,y+h),radius=10,
                fill=(*rgb,42),outline=(*rgb,235),width=4)
            if label: draw.text((x+8,max(2,y-24)),label,fill=(*rgb,255),font=self._technical_font(15))
            self.image=Image.alpha_composite(self.image.convert("RGBA"),overlay).convert("RGB")
        self._record("highlight_region",{"x":x,"y":y,"w":w,"h":h},{"color":color},label=label)

    def annotate_live(self, text: str, x: float, y: float, color: str) -> None:
        from PIL import ImageDraw
        text=normalize_text(text).strip()
        if not text or len(text)>180: raise ValueError("Anotación vacía o demasiado larga")
        x,y=float(x),float(y); w=min(360,max(120,len(text)*8.2)); h=54
        if x+w>996: x=996-w
        if y+h>696: y=696-h
        self._snapshot()
        with self.lock:
            draw=ImageDraw.Draw(self.image,"RGBA")
            draw.rounded_rectangle((x,y,x+w,y+h),radius=12,fill="#FFFBEBEE",outline=color,width=3)
            self._fit_label(draw,text,(x,y,w,h),"#0F172A",16)
        self._record("annotate_live",{"x":x,"y":y,"w":w,"h":h},{"color":color},text=text)

    def split_view(self, panels: list[dict[str, Any]], orientation: str,
                   x: float, y: float, w: float, h: float) -> dict[str, Any]:
        from PIL import ImageDraw
        if not 2<=len(panels)<=4: raise ValueError("split_view admite entre 2 y 4 paneles")
        x,y,w,h=map(float,(x,y,w,h)); horizontal=orientation=="horizontal"
        if w<300 or h<180 or x<4 or y<4 or x+w>996 or y+h>696: raise ValueError("Región inválida")
        weights=[max(.1,float(p.get("weight",1))) for p in panels]; total=sum(weights)
        regions=[]; cursor=x if horizontal else y
        self._snapshot()
        with self.lock:
            draw=ImageDraw.Draw(self.image)
            for index,(panel,weight) in enumerate(zip(panels,weights)):
                span=(w if horizontal else h)*weight/total
                region={"x":cursor if horizontal else x,"y":y if horizontal else cursor,
                        "w":span if horizontal else w,"h":h if horizontal else span}
                cursor+=span;regions.append(region)
                draw.rounded_rectangle((region["x"],region["y"],region["x"]+region["w"],region["y"]+region["h"]),
                                       radius=10,fill="#F8FAFC",outline="#94A3B8",width=2)
                draw.text((region["x"]+10,region["y"]+8),str(panel.get("title",f"Panel {index+1}")),
                          fill="#334155",font=self._technical_font(15))
        self._record("split_view",{"x":x,"y":y,"w":w,"h":h},{},panels=copy.deepcopy(panels),regions=regions)
        return {"regions":regions}

    def matrix_operations(self, A: list[list[float]], B: list[list[float]], operation: str,
                          x: float, y: float, w: float, h: float) -> dict[str, Any]:
        import numpy as np
        from PIL import ImageDraw
        a=np.asarray(A,dtype=float);b=np.asarray(B,dtype=float)
        if a.ndim!=2 or b.ndim!=2 or max(a.shape+b.shape)>6: raise ValueError("Matrices inválidas o mayores que 6x6")
        op=operation.lower()
        if op=="multiply":
            if a.shape[1]!=b.shape[0]: raise ValueError("Columnas de A deben igualar filas de B")
            result=a@b; symbol="×"
        elif op in {"add","subtract"}:
            if a.shape!=b.shape: raise ValueError("Para sumar/restar, las dimensiones deben coincidir")
            result=a+b if op=="add" else a-b;symbol="+" if op=="add" else "−"
        else: raise ValueError("Operación matricial no compatible")
        x,y,w,h=map(float,(x,y,w,h));
        if w<420 or h<190 or x<4 or y<4 or x+w>996 or y+h>696: raise ValueError("Región matricial inválida")
        self._snapshot()
        with self.lock:
            draw=ImageDraw.Draw(self.image);draw.rounded_rectangle((x,y,x+w,y+h),12,fill="#F8FAFC",outline="#CBD5E1",width=2)
            font=self._technical_font(15); title_font=self._technical_font(18)
            draw.text((x+12,y+8),"OPERACIÓN DE MATRICES — PASO A PASO",fill="#0F172A",font=title_font)
            def render(matrix: Any, left: float, top: float, box_w: float, tint: str) -> None:
                rows,cols=matrix.shape;cw=(box_w-20)/cols;ch=min(34,(h-92)/rows)
                draw.rounded_rectangle((left,top,left+box_w,top+rows*ch+14),8,fill=tint,outline="#64748B",width=2)
                for r in range(rows):
                    for c in range(cols):
                        value=float(matrix[r,c]);text=f"{value:g}"
                        draw.text((left+10+(c+.5)*cw,top+7+(r+.5)*ch),text,fill="#0F172A",font=font,anchor="mm")
            gap=30; box=(w-2*gap)/3
            render(a,x,y+45,box,"#DBEAFE");render(b,x+box+gap,y+45,box,"#DCFCE7");render(result,x+2*(box+gap),y+45,box,"#FEF3C7")
            draw.text((x+box+gap/2,y+80),symbol,fill="#334155",font=self._technical_font(26),anchor="mm")
            draw.text((x+2*box+1.5*gap,y+80),"=",fill="#334155",font=self._technical_font(26),anchor="mm")
            if op=="multiply":
                terms=" + ".join(f"{a[0,k]:g}·{b[k,0]:g}" for k in range(a.shape[1]))
                draw.text((x+14,y+h-34),f"Ejemplo: C₁₁ = {terms} = {result[0,0]:g}",fill="#475569",font=font)
        self._record("matrix_operations",{"x":x,"y":y,"w":w,"h":h},{},A=A,B=B,
                     operation=op,result=result.tolist())
        return {"result":result.tolist(),"shape":list(result.shape)}

    def token_visualizer(self, text: str, encoding: str, x: float, y: float,
                         w: float, h: float) -> dict[str, Any]:
        import tiktoken
        from PIL import ImageDraw
        if not text or len(text)>500: raise ValueError("Texto vacío o demasiado largo")
        if encoding not in {"cl100k_base","o200k_base","p50k_base"}: raise ValueError("Encoding no permitido")
        codec=tiktoken.get_encoding(encoding);ids=codec.encode(text)
        pieces=[codec.decode_single_token_bytes(token).decode("utf-8","replace") for token in ids]
        x,y,w,h=map(float,(x,y,w,h));
        if w<300 or h<150 or x<4 or y<4 or x+w>996 or y+h>696: raise ValueError("Región inválida")
        self._snapshot()
        with self.lock:
            draw=ImageDraw.Draw(self.image);draw.rounded_rectangle((x,y,x+w,y+h),12,fill="#F8FAFC",outline="#CBD5E1",width=2)
            draw.text((x+12,y+9),f"TOKENS — {encoding} ({len(ids)})",fill="#0F172A",font=self._technical_font(17))
            palette=["#DBEAFE","#DCFCE7","#FEF3C7","#F3E8FF","#FFE4E6"]
            cx,cy=x+12,y+42;font=self._technical_font(14)
            for index,(token,piece) in enumerate(zip(ids,pieces)):
                label=(piece.replace("\n","↵") or "∅")+f"  #{token}";tw=min(w-24,max(72,draw.textbbox((0,0),label,font=font)[2]+18))
                if cx+tw>x+w-10: cx=x+12;cy+=38
                if cy+31>y+h-8: break
                draw.rounded_rectangle((cx,cy,cx+tw,cy+29),7,fill=palette[index%5],outline="#94A3B8",width=1)
                draw.text((cx+8,cy+7),label,fill="#0F172A",font=font);cx+=tw+7
        self._record("token_visualizer",{"x":x,"y":y,"w":w,"h":h},{},text=text,encoding=encoding,
                     token_ids=ids,pieces=pieces,truncated=len(ids)>len(pieces))
        return {"encoding":encoding,"count":len(ids),"tokens":[{"id":i,"text":p} for i,p in zip(ids,pieces)]}

    def simulate_training(self, model: str, dataset: str, epochs: int, x: float, y: float,
                          w: float, h: float) -> dict[str, Any]:
        """Simulación didáctica explícita; jamás se presenta como entrenamiento real."""
        from PIL import ImageDraw
        epochs=max(5,min(100,int(epochs)));x,y,w,h=map(float,(x,y,w,h))
        if w<320 or h<200 or x<4 or y<4 or x+w>996 or y+h>696: raise ValueError("Región inválida")
        rng=random.Random(f"{model}|{dataset}|{epochs}")
        loss=[];accuracy=[]
        for index in range(epochs):
            progress=index/max(1,epochs-1)
            loss.append(max(.03,1.15*math.exp(-3.2*progress)+rng.uniform(-.025,.025)))
            accuracy.append(min(.995,max(0,.35+.62*(1-math.exp(-3.5*progress))+rng.uniform(-.012,.012))))
        self._snapshot()
        with self.lock:
            draw=ImageDraw.Draw(self.image);draw.rounded_rectangle((x,y,x+w,y+h),12,fill="#F8FAFC",outline="#CBD5E1",width=2)
            draw.text((x+12,y+8),"SIMULACIÓN DIDÁCTICA — NO ES ENTRENAMIENTO REAL",fill="#B45309",font=self._technical_font(15))
            draw.text((x+12,y+30),f"{model} · {dataset} · {epochs} epochs",fill="#334155",font=self._technical_font(13))
            left,top,right,bottom=x+48,y+62,x+w-18,y+h-36
            for i in range(6):
                gy=top+(bottom-top)*i/5;draw.line((left,gy,right,gy),fill="#E2E8F0",width=1)
            def curve(values: list[float], color: str, maximum: float) -> None:
                pts=[(left+(right-left)*i/max(1,epochs-1),bottom-(bottom-top)*v/maximum) for i,v in enumerate(values)]
                draw.line(pts,fill=color,width=4)
            curve(loss,"#EF4444",1.25);curve(accuracy,"#2563EB",1.0)
            draw.text((left,bottom+8),"epoch →",fill="#475569",font=self._technical_font(12))
            draw.text((right-150,top+5),"loss",fill="#EF4444",font=self._technical_font(13));draw.text((right-80,top+5),"accuracy",fill="#2563EB",font=self._technical_font(13))
        self._record("simulate_training",{"x":x,"y":y,"w":w,"h":h},{},model=model,dataset=dataset,
                     epochs=epochs,loss=loss,accuracy=accuracy,simulated=True)
        return {"simulated":True,"final_loss":loss[-1],"final_accuracy":accuracy[-1]}

    def create_poll(self, question: str, options: list[str], x: float, y: float,
                    w: float, h: float) -> dict[str, Any]:
        from PIL import ImageDraw
        if not question.strip() or not 2<=len(options)<=6: raise ValueError("Encuesta inválida")
        x,y,w,h=map(float,(x,y,w,h));
        if w<280 or h<150 or x<4 or y<4 or x+w>996 or y+h>696: raise ValueError("Región inválida")
        poll_id=f"poll_{self.next_object_id}";self._snapshot()
        with self.lock:
            draw=ImageDraw.Draw(self.image);draw.rounded_rectangle((x,y,x+w,y+h),14,fill="#EFF6FF",outline="#3B82F6",width=3)
            self._fit_label(draw,question,(x+10,y+8,w-20,48),"#0F172A",17)
            row_h=(h-68)/len(options)
            for index,option in enumerate(options):
                oy=y+60+index*row_h
                draw.rounded_rectangle((x+16,oy,x+w-16,oy+row_h-7),8,fill="#FFFFFF",outline="#BFDBFE",width=2)
                draw.ellipse((x+28,oy+9,x+46,oy+27),outline="#2563EB",width=2)
                draw.text((x+56,oy+9),f"{chr(65+index)}. {option}",fill="#1E293B",font=self._technical_font(14))
        self._record("create_poll",{"x":x,"y":y,"w":w,"h":h},{},poll_id=poll_id,
                     question=question,options=list(options),votes=[0]*len(options),status="open")
        return {"poll_id":poll_id,"status":"open","options":options}

    def spawn_slider(self, variable: str, minimum: float, maximum: float, value: float,
                     x: float, y: float, w: float, color: str) -> dict[str, Any]:
        from PIL import ImageDraw
        minimum,maximum,value=map(float,(minimum,maximum,value));x,y,w=float(x),float(y),float(w)
        if maximum<=minimum or not minimum<=value<=maximum or w<180 or x<4 or x+w>996 or y<4 or y+82>696:
            raise ValueError("Slider o región inválidos")
        self._snapshot();ratio=(value-minimum)/(maximum-minimum);marker=x+20+(w-40)*ratio
        with self.lock:
            draw=ImageDraw.Draw(self.image);draw.rounded_rectangle((x,y,x+w,y+82),12,fill="#F8FAFC",outline="#CBD5E1",width=2)
            draw.text((x+14,y+10),f"{variable} = {value:g}",fill="#0F172A",font=self._technical_font(16))
            draw.line((x+20,y+53,x+w-20,y+53),fill="#94A3B8",width=6)
            draw.line((x+20,y+53,marker,y+53),fill=color,width=6);draw.ellipse((marker-9,y+44,marker+9,y+62),fill=color,outline="white",width=2)
            draw.text((x+15,y+64),f"{minimum:g}",fill="#64748B",font=self._technical_font(11));draw.text((x+w-15,y+64),f"{maximum:g}",fill="#64748B",font=self._technical_font(11),anchor="ra")
        widget_id=f"slider_{self.next_object_id}"
        self._record("spawn_slider",{"x":x,"y":y,"w":w,"h":82},{},widget_id=widget_id,
                     variable=variable,minimum=minimum,maximum=maximum,value=value)
        return {"widget_id":widget_id,"variable":variable,"value":value}

    @staticmethod
    def _safe_python(code: str, timeout: float = 2.0) -> dict[str, Any]:
        """Ejecutor educativo fail-closed: sin imports, atributos, archivos, red ni procesos."""
        if len(code)>6000: raise ValueError("Código demasiado largo")
        tree=ast.parse(code,mode="exec")
        forbidden=(ast.Import,ast.ImportFrom,ast.Attribute,ast.With,ast.AsyncWith,ast.Global,
                   ast.Nonlocal,ast.ClassDef,ast.Lambda,ast.Try,ast.Raise,ast.Delete)
        forbidden_calls={"open","eval","exec","compile","input","globals","locals","vars","dir",
                         "getattr","setattr","delattr","__import__","help","breakpoint","exit","quit"}
        for node in ast.walk(tree):
            if isinstance(node,forbidden): raise ValueError(f"Construcción no permitida: {type(node).__name__}")
            if isinstance(node,ast.Name) and node.id.startswith("__"): raise ValueError("Nombres dunder no permitidos")
            if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id in forbidden_calls:
                raise ValueError(f"Llamada no permitida: {node.func.id}")
        wrapper=("safe={'print':print,'range':range,'len':len,'sum':sum,'min':min,'max':max,"
                 "'abs':abs,'round':round,'enumerate':enumerate,'zip':zip,'list':list,'dict':dict,"
                 "'set':set,'tuple':tuple,'sorted':sorted,'str':str,'int':int,'float':float,'bool':bool}\n"
                 "exec(compile("+repr(code)+",'<sandbox>','exec'),{'__builtins__':safe},{})")
        with tempfile.TemporaryDirectory(prefix="teachai-sandbox-") as temp:
            result=subprocess.run([sys.executable,"-I","-S","-c",wrapper],cwd=temp,
                capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=timeout,
                env={"PATH":os.environ.get("PATH","")})
        return {"exit_code":result.returncode,"stdout":result.stdout[:4000],"stderr":result.stderr[:2000],"timed_out":False}

    def code_sandbox(self, language: str, initial_code: str, x: float, y: float,
                     w: float, h: float, run: bool) -> dict[str, Any]:
        from PIL import ImageDraw
        if language.lower()!="python": raise ValueError("El sandbox aislado admite Python en esta versión")
        result=self._safe_python(initial_code) if run else {"status":"ready","stdout":"","stderr":""}
        x,y,w,h=map(float,(x,y,w,h));
        if w<360 or h<220 or x<4 or y<4 or x+w>996 or y+h>696: raise ValueError("Región inválida")
        self._snapshot()
        with self.lock:
            draw=ImageDraw.Draw(self.image);draw.rounded_rectangle((x,y,x+w,y+h),12,fill="#0F172A",outline="#334155",width=2)
            draw.text((x+14,y+10),"PYTHON · SANDBOX AISLADO",fill="#93C5FD",font=self._technical_font(15))
            font=self._technical_font(13);cy=y+40
            for line in initial_code.splitlines()[:12]:
                draw.text((x+14,cy),line[:72],fill="#E2E8F0",font=font);cy+=18
            if run:
                split=y+h*.68;draw.line((x+10,split,x+w-10,split),fill="#475569",width=1)
                output=(result.get("stdout") or result.get("stderr") or "(sin salida)").splitlines()
                draw.text((x+14,split+8),"SALIDA",fill="#86EFAC",font=font)
                for row,line in enumerate(output[:5]): draw.text((x+14,split+29+row*17),line[:80],fill="#CBD5E1",font=font)
        self._record("code_sandbox",{"x":x,"y":y,"w":w,"h":h},{},language="python",code=initial_code,
                     execution=result if run else None,isolated=True)
        return result

    def grade_snippet(self, language: str, code: str, criteria: list[str]) -> dict[str, Any]:
        if language.lower()!="python": raise ValueError("Solo se corrige Python de forma ejecutable")
        try:
            execution=self._safe_python(code);syntax_ok=True
        except (SyntaxError,ValueError) as exc:
            execution={"exit_code":1,"stdout":"","stderr":str(exc)};syntax_ok=False
        passed=syntax_ok and execution.get("exit_code")==0
        return {"passed":passed,"syntax_ok":syntax_ok,"execution":execution,
                "criteria":[{"criterion":item,"status":"requires_teacher_review"} for item in criteria],
                "warning":"Los criterios semánticos no se marcan automáticamente sin tests explícitos."}

    def generate_exercise(self, topic: str, difficulty: str, prompt: str,
                          hints: list[str], expected_answer: str, x: float, y: float,
                          w: float, h: float) -> dict[str, Any]:
        from PIL import ImageDraw
        x,y,w,h=map(float,(x,y,w,h));
        if w<340 or h<200 or x<4 or y<4 or x+w>996 or y+h>696: raise ValueError("Región inválida")
        self._snapshot()
        with self.lock:
            draw=ImageDraw.Draw(self.image);draw.rounded_rectangle((x,y,x+w,y+h),14,fill="#FFFBEB",outline="#F59E0B",width=3)
            draw.text((x+14,y+10),f"EJERCICIO · {difficulty.upper()} · {topic}",fill="#92400E",font=self._technical_font(16))
            self._fit_label(draw,prompt,(x+15,y+43,w-30,min(90,h*.42)),"#1E293B",17)
            cy=y+145
            for index,hint in enumerate(hints[:3]):
                draw.text((x+18,cy),f"Pista {index+1}: {hint}",fill="#64748B",font=self._technical_font(13));cy+=23
        exercise_id=f"exercise_{self.next_object_id}"
        self._record("generate_exercise",{"x":x,"y":y,"w":w,"h":h},{},exercise_id=exercise_id,
                     topic=topic,difficulty=difficulty,prompt=prompt,hints=hints,
                     expected_answer=expected_answer,status="awaiting_student")
        return {"exercise_id":exercise_id,"status":"awaiting_student"}

    @staticmethod
    def _safe_name(name: str) -> str:
        value=re.sub(r"[^A-Za-z0-9._-]+","-",normalize_text(name)).strip("-.")
        return value[:80] or time.strftime("session-%Y%m%d-%H%M%S")

    def save_snapshot(self, name: str) -> dict[str, Any]:
        folder=app_data_dir()/"snapshots";folder.mkdir(parents=True,exist_ok=True);safe=self._safe_name(name)
        png=folder/f"{safe}.png";manifest=folder/f"{safe}.json"
        with self.lock:
            self.image.save(png,"PNG")
            payload={"format":1,"page":self.page_number,"next_object_id":self.next_object_id,
                     "objects":self.objects,"image":base64.b64encode(png.read_bytes()).decode("ascii")}
        manifest.write_text(json.dumps(payload,ensure_ascii=False,separators=(",",":")),encoding="utf-8")
        return {"name":safe,"png":str(png),"manifest":str(manifest),"object_count":len(self.objects)}

    def load_snapshot(self, name: str) -> dict[str, Any]:
        from PIL import Image
        safe=self._safe_name(name);manifest=app_data_dir()/"snapshots"/f"{safe}.json"
        if not manifest.is_file(): raise ValueError(f"No existe el snapshot {safe}")
        payload=json.loads(manifest.read_text(encoding="utf-8"))
        if payload.get("format")!=1 or not isinstance(payload.get("objects"),list): raise ValueError("Snapshot incompatible")
        raw=base64.b64decode(payload["image"],validate=True);image=Image.open(io.BytesIO(raw)).convert("RGB")
        if image.size!=(1000,700): raise ValueError("Tamaño de snapshot incompatible")
        self._snapshot()
        with self.lock:
            self.image=image;self.objects=payload["objects"];self.page_number=int(payload.get("page",1))
            self.next_object_id=max(int(payload.get("next_object_id",1)),len(self.objects)+1);self._mark_dirty()
        return {"name":safe,"object_count":len(self.objects),"page":self.page_number}

    def export_board(self, format: str, name: str) -> dict[str, Any]:
        format=format.lower();safe=self._safe_name(name);folder=app_data_dir()/"exports";folder.mkdir(parents=True,exist_ok=True)
        if format not in {"png","pdf","json"}: raise ValueError("Formato permitido: png, pdf o json")
        target=folder/f"{safe}.{format}"
        with self.lock:
            if format=="png": self.image.save(target,"PNG")
            elif format=="pdf": self.image.convert("RGB").save(target,"PDF",resolution=144)
            else: target.write_text(json.dumps(self.semantic_state(),indent=2,ensure_ascii=False),encoding="utf-8")
        return {"format":format,"path":str(target),"object_count":len(self.objects)}

    def polygon(self, points: list[dict[str, Any]], closed: bool, color: str,
                width: int, speed: float) -> None:
        coords=[(float(p["x"]),float(p["y"])) for p in points]
        if len(coords)<2: raise ValueError("El polígono necesita al menos dos puntos")
        path=coords+([coords[0]] if closed and coords[-1]!=coords[0] else [])
        self._snapshot(); self._animate_path(path,color,width,speed)
        self._record("polygon",self._bounds(path,width+2),{"paths":[path],"color":color,"width":width},
                     vertices=len(coords),closed=closed)

    def arc(self, cx: float, cy: float, rx: float, ry: float, start_deg: float,
            end_deg: float, color: str, width: int, speed: float) -> None:
        cx,cy,rx,ry=map(float,(cx,cy,rx,ry))
        span=float(end_deg)-float(start_deg)
        if rx<=0 or ry<=0 or abs(span)<1 or abs(span)>360: raise ValueError("Arco inválido")
        count=max(12,int(abs(span)/3))
        path=[(cx+rx*math.cos(math.radians(float(start_deg)+span*i/count)),
               cy+ry*math.sin(math.radians(float(start_deg)+span*i/count))) for i in range(count+1)]
        self._snapshot(); self._animate_path(path,color,width,speed)
        self._record("arc",self._bounds(path,width+2),{"paths":[path],"color":color,"width":width},
                     center=[cx,cy],radii=[rx,ry],angles=[start_deg,end_deg])

    def angle_mark(self, cx: float, cy: float, radius: float, start_deg: float,
                   end_deg: float, label: str, color: str, width: int) -> None:
        from PIL import ImageDraw
        cx,cy,radius=map(float,(cx,cy,radius));span=float(end_deg)-float(start_deg)
        if radius<12 or radius>180 or abs(span)<1 or abs(span)>360: raise ValueError("Ángulo inválido")
        count=max(12,int(abs(span)/3))
        path=[(cx+radius*math.cos(math.radians(float(start_deg)+span*i/count)),
               cy+radius*math.sin(math.radians(float(start_deg)+span*i/count))) for i in range(count+1)]
        self._snapshot(); self._animate_path(path,color,width,1.2)
        middle=math.radians(float(start_deg)+span/2); text=label.strip() or f"{abs(span):g}°"
        with self.lock:
            draw=ImageDraw.Draw(self.image);font=self._technical_font(15)
            pos=(cx+(radius+18)*math.cos(middle),cy+(radius+18)*math.sin(middle))
            draw.text(pos,text,fill=color,font=font,anchor="mm")
        self._record("angle_mark",self._bounds(path,35),{"paths":[path],"color":color,"width":width},
                     center=[cx,cy],radius=radius,angles=[start_deg,end_deg],label=text)

    def dimension(self, x1: float, y1: float, x2: float, y2: float, offset: float,
                  label: str, units: str, color: str) -> None:
        """Cota técnica ISO-like: auxiliares, línea de cota, dos puntas y etiqueta centrada."""
        from PIL import ImageDraw
        x1,y1,x2,y2,offset=map(float,(x1,y1,x2,y2,offset))
        length=math.dist((x1,y1),(x2,y2))
        if length<20: raise ValueError("La cota es demasiado corta")
        nx,ny=-(y2-y1)/length,(x2-x1)/length
        q1=(x1+nx*offset,y1+ny*offset); q2=(x2+nx*offset,y2+ny*offset)
        bounds=self._bounds([(x1,y1),(x2,y2),q1,q2],18)
        if bounds["x"]<0 or bounds["y"]<0 or bounds["x"]+bounds["w"]>1000 or bounds["y"]+bounds["h"]>700:
            raise ValueError("La cota sale de la pizarra")
        self._snapshot()
        with self.lock:
            draw=ImageDraw.Draw(self.image); pw=2
            draw.line((x1,y1,q1[0]+nx*8,q1[1]+ny*8),fill=color,width=pw)
            draw.line((x2,y2,q2[0]+nx*8,q2[1]+ny*8),fill=color,width=pw)
            draw.line((*q1,*q2),fill=color,width=pw)
            angle=math.atan2(q2[1]-q1[1],q2[0]-q1[0]); size=10
            for q,direction in ((q1,0),(q2,math.pi)):
                a=angle+direction
                draw.polygon([q,(q[0]+math.cos(a+.42)*size,q[1]+math.sin(a+.42)*size),
                              (q[0]+math.cos(a-.42)*size,q[1]+math.sin(a-.42)*size)],fill=color)
            text=(label.strip() or f"{length:.1f}")+(f" {units.strip()}" if units.strip() else "")
            font=self._technical_font(16); box=draw.textbbox((0,0),text,font=font)
            tx=(q1[0]+q2[0])/2-(box[2]-box[0])/2; ty=(q1[1]+q2[1])/2-(box[3]-box[1])/2-3
            draw.rectangle((tx-4,ty-2,tx+(box[2]-box[0])+4,ty+(box[3]-box[1])+2),fill="white")
            draw.text((tx,ty),text,fill=color,font=font)
        self._record("dimension",bounds,{"color":color},label=text,measured_length=length,
                     endpoints=[[x1,y1],[x2,y2]],offset=offset)

    def erase(self, x: float, y: float, w: float, h: float, size: int,
              speed: float, reason: str) -> None:
        del speed
        x, y, w, h, size = float(x), float(y), float(w), float(h), int(size)
        if w <= 0 or h <= 0 or x < 0 or y < 0 or x+w > VIRTUAL_W or y+h > VIRTUAL_H:
            raise ValueError("La región de borrado debe caber dentro de la pizarra")
        if w > 500 or h > 220 or w*h > VIRTUAL_W*VIRTUAL_H*.12:
            raise ValueError("Borrado demasiado grande; usa undo, next_page o clear")
        self._snapshot()
        region = {"x":x,"y":y,"w":w,"h":h}
        removed, partial = [], []
        with self.lock:
            for obj in self.objects:
                b = obj["bounds"]
                contained = (b["x"] >= x and b["y"] >= y and
                             b["x"]+b["w"] <= x+w and b["y"]+b["h"] <= y+h)
                if contained:
                    removed.append(obj["id"])
                elif self._intersects(region, b, margin=0):
                    partial.append(obj["id"])
            self.objects = [obj for obj in self.objects if obj["id"] not in removed]
            from PIL import ImageDraw
            ImageDraw.Draw(self.image).rectangle((x,y,x+w,y+h), fill="white")
            for obj in self.objects:
                if obj["id"] in partial:
                    obj["partially_erased"] = True
        warnings = (["El borrado cortó parcialmente: " + ", ".join(partial)] if partial else [])
        self.last_report = {
            "backend":"native", "status":"warning" if warnings else "ok",
            "type":"erase", "actual_bounds":region, "reason":reason,
            "removed_ids":removed, "partially_erased_ids":partial,
            "warnings":warnings, "object_count":len(self.objects),
        }
        self._mark_dirty()
        if self.on_change:
            try: self.on_change()
            except Exception: pass

    def clear(self) -> None:
        from PIL import Image
        self._snapshot()
        with self.lock:
            self.image = Image.new("RGB", (int(VIRTUAL_W), int(VIRTUAL_H)), "white")
            self.objects = []
            self.cursor = None
            self.last_report = {"backend":"native", "status":"ok", "type":"clear", "object_count":0}
        self._mark_dirty()
        if self.on_change:
            try: self.on_change()
            except Exception: pass

    def undo(self) -> None:
        if not self.undo_stack:
            raise ValueError("No hay ninguna acción que deshacer")
        with self.lock:
            self.redo_stack.append((self.image.copy(), copy.deepcopy(self.objects)))
            self.image, self.objects = self.undo_stack.pop()
            self.last_report = {"backend":"native", "status":"ok", "type":"undo",
                                "object_count":len(self.objects)}
        self._mark_dirty()
        if self.on_change:
            try: self.on_change()
            except Exception: pass

    def redo(self) -> None:
        if not self.redo_stack:
            raise ValueError("No hay ninguna acción que rehacer")
        with self.lock:
            self.undo_stack.append((self.image.copy(), copy.deepcopy(self.objects)))
            self.image, self.objects = self.redo_stack.pop()
            self.last_report = {"backend":"native", "status":"ok", "type":"redo",
                                "object_count":len(self.objects)}
        self._mark_dirty()
        if self.on_change:
            try: self.on_change()
            except Exception: pass

    def next_page(self, label: str) -> str:
        from PIL import Image
        folder = app_data_dir() / "lesson_pages" / time.strftime("%Y-%m-%d")
        folder.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_-]+", "-", label).strip("-") or "lesson"
        path = folder / f"{time.strftime('%H%M%S')}-{safe[:48]}.png"
        with self.lock:
            self.image.save(path, "PNG")
            self.archived_pages.append(str(path))
            self.image = Image.new("RGB", (int(VIRTUAL_W), int(VIRTUAL_H)), "white")
            self.objects = []
            self.undo_stack.clear()
            self.redo_stack.clear()
            self.page_number += 1
            self.last_report = {"backend":"native", "status":"ok", "type":"next_page",
                                "archived_page":str(path), "page":self.page_number, "object_count":0}
        self.log(f"Página anterior archivada: {path}")
        self._mark_dirty()
        if self.on_change:
            try: self.on_change()
            except Exception: pass
        return str(path)

    def zoom(self, direction: str, steps: int) -> None:
        delta = .1 * max(1, min(8, int(steps)))
        self.zoom_level = min(1.8, max(.55, self.zoom_level + (delta if direction == "in" else -delta)))
        self.last_report = {"backend":"native", "status":"ok", "type":"zoom", "zoom":self.zoom_level}
        self._mark_dirty()

    def toggle_grid(self) -> None:
        self.grid_visible = not self.grid_visible
        self.last_report = {"backend":"native", "status":"ok", "type":"toggle_grid",
                            "grid_visible":self.grid_visible}
        self._mark_dirty()

    def screenshot_data_url(self) -> str | None:
        try:
            with self.lock:
                image = self.image.copy()
            stream = io.BytesIO()
            image.save(stream, "PNG", optimize=True)
            return "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode("ascii")
        except Exception as exc:
            self.log(f"Captura nativa omitida: {exc}")
            return None

    def action_report(self) -> dict[str, Any]:
        return copy.deepcopy(self.last_report)

    def _planned_text_bounds(self, args: dict[str, Any]) -> dict[str, float]:
        text = self._clean_text(str(args.get("text", ""))).strip()
        x, y = max(8.0, float(args.get("x", 0))), max(8.0, float(args.get("y", 0)))
        available = min(max(80.0, float(args.get("max_width", 80))), VIRTUAL_W-x-8)
        requested = min(90.0, max(14.0, float(args.get("height", 14))))
        longest = max((len(word) for word in text.split()), default=1)
        actual = max(14.0, min(requested, available / max(1.0, longest*.72)))
        while True:
            capacity = max(2, int(available/(actual*.72)))
            lines = self._wrap_text(text, capacity)
            total_h = actual + max(0, len(lines)-1)*actual*1.42
            if y+total_h <= VIRTUAL_H-8 or actual <= 14:
                break
            actual = max(14.0, actual-1)
        max_chars = max((len(line) for line in lines), default=0)
        return {"x":round(x,1), "y":round(y,1),
                "w":round(min(available,max_chars*actual*.72),1), "h":round(total_h,1)}

    @staticmethod
    def _contains_point(bounds: dict[str, float], point: tuple[float, float], pad: float = 12) -> bool:
        return (bounds["x"]-pad <= point[0] <= bounds["x"]+bounds["w"]+pad
                and bounds["y"]-pad <= point[1] <= bounds["y"]+bounds["h"]+pad)

    def preflight_action(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Guardia determinista: impide que una mala coordenada llegue al lienzo."""
        state = self.semantic_state()
        if state["warnings"] and name not in {"undo", "clear", "next_page"}:
            return {"ok":False, "code":"damaged_scene",
                    "error":"La escena contiene un objeto parcialmente dañado; usa undo antes de continuar.",
                    "evidence":state["warnings"]}
        if name not in self.VISUAL_PRIMITIVES:
            return {"ok":True}
        try:
            if name == "write":
                bounds = self._planned_text_bounds(args)
            elif name in {"math_formula","render_equation"}:
                bounds = {"x":float(args["x"]),"y":float(args["y"]),
                          "w":float(args["max_width"]),"h":float(args["max_height"])}
            elif name == "math_matrix":
                rows=args.get("rows",[]);columns=max((len(row) for row in rows),default=0)
                bounds={"x":float(args["x"]),"y":float(args["y"]),
                        "w":columns*float(args["cell_width"])+34,
                        "h":len(rows)*float(args["cell_height"])+12}
            elif name in {"plot_function","draw_diagram","split_view","matrix_operations",
                          "token_visualizer","simulate_training","create_poll","code_sandbox",
                          "generate_exercise","highlight_region"}:
                bounds = {key:float(args[key]) for key in ("x","y","w","h")}
            elif name == "annotate_live":
                bounds = {"x":float(args["x"]),"y":float(args["y"]),"w":360,"h":54}
            elif name == "spawn_slider":
                bounds = {"x":float(args["x"]),"y":float(args["y"]),"w":float(args["w"]),"h":82}
            elif name in {"rectangle", "ellipse", "erase"}:
                bounds = {key:float(args[key]) for key in ("x","y","w","h")}
            elif name == "point":
                radius = 10+int(args.get("width",2))*2
                bounds = {"x":float(args["x"])-radius,"y":float(args["y"])-radius,
                          "w":radius*2,"h":radius*2}
            elif name in {"line", "arrow"}:
                bounds = self._bounds([(float(args["x1"]),float(args["y1"])),
                                       (float(args["x2"]),float(args["y2"]))], 5)
            elif name in {"freehand","polygon"}:
                points = [(float(p["x"]),float(p["y"])) for stroke in args.get("strokes",[])
                          for p in stroke.get("points",[])]
                if name == "polygon":
                    points=[(float(p["x"]),float(p["y"])) for p in args.get("points",[])]
                bounds = self._bounds(points, 5)
            elif name in {"arc","angle_mark"}:
                rx=float(args["rx"]) if name=="arc" else float(args["radius"])+35
                ry=float(args["ry"]) if name=="arc" else float(args["radius"])+35
                bounds={"x":float(args["cx"])-rx,"y":float(args["cy"])-ry,"w":2*rx,"h":2*ry}
            else:  # dimension
                p1=(float(args["x1"]),float(args["y1"]));p2=(float(args["x2"]),float(args["y2"]))
                length=max(.001,math.dist(p1,p2));nx,ny=-(p2[1]-p1[1])/length,(p2[0]-p1[0])/length
                offset=float(args["offset"]);q1=(p1[0]+nx*offset,p1[1]+ny*offset);q2=(p2[0]+nx*offset,p2[1]+ny*offset)
                bounds=self._bounds([p1,p2,q1,q2],18)
        except Exception as exc:
            return {"ok":False,"code":"invalid_geometry","error":str(exc)}
        if (bounds["x"] < 4 or bounds["y"] < 4 or bounds["x"]+bounds["w"] > 996
                or bounds["y"]+bounds["h"] > 696):
            return {"ok":False,"code":"outside_safe_area",
                    "error":"La acción toca o sale del margen seguro de la pizarra.","planned_bounds":bounds}
        if name == "ellipse" and (bounds["w"] > 420 or bounds["h"] > 340
                                    or bounds["w"]*bounds["h"] > 120000):
            return {"ok":False,"code":"giant_ellipse",
                    "error":"Elipse decorativa desproporcionada; divide la explicación en objetos útiles.",
                    "planned_bounds":bounds}
        texts = [obj for obj in state["objects"] if obj["type"] in {"write","math_formula","math_matrix","dimension"}]
        if name in {"write","math_formula","render_equation","math_matrix"}:
            collisions = [obj["id"] for obj in texts if self._intersects(bounds,obj["bounds"],4)]
            if collisions:
                return {"ok":False,"code":"text_collision",
                        "error":"El texto nuevo se solaparía con texto existente.",
                        "collides_with":collisions,"planned_bounds":bounds}
        if name in {"line","arrow"}:
            start=(float(args["x1"]),float(args["y1"])); end=(float(args["x2"]),float(args["y2"]))
            if math.dist(start,end) > 650:
                return {"ok":False,"code":"giant_connector",
                        "error":"Conector desproporcionadamente largo; acerca los componentes o divide la página.",
                        "planned_bounds":bounds}
            crossed=[obj["id"] for obj in texts if self._intersects(bounds,obj["bounds"],0)
                     and not self._contains_point(obj["bounds"],start)
                     and not self._contains_point(obj["bounds"],end)]
            if crossed:
                return {"ok":False,"code":"line_crosses_text",
                        "error":"La línea atravesaría texto que debe seguir siendo legible.",
                        "collides_with":crossed,"planned_bounds":bounds}
        return {"ok":True,"planned_bounds":bounds}

    VISUAL_PRIMITIVES = {"write","line","arrow","rectangle","ellipse","freehand","point","erase",
                         "math_formula","render_equation","math_matrix","plot_function","polygon","arc",
                         "angle_mark","dimension","draw_diagram","highlight_region","annotate_live",
                         "split_view","matrix_operations","token_visualizer","simulate_training",
                         "create_poll","spawn_slider","code_sandbox","generate_exercise"}

    def semantic_state(self) -> dict[str, Any]:
        with self.lock:
            objects = [{
                "id":obj["id"], "type":obj["type"], "bounds":obj["bounds"],
                **({"text":obj.get("text"), "lines":obj.get("lines"),
                    "actual_height":obj.get("actual_height"),
                    "auto_fitted":obj.get("auto_fitted", False)} if obj["type"] == "write" else {}),
                **({"partially_erased":True} if obj.get("partially_erased") else {}),
                **({key:copy.deepcopy(obj[key]) for key in (
                    "latex","expression","title","domain","range","label","measured_length",
                    "vertices","closed","center","radii","radius","angles","endpoints","offset",
                    "rows","brackets","diagram_type","nodes","edges","layout","regions","panels",
                    "token_ids","pieces","encoding","model","dataset","epochs","loss","accuracy",
                    "poll_id","question","options","votes","widget_id","variable","minimum","maximum",
                    "value","language","code","execution","exercise_id","topic","difficulty","prompt",
                    "hints","status","operation","result","simulated"
                ) if key in obj}),
            } for obj in self.objects]
            warnings = [
                {"object_id":obj["id"], "issue":"partially_erased", "bounds":obj["bounds"]}
                for obj in self.objects if obj.get("partially_erased")
            ]
            return {
                "backend":"native", "board_size":{"w":1000,"h":700},
                "page":self.page_number, "object_count":len(objects), "objects":objects,
                "warnings":warnings, "last_action":copy.deepcopy(self.last_report),
                "undo_available":bool(self.undo_stack), "redo_available":bool(self.redo_stack),
                "archived_pages":len(self.archived_pages),
            }

    def diagnose(self) -> Path:
        path = app_data_dir() / "native_board_state.json"
        path.write_text(json.dumps(self.semantic_state(), indent=2, ensure_ascii=False), encoding="utf-8")
        self.log(f"Estado exacto de la pizarra guardado: {path}")
        return path

    def close(self) -> None:
        self.closed = True
        if self.window:
            try:
                self._ui_sync(self.window.destroy)
            except Exception:
                pass


class SpeechIO:
    SAMPLE_RATE = 16000
    FRAME_MS = 20
    FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000

    def __init__(self, config: dict[str, Any], on_text: Callable[[str], None], log: Callable[[str], None],
                 meter_callback: Callable[[float, bool], None] | None = None,
                 caption_callback: Callable[[str, str], None] | None = None):
        self.config = config
        self.on_text = on_text
        self.log = log
        self.meter_callback = meter_callback
        self.caption_callback = caption_callback
        self.stop_event = threading.Event()
        self.tts_stop = threading.Event()
        self.speaking = threading.Event()
        self.current_tts = ""
        self.audio_queue: queue.Queue[bytes] = queue.Queue(maxsize=300)
        self.stream = None
        self.stt = None
        self.listener_thread: threading.Thread | None = None
        self.tts_lock = threading.Lock()
        self.kokoro = None
        self.kokoro_failed = False
        self.audio_sink: Callable[[str], None] | None = None

    @staticmethod
    def input_devices() -> list[tuple[int, str]]:
        try:
            import sounddevice as sd
            return [(index, str(device.get("name", f"Micrófono {index}")))
                    for index, device in enumerate(sd.query_devices())
                    if int(device.get("max_input_channels", 0)) > 0]
        except Exception:
            return []

    def start(self) -> None:
        import sounddevice as sd
        import webrtcvad
        from faster_whisper import WhisperModel

        if self.stream is not None:
            return
        self.stop_event.clear()
        if self.stt is None:
            self.log(f"Cargando Whisper {self.config['stt_model']}…")
            compute = "float16" if self.config["stt_device"] == "cuda" else "int8"
            self.stt = WhisperModel(self.config["stt_model"], device=self.config["stt_device"], compute_type=compute)
            self.vad = webrtcvad.Vad(2)

        def callback(indata, frames, _time_info, status):
            if status:
                self.log(f"Audio: {status}")
            raw = bytes(indata)
            try:
                self.audio_queue.put_nowait(raw)
            except queue.Full:
                try:
                    self.audio_queue.get_nowait()
                    self.audio_queue.put_nowait(raw)
                except queue.Empty:
                    pass

        self.stream = sd.RawInputStream(
            samplerate=self.SAMPLE_RATE, channels=1, dtype="int16",
            blocksize=self.FRAME_SAMPLES, callback=callback,
            device=self.config.get("microphone_device"),
        )
        self.stream.start()
        self.listener_thread = threading.Thread(target=self._listen_loop, daemon=True)
        self.listener_thread.start()
        self.log("Micrófono activo — puedes interrumpir hablando")

    def _listen_loop(self) -> None:
        import numpy as np
        speech: list[bytes] = []
        preroll: list[bytes] = []
        silence_frames = 0
        active = False
        while not self.stop_event.is_set():
            try:
                frame = self.audio_queue.get(timeout=.2)
            except queue.Empty:
                continue
            if len(frame) != self.FRAME_SAMPLES * 2:
                continue
            rms = float(np.sqrt(np.mean(np.frombuffer(frame, dtype=np.int16).astype(np.float32) ** 2)))
            voiced = self.vad.is_speech(frame, self.SAMPLE_RATE) and rms > 180
            if self.meter_callback:
                # Curva logarítmica: el habla normal ocupa visualmente buena parte del medidor.
                level = max(0.0, min(1.0, math.log10(1.0+rms)/4.15))
                try:
                    self.meter_callback(level, voiced)
                except Exception:
                    pass
            preroll.append(frame)
            preroll = preroll[-10:]
            if voiced:
                if not active:
                    active = True
                    speech = list(preroll)
                else:
                    speech.append(frame)
                silence_frames = 0
            elif active:
                speech.append(frame)
                silence_frames += 1
                if silence_frames >= 28:  # ~560 ms
                    if len(speech) >= 22:
                        threading.Thread(target=self._transcribe, args=(b"".join(speech),), daemon=True).start()
                    speech, active, silence_frames = [], False, 0

    def _transcribe(self, pcm: bytes) -> None:
        import numpy as np
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        try:
            segments, _ = self.stt.transcribe(
                audio, language=self.config["language"], beam_size=3,
                vad_filter=True, condition_on_previous_text=False,
            )
            text = " ".join(s.text.strip() for s in segments).strip()
            if not text:
                return
            if self.speaking.is_set():
                heard = normalize_text(text)
                spoken = normalize_text(self.current_tts)
                similarity = SequenceMatcher(None, heard, spoken).ratio() if heard and spoken else 0
                # Descarta el eco; conserva órdenes claras de barge-in.
                interrupt_word = bool(re.search(r"\b(para|espera|alto|oye|no|stop)\b", heard))
                if similarity > .58 and not interrupt_word:
                    return
            self.on_text(text)
        except Exception as exc:
            self.log(f"STT: {exc}")

    async def _synthesize(self, text: str, path: str) -> None:
        import edge_tts
        communicator = edge_tts.Communicate(text, self.config["voice"], rate="-4%")
        await communicator.save(path)

    def _kokoro_paths(self) -> tuple[Path, Path]:
        folder = app_data_dir() / "kokoro"
        folder.mkdir(parents=True, exist_ok=True)
        return folder / "kokoro-v1.0.onnx", folder / "voices-v1.0.bin"

    def _ensure_kokoro(self) -> Any:
        if self.kokoro is not None:
            return self.kokoro
        model, voices = self._kokoro_paths()
        urls = {
            model:"https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1/kokoro-v1.0.onnx",
            voices:"https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1/voices-v1.0.bin",
        }
        for target, url in urls.items():
            if target.exists() and target.stat().st_size > 100_000:
                continue
            self.log(f"Descargando voz Kokoro: {target.name}…")
            partial = target.with_suffix(target.suffix+".part")
            urllib.request.urlretrieve(url, partial)
            partial.replace(target)
        from kokoro_onnx import Kokoro
        self.kokoro = Kokoro(str(model), str(voices))
        self.log(f"Kokoro preparado — voz {self.config.get('kokoro_voice','ef_dora')}")
        return self.kokoro

    def _synthesize_kokoro(self, text: str, path: str) -> None:
        import soundfile as sf
        kokoro = self._ensure_kokoro()
        samples, sample_rate = kokoro.create(
            text, voice=self.config.get("kokoro_voice", "ef_dora"),
            speed=0.98, lang="es",
        )
        sf.write(path, samples, sample_rate)

    @staticmethod
    def clean_for_speech(text: str) -> str:
        """Convierte Markdown del modelo en prosa; el TTS nunca verbaliza delimitadores."""
        value=str(text)
        value=re.sub(r"```(?:[A-Za-z0-9_+-]+)?\s*(.*?)```",r"\1",value,flags=re.S)
        value=re.sub(r"!\[([^]]*)\]\([^)]*\)",r"\1",value)
        value=re.sub(r"\[([^]]+)\]\([^)]*\)",r"\1",value)
        value=re.sub(r"(?m)^\s{0,3}#{1,6}\s*","",value)
        value=re.sub(r"(?m)^\s*[-+*]\s+","",value)
        value=re.sub(r"(?m)^\s*>\s?","",value)
        value=value.replace("**","").replace("__","").replace("~~","")
        value=value.replace("*","").replace("`","").replace("_"," ")
        value=re.sub(r"\s+"," ",value).strip()
        return value

    def speak(self, text: str, cancel: threading.Event) -> None:
        text=self.clean_for_speech(text)
        if not text.strip():
            return
        import pygame
        with self.tts_lock:
            self.tts_stop.clear()
            self.current_tts = text
            self.speaking.set()
            use_kokoro = self.config.get("tts_engine", "kokoro") == "kokoro" and not self.kokoro_failed
            fd, path = tempfile.mkstemp(suffix=".wav" if use_kokoro else ".mp3", prefix="paint_professor_")
            os.close(fd)
            try:
                if use_kokoro:
                    try:
                        self._synthesize_kokoro(text, path)
                    except Exception as exc:
                        self.kokoro_failed = True
                        self.log(f"Kokoro no está disponible ({exc}); continúo con Edge TTS")
                        try: os.unlink(path)
                        except OSError: pass
                        fd, path = tempfile.mkstemp(suffix=".mp3", prefix="paint_professor_")
                        os.close(fd)
                        asyncio.run(self._synthesize(text, path))
                else:
                    asyncio.run(self._synthesize(text, path))
                if self.audio_sink:
                    try: self.audio_sink(path)
                    except Exception as exc: self.log(f"Discord audio: {exc}")
                if cancel.is_set() or self.tts_stop.is_set():
                    return
                if not pygame.mixer.get_init():
                    pygame.mixer.init()
                pygame.mixer.music.load(path)
                if self.caption_callback:
                    try: self.caption_callback("start", text)
                    except Exception: pass
                pygame.mixer.music.play()
                while pygame.mixer.music.get_busy():
                    if cancel.is_set() or self.tts_stop.is_set():
                        pygame.mixer.music.stop()
                        break
                    time.sleep(.025)
            finally:
                if self.caption_callback:
                    try: self.caption_callback("end", text)
                    except Exception: pass
                self.speaking.clear()
                self.current_tts = ""
                try:
                    os.unlink(path)
                except OSError:
                    pass

    def interrupt(self) -> None:
        self.tts_stop.set()
        try:
            import pygame
            if pygame.mixer.get_init():
                pygame.mixer.music.stop()
        except Exception:
            pass

    def stop_listening(self) -> None:
        self.stop_event.set()
        if self.stream:
            try: self.stream.stop()
            except Exception: pass
            try: self.stream.close()
            except Exception: pass
        self.stream = None
        self.listener_thread = None
        self.meter_callback and self.meter_callback(0.0, False)

    def close(self) -> None:
        self.stop_listening()
        self.interrupt()


SYSTEM_PROMPT = """
Eres Paint Professor, una profesora cercana, precisa y visual que controla una pizarra docente.
Hablas en español natural. Tu alumno puede interrumpirte en cualquier momento.

REGLAS DE PIZARRA:
- PRINCIPIO CENTRAL: toda explicación sustantiva debe existir primero en la pizarra de forma MUY
  gráfica y visual. No sustituyas una demostración por un párrafo hablado. Convierte cada idea en
  relaciones visibles: diagramas, flujo, jerarquías, comparaciones, ejemplos, colores, fórmulas,
  gráficas, matrices, código o simulaciones según corresponda. El alumno debe poder entender la
  estructura principal mirando la pizarra incluso con el audio silenciado.
- Para cada concepto nuevo muestra al menos una representación concreta y una relación con lo que
  ya existe. Prefiere draw_diagram, plot_function, render_equation, split_view, highlight_region,
  matrix_operations, token_visualizer o una demostración específica antes que listas de texto.
  write sirve para etiquetas y conclusiones breves, nunca para volcar la explicación completa.
- No finalices una explicación sin un bloque visual coherente y verificable. Una respuesta solo
  oral es válida para saludos, confirmaciones breves, preguntas de aclaración o conversación que
  no enseñe contenido. Si estás enseñando, primero investiga/planifica, después dibuja y narra a la vez.
- SALIDA Y VOZ SIN MARKDOWN: no uses asteriscos, dobles asteriscos, guiones bajos, backticks,
  encabezados con almohadilla ni sintaxis Markdown en say, voice_explain, narration_script o en la
  respuesta que será hablada. Di el énfasis con entonación y frases naturales. Jamás pronuncies
  «asterisco», «asterisco asterisco» ni nombres de delimitadores de formato.
- Antes de planificar un tema factual, técnico, científico, histórico o potencialmente actualizado,
  llama a research_verified_sources. Copia las referencias pertinentes a source_refs de plan_lesson
  y relaciona cada afirmación material con una fuente. Contrasta lo importante: Wikipedia es
  terciaria, arXiv puede ser un preprint y Crossref verifica metadatos, no resultados. Si la consulta
  falla, dilo y no inventes referencias. Una operación matemática autosuficiente puede usar una
  lista source_refs vacía.
- El tablero lógico mide 1000 x 700. Reserva márgenes y no salgas de ellos.
- Ante una nueva explicación o dibujo, llama primero a plan_lesson. Define composición, orden,
  páginas estimadas y criterios visuales de éxito ANTES de tocar Paint. Ejecuta ese plan por bloques
  coherentes; solo replantea si el auditor visual demuestra que algo salió mal.
- plan_lesson incluye un guion oral separado. narration_script debe explicar el porqué y el flujo,
  no leer literalmente lo escrito. El sistema reproducirá cada fragmento mientras dibujas el paso
  correspondiente. Haz una narración breve por paso, con lenguaje hablado y sin Markdown.
- Incluye en key_claims solo afirmaciones técnicas comprobables necesarias para la clase. Un crítico
  independiente revisará plan, afirmaciones y guion antes de autorizar la primera coordenada. Si lo
  rechaza, corrige el plan; no intentes dibujar saltándote la revisión.
- Usa say para narrar frases cortas y alterna narración con dibujo, como una profesora real.
- Si existe narration_script aprobado, no dupliques ese contenido con say: el sistema ya lo habla
  en paralelo con los trazos. Usa say solo para una reacción directa o una pregunta al alumno.
- Para texto usa write. La escritura es monolineal y se dibuja carácter por carácter. Escribe
  SOLO texto simple: letras, números y puntuación ASCII. No pongas emojis, pictogramas ni flechas
  Unicode en write: usa palabras breves y la herramienta arrow para las conexiones.
- MATEMÁTICAS: jamás falsifiques notación avanzada con write o freehand. Usa math_formula para
  fracciones, potencias, raíces, límites, derivadas, integrales, sumatorios, vectores, sistemas y
  símbolos griegos; usa math_matrix para matrices y determinantes. Usa plot_function cuando la forma de una función aporte comprensión;
  elige dominio y rango que muestren el fenómeno importante y explica oralmente escala y unidades.
- Para arquitecturas, árboles, redes neuronales y flujos usa draw_diagram: sus nodos y conexiones
  quedan registrados y legibles. Usa split_view para coordinar código, gráfica y diagrama; usa
  highlight_region o gesture_point para señalar un objeto existente sin dibujar círculos gigantes.
- simulate_training siempre es una simulación didáctica: dilo y no atribuyas sus métricas a un
  entrenamiento real. token_visualizer solo representa el encoding solicitado, no el tokenizer
  de Qwen/DeepSeek salvo coincidencia demostrada. code_sandbox es Python aislado y su salida real
  es la única evidencia de ejecución. grade_snippet no aprueba semántica sin tests/evidencia.
- MODELO DEL ALUMNO: track_mastery solo después de una respuesta o actuación observable; cita esa
  evidencia y usa confianza conservadora. No subas dominio porque tú hayas explicado algo.
  detect_misconception exige identificar el razonamiento concreto, no etiquetar simplemente «mal».
  Si faltan pruebas, pregunta. adapt_difficulty evita cambios bruscos por un único intento y
  spaced_repetition_schedule conserva fechas reales. recall_previous_session puede decir «no hay
  evidencia»: jamás inventes que algo le costó la semana pasada.
- Reexplicar no es repetir más despacio: set_explanation_mode debe declarar un ángulo nuevo.
  Alterna intuición, analogía relevante, matemática formal e implementación sin perder el objeto
  visual original. Usa preguntas guía, espera interrumpible, confianza previa y reflexión final
  cuando aporten aprendizaje; no conviertas cada turno en un interrogatorio.
- DIBUJO TÉCNICO: usa polygon para perfiles y vistas, arc/ellipse para radios y taladros, line con
  width 1 para auxiliares, y dimension para TODA medida. Una cota debe quedar fuera de la pieza,
  no atravesarla, indicar unidades y no duplicar otra cota. Conserva alineación, simetría, jerarquía
  de línea y suficiente separación. No uses freehand para geometría técnica.
- Construye fórmulas y planos de izquierda a derecha y de lo general a lo particular. No mezcles
  una derivación completa en una sola fórmula: muestra estados consecutivos alineados y conecta
  cada transformación con la explicación oral correspondiente.
- Para dibujos orgánicos (manos, caras, animales, objetos, iconos o siluetas) usa SIEMPRE
  freehand. Tú decides las coordenadas reales y su orden: cada point es una ancla (x,y) por
  la que debe pasar el lápiz. Usa 8–40 anclas por contorno y varios strokes para detalles.
  El motor solo interpola tus anclas a 120 Hz; no reemplaza tu dibujo por figuras geométricas.
- No construyas formas orgánicas apilando elipses o rectángulos. Agrupa en una llamada
  freehand los trazos del mismo color/grosor, ordenados como los haría una mano humana:
  contorno principal primero y detalles interiores después.
- No uses freehand como decoración de un diagrama técnico. En explicaciones como RAG, ROS 2 o
  redes usa write, rectangle, line y arrow. Reserva freehand para una ilustración orgánica que
  el alumno haya pedido o que sea pedagógicamente necesaria.
- Antes de añadir contenido, piensa en la composición. No amontones elementos.
- Reserva aproximadamente y=25..90 para el título y y=130..640 para el contenido. Dentro de una
  caja, usa etiquetas de 1–3 palabras, height 22–32 y suficiente max_width. Una línea de N letras
  ocupa aproximadamente N * height * 0.72; calcula el espacio antes de escribir.
- Colores recomendados: #1F2937 texto, #2563EB conceptos, #DC2626 alerta/error,
  #16A34A acierto, #7C3AED énfasis. Fondo blanco.
- PROHIBIDO usar #FFFFFF, blanco o colores casi blancos: serían invisibles sobre el lienzo.
- width: 1 fino, 2 normal, 3 énfasis, 4 grueso. speed: 0.6 lento, 1 normal, 1.5 rápido.
- Usa diagramas, flechas y ejemplos. No vuelques párrafos enteros en Paint.
- Cuando el alumno pida dibujar o explicar algo, EMPIEZA INMEDIATAMENTE. No preguntes permiso
  para comenzar ni digas "preparando". Haz un bloque visual útil y completo antes de preguntar.
- Un bloque visual normal debe incluir al menos: título, 3 componentes y sus conexiones; usa entre
  5 y 12 herramientas de dibujo. Eso es un BLOQUE, no el límite de la clase: una explicación
  profunda puede encadenar tantos bloques como necesite. No termines tras clear o una sola figura.
- Durante clases largas recibirás LESSON_CHECKPOINT automáticos. No significan que debas parar.
  Revisa la captura más reciente y continúa hasta contestar de verdad. Si el lienzo está lleno,
  usa next_page: guardará la página como PNG, la limpiará y podrás seguir con «PARTE 2», «PARTE 3»…
  No uses clear para cambiar de página porque perdería la página anterior sin archivarla.
- Como máximo usa una frase corta de say antes de empezar a dibujar. Narra mientras avanzas.
- Si una herramienta devuelve ok=false, esa acción NO ocurrió: no afirmes que está hecha. Reintenta
  una sola vez con #1F2937 y width 2; si vuelve a fallar, explica el error exacto sin fingir éxito.
- Después de cada bloque de acciones visuales recibirás un mensaje BOARD_MONITOR con una captura
  REAL del lienzo. No es otra petición del alumno. Antes de continuar debes inspeccionar los píxeles:
  comprueba que el trazo existe, el texto es legible y completo, no hay solapamientos, no hay marcas
  sin significado y la composición coincide con tu explicación. Si ves un fallo, corrígelo de
  inmediato con undo, erase o un redibujado mejor colocado. No sigas acumulando errores.
- La intención de una tool call NO demuestra que el dibujo salió bien. Solo puedes afirmar «listo»
  si la captura BOARD_MONITOR más reciente muestra realmente un bloque visual coherente.
- BOARD_AUDIT es un informe de un crítico visual separado que compara los píxeles reales con la
  orden exacta. Usa sus regiones y evidencias, pero no borres automáticamente por una sugerencia.
  Un espacio vacío significa «aún incompleto», no «hay que borrar».
- Cuando BOARD_MONITOR incluya BOARD_STATE VECTORIAL EXACTO, ese estado es la verdad autoritativa:
  contiene cada ID, tipo, texto, líneas finales y límites realmente renderizados. No imagines lo que
  puede haber ocurrido ni contradigas ese estado. Si warnings está vacío, NO borres por intuición.
  Si la última acción quedó mal, prefiere undo: en la pizarra nativa deshace exactamente una acción
  completa sin dañar ningún objeto vecino. Usa erase solo para una corrección local deliberada.
- erase es cirugía local: úsalo únicamente cuando puedas indicar en reason el defecto visible exacto.
  Borra la caja mínima alrededor del error, con size 16–40 según el área. Nunca borres contenido
  correcto para reorganizarlo, nunca encadenes dos erase sin redibujar entre ellos y nunca uses erase
  para limpiar la página completa. Para eso existen next_page y clear.
- Si la captura muestra contenido previo, reutilízalo. Si el alumno interrumpe, responde a su duda
  antes de continuar el plan anterior.
- Nunca digas que has dibujado algo sin llamar a la herramienta correspondiente.
- No expongas razonamiento privado. Sí ofrece una explicación pedagógica visible y verificable.
- Cuando termines un bloque, pregunta algo breve para comprobar comprensión.
""".strip()


def object_schema(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


NUM = {"type": "number"}
COLOUR = {"type": "string", "pattern": "^#[0-9A-Fa-f]{6}$"}
WIDTH = {"type": "integer", "minimum": 1, "maximum": 4}
SPEED = {"type": "number", "minimum": 0.4, "maximum": 2.0}
POINT_SCHEMA = {
    "type": "object",
    "properties": {
        "x": {"type": "number", "minimum": 0, "maximum": 1000},
        "y": {"type": "number", "minimum": 0, "maximum": 700},
    },
    "required": ["x", "y"],
    "additionalProperties": False,
}
FREEHAND_STROKE = {
    "type": "object",
    "properties": {
        "points": {"type": "array", "items": POINT_SCHEMA, "minItems": 2, "maxItems": 64},
        "closed": {"type": "boolean"},
    },
    "required": ["points", "closed"],
    "additionalProperties": False,
}

TOOLS = [
    {"type":"function","function":{"name":"plan_lesson","description":"Planifica la composición y los criterios de éxito antes de empezar a dibujar una nueva respuesta.","strict":True,
      "parameters":object_schema({
          "title":{"type":"string","minLength":1,"maxLength":80},
          "objective":{"type":"string","minLength":1,"maxLength":300},
          "estimated_pages":{"type":"integer","minimum":1,"maximum":8},
          "steps":{"type":"array","items":{"type":"string","minLength":1,"maxLength":160},"minItems":3,"maxItems":20},
          "success_checks":{"type":"array","items":{"type":"string","minLength":1,"maxLength":160},"minItems":2,"maxItems":10},
          "key_claims":{"type":"array","items":{"type":"string","minLength":1,"maxLength":220},"minItems":1,"maxItems":16},
          "source_refs":{"type":"array","maxItems":12,"items":{"type":"object","properties":{
              "claim":{"type":"string","minLength":1,"maxLength":220},
              "title":{"type":"string","minLength":1,"maxLength":220},
              "url":{"type":"string","pattern":"^https?://"}
          },"required":["claim","title","url"],"additionalProperties":False}},
          "narration_script":{"type":"array","items":{"type":"object","properties":{
              "step":{"type":"integer","minimum":1,"maximum":20},
              "text":{"type":"string","minLength":1,"maxLength":420}
          },"required":["step","text"],"additionalProperties":False},"minItems":3,"maxItems":20}
      })}},
    {"type":"function","function":{"name":"research_verified_sources","description":"Busca antes de planificar en catálogos conocidos con procedencia trazable: Wikipedia/Wikidata, Crossref y arXiv. Devuelve metadatos y URL; no convierte una fuente en verdad automática.","strict":True,
      "parameters":object_schema({"query":{"type":"string","minLength":3,"maxLength":240},"sources":{"type":"array","minItems":1,"maxItems":3,"uniqueItems":True,"items":{"type":"string","enum":["wikipedia","crossref","arxiv"]}},"max_results":{"type":"integer","minimum":1,"maximum":9}})}},
    {"type":"function","function":{"name":"say","description":"Habla al alumno. Una o dos frases cortas.","strict":True,
      "parameters":object_schema({"text":{"type":"string","minLength":1,"maxLength":500}})}},
    {"type":"function","function":{"name":"write","description":"Escribe a mano texto breve en la pizarra.","strict":True,
      "parameters":object_schema({"text":{"type":"string","maxLength":180},"x":NUM,"y":NUM,"height":NUM,"max_width":NUM,"color":COLOUR,"width":WIDTH,"speed":SPEED})}},
    {"type":"function","function":{"name":"math_formula","description":"Compone notación matemática profesional con LaTeX: fracciones, raíces, límites, sumatorios, integrales, vectores y sistemas. Para matrices usa math_matrix; no uses write para fórmulas.","strict":True,
      "parameters":object_schema({"latex":{"type":"string","minLength":1,"maxLength":500},"x":NUM,"y":NUM,"max_width":NUM,"max_height":NUM,"color":COLOUR})}},
    {"type":"function","function":{"name":"math_matrix","description":"Dibuja una matriz o determinante humano-legible con filas, columnas y corchetes/paréntesis reales.","strict":True,
      "parameters":object_schema({"rows":{"type":"array","items":{"type":"array","items":{"type":"string","maxLength":24},"minItems":1,"maxItems":8},"minItems":1,"maxItems":8},"x":NUM,"y":NUM,"cell_width":NUM,"cell_height":NUM,"brackets":{"type":"string","enum":["square","parentheses"]},"color":COLOUR})}},
    {"type":"function","function":{"name":"plot_function","description":"Dibuja una gráfica cartesiana legible con dominio, rango, escala, rejilla, ejes, ticks y una función segura de x.","strict":True,
      "parameters":object_schema({"expression":{"type":"string","minLength":1,"maxLength":120},"x_min":NUM,"x_max":NUM,"y_min":NUM,"y_max":NUM,"x":NUM,"y":NUM,"w":NUM,"h":NUM,"color":COLOUR,"title":{"type":"string","maxLength":80}})}},
    {"type":"function","function":{"name":"render_equation","description":"Renderiza LaTeX como fórmula matemática real; nombre preferido para notación avanzada.","strict":True,
      "parameters":object_schema({"latex":{"type":"string","minLength":1,"maxLength":500},"x":NUM,"y":NUM,"max_width":NUM,"max_height":NUM,"color":COLOUR})}},
    {"type":"function","function":{"name":"draw_diagram","description":"Crea un diagrama semántico con layout automático, nodos legibles y aristas que no atraviesan etiquetas.","strict":True,
      "parameters":object_schema({"diagram_type":{"type":"string","enum":["flowchart","decision_tree","tree","neural_network","architecture"]},
        "nodes":{"type":"array","minItems":2,"maxItems":24,"items":{"type":"object","properties":{"id":{"type":"string","minLength":1,"maxLength":24},"label":{"type":"string","minLength":1,"maxLength":60},"kind":{"type":"string","enum":["box","circle"]},"color":COLOUR},"required":["id","label","kind","color"],"additionalProperties":False}},
        "edges":{"type":"array","maxItems":40,"items":{"type":"object","properties":{"from":{"type":"string"},"to":{"type":"string"},"label":{"type":"string","maxLength":30}},"required":["from","to","label"],"additionalProperties":False}},
        "x":NUM,"y":NUM,"w":NUM,"h":NUM,"direction":{"type":"string","enum":["horizontal","vertical"]},"title":{"type":"string","maxLength":80}})}},
    {"type":"function","function":{"name":"highlight_region","description":"Resalta una región ya existente sin destruir su contenido.","strict":True,
      "parameters":object_schema({"x":NUM,"y":NUM,"w":NUM,"h":NUM,"color":COLOUR,"label":{"type":"string","maxLength":50}})}},
    {"type":"function","function":{"name":"annotate_live","description":"Añade una nota breve de apoyo mientras se explica, sin recargar la escena.","strict":True,
      "parameters":object_schema({"text":{"type":"string","minLength":1,"maxLength":180},"x":NUM,"y":NUM,"color":COLOUR})}},
    {"type":"function","function":{"name":"split_view","description":"Divide una zona en paneles coordinados para código, gráfica, fórmula o diagrama.","strict":True,
      "parameters":object_schema({"panels":{"type":"array","minItems":2,"maxItems":4,"items":{"type":"object","properties":{"title":{"type":"string","minLength":1,"maxLength":40},"weight":{"type":"number","minimum":0.1,"maximum":10}},"required":["title","weight"],"additionalProperties":False}},"orientation":{"type":"string","enum":["horizontal","vertical"]},"x":NUM,"y":NUM,"w":NUM,"h":NUM})}},
    {"type":"function","function":{"name":"matrix_operations","description":"Visualiza suma, resta o multiplicación de matrices y devuelve el resultado exacto.","strict":True,
      "parameters":object_schema({"A":{"type":"array","minItems":1,"maxItems":6,"items":{"type":"array","minItems":1,"maxItems":6,"items":NUM}},"B":{"type":"array","minItems":1,"maxItems":6,"items":{"type":"array","minItems":1,"maxItems":6,"items":NUM}},"operation":{"type":"string","enum":["add","subtract","multiply"]},"x":NUM,"y":NUM,"w":NUM,"h":NUM})}},
    {"type":"function","function":{"name":"token_visualizer","description":"Muestra tokens y sus IDs con un encoding explícito; no atribuyas este tokenizado a otro modelo.","strict":True,
      "parameters":object_schema({"text":{"type":"string","minLength":1,"maxLength":500},"encoding":{"type":"string","enum":["cl100k_base","o200k_base","p50k_base"]},"x":NUM,"y":NUM,"w":NUM,"h":NUM})}},
    {"type":"function","function":{"name":"simulate_training","description":"Simula curvas educativas de loss/accuracy. Siempre se rotula como simulación, nunca como entrenamiento real.","strict":True,
      "parameters":object_schema({"model":{"type":"string","minLength":1,"maxLength":60},"dataset":{"type":"string","minLength":1,"maxLength":60},"epochs":{"type":"integer","minimum":5,"maximum":100},"x":NUM,"y":NUM,"w":NUM,"h":NUM})}},
    {"type":"function","function":{"name":"create_poll","description":"Crea una pregunta de pulso con opciones y estado semántico abierto.","strict":True,
      "parameters":object_schema({"question":{"type":"string","minLength":1,"maxLength":180},"options":{"type":"array","minItems":2,"maxItems":6,"items":{"type":"string","minLength":1,"maxLength":80}},"x":NUM,"y":NUM,"w":NUM,"h":NUM})}},
    {"type":"function","function":{"name":"spawn_slider","description":"Crea un control visual de hiperparámetro con valor y rango exactos.","strict":True,
      "parameters":object_schema({"variable":{"type":"string","minLength":1,"maxLength":30},"minimum":NUM,"maximum":NUM,"value":NUM,"x":NUM,"y":NUM,"w":NUM,"color":COLOUR})}},
    {"type":"function","function":{"name":"code_sandbox","description":"Crea un panel Python y, si se pide, lo ejecuta en sandbox local aislado sin imports, archivos, red ni procesos.","strict":True,
      "parameters":object_schema({"language":{"type":"string","enum":["python"]},"initial_code":{"type":"string","minLength":1,"maxLength":6000},"x":NUM,"y":NUM,"w":NUM,"h":NUM,"run":{"type":"boolean"}})}},
    {"type":"function","function":{"name":"grade_snippet","description":"Ejecuta código Python aislado y reporta evidencia; no inventa que criterios semánticos pasaron sin tests.","strict":True,
      "parameters":object_schema({"language":{"type":"string","enum":["python"]},"code":{"type":"string","minLength":1,"maxLength":6000},"criteria":{"type":"array","minItems":1,"maxItems":8,"items":{"type":"string","maxLength":120}}})}},
    {"type":"function","function":{"name":"generate_exercise","description":"Publica un ejercicio relacionado y conserva la respuesta esperada oculta en el estado semántico.","strict":True,
      "parameters":object_schema({"topic":{"type":"string","minLength":1,"maxLength":60},"difficulty":{"type":"string","enum":["beginner","intermediate","advanced"]},"prompt":{"type":"string","minLength":1,"maxLength":500},"hints":{"type":"array","maxItems":3,"items":{"type":"string","maxLength":140}},"expected_answer":{"type":"string","minLength":1,"maxLength":1000},"x":NUM,"y":NUM,"w":NUM,"h":NUM})}},
    {"type":"function","function":{"name":"save_snapshot","description":"Guarda imagen y estado semántico completo con un nombre estable.","strict":True,"parameters":object_schema({"name":{"type":"string","minLength":1,"maxLength":80}})}},
    {"type":"function","function":{"name":"load_snapshot","description":"Restaura imagen y objetos exactos de un snapshot anterior.","strict":True,"parameters":object_schema({"name":{"type":"string","minLength":1,"maxLength":80}})}},
    {"type":"function","function":{"name":"export_board","description":"Exporta la pizarra actual a PNG, PDF o JSON semántico.","strict":True,"parameters":object_schema({"format":{"type":"string","enum":["png","pdf","json"]},"name":{"type":"string","minLength":1,"maxLength":80}})}},
    {"type":"function","function":{"name":"track_mastery","description":"Actualiza el dominio de un concepto solo con evidencia observable del alumno.","strict":True,
      "parameters":object_schema({"concept":{"type":"string","minLength":1,"maxLength":80},"outcome":{"type":"string","enum":["correct","partial","incorrect"]},"evidence":{"type":"string","minLength":3,"maxLength":300},"confidence":{"type":"number","minimum":0,"maximum":1}})}},
    {"type":"function","function":{"name":"detect_misconception","description":"Registra un error de razonamiento concreto con cita/evidencia; no acepta diagnósticos vagos.","strict":True,
      "parameters":object_schema({"concept":{"type":"string","minLength":1,"maxLength":80},"student_response":{"type":"string","minLength":1,"maxLength":800},"misconception":{"type":"string","minLength":5,"maxLength":240},"evidence":{"type":"string","minLength":3,"maxLength":300},"confidence":{"type":"number","minimum":0,"maximum":1}})}},
    {"type":"function","function":{"name":"adapt_difficulty","description":"Calcula la dificultad siguiente desde rendimiento reciente; no cambia bruscamente por una sola respuesta.","strict":True,
      "parameters":object_schema({"concept":{"type":"string","minLength":1,"maxLength":80},"performance":{"type":"number","minimum":0,"maximum":1}})}},
    {"type":"function","function":{"name":"spaced_repetition_schedule","description":"Programa repasos por concepto usando dominio, errores y fecha del último intento.","strict":True,
      "parameters":object_schema({"concepts":{"type":"array","minItems":1,"maxItems":20,"items":{"type":"string","minLength":1,"maxLength":80}}})}},
    {"type":"function","function":{"name":"set_explanation_mode","description":"Cambia el ángulo explicativo sin repetir: reexplicación, analogía o nivel de abstracción.","strict":True,
      "parameters":object_schema({"concept":{"type":"string","minLength":1,"maxLength":80},"mode":{"type":"string","enum":["reexplain","analogy","zoom_abstraction"]},"level":{"type":"string","enum":["intuition","beginner","intermediate","formal_math","implementation"]},"interest_domain":{"type":"string","maxLength":80},"new_angle":{"type":"string","minLength":8,"maxLength":300}})}},
    {"type":"function","function":{"name":"ask_guiding_question","description":"Formula una pregunta socrática concreta y la conserva como hilo pendiente.","strict":True,
      "parameters":object_schema({"context":{"type":"string","minLength":1,"maxLength":300},"question":{"type":"string","minLength":3,"maxLength":300},"target_insight":{"type":"string","minLength":3,"maxLength":240}})}},
    {"type":"function","function":{"name":"wait_for_struggle","description":"Espera de forma interrumpible antes de ofrecer pista; máximo 30 segundos.","strict":True,
      "parameters":object_schema({"timeout":{"type":"integer","minimum":1,"maximum":30},"then_hint":{"type":"string","minLength":1,"maxLength":240}})}},
    {"type":"function","function":{"name":"ask_confidence","description":"Pregunta el nivel de seguridad antes de corregir y abre una medición metacognitiva.","strict":True,
      "parameters":object_schema({"question":{"type":"string","minLength":3,"maxLength":300},"scale":{"type":"integer","minimum":3,"maximum":10}})}},
    {"type":"function","function":{"name":"record_reflection","description":"Registra el resumen o razonamiento del alumno sin atribuirle contenido no dicho.","strict":True,
      "parameters":object_schema({"kind":{"type":"string","enum":["reasoning","session_reflection","confidence"]},"student_text":{"type":"string","minLength":1,"maxLength":1200},"concepts":{"type":"array","maxItems":12,"items":{"type":"string","maxLength":80}}})}},
    {"type":"function","function":{"name":"recall_previous_session","description":"Recupera evidencia pedagógica persistida sobre un tema; nunca inventa recuerdos.","strict":True,
      "parameters":object_schema({"topic":{"type":"string","minLength":1,"maxLength":80}})}},
    {"type":"function","function":{"name":"build_learning_narrative","description":"Actualiza el hilo conductor del curso con hechos ya enseñados y siguiente puente.","strict":True,
      "parameters":object_schema({"chapter":{"type":"string","minLength":1,"maxLength":100},"learned":{"type":"array","minItems":1,"maxItems":12,"items":{"type":"string","maxLength":120}},"next_bridge":{"type":"string","minLength":3,"maxLength":240}})}},
    {"type":"function","function":{"name":"voice_explain","description":"Narra un fragmento breve sincronizado con el paso visual actual.","strict":True,
      "parameters":object_schema({"text":{"type":"string","minLength":1,"maxLength":500},"visual_object_id":{"type":"string","maxLength":32}})}},
    {"type":"function","function":{"name":"gesture_point","description":"Señala un objeto semántico existente por ID; nunca inventa coordenadas de un elemento ausente.","strict":True,
      "parameters":object_schema({"element_id":{"type":"string","minLength":1,"maxLength":32},"color":COLOUR})}},
    {"type":"function","function":{"name":"polygon","description":"Traza polígonos, perfiles, vistas ortográficas y geometría técnica mediante vértices exactos.","strict":True,
      "parameters":object_schema({"points":{"type":"array","items":POINT_SCHEMA,"minItems":2,"maxItems":64},"closed":{"type":"boolean"},"color":COLOUR,"width":WIDTH,"speed":SPEED})}},
    {"type":"function","function":{"name":"arc","description":"Traza un arco matemático o técnico exacto mediante centro, radios y ángulos en grados.","strict":True,
      "parameters":object_schema({"cx":NUM,"cy":NUM,"rx":NUM,"ry":NUM,"start_deg":NUM,"end_deg":NUM,"color":COLOUR,"width":WIDTH,"speed":SPEED})}},
    {"type":"function","function":{"name":"angle_mark","description":"Marca un ángulo con arco geométrico exacto y etiqueta en grados o radianes.","strict":True,
      "parameters":object_schema({"cx":NUM,"cy":NUM,"radius":NUM,"start_deg":NUM,"end_deg":NUM,"label":{"type":"string","maxLength":32},"color":COLOUR,"width":WIDTH})}},
    {"type":"function","function":{"name":"dimension","description":"Añade una cota técnica legible con líneas auxiliares, doble punta, valor/etiqueta y unidades.","strict":True,
      "parameters":object_schema({"x1":NUM,"y1":NUM,"x2":NUM,"y2":NUM,"offset":NUM,"label":{"type":"string","maxLength":40},"units":{"type":"string","maxLength":12},"color":COLOUR})}},
    {"type":"function","function":{"name":"line","description":"Dibuja una línea.","strict":True,
      "parameters":object_schema({"x1":NUM,"y1":NUM,"x2":NUM,"y2":NUM,"color":COLOUR,"width":WIDTH,"speed":SPEED})}},
    {"type":"function","function":{"name":"arrow","description":"Dibuja una flecha.","strict":True,
      "parameters":object_schema({"x1":NUM,"y1":NUM,"x2":NUM,"y2":NUM,"color":COLOUR,"width":WIDTH,"speed":SPEED})}},
    {"type":"function","function":{"name":"rectangle","description":"Dibuja un rectángulo desde x,y con ancho w y alto h.","strict":True,
      "parameters":object_schema({"x":NUM,"y":NUM,"w":NUM,"h":NUM,"color":COLOUR,"width":WIDTH,"speed":SPEED})}},
    {"type":"function","function":{"name":"ellipse","description":"Dibuja una elipse o círculo en su caja x,y,w,h.","strict":True,
      "parameters":object_schema({"x":NUM,"y":NUM,"w":NUM,"h":NUM,"color":COLOUR,"width":WIDTH,"speed":SPEED})}},
    {"type":"function","function":{"name":"freehand","description":"Dibujo libre gobernado por coordenadas de la IA para manos, caras, objetos y formas orgánicas. Cada stroke mantiene el lápiz apoyado y recorre sus points en orden; closed cierra el contorno. El ejecutor interpola suavemente a 120 Hz.","strict":True,
      "parameters":object_schema({"strokes":{"type":"array","items":FREEHAND_STROKE,"minItems":1,"maxItems":16},"color":COLOUR,"width":WIDTH,"speed":SPEED})}},
    {"type":"function","function":{"name":"point","description":"Señala o rodea brevemente un punto importante.","strict":True,
      "parameters":object_schema({"x":NUM,"y":NUM,"color":COLOUR,"width":WIDTH})}},
    {"type":"function","function":{"name":"erase","description":"Corrige una región pequeña con una goma de tamaño configurable. Requiere explicar el defecto visible; no sirve para limpiar la pizarra.","strict":True,
      "parameters":object_schema({"x":NUM,"y":NUM,"w":NUM,"h":NUM,"size":{"type":"integer","minimum":6,"maximum":64},"speed":SPEED,"reason":{"type":"string","minLength":8,"maxLength":240}})}},
    {"type":"function","function":{"name":"undo","description":"Deshace la última operación de Paint.","strict":True,"parameters":object_schema({})}},
    {"type":"function","function":{"name":"redo","description":"Rehace la última operación deshecha.","strict":True,"parameters":object_schema({})}},
    {"type":"function","function":{"name":"zoom","description":"Acerca o aleja la vista de Paint.","strict":True,
      "parameters":object_schema({"direction":{"type":"string","enum":["in","out"]},"steps":{"type":"integer","minimum":1,"maximum":8}})}},
    {"type":"function","function":{"name":"toggle_grid","description":"Muestra u oculta la rejilla de Paint.","strict":True,"parameters":object_schema({})}},
    {"type":"function","function":{"name":"next_page","description":"Para una clase larga: archiva el lienzo actual como PNG y abre una página limpia. Continúa después escribiendo el título de la nueva parte.","strict":True,
      "parameters":object_schema({"label":{"type":"string","minLength":1,"maxLength":60}})}},
    {"type":"function","function":{"name":"clear","description":"Limpia toda la pizarra. Úsalo solo si hace falta empezar de cero.","strict":True,"parameters":object_schema({})}},
]


class ModelDownloadCancelled(RuntimeError):
    pass


class LMStudioManager:
    """Arranca llmster, elige/carga un LLM y expone su identificador OpenAI."""

    RECOMMENDED_MODEL = "qwen/qwen3.5-9b@q4_k_m"
    PROCESS_LOCK = threading.Lock()

    def __init__(self, config: dict[str, Any], log: Callable[[str], None],
                 progress: Callable[[dict[str, Any]], None] | None = None,
                 download_cancel: threading.Event | None = None):
        self.config = config
        self.log = log
        self.progress = progress
        self.download_cancel = download_cancel or threading.Event()
        self.cli = self._find_cli()
        self.model_identifier = ""
        self.daemon_boot_process: subprocess.Popen[str] | None = None
        self.server_boot_process: subprocess.Popen[str] | None = None
        self.last_daemon_diagnostic: Path | None = None

    def _emit(self, event: str, **data: Any) -> None:
        if self.progress:
            try:
                self.progress({"event": event, **data})
            except Exception:
                pass

    @staticmethod
    def _find_cli() -> str:
        candidates = [
            Path.home() / ".lmstudio" / "bin" / "lms.exe",
            Path.home() / ".lmstudio" / "bin" / "lms.cmd",
            Path(os.getenv("LOCALAPPDATA", "")) / "LM Studio" / "bin" / "lms.exe",
        ]
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
        found = shutil.which("lms") or shutil.which("lms.exe") or shutil.which("lms.cmd")
        if found:
            return found
        raise RuntimeError(
            "No encuentro el comando lms. Instala/abre LM Studio al menos una vez "
            "y comprueba `lms --help` en PowerShell."
        )

    def _run(self, *args: str, timeout: int = 120, input_text: str | None = None,
             check: bool = True) -> subprocess.CompletedProcess[str]:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0
        command = self._command(*args)
        result = subprocess.run(
            command, input=input_text, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
            creationflags=flags,
        )
        if check and result.returncode:
            detail = (result.stderr or result.stdout or "error desconocido").strip()
            raise RuntimeError(f"lms {' '.join(args)} falló: {detail}")
        return result

    def _command(self, *args: str) -> list[str]:
        command = [self.cli, *args]
        if IS_WINDOWS and self.cli.lower().endswith((".cmd", ".bat")):
            command = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", self.cli, *args]
        return command

    def _run_quiet(self, *args: str, timeout: int = 10) -> subprocess.CompletedProcess[str] | None:
        try:
            return self._run(*args, timeout=timeout, check=False)
        except (subprocess.TimeoutExpired, OSError):
            return None

    @staticmethod
    def _clean_cli_output(value: str) -> str:
        value = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)
        value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", value)
        return value.strip()

    @staticmethod
    def _progress_from_output(value: str) -> tuple[float | None, str]:
        clean = LMStudioManager._clean_cli_output(value)
        percent_match = re.search(r"(?<!\d)(100|\d{1,2})(?:[.,](\d+))?\s*%", clean)
        percent: float | None = None
        if percent_match:
            percent = float(percent_match.group(1) + "." + (percent_match.group(2) or "0"))
        else:
            size_match = re.search(
                r"(\d+(?:[.,]\d+)?)\s*(B|KB|MB|GB|KIB|MIB|GIB)\s*(?:/|de|of)\s*"
                r"(\d+(?:[.,]\d+)?)\s*(B|KB|MB|GB|KIB|MIB|GIB)",
                clean, re.I,
            )
            if size_match:
                factors = {
                    "B": 1, "KB": 1000, "MB": 1000**2, "GB": 1000**3,
                    "KIB": 1024, "MIB": 1024**2, "GIB": 1024**3,
                }
                current = float(size_match.group(1).replace(",", ".")) * factors[size_match.group(2).upper()]
                total = float(size_match.group(3).replace(",", ".")) * factors[size_match.group(4).upper()]
                if total > 0:
                    percent = min(100.0, current * 100.0 / total)
        return percent, clean

    def _run_streaming(self, *args: str, timeout: int = 7200,
                       input_text: str | None = None,
                       abort_patterns: tuple[str, ...] = ()) -> subprocess.CompletedProcess[str]:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0
        command = self._command(*args)
        process = subprocess.Popen(
            command, stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", bufsize=1, creationflags=flags,
        )
        if process.stdin is not None:
            try:
                process.stdin.write(input_text or "")
                process.stdin.flush()
            finally:
                process.stdin.close()

        chunks: queue.Queue[str] = queue.Queue()

        def read_output() -> None:
            assert process.stdout is not None
            buffer: list[str] = []
            while True:
                char = process.stdout.read(1)
                if not char:
                    break
                if char in "\r\n":
                    if buffer:
                        chunks.put("".join(buffer))
                        buffer.clear()
                else:
                    buffer.append(char)
            if buffer:
                chunks.put("".join(buffer))

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        started = time.monotonic()
        captured: list[str] = []
        aborted_on = ""
        last_progress: float | None = None
        last_progress_emit = 0.0

        def record(raw: str) -> None:
            nonlocal aborted_on, last_progress, last_progress_emit
            clean = self._clean_cli_output(raw)
            if not clean:
                return
            captured.append(clean)
            percent, detail = self._progress_from_output(clean)
            if percent is None:
                self._emit("output", text=detail, percent=None)
            else:
                now = time.monotonic()
                # El CLI imprime varios frames del spinner por cada décima. La
                # ventana ya anima su propia barra a 60 FPS: enviamos como mucho
                # un dato por segundo o cada 1 %, sin llenar ACTIVIDAD de copias.
                changed = last_progress is None or abs(percent - last_progress) >= 1.0
                if changed or now - last_progress_emit >= 1.2 or percent >= 100:
                    friendly = re.sub(r"^[^\[]*\[[^\]]*\]\s*", "", detail)
                    friendly = re.sub(r"\s*\|\s*", " · ", friendly).strip(" ·")
                    self._emit(
                        "output", text=friendly or f"{percent:.1f} %",
                        percent=percent, transient=True,
                    )
                    last_progress = percent
                    last_progress_emit = now
            lowered = clean.lower()
            for pattern in abort_patterns:
                if pattern.lower() in lowered:
                    aborted_on = pattern
                    break

        while process.poll() is None:
            if self.download_cancel.is_set():
                process.terminate()
                try:
                    process.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=4)
                self._emit("cancelled")
                raise ModelDownloadCancelled("Descarga del modelo cancelada")
            if time.monotonic() - started > timeout:
                process.kill()
                process.wait(timeout=4)
                raise TimeoutError(f"lms {' '.join(args)} superó {timeout} segundos")
            while True:
                try:
                    raw = chunks.get_nowait()
                except queue.Empty:
                    break
                record(raw)
            if aborted_on:
                self._stop_process(process)
                break
            time.sleep(.08)
        reader.join(timeout=1)
        while True:
            try:
                raw = chunks.get_nowait()
            except queue.Empty:
                break
            record(raw)
        output = "\n".join(captured[-500:])
        returncode = process.returncode if process.returncode is not None else 1
        if aborted_on and returncode == 0:
            returncode = 1
        return subprocess.CompletedProcess(command, returncode, output, "")

    def _json(self, *args: str) -> Any:
        text = self._run(*args).stdout.strip()
        # Versiones antiguas del CLI podían anteponer una línea informativa.
        starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
        if not starts:
            return {}
        start = min(starts)
        try:
            return json.loads(text[start:])
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"La salida JSON de lms no se pudo interpretar: {exc}") from exc

    @staticmethod
    def _records(value: Any) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        if isinstance(value, list):
            for item in value:
                records.extend(LMStudioManager._records(item))
        elif isinstance(value, dict):
            keys = {str(k).lower() for k in value}
            if keys & {"identifier", "modelidentifier", "modelkey", "model_key", "path", "modelpath"}:
                records.append(value)
            for child in value.values():
                if isinstance(child, (dict, list)):
                    records.extend(LMStudioManager._records(child))
        return records

    @staticmethod
    def _model_key(record: dict[str, Any], loaded: bool = False) -> str:
        order = (
            ("identifier", "modelIdentifier", "model_identifier", "instanceReference")
            if loaded else
            ("modelKey", "model_key", "path", "modelPath", "identifier", "name")
        )
        for key in order:
            value = record.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    @staticmethod
    def _score(record: dict[str, Any]) -> float:
        blob = json.dumps(record, ensure_ascii=False).lower()
        if any(word in blob for word in ("embedding", "rerank", "nomic-embed", "bge-")):
            return -10_000
        score = 0.0
        preferences = [
            ("qwen3.8", 180), ("qwen3.5", 170), ("qwen3-vl", 160),
            ("qwen2.5-vl", 135), ("muse-glimmer", 125), ("gemma-4", 115),
            ("qwen3", 105), ("gpt-oss", 95), ("mistral", 80),
            ("llama", 65), ("deepseek", 55),
        ]
        score += next((points for needle, points in preferences if needle in blob), 25)
        if LMStudioManager._supports_vision(record):
            score += 35
        if any(word in blob for word in ("instruct", "tool", "agent")):
            score += 25
        # En la RTX 5070, 4B–12B ofrece el mejor equilibrio para herramientas y voz.
        match = re.search(r"(?:^|[^0-9])(\d+(?:\.\d+)?)\s*b(?:[^a-z]|$)", blob)
        if match:
            params = float(match.group(1))
            if 7 <= params <= 12:
                score += 30
            elif 3 <= params < 7:
                score += 18
            elif params > 20:
                score -= 55
        return score

    @staticmethod
    def _supports_vision(record: dict[str, Any]) -> bool:
        blob = json.dumps(record, ensure_ascii=False).lower()
        # Familias cuyo catálogo es nativamente multimodal.
        if any(name in blob for name in (
            "qwen3.8", "qwen3.5", "qwen3-vl", "qwen2.5-vl",
            "muse-glimmer", "gemma-4", "nemotron-3-omni",
        )):
            return True

        def inspect(value: Any) -> bool:
            if isinstance(value, dict):
                for key, child in value.items():
                    normalized = str(key).replace("_", "").replace("-", "").lower()
                    if normalized in {
                        "vision", "supportsvision", "visionsupported", "imageinput",
                        "supportsimageinput", "multimodal",
                    } and child is True:
                        return True
                    if normalized in {"capabilities", "inputmodalities", "modalities"}:
                        if isinstance(child, str) and any(x in child.lower() for x in ("vision", "image")):
                            return True
                        if isinstance(child, list) and any(
                            isinstance(item, str) and item.lower() in {"vision", "image", "images"}
                            for item in child
                        ):
                            return True
                    if inspect(child):
                        return True
            elif isinstance(value, list):
                return any(inspect(item) for item in value)
            return False

        return inspect(record)

    def _choose(self, data: Any, loaded: bool = False) -> str:
        records = self._records(data)
        explicit = str(self.config.get("local_model", "")).strip().lower()
        if explicit:
            exact = [r for r in records if explicit in json.dumps(r, ensure_ascii=False).lower()]
            if exact:
                records = exact
        ranked = sorted(records, key=self._score, reverse=True)
        minimum = 150 if self.config.get("vision", True) else 80
        for record in ranked:
            # Si el usuario indicó uno, lo respetamos. En automático exigimos un
            # modelo multimodal si vamos a mandarle capturas de Paint.
            visual_ok = not self.config.get("vision", True) or self._supports_vision(record)
            if explicit or (visual_ok and self._score(record) >= minimum):
                key = self._model_key(record, loaded=loaded)
                if key:
                    return key
        return ""

    def _server_ready(self) -> bool:
        url = self.config["lmstudio_url"].rstrip("/") + "/models"
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                return 200 <= response.status < 300
        except (OSError, urllib.error.URLError):
            return False

    def _server_has_model(self, identifier: str) -> bool:
        url = self.config["lmstudio_url"].rstrip("/") + "/models"
        try:
            with urllib.request.urlopen(url, timeout=4) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
            return normalize_text(identifier) in normalize_text(json.dumps(payload, ensure_ascii=False))
        except (OSError, ValueError, urllib.error.URLError):
            return False

    def _wait_server_model(self, identifier: str, seconds: float = 18.0) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self._server_has_model(identifier):
                return True
            time.sleep(.5)
        return False

    @staticmethod
    def _catalog_has_model(data: Any, model_key: str) -> bool:
        blob = normalize_text(json.dumps(data, ensure_ascii=False))
        base = model_key.split("@", 1)[0].rsplit("/", 1)[-1]
        return bool(base and normalize_text(base) in blob)

    def _resolve_downloaded_key(self, data: Any, requested: str) -> str:
        """Convierte el nombre de búsqueda en el model_key local que acepta load."""
        base = requested.split("@", 1)[0].rsplit("/", 1)[-1]
        base_needle = normalize_text(base)
        quant = requested.split("@", 1)[1] if "@" in requested else ""
        quant_needle = normalize_text(quant)
        matches: list[tuple[float, str]] = []
        for record in self._records(data):
            key = self._model_key(record)
            if not key:
                continue
            blob = normalize_text(json.dumps(record, ensure_ascii=False))
            if base_needle and base_needle not in blob:
                continue
            score = self._score(record)
            if quant_needle and quant_needle in blob:
                score += 500
            if normalize_text(key) == normalize_text(requested):
                score += 1000
            matches.append((score, key))
        if matches:
            return max(matches, key=lambda item: item[0])[1]
        return ""

    @staticmethod
    def _json_from_output(value: str) -> Any:
        starts = [i for i in (value.find("{"), value.find("[")) if i >= 0]
        if not starts:
            return {}
        try:
            return json.loads(value[min(starts):])
        except json.JSONDecodeError:
            return {}

    def _daemon_running(self) -> bool:
        result = self._run_quiet("daemon", "status", "--json", timeout=4)
        if not result:
            return False
        data = self._json_from_output(result.stdout)
        if isinstance(data, dict):
            return str(data.get("status", "")).lower() == "running" or data.get("isDaemon") is True
        text = result.stdout.lower()
        return "running" in text and "not-running" not in text and "not running" not in text

    def _llmster_candidates(self) -> list[Path]:
        """Devuelve instalaciones reales del daemon, primero la más reciente."""
        root = Path.home() / ".lmstudio" / "llmster"
        if not root.is_dir():
            return []
        candidates: list[Path] = []
        try:
            candidates.extend(path for path in root.rglob("llmster.exe") if path.is_file())
        except OSError:
            return []

        def sort_key(path: Path) -> tuple[tuple[int, ...], float]:
            # Carpetas habituales: 0.0.24-1, 0.0.21-2. La fecha desempata
            # instalaciones parciales o nombres que cambien en el futuro.
            numbers = tuple(int(value) for value in re.findall(r"\d+", str(path.parent)))
            try:
                modified = path.stat().st_mtime
            except OSError:
                modified = 0.0
            return numbers, modified

        return sorted(set(candidates), key=sort_key, reverse=True)

    def _ensure_model_storage(self) -> str:
        """Crea ~/.lmstudio/models y rescata enlaces rotos de discos ausentes.

        LM Studio permite ubicar los modelos en otro disco mediante un junction.
        Si ese disco desaparece, Windows conserva el punto de reanálisis pero
        Path.exists()/Node mkdir fallan con ENOENT. Nunca borramos el destino:
        apartamos únicamente el enlace roto y creamos una carpeta local nueva.
        """
        root = Path.home() / ".lmstudio"
        models = root / "models"
        root.mkdir(parents=True, exist_ok=True)
        if models.is_dir():
            return ""

        is_junction = False
        try:
            junction_probe = getattr(models, "is_junction", None)
            is_junction = bool(junction_probe and junction_probe())
        except OSError:
            pass
        is_symlink = models.is_symlink()
        entry_exists = os.path.lexists(models)
        backup: Path | None = None

        if entry_exists or is_junction or is_symlink:
            backup_dir = app_data_dir() / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup = backup_dir / f"lmstudio-models-broken-link-{int(time.time())}"
            try:
                models.rename(backup)
            except OSError as move_exc:
                # rmdir sobre un junction elimina solo el enlace, jamás el
                # contenido del volumen de destino. Para un symlink usamos
                # unlink. Una carpeta real no vacía hará fallar ambas opciones
                # y se conserva intacta.
                try:
                    if is_symlink and not is_junction:
                        models.unlink()
                    else:
                        models.rmdir()
                    backup = None
                except OSError as remove_exc:
                    raise RuntimeError(
                        "Existe C:\\Users\\...\\.lmstudio\\models, pero Windows no puede "
                        "abrirla ni apartarla. Probablemente es un enlace roto hacia el "
                        "disco antiguo. Detalles: "
                        f"mover={move_exc}; retirar enlace={remove_exc}"
                    ) from remove_exc

        try:
            models.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RuntimeError(
                f"No pude crear la nueva carpeta local de modelos: {models}. {exc}"
            ) from exc
        if not models.is_dir():
            raise RuntimeError(f"La carpeta de modelos sigue sin ser accesible: {models}")

        if backup is not None:
            return (
                "Detecté que la carpeta de modelos apuntaba a un disco ausente. "
                f"Aparté el enlace roto en {backup} y creé {models}. "
                "No se borró ningún modelo del disco antiguo."
            )
        return f"Creé la carpeta local necesaria para LM Studio: {models}"

    @staticmethod
    def _windows_daemon_events() -> str:
        """Extrae los fallos recientes de llmster/LM Studio del registro de Windows."""
        if not IS_WINDOWS:
            return ""
        script = (
            "$since=(Get-Date).AddMinutes(-15); "
            "Get-WinEvent -FilterHashtable @{LogName='Application';StartTime=$since} "
            "-ErrorAction SilentlyContinue | Where-Object {"
            "$_.Message -match 'llmster|LM Studio' -or "
            "($_.ProviderName -eq 'Application Error' -and $_.Message -match 'llmster.exe')"
            "} | Select-Object -First 8 TimeCreated,ProviderName,Id,LevelDisplayName,Message | "
            "Format-List | Out-String -Width 220"
        )
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=15, creationflags=flags, check=False,
            )
            return (result.stdout or result.stderr).strip()
        except (OSError, subprocess.TimeoutExpired):
            return ""

    def _write_daemon_diagnostic(self, *sections: str) -> Path:
        """Guarda un informe accionable sin depender de que la interfaz siga abierta."""
        path = app_data_dir() / "lmstudio-daemon-diagnostic.txt"
        status = self._run_quiet("daemon", "status", "--json", timeout=8)
        version = self._run_quiet("--version", timeout=8)
        candidates = self._llmster_candidates()
        rows = [
            f"{APP_NAME} {APP_VERSION}",
            f"Fecha: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"Python: {sys.executable}",
            f"Elevado: {is_elevated()}",
            f"CLI: {self.cli}",
            "Versión CLI: " + (((version.stdout or version.stderr).strip()) if version else "sin respuesta"),
            "Estado daemon: " + (((status.stdout or status.stderr).strip()) if status else "sin respuesta"),
            "Binarios llmster:",
        ]
        if candidates:
            for candidate in candidates:
                try:
                    rows.append(
                        f"  - {candidate} | {candidate.stat().st_size} bytes | "
                        f"mtime={candidate.stat().st_mtime}"
                    )
                except OSError as exc:
                    rows.append(f"  - {candidate} | error: {exc}")
        else:
            rows.append("  - ninguno")
        rows.extend(section.strip() for section in sections if section and section.strip())
        events = self._windows_daemon_events()
        if events:
            rows.extend(("Eventos recientes de Windows:", events))
        path.write_text("\n\n".join(rows) + "\n", encoding="utf-8", errors="replace")
        self.last_daemon_diagnostic = path
        return path

    @staticmethod
    def _stop_process(process: subprocess.Popen[str] | None) -> None:
        if not process or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=4)
        except Exception:
            try:
                process.kill()
                process.wait(timeout=4)
            except Exception:
                pass

    def _start_daemon_once(self, seconds: float) -> tuple[bool, str]:
        """Arranca llmster conservando su salida para poder reparar el fallo."""
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0
        command = self._command("daemon", "up", "--json")
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=flags,
            )
        except OSError as exc:
            return False, f"No se pudo ejecutar {' '.join(command)}: {exc}"

        self.daemon_boot_process = process
        output: list[str] = []

        def drain() -> None:
            if process.stdout is None:
                return
            for raw in iter(process.stdout.readline, ""):
                clean = self._clean_cli_output(raw)
                if clean:
                    output.append(clean)
                    self._emit("output", text=clean, percent=None)

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.download_cancel.is_set():
                self._stop_process(process)
                raise ModelDownloadCancelled("Preparación de LM Studio cancelada")
            if self._daemon_running():
                reader.join(timeout=.3)
                return True, "\n".join(output[-80:])
            if process.poll() is not None:
                # Puede haber terminado justo después de crear el daemon.
                reader.join(timeout=1)
                grace = min(deadline, time.monotonic() + 7)
                while time.monotonic() < grace:
                    if self._daemon_running():
                        return True, "\n".join(output[-80:])
                    time.sleep(.6)
                return False, "\n".join(output[-80:])
            time.sleep(.6)

        self._stop_process(process)
        reader.join(timeout=1)
        return self._daemon_running(), "\n".join(output[-80:])

    def _start_llmster_direct(self, seconds: float = 35.0) -> tuple[bool, str]:
        """Plan B: inicia el daemon real y conserva el error que lms suele ocultar."""
        candidates = self._llmster_candidates()
        if not candidates:
            return False, "No existe ningún llmster.exe instalado."
        executable = candidates[0]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if IS_WINDOWS:
            flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

        # Un arranque fallido de `lms daemon up` puede dejar un proceso zombi
        # que impide que el siguiente publique su socket/API.
        if IS_WINDOWS and not self._daemon_running():
            subprocess.run(
                ["taskkill", "/F", "/IM", "llmster.exe"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False,
            )
            time.sleep(.7)

        self.log(f"Arranque directo de llmster: {executable}")
        self._emit(
            "stage", title="Arranque directo del motor",
            detail="El CLI no conectó; iniciando llmster.exe y capturando su diagnóstico…",
        )
        try:
            process = subprocess.Popen(
                [str(executable)], cwd=str(executable.parent), stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="replace", bufsize=1, creationflags=flags,
            )
        except OSError as exc:
            return False, f"No se pudo ejecutar {executable}: {exc}"

        self.daemon_boot_process = process
        output: list[str] = [f"Ejecutable directo: {executable}"]

        def drain() -> None:
            if process.stdout is None:
                return
            for raw in iter(process.stdout.readline, ""):
                clean = self._clean_cli_output(raw)
                if clean:
                    output.append(clean)
                    self._emit("output", text=clean, percent=None)

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        deadline = time.monotonic() + seconds
        exited_at: float | None = None
        while time.monotonic() < deadline:
            if self.download_cancel.is_set():
                self._stop_process(process)
                raise ModelDownloadCancelled("Preparación de LM Studio cancelada")
            if self._daemon_running():
                reader.join(timeout=.3)
                return True, "\n".join(output[-120:])
            if process.poll() is not None:
                if exited_at is None:
                    exited_at = time.monotonic()
                    output.append(f"llmster.exe terminó con código {process.returncode}")
                # Algunos builds lanzan un hijo y el ejecutable padre termina.
                if time.monotonic() - exited_at >= 7:
                    break
            time.sleep(.5)

        if process.poll() is None:
            output.append("llmster.exe siguió vivo, pero no publicó el estado running.")
            self._stop_process(process)
        reader.join(timeout=1)
        return self._daemon_running(), "\n".join(output[-120:])

    def _repair_daemon(self) -> str:
        """Instala o actualiza llmster con el propio CLI oficial."""
        self._emit(
            "stage",
            title="Instalando motor headless",
            detail="El daemon no arrancó; descargando o reparando llmster sin abrir la interfaz…",
        )
        self.log("llmster no arrancó; ejecuto la reparación oficial `lms daemon update`…")
        # La documentación exige detenerlo antes de actualizar. No falla si ya
        # estaba parado o parcialmente instalado.
        self._run("daemon", "down", timeout=20, check=False)
        self._stop_process(self.daemon_boot_process)
        self.daemon_boot_process = None

        original_cli = self.cli
        temporary_cli_dir: tempfile.TemporaryDirectory[str] | None = None
        headless_cli = Path.home() / ".lmstudio" / "bin" / "lms.exe"
        canonical_cli = headless_cli if headless_cli.is_file() else Path(original_cli)
        backup_cli: Path | None = None
        result: subprocess.CompletedProcess[str] | None = None
        update_verified = False
        try:
            if IS_WINDOWS:
                # El actualizador sustituye ~/.lmstudio/bin/lms.exe. Cerramos
                # cualquier invocación huérfana y ejecutamos el update desde una
                # COPIA: Windows no permite borrar un ejecutable que está en uso,
                # ni siquiera con privilegios de administrador.
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                for image in ("lms.exe", "llmster.exe"):
                    subprocess.run(
                        ["taskkill", "/F", "/IM", image],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=flags,
                        check=False,
                    )
                time.sleep(.9)
                if canonical_cli.suffix.lower() == ".exe" and canonical_cli.is_file():
                    temporary_cli_dir = tempfile.TemporaryDirectory(prefix="paint-professor-lms-")
                    temporary_cli = Path(temporary_cli_dir.name) / "lms.exe"
                    shutil.copy2(canonical_cli, temporary_cli)

                    # El updater de llmster 0.0.21 intenta hacer rmSync sobre el
                    # lms.exe canónico y puede obtener EPERM incluso con UAC. Lo
                    # apartamos antes; la copia queda disponible para restaurar.
                    backup_dir = app_data_dir() / "backups"
                    backup_dir.mkdir(parents=True, exist_ok=True)
                    backup_cli = backup_dir / f"lms-before-update-{int(time.time())}.exe"
                    last_move_error: Exception | None = None
                    for attempt in range(6):
                        try:
                            os.chmod(canonical_cli, 0o700)
                            canonical_cli.replace(backup_cli)
                            last_move_error = None
                            break
                        except PermissionError as exc:
                            last_move_error = exc
                            if attempt == 1:
                                subprocess.run(
                                    ["attrib", "-R", "-S", "-H", str(canonical_cli)],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    creationflags=flags, check=False,
                                )
                            if attempt == 3:
                                identity = "\\".join(
                                    part for part in (
                                        os.getenv("USERDOMAIN", ""), os.getenv("USERNAME", ""),
                                    ) if part
                                )
                                subprocess.run(
                                    ["takeown", "/F", str(canonical_cli)],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    creationflags=flags, check=False,
                                )
                                if identity:
                                    subprocess.run(
                                        ["icacls", str(canonical_cli), "/grant:r", f"{identity}:(F)"],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        creationflags=flags, check=False,
                                    )
                            time.sleep(.45 * (attempt + 1))
                    if canonical_cli.exists():
                        raise RuntimeError(
                            "Windows no permitió liberar lms.exe ni después de cerrar procesos, "
                            "retirar atributos y reparar sus permisos. Reinicia Windows y vuelve "
                            f"a intentarlo. Detalle: {last_move_error}"
                        )

                    self.cli = str(temporary_cli)
                    self._emit(
                        "output",
                        text=f"lms.exe liberado; copia de seguridad: {backup_cli}",
                        percent=None,
                    )

            result = self._run_streaming(
                "daemon", "update", timeout=600, input_text="y\n" * 4,
                abort_patterns=("eperm", "failed to apply staged daemon update"),
            )
            output = result.stdout.lower()
            if result.returncode or "eperm" in output or "failed to apply" in output:
                detail = result.stdout.strip() or f"código {result.returncode}"
                raise RuntimeError(f"No se pudo instalar/actualizar llmster:\n{detail[-3000:]}")
            if IS_WINDOWS and backup_cli is not None:
                # En Windows, `lms daemon update` puede devolver 0 justo después
                # de abrir OTRO terminal donde se realiza la aplicación real.
                # No confundimos el fin del lanzador con el fin del updater.
                self._emit(
                    "stage",
                    title="Aplicando actualización",
                    detail="El instalador continúa en otra terminal; esperando el nuevo lms.exe…",
                )
                self.log("El actualizador externo sigue trabajando; espero a que publique el nuevo lms.exe…")
                deadline = time.monotonic() + 240
                last_size = -1
                stable_reads = 0
                verified_version = ""
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                while time.monotonic() < deadline:
                    if self.download_cancel.is_set():
                        raise ModelDownloadCancelled("Preparación de LM Studio cancelada")
                    if canonical_cli.is_file():
                        try:
                            size = canonical_cli.stat().st_size
                        except OSError:
                            size = -1
                        if size > 1_000_000 and size == last_size:
                            stable_reads += 1
                        else:
                            stable_reads = 0
                        last_size = size
                        if stable_reads >= 2:
                            try:
                                probe = subprocess.run(
                                    [str(canonical_cli), "--version"],
                                    capture_output=True, text=True, encoding="utf-8",
                                    errors="replace", timeout=12,
                                    creationflags=flags, check=False,
                                )
                                verified_version = (probe.stdout or probe.stderr).strip()
                                if probe.returncode == 0:
                                    break
                            except (OSError, subprocess.TimeoutExpired):
                                pass
                    time.sleep(.75)
                else:
                    raise RuntimeError(
                        "El actualizador externo no publicó un lms.exe utilizable en cuatro minutos. "
                        "La copia anterior sigue guardada y será restaurada."
                    )
                self._emit(
                    "output",
                    text=f"Nuevo CLI verificado: {verified_version or canonical_cli}",
                    percent=None,
                )
                self.log(f"Actualización externa terminada: {verified_version or 'nuevo lms.exe verificado'}")
                update_verified = True
        except BaseException:
            # Recuperación transaccional: si el updater no dejó un ejecutable
            # utilizable, reponemos el anterior sin borrar el backup.
            if backup_cli is not None and backup_cli.is_file() and not update_verified:
                try:
                    if IS_WINDOWS:
                        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                        for image in ("lms.exe", "llmster.exe"):
                            subprocess.run(
                                ["taskkill", "/F", "/IM", image],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=flags, check=False,
                            )
                        time.sleep(.5)
                    if canonical_cli.exists():
                        failed_cli = backup_cli.with_name(
                            f"lms-failed-update-{int(time.time())}.exe"
                        )
                        canonical_cli.replace(failed_cli)
                    shutil.copy2(backup_cli, canonical_cli)
                except Exception as restore_exc:
                    self.log(f"ATENCIÓN: tampoco pude restaurar lms.exe: {restore_exc}")
            raise
        finally:
            self.cli = original_cli
            if temporary_cli_dir is not None:
                try:
                    temporary_cli_dir.cleanup()
                except PermissionError:
                    # El proceso ya terminó; Windows/Defender puede tardar unos
                    # segundos en soltar la imagen temporal. El SO limpiará temp.
                    pass

        # El updater puede haber reemplazado la ruta canónica. La resolvemos de
        # nuevo antes del siguiente daemon up.
        self.cli = self._find_cli()
        assert result is not None
        return result.stdout.strip()

    def _spawn_service_command(self, *args: str) -> subprocess.Popen[str]:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if IS_WINDOWS:
            flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return subprocess.Popen(
            self._command(*args), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            text=True, creationflags=flags,
        )

    def _ensure_daemon(self) -> None:
        if self._daemon_running():
            return
        storage_change = self._ensure_model_storage()
        if storage_change:
            self.log(storage_change)
            self._emit(
                "output", text=storage_change,
                percent=None,
            )
        self._emit("stage", title="Iniciando llmster", detail="Arrancando el servicio local de LM Studio…")
        self.log("Iniciando llmster en segundo plano…")

        started, first_output = self._start_daemon_once(22)
        if started:
            self.log("llmster está ejecutándose en modo headless")
            return

        direct_output = ""
        candidates_before_repair = self._llmster_candidates()
        if candidates_before_repair:
            started, direct_output = self._start_llmster_direct(35)
            if started:
                self.log("llmster arrancó directamente en modo headless")
                return

        update_output = ""
        second_output = ""
        # Tener un llmster.exe real y reciente pero que se cierra es un fallo de
        # ejecución, no de instalación. Actualizarlo otra vez crea un bucle y
        # vuelve a bloquear lms.exe. Solo reparamos cuando el daemon NO existe.
        if not candidates_before_repair:
            update_output = self._repair_daemon()
            self._emit(
                "stage",
                title="Arrancando motor reparado",
                detail="llmster quedó instalado; comprobando el proceso headless…",
            )
            started, second_output = self._start_daemon_once(45)
            if started:
                self.log("llmster reparado y ejecutándose en modo headless")
                return
            started, direct_output = self._start_llmster_direct(35)
            if started:
                self.log("llmster reparado y arrancado directamente")
                return

        status = self._run_quiet("daemon", "status", "--json", timeout=8)
        detail = (status.stdout if status else "lms daemon status tampoco respondió").strip()
        version = self._run_quiet("--version", timeout=8)
        version_text = ""
        if version:
            version_text = (version.stdout or version.stderr).strip()
        diagnostic = "\n\n".join(
            part for part in (
                f"CLI: {self.cli}",
                f"Versión: {version_text or 'no disponible'}",
                f"Primer arranque:\n{first_output}" if first_output else "",
                f"Actualización:\n{update_output}" if update_output else "",
                f"Segundo arranque:\n{second_output}" if second_output else "",
                f"Arranque directo:\n{direct_output}" if direct_output else "",
                f"Estado final: {detail or 'sin respuesta'}",
            ) if part
        )
        report = self._write_daemon_diagnostic(diagnostic)
        raise RuntimeError(
            "llmster está instalado, pero se cierra o no publica su API local. "
            "No volveré a ejecutar el actualizador en bucle. Guardé el motivo completo en:\n"
            f"{report}\n\n{diagnostic}"
        )

    def _ensure_server(self) -> None:
        if self._server_ready():
            return
        self._ensure_daemon()
        self.log("Iniciando el servidor local de LM Studio…")
        self._emit("stage", title="Iniciando servidor local", detail="Esperando http://127.0.0.1:1234/v1…")
        try:
            self.server_boot_process = self._spawn_service_command("server", "start", "--port", "1234")
        except OSError as exc:
            raise RuntimeError(f"No pude ejecutar lms server start: {exc}") from exc
        for _ in range(120):
            if self.download_cancel.is_set():
                raise ModelDownloadCancelled("Preparación de LM Studio cancelada")
            if self._server_ready():
                return
            time.sleep(.5)
        status = self._run_quiet("server", "status", "--json", "--quiet", timeout=8)
        detail = (status.stdout if status else "sin respuesta de lms server status").strip()
        raise RuntimeError(f"LM Studio no abrió http://127.0.0.1:1234/v1. Estado: {detail}")

    def ensure_model(self) -> str:
        try:
            with self.PROCESS_LOCK:
                return self._ensure_model()
        except ModelDownloadCancelled:
            self._emit("cancelled")
            raise
        except Exception as exc:
            self._emit("error", message=str(exc))
            raise

    def _ensure_model(self) -> str:
        if self.model_identifier:
            return self.model_identifier
        self._emit("stage", title="Comprobando LM Studio", detail="Buscando modelos descargados y cargados…")
        self.log("Conectando con llmster mediante lms…")
        self._ensure_daemon()

        loaded = self._choose(self._json("ps", "--json"), loaded=True)
        if loaded:
            self.model_identifier = loaded
            self._ensure_server()
            if not self._wait_server_model(loaded):
                raise RuntimeError(f"El servidor responde, pero no publica el modelo cargado {loaded}")
            self.log(f"LM Studio usará el modelo ya cargado: {loaded}")
            self._emit("ready", model=loaded, detail="El modelo ya estaba cargado y el servidor respondió correctamente.")
            return loaded

        catalog = self._json("ls", "--llm", "--json", "--detailed")
        downloaded = self._choose(catalog)
        requested = str(self.config.get("local_model", "")).strip()
        needs_download = False
        if requested:
            model_key = self._resolve_downloaded_key(catalog, requested)
            if not model_key:
                model_key = requested
                needs_download = True
        else:
            model_key = downloaded
        if not model_key:
            if not self.config.get("local_auto_download", True):
                raise RuntimeError("No hay ningún LLM descargado en LM Studio")
            model_key = self.RECOMMENDED_MODEL
            needs_download = True

        if needs_download:
            self.log("No hay un modelo local apropiado; descargaré Qwen3.5-9B Q4_K_M (~7 GB)…")
            self.download_cancel.clear()
            self._emit(
                "download_start", model=model_key,
                title="Descargando modelo local",
                detail="Qwen3.5 9B · cuantización Q4_K_M · aproximadamente 7 GB",
            )
            result = self._run_streaming("get", model_key, "--yes", timeout=7200, input_text="y\n" * 20)
            if result.returncode:
                option_problem = any(word in result.stdout.lower() for word in (
                    "unknown option", "unrecognized option", "unexpected argument", "--yes",
                ))
                if not option_problem:
                    raise RuntimeError(f"La descarga falló:\n{result.stdout[-2500:]}")
                self._emit("stage", title="Reintentando descarga", detail="Esta versión de lms no admite --yes.")
                result = self._run_streaming("get", model_key, timeout=7200, input_text="y\n" * 20)
                if result.returncode:
                    raise RuntimeError(f"La descarga falló:\n{result.stdout[-2500:]}")

            self._emit("stage", title="Verificando descarga", detail="Comprobando el catálogo local de LM Studio…")
            catalog = self._json("ls", "--llm", "--json", "--detailed")
            resolved_key = self._resolve_downloaded_key(catalog, model_key)
            if not resolved_key:
                raise RuntimeError(
                    "lms terminó sin error, pero el modelo no aparece en el catálogo local. "
                    "No lo marcaré como descargado."
                )
            if normalize_text(resolved_key) != normalize_text(model_key):
                self.log(f"LM Studio registró el modelo como: {resolved_key}")
            model_key = resolved_key
            self._emit("download_verified", model=model_key)

        identifier = str(self.config.get("local_identifier", "paint-professor-local"))
        context = str(int(self.config.get("local_context", 32768)))
        self.log(f"Cargando {model_key} en LM Studio (GPU automática)…")
        self._emit("stage", title="Cargando modelo en GPU", detail=f"Identificador local: {identifier}")
        # El identificador puede pertenecer a una carga antigua sin visión.
        # Solo descargamos la instancia que creó Paint Professor.
        self._run("unload", identifier, "--yes", timeout=90, input_text="y\n", check=False)
        load_args = (
            "load", model_key, "--identifier", identifier,
            "--context-length", context, "--ttl", "3600", "--yes",
        )
        try:
            self._run(*load_args, timeout=600, input_text="y\n" * 10)
        except RuntimeError as exc:
            if "model not found" not in str(exc).lower():
                raise
            # Última defensa ante cambios de esquema del CLI: refrescamos el
            # catálogo y sustituimos cualquier alias remoto por su clave local.
            refreshed = self._json("ls", "--llm", "--json", "--detailed")
            recovered = self._resolve_downloaded_key(refreshed, model_key) or self._choose(refreshed)
            if not recovered or normalize_text(recovered) == normalize_text(model_key):
                raise
            model_key = recovered
            self.log(f"Reintentando con la clave local registrada por LM Studio: {model_key}")
            self._run(
                "load", model_key, "--identifier", identifier,
                "--context-length", context, "--ttl", "3600", "--yes", timeout=600,
                input_text="y\n" * 10,
            )
        loaded_state = self._json("ps", "--json")
        if normalize_text(identifier) not in normalize_text(json.dumps(loaded_state, ensure_ascii=False)):
            raise RuntimeError("lms load terminó, pero el modelo no aparece entre los modelos cargados")
        self._emit("stage", title="Iniciando servidor local", detail="Verificando el endpoint compatible con OpenAI…")
        self._ensure_server()
        if not self._wait_server_model(identifier):
            raise RuntimeError("El servidor de LM Studio responde, pero todavía no publica el modelo cargado")
        self.model_identifier = identifier
        self.log(f"Modelo local preparado: {model_key}")
        self._emit("ready", model=identifier, detail="Descarga, carga y servidor verificados. Ya puedes preguntar.")
        return identifier


class TeacherAgent:
    VISUAL_ACTIONS = {
        "write", "line", "arrow", "rectangle", "ellipse", "freehand",
        "point", "math_formula", "render_equation", "math_matrix", "plot_function", "polygon", "arc", "angle_mark", "dimension",
        "draw_diagram", "highlight_region", "annotate_live", "split_view", "matrix_operations",
        "token_visualizer", "simulate_training", "create_poll", "spawn_slider", "code_sandbox", "generate_exercise",
        "erase", "undo", "redo", "zoom", "toggle_grid", "next_page", "clear",
    }
    PLANNED_DRAW_ACTIONS = {
        "write", "line", "arrow", "rectangle", "ellipse", "freehand", "point",
        "math_formula", "render_equation", "math_matrix", "plot_function", "polygon", "arc", "angle_mark", "dimension",
        "draw_diagram", "highlight_region", "annotate_live", "split_view", "matrix_operations",
        "token_visualizer", "simulate_training", "create_poll", "spawn_slider", "code_sandbox", "generate_exercise", "erase",
    }
    CHECKPOINT_EVERY_VISUAL_ACTIONS = 18
    COMPACT_HISTORY_MESSAGES = 72
    MAX_IDENTICAL_VISUAL_REPEATS = 3
    MAX_BLOCKED_LOOP_ACTIONS = 6
    LEARNER_ACTIONS = {"track_mastery","detect_misconception","adapt_difficulty",
        "spaced_repetition_schedule","set_explanation_mode","ask_guiding_question",
        "wait_for_struggle","ask_confidence","record_reflection","recall_previous_session",
        "build_learning_narrative","voice_explain","gesture_point"}

    def __init__(self, config: dict[str, Any], paint: Any, speech: SpeechIO,
                 cancel: threading.Event, log: Callable[[str], None],
                 model_progress: Callable[[dict[str, Any]], None] | None = None,
                 model_download_cancel: threading.Event | None = None):
        from openai import OpenAI
        self.config = config
        self.paint = paint
        self.speech = speech
        self.cancel = cancel
        self.log = log
        self.provider = str(config.get("provider", "auto")).lower()
        if self.provider not in {"auto", "deepseek", "lmstudio"}:
            self.provider = "auto"
        key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        self.deepseek_client = OpenAI(api_key=key, base_url="https://api.deepseek.com", timeout=90.0) if key else None
        self.local_client = OpenAI(api_key="lm-studio", base_url=config["lmstudio_url"], timeout=180.0)
        self.lmstudio = LMStudioManager(config, log, model_progress, model_download_cancel)
        self.active_provider = "lmstudio" if self.provider == "lmstudio" or (self.provider == "auto" and not key) else "deepseek"
        self.local_vision_disabled = False
        self.lesson_page_number = 1
        self.current_plan: dict[str, Any] = {}
        self.narration_script: list[dict[str, Any]] = []
        self.narration_index = 0
        self.plan_visual_actions = 0
        self.history: list[dict[str, Any]] = [{"role":"system","content":SYSTEM_PROMPT}]
        self.learner_path = app_data_dir() / "learner-model.json"
        self.learner = self._load_learner_model()
        self.research_cache: dict[str, Any] = {}

    def research_verified_sources(self, query: str, sources: list[str], max_results: int) -> dict[str, Any]:
        """Consulta catálogos públicos conocidos y conserva procedencia; no hace scraping arbitrario."""
        from urllib.parse import quote, urlencode
        import xml.etree.ElementTree as ET
        query=normalize_text(query).strip();maximum=max(1,min(9,int(max_results)))
        key=json.dumps([query,sorted(sources),maximum],ensure_ascii=False)
        if key in self.research_cache:return self.research_cache[key]
        headers={"User-Agent":f"TeachAI/{APP_VERSION} educational-research"}
        def get_json(url:str)->Any:
            request=urllib.request.Request(url,headers=headers)
            with urllib.request.urlopen(request,timeout=12) as response:
                return json.loads(response.read().decode("utf-8"))
        items:list[dict[str,Any]]=[];errors:list[dict[str,str]]=[]
        per=max(1,math.ceil(maximum/max(1,len(sources))))
        if "wikipedia" in sources:
            try:
                data=get_json("https://en.wikipedia.org/w/api.php?"+urlencode({"action":"query","generator":"search","gsrsearch":query,"gsrlimit":per,"prop":"extracts|info","exintro":1,"explaintext":1,"inprop":"url","format":"json","origin":"*"}))
                pages=list(data.get("query",{}).get("pages",{}).values())
                for page in pages:
                    items.append({"source":"Wikipedia","title":page.get("title"),"url":page.get("fullurl"),
                                  "published":None,"authors":["Wikipedia contributors"],"excerpt":str(page.get("extract", ""))[:900],
                                  "source_type":"tertiary_reference"})
            except Exception as exc:errors.append({"source":"wikipedia","error":str(exc)})
        if "crossref" in sources:
            try:
                data=get_json("https://api.crossref.org/works?"+urlencode({"query":query,"rows":per,"select":"DOI,title,author,published,URL,publisher,type"}))
                for work in data.get("message",{}).get("items",[]):
                    authors=[" ".join(filter(None,[a.get("given"),a.get("family")])) for a in work.get("author",[])[:8]]
                    parts=(work.get("published",{}).get("date-parts") or [[None]])[0]
                    items.append({"source":"Crossref","title":"; ".join(work.get("title",[])),"url":work.get("URL"),
                                  "doi":work.get("DOI"),"published":"-".join(str(p) for p in parts if p is not None) or None,
                                  "authors":authors,"publisher":work.get("publisher"),"source_type":work.get("type","scholarly_work")})
            except Exception as exc:errors.append({"source":"crossref","error":str(exc)})
        if "arxiv" in sources:
            try:
                request=urllib.request.Request("https://export.arxiv.org/api/query?"+urlencode({"search_query":"all:"+query,"start":0,"max_results":per}),headers=headers)
                with urllib.request.urlopen(request,timeout=12) as response:root=ET.fromstring(response.read())
                ns={"a":"http://www.w3.org/2005/Atom"}
                for entry in root.findall("a:entry",ns):
                    items.append({"source":"arXiv","title":" ".join((entry.findtext("a:title",default="",namespaces=ns)).split()),
                                  "url":entry.findtext("a:id",default="",namespaces=ns),"published":entry.findtext("a:published",default="",namespaces=ns),
                                  "authors":[a.findtext("a:name",default="",namespaces=ns) for a in entry.findall("a:author",ns)],
                                  "excerpt":" ".join((entry.findtext("a:summary",default="",namespaces=ns)).split())[:900],"source_type":"preprint"})
            except Exception as exc:errors.append({"source":"arxiv","error":str(exc)})
        items=[item for item in items if item.get("title") and item.get("url")][:maximum]
        result={"query":query,"results":items,"errors":errors,"retrieved_at":time.time(),
                "warning":"Contrasta afirmaciones importantes entre fuentes. arXiv contiene preprints y Wikipedia es terciaria."}
        self.research_cache[key]=result
        self.log(f"Fuentes verificables: {len(items)} resultado(s) para «{query}»")
        return result

    def _load_learner_model(self) -> dict[str, Any]:
        empty={"version":1,"concepts":{},"misconceptions":[],"reflections":[],
               "socratic_threads":[],"narrative":[],"updated_at":time.time()}
        try:
            data=json.loads(self.learner_path.read_text(encoding="utf-8"))
            return data if isinstance(data,dict) and data.get("version")==1 else empty
        except (OSError,json.JSONDecodeError):
            return empty

    def _save_learner_model(self) -> None:
        self.learner["updated_at"]=time.time();self.learner_path.parent.mkdir(parents=True,exist_ok=True)
        temporary=self.learner_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.learner,indent=2,ensure_ascii=False),encoding="utf-8")
        temporary.replace(self.learner_path)

    def learner_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Memoria pedagógica con evidencia, actualización conservadora y fechas reales."""
        now=time.time();concepts=self.learner.setdefault("concepts",{})
        if name=="track_mastery":
            key=normalize_text(args["concept"]).strip().lower();entry=concepts.setdefault(key,{"mastery":0.35,"attempts":0,"evidence":[]})
            target={"correct":1.0,"partial":.55,"incorrect":0.0}[args["outcome"]]
            confidence=float(args["confidence"]);weight=min(.28,.08+.20*confidence)
            entry["mastery"]=round(float(entry.get("mastery",.35))*(1-weight)+target*weight,4)
            entry["attempts"]=int(entry.get("attempts",0))+1;entry["last_seen"]=now
            entry.setdefault("evidence",[]).append({"at":now,"outcome":args["outcome"],"text":args["evidence"],"confidence":confidence})
            entry["evidence"]=entry["evidence"][-20:];result={"concept":key,**entry}
        elif name=="detect_misconception":
            if float(args["confidence"])<.55:
                return {"recorded":False,"reason":"Confianza insuficiente; pregunta antes de diagnosticar."}
            item={"at":now,**args};self.learner.setdefault("misconceptions",[]).append(item)
            self.learner["misconceptions"]=self.learner["misconceptions"][-100:];result={"recorded":True,"item":item}
        elif name=="adapt_difficulty":
            key=normalize_text(args["concept"]).strip().lower();entry=concepts.setdefault(key,{"mastery":.35,"attempts":0,"evidence":[]})
            previous=int(entry.get("difficulty",2));score=float(args["performance"])
            delta=1 if score>=.82 and entry.get("attempts",0)>=2 else (-1 if score<.42 else 0)
            entry["difficulty"]=max(1,min(5,previous+delta));result={"concept":key,"previous":previous,"next":entry["difficulty"],"changed":delta!=0}
        elif name=="spaced_repetition_schedule":
            schedule=[]
            for raw in args["concepts"]:
                key=normalize_text(raw).strip().lower();entry=concepts.get(key,{"mastery":.25,"attempts":0})
                mastery=float(entry.get("mastery",.25));days=max(1,min(30,round(1+mastery*mastery*29)))
                due=float(entry.get("last_seen",now))+days*86400
                schedule.append({"concept":key,"mastery":mastery,"interval_days":days,"due_at":due})
            result={"schedule":sorted(schedule,key=lambda item:item["due_at"])}
        elif name=="set_explanation_mode":
            result={"accepted":True,"instruction":("Explica desde un ángulo realmente distinto: "+args["new_angle"]),**args}
        elif name=="ask_guiding_question":
            item={"at":now,**args,"status":"awaiting_answer"};self.learner.setdefault("socratic_threads",[]).append(item)
            self.speech.speak(args["question"],self.cancel);result=item
        elif name=="wait_for_struggle":
            deadline=time.monotonic()+min(30,int(args["timeout"]))
            while time.monotonic()<deadline and not self.cancel.wait(.1): pass
            result={"interrupted":self.cancel.is_set(),"hint":None if self.cancel.is_set() else args["then_hint"]}
        elif name=="ask_confidence":
            self.speech.speak(args["question"],self.cancel);result={"awaiting_confidence":True,"scale":args["scale"]}
        elif name=="record_reflection":
            item={"at":now,**args};self.learner.setdefault("reflections",[]).append(item);result={"recorded":True}
        elif name=="recall_previous_session":
            needle=normalize_text(args["topic"]).lower()
            matches={key:value for key,value in concepts.items() if needle in key or key in needle}
            misconceptions=[item for item in self.learner.get("misconceptions",[]) if needle in normalize_text(str(item.get("concept",""))).lower()]
            result={"topic":args["topic"],"found":bool(matches or misconceptions),"concepts":matches,"misconceptions":misconceptions[-8:]}
        elif name=="build_learning_narrative":
            item={"at":now,**args};self.learner.setdefault("narrative",[]).append(item);result={"chapter_index":len(self.learner["narrative"]),**item}
        elif name=="voice_explain":
            self.speech.speak(args["text"],self.cancel);result={"spoken":not self.cancel.is_set(),"visual_object_id":args["visual_object_id"]}
        elif name=="gesture_point":
            state=self.paint.semantic_state();obj=next((item for item in state.get("objects",[]) if item.get("id")==args["element_id"]),None)
            if not obj: return {"ok":False,"error":"El objeto indicado no existe en la escena"}
            b=obj["bounds"];self.paint.highlight_region(b["x"],b["y"],b["w"],b["h"],args["color"],"")
            result={"pointed":args["element_id"],"bounds":b}
        else: raise ValueError(f"Herramienta pedagógica desconocida: {name}")
        self._save_learner_model();return result

    def review_lesson_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        """Segunda pasada independiente y sin herramientas antes de autorizar la escena."""
        steps = [normalize_text(str(item)) for item in plan.get("steps", [])]
        narration = plan.get("narration_script", [])
        deterministic: list[str] = []
        if len(set(steps)) != len(steps):
            deterministic.append("El plan contiene pasos duplicados")
        if len(narration) < max(3, (len(steps)+1)//2):
            deterministic.append("El guion oral no cubre suficientes pasos")
        if any(len(str(item.get("text", "")).split()) > 65 for item in narration if isinstance(item,dict)):
            deterministic.append("Algún fragmento oral es demasiado largo para acompañar un dibujo")
        if deterministic:
            return {"approved":False,"issues":deterministic,"source":"deterministic"}
        prompt = (
            "Actúas como revisor pedagógico y factual independiente. No dibujas ni llamas herramientas. "
            "Revisa este plan de clase antes de ejecutarlo. Detecta afirmaciones falsas, causalidad "
            "inventada, contradicciones, pasos que no cumplen el objetivo, repetición, guion que describe "
            "cosas distintas del dibujo o explicaciones engañosamente absolutas. Devuelve SOLO JSON válido: "
            "{\"approved\":boolean,\"issues\":[string],\"warnings\":[string],\"confidence\":number}. "
            "Rechaza si existe cualquier error factual material; no rechaces solo por estilo.\nPLAN:\n"
            + json.dumps(plan, ensure_ascii=False)
        )
        try:
            if self.active_provider == "lmstudio":
                model = self.lmstudio.ensure_model()
                response = self.local_client.chat.completions.create(
                    model=model, messages=[{"role":"user","content":prompt}],
                    temperature=0.05, max_tokens=900,
                )
            elif self.deepseek_client is not None:
                response = self.deepseek_client.chat.completions.create(
                    model=self.config["model"], messages=[{"role":"user","content":prompt}],
                    temperature=0.05, max_tokens=900,
                )
            else:
                return {"approved":True,"issues":[],"warnings":["Crítico no disponible"],"confidence":0.4}
            content = (response.choices[0].message.content or "").strip()
            fenced = re.search(r"```(?:json)?\s*(.*?)\s*```",content,re.I|re.S)
            review = json.loads(fenced.group(1) if fenced else content)
            if not isinstance(review,dict) or not isinstance(review.get("approved"),bool):
                raise ValueError("respuesta de crítico incompleta")
            review["source"] = "independent_model_pass"
            return review
        except Exception as exc:
            self.log(f"Crítico factual no disponible ({exc}); aplico las barreras deterministas")
            return {"approved":True,"issues":[],"warnings":[str(exc)],"confidence":0.35,
                    "source":"deterministic_fallback"}

    def _start_planned_narration(self) -> None:
        if not self.narration_script or self.narration_index >= len(self.narration_script):
            return
        # Una nueva sección oral aproximadamente cada dos acciones visuales.
        if self.narration_index > self.plan_visual_actions // 2:
            return
        segment = self.narration_script[self.narration_index]
        self.narration_index += 1
        text = str(segment.get("text", "")).strip()
        if not text:
            return
        self.log(f"Guion oral {self.narration_index}/{len(self.narration_script)}: {text[:90]}")
        threading.Thread(target=self.speech.speak,args=(text,self.cancel),daemon=True).start()

    @staticmethod
    def _fallback_worthy(exc: Exception) -> bool:
        status = getattr(exc, "status_code", None)
        text = str(exc).lower()
        return status in {401, 402, 403, 429, 500, 502, 503, 504} or any(
            phrase in text for phrase in (
                "insufficient balance", "connection error", "timed out",
                "rate limit", "authentication", "402",
            )
        )

    @staticmethod
    def _image_rejected(exc: Exception) -> bool:
        problem = str(exc).lower()
        return any(phrase in problem for phrase in (
            "does not support image", "not support image", "image inputs",
            "vision is not supported", "unsupported modality",
        ))

    def _completion(self) -> Any:
        kwargs = dict(
            messages=self.history, tools=TOOLS, tool_choice="auto",
            temperature=0.35, max_tokens=4000,
        )
        if self.active_provider == "lmstudio":
            model = self.lmstudio.ensure_model()
            self.log(f"LM Studio ({model}) está preparando la explicación…")
            if self.local_vision_disabled:
                kwargs["messages"] = self.without_images(self.history)
            try:
                return self.local_client.chat.completions.create(model=model, **kwargs)
            except Exception as exc:
                if not self._image_rejected(exc):
                    raise
                self.local_vision_disabled = True
                self.log("Este modelo local no acepta imágenes; repito el turno sin captura y continúo.")
                kwargs["messages"] = self.without_images(self.history)
                return self.local_client.chat.completions.create(model=model, **kwargs)

        if self.deepseek_client is None:
            if self.provider == "deepseek":
                raise RuntimeError("Falta DEEPSEEK_API_KEY para el modo DeepSeek")
            self.active_provider = "lmstudio"
            return self._completion()
        self.log("DeepSeek está preparando la explicación…")
        try:
            return self.deepseek_client.chat.completions.create(model=self.config["model"], **kwargs)
        except Exception as exc:
            if self.provider != "auto" or not self._fallback_worthy(exc):
                raise
            self.log(f"DeepSeek no está disponible ({exc}). Cambio automático a LM Studio.")
            self.active_provider = "lmstudio"
            return self._completion()

    def user_message(self, text: str) -> dict[str, Any]:
        semantic_reader = getattr(self.paint, "semantic_state", None)
        if callable(semantic_reader):
            state = json.dumps(semantic_reader(), ensure_ascii=False)
            return {
                "role": "user",
                "content": (
                    text + "\n\n[ESTADO NATIVO AUTORITATIVO ANTES DEL TURNO]\n" + state
                    + "\nUsa este estado exacto; no supongas objetos que no figuren en él."
                ),
            }
        if self.config.get("vision", True) and not (
            self.active_provider == "lmstudio" and self.local_vision_disabled
        ):
            image = self.paint.screenshot_data_url()
            if image:
                return {"role":"user","content":[
                    {"type":"text","text":text},
                    {"type":"image_url","image_url":{"url":image,"detail":"high"}},
                ]}
        return {"role":"user","content":text}

    @staticmethod
    def compact_action_args(name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Conserva intención y geometría sin enviar cientos de anclas al auditor."""
        if name != "freehand":
            return {
                key: (value[:240] if isinstance(value, str) else value)
                for key, value in args.items()
            }
        strokes = args.get("strokes", []) if isinstance(args, dict) else []
        points = [
            point for stroke in strokes if isinstance(stroke, dict)
            for point in stroke.get("points", []) if isinstance(point, dict)
            and isinstance(point.get("x"), (int, float)) and isinstance(point.get("y"), (int, float))
        ]
        compact: dict[str, Any] = {
            "stroke_count": len(strokes),
            "anchor_count": len(points),
            "color": args.get("color"), "width": args.get("width"), "speed": args.get("speed"),
        }
        if points:
            xs, ys = [float(p["x"]) for p in points], [float(p["y"]) for p in points]
            compact["planned_bounds"] = {
                "x": round(min(xs), 1), "y": round(min(ys), 1),
                "w": round(max(xs) - min(xs), 1), "h": round(max(ys) - min(ys), 1),
            }
        return compact

    def visual_audit(self, image: str, records: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Segunda pasada independiente: un crítico visual evalúa los píxeles reales."""
        actions_json = json.dumps(records, ensure_ascii=False, separators=(",", ":"))
        messages = [
            {"role":"system","content":(
                "Eres el auditor visual de una pizarra de 1000x700. No das la clase ni llamas "
                "herramientas. Compara la captura REAL con las últimas acciones previstas. "
                "Distingue contenido incompleto de contenido dañado. Un hueco en blanco no es un "
                "error. No recomiendes borrar salvo que existan píxeles incorrectos concretos. "
                "Devuelve SOLO JSON válido con: status (ok|continue|correct), visible_summary, "
                "defects (lista de objetos type, evidence, severity, region{x,y,w,h}, fix), "
                "protected_content (lista), next_best_action y confidence (0..1)."
            )},
            {"role":"user","content":[
                {"type":"text","text":f"Últimas acciones exactas: {actions_json}"},
                {"type":"image_url","image_url":{"url":image,"detail":"high"}},
            ]},
        ]
        result_box: dict[str, Any] = {}
        done = threading.Event()
        def request() -> None:
            try:
                if self.active_provider == "lmstudio":
                    model = self.lmstudio.ensure_model()
                    result_box["response"] = self.local_client.chat.completions.create(
                        model=model, messages=messages, temperature=0.05, max_tokens=1000,
                    )
                elif self.deepseek_client is not None:
                    result_box["response"] = self.deepseek_client.chat.completions.create(
                        model=self.config["model"], messages=messages, temperature=0.05, max_tokens=1000,
                    )
                else:
                    result_box["unavailable"] = True
            except Exception as error:
                result_box["error"] = error
            finally:
                done.set()
        threading.Thread(target=request, daemon=True).start()
        while not done.wait(.08):
            if self.cancel.is_set():
                self.log("Auditor visual cancelado inmediatamente por la interrupción")
                return None
        if result_box.get("unavailable"):
            return None
        if "error" in result_box:
            exc = result_box["error"]
            if self.active_provider == "lmstudio" and self._image_rejected(exc):
                self.local_vision_disabled = True
                self.log("El modelo local no admite visión; desactivo el auditor visual para esta sesión")
            else:
                self.log(f"Auditor visual omitido en esta acción: {exc}")
            return None
        if self.cancel.is_set():
            return None
        response = result_box["response"]
        content = (response.choices[0].message.content or "").strip()
        fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", content, re.I | re.S)
        candidate = fenced.group(1) if fenced else content
        try:
            audit = json.loads(candidate)
            if not isinstance(audit, dict):
                raise ValueError("el informe no es un objeto")
        except Exception:
            audit = {
                "status": "continue", "visible_summary": content[:1200],
                "defects": [], "protected_content": [],
                "next_best_action": "Inspeccionar la captura directamente", "confidence": 0.35,
            }
        self.log(
            f"Auditor visual: {audit.get('status', 'sin estado')} — "
            f"{len(audit.get('defects', [])) if isinstance(audit.get('defects'), list) else 0} defecto(s)"
        )
        return audit

    def board_monitor_message(self, records: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Devuelve estado exacto en nativo; en Paint legado recurre a captura y visión."""
        if not records or not self.config.get("visual_monitor", True):
            return None
        semantic_reader = getattr(self.paint, "semantic_state", None)
        native_mode = callable(semantic_reader)
        if not native_mode and not self.config.get("vision", True):
            return None
        if not native_mode and self.active_provider == "lmstudio" and self.local_vision_disabled:
            return None
        # Paint necesita presentar el frame; el backend nativo ya posee la imagen canónica.
        if not native_mode:
            time.sleep(.12)
        image = self.paint.screenshot_data_url() if not native_mode else None
        if not native_mode and not image:
            return None
        if self.cancel.is_set():
            return None
        summary = ", ".join(
            str(record.get("action", "?")) + ("" if record.get("ok") else " (falló)")
            for record in records
        )
        self.log(("Monitor vectorial" if native_mode else "Monitor visual") + f": estado actualizado tras {summary}")
        semantic_state = None
        if native_mode:
            semantic_state = semantic_reader()
            native_warnings = list(semantic_state.get("warnings", []))
            last_action = semantic_state.get("last_action", {})
            for warning in last_action.get("warnings", []) if isinstance(last_action, dict) else []:
                native_warnings.append({
                    "object_id": last_action.get("object_id"), "issue": warning,
                    "bounds": last_action.get("actual_bounds"),
                })
            audit = {
                "status": "needs_correction" if native_warnings else "correct",
                "visible_summary": (
                    f"Estado vectorial exacto: {semantic_state.get('object_count', 0)} objetos "
                    f"en página {semantic_state.get('page', 1)}"
                ),
                "defects": native_warnings,
                "protected_content": [obj.get("id") for obj in semantic_state.get("objects", [])],
                "next_best_action": "Corregir solo los warnings exactos" if native_warnings else "Continuar el plan",
                "confidence": 1.0,
                "source": "native_object_model",
            }
            self.log(
                f"Estado nativo omnisciente: {semantic_state.get('object_count', 0)} objetos, "
                f"{len(native_warnings)} aviso(s) exactos"
            )
        else:
            audit = self.visual_audit(image, records)
        if not native_mode and self.active_provider == "lmstudio" and self.local_vision_disabled:
            return None
        records_text = json.dumps(records, ensure_ascii=False)
        state_text = json.dumps(semantic_state, ensure_ascii=False) if semantic_state else "No disponible en el backend Paint."
        audit_text = json.dumps(audit, ensure_ascii=False) if audit else "No disponible; inspecciona tú la captura."
        instruction = (
            "[BOARD_MONITOR AUTOMÁTICO — NO ES UNA NUEVA PETICIÓN DEL ALUMNO]\n"
            f"Esta es la captura REAL del lienzo justo después de: {summary}.\n"
            "Inspecciona lo que realmente quedó dibujado, no lo que pretendías dibujar. "
            "Comprueba legibilidad, texto cortado, signos extraños, solapamientos, proporción, "
            "alineación, conexiones y marcas sin significado. Si está correcto pero incompleto, "
            "continúa sin borrar. Si hay un defecto real, prefiere redibujar o una corrección local; "
            "usa erase solo sobre la región mínima identificada por evidencia visible y nunca sobre "
            "contenido correcto. "
            "No describas este mensaje ni felicites el resultado antes de verificarlo visualmente.\n"
            f"[ÚLTIMAS ACCIONES Y COORDENADAS]\n{records_text}\n"
            f"[BOARD_STATE VECTORIAL EXACTO]\n{state_text}\n"
            f"[BOARD_AUDIT DEL CRÍTICO VISUAL]\n{audit_text}"
        )
        if native_mode:
            # El modelo local no necesita visión: recibe una descripción estructurada y exacta.
            return {"role":"user", "content":instruction}
        return {"role":"user","content":[
            {"type":"text","text":instruction},
            {"type":"image_url","image_url":{"url":image,"detail":"high"}},
        ]}

    @staticmethod
    def is_board_monitor_message(message: dict[str, Any]) -> bool:
        content = message.get("content")
        if isinstance(content, str):
            return content.startswith("[BOARD_MONITOR AUTOMÁTICO")
        if isinstance(content, list):
            return any(
                isinstance(part, dict)
                and str(part.get("text", "")).startswith("[BOARD_MONITOR AUTOMÁTICO")
                for part in content
            )
        return False

    @staticmethod
    def message_text(message: dict[str, Any]) -> str:
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                str(part.get("text", "")) for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
        return ""

    @classmethod
    def is_automatic_message(cls, message: dict[str, Any]) -> bool:
        text = cls.message_text(message).lstrip()
        return text.startswith((
            "[BOARD_MONITOR AUTOMÁTICO", "[LESSON_CHECKPOINT AUTOMÁTICO",
            "[LOOP_GUARD AUTOMÁTICO", "[VISUAL_GUARD AUTOMÁTICO", "[CONTINUACIÓN AUTOMÁTICA",
        ))

    def lesson_checkpoint_message(self, total_actions: int, page_number: int) -> dict[str, Any]:
        return {"role":"user","content":(
            "[LESSON_CHECKPOINT AUTOMÁTICO — NO ES UNA NUEVA PETICIÓN DEL ALUMNO]\n"
            f"La clase lleva {total_actions} acciones visuales y está en la página {page_number}. "
            "Esto NO es un límite ni una orden de terminar. Comprueba la captura más reciente: "
            "si aún hay espacio, continúa el siguiente bloque; si está llena, usa next_page, "
            "titula la nueva parte y sigue. Solo finaliza cuando la solicitud esté realmente "
            "respondida con la profundidad que pidió el alumno."
        )}

    def compact_long_turn(self, active_request: str, total_actions: int, page_number: int) -> None:
        """Resume un turno enorme en un punto seguro sin separar tool calls de sus resultados."""
        if len(self.history) < self.COMPACT_HISTORY_MESSAGES:
            return
        latest_monitor = next(
            (message for message in reversed(self.history) if self.is_board_monitor_message(message)),
            None,
        )
        recent_dialogue: list[str] = []
        for message in self.history[1:]:
            if self.is_automatic_message(message) or message.get("role") == "tool" or message.get("tool_calls"):
                continue
            role = message.get("role")
            content = self.message_text(message).strip()
            if role in {"user", "assistant"} and content:
                speaker = "Alumno" if role == "user" else "Profesora"
                recent_dialogue.append(f"{speaker}: {content[:500]}")
        context = (
            "[CONTINUACIÓN AUTOMÁTICA DE UNA CLASE LARGA — NO ES UNA NUEVA PETICIÓN]\n"
            f"Solicitud activa: {active_request[:1200]}\n"
            f"Progreso: {total_actions} acciones visuales; página {page_number}.\n"
            f"Plan visual vigente: {json.dumps(self.current_plan, ensure_ascii=False)[:1800]}\n"
            + ("Conversación reciente:\n" + "\n".join(recent_dialogue[-6:]) + "\n" if recent_dialogue else "")
            + "Continúa desde el estado visible. No reinicies la explicación ni repitas lo ya dibujado. "
              "Si no queda espacio, usa next_page y continúa en una página nueva archivada."
        )
        if latest_monitor and isinstance(latest_monitor.get("content"), list):
            content = [dict(part) if isinstance(part, dict) else part for part in latest_monitor["content"]]
            text_part = next((part for part in content if isinstance(part, dict) and part.get("type") == "text"), None)
            if text_part is not None:
                text_part["text"] = self.message_text(latest_monitor) + "\n\n" + context
            continuation: dict[str, Any] = {"role":"user", "content":content}
        else:
            continuation = {"role":"user", "content":context}
        self.history = [self.history[0], continuation]
        self.log(f"Clase larga: contexto compactado sin detenerla ({total_actions} acciones)")

    def discard_stale_images(self) -> None:
        """Mantiene solo la captura más reciente para no inflar el contexto visual."""
        for message in self.history:
            content = message.get("content")
            if not isinstance(content, list):
                continue
            text_parts = [
                str(part.get("text", "")) for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            ]
            if self.is_board_monitor_message(message):
                message["content"] = "[BOARD_MONITOR anterior ya procesado; usa la captura más reciente]"
            else:
                message["content"] = "\n".join(filter(None, text_parts))

    @staticmethod
    def serialize_assistant(message: Any) -> dict[str, Any]:
        data: dict[str, Any] = {"role":"assistant", "content":message.content}
        if message.tool_calls:
            data["tool_calls"] = [
                {"id":tc.id,"type":"function","function":{"name":tc.function.name,"arguments":tc.function.arguments}}
                for tc in message.tool_calls
            ]
        return data

    @staticmethod
    def without_images(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Copia el historial conservando texto/tool calls pero retirando imágenes."""
        clean: list[dict[str, Any]] = []
        for message in messages:
            cloned = dict(message)
            content = cloned.get("content")
            if isinstance(content, list):
                text_parts = [
                    str(part.get("text", "")) for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                ]
                cloned["content"] = "\n".join(filter(None, text_parts))
            clean.append(cloned)
        return clean

    def execute_tool(self, name: str, args: dict[str, Any]) -> str:
        if self.cancel.is_set():
            raise CancelledDrawing()
        self.log(f"Acción: {name}")
        if name == "research_verified_sources":
            return json.dumps({"ok":True,"action":name,"result":self.research_verified_sources(**args)},ensure_ascii=False)
        if name == "plan_lesson":
            review = self.review_lesson_plan(args)
            if not review.get("approved"):
                self.current_plan = {}
                self.log("Plan rechazado por el anti-alucinador: " + "; ".join(review.get("issues", [])))
                return json.dumps({"ok":False,"action":name,"plan_accepted":False,
                                   "review":review,"error":"Corrige el plan antes de dibujar."},ensure_ascii=False)
            self.current_plan = dict(args)
            self.narration_script = list(args.get("narration_script", []))
            self.narration_index = 0
            self.plan_visual_actions = 0
            self.log(
                f"Plan visual: {args.get('title', 'sin título')} — "
                f"{len(args.get('steps', []))} pasos, {args.get('estimated_pages', 1)} página(s)"
            )
            try: confidence = float(review.get("confidence", 0))
            except (TypeError,ValueError): confidence = 0.0
            self.log(f"Anti-alucinador: plan aprobado ({confidence:.0%} confianza)")
            return json.dumps({"ok": True, "action": name, "plan_accepted": True,
                               "review":review}, ensure_ascii=False)
        if name in self.PLANNED_DRAW_ACTIONS:
            guard = getattr(self.paint,"preflight_action",None)
            if callable(guard):
                verdict = guard(name,args)
                if not verdict.get("ok"):
                    self.log(f"Guardián geométrico bloqueó {name}: {verdict.get('error')}")
                    return json.dumps({"ok":False,"action":name,"blocked_by_geometry_guard":True,
                                       **verdict},ensure_ascii=False)
            self._start_planned_narration()
        tool_result: Any = None
        if name == "say":
            self.speech.speak(args["text"], self.cancel)
        elif name == "write": self.paint.write(**args)
        elif name == "math_formula": self.paint.math_formula(**args)
        elif name == "render_equation": self.paint.render_equation(**args)
        elif name == "math_matrix": self.paint.math_matrix(**args)
        elif name == "plot_function": self.paint.plot_function(**args)
        elif name == "draw_diagram": tool_result=self.paint.draw_diagram(**args)
        elif name == "highlight_region": self.paint.highlight_region(**args)
        elif name == "annotate_live": self.paint.annotate_live(**args)
        elif name == "split_view": tool_result=self.paint.split_view(**args)
        elif name == "matrix_operations": tool_result=self.paint.matrix_operations(**args)
        elif name == "token_visualizer": tool_result=self.paint.token_visualizer(**args)
        elif name == "simulate_training": tool_result=self.paint.simulate_training(**args)
        elif name == "create_poll": tool_result=self.paint.create_poll(**args)
        elif name == "spawn_slider": tool_result=self.paint.spawn_slider(**args)
        elif name == "code_sandbox": tool_result=self.paint.code_sandbox(**args)
        elif name == "grade_snippet": tool_result=self.paint.grade_snippet(**args)
        elif name == "generate_exercise": tool_result=self.paint.generate_exercise(**args)
        elif name == "save_snapshot": tool_result=self.paint.save_snapshot(**args)
        elif name == "load_snapshot": tool_result=self.paint.load_snapshot(**args)
        elif name == "export_board": tool_result=self.paint.export_board(**args)
        elif name in self.LEARNER_ACTIONS: tool_result=self.learner_tool(name,args)
        elif name == "polygon": self.paint.polygon(**args)
        elif name == "arc": self.paint.arc(**args)
        elif name == "angle_mark": self.paint.angle_mark(**args)
        elif name == "dimension": self.paint.dimension(**args)
        elif name == "line": self.paint.line(**args)
        elif name == "arrow": self.paint.arrow(**args)
        elif name == "rectangle": self.paint.rectangle(**args)
        elif name == "ellipse": self.paint.ellipse(**args)
        elif name == "freehand": self.paint.freehand(**args)
        elif name == "point": self.paint.point(**args)
        elif name == "erase": self.paint.erase(**args)
        elif name == "undo": self.paint.undo()
        elif name == "redo": self.paint.redo()
        elif name == "zoom": self.paint.zoom(**args)
        elif name == "toggle_grid": self.paint.toggle_grid()
        elif name == "next_page":
            archived = self.paint.next_page(**args)
            payload: dict[str, Any] = {"ok": True, "action": name, "archived_page": archived}
            reporter = getattr(self.paint, "action_report", None)
            if callable(reporter):
                payload["board_report"] = reporter()
            return json.dumps(payload, ensure_ascii=False)
        elif name == "clear": self.paint.clear()
        else: raise ValueError(f"Herramienta desconocida: {name}")
        if name in self.PLANNED_DRAW_ACTIONS:
            self.plan_visual_actions += 1
        payload = {"ok": True, "action": name}
        if tool_result is not None:
            payload["result"] = tool_result
        reporter = getattr(self.paint, "action_report", None)
        if callable(reporter):
            payload["board_report"] = reporter()
        return json.dumps(payload, ensure_ascii=False)

    def respond(self, text: str) -> None:
        self.cancel.clear()
        self.current_plan = {}
        self.narration_script = []
        self.narration_index = 0
        self.plan_visual_actions = 0
        self.history.append(self.user_message(text))
        # Recorta solo por un límite de turno de usuario para no dejar una llamada
        # a herramienta separada de su respuesta, lo que invalidaría el historial.
        if len(self.history) > 60:
            cut = next((i for i in range(max(1, len(self.history)-45), len(self.history))
                        if self.history[i].get("role") == "user"
                        and not self.is_automatic_message(self.history[i])), 1)
            self.history = [self.history[0]] + self.history[cut:]
        total_visual_actions = 0
        actions_since_checkpoint = 0
        page_number = self.lesson_page_number
        last_visual_signature = ""
        identical_visual_repeats = 0
        blocked_loop_actions = 0
        erase_needs_redraw = False
        visual_answer_retries = 0

        # No existe un máximo arbitrario de órdenes: una clase termina cuando el
        # modelo responde, el alumno la interrumpe o el detector encuentra un
        # bucle real sin progreso.
        while True:
            if self.cancel.is_set():
                raise CancelledDrawing()
            response = self._completion()
            message = response.choices[0].message
            self.history.append(self.serialize_assistant(message))
            if not message.tool_calls:
                if message.content:
                    words=normalize_text(message.content).split()
                    trivial=bool(re.fullmatch(
                        r"\s*(hola|buenas|gracias|de nada|vale|ok|sí|si|no|hasta luego)[.!?\s]*",
                        normalize_text(text),re.I,
                    ))
                    if total_visual_actions==0 and not trivial and len(words)>=5 and visual_answer_retries<2:
                        visual_answer_retries+=1
                        self.history.append({"role":"user","content":(
                            "[VISUAL_GUARD AUTOMÁTICO — NO ES UNA PETICIÓN NUEVA]\n"
                            "Intentaste terminar una explicación sustantiva sin representar nada en la pizarra. "
                            "La respuesta oral no cuenta como clase visual. Investiga si corresponde, llama a "
                            "plan_lesson y explica el contenido con herramientas gráficas antes de responder. "
                            "No repitas el párrafo anterior ni menciones esta barrera."
                        )})
                        self.log("Guardián visual: respuesta solo oral rechazada; debe enseñarlo en la pizarra")
                        continue
                    provider_name = "LM Studio" if self.active_provider == "lmstudio" else "DeepSeek"
                    self.log(provider_name + ": " + message.content.strip())
                    self.speech.speak(message.content.strip(), self.cancel)
                return
            monitored_actions: list[dict[str, Any]] = []
            loop_guard_triggered = False
            for call in message.tool_calls:
                action_name = call.function.name
                args: dict[str, Any] = {}
                try:
                    args = json.loads(call.function.arguments or "{}")
                    signature = action_name + ":" + json.dumps(args, sort_keys=True, ensure_ascii=False)
                    if action_name in self.PLANNED_DRAW_ACTIONS and not self.current_plan:
                        result = json.dumps({
                            "ok": False, "plan_required": True,
                            "error": "Antes de dibujar debes llamar a plan_lesson y definir composición y criterios de éxito.",
                        }, ensure_ascii=False)
                        self.log(f"Acción {action_name} aplazada: falta el plan visual")
                    elif action_name == "erase" and erase_needs_redraw:
                        result = json.dumps({
                            "ok": False, "blocked_erase_chain": True,
                            "error": "Ya borraste una región; redibuja la corrección antes de volver a usar erase.",
                        }, ensure_ascii=False)
                        self.log("Borrado consecutivo bloqueado: primero debe redibujar la zona anterior")
                    elif action_name in self.VISUAL_ACTIONS:
                        if signature == last_visual_signature:
                            identical_visual_repeats += 1
                        else:
                            last_visual_signature = signature
                            identical_visual_repeats = 1
                        if identical_visual_repeats > self.MAX_IDENTICAL_VISUAL_REPEATS:
                            loop_guard_triggered = True
                            blocked_loop_actions += 1
                            result = json.dumps({
                                "ok": False,
                                "blocked_by_loop_guard": True,
                                "error": "La misma orden visual ya se repitió sin aportar progreso; cambia de estrategia.",
                            }, ensure_ascii=False)
                            self.log(f"Bucle evitado: omití una repetición idéntica de {action_name}")
                        else:
                            result = self.execute_tool(action_name, args)
                    else:
                        result = self.execute_tool(action_name, args)
                except CancelledDrawing:
                    result = json.dumps({"ok":False,"interrupted":True}, ensure_ascii=False)
                    self.history.append({"role":"tool","tool_call_id":call.id,"content":result})
                    raise
                except Exception as exc:
                    result = json.dumps({"ok":False,"error":str(exc)}, ensure_ascii=False)
                    self.log(f"Error en {action_name}: {exc}")
                self.history.append({"role":"tool","tool_call_id":call.id,"content":result})
                if action_name in self.VISUAL_ACTIONS:
                    try:
                        parsed_result = json.loads(result)
                        ok = bool(parsed_result.get("ok"))
                    except Exception:
                        parsed_result = {"ok": False, "error": "Resultado de herramienta no interpretable"}
                        ok = False
                    monitored_actions.append({
                        "action": action_name,
                        "args": self.compact_action_args(action_name, args),
                        "ok": ok,
                        "result": parsed_result,
                    })
                    if ok:
                        total_visual_actions += 1
                        actions_since_checkpoint += 1
                        blocked_loop_actions = 0
                        if action_name == "next_page":
                            page_number += 1
                            self.lesson_page_number = page_number
                        elif action_name == "clear":
                            page_number = 1
                            self.lesson_page_number = 1
                        if action_name == "erase":
                            erase_needs_redraw = True
                        elif action_name in self.PLANNED_DRAW_ACTIONS:
                            erase_needs_redraw = False
                        elif action_name in {"undo", "clear", "next_page"}:
                            erase_needs_redraw = False
            monitor = self.board_monitor_message(monitored_actions)
            if monitor:
                self.discard_stale_images()
                self.history.append(monitor)
            if loop_guard_triggered:
                self.history.append({"role":"user","content":(
                    "[LOOP_GUARD AUTOMÁTICO — NO ES UNA NUEVA PETICIÓN DEL ALUMNO]\n"
                    "Has repetido exactamente la misma orden visual. Esa repetición fue bloqueada. "
                    "Mira el lienzo, elige una acción distinta que produzca progreso o termina si la "
                    "explicación ya está completa. No vuelvas a emitir la misma llamada."
                )})
            if blocked_loop_actions >= self.MAX_BLOCKED_LOOP_ACTIONS:
                explanation = (
                    "Detuve únicamente un bucle de órdenes idénticas que no modificaba la pizarra. "
                    "Puedes pedirme que continúe y retomaré desde el estado visible."
                )
                self.log("Protección de bucle activada: seis órdenes idénticas bloqueadas")
                self.speech.speak(explanation, self.cancel)
                return
            if actions_since_checkpoint >= self.CHECKPOINT_EVERY_VISUAL_ACTIONS:
                self.history.append(self.lesson_checkpoint_message(total_visual_actions, page_number))
                self.log(
                    f"Clase larga: checkpoint {total_visual_actions}; la explicación continúa"
                )
                actions_since_checkpoint = 0
            self.compact_long_turn(text, total_visual_actions, page_number)


class DiscordClassroomIntegration:
    """Aula Discord activable; limita toda lectura y publicación a una sesión explícita."""

    SERVICE = "TeachAI Discord Bot"

    def __init__(self, config: dict[str, Any], log: Callable[[str], None],
                 question_callback: Callable[[str], None], snapshot_provider: Callable[[], str | None]):
        self.config,self.log=config,log
        self.question_callback,self.snapshot_provider=question_callback,snapshot_provider
        self.thread: threading.Thread | None=None
        self.loop: asyncio.AbstractEventLoop | None=None
        self.bot=None;self.voice_client=None;self.audio_queue=None
        self.connected=threading.Event();self.stop_requested=threading.Event()
        self.guild_id: int | None=None;self.channel_id: int | None=None;self.owner_id: int | None=None
        self.read_chat=False;self.recent_chat: deque[str]=deque(maxlen=20)
        self.last_snapshot=0.0;self.snapshot_pending=False

    @classmethod
    def save_token(cls, token: str) -> None:
        import keyring
        keyring.set_password(cls.SERVICE,"bot-token",token.strip())

    @classmethod
    def load_token(cls) -> str:
        try:
            import keyring
            return keyring.get_password(cls.SERVICE,"bot-token") or ""
        except Exception:
            return ""

    @classmethod
    def delete_token(cls) -> None:
        try:
            import keyring
            keyring.delete_password(cls.SERVICE,"bot-token")
        except Exception:
            pass

    def start(self, token: str) -> None:
        if self.thread and self.thread.is_alive():
            self.log("Discord ya está conectado o conectándose")
            return
        if not token.strip(): raise ValueError("Falta el token del bot de Discord")
        self.stop_requested.clear()
        self.thread=threading.Thread(target=self._thread_main,args=(token.strip(),),daemon=True)
        self.thread.start()

    def _thread_main(self, token: str) -> None:
        try: asyncio.run(self._run(token))
        except Exception as exc:
            self.connected.clear();self.log(f"ERROR Discord: {exc}")

    async def _run(self, token: str) -> None:
        import discord
        from discord.ext import commands
        intents=discord.Intents.none();intents.guilds=True;intents.voice_states=True
        intents.messages=True;intents.message_content=bool(self.config.get("discord_read_chat",False))
        bot=commands.Bot(command_prefix="!teachai-disabled-",intents=intents)
        self.bot=bot;self.loop=asyncio.get_running_loop();self.audio_queue=asyncio.Queue()
        group=discord.app_commands.Group(name="teachai",description="Aula interactiva TeachAI")

        @group.command(name="learn",description="Abre esta sala como aula y conecta a TeachAI")
        @discord.app_commands.describe(leer_chat="Usar el chat de esta sala como contexto",unirse_voz="Entrar en tu llamada o Stage")
        async def learn(interaction: discord.Interaction, leer_chat: bool=False, unirse_voz: bool=True):
            if not interaction.guild or not interaction.channel:
                await interaction.response.send_message("TeachAI necesita ejecutarse dentro de un servidor.",ephemeral=True);return
            if self.owner_id and self.owner_id!=interaction.user.id:
                await interaction.response.send_message("Ya existe un aula activa abierta por otra persona. Debe cerrarla primero con `/teachai leave`.",ephemeral=True);return
            self.guild_id=interaction.guild.id;self.channel_id=interaction.channel.id
            self.owner_id=interaction.user.id
            self.read_chat=bool(leer_chat and self.config.get("discord_read_chat",False))
            joined=""
            if unirse_voz:
                voice=getattr(interaction.user,"voice",None)
                target=getattr(voice,"channel",None)
                if target:
                    try:
                        if interaction.guild.voice_client: await interaction.guild.voice_client.move_to(target)
                        else: await target.connect(self_deaf=True)
                        self.voice_client=interaction.guild.voice_client
                        if isinstance(target,discord.StageChannel):
                            try: await interaction.guild.me.edit(suppress=False)
                            except Exception: pass
                        joined=f" Voz conectada a **{target.name}**."
                    except Exception as exc: joined=f" No pude entrar en voz: `{exc}`."
                else: joined=" Entra primero en una llamada o Stage si quieres audio."
            mode=("Leeré solamente este canal." if self.read_chat else
                  "No leeré el chat general; solo los slash commands."
                  + (" Activa Lectura de chat en el PC y el Message Content Intent si quieres contexto." if leer_chat else ""))
            await interaction.response.send_message(
                f"📚 Aula TeachAI activada aquí. {mode}{joined}\n"
                "Usa `/teachai question` para preguntar y `/teachai board` para ver la pizarra. "
                "Discord no permite a bots oficiales emitir Go Live; publicaré la pizarra como imágenes actualizadas.")
            await self._publish_snapshot(force=True)

        @group.command(name="question",description="Envía una pregunta a la profesora del PC")
        @discord.app_commands.describe(pregunta="Lo que quieres preguntar a TeachAI")
        async def question(interaction: discord.Interaction,pregunta: str):
            if not self._authorized(interaction):
                await interaction.response.send_message("Esta sala no es el aula activa. Usa `/teachai learn` aquí primero.",ephemeral=True);return
            context="\n".join(self.recent_chat)[-1800:] if self.read_chat else ""
            prompt=f"[Pregunta recibida desde Discord por {interaction.user.display_name}] {pregunta.strip()}"
            if context: prompt+=f"\n[Contexto reciente del aula]\n{context}"
            self.question_callback(prompt)
            await interaction.response.send_message("✦ Pregunta enviada a la profesora. Puedes interrumpirla con otra pregunta.")

        @group.command(name="board",description="Publica el estado actual de la pizarra")
        async def board(interaction: discord.Interaction):
            if not self._authorized(interaction):
                await interaction.response.send_message("Esta sala no es el aula activa.",ephemeral=True);return
            await interaction.response.defer(thinking=True)
            sent=await self._publish_snapshot(force=True,channel=interaction.channel)
            await interaction.followup.send("Pizarra actualizada." if sent else "No pude capturar la pizarra.",ephemeral=True)

        @group.command(name="leave",description="Cierra el aula y desconecta la llamada")
        async def leave(interaction: discord.Interaction):
            if not self._authorized(interaction) or (self.owner_id and interaction.user.id!=self.owner_id):
                await interaction.response.send_message("Solo quien abrió esta aula puede cerrarla.",ephemeral=True);return
            if interaction.guild and interaction.guild.voice_client: await interaction.guild.voice_client.disconnect(force=True)
            self.guild_id=self.channel_id=self.owner_id=None;self.read_chat=False;self.recent_chat.clear()
            await interaction.response.send_message("Aula cerrada. El bot sigue disponible pero ya no escucha ninguna sala.")

        bot.tree.add_command(group)

        @bot.event
        async def on_ready():
            try: synced=await bot.tree.sync()
            except Exception as exc: self.log(f"Discord: no pude sincronizar comandos: {exc}");synced=[]
            self.connected.set();self.config["discord_enabled"]=True
            self.log(f"Discord conectado como {bot.user} — {len(synced)} comando(s) sincronizado(s)")
            asyncio.create_task(self._audio_worker())

        @bot.event
        async def on_message(message: discord.Message):
            if message.author.bot or not self.read_chat or message.channel.id!=self.channel_id:
                return
            self.recent_chat.append(f"{message.author.display_name}: {message.content[:500]}")

        try: await bot.start(token,reconnect=True)
        finally: self.connected.clear();self.bot=None;self.loop=None

    def _authorized(self, interaction: Any) -> bool:
        return bool(interaction.guild and interaction.channel and interaction.guild.id==self.guild_id
                    and interaction.channel.id==self.channel_id)

    async def _audio_worker(self) -> None:
        import discord, imageio_ffmpeg
        while self.bot and not self.bot.is_closed():
            path=await self.audio_queue.get()
            voice=self.voice_client
            try:
                if not voice or not voice.is_connected(): continue
                while voice.is_playing(): await asyncio.sleep(.08)
                done=asyncio.Event()
                source=discord.FFmpegPCMAudio(path,executable=imageio_ffmpeg.get_ffmpeg_exe())
                voice.play(source,after=lambda _error:self.loop.call_soon_threadsafe(done.set))
                await done.wait()
            except Exception as exc: self.log(f"Discord voz: {exc}")
            finally:
                try: os.unlink(path)
                except OSError: pass

    def play_audio(self, source_path: str) -> None:
        if not self.loop or not self.audio_queue or not self.voice_client: return
        suffix=Path(source_path).suffix;fd,copied=tempfile.mkstemp(suffix=suffix,prefix="teachai_discord_");os.close(fd)
        shutil.copyfile(source_path,copied)
        asyncio.run_coroutine_threadsafe(self.audio_queue.put(copied),self.loop)

    async def _publish_snapshot(self, force: bool=False, channel: Any=None) -> bool:
        import discord
        now=time.monotonic();interval=float(self.config.get("discord_snapshot_interval",8.0))
        if not force and now-self.last_snapshot<interval:
            await asyncio.sleep(interval-(now-self.last_snapshot))
        if channel is None and self.bot and self.channel_id: channel=self.bot.get_channel(self.channel_id)
        if channel is None: return False
        data=self.snapshot_provider()
        if not data or "," not in data:return False
        raw=base64.b64decode(data.split(",",1)[1]);self.last_snapshot=now
        await channel.send(content="🧠 **Pizarra TeachAI — actualización en directo**",
                           file=discord.File(io.BytesIO(raw),filename="teachai-board.png"))
        return True

    def publish_snapshot(self) -> None:
        if self.loop and self.channel_id and not self.snapshot_pending:
            self.snapshot_pending=True
            async def publish_latest() -> None:
                try: await self._publish_snapshot()
                finally: self.snapshot_pending=False
            asyncio.run_coroutine_threadsafe(publish_latest(),self.loop)

    def stop(self) -> None:
        self.config["discord_enabled"]=False
        self.guild_id=self.channel_id=self.owner_id=None;self.read_chat=False
        if self.loop and self.bot:
            asyncio.run_coroutine_threadsafe(self.bot.close(),self.loop)
        self.connected.clear();self.log("Integración Discord desconectada")


MODERN_UI_HTML = r'''<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>TeachAI</title>
<style>
.activity-pill{position:absolute;z-index:18;left:50%;top:72px;transform:translateX(-50%);display:flex;align-items:center;gap:11px;min-height:40px;max-width:min(620px,72%);padding:8px 15px 8px 10px;border:1px solid rgba(255,255,255,.09);border-radius:999px;background:rgba(12,12,17,.76);backdrop-filter:blur(22px) saturate(135%);box-shadow:0 14px 48px rgba(0,0,0,.34),inset 0 1px rgba(255,255,255,.035);color:#dcd8e9;pointer-events:none;transition:border-color .45s,box-shadow .45s,background .45s}.activity-orbit{position:relative;width:24px;height:24px;flex:0 0 24px;border-radius:50%;background:rgba(118,87,255,.1)}.activity-orbit:before{content:"";position:absolute;inset:3px;border:1.5px solid rgba(168,149,255,.22);border-top-color:var(--accent2);border-radius:50%;animation:orbitSpin .9s linear infinite}.activity-orbit:after{content:"";position:absolute;width:5px;height:5px;left:9.5px;top:9.5px;border-radius:50%;background:var(--accent2);box-shadow:0 0 10px var(--accent)}.activity-copy{min-width:0;display:grid}.activity-kicker{font-size:9px;line-height:11px;text-transform:uppercase;letter-spacing:.14em;color:#777384}.activity-label{font-size:12px;line-height:16px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;will-change:opacity,filter,transform}.activity-label.swap{animation:stateBlur .48s cubic-bezier(.2,.8,.2,1)}.activity-pill[data-phase="idle"] .activity-orbit:before{animation:none;border-color:rgba(101,230,168,.32)}.activity-pill[data-phase="idle"] .activity-orbit:after{background:var(--ok);box-shadow:0 0 12px rgba(101,230,168,.8)}.activity-pill[data-phase="error"]{border-color:rgba(255,93,120,.3);box-shadow:0 14px 48px rgba(0,0,0,.34),0 0 28px rgba(255,93,120,.07)}.activity-pill[data-phase="error"] .activity-orbit:before{animation:dangerPulse 1.1s ease-in-out infinite;border-color:var(--danger)}.activity-pill[data-phase="error"] .activity-orbit:after{background:var(--danger)}.activity-pill[data-phase="interrupted"] .activity-orbit:before{animation:none;border-color:#f7c96f}.activity-pill[data-phase="interrupted"] .activity-orbit:after{background:#f7c96f;box-shadow:0 0 10px #f7c96f}.activity-pill[data-phase="verify"] .activity-orbit:before{animation-duration:1.6s}.activity-pill[data-phase="speak"] .activity-orbit{animation:speakingGlow 1.35s ease-in-out infinite}@keyframes orbitSpin{to{transform:rotate(360deg)}}@keyframes stateBlur{0%{opacity:.12;filter:blur(9px);transform:translateY(5px)}100%{opacity:1;filter:blur(0);transform:none}}@keyframes dangerPulse{50%{opacity:.35;transform:scale(.82)}}@keyframes speakingGlow{50%{box-shadow:0 0 18px rgba(118,87,255,.38)}}
.control-panel{margin-top:28px;padding-top:18px;border-top:1px solid var(--line);display:grid;gap:12px}.control-panel label{display:grid;gap:6px;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.08em}.control-panel select{width:100%;height:32px;color:#ddd9eb;background:#0a0a0d;border:1px solid var(--line);border-radius:8px;padding:0 8px;outline:0}.control-grid{display:grid;grid-template-columns:1fr 1fr;gap:6px}.control-grid button{height:32px;color:#aaa6b7;background:rgba(255,255,255,.025);border:1px solid var(--line);border-radius:8px;font-size:11px}.control-grid button:hover{color:#fff;border-color:rgba(118,87,255,.5);background:rgba(118,87,255,.09)}.captions{position:absolute;z-index:15;left:50%;bottom:54px;transform:translate(-50%,12px);max-width:min(820px,80%);padding:12px 18px;border:1px solid rgba(255,255,255,.1);border-radius:13px;background:rgba(8,8,12,.78);backdrop-filter:blur(18px);box-shadow:0 18px 60px rgba(0,0,0,.45);font-size:17px;line-height:1.45;text-align:center;opacity:0;pointer-events:none;transition:opacity .55s,transform .55s,filter .55s;filter:blur(8px)}.captions.show{opacity:1;transform:translate(-50%,0);filter:blur(0)}.captions.leaving{opacity:0;transform:translate(-50%,8px);filter:blur(7px)}.captions span{display:inline-block;margin-right:.28em;opacity:0;transform:translateY(5px);filter:blur(6px);animation:captionWord .42s cubic-bezier(.2,.8,.2,1) forwards}@keyframes captionWord{to{opacity:1;transform:none;filter:blur(0)}}#micbutton.active{color:var(--ok);background:rgba(101,230,168,.08)}#micbutton.loading{color:var(--accent2);animation:pulse 1.2s infinite}@keyframes pulse{50%{opacity:.55}}
:root{--bg:#08080a;--panel:#0e0e12;--panel2:#131319;--line:rgba(255,255,255,.075);--text:#f4f2ff;--muted:#8e8b9b;--accent:#7657ff;--accent2:#a895ff;--ok:#65e6a8;--danger:#ff5d78}*{box-sizing:border-box}html,body{margin:0;height:100%;overflow:hidden;background:var(--bg);color:var(--text);font:13px Inter,"Segoe UI Variable",Segoe UI,sans-serif}button,input,select{font:inherit}.shell{height:100%;display:grid;grid-template-rows:48px 1fr 46px}.top{display:flex;align-items:center;padding:0 18px;border-bottom:1px solid var(--line);background:rgba(8,8,10,.82);backdrop-filter:blur(22px);z-index:20}.brand{font-weight:650;letter-spacing:-.02em;font-size:15px}.brand i{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--accent);box-shadow:0 0 18px var(--accent);margin-right:9px}.timeline{height:1px;background:var(--line);flex:1;margin:0 32px;position:relative}.timeline:after{content:"";position:absolute;left:0;top:-1px;width:var(--progress,8%);height:3px;border-radius:4px;background:linear-gradient(90deg,var(--accent),var(--accent2));transition:width .6s cubic-bezier(.2,.8,.2,1)}.topbtn,.iconbtn{border:1px solid var(--line);background:rgba(255,255,255,.025);color:var(--muted);border-radius:9px;padding:7px 10px;transition:.2s}.topbtn:hover,.iconbtn:hover{color:#fff;border-color:rgba(118,87,255,.5);box-shadow:0 0 20px rgba(118,87,255,.13)}.main{min-height:0;display:grid;grid-template-columns:248px 1fr 280px;transition:grid-template-columns .35s cubic-bezier(.2,.8,.2,1)}body.focus .main{grid-template-columns:0 1fr 0}aside{overflow:hidden;border-right:1px solid var(--line);background:var(--panel);transition:opacity .2s}aside.right{border-left:1px solid var(--line);border-right:0}body.focus aside{opacity:0;pointer-events:none}.section{padding:18px}.eyebrow{text-transform:uppercase;letter-spacing:.13em;font-size:10px;color:#777384;margin-bottom:14px}.mastery{display:grid;gap:13px}.skill{display:grid;grid-template-columns:1fr auto;gap:7px}.skill b{font-size:12px;font-weight:520}.skill span{font-variant-numeric:tabular-nums;color:var(--muted);font-size:11px}.rail{grid-column:1/3;height:2px;background:#202029;border-radius:5px}.rail i{display:block;height:100%;width:var(--v);background:var(--accent);box-shadow:0 0 10px rgba(118,87,255,.45);transition:width .55s}.canvas-wrap{position:relative;min-width:0;overflow:hidden;background:radial-gradient(circle at 50% 45%,#15131e 0,#0b0b0e 52%,#08080a 100%)}.canvas-wrap:before{content:"";position:absolute;inset:0;background-image:radial-gradient(rgba(255,255,255,.09) .7px,transparent .7px);background-size:24px 24px;mask-image:radial-gradient(circle,#000 15%,transparent 75%);pointer-events:none}.canvas{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%) scale(1);transform-origin:center;will-change:transform;transition:filter .2s}.board-card{padding:10px;border:1px solid rgba(255,255,255,.1);border-radius:16px;background:#111116;box-shadow:0 22px 80px rgba(0,0,0,.5)}#board{display:block;width:min(78vw,1000px);height:auto;border-radius:10px;background:#fff}.cursorhint{position:absolute;left:20px;bottom:18px;color:#676473;font-size:11px}.floatbar{position:absolute;left:50%;top:18px;transform:translateX(-50%);display:flex;gap:6px;padding:5px;border:1px solid var(--line);border-radius:12px;background:rgba(14,14,18,.82);backdrop-filter:blur(18px)}.floatbar button{border:0;background:transparent;color:#aaa6b7;border-radius:8px;padding:7px 10px}.floatbar button:hover{background:rgba(118,87,255,.13);color:#fff}.log{height:calc(100% - 190px);overflow:auto;scrollbar-width:thin;padding-right:4px}.event{padding:10px 0;border-bottom:1px solid rgba(255,255,255,.045);color:#aaa6b7;line-height:1.45}.event time{display:block;color:#555260;font-size:10px;margin-bottom:3px}.voice{margin-top:14px;padding:14px;border:1px solid var(--line);border-radius:13px;background:#0b0b0e}.meter{height:28px;display:flex;align-items:center;gap:3px}.meter i{width:3px;height:calc(3px + var(--level)*22px);background:var(--accent);border-radius:5px;transition:height .08s}.voice small{color:var(--muted)}.bottom{display:flex;align-items:center;gap:10px;padding:0 14px;border-top:1px solid var(--line);background:var(--panel)}.prompt{flex:1;height:32px;border:1px solid var(--line);border-radius:9px;background:#0a0a0d;color:#fff;padding:0 12px;outline:0}.prompt:focus{border-color:rgba(118,87,255,.65);box-shadow:0 0 0 3px rgba(118,87,255,.09)}.send{height:32px;border:0;border-radius:9px;padding:0 16px;background:var(--accent);color:#fff;font-weight:600}.stop{height:32px;border:1px solid rgba(255,93,120,.25);border-radius:9px;background:rgba(255,93,120,.08);color:#ff8296}.status{color:var(--muted);font-size:11px;min-width:120px}.palette{position:fixed;top:18%;left:50%;width:min(620px,90vw);transform:translate(-50%,-12px) scale(.98);opacity:0;pointer-events:none;z-index:100;background:#111116;border:1px solid rgba(255,255,255,.11);border-radius:16px;box-shadow:0 35px 120px #000;padding:10px;transition:.18s}.palette.open{opacity:1;transform:translate(-50%,0) scale(1);pointer-events:auto}.palette input{width:100%;height:44px;border:0;border-bottom:1px solid var(--line);background:transparent;color:#fff;outline:0;font-size:15px;padding:0 10px}.commands{padding-top:8px}.cmd{padding:11px 10px;border-radius:9px;color:#bbb7c7}.cmd:hover{background:rgba(118,87,255,.12);color:#fff}.toast{position:fixed;right:18px;top:62px;background:#15151b;border:1px solid var(--line);border-radius:11px;padding:11px 14px;box-shadow:0 18px 50px #000;opacity:0;transform:translateY(-8px);transition:.25s;z-index:200}.toast.show{opacity:1;transform:none}.skeleton{animation:shimmer 1.4s infinite;background:linear-gradient(100deg,#17171d 35%,#23232c 50%,#17171d 65%);background-size:300% 100%}@keyframes shimmer{to{background-position:-100% 0}}@media(max-width:1000px){.main{grid-template-columns:210px 1fr}.right{display:none}}@media(max-width:720px){.main{grid-template-columns:1fr}aside{display:none}.status{display:none}}
</style></head><body><div class="shell"><header class="top"><div class="brand"><i></i>TeachAI</div><div class="timeline"></div><button class="topbtn" onclick="palette(true)">⌘ K</button><button class="topbtn" style="margin-left:7px" onclick="focusMode()">Focus</button></header><main class="main"><aside><div class="section"><div class="eyebrow">Skill tree</div><div id="skills" class="mastery"><div class="skill"><b>Preparando modelo</b><span>—</span><div class="rail skeleton"></div></div></div><div class="control-panel"><div class="eyebrow">Controles</div><label>Micrófono<select id="micselect" onchange="setMic(this.value)"><option value="default">Predeterminado</option></select></label><label>Modelo<select id="provider" onchange="act('set_provider',{value:this.value})"><option value="auto">Automático</option><option value="lmstudio">LM Studio</option><option value="deepseek">DeepSeek</option></select></label><label>Voz<select id="tts" onchange="act('set_tts',{value:this.value})"><option value="kokoro">Kokoro humana</option><option value="edge">Edge TTS</option></select></label><div class="control-grid"><button onclick="act('undo')">↶ Deshacer</button><button onclick="act('redo')">↷ Rehacer</button><button onclick="act('grid')">⌗ Cuadrícula</button><button onclick="act('export',{format:'png'})">⇩ Exportar</button><button onclick="clearBoard()">⌫ Limpiar</button><button onclick="act('snapshot')">◇ Snapshot</button></div></div></div></aside><section class="canvas-wrap" id="viewport"><div class="floatbar"><button onclick="zoom(-.1)">−</button><button onclick="resetView()">Encajar</button><button onclick="zoom(.1)">＋</button><button id="micbutton" onclick="toggleMic()">Micrófono</button><button onclick="act('snapshot')">Snapshot</button></div><div class="canvas" id="canvas"><div class="board-card"><img id="board" alt="Pizarra TeachAI"></div></div><div class="captions" id="captions"></div><div class="cursorhint">Rueda: zoom · arrastrar: mover · doble clic: encajar</div></section><aside class="right"><div class="section" style="height:100%"><div class="eyebrow">Actividad en tiempo real</div><div class="log" id="log"></div><div class="voice"><div class="meter" id="meter"></div><small id="mictext">Micrófono inactivo</small></div></div></aside></main><footer class="bottom"><span class="status" id="status">Preparado</span><input class="prompt" id="prompt" placeholder="Pregunta, explica o dibuja…" autocomplete="off"><button class="send" onclick="submitPrompt()">Enviar</button><button class="stop" onclick="act('interrupt')">Interrumpir</button></footer></div><div class="palette" id="palette"><input id="search" placeholder="Buscar comandos, conceptos y sesiones…"><div class="commands"><div class="cmd" onclick="act('voice');palette(false)">Activar escucha full-duplex</div><div class="cmd" onclick="act('stop_voice');palette(false)">Desactivar micrófono</div><div class="cmd" onclick="act('prepare_model');palette(false)">Preparar modelo local</div><div class="cmd" onclick="act('snapshot');palette(false)">Guardar snapshot de la clase</div><div class="cmd" onclick="act('export',{format:'pdf'});palette(false)">Exportar clase en PDF</div><div class="cmd" onclick="focusMode();palette(false)">Alternar modo focus</div><div class="cmd" onclick="resetView();palette(false)">Centrar pizarra</div></div></div><div class="toast" id="toast"></div><script>
const TOKEN='__TOKEN__';let scale=1,tx=0,ty=0,drag=false,last=[0,0],seq=-1;const $=q=>document.querySelector(q);const viewport=$('#viewport'),canvas=$('#canvas');for(let i=0;i<22;i++)$('#meter').innerHTML+='<i style="--level:.08"></i>';
function toast(t){let e=$('#toast');e.textContent=t;e.classList.add('show');setTimeout(()=>e.classList.remove('show'),2400)}
async function api(path,opt={}){opt.headers={...(opt.headers||{}),'X-TeachAI-Token':TOKEN,'Content-Type':'application/json'};let r=await fetch(path,opt);if(!r.ok)throw Error(await r.text());return r.json()}
async function act(action,data={}){try{let r=await api('/api/action',{method:'POST',body:JSON.stringify({action,...data})});toast(r.message||'Hecho')}catch(e){toast('Error: '+e.message)}}
function submitPrompt(){let e=$('#prompt'),text=e.value.trim();if(!text)return;e.value='';act('prompt',{text})}$('#prompt').addEventListener('keydown',e=>{if(e.key==='Enter')submitPrompt();if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k')palette(true)});document.addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'){e.preventDefault();palette(true)}if(e.key==='Escape')palette(false)});
function palette(on){$('#palette').classList.toggle('open',on);if(on)setTimeout(()=>$('#search').focus(),30)}function focusMode(){document.body.classList.toggle('focus');setTimeout(resetView,360)}
function apply(){canvas.style.transform=`translate(calc(-50% + ${tx}px),calc(-50% + ${ty}px)) scale(${scale})`}function zoom(d){scale=Math.max(.35,Math.min(3,scale+d));apply()}function resetView(){scale=1;tx=ty=0;apply()}viewport.addEventListener('wheel',e=>{e.preventDefault();zoom(e.deltaY>0?-.08:.08)},{passive:false});viewport.addEventListener('pointerdown',e=>{drag=true;last=[e.clientX,e.clientY];viewport.setPointerCapture(e.pointerId)});viewport.addEventListener('pointermove',e=>{if(!drag)return;tx+=e.clientX-last[0];ty+=e.clientY-last[1];last=[e.clientX,e.clientY];apply()});viewport.addEventListener('pointerup',()=>drag=false);viewport.addEventListener('dblclick',resetView);
let lastPhase='',lastState=null,micState='off',captionTimer=null,statusSwap=0;
const activity=document.createElement('div');activity.id='activity';activity.className='activity-pill';activity.dataset.phase='idle';activity.innerHTML='<span class="activity-orbit"></span><span class="activity-copy"><span class="activity-kicker" id="activityKicker">Estado del profesor</span><span class="activity-label" id="activityLabel">Esperando tu pregunta</span></span>';viewport.appendChild(activity);
function transitionActivity(s){let label=$('#activityLabel'),pill=$('#activity'),next=(s.phase_label||'Procesando')+(s.status&&s.status!==s.phase_label?' · '+s.status:'');pill.dataset.phase=s.phase||'idle';$('#activityKicker').textContent=s.busy?'TeachAI está trabajando':'Estado del profesor';if(label.textContent===next)return;let ticket=++statusSwap;label.classList.remove('swap');void label.offsetWidth;if(ticket===statusSwap){label.textContent=next;label.classList.add('swap')}}
function escapeHtml(s){let d=document.createElement('div');d.textContent=s;return d.innerHTML}
function updateAudio(level,voiced,status=micState){micState=status;let bars=[...document.querySelectorAll('#meter i')],n=Math.round(level*bars.length);bars.forEach((b,i)=>b.style.setProperty('--level',i<n?(.25+Math.random()*.75):.08));let labels={off:'Micrófono inactivo',loading:'Cargando Whisper y abriendo micrófono…',active:voiced?'Voz detectada':'Escuchando en tiempo real…',error:'Error de micrófono'};$('#mictext').textContent=labels[status]||labels.off;$('#micbutton').className=status==='active'?'active':status==='loading'?'loading':'';$('#micbutton').textContent=status==='active'?'● Escuchando':status==='loading'?'Preparando…':'Micrófono'}
function loadBoard(rev){if(rev===seq)return;seq=rev;let img=$('#board');img.onerror=()=>{$('#status').textContent='No pude cargar la pizarra · reintentando…'};img.src='/api/board.png?t='+seq+'&token='+encodeURIComponent(TOKEN)}
function renderState(s){lastState=s;$('#status').textContent='● '+s.phase_label+' · '+s.status;if(lastPhase&&lastPhase!==s.phase)toast(s.phase_label);lastPhase=s.phase;document.documentElement.style.setProperty('--progress',Math.min(100,Math.max(5,s.progress))+'%');$('#log').innerHTML=s.logs.slice(-30).reverse().map(x=>`<div class="event"><time>${x.time} · ${(x.phase||'idle').toUpperCase()}</time>${escapeHtml(x.text)}</div>`).join('');let skills=Object.entries(s.mastery||{}).sort((a,b)=>b[1].mastery-a[1].mastery).slice(0,12);$('#skills').innerHTML=skills.length?skills.map(([k,v])=>`<div class="skill"><b>${escapeHtml(k)}</b><span>${Math.round(v.mastery*100)}%</span><div class="rail"><i style="--v:${Math.round(v.mastery*100)}%"></i></div></div>`).join(''):'<div style="color:var(--muted)">Aún no hay evidencia de dominio.</div>';updateAudio(s.mic_level,s.mic_voiced,s.mic_status);if(s.mic_error)$('#mictext').textContent='Error: '+s.mic_error;let m=$('#micselect');if(document.activeElement!==m){let current=String(s.microphone_device??'default');m.innerHTML='<option value="default">Predeterminado</option>'+s.microphones.map(x=>`<option value="${x.id}">${escapeHtml(x.name)}</option>`).join('');m.value=current}$('#provider').value=s.provider;$('#tts').value=s.tts_engine;loadBoard(s.board_revision)}
function showCaption(data){let box=$('#captions');clearTimeout(captionTimer);if(data.event==='start'){box.innerHTML='';data.text.split(/\s+/).forEach((word,i)=>{let span=document.createElement('span');span.textContent=word;span.style.animationDelay=Math.min(i*38,1800)+'ms';box.appendChild(span)});box.classList.remove('leaving');requestAnimationFrame(()=>box.classList.add('show'))}else{captionTimer=setTimeout(()=>{box.classList.add('leaving');box.classList.remove('show');setTimeout(()=>{if(!box.classList.contains('show'))box.innerHTML=''},650)},3000)}}
function toggleMic(){act(micState==='active'||micState==='loading'?'stop_voice':'voice')}function setMic(device){act('set_microphone',{device})}function clearBoard(){if(confirm('¿Limpiar la pizarra actual? Puedes usar Deshacer después.'))act('clear')}
async function initial(){try{let s=await api('/api/state');transitionActivity(s);renderState(s)}catch(e){$('#status').textContent='Interfaz desconectada: '+e.message}}initial();
function connectEvents(){let source=new EventSource('/api/events?token='+encodeURIComponent(TOKEN));source.addEventListener('state',e=>{let s=JSON.parse(e.data);transitionActivity(s);renderState(s)});source.addEventListener('board',e=>loadBoard(JSON.parse(e.data).revision));source.addEventListener('audio',e=>{let a=JSON.parse(e.data);updateAudio(a.level,a.voiced,a.status)});source.addEventListener('caption',e=>showCaption(JSON.parse(e.data)));source.onerror=()=>{$('#status').textContent='Reconectando canal en tiempo real…'}}connectEvents();
</script></body></html>'''


class ModernTeachAIApp:
    """Shell WebView2 premium; Python sigue siendo el único dueño del estado."""

    def __init__(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import secrets
        self.config=load_config();self.cancel=threading.Event();self.running=True
        self.prompt_queue: queue.Queue[str]=queue.Queue();self.busy=threading.Event()
        self.model_download_cancel=threading.Event();self.agent: TeacherAgent|None=None
        self.logs: deque[dict[str,str]]=deque(maxlen=500);self.status="Preparado"
        self.phase="idle";self.phase_label="Esperando";self.phase_history:deque[dict[str,Any]]=deque(maxlen=120)
        self.mic_level=0.0;self.mic_voiced=False;self.board_revision=1;self.token=secrets.token_urlsafe(24)
        self.mic_status="off";self.mic_error="";self.mic_requested=False;self.last_audio_publish=0.0
        self.last_frame_publish=0.0;self.event_clients:set[queue.Queue[dict[str,Any]]]=set();self.event_lock=threading.Lock()
        self.paint=NativeBoardController(self.config,self.cancel,self.log)
        self.paint.on_change=self._board_changed
        self.paint.on_frame=self._board_frame
        self.speech=SpeechIO(self.config,self.on_voice_text,self.log,self.on_audio_level,self.on_caption)
        self.discord=DiscordClassroomIntegration(self.config,self.log,self.submit_external,self.paint.screenshot_data_url)
        self.speech.audio_sink=self.discord.play_audio
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*_args:Any)->None: pass
            def authorized(self)->bool:
                from urllib.parse import urlparse,parse_qs
                query=parse_qs(urlparse(self.path).query)
                return self.headers.get("X-TeachAI-Token")==owner.token or query.get("token",[""])[0]==owner.token
            def reply(self,status:int,body:bytes,mime:str)->None:
                self.send_response(status);self.send_header("Content-Type",mime);self.send_header("Content-Length",str(len(body)))
                self.send_header("Cache-Control","no-store");self.end_headers();self.wfile.write(body)
            def do_GET(self)->None:
                from urllib.parse import urlparse
                path=urlparse(self.path).path
                if path=="/": self.reply(200,MODERN_UI_HTML.replace("__TOKEN__",owner.token).encode(),"text/html; charset=utf-8");return
                if not self.authorized(): self.reply(403,b"forbidden","text/plain");return
                if path=="/api/state": self.reply(200,json.dumps(owner.state(),ensure_ascii=False).encode(),"application/json");return
                if path=="/api/events":
                    self.send_response(200);self.send_header("Content-Type","text/event-stream; charset=utf-8")
                    self.send_header("Cache-Control","no-cache");self.send_header("Connection","keep-alive");self.end_headers()
                    events:queue.Queue[dict[str,Any]]=queue.Queue(maxsize=200)
                    with owner.event_lock:owner.event_clients.add(events)
                    try:
                        owner._write_sse(self,"state",owner.state())
                        while owner.running:
                            try:item=events.get(timeout=12);owner._write_sse(self,item["event"],item["data"])
                            except queue.Empty:owner._write_sse(self,"ping",{"at":time.time()})
                    except (BrokenPipeError,ConnectionResetError,OSError):pass
                    finally:
                        with owner.event_lock:owner.event_clients.discard(events)
                    return
                if path=="/api/board.png":
                    with owner.paint.lock:
                        stream=io.BytesIO();owner.paint.image.save(stream,"PNG",optimize=True)
                    self.reply(200,stream.getvalue(),"image/png");return
                self.reply(404,b"not found","text/plain")
            def do_POST(self)->None:
                if not self.authorized(): self.reply(403,b"forbidden","text/plain");return
                try:
                    size=min(20000,int(self.headers.get("Content-Length","0")));payload=json.loads(self.rfile.read(size) or b"{}")
                    result=owner.action(payload);self.reply(200,json.dumps(result,ensure_ascii=False).encode(),"application/json")
                except Exception as exc:self.reply(400,json.dumps({"error":str(exc)},ensure_ascii=False).encode(),"application/json")
        self.server=ThreadingHTTPServer(("127.0.0.1",0),Handler);self.server.daemon_threads=True;self.port=self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        threading.Thread(target=self.worker,daemon=True).start()
        self.log(f"{APP_NAME} {APP_VERSION} — interfaz WebView2 preparada")

    def _board_changed(self)->None:
        self.board_revision+=1;self.emit("board",{"revision":self.board_revision});self.discord.publish_snapshot()
    def _board_frame(self)->None:
        """Publica el trazo en curso a 20 FPS sin inundar Discord ni serializar cada píxel."""
        now=time.monotonic()
        if now-self.last_frame_publish>=.05:
            self.last_frame_publish=now
            self.board_revision+=1
            self.emit("board",{"revision":self.board_revision})
    @staticmethod
    def _write_sse(handler:Any,event:str,data:Any)->None:
        payload=json.dumps(data,ensure_ascii=False,separators=(",",":"))
        handler.wfile.write(f"event: {event}\ndata: {payload}\n\n".encode("utf-8"));handler.wfile.flush()
    def emit(self,event:str,data:Any)->None:
        item={"event":event,"data":data}
        with self.event_lock:clients=list(self.event_clients)
        for client in clients:
            try:client.put_nowait(item)
            except queue.Full:
                try:client.get_nowait();client.put_nowait(item)
                except (queue.Empty,queue.Full):pass
    def log(self,text:str)->None:
        value=str(text);lower=value.lower();phase=None;label=None
        if "fuentes verific" in lower or "research_verified" in lower:phase,label="research","Investigando fuentes"
        elif "plan visual" in lower or "plan aprobado" in lower:phase,label="plan","Planificando la clase"
        elif lower.startswith("acción:"):phase,label="draw","Componiendo la pizarra"
        elif "guion oral" in lower or "está preparando la explicación" in lower:phase,label="speak","Explicando"
        elif "monitor" in lower or "estado nativo" in lower or "auditor" in lower:phase,label="verify","Verificando el resultado"
        elif "turno terminado" in lower:phase,label="idle","Esperando tu pregunta"
        elif "interrump" in lower:phase,label="interrupted","Interrumpido"
        elif "error" in lower:phase,label="error","Necesita atención"
        if phase and phase!=self.phase:
            self.phase,self.phase_label=phase,label or phase
            self.phase_history.append({"at":time.time(),"phase":phase,"label":self.phase_label})
        self.status=value;self.logs.append({"time":time.strftime("%H:%M:%S"),"text":value,"phase":self.phase})
        if hasattr(self,"speech"):self.emit("state",self.state())
    def model_progress(self,event:dict[str,Any])->None:
        detail=str(event.get("detail") or event.get("message") or event.get("event") or "Preparando modelo")
        self.log(detail)
    def on_audio_level(self,level:float,voiced:bool)->None:
        self.mic_level=float(level);self.mic_voiced=bool(voiced);now=time.monotonic()
        if now-self.last_audio_publish>=.04:
            self.last_audio_publish=now;self.emit("audio",{"level":self.mic_level,"voiced":self.mic_voiced,"status":self.mic_status})
    def on_caption(self,event:str,text:str)->None:
        self.emit("caption",{"event":event,"text":text,"at":time.time()})
    def on_voice_text(self,text:str)->None:
        self.log("Tú: "+text);self.interrupt(True);self.prompt_queue.put(text)
    def submit_external(self,text:str)->None:self.log("Discord: "+text);self.interrupt(True);self.prompt_queue.put(text)
    def interrupt(self,silent:bool=False)->None:
        self.cancel.set();self.speech.interrupt()
        if not silent:self.log("Interrumpido; puedes continuar cuando quieras")
    def state(self)->dict[str,Any]:
        mastery=(self.agent.learner.get("concepts",{}) if self.agent else {})
        objects=len(self.paint.objects);progress=min(96,5+objects*2)
        return {"status":self.status,"logs":list(self.logs),"mic_level":self.mic_level,
                "mic_voiced":self.mic_voiced,"mic_active":self.mic_status=="active","mic_status":self.mic_status,
                "mic_error":self.mic_error,"microphone_device":self.config.get("microphone_device"),
                "microphones":[{"id":i,"name":name} for i,name in SpeechIO.input_devices()],
                "provider":self.config.get("provider","auto"),"tts_engine":self.config.get("tts_engine","kokoro"),
                "board_revision":self.board_revision,"mastery":mastery,"progress":progress,"busy":self.busy.is_set(),
                "phase":self.phase,"phase_label":self.phase_label,"phase_history":list(self.phase_history)}
    def action(self,payload:dict[str,Any])->dict[str,Any]:
        action=str(payload.get("action",""))
        if action=="prompt":
            text=str(payload.get("text","")).strip()
            if not text:raise ValueError("La pregunta está vacía")
            self.log("Tú: "+text);self.interrupt(True);self.prompt_queue.put(text);return {"message":"Pregunta enviada"}
        if action=="interrupt":self.interrupt();return {"message":"Turno interrumpido"}
        if action=="voice":
            self.mic_requested=True
            if self.mic_status not in {"loading","active"}:threading.Thread(target=self._start_voice,daemon=True).start()
            return {"message":"Cargando escucha" if self.mic_status!="active" else "El micrófono ya está escuchando"}
        if action=="stop_voice":
            self.mic_requested=False;self.speech.stop_listening();self.mic_status="off";self.mic_error="";self.emit("state",self.state())
            return {"message":"Micrófono desactivado"}
        if action=="set_microphone":
            raw=payload.get("device");self.config["microphone_device"]=None if raw in {None,"","default"} else int(raw);save_config(self.config)
            if self.mic_status in {"active","loading"}:
                self.mic_requested=True;self.speech.stop_listening();self.mic_status="off";threading.Thread(target=self._start_voice,daemon=True).start()
            return {"message":"Micrófono seleccionado"}
        if action=="set_provider":
            value=str(payload.get("value","auto"));
            if value not in {"auto","lmstudio","deepseek"}:raise ValueError("Proveedor no válido")
            self.config["provider"]=value;save_config(self.config);self.agent=None;return {"message":"Proveedor actualizado"}
        if action=="set_tts":
            value=str(payload.get("value","kokoro"));
            if value not in {"kokoro","edge"}:raise ValueError("Motor de voz no válido")
            self.config["tts_engine"]=value;save_config(self.config);return {"message":"Voz actualizada"}
        if action=="undo":self.paint.undo();return {"message":"Acción deshecha"}
        if action=="redo":self.paint.redo();return {"message":"Acción rehecha"}
        if action=="clear":self.paint.clear();return {"message":"Pizarra limpia"}
        if action=="grid":self.paint.toggle_grid();return {"message":"Cuadrícula alternada"}
        if action=="export":
            result=self.paint.export_board(str(payload.get("format","png")),time.strftime("clase-%Y%m%d-%H%M%S"));return {"message":"Clase exportada","result":result}
        if action=="prepare_model":
            def prepare()->None:
                try:
                    if self.agent is None:self.agent=TeacherAgent(self.config,self.paint,self.speech,self.cancel,self.log,self.model_progress,self.model_download_cancel)
                    self.agent.lmstudio.ensure_model();self.log("Modelo local preparado")
                except Exception as exc:self.log(f"ERROR LM Studio: {exc}")
            threading.Thread(target=prepare,daemon=True).start();return {"message":"Preparando modelo local"}
        if action=="snapshot":
            result=self.paint.save_snapshot(time.strftime("clase-%Y%m%d-%H%M%S"));return {"message":"Snapshot guardado","result":result}
        raise ValueError("Acción desconocida")
    def _start_voice(self)->None:
        self.mic_status="loading";self.mic_error="";self.emit("state",self.state())
        try:
            self.speech.start()
            if not self.mic_requested:
                self.speech.stop_listening();self.mic_status="off"
            else:self.mic_status="active"
            self.emit("state",self.state())
        except Exception as exc:
            self.mic_status="error";self.mic_error=str(exc);self.log(f"No pude activar la voz: {exc}");self.emit("state",self.state())
    def worker(self)->None:
        while self.running:
            try:text=self.prompt_queue.get(timeout=.2)
            except queue.Empty:continue
            while True:
                try:text=self.prompt_queue.get_nowait()
                except queue.Empty:break
            try:
                self.cancel.clear();self.paint.attach()
                if self.agent is None:self.agent=TeacherAgent(self.config,self.paint,self.speech,self.cancel,self.log,self.model_progress,self.model_download_cancel)
                self.busy.set();self.agent.respond(text);self.log("Turno terminado — sigo escuchando")
            except CancelledDrawing:self.log("Turno cortado por la interrupción")
            except Exception as exc:self.log(f"ERROR: {exc}")
            finally:self.busy.clear()
    def close(self)->None:
        self.running=False;self.cancel.set();self.model_download_cancel.set();self.discord.stop();self.speech.close();self.paint.close();self.server.shutdown()
    def run(self)->None:
        url=f"http://127.0.0.1:{self.port}/"
        try:
            import webview
            window=webview.create_window(f"TeachAI {APP_VERSION}",url,width=1440,height=900,min_size=(900,620),background_color="#08080A")
            window.events.closed+=lambda:self.close();webview.start(debug=False,private_mode=False)
        except Exception as exc:
            import webbrowser
            self.log(f"WebView2 no disponible ({exc}); abro la interfaz en el navegador")
            webbrowser.open(url)
            try:
                while self.running:time.sleep(.25)
            except KeyboardInterrupt:self.close()


class PaintProfessorApp:
    def __init__(self):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk = tk, ttk
        self.config = load_config()
        self.root = tk.Tk()
        self.root.configure(bg="#0B1020")
        style = ttk.Style(self.root)
        try: style.theme_use("clam")
        except Exception: pass
        style.configure(".", background="#0B1020", foreground="#E8EDF7",
                        fieldbackground="#11182A", bordercolor="#25314A",
                        lightcolor="#25314A", darkcolor="#25314A",
                        font=("Segoe UI",10))
        style.configure("TFrame",background="#0B1020")
        style.configure("TLabel",background="#0B1020",foreground="#AAB7CE")
        style.configure("TButton",background="#17213A",foreground="#F7F9FC",
                        padding=(12,8),borderwidth=0)
        style.map("TButton",background=[("active","#243252"),("pressed","#30436B")])
        style.configure("TCombobox",fieldbackground="#11182A",background="#17213A",
                        foreground="#F7F9FC",arrowcolor="#8EABFF",padding=6)
        style.map("TCombobox",fieldbackground=[("readonly","#11182A")],
                  foreground=[("readonly","#F7F9FC")])
        style.configure("TEntry",fieldbackground="#11182A",foreground="#F7F9FC",
                        insertcolor="#F7F9FC",padding=8)
        self.main_thread_id = threading.get_ident()
        self.ui_queue: queue.Queue[Callable[[], None]] = queue.Queue()
        self.root.title(f"{APP_NAME} {APP_VERSION}")
        self.root.geometry("980x720")
        self.root.minsize(820, 600)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(40, self._drain_ui)
        self.cancel = threading.Event()
        self.prompt_queue: queue.Queue[str] = queue.Queue()
        self.running = True
        self.busy = threading.Event()
        self.model_download_cancel = threading.Event()
        self.model_prepare_running = threading.Event()
        self.model_progress_ui: dict[str, Any] = {}

        self.status = tk.StringVar(value="Preparado")
        ttk.Label(self.root, text="PAINT PROFESSOR", font=("Segoe UI Semibold", 20)).pack(anchor="w", padx=22, pady=(18,2))
        ttk.Label(self.root, text="DeepSeek o LM Studio enseñan en una pizarra vectorial propia (Paint queda como modo legado)", font=("Segoe UI", 10)).pack(anchor="w", padx=23)
        bar = ttk.Frame(self.root)
        bar.pack(fill="x", padx=20, pady=(14,6))
        self.connect_button = ttk.Button(bar, text="Abrir pizarra", command=self.connect)
        self.connect_button.pack(side="left", padx=3)
        self.calibrate_button = ttk.Button(bar, text="Calibrar Paint", command=self.calibrate)
        self.calibrate_button.pack(side="left", padx=3)
        ttk.Button(bar, text="Probar dibujo", command=self.test_drawing).pack(side="left", padx=3)
        ttk.Button(bar, text="Activar voz", command=self.start_voice).pack(side="left", padx=3)
        ttk.Button(bar, text="INTERRUMPIR", command=self.interrupt).pack(side="right", padx=3)

        board_bar = ttk.Frame(self.root)
        board_bar.pack(fill="x", padx=23, pady=(0,7))
        ttk.Label(board_bar, text="Pizarra:").pack(side="left")
        board_names = {"native":"Nativa omnisciente (recomendada)", "paint":"Microsoft Paint (legado)"}
        self.board_var = tk.StringVar(value=board_names.get(self.config.get("board_backend", "native"), board_names["native"]))
        self.board_box = ttk.Combobox(board_bar, textvariable=self.board_var, state="readonly", width=31,
                                      values=list(board_names.values()))
        self.board_box.pack(side="left", padx=(7,8))
        self.board_box.bind("<<ComboboxSelected>>", self.change_board_backend)
        ttk.Label(board_bar, text="Sin ratón del sistema · estado vectorial exacto",
                  foreground="#16805A").pack(side="left")

        audio_bar = ttk.Frame(self.root)
        audio_bar.pack(fill="x", padx=23, pady=(0,7))
        ttk.Label(audio_bar, text="Micrófono:").pack(side="left")
        self.microphone_var = tk.StringVar(value="Predeterminado del sistema")
        self.microphone_box = ttk.Combobox(audio_bar, textvariable=self.microphone_var,
                                           state="readonly", width=34)
        self.microphone_box.pack(side="left", padx=(7,8))
        self.microphone_box.bind("<<ComboboxSelected>>", self.change_microphone)
        ttk.Button(audio_bar, text="↻", width=3, command=self.refresh_microphones).pack(side="left")
        self.mic_meter = tk.Canvas(audio_bar, width=150, height=14, highlightthickness=0,
                                   bg="#DDE3EA")
        self.mic_meter.pack(side="left", padx=(10,7))
        self.mic_state = tk.StringVar(value="Micrófono inactivo")
        ttk.Label(audio_bar, textvariable=self.mic_state).pack(side="left")
        self.mic_level_target = 0.0
        self.mic_level_display = 0.0
        self.mic_voiced = False

        voice_bar = ttk.Frame(self.root)
        voice_bar.pack(fill="x", padx=23, pady=(0,8))
        ttk.Label(voice_bar,text="Voz profesora:").pack(side="left")
        voice_names={"kokoro":"Kokoro local — humana","edge":"Edge TTS — respaldo"}
        self.tts_var=tk.StringVar(value=voice_names.get(self.config.get("tts_engine","kokoro"),voice_names["kokoro"]))
        self.tts_box=ttk.Combobox(voice_bar,textvariable=self.tts_var,state="readonly",width=25,
                                  values=list(voice_names.values()))
        self.tts_box.pack(side="left",padx=(7,8))
        self.tts_box.bind("<<ComboboxSelected>>",self.change_tts_engine)
        ttk.Button(voice_bar,text="Probar voz",command=self.test_voice).pack(side="left")
        ttk.Label(voice_bar,text="Kokoro se descarga una vez y después funciona sin conexión",
                  foreground="#5B6472").pack(side="left",padx=(10,0))

        discord_bar=ttk.Frame(self.root)
        discord_bar.pack(fill="x",padx=23,pady=(0,9))
        ttk.Label(discord_bar,text="Discord:").pack(side="left")
        self.discord_token_var=tk.StringVar()
        self.discord_token_entry=ttk.Entry(discord_bar,textvariable=self.discord_token_var,show="•",width=25)
        self.discord_token_entry.pack(side="left",padx=(7,6))
        ttk.Button(discord_bar,text="Guardar token",command=self.save_discord_token).pack(side="left")
        self.discord_read_var=tk.BooleanVar(value=bool(self.config.get("discord_read_chat",False)))
        ttk.Checkbutton(discord_bar,text="Leer solo aula activa",variable=self.discord_read_var,
                        command=self.change_discord_read_chat).pack(side="left",padx=(9,5))
        ttk.Button(discord_bar,text="Conectar",command=self.connect_discord).pack(side="left",padx=3)
        ttk.Button(discord_bar,text="Desconectar",command=self.disconnect_discord).pack(side="left",padx=3)
        self.discord_state=tk.StringVar(value="Desconectado")
        ttk.Label(discord_bar,textvariable=self.discord_state).pack(side="left",padx=(8,0))

        provider_bar = ttk.Frame(self.root)
        provider_bar.pack(fill="x", padx=23, pady=(0,10))
        ttk.Label(provider_bar, text="Cerebro:").pack(side="left")
        provider_names = {"auto":"Auto (DeepSeek → local)", "deepseek":"Solo DeepSeek", "lmstudio":"Solo LM Studio"}
        current_provider = provider_names.get(self.config.get("provider", "auto"), provider_names["auto"])
        self.provider_var = tk.StringVar(value=current_provider)
        self.provider_box = ttk.Combobox(
            provider_bar, textvariable=self.provider_var, state="readonly", width=25,
            values=list(provider_names.values()),
        )
        self.provider_box.pack(side="left", padx=(7,8))
        self.provider_box.bind("<<ComboboxSelected>>", self.change_provider)
        ttk.Button(provider_bar, text="Preparar LM Studio", command=self.prepare_lmstudio).pack(side="left")
        ttk.Button(provider_bar, text="Estado de pizarra", command=self.debug_paint).pack(side="left", padx=(7,0))

        self.log_box = tk.Text(self.root, height=16, wrap="word", state="disabled", font=("Cascadia Mono", 10),
                               bg="#080D19",fg="#B8C7E3",insertbackground="#FFFFFF",
                               relief="flat",padx=14,pady=12)
        self.log_box.pack(fill="both", expand=True, padx=22, pady=(0,10))
        entrybar = ttk.Frame(self.root)
        entrybar.pack(fill="x", padx=22, pady=(0,8))
        self.entry = ttk.Entry(entrybar, font=("Segoe UI", 11))
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Return>", lambda _e: self.submit())
        ttk.Button(entrybar, text="Enviar", command=self.submit).pack(side="left", padx=(8,0))
        ttk.Label(self.root, textvariable=self.status).pack(anchor="w", padx=23, pady=(0,12))

        self.paint = self._create_board_controller()
        self._update_board_controls()
        self.speech = SpeechIO(self.config, self.on_voice_text, self.log, self.on_audio_level)
        self.discord = DiscordClassroomIntegration(
            self.config,self.log,self.on_discord_question,self.paint.screenshot_data_url)
        self.speech.audio_sink=self.discord.play_audio
        if hasattr(self.paint,"on_change"):
            self.paint.on_change=self.discord.publish_snapshot
        self.refresh_microphones()
        self.root.after(16, self._animate_audio_meter)
        self.agent: TeacherAgent | None = None
        threading.Thread(target=self.worker, daemon=True).start()
        self.log(f"{APP_NAME} {APP_VERSION} — archivo activo: {Path(__file__).resolve()}")
        self.log("1) Abre la pizarra  2) activa voz o escribe una pregunta  3) interrumpe cuando quieras")
        if not os.getenv("DEEPSEEK_API_KEY") and self.config.get("provider") != "lmstudio":
            from tkinter import simpledialog
            key = simpledialog.askstring("DeepSeek (opcional)", "Pega tu DEEPSEEK_API_KEY. Puedes cancelar para trabajar solo con LM Studio:", show="*", parent=self.root)
            if key and key.strip():
                os.environ["DEEPSEEK_API_KEY"] = key.strip()

    def ui(self, fn: Callable[[], None]) -> None:
        if threading.get_ident() == self.main_thread_id:
            fn()
        else:
            self.ui_queue.put(fn)

    def _drain_ui(self) -> None:
        try:
            while True:
                self.ui_queue.get_nowait()()
        except queue.Empty:
            pass
        if self.running:
            self.root.after(40, self._drain_ui)

    def log(self, text: str) -> None:
        def write():
            stamp = time.strftime("%H:%M:%S")
            self.log_box.configure(state="normal")
            self.log_box.insert("end", f"[{stamp}] {text}\n")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
            self.status.set(text)
        self.ui(write)

    def model_progress(self, event: dict[str, Any]) -> None:
        snapshot = dict(event)
        self.ui(lambda: self._render_model_progress(snapshot))

    def _create_model_progress_window(self) -> None:
        tk = self.tk
        bg, panel, text, muted = "#0B1020", "#11182A", "#F7F9FC", "#92A0B8"
        accent = "#4F7CFF"
        top = tk.Toplevel(self.root)
        top.title("Preparando modelo local — Paint Professor")
        top.geometry("720x500")
        top.minsize(590, 410)
        top.transient(self.root)
        top.configure(bg=bg)

        body = tk.Frame(top, bg=bg, padx=28, pady=24)
        body.pack(fill="both", expand=True)
        title = tk.StringVar(value="Comprobando LM Studio")
        detail = tk.StringVar(value="Un momento…")
        percent = tk.StringVar(value="PREPARANDO")
        elapsed = tk.StringVar(value="00:00")
        tk.Label(body, text="TEACHAI  •  MODELO LOCAL", bg=bg, fg="#7898FF",
                 font=("Segoe UI Semibold", 9)).pack(anchor="w")
        tk.Label(body, textvariable=title, bg=bg, fg=text,
                 font=("Segoe UI Semibold", 22)).pack(anchor="w", pady=(7, 3))
        tk.Label(body, textvariable=detail, bg=bg, fg=muted, justify="left",
                 wraplength=650, font=("Segoe UI", 10)).pack(anchor="w", pady=(0, 18))

        stages_frame = tk.Frame(body, bg=bg)
        stages_frame.pack(fill="x", pady=(0, 14))
        stage_labels: list[Any] = []
        for stage_name in ("MOTOR", "DESCARGA", "VERIFICACIÓN", "GPU", "SERVIDOR"):
            label = tk.Label(stages_frame, text="○  " + stage_name, bg=bg, fg="#526078",
                             font=("Segoe UI Semibold", 9), anchor="w")
            label.pack(side="left", expand=True, fill="x")
            stage_labels.append(label)

        progress_canvas = tk.Canvas(body, height=18, bg=bg, highlightthickness=0)
        progress_canvas.pack(fill="x")
        meta = tk.Frame(body, bg=bg)
        meta.pack(fill="x", pady=(7, 14))
        tk.Label(meta, textvariable=percent, bg=bg, fg="#AFC2FF",
                 font=("Segoe UI Semibold", 9)).pack(side="left")
        tk.Label(meta, textvariable=elapsed, bg=bg, fg=muted,
                 font=("Consolas", 9)).pack(side="right")

        console_header = tk.Frame(body, bg=bg)
        console_header.pack(fill="x")
        tk.Label(console_header, text="ACTIVIDAD", bg=bg, fg=muted,
                 font=("Segoe UI Semibold", 9)).pack(side="left")
        details_button = tk.Button(
            console_header, text="Ocultar detalles", relief="flat", bd=0,
            bg=bg, activebackground=bg, fg="#8EABFF", activeforeground="#BCD0FF",
            font=("Segoe UI", 9), cursor="hand2",
        )
        details_button.pack(side="right")
        output_frame = tk.Frame(body, bg=panel, padx=1, pady=1)
        output = tk.Text(
            output_frame, height=10, wrap="word", state="disabled",
            font=("Cascadia Mono", 9), bg=panel, fg="#BCC7D9",
            insertbackground=text, relief="flat", padx=12, pady=10,
        )
        output.pack(fill="both", expand=True)
        output_frame.pack(fill="both", expand=True, pady=(7, 0))

        def toggle_details() -> None:
            state = self.model_progress_ui
            if not state:
                return
            state["details_visible"] = not state.get("details_visible", False)
            if state["details_visible"]:
                output_frame.pack(fill="both", expand=True, pady=(7, 0))
                details_button.configure(text="Ocultar detalles")
                top.geometry("720x500")
            else:
                output_frame.pack_forget()
                details_button.configure(text="Mostrar detalles")
                top.geometry("720x350")

        details_button.configure(command=toggle_details)
        buttons = tk.Frame(body, bg=bg)
        buttons.pack(fill="x", pady=(12, 0))
        close_button = tk.Button(
            buttons, text="Ocultar", command=top.withdraw, relief="flat", bd=0,
            bg="#1A2438", fg=text, activebackground="#26334B", activeforeground=text,
            padx=18, pady=8, cursor="hand2",
        )
        close_button.pack(side="right")
        cancel_button = tk.Button(
            buttons, text="Cancelar", command=self.cancel_model_download, relief="flat", bd=0,
            bg="#2A1720", fg="#FF9AAE", activebackground="#40202C", activeforeground="#FFD0D9",
            padx=18, pady=8, cursor="hand2",
        )
        cancel_button.pack(side="right", padx=(0, 8))

        self.model_progress_ui = {
            "top": top, "title": title, "detail": detail, "percent": percent,
            "elapsed": elapsed, "progress_canvas": progress_canvas, "output": output,
            "cancel": cancel_button, "close": close_button, "active": True,
            "determinate": False, "current": 0.0, "target": 0.0, "phase": 0.0,
            "accent": accent, "started": time.monotonic(), "stage": -1,
            "stage_labels": stage_labels, "details_visible": True,
        }
        top.protocol("WM_DELETE_WINDOW", self._close_or_cancel_model_progress)
        self._animate_model_progress()

    @staticmethod
    def _rounded_canvas_rect(canvas: Any, x1: float, y1: float, x2: float, y2: float,
                             radius: float, **kwargs: Any) -> None:
        radius = max(1.0, min(radius, (x2-x1)/2, (y2-y1)/2))
        points = [
            x1+radius,y1, x2-radius,y1, x2,y1, x2,y1+radius,
            x2,y2-radius, x2,y2, x2-radius,y2, x1+radius,y2,
            x1,y2, x1,y2-radius, x1,y1+radius, x1,y1,
        ]
        canvas.create_polygon(points, smooth=True, splinesteps=24, **kwargs)

    def _set_model_stage(self, index: int, failed: bool = False) -> None:
        state = self.model_progress_ui
        if not state:
            return
        state["stage"] = index
        for position, label in enumerate(state["stage_labels"]):
            if failed and position == index:
                label.configure(text="×  " + label.cget("text")[3:], fg="#FF6885")
            elif position < index:
                label.configure(text="✓  " + label.cget("text")[3:], fg="#53D69D")
            elif position == index:
                label.configure(text="●  " + label.cget("text")[3:], fg="#7FA1FF")
            else:
                label.configure(text="○  " + label.cget("text")[3:], fg="#526078")

    def _animate_model_progress(self) -> None:
        state = self.model_progress_ui
        if not state:
            return
        top = state.get("top")
        try:
            if not top or not top.winfo_exists():
                return
            canvas = state["progress_canvas"]
            width = max(20, canvas.winfo_width())
            height = max(10, canvas.winfo_height())
            canvas.delete("all")
            self._rounded_canvas_rect(canvas, 1, 3, width-1, height-3, 7, fill="#1C2740", outline="")
            if state.get("determinate"):
                current = float(state.get("current", 0.0))
                target = float(state.get("target", 0.0))
                current += (target-current) * .11
                if abs(target-current) < .04:
                    current = target
                state["current"] = current
                filled = 1 + (width-2) * current / 100.0
                if filled > 4:
                    self._rounded_canvas_rect(
                        canvas, 1, 3, max(4, filled), height-3, 7,
                        fill=state.get("accent", "#4F7CFF"), outline="",
                    )
            else:
                state["phase"] = (float(state.get("phase", 0.0)) + .012) % 1.0
                segment = max(70, width * .22)
                travel = width + segment
                start = state["phase"] * travel - segment
                segment_start, segment_end = max(1, start), min(width-1, start+segment)
                if segment_end > segment_start + 2:
                    self._rounded_canvas_rect(
                        canvas, segment_start, 3, segment_end, height-3, 7,
                        fill=state.get("accent", "#4F7CFF"), outline="",
                    )
            elapsed = max(0, int(time.monotonic() - float(state.get("started", time.monotonic()))))
            state["elapsed"].set(f"{elapsed//60:02d}:{elapsed%60:02d}")
            top.after(16, self._animate_model_progress)
        except Exception:
            return

    def _close_or_cancel_model_progress(self) -> None:
        state = self.model_progress_ui
        if not state:
            return
        if state.get("active"):
            self.cancel_model_download()
        else:
            state["top"].destroy()
            self.model_progress_ui = {}

    def cancel_model_download(self) -> None:
        self.model_download_cancel.set()
        state = self.model_progress_ui
        if state:
            state["title"].set("Cancelando descarga…")
            state["detail"].set("Cerrando el proceso de LM Studio de forma segura.")
            state["cancel"].configure(state="disabled")

    def _append_model_output(self, value: str) -> None:
        state = self.model_progress_ui
        if not state or not value:
            return
        box = state["output"]
        box.configure(state="normal")
        box.insert("end", value.rstrip() + "\n")
        try:
            lines = int(box.index("end-1c").split(".")[0])
            if lines > 220:
                box.delete("1.0", f"{lines - 200}.0")
        except Exception:
            pass
        box.see("end")
        box.configure(state="disabled")

    def _render_model_progress(self, event: dict[str, Any]) -> None:
        kind = str(event.get("event", "stage"))
        state = self.model_progress_ui
        if not state or not state.get("top") or not state["top"].winfo_exists():
            self._create_model_progress_window()
            state = self.model_progress_ui
        elif kind in {"ready", "error", "cancelled"}:
            state["top"].deiconify()
            state["top"].lift()

        if kind == "download_start":
            self.model_download_cancel.clear()
            state["active"] = True
            state["cancel"].configure(state="normal")
            state["close"].configure(text="Ocultar", command=state["top"].withdraw)
            state["determinate"] = False
            state["current"] = 0.0
            state["target"] = 0.0
            state["accent"] = "#4F7CFF"
            state["started"] = time.monotonic()
            state["percent"].set("DESCARGANDO")
            self._set_model_stage(1)
            self._append_model_output(f"Modelo: {event.get('model', '')}")

        if kind in {"stage", "download_start"}:
            if event.get("title"):
                title_value = str(event["title"])
                state["title"].set(title_value)
                normalized = normalize_text(title_value)
                if "verific" in normalized:
                    self._set_model_stage(2)
                elif "gpu" in normalized or "cargando modelo" in normalized:
                    self._set_model_stage(3)
                elif "servidor" in normalized:
                    self._set_model_stage(4)
                elif "llmster" in normalized or "motor" in normalized or "lm studio" in normalized:
                    self._set_model_stage(0)
            if event.get("detail"):
                state["detail"].set(str(event["detail"]))
                self._append_model_output(str(event["detail"]))
        elif kind == "output":
            value = str(event.get("text", ""))
            # Los frames de progreso actualizan la etiqueta y la barra suave,
            # pero no se acumulan en el cuadro ACTIVIDAD.
            if not event.get("transient"):
                self._append_model_output(value)
            if value:
                state["detail"].set(value[-180:])
            progress_value = event.get("percent")
            if isinstance(progress_value, (int, float)):
                state["determinate"] = True
                progress_value = max(0.0, min(100.0, float(progress_value)))
                state["target"] = progress_value
                state["percent"].set(f"{progress_value:.1f} %")
        elif kind == "download_verified":
            state["determinate"] = True
            state["target"] = 100.0
            state["title"].set("Descarga verificada")
            state["detail"].set("El modelo aparece realmente en el catálogo local. Ahora lo cargaré.")
            self._set_model_stage(2)
            self._append_model_output("✓ Descarga comprobada en el catálogo de LM Studio")
        elif kind == "ready":
            state["determinate"] = True
            state["target"] = 100.0
            state["accent"] = "#35C98B"
            state["percent"].set("100 %")
            state["title"].set("Modelo preparado para preguntas")
            state["detail"].set(str(event.get("detail", "LM Studio está listo.")))
            state["active"] = False
            self._set_model_stage(5)
            state["cancel"].configure(state="disabled")
            state["close"].configure(text="Cerrar", command=self._close_model_progress)
            self._append_model_output(f"✓ LISTO: {event.get('model', '')}")
        elif kind in {"error", "cancelled"}:
            state["active"] = False
            state["cancel"].configure(state="disabled")
            state["close"].configure(text="Cerrar", command=self._close_model_progress)
            if kind == "cancelled":
                state["determinate"] = True
                state["target"] = state.get("current", 0.0)
                state["accent"] = "#718096"
                state["percent"].set("CANCELADO")
                state["title"].set("Descarga cancelada")
                state["detail"].set("El modelo no se marcará como preparado.")
                self._append_model_output("Descarga cancelada por el usuario")
            else:
                message = str(event.get("message", "Error desconocido"))
                state["determinate"] = True
                state["target"] = state.get("current", 0.0)
                state["accent"] = "#F05270"
                state["percent"].set("ERROR")
                self._set_model_stage(max(0, int(state.get("stage", 0))), failed=True)
                state["title"].set("No se pudo preparar el modelo")
                state["detail"].set(message)
                self._append_model_output("ERROR: " + message)

    def _close_model_progress(self) -> None:
        state = self.model_progress_ui
        if state:
            try:
                state["top"].destroy()
            except Exception:
                pass
        self.model_progress_ui = {}

    def _create_board_controller(self) -> Any:
        backend = str(self.config.get("board_backend", "native")).lower()
        if backend == "paint":
            return PaintController(self.config, self.cancel, self.log)
        self.config["board_backend"] = "native"
        return NativeBoardController(self.config, self.cancel, self.log, self.root, self.ui)

    def refresh_microphones(self) -> None:
        devices = SpeechIO.input_devices()
        self.microphone_devices = {"Predeterminado del sistema": None}
        for index, name in devices:
            label = f"{index}: {name}"
            self.microphone_devices[label] = index
        self.microphone_box.configure(values=list(self.microphone_devices))
        selected = self.config.get("microphone_device")
        match = next((label for label, index in self.microphone_devices.items() if index == selected),
                     "Predeterminado del sistema")
        self.microphone_var.set(match)

    def change_microphone(self, _event: Any = None) -> None:
        selected = self.microphone_devices.get(self.microphone_var.get())
        was_active = bool(self.speech.listener_thread and self.speech.listener_thread.is_alive())
        self.interrupt(silent=True)
        self.speech.close()
        self.config["microphone_device"] = selected
        save_config(self.config)
        self.speech = SpeechIO(self.config, self.on_voice_text, self.log, self.on_audio_level)
        self.agent = None
        self.log(f"Micrófono seleccionado: {self.microphone_var.get()}")
        if was_active:
            self.start_voice()

    def change_tts_engine(self, _event: Any = None) -> None:
        self.config["tts_engine"] = "kokoro" if self.tts_var.get().startswith("Kokoro") else "edge"
        save_config(self.config)
        self.speech.config = self.config
        self.speech.kokoro_failed = False
        self.log(f"Motor de voz cambiado a: {self.tts_var.get()}")

    def save_discord_token(self) -> None:
        token=self.discord_token_var.get().strip()
        if not token:
            self.message("Discord","Pega primero el token del bot.",error=True);return
        try:
            DiscordClassroomIntegration.save_token(token)
            self.discord_token_var.set("")
            self.log("Token de Discord guardado de forma segura en Windows")
        except Exception as exc:
            self.message("Discord",f"No pude guardar el token de forma segura: {exc}",error=True)

    def change_discord_read_chat(self) -> None:
        self.config["discord_read_chat"]=bool(self.discord_read_var.get())
        save_config(self.config)
        self.log("Lectura del chat del aula " + ("activada" if self.discord_read_var.get() else "desactivada"))
        if self.discord.connected.is_set():
            self.log("Reconecta Discord para aplicar el cambio de intent")

    def connect_discord(self) -> None:
        token=self.discord_token_var.get().strip() or DiscordClassroomIntegration.load_token()
        try:
            if self.discord_token_var.get().strip(): DiscordClassroomIntegration.save_token(token)
            self.discord.start(token);self.discord_state.set("Conectando…")
        except Exception as exc: self.message("Discord",str(exc),error=True)

    def disconnect_discord(self) -> None:
        self.discord.stop();self.discord_state.set("Desconectado")

    def on_discord_question(self, text: str) -> None:
        self.log(text)
        self.interrupt(silent=True)
        self.prompt_queue.put(text)

    def test_voice(self) -> None:
        threading.Thread(target=self.speech.speak,args=(
            "Hola. Soy la profesora de TeachAI. Esta es una prueba de mi voz y del ritmo de explicación.",
            self.cancel),daemon=True).start()

    def on_audio_level(self, level: float, voiced: bool) -> None:
        self.mic_level_target = level
        self.mic_voiced = voiced

    def _animate_audio_meter(self) -> None:
        if not self.running:
            return
        self.mic_level_display += (self.mic_level_target-self.mic_level_display)*.22
        if self.mic_level_target < self.mic_level_display:
            self.mic_level_target *= .91
        canvas = self.mic_meter
        try:
            width, height = max(10, canvas.winfo_width()), max(8, canvas.winfo_height())
            canvas.delete("all")
            canvas.create_rectangle(0,0,width,height,fill="#DDE3EA",outline="")
            fill = "#16A34A" if self.mic_voiced else "#4F7CFF"
            canvas.create_rectangle(0,0,width*self.mic_level_display,height,fill=fill,outline="")
            active = bool(self.speech.listener_thread and self.speech.listener_thread.is_alive())
            self.mic_state.set("Voz detectada" if active and self.mic_voiced else
                               "Escuchando" if active else "Micrófono inactivo")
            if hasattr(self,"discord"):
                self.discord_state.set("Conectado" if self.discord.connected.is_set() else
                                       "Conectando…" if self.discord.thread and self.discord.thread.is_alive()
                                       else "Desconectado")
        except Exception:
            pass
        self.root.after(16, self._animate_audio_meter)

    def _update_board_controls(self) -> None:
        native = getattr(self.paint, "backend_name", "paint") == "native"
        self.connect_button.configure(text="Abrir pizarra" if native else "Conectar Paint")
        self.calibrate_button.configure(state="disabled" if native else "normal")

    def change_board_backend(self, _event: Any = None) -> None:
        reverse = {
            "Nativa omnisciente (recomendada)": "native",
            "Microsoft Paint (legado)": "paint",
        }
        selected = reverse.get(self.board_var.get(), "native")
        if selected == self.config.get("board_backend", "native"):
            return
        self.interrupt(silent=True)
        old = self.paint
        try:
            old.close()
        except Exception:
            pass
        self.config["board_backend"] = selected
        save_config(self.config)
        self.paint = self._create_board_controller()
        if hasattr(self,"discord"):
            self.discord.snapshot_provider=self.paint.screenshot_data_url
            if hasattr(self.paint,"on_change"):
                self.paint.on_change=self.discord.publish_snapshot
        self.agent = None
        self._update_board_controls()
        self.paint.attach()
        if selected == "native":
            self.log("Modo nativo activado: estado exacto, historial atómico y cero ratón del sistema")
        else:
            self.log("Microsoft Paint activado en modo legado; puede requerir calibración y permisos")

    def connect(self) -> None:
        try: self.paint.attach()
        except Exception as exc: self.message("Error", str(exc), error=True)

    def test_drawing(self) -> None:
        def draw() -> None:
            try:
                self.cancel.clear()
                self.paint.attach()
                self.paint.write("OK", 35, 35, 42, 160, "#1F2937", 2, 1.25)
                self.paint.rectangle(25, 22, 135, 75, "#1F2937", 2, 1.25)
                self.log("Prueba terminada: debes ver OK dentro de un recuadro")
            except Exception as exc:
                self.log(f"ERROR en prueba de dibujo: {exc}")
        threading.Thread(target=draw, daemon=True).start()

    def debug_paint(self) -> None:
        def inspect() -> None:
            try:
                path = self.paint.diagnose()
                os.startfile(path)
            except Exception as exc:
                self.log(f"ERROR al obtener el estado de la pizarra: {exc}")
        threading.Thread(target=inspect, daemon=True).start()

    def change_provider(self, _event: Any = None) -> None:
        reverse = {
            "Auto (DeepSeek → local)": "auto",
            "Solo DeepSeek": "deepseek",
            "Solo LM Studio": "lmstudio",
        }
        self.interrupt(silent=True)
        self.config["provider"] = reverse[self.provider_var.get()]
        save_config(self.config)
        self.agent = None
        self.log(f"Modo cambiado a: {self.provider_var.get()}")

    def prepare_lmstudio(self) -> None:
        if self.model_prepare_running.is_set() or LMStudioManager.PROCESS_LOCK.locked():
            self.model_progress({"event":"stage", "title":"Preparación en curso", "detail":"La descarga o carga ya está ejecutándose."})
            return

        def prepare() -> None:
            self.model_prepare_running.set()
            try:
                self.model_progress({
                    "event":"stage", "title":"Comprobando LM Studio",
                    "detail":"Buscando un modelo válido antes de descargar nada…",
                })
                model = LMStudioManager(
                    self.config, self.log, self.model_progress, self.model_download_cancel,
                ).ensure_model()
                self.log(f"LM Studio listo para Paint Professor: {model}")
            except ModelDownloadCancelled:
                self.log("Descarga del modelo cancelada")
            except Exception as exc:
                self.log(f"ERROR LM Studio: {exc}")
            finally:
                self.model_prepare_running.clear()
        threading.Thread(target=prepare, daemon=True).start()

    def message(self, title: str, text: str, error: bool = False) -> None:
        from tkinter import messagebox
        (messagebox.showerror if error else messagebox.showinfo)(title, text, parent=self.root)

    def capture_relative(self, label: str, delay: int = 3) -> list[float]:
        self.message("Calibración", f"Pulsa Aceptar y coloca el cursor sobre:\n\n{label}\n\nSe capturará en {delay} segundos. NO hagas clic.")
        for remaining in range(delay, 0, -1):
            self.log(f"Capturando {label}: {remaining}…")
            self.root.update()
            time.sleep(1)
        x, y = WinInput.cursor()
        l, t, r, b = self.paint.rect()
        return [(x-l)/(r-l), (y-t)/(b-t)]

    def capture_hover(self, label: str, delay: int = 3) -> list[float]:
        for remaining in range(delay, 0, -1):
            self.log(f"Pon el cursor sobre {label}: {remaining}…")
            self.root.update()
            time.sleep(1)
        x, y = WinInput.cursor()
        l, t, r, b = self.paint.rect()
        return [(x-l)/(r-l), (y-t)/(b-t)]

    def calibrate(self) -> None:
        from tkinter import messagebox
        if getattr(self.paint, "backend_name", "paint") == "native":
            self.paint.attach()
            self.log("La pizarra nativa conoce exactamente su espacio 1000×700; no necesita calibración")
            self.message("Calibración innecesaria", "La pizarra nativa ya conoce sus coordenadas y objetos con precisión exacta.")
            return
        try:
            self.paint.attach()
            self.root.attributes("-topmost", True)
            tl = self.capture_relative("la esquina SUPERIOR IZQUIERDA del lienzo blanco")
            br = self.capture_relative("la esquina INFERIOR DERECHA del lienzo blanco")
            self.config["paint"]["canvas"] = [tl[0], tl[1], br[0], br[1]]
            full = messagebox.askyesno("Calibración", "¿Hacemos calibración completa de controles?\n\nRecomendada si la detección automática no funciona.", parent=self.root)
            if full:
                self.paint.ensure_toolbar()
                controls = self.config["paint"].setdefault("controls", {})
                controls["tool_brush"] = self.capture_relative("el botón LÁPIZ o PINCEL ya visible")
                controls["tool_eraser"] = self.capture_relative("el botón BORRADOR")
                controls["edit_colors"] = self.capture_relative("el botón EDITAR COLORES")
                self.paint.palette_control_available = None
                self.paint.color_control_available = None
                self.paint._palette_warning_shown = False
                self.paint._color_warning_shown = False
                self.paint.collapse_toolbar()
            save_config(self.config)
            self.log("Calibración guardada")
            self.message("Listo", "Paint quedó calibrado.")
        except Exception as exc:
            self.message("Error de calibración", str(exc), error=True)
        finally:
            self.root.attributes("-topmost", False)

    def start_voice(self) -> None:
        if self.speech.listener_thread and self.speech.listener_thread.is_alive():
            self.log("La voz ya está activa")
            return
        threading.Thread(target=self._voice_boot, daemon=True).start()

    def _voice_boot(self) -> None:
        try: self.speech.start()
        except Exception as exc: self.log(f"No pude activar la voz: {exc}")

    def on_voice_text(self, text: str) -> None:
        self.log("Tú: " + text)
        self.interrupt(silent=True)
        self.prompt_queue.put(text)

    def submit(self) -> None:
        text = self.entry.get().strip()
        if not text: return
        self.entry.delete(0, "end")
        self.log("Tú: " + text)
        self.interrupt(silent=True)
        self.prompt_queue.put(text)

    def interrupt(self, silent: bool = False) -> None:
        self.cancel.set()
        self.speech.interrupt()
        if not silent: self.log("Interrumpido; puedes hablar o escribir otra pregunta")

    def worker(self) -> None:
        while self.running:
            try: text = self.prompt_queue.get(timeout=.2)
            except queue.Empty: continue
            # Conserva solo el mensaje más reciente si llegaron varios mientras se interrumpía.
            while True:
                try: text = self.prompt_queue.get_nowait()
                except queue.Empty: break
            try:
                self.cancel.clear()
                self.paint.attach()
                if self.agent is None:
                    self.agent = TeacherAgent(
                        self.config, self.paint, self.speech, self.cancel, self.log,
                        self.model_progress, self.model_download_cancel,
                    )
                agent = self.agent
                self.busy.set()
                agent.respond(text)
                self.log("Turno terminado — sigo escuchando")
            except CancelledDrawing:
                self.log("Turno cortado por la interrupción")
            except Exception as exc:
                self.log(f"ERROR: {exc}")
            finally:
                self.busy.clear()

    def close(self) -> None:
        self.running = False
        self.cancel.set()
        self.model_download_cancel.set()
        if hasattr(self,"discord"):
            self.discord.stop()
        self.speech.close()
        self.paint.close()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def self_check() -> int:
    print(f"{APP_NAME}: {APP_VERSION}")
    print(f"Archivo: {Path(__file__).resolve()}")
    print(f"Python: {sys.version.split()[0]}")
    print(f"Sistema: {sys.platform}")
    missing = dependency_error()
    print("Dependencias:", "OK" if not missing else "FALTAN " + ", ".join(missing))
    checked_config = load_config()
    print("Proveedor:", checked_config.get("provider", "auto"))
    print("Pizarra:", checked_config.get("board_backend", "native"))
    print("DeepSeek API key:", "OK" if os.getenv("DEEPSEEK_API_KEY") else "NO DEFINIDA (se usará LM Studio)")
    print("LM Studio CLI:", shutil.which("lms") or "NO DETECTADO")
    print("Configuración:", CONFIG_PATH)
    return 0 if not missing else 1


def is_elevated() -> bool:
    if not IS_WINDOWS:
        return True
    try:
        return bool(shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_elevated() -> bool:
    """Relanza este mismo Python mediante UAC conservando todos los argumentos."""
    if not IS_WINDOWS or is_elevated():
        return True
    script = Path(__file__).resolve()
    parameters = subprocess.list2cmdline([str(script), *sys.argv[1:]])
    try:
        result = shell32.ShellExecuteW(
            None,
            "runas",
            str(Path(sys.executable).resolve()),
            parameters,
            str(script.parent),
            1,  # SW_SHOWNORMAL
        )
        return int(result) > 32
    except Exception:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {APP_VERSION}")
    parser.add_argument("--install", action="store_true", help="Instala todas las dependencias")
    parser.add_argument("--install-inner", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--check", action="store_true", help="Comprueba el entorno sin iniciar")
    parser.add_argument("--legacy-ui", action="store_true", help="Usa temporalmente la interfaz Tk anterior")
    args = parser.parse_args()
    boot_config = load_config()
    boot_backend = str(boot_config.get("board_backend", "native")).lower()
    # La pizarra nativa no toca Paint ni inyecta entrada, por tanto jamás necesita UAC.
    require_admin = (
        boot_backend == "paint"
        and os.getenv("PAINT_PROFESSOR_REQUIRE_ADMIN", "1" if boot_config.get("require_admin", True) else "0") != "0"
    )
    if IS_WINDOWS and require_admin and not is_elevated():
        print("Paint Professor solicita permisos de administrador mediante UAC…")
        if relaunch_elevated():
            return 0
        print(
            "No se concedieron permisos de administrador. "
            "Acepta la ventana de UAC o define PAINT_PROFESSOR_REQUIRE_ADMIN=0.",
            file=sys.stderr,
        )
        return 5
    if args.install or args.install_inner:
        install_dependencies(inner=args.install_inner)
        return 0
    if args.check:
        return self_check()
    if not IS_WINDOWS:
        print("Paint Professor controla Microsoft Paint y debe ejecutarse en Windows 10/11.", file=sys.stderr)
        return 2
    missing = dependency_error()
    if missing:
        print("Faltan dependencias: " + ", ".join(missing), file=sys.stderr)
        print("Ejecuta: py paint_professor.py --install", file=sys.stderr)
        return 2
    (PaintProfessorApp() if args.legacy_ui else ModernTeachAIApp()).run()
    return 0


if __name__ == "__main__":
    try:
        exit_code = main()
    except Exception:
        # Los procesos elevados mediante ShellExecute no conservan la consola
        # del padre. Guardamos y mostramos cualquier fallo para que nunca vuelva
        # a aparecer una ventana que se cierra sin explicar el motivo.
        detail = traceback.format_exc()
        crash_path = app_data_dir() / "paint-professor-crash.log"
        try:
            crash_path.write_text(detail, encoding="utf-8", errors="replace")
        except OSError:
            pass
        print(detail, file=sys.stderr)
        if IS_WINDOWS:
            try:
                user32.MessageBoxW(
                    None,
                    f"Paint Professor encontró un error.\n\n{detail[-2400:]}\n\n"
                    f"Diagnóstico guardado en:\n{crash_path}",
                    f"{APP_NAME} {APP_VERSION} — Error",
                    0x10,
                )
            except Exception:
                pass
        exit_code = 1
    raise SystemExit(exit_code)
