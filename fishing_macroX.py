"""
Fishing Bot GPO - v6

Novidades desta versao:
- "Minhas Frutas": lista MANUAL (digitada por voce) das frutas que ja
  possui. O bot le o NOME da fruta pescada (OCR numa area calibravel) e
  compara com essa lista (comparacao tolerante a erro de OCR via
  difflib). Se bater -> descarta com Backspace ANTES de tentar guardar.
  Se nao bater -> clica no ponto calibrado do prompt "Store Fruit" e adiciona esse
  nome na lista automaticamente (evita duplicar da proxima vez).
  Se a lista estiver vazia, clica no ponto calibrado e le a mensagem de erro
  depois.
- Guardar fruta usa um ponto de clique calibrado na area cliente; descartar
  fruta continua usando Backspace somente no fluxo de duplicata confirmado.
- Selecao de isca: agora que a isca fica numa lista empilhada clicavel
  (nao um hotbar), voce marca a posicao de cada opcao na tela (um clique
  por isca, com nome). Quando o contador de isca zera, o bot clica
  automaticamente na isca de fallback que voce escolher. Se nada for
  configurado, ele pausa e avisa (comportamento anterior, mais seguro).

IMPORTANTE: automacao viola os Termos de Uso do Roblox. Use por sua conta e risco.

Dependencias:
    pip install mss numpy opencv-python pydirectinput keyboard pytesseract
Alem disso, o PROGRAMA Tesseract-OCR precisa estar instalado:
    https://github.com/UB-Mannheim/tesseract/wiki
"""

import json
import logging
import os
import time
import difflib
import threading
import queue
import tempfile
import ctypes
import ctypes.wintypes
import sys
from enum import Enum
from dataclasses import dataclass
from collections import deque
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

import mss
import numpy as np
import cv2
import pydirectinput
import keyboard


def configurar_dpi_processo():
    """Define DPI por monitor antes de criar Tk; usa fallback em Windows antigo."""
    if sys.platform != "win32":
        return False
    try:
        return bool(ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)))
    except (AttributeError, OSError):
        try:
            return bool(ctypes.windll.user32.SetProcessDPIAware())
        except (AttributeError, OSError):
            return False


configurar_dpi_processo()

try:
    import pytesseract
    CAMINHO_TESSERACT = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    if os.path.exists(CAMINHO_TESSERACT):
        pytesseract.pytesseract.tesseract_cmd = CAMINHO_TESSERACT
    OCR_DISPONIVEL = True
except ImportError:
    OCR_DISPONIVEL = False

pydirectinput.FAILSAFE = False

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
BASELINE_PATH = os.path.join(APP_DIR, "slot_baseline.png")
HOTKEY_PARAR = "f8"
FRASES_DUPLICATA_PADRAO = "you can only store one of each fruit"
LOG_PATH = os.path.join(APP_DIR, "fishing_bot.log")

# Adotado dos exemplos: estados, logging local e métricas tornam falhas
# operacionais observáveis sem adicionar integrações externas. Combate,
# exploração, webhooks/Discord e qualquer automação fora da pesca ficam
# explicitamente fora do escopo.
LOGGER = logging.getLogger("fishing_bot")
LOGGER.setLevel(logging.INFO)
_LOGGING_CONFIGURED = False
if not LOGGER.handlers:
    LOGGER.addHandler(logging.StreamHandler())


def _ensure_logging_configured():
    """Tenta habilitar arquivo; mantém console explícito se o diretório não permitir."""
    global _LOGGING_CONFIGURED
    if _LOGGING_CONFIGURED:
        return
    try:
        handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    except OSError as exc:
        LOGGER.warning("Nao foi possivel abrir log em %s: %s; usando console.", LOG_PATH, exc)
    else:
        LOGGER.addHandler(handler)
    _LOGGING_CONFIGURED = True


class BotState(Enum):
    PARADO = "parado"
    RODANDO = "rodando"
    PAUSADO_FRUTA = "pausado - fruta/OCR"
    PAUSADO_ISCA = "pausado - sem isca"
    ERRO = "erro"
    ENCERRANDO = "encerrando"


class BotMetrics:
    """Métricas de captura e alterações de slot, thread-safe e sem GUI."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._inicio = clock()
        self._frames = 0
        self._eventos_slot = 0
        self._lock = threading.Lock()

    def frame(self, quantidade=1):
        with self._lock:
            self._frames += quantidade

    def fruta_detectada(self):
        """Alias compatível: o evento medido é uma alteração do slot."""
        with self._lock:
            self._eventos_slot += 1

    def snapshot(self):
        with self._lock:
            decorrido = max(0.001, self._clock() - self._inicio)
            return {
                "frames": self._frames,
                "eventos_slot": self._eventos_slot,
                "eventos_fruta": self._eventos_slot,
                "fps": self._frames / decorrido,
                "eventos_por_hora": self._eventos_slot * 3600.0 / decorrido,
            }


class InputController:
    """Abstração mínima testável sobre pydirectinput; mantém o escopo da pesca."""

    def click(self, *args, **kwargs):
        return pydirectinput.click(*args, **kwargs)

    def mouse_down(self):
        return pydirectinput.mouseDown()

    def mouse_up(self):
        return pydirectinput.mouseUp()

    def press(self, tecla):
        return pydirectinput.press(tecla)


class ScreenCapture:
    """Abstração de captura para permitir testes sem abrir uma tela real."""

    def __enter__(self):
        self._captura = mss.mss()
        return self._captura

    def __exit__(self, exc_type, exc_value, traceback):
        self._captura.close()


@dataclass(frozen=True)
class ScreenPoint:
    x: int
    y: int


@dataclass(frozen=True)
class WindowRect:
    left: int
    top: int
    width: int
    height: int

    @property
    def right(self):
        return self.left + self.width

    @property
    def bottom(self):
        return self.top + self.height

    def contains(self, point):
        return self.left <= point.x < self.right and self.top <= point.y < self.bottom


@dataclass(frozen=True)
class WindowSnapshot:
    hwnd: int
    title: str
    client: WindowRect
    monitor: WindowRect
    dpi: int
    foreground: bool
    minimized: bool


def point_from_pct(area, client):
    """Converte uma região percentual da área cliente em coordenadas de tela."""
    if not validar_regiao_pct(area):
        raise ValueError("Regiao percentual invalida.")
    return WindowRect(
        client.left + int(area["x1_pct"] * client.width),
        client.top + int(area["y1_pct"] * client.height),
        max(1, int((area["x2_pct"] - area["x1_pct"]) * client.width)),
        max(1, int((area["y2_pct"] - area["y1_pct"]) * client.height)),
    )


def ponto_dentro_cliente(point, client):
    return client.contains(point)


def validar_ponto_pct(ponto):
    if not isinstance(ponto, dict):
        return False
    try:
        x, y = float(ponto["x_pct"]), float(ponto["y_pct"])
    except (KeyError, TypeError, ValueError):
        return False
    return 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0


def screen_point_from_pct(ponto, client):
    """Converte um ponto calibrado relativo ao cliente em coordenada de tela."""
    if not validar_ponto_pct(ponto):
        raise ValueError("Ponto percentual invalido.")
    resultado = ScreenPoint(
        client.left + int(float(ponto["x_pct"]) * client.width),
        client.top + int(float(ponto["y_pct"]) * client.height),
    )
    if not ponto_dentro_cliente(resultado, client):
        raise ValueError("Ponto fora da area cliente.")
    return resultado


class WindowTracker:
    """Localiza janela/cliente com APIs documentadas; nunca lê memória ou injeta."""

    def __init__(self, title_hint="Roblox", capture_factory=ScreenCapture):
        self.title_hint = title_hint.lower()
        self.capture_factory = capture_factory
        self._user32 = None
        if sys.platform == "win32":
            self._user32 = ctypes.windll.user32
            try:
                self._user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
            except (AttributeError, OSError):
                try:
                    self._user32.SetProcessDPIAware()
                except (AttributeError, OSError):
                    pass

    def _window_candidates(self):
        if self._user32 is None:
            return []
        candidates = []
        enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def callback(hwnd, _):
            length = self._user32.GetWindowTextLengthW(hwnd)
            if length:
                buffer = ctypes.create_unicode_buffer(length + 1)
                self._user32.GetWindowTextW(hwnd, buffer, length + 1)
                if self.title_hint in buffer.value.lower():
                    candidates.append((int(hwnd), buffer.value))
            return True

        self._user32.EnumWindows(enum_proc(callback), 0)
        return candidates

    def locate(self):
        candidates = self._window_candidates()
        if not candidates:
            return None
        foreground = int(self._user32.GetForegroundWindow())
        hwnd, title = next(((h, t) for h, t in candidates if h == foreground), candidates[0])
        rect = ctypes.wintypes.RECT()
        if not self._user32.GetClientRect(hwnd, ctypes.byref(rect)):
            return None
        origin = ctypes.wintypes.POINT(0, 0)
        if not self._user32.ClientToScreen(hwnd, ctypes.byref(origin)):
            return None
        client = WindowRect(origin.x, origin.y, rect.right - rect.left, rect.bottom - rect.top)
        monitor_handle = self._user32.MonitorFromWindow(hwnd, 2)
        monitor = WindowRect(client.left, client.top, client.width, client.height)
        monitor_info = _monitor_info()
        if monitor_handle and self._user32.GetMonitorInfoW(
                monitor_handle, ctypes.byref(monitor_info)):
            monitor = WindowRect(
                monitor_info.rcMonitor.left, monitor_info.rcMonitor.top,
                monitor_info.rcMonitor.right - monitor_info.rcMonitor.left,
                monitor_info.rcMonitor.bottom - monitor_info.rcMonitor.top,
            )
        try:
            dpi = int(self._user32.GetDpiForWindow(hwnd))
        except AttributeError:
            dpi = 96
        minimized = bool(self._user32.IsIconic(hwnd))
        return WindowSnapshot(hwnd, title, client, monitor, dpi, hwnd == foreground, minimized)

    def fallback(self):
        with self.capture_factory() as sct:
            monitor = monitor_principal(sct)
        rect = WindowRect(monitor["left"], monitor["top"], monitor["width"], monitor["height"])
        return WindowSnapshot(0, "monitor fallback", rect, rect, 96, True, False)

    def snapshot(self):
        return self.locate() or self.fallback()


def _monitor_info():
    info = ctypes.wintypes.MONITORINFO()
    info.cbSize = ctypes.sizeof(info)
    return info


def configuracao_padrao():
    return {
        "frutas_possuidas": [],
        "posicoes_isca": [],
        "fruta": {
            "limiar_diff": 20.0,
            "cooldown_segundos": 8.0,
            "limiar_nome": 0.6,
            "frases_duplicata": FRASES_DUPLICATA_PADRAO,
        },
        "loop": {"margem_pixels": 4, "intervalo_segundos": 0.02},
    }


def validar_regiao_pct(area):
    if not isinstance(area, dict):
        return False
    try:
        valores = [float(area[chave]) for chave in ("x1_pct", "y1_pct", "x2_pct", "y2_pct")]
    except (KeyError, TypeError, ValueError):
        return False
    return all(0.0 <= valor <= 1.0 for valor in valores) and valores[0] < valores[2] and valores[1] < valores[3]


def monitor_principal(sct):
    monitores = sct.monitors
    if len(monitores) < 2:
        raise RuntimeError("Nenhum monitor físico disponível para calibracao.")
    monitor = monitores[1]
    return {
        "left": int(monitor.get("left", 0)),
        "top": int(monitor.get("top", 0)),
        "width": int(monitor["width"]),
        "height": int(monitor["height"]),
    }


def normalizar_config(cfg):
    """Aplica defaults e rejeita somente estruturas inválidas, sem perder dados válidos."""
    if not isinstance(cfg, dict):
        cfg = {}
    resultado = configuracao_padrao()
    resultado.update(cfg)
    if not isinstance(resultado.get("frutas_possuidas"), list):
        resultado["frutas_possuidas"] = []
    if not isinstance(resultado.get("posicoes_isca"), list):
        resultado["posicoes_isca"] = []
    fruta = configuracao_padrao()["fruta"]
    fruta.update(resultado.get("fruta") if isinstance(resultado.get("fruta"), dict) else {})
    fruta.pop("tecla_guardar", None)
    resultado["fruta"] = fruta
    loop = configuracao_padrao()["loop"]
    loop.update(resultado.get("loop") if isinstance(resultado.get("loop"), dict) else {})
    resultado["loop"] = loop
    for chave in ("regiao_pesca", "regiao_slot_fruta", "regiao_texto_erro",
                  "regiao_contador_isca", "regiao_nome_fruta"):
        if chave in resultado and not validar_regiao_pct(resultado[chave]):
            resultado.pop(chave)
    return resultado


def migrar_config_janela(cfg, snapshot):
    """Migra regiões antigas de monitor para cliente sem alterar itens desconhecidos."""
    cfg = normalizar_config(cfg)
    referencia = cfg.get("referencia_coordenadas", "monitor")
    if referencia == "cliente":
        return cfg, False
    monitor = snapshot.monitor
    cliente = snapshot.client
    for chave in ("regiao_pesca", "regiao_slot_fruta", "regiao_texto_erro",
                  "regiao_contador_isca", "regiao_nome_fruta"):
        area = cfg.get(chave)
        if not validar_regiao_pct(area):
            continue
        tela = point_from_pct(area, monitor)
        inter_left = max(tela.left, cliente.left)
        inter_top = max(tela.top, cliente.top)
        inter_right = min(tela.right, cliente.right)
        inter_bottom = min(tela.bottom, cliente.bottom)
        if inter_left >= inter_right or inter_top >= inter_bottom:
            cfg.pop(chave, None)
            continue
        cfg[chave] = {
            "x1_pct": (inter_left - cliente.left) / cliente.width,
            "y1_pct": (inter_top - cliente.top) / cliente.height,
            "x2_pct": (inter_right - cliente.left) / cliente.width,
            "y2_pct": (inter_bottom - cliente.top) / cliente.height,
        }
    cfg["referencia_coordenadas"] = "cliente"
    cfg["janela_calibracao"] = {
        "width": cliente.width, "height": cliente.height, "dpi": snapshot.dpi,
    }
    return cfg, True


def validar_contexto_calibracao(cfg, snapshot, tolerancia=0.20):
    contexto = cfg.get("janela_calibracao", {})
    if not contexto:
        return True
    largura = contexto.get("width", snapshot.client.width)
    largura_ok = abs(largura - snapshot.client.width) <= snapshot.client.width * tolerancia
    altura_ok = abs(contexto.get("height", snapshot.client.height) -
                    snapshot.client.height) <= snapshot.client.height * tolerancia
    dpi = contexto.get("dpi", snapshot.dpi)
    return largura_ok and altura_ok and abs(dpi - snapshot.dpi) <= 24


def janela_pronta(snapshot):
    return snapshot.hwnd != 0 and snapshot.foreground and not snapshot.minimized


def mesma_janela(a, b):
    return (
        a.hwnd == b.hwnd
        and a.client == b.client
        and a.monitor == b.monitor
        and a.dpi == b.dpi
    )


# ---------------------------------------------------------------------------
# RETANGULO ARRASTAVEL / REDIMENSIONAVEL
# ---------------------------------------------------------------------------

class RetanguloAjustavel:
    TAMANHO_ALCA = 9

    def __init__(self, canvas, x1, y1, x2, y2, cor, rotulo):
        self.canvas = canvas
        self.cor = cor
        self.rect = canvas.create_rectangle(x1, y1, x2, y2, outline=cor, width=5, fill="")
        self.texto = canvas.create_text(x1 + 6, y1 - 12, text=rotulo, fill=cor,
                                         anchor="w", font=("Segoe UI", 10, "bold"))
        self.alcas = {}
        for nome in ("nw", "ne", "sw", "se"):
            self.alcas[nome] = canvas.create_rectangle(0, 0, 0, 0, fill=cor, outline="white")
        self._atualizar_alcas()
        self._drag = {"x": 0, "y": 0}

        canvas.tag_bind(self.rect, "<ButtonPress-1>", self._iniciar_arrasto_corpo)
        canvas.tag_bind(self.rect, "<B1-Motion>", self._arrastar_corpo)
        for nome, alca in self.alcas.items():
            canvas.tag_bind(alca, "<ButtonPress-1>", self._iniciar_arrasto_corpo)
            canvas.tag_bind(alca, "<B1-Motion>", lambda e, n=nome: self._redimensionar(e, n))
            canvas.tag_raise(alca)
        canvas.tag_raise(self.texto)

    def coords(self):
        return self.canvas.coords(self.rect)

    def _atualizar_alcas(self):
        x1, y1, x2, y2 = self.coords()
        s = self.TAMANHO_ALCA
        posicoes = {"nw": (x1, y1), "ne": (x2, y1), "sw": (x1, y2), "se": (x2, y2)}
        for nome, (x, y) in posicoes.items():
            self.canvas.coords(self.alcas[nome], x - s, y - s, x + s, y + s)
        self.canvas.coords(self.texto, x1 + 6, y1 - 12)

    def _iniciar_arrasto_corpo(self, event):
        self._drag["x"] = event.x
        self._drag["y"] = event.y

    def _arrastar_corpo(self, event):
        dx = event.x - self._drag["x"]
        dy = event.y - self._drag["y"]
        self.canvas.move(self.rect, dx, dy)
        self.canvas.move(self.texto, dx, dy)
        self._drag["x"] = event.x
        self._drag["y"] = event.y
        self._atualizar_alcas()

    def _redimensionar(self, event, canto):
        x1, y1, x2, y2 = self.coords()
        if canto == "nw":
            x1, y1 = event.x, event.y
        elif canto == "ne":
            x2, y1 = event.x, event.y
        elif canto == "sw":
            x1, y2 = event.x, event.y
        elif canto == "se":
            x2, y2 = event.x, event.y
        self.canvas.coords(self.rect, x1, y1, x2, y2)
        self._atualizar_alcas()

    def coords_normalizadas_pct(self, largura_tela, altura_tela):
        x1, y1, x2, y2 = normalizar_retangulo(self.coords(), largura_tela, altura_tela)
        return {
            "x1_pct": x1 / largura_tela, "y1_pct": y1 / altura_tela,
            "x2_pct": x2 / largura_tela, "y2_pct": y2 / altura_tela,
        }


def normalizar_retangulo(coords, largura, altura):
    """Ordena e limita um retângulo ao canvas antes de persistir a calibração."""
    if largura <= 0 or altura <= 0 or len(coords) != 4:
        raise ValueError("Dimensoes do canvas invalidas.")
    x1, y1, x2, y2 = coords
    x1, x2 = sorted((x1, x2))
    y1, y2 = sorted((y1, y2))
    x1 = min(max(0, x1), largura - 1)
    y1 = min(max(0, y1), altura - 1)
    x2 = min(max(x1 + 1, x2), largura)
    y2 = min(max(y1 + 1, y2), altura)
    return x1, y1, x2, y2


COR_OVERLAY = "#101820"
ALPHA_OVERLAY = 0.28


class JanelaCalibracaoAreas:
    """5 retangulos: azul=barras de pesca, laranja=slot da fruta,
    ciano=area da mensagem de erro (duplicata), amarelo=contador de isca,
    rosa=nome da fruta pescada. O ponto de Store Fruit e calibrado em uma
    janela separada, relativo a area cliente.

    Overlay semi-transparente: voce ve o jogo ao vivo por tras da janela,
    enquanto os retangulos ficam desenhados por cima. Se ja existir
    calibracao salva, cada retangulo comeca EXATAMENTE onde estava -
    corrigir so um deles nao mexe nos outros."""

    def __init__(self, parent, ao_confirmar, cfg_existente=None):
        self.ao_confirmar = ao_confirmar
        cfg_existente = cfg_existente or {}

        snapshot = WindowTracker().snapshot()
        self.origem_x = snapshot.client.left
        self.origem_y = snapshot.client.top
        self.largura = snapshot.client.width
        self.altura = snapshot.client.height
        self.dpi = snapshot.dpi

        self.janela = tk.Toplevel(parent)
        self.janela.overrideredirect(True)
        self.janela.geometry(f"{self.largura}x{self.altura}+{self.origem_x}+{self.origem_y}")
        self.janela.attributes("-topmost", True)
        self.janela.attributes("-alpha", ALPHA_OVERLAY)
        self.janela.configure(bg=COR_OVERLAY)
        self.janela.lift()
        self.janela.focus_force()

        self.canvas = tk.Canvas(self.janela, width=self.largura, height=self.altura,
                                 highlightthickness=0, bg=COR_OVERLAY)
        self.canvas.pack(fill="both", expand=True)

        def posicao_inicial(chave_config, padrao_px):
            area = cfg_existente.get(chave_config)
            if area:
                return (area["x1_pct"] * self.largura, area["y1_pct"] * self.altura,
                         area["x2_pct"] * self.largura, area["y2_pct"] * self.altura)
            return padrao_px

        cx, cy = self.largura // 2, self.altura // 2

        x1, y1, x2, y2 = posicao_inicial("regiao_pesca", (cx - 150, cy - 150, cx + 150, cy + 150))
        self.ret_pesca = RetanguloAjustavel(self.canvas, x1, y1, x2, y2, "#2196F3", "PESCA (barras)")

        x1, y1, x2, y2 = posicao_inicial("regiao_slot_fruta",
                                          (cx - 50, self.altura - 130, cx + 20, self.altura - 60))
        self.ret_slot = RetanguloAjustavel(self.canvas, x1, y1, x2, y2, "#FF9800", "SLOT DA FRUTA")

        x1, y1, x2, y2 = posicao_inicial("regiao_texto_erro",
                                          (cx - 220, int(self.altura * 0.10), cx + 220, int(self.altura * 0.17)))
        self.ret_texto_erro = RetanguloAjustavel(
            self.canvas, x1, y1, x2, y2, "#00BCD4", "AREA da msg 'You can only store one of each fruit!'"
        )

        x1, y1, x2, y2 = posicao_inicial("regiao_contador_isca",
                                          (20, self.altura - 280, 220, self.altura - 220))
        self.ret_contador_isca = RetanguloAjustavel(self.canvas, x1, y1, x2, y2, "#FFEB3B",
                                                     "CONTADOR de isca (ex: x185)")

        x1, y1, x2, y2 = posicao_inicial("regiao_nome_fruta",
                                          (cx - 200, self.altura - 320, cx + 50, self.altura - 280))
        self.ret_nome_fruta = RetanguloAjustavel(self.canvas, x1, y1, x2, y2, "#E91E63", "NOME da fruta pescada")

        frame_botoes = tk.Frame(self.janela, bg="#101010")
        frame_botoes.place(relx=0.5, rely=0.98, anchor="s")
        tk.Label(frame_botoes, text="Areas visiveis sobre o jogo. Arraste/redimensione; ESC ou Cancelar sai sem salvar.",
                 bg="#101010", fg="white").pack(side="top", pady=(0, 4))
        tk.Button(frame_botoes, text="Confirmar", command=self._confirmar,
                  bg="#4CAF50", fg="white", padx=20, pady=8).pack(side="left", padx=5)
        tk.Button(frame_botoes, text="Cancelar", command=self.janela.destroy,
                  bg="#f44336", fg="white", padx=20, pady=8).pack(side="left", padx=5)

        self.janela.bind("<Escape>", lambda e: self.janela.destroy())
        self.janela.after(100, self._garantir_visibilidade)

    def _garantir_visibilidade(self):
        if self.janela.winfo_exists():
            self.janela.lift()
            self.janela.attributes("-topmost", True)

    def _confirmar(self):
        resolucao = (self.largura, self.altura)
        self.ao_confirmar(
            self.ret_pesca.coords_normalizadas_pct(*resolucao),
            self.ret_slot.coords_normalizadas_pct(*resolucao),
            self.ret_texto_erro.coords_normalizadas_pct(*resolucao),
            self.ret_contador_isca.coords_normalizadas_pct(*resolucao),
            self.ret_nome_fruta.coords_normalizadas_pct(*resolucao),
            resolucao,
        )
        self.janela.destroy()


class JanelaMarcarPonto:
    """Overlay TRANSPARENTE de tela cheia para marcar UM ponto (posicoes de
    isca). Sem print de fundo - o jogo aparece ao vivo por tras."""

    def __init__(self, parent, ao_confirmar,
                 texto="Clique no ponto desejado (ESC cancela)"):
        self.ao_confirmar = ao_confirmar
        snapshot = WindowTracker().snapshot()
        self.origem_x = snapshot.client.left
        self.origem_y = snapshot.client.top
        self.largura = snapshot.client.width
        self.altura = snapshot.client.height

        self.janela = tk.Toplevel(parent)
        self.janela.overrideredirect(True)
        self.janela.geometry(f"{self.largura}x{self.altura}+{self.origem_x}+{self.origem_y}")
        self.janela.attributes("-topmost", True)
        self.janela.attributes("-alpha", ALPHA_OVERLAY)
        self.janela.configure(bg=COR_OVERLAY)
        self.janela.lift()
        self.janela.focus_force()

        self.canvas = tk.Canvas(self.janela, width=self.largura, height=self.altura,
                                 highlightthickness=0, bg=COR_OVERLAY)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Button-1>", self._clicou)
        self.janela.bind("<Escape>", lambda e: self.janela.destroy())
        self.janela.after(100, self._garantir_visibilidade)

        tk.Label(self.janela, text=texto,
                 bg="black", fg="white", font=("Segoe UI", 12, "bold")).place(relx=0.5, rely=0.03, anchor="n")

    def _garantir_visibilidade(self):
        if self.janela.winfo_exists():
            self.janela.lift()
            self.janela.attributes("-topmost", True)

    def _clicou(self, event):
        x_pct = event.x / self.largura
        y_pct = event.y / self.altura
        self.janela.destroy()
        self.ao_confirmar(x_pct, y_pct)


# ---------------------------------------------------------------------------
# FUNCOES DE VISAO
# ---------------------------------------------------------------------------

def pegar_pontos(frame, largura, altura, titulo_janela):
    pontos = []
    img = frame.copy()

    def click(event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            pontos.append((x / largura, y / altura))
            cv2.circle(img, (x, y), 6, (0, 0, 255), -1)
            cv2.imshow(titulo_janela, img)

    cv2.namedWindow(titulo_janela, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(titulo_janela, 1280, 720)
    cv2.imshow(titulo_janela, img)
    cv2.setMouseCallback(titulo_janela, click)
    while True:
        if (cv2.waitKey(20) & 0xFF) == 27:
            break
    cv2.destroyWindow(titulo_janela)
    return pontos


def hsv_de_ponto(frame, ponto_pct, largura, altura, margem=15):
    x = int(ponto_pct[0] * largura)
    y = int(ponto_pct[1] * altura)
    bgr = frame[y, x, :3].reshape(1, 1, 3).astype(np.uint8)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)[0, 0]
    lower = [max(0, int(hsv[0]) - margem), max(0, int(hsv[1]) - 60), max(0, int(hsv[2]) - 60)]
    upper = [min(179, int(hsv[0]) + margem), 255, 255]
    return lower, upper


def calcular_regiao_pixels(area_pct, largura_atual, altura_atual, origem_x=0, origem_y=0):
    x1 = int(area_pct["x1_pct"] * largura_atual)
    y1 = int(area_pct["y1_pct"] * altura_atual)
    x2 = int(area_pct["x2_pct"] * largura_atual)
    y2 = int(area_pct["y2_pct"] * altura_atual)
    return {"top": origem_y + y1, "left": origem_x + x1, "width": max(1, x2 - x1), "height": max(1, y2 - y1)}


def centro_y_da_cor(frame_bgr, lower, upper):
    hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array(lower), np.array(upper))
    contornos, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contornos:
        return None
    maior = max(contornos, key=cv2.contourArea)
    if cv2.contourArea(maior) < 4:
        return None
    M = cv2.moments(maior)
    if M["m00"] == 0:
        return None
    return M["m01"] / M["m00"]


def centro_e_area_da_cor(frame_bgr, lower, upper, area_minima=30):
    hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array(lower), np.array(upper))
    contornos, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contornos:
        return None
    maior = max(contornos, key=cv2.contourArea)
    area = cv2.contourArea(maior)
    if area < area_minima:
        return None
    M = cv2.moments(maior)
    if M["m00"] == 0:
        return None
    return (M["m10"] / M["m00"], M["m01"] / M["m00"])


def preparar_para_diff(frame_bgr):
    pequeno = cv2.resize(frame_bgr, (32, 32))
    return cv2.cvtColor(pequeno, cv2.COLOR_BGR2GRAY)


def calcular_diff(frame_bgr, baseline_cinza_32):
    atual = preparar_para_diff(frame_bgr)
    return float(np.mean(cv2.absdiff(atual, baseline_cinza_32)))


def ler_texto(frame_bgr):
    if not OCR_DISPONIVEL:
        return ""
    frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    try:
        return pytesseract.image_to_string(frame_rgb).strip().lower()
    except Exception:
        return ""


def ler_numero(frame_bgr):
    if not OCR_DISPONIVEL:
        return None
    frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    try:
        texto = pytesseract.image_to_string(
            frame_rgb, config="--psm 7 -c tessedit_char_whitelist=0123456789"
        ).strip()
        return int(texto) if texto.isdigit() else None
    except Exception:
        return None


def texto_contem_alguma_frase(texto, frases_csv):
    frases = [f.strip().lower() for f in frases_csv.split(",") if f.strip()]
    for frase in frases:
        if frase in texto:
            return True, frase
    return False, None


def nome_bate_com_lista(texto_lido, lista_nomes, limiar=0.6):
    """Compara o nome lido por OCR com a lista manual, tolerando erro de OCR."""
    texto_lido = (texto_lido or "").lower().strip()
    if not texto_lido:
        return False, None, 0.0
    melhor_score = 0.0
    melhor_nome = None
    for nome in lista_nomes:
        nome_l = nome.lower().strip()
        if not nome_l:
            continue
        if nome_l in texto_lido or texto_lido in nome_l:
            return True, nome, 1.0
        score = difflib.SequenceMatcher(None, texto_lido, nome_l).ratio()
        if score > melhor_score:
            melhor_score = score
            melhor_nome = nome
    return (melhor_score >= limiar), melhor_nome, melhor_score


def decidir_ocr_fruta(texto_lido, lista_nomes, limiar=0.6):
    """Decide de forma segura: OCR vazio/incerto nunca vira fruta nova."""
    texto = (texto_lido or "").strip().lower()
    if not texto:
        return {"acao": "pausar", "motivo": "OCR vazio", "score": 0.0, "nome": None}

    duplicata, nome, score = nome_bate_com_lista(texto, lista_nomes, limiar)
    if duplicata:
        return {"acao": "descartar", "motivo": "fruta já possuída", "score": score, "nome": nome}
    if lista_nomes and score < limiar:
        return {"acao": "pausar", "motivo": "OCR incerto; não corresponde à lista conhecida",
                "score": score, "nome": nome}
    return {"acao": "guardar", "motivo": "nome OCR confirmado", "score": score, "nome": texto}


def decidir_leitura_isca(numero, zeros_consecutivos, limite=3):
    """OCR inválido não conta como zero; só zeros válidos consecutivos disparam troca."""
    if numero is None:
        return {"acao": "invalida", "zeros_consecutivos": 0}
    if numero < 0:
        return {"acao": "invalida", "zeros_consecutivos": 0}
    if numero == 0:
        zeros = zeros_consecutivos + 1
        return {
            "acao": "acabou" if zeros >= limite else "aguardar",
            "zeros_consecutivos": zeros,
        }
    return {"acao": "normal", "zeros_consecutivos": 0}


# ---------------------------------------------------------------------------
# JANELA: MINHAS FRUTAS (lista manual)
# ---------------------------------------------------------------------------

class JanelaMinhasFrutas:
    def __init__(self, parent, obter_lista, salvar_lista):
        self.obter_lista = obter_lista
        self.salvar_lista = salvar_lista

        self.janela = tk.Toplevel(parent)
        self.janela.title("Minhas frutas (ja possuidas)")
        self.janela.geometry("340x420")

        ttk.Label(self.janela, text="Digite o nome exatamente como aparece no jogo:").pack(
            anchor="w", padx=10, pady=(10, 0))

        self.lista = tk.Listbox(self.janela)
        self.lista.pack(fill="both", expand=True, padx=10, pady=10)
        for nome in self.obter_lista():
            self.lista.insert("end", nome)

        frame_form = ttk.Frame(self.janela, padding=10)
        frame_form.pack(fill="x")
        self.var_nome = tk.StringVar()
        entrada = ttk.Entry(frame_form, textvariable=self.var_nome)
        entrada.pack(side="left", fill="x", expand=True)
        entrada.bind("<Return>", lambda e: self._adicionar())
        ttk.Button(frame_form, text="Adicionar", command=self._adicionar).pack(side="left", padx=5)

        ttk.Button(self.janela, text="Remover selecionada", command=self._remover).pack(pady=(0, 10))

    def _adicionar(self):
        nome = self.var_nome.get().strip()
        if not nome:
            return
        self.lista.insert("end", nome)
        self.var_nome.set("")
        self._salvar()

    def _remover(self):
        selecao = self.lista.curselection()
        if not selecao:
            return
        self.lista.delete(selecao[0])
        self._salvar()

    def _salvar(self):
        nomes = list(self.lista.get(0, "end"))
        self.salvar_lista(nomes)


# ---------------------------------------------------------------------------
# JANELA: POSICOES DE ISCA
# ---------------------------------------------------------------------------

class JanelaPosicoesIsca:
    def __init__(self, parent, obter_posicoes, salvar_posicoes):
        self.parent = parent
        self.obter_posicoes = obter_posicoes
        self.salvar_posicoes = salvar_posicoes

        self.janela = tk.Toplevel(parent)
        self.janela.title("Posicoes de isca (lista empilhada)")
        self.janela.geometry("340x400")

        ttk.Label(self.janela, text="Iscas marcadas:").pack(anchor="w", padx=10, pady=(10, 0))
        self.lista = tk.Listbox(self.janela)
        self.lista.pack(fill="both", expand=True, padx=10, pady=10)
        self._atualizar_lista()

        ttk.Button(self.janela, text="Marcar nova posicao de isca",
                   command=self._marcar_nova).pack(padx=10, pady=5, fill="x")
        ttk.Button(self.janela, text="Remover selecionada",
                   command=self._remover).pack(padx=10, pady=5, fill="x")

    def _atualizar_lista(self):
        self.lista.delete(0, "end")
        for pos in self.obter_posicoes():
            self.lista.insert("end", pos["nome"])

    def _marcar_nova(self):
        def ao_clicar(x_pct, y_pct):
            nome = simpledialog.askstring("Nome da isca", "Como quer chamar esta isca? (ex: Comum, Rara)")
            if not nome:
                return
            posicoes = self.obter_posicoes()
            posicoes.append({"nome": nome, "x_pct": x_pct, "y_pct": y_pct})
            self.salvar_posicoes(posicoes)
            self._atualizar_lista()
        JanelaMarcarPonto(self.parent, ao_clicar)

    def _remover(self):
        selecao = self.lista.curselection()
        if not selecao:
            return
        posicoes = self.obter_posicoes()
        del posicoes[selecao[0]]
        self.salvar_posicoes(posicoes)
        self._atualizar_lista()


# ---------------------------------------------------------------------------
# APLICACAO GUI
# ---------------------------------------------------------------------------

class FishingBotApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Fishing Bot GPO")
        self.root.geometry("580x640")
        self.root.resizable(False, False)

        self.macro_rodando = threading.Event()
        self.pausar_para_fruta = threading.Event()
        self.pausar_sem_isca = threading.Event()
        self._ui_queue = queue.Queue()
        self._workers = []
        self._closing = threading.Event()
        self._hotkey_handle = None
        self.estado = BotState.PARADO
        self.metrics = BotMetrics()
        self.input = InputController()
        self.window_tracker = WindowTracker()

        self._montar_interface()
        self.root.after(50, self._drenar_atualizacoes_ui)
        self.root.protocol("WM_DELETE_WINDOW", self.encerrar)
        self._registrar_hotkey_emergencia()

        if not OCR_DISPONIVEL:
            self.log("AVISO: pytesseract nao instalado - leitura de nome de fruta, mensagem de erro "
                     "e contador de isca ficarao desativados.")

        self.log(f"Pronto. Tecla de emergencia: {HOTKEY_PARAR.upper()}.")
        self.log("Lembrete: equipe a vara e selecione a isca MANUALMENTE antes de iniciar (ou configure o fallback).")
        if os.path.exists(CONFIG_PATH):
            self.log("Configuracao existente encontrada.")

    def _montar_interface(self):
        ttk.Label(
            self.root,
            text="Configure 'Minhas Frutas' e 'Posicoes de Isca' para automatizar tudo.",
            foreground="#b8860b", wraplength=550
        ).pack(fill="x", padx=10, pady=(10, 0))

        frame_topo = ttk.Frame(self.root, padding=10)
        frame_topo.pack(fill="x")

        self.var_deteccao_fruta = tk.BooleanVar(value=True)
        ttk.Checkbutton(frame_topo, text="Detectar fruta / verificar duplicata / descartar",
                         variable=self.var_deteccao_fruta).grid(row=0, column=0, columnspan=2, sticky="w")

        self.var_monitorar_isca = tk.BooleanVar(value=True)
        ttk.Checkbutton(frame_topo, text="Monitorar isca e trocar/pausar quando acabar",
                         variable=self.var_monitorar_isca).grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))

        ttk.Label(frame_topo, text="Isca de fallback ao acabar:").grid(row=3, column=0, sticky="w", pady=(8, 0))
        self.var_isca_fallback = tk.StringVar()
        self.combo_isca_fallback = ttk.Combobox(frame_topo, textvariable=self.var_isca_fallback,
                                                 state="readonly", width=20)
        self.combo_isca_fallback.grid(row=3, column=1, sticky="w", pady=(8, 0))
        self._atualizar_combo_isca()

        frame_gerenciar = ttk.Frame(self.root, padding=10)
        frame_gerenciar.pack(fill="x")
        ttk.Button(frame_gerenciar, text="Minhas Frutas", command=self.abrir_minhas_frutas).pack(
            side="left", padx=5, fill="x", expand=True)
        ttk.Button(frame_gerenciar, text="Posicoes de Isca", command=self.abrir_posicoes_isca).pack(
            side="left", padx=5, fill="x", expand=True)
        ttk.Button(frame_gerenciar, text="Ponto Store Fruit",
                   command=self.iniciar_calibracao_store_fruit).pack(
            side="left", padx=5, fill="x", expand=True)

        frame_botoes = ttk.Frame(self.root, padding=10)
        frame_botoes.pack(fill="x")

        self.btn_areas = ttk.Button(frame_botoes, text="1. Ajustar Areas", command=self.iniciar_calibracao_areas)
        self.btn_areas.grid(row=0, column=0, padx=5, pady=5, sticky="ew")

        self.btn_cores = ttk.Button(frame_botoes, text="2. Calibrar Cores", command=self.iniciar_calibracao_cores)
        self.btn_cores.grid(row=0, column=1, padx=5, pady=5, sticky="ew")

        self.btn_baseline = ttk.Button(frame_botoes, text="3. Capturar Slot Normal", command=self.capturar_baseline)
        self.btn_baseline.grid(row=1, column=0, padx=5, pady=5, sticky="ew")

        self.btn_iniciar = ttk.Button(frame_botoes, text="4. Iniciar Macro", command=self.iniciar_macro)
        self.btn_iniciar.grid(row=1, column=1, padx=5, pady=5, sticky="ew")

        self.btn_continuar_ocr = ttk.Button(frame_botoes, text="Continuar apos OCR",
                                             command=self.continuar_apos_ocr, state="disabled")
        self.btn_continuar_ocr.grid(row=2, column=0, padx=5, pady=5, sticky="ew")

        self.btn_continuar_isca = ttk.Button(frame_botoes, text="Continuar apos isca",
                                              command=self.continuar_apos_isca, state="disabled")
        self.btn_continuar_isca.grid(row=2, column=1, padx=5, pady=5, sticky="ew")

        self.btn_parar = ttk.Button(frame_botoes, text=f"Parar ({HOTKEY_PARAR.upper()})",
                                     command=self.parar_macro, state="disabled")
        self.btn_parar.grid(row=3, column=0, padx=5, pady=5, sticky="ew")

        self.btn_diagnostico = ttk.Button(
            frame_botoes, text="Diagnostico (sem interacao)", command=self.iniciar_diagnostico
        )
        self.btn_diagnostico.grid(row=4, column=0, columnspan=2, padx=5, pady=5, sticky="ew")

        frame_botoes.columnconfigure((0, 1), weight=1)

        self.lbl_status = ttk.Label(self.root, text="Status: parado", foreground="gray")
        self.lbl_status.pack(anchor="w", padx=10)

        frame_log = ttk.Frame(self.root, padding=10)
        frame_log.pack(fill="both", expand=True)
        self.txt_log = tk.Text(frame_log, height=12, state="disabled", wrap="word")
        self.txt_log.pack(fill="both", expand=True)

    def _atualizar_combo_isca(self):
        posicoes = self._obter_posicoes_isca()
        nomes = ["Nenhum (pausar)"] + [p["nome"] for p in posicoes]
        self.combo_isca_fallback["values"] = nomes
        if not self.var_isca_fallback.get():
            self.var_isca_fallback.set(nomes[0])

    def log(self, mensagem, nivel=logging.INFO):
        _ensure_logging_configured()
        LOGGER.log(nivel, mensagem)
        def _escrever():
            self.txt_log.config(state="normal")
            self.txt_log.insert("end", f"{mensagem}\n")
            self.txt_log.see("end")
            self.txt_log.config(state="disabled")
        self._agendar_ui(_escrever)

    def set_status(self, texto, cor="gray"):
        self._agendar_ui(lambda: self.lbl_status.config(text=f"Status: {texto}", foreground=cor))

    def _definir_estado(self, estado):
        self.estado = estado
        cor = "red" if estado == BotState.ERRO else (
            "orange" if estado in (BotState.PAUSADO_FRUTA, BotState.PAUSADO_ISCA)
            else "green" if estado == BotState.RODANDO else "gray"
        )
        self.set_status(estado.value, cor)

    def _agendar_ui(self, callback):
        """Workers só enfileiram; o callback é executado pelo mainloop."""
        self._ui_queue.put(callback)

    def _drenar_atualizacoes_ui(self):
        while True:
            try:
                callback = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            callback()
        if self.root.winfo_exists():
            self.root.after(50, self._drenar_atualizacoes_ui)

    def _carregar_config_completa(self):
        if not os.path.exists(CONFIG_PATH):
            return configuracao_padrao()
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return normalizar_config(json.load(f))
        except (OSError, ValueError, TypeError) as exc:
            self.log(f"AVISO: configuracao invalida, usando defaults ({exc}).")
            return configuracao_padrao()

    def _salvar_config_completa(self, cfg):
        cfg = normalizar_config(cfg)
        pasta = os.path.dirname(CONFIG_PATH) or APP_DIR
        os.makedirs(pasta, exist_ok=True)
        fd, temporario = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=pasta)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporario, CONFIG_PATH)
        except Exception:
            try:
                os.unlink(temporario)
            except OSError:
                pass
            raise

    def _valor_config_fruta(self, chave, padrao):
        return self._carregar_config_completa().get("fruta", {}).get(chave, padrao)

    # --- minhas frutas -------------------------------------------------

    def abrir_minhas_frutas(self):
        def obter_lista():
            return self._carregar_config_completa().get("frutas_possuidas", [])

        def salvar_lista(nomes):
            cfg = self._carregar_config_completa()
            cfg["frutas_possuidas"] = nomes
            self._salvar_config_completa(cfg)

        JanelaMinhasFrutas(self.root, obter_lista, salvar_lista)

    # --- posicoes de isca ------------------------------------------------

    def _obter_posicoes_isca(self):
        return self._carregar_config_completa().get("posicoes_isca", [])

    def abrir_posicoes_isca(self):
        def obter_posicoes():
            return self._obter_posicoes_isca()

        def salvar_posicoes(posicoes):
            cfg = self._carregar_config_completa()
            cfg["posicoes_isca"] = posicoes
            self._salvar_config_completa(cfg)
            self._atualizar_combo_isca()

        JanelaPosicoesIsca(self.root, obter_posicoes, salvar_posicoes)

    def iniciar_calibracao_store_fruit(self):
        snapshot = self.window_tracker.snapshot()
        if not janela_pronta(snapshot):
            messagebox.showwarning(
                "Fishing Bot",
                "Coloque a janela do Roblox em primeiro plano e nao minimizada.",
            )
            return

        def ao_clicar(x_pct, y_pct):
            atual = self.window_tracker.snapshot()
            if not janela_pronta(atual) or not mesma_janela(snapshot, atual):
                self.log("Calibracao Store Fruit cancelada: janela mudou ou perdeu foco.",
                         logging.WARNING)
                return
            ponto = {"x_pct": x_pct, "y_pct": y_pct}
            try:
                screen_point_from_pct(ponto, atual.client)
            except ValueError as exc:
                self.log(f"Ponto Store Fruit invalido: {exc}", logging.ERROR)
                return
            cfg = self._carregar_config_completa()
            cfg["fruta"]["ponto_guardar_pct"] = ponto
            cfg["fruta"]["janela_ponto"] = {
                "width": atual.client.width,
                "height": atual.client.height,
                "dpi": atual.dpi,
            }
            self._salvar_config_completa(cfg)
            self.log("Ponto Store Fruit calibrado relativo a area cliente.")

        JanelaMarcarPonto(
            self.root, ao_clicar,
            "Clique no prompt Store Fruit (ESC cancela)",
        )

    # --- passo 1: areas ---------------------------------------------------

    def iniciar_calibracao_areas(self):
        self.log("Overlay ao vivo: o jogo aparece por tras. Arraste os 5 retangulos "
                 "(os que ja estavam calibrados aparecem na posicao salva). ESC cancela sem salvar.")
        cfg_existente = self._carregar_config_completa()
        snapshot = self.window_tracker.snapshot()
        if janela_pronta(snapshot):
            cfg_existente, migrada = migrar_config_janela(cfg_existente, snapshot)
            if migrada:
                self._salvar_config_completa(cfg_existente)
                self.log("Configuracao antiga migrada antes da calibracao.")
        JanelaCalibracaoAreas(self.root, self._salvar_areas, cfg_existente)

    def _salvar_areas(self, area_pesca, area_slot, area_texto_erro,
                       area_contador_isca, area_nome_fruta, resolucao):
        areas = (area_pesca, area_slot, area_texto_erro, area_contador_isca, area_nome_fruta)
        if not all(validar_regiao_pct(area) for area in areas):
            self.log("Areas invalidas: cada retangulo deve estar dentro do monitor e ter tamanho positivo.")
            self.set_status("calibracao invalida", "red")
            return
        cfg = self._carregar_config_completa()
        cfg["regiao_pesca"] = area_pesca
        cfg["regiao_slot_fruta"] = area_slot
        cfg["regiao_texto_erro"] = area_texto_erro
        cfg["regiao_contador_isca"] = area_contador_isca
        cfg["regiao_nome_fruta"] = area_nome_fruta
        cfg["resolucao_calibrada"] = list(resolucao)
        snapshot = self.window_tracker.snapshot()
        cfg["referencia_coordenadas"] = "cliente"
        cfg["janela_calibracao"] = {
            "width": resolucao[0], "height": resolucao[1], "dpi": snapshot.dpi,
        }
        self._salvar_config_completa(cfg)
        self.log("Areas salvas. Proximo passo: calibrar cores.")

    # --- passo 2: cores ---------------------------------------------------

    def iniciar_calibracao_cores(self):
        cfg = self._carregar_config_completa()
        if "regiao_pesca" not in cfg:
            messagebox.showwarning("Fishing Bot", "Ajuste as areas primeiro (passo 1).")
            return
        self._iniciar_worker(self._rodar_calibracao_cores, cfg)

    def _rodar_calibracao_cores(self, cfg):
        try:
            self.set_status("calibrando cores", "orange")
            with mss.mss() as sct:
                snapshot = self.window_tracker.snapshot()
                if not janela_pronta(snapshot):
                    raise RuntimeError("Janela Roblox indisponivel para calibracao.")
                area = {"top": snapshot.client.top, "left": snapshot.client.left,
                        "width": snapshot.client.width, "height": snapshot.client.height}
                largura, altura = snapshot.client.width, snapshot.client.height
                frame = np.array(sct.grab(area))[:, :, :3]

            self.log("Clique na cor VERDE da zona-alvo da pesca. ESC confirma.")
            p1 = pegar_pontos(frame, largura, altura, "Cor da zona-alvo (ESC confirma)")
            if not p1:
                self.log("Cancelado.")
                return
            verde_lower, verde_upper = hsv_de_ponto(frame, p1[-1], largura, altura)

            self.log("Clique no marcador/icone do peixe. ESC confirma.")
            p2 = pegar_pontos(frame, largura, altura, "Marcador do peixe (ESC confirma)")
            if not p2:
                self.log("Cancelado.")
                return
            marcador_lower, marcador_upper = hsv_de_ponto(frame, p2[-1], largura, altura, margem=25)

            cfg["cores"] = {
                "zona_verde": {"lower": verde_lower, "upper": verde_upper},
                "marcador": {"lower": marcador_lower, "upper": marcador_upper},
            }
            cfg.setdefault("loop", {"margem_pixels": 4, "intervalo_segundos": 0.02})
            cfg.setdefault("fruta", {})
            cfg["fruta"].setdefault("limiar_diff", 20)
            cfg["fruta"].setdefault("cooldown_segundos", 8)
            cfg["fruta"].setdefault("frases_duplicata", FRASES_DUPLICATA_PADRAO)
            cfg["fruta"].setdefault("limiar_nome", 0.6)
            self._salvar_config_completa(cfg)

            self.log("Cores calibradas. Proximo passo: capturar o slot em estado normal (sem fruta).")
            self.set_status("cores ok", "green")
        except Exception as e:
            self.log(f"Erro na calibracao de cores: {e}")
            self.set_status("erro", "red")

    # --- passo 3: baseline do slot -------------------------------------

    def capturar_baseline(self):
        cfg = self._carregar_config_completa()
        if "regiao_slot_fruta" not in cfg:
            messagebox.showwarning("Fishing Bot", "Ajuste as areas primeiro (passo 1).")
            return
        messagebox.showinfo(
            "Fishing Bot",
            "Va para o jogo AGORA e garanta que o slot calibrado esta VAZIO/normal. "
            "Clique OK - vai tirar o print em 3 segundos."
        )
        self._iniciar_worker(self._rodar_captura_baseline, cfg)

    def _rodar_captura_baseline(self, cfg):
        time.sleep(3)
        with mss.mss() as sct:
            snapshot = self.window_tracker.snapshot()
            if not janela_pronta(snapshot):
                raise RuntimeError("Janela Roblox indisponivel para baseline.")
            largura, altura = snapshot.client.width, snapshot.client.height
            regiao_px = calcular_regiao_pixels(
                cfg["regiao_slot_fruta"], largura, altura,
                snapshot.client.left, snapshot.client.top
            )
            frame = np.array(sct.grab(regiao_px))[:, :, :3]
        cv2.imwrite(BASELINE_PATH, frame)
        cfg["fruta_baseline_arquivo"] = BASELINE_PATH
        self._salvar_config_completa(cfg)
        self.log("Baseline do slot capturada. Tudo pronto - pode iniciar o macro.")
        self.set_status("baseline ok", "green")

    # --- passo 4: macro -----------------------------------------------------

    def iniciar_macro(self):
        cfg = self._carregar_config_completa()
        snapshot = self.window_tracker.snapshot()
        if not janela_pronta(snapshot):
            messagebox.showwarning("Fishing Bot", "Coloque a janela do Roblox em primeiro plano e nao minimizada.")
            self.log("Pre-voo bloqueado: janela Roblox sem foco ou minimizada.", logging.WARNING)
            return
        self.log("Pre-voo: Roblox=%s cliente=%sx%s monitor=%sx%s dpi=%s" % (
            snapshot.title, snapshot.client.width, snapshot.client.height,
            snapshot.monitor.width, snapshot.monitor.height, snapshot.dpi
        ))
        cfg, migrada = migrar_config_janela(cfg, snapshot)
        if migrada:
            self._salvar_config_completa(cfg)
            self.log("Configuracao antiga migrada para coordenadas da area cliente.")
        if not validar_contexto_calibracao(cfg, snapshot):
            messagebox.showwarning("Fishing Bot", "A calibracao nao e compativel com a janela atual. Recalibre as areas.")
            self.log("Pre-voo bloqueado: resolucao/DPI da calibracao incompatível.", logging.WARNING)
            return
        faltando = [k for k in ("regiao_pesca", "cores") if k not in cfg]
        if faltando:
            messagebox.showwarning("Fishing Bot", "Complete os passos anteriores primeiro.")
            return
        if self.var_deteccao_fruta.get() and not os.path.exists(BASELINE_PATH):
            messagebox.showwarning("Fishing Bot", "Capture o baseline do slot (passo 3) ou desmarque a deteccao de fruta.")
            return
        fruta_cfg = cfg.get("fruta", {})
        ponto_guardar = fruta_cfg.get("ponto_guardar_pct")
        ponto_contexto = {"janela_calibracao": fruta_cfg.get("janela_ponto", {})}
        if self.var_deteccao_fruta.get() and (
                not validar_ponto_pct(ponto_guardar)
                or not validar_contexto_calibracao(ponto_contexto, snapshot)):
            messagebox.showwarning(
                "Fishing Bot",
                "Calibre o ponto Store Fruit antes de iniciar a deteccao de fruta.",
            )
            self.log("Pre-voo bloqueado: ponto Store Fruit nao calibrado.", logging.WARNING)
            return

        cfg["isca_fallback"] = self.var_isca_fallback.get()
        cfg.setdefault("fruta", {})
        self._salvar_config_completa(cfg)

        self.macro_rodando.set()
        self._definir_estado(BotState.RODANDO)
        self.metrics = BotMetrics()
        self.pausar_para_fruta.clear()
        self.pausar_sem_isca.clear()
        self.btn_iniciar.config(state="disabled")
        self.btn_areas.config(state="disabled")
        self.btn_cores.config(state="disabled")
        self.btn_baseline.config(state="disabled")
        self.btn_parar.config(state="normal")
        self.set_status("rodando", "green")
        self.log("Macro iniciado. Va para o jogo. F8 para parar.")

        self._iniciar_worker(self._loop_macro, cfg)
        if self.var_deteccao_fruta.get() and OCR_DISPONIVEL:
            self._iniciar_worker(self._loop_deteccao_fruta, cfg)
        if self.var_monitorar_isca.get() and OCR_DISPONIVEL and "regiao_contador_isca" in cfg:
            self._iniciar_worker(self._loop_monitorar_isca, cfg)

    def iniciar_diagnostico(self):
        """Captura/inspeciona sem enviar mouse ou teclado."""
        cfg = self._carregar_config_completa()
        self._iniciar_worker(self._rodar_diagnostico, cfg)

    def _rodar_diagnostico(self, cfg):
        snapshot = self.window_tracker.snapshot()
        self.log(
            "Diagnostico: janela=%r hwnd=%s foco=%s minimizada=%s cliente=%sx%s+%s+%s "
            "monitor=%sx%s+%s+%s dpi=%s" % (
                snapshot.title, snapshot.hwnd, snapshot.foreground, snapshot.minimized,
                snapshot.client.width, snapshot.client.height, snapshot.client.left, snapshot.client.top,
                snapshot.monitor.width, snapshot.monitor.height, snapshot.monitor.left, snapshot.monitor.top,
                snapshot.dpi,
            )
        )
        if not janela_pronta(snapshot):
            self.log("Diagnostico interrompido: janela Roblox nao esta pronta.", logging.WARNING)
            return
        with ScreenCapture() as sct:
            area = {"top": snapshot.client.top, "left": snapshot.client.left,
                    "width": snapshot.client.width, "height": snapshot.client.height}
            frame = np.array(sct.grab(area))[:, :, :3]
        self.log("Diagnostico: captura cliente=%sx%s media_bgr=%s" % (
            frame.shape[1], frame.shape[0], np.mean(frame, axis=(0, 1)).round(1).tolist()
        ))
        for chave in ("regiao_pesca", "regiao_slot_fruta", "regiao_nome_fruta",
                      "regiao_contador_isca", "regiao_texto_erro"):
            area_pct = cfg.get(chave)
            if validar_regiao_pct(area_pct):
                regiao = point_from_pct(area_pct, snapshot.client)
                self.log("Diagnostico: %s=%s dentro_cliente=%s" % (
                    chave, regiao, ponto_dentro_cliente(
                        ScreenPoint(regiao.left, regiao.top), snapshot.client
                    )
                ))

    def _iniciar_worker(self, alvo, cfg):
        trabalhador = threading.Thread(target=self._executar_worker, args=(alvo, cfg), daemon=True)
        self._workers.append(trabalhador)
        trabalhador.start()

    def _executar_worker(self, alvo, cfg):
        """Converte qualquer falha de worker em parada explícita e segura."""
        try:
            alvo(cfg)
        except Exception as exc:
            nome = getattr(alvo, "__name__", "worker")
            self.log(f"Erro no {nome}: {exc!r}. Automacao interrompida com seguranca.", logging.ERROR)
            self._finalizar_macro(BotState.ERRO)

    def _finalizar_macro(self, estado=BotState.PARADO):
        """Finaliza de forma idempotente e atualiza todos os controles da GUI."""
        self.macro_rodando.clear()
        self.pausar_para_fruta.set()
        self.pausar_sem_isca.set()
        try:
            self.input.mouse_up()
        except Exception as exc:
            self.log(f"Erro ao liberar mouse durante finalizacao: {exc!r}", logging.ERROR)
        self._definir_estado(estado)
        for btn in (self.btn_iniciar, self.btn_areas, self.btn_cores, self.btn_baseline):
            self._agendar_ui(lambda b=btn: b.config(state="normal"))
        self._agendar_ui(lambda: self.btn_parar.config(state="disabled"))
        self._agendar_ui(lambda: self.btn_continuar_isca.config(state="disabled"))
        if hasattr(self, "btn_continuar_ocr"):
            self._agendar_ui(lambda: self.btn_continuar_ocr.config(state="disabled"))

    def parar_macro(self):
        if self.macro_rodando.is_set():
            self.log("Macro parado.")
        self._finalizar_macro(BotState.PARADO)

    def encerrar(self):
        """Para a automação antes de destruir a janela e seus recursos."""
        if self._closing.is_set():
            return
        self._closing.set()
        self._definir_estado(BotState.ENCERRANDO)
        self.macro_rodando.clear()
        self.pausar_para_fruta.set()
        self.pausar_sem_isca.set()
        try:
            self.input.mouse_up()
        except Exception as exc:
            self.log(f"Aviso ao liberar mouse durante encerramento: {exc}")
        if self._hotkey_handle is not None:
            try:
                keyboard.remove_hotkey(self._hotkey_handle)
            except Exception as exc:
                self.log(f"Aviso ao remover hotkey: {exc}")
            self._hotkey_handle = None
        for trabalhador in list(self._workers):
            trabalhador.join(timeout=2.0)
        self._workers.clear()
        try:
            cv2.destroyAllWindows()
        except Exception as exc:
            self.log(f"Aviso ao fechar overlays de calibracao: {exc}")
        for janela in self.root.winfo_children():
            if isinstance(janela, tk.Toplevel):
                janela.destroy()
        self.root.destroy()

    def continuar_apos_isca(self):
        self.pausar_sem_isca.clear()
        self.pausar_para_fruta.clear()
        self._definir_estado(BotState.RODANDO)
        self.btn_continuar_isca.config(state="disabled")
        self.log("Retomando apos troca de isca ou revisao do OCR.")
        self.set_status("rodando", "green")

    def continuar_apos_ocr(self):
        self.pausar_para_fruta.clear()
        self._definir_estado(BotState.RODANDO)
        self.btn_continuar_ocr.config(state="disabled")
        self.log("Retomando apos revisao manual do OCR.")
        self.set_status("rodando", "green")

    def _loop_macro(self, cfg):
        try:
            self.input.click()
            time.sleep(1.0)

            with ScreenCapture() as sct:
                snapshot = self.window_tracker.snapshot()
                if not janela_pronta(snapshot):
                    raise RuntimeError("Janela Roblox indisponivel no inicio da pesca.")
                largura_atual, altura_atual = snapshot.client.width, snapshot.client.height
                regiao_px = calcular_regiao_pixels(
                    cfg["regiao_pesca"], largura_atual, altura_atual,
                    snapshot.client.left, snapshot.client.top
                )

                verde = cfg["cores"]["zona_verde"]
                marcador = cfg["cores"]["marcador"]

                segurando = False
                tempo_sem_barra_inicio = None
                LIMITE_RECAST_SEGUNDOS = 1.5

                y_marcador_anterior = None
                tempo_anterior = None

                # --- auto-ajuste da MARGEM (dead-zone) ---
                # comeca com o valor calibrado (ou 4px) e se corrige sozinho:
                # sobe se detectar "chattering" (segurar/soltar demais = ruido
                # vencendo a decisao), desce aos poucos se estiver estavel
                # demais (pra nao ficar menos responsivo que o necessario).
                margem = max(1, cfg["loop"].get("margem_pixels", 4))
                MARGEM_MIN, MARGEM_MAX = 1, 40
                contagem_toggles = 0
                janelas_sem_toggle = 0
                inicio_janela_margem = time.time()

                # --- auto-ajuste da LATENCIA DE PREVISAO (hill climbing) ---
                # a cada 2s, compara o erro medio de previsao com a janela
                # anterior: se melhorou, continua na mesma direcao; se
                # piorou, inverte a direcao e reduz o passo (busca 1D classica).
                latencia = 0.06
                LATENCIA_MIN, LATENCIA_MAX = 0.0, 0.30
                direcao_latencia = 1
                passo_latencia = 0.02
                erro_medio = None
                erro_medio_anterior = None
                inicio_janela_latencia = time.time()
                previsoes_pendentes = deque()

                contagem_frames = 0
                inicio_janela_fps = time.time()

                while self.macro_rodando.is_set():
                    atual = self.window_tracker.snapshot()
                    if not janela_pronta(atual) or not mesma_janela(snapshot, atual):
                        if not self.pausar_para_fruta.is_set():
                            self._definir_estado(BotState.PAUSADO_FRUTA)
                            self.input.mouse_up()
                            self.log("Pesca pausada: janela perdeu foco ou mudou de layout.", logging.WARNING)
                        self.pausar_para_fruta.set()
                        time.sleep(0.5)
                        continue
                    if self.pausar_para_fruta.is_set() or self.pausar_sem_isca.is_set():
                        if segurando:
                            self.input.mouse_up()
                            segurando = False
                        y_marcador_anterior = None
                        tempo_anterior = None
                        previsoes_pendentes.clear()
                        time.sleep(0.2)
                        continue

                    agora = time.time()
                    frame = np.array(sct.grab(regiao_px))[:, :, :3]
                    y_verde = centro_y_da_cor(frame, verde["lower"], verde["upper"])
                    y_marcador = centro_y_da_cor(frame, marcador["lower"], marcador["upper"])

                    contagem_frames += 1
                    self.metrics.frame()
                    if agora - inicio_janela_fps >= 2.0:
                        fps_real = contagem_frames / (agora - inicio_janela_fps)
                        self.log(f"[auto] fps={fps_real:.1f} | margem={margem}px | "
                                 f"latencia_previsao={latencia * 1000:.0f}ms")
                        contagem_frames = 0
                        inicio_janela_fps = agora

                    if y_verde is None or y_marcador is None:
                        if segurando:
                            self.input.mouse_up()
                            segurando = False
                        y_marcador_anterior = None
                        tempo_anterior = None
                        previsoes_pendentes.clear()
                        if tempo_sem_barra_inicio is None:
                            tempo_sem_barra_inicio = agora
                        elif agora - tempo_sem_barra_inicio >= LIMITE_RECAST_SEGUNDOS:
                            self.input.click()
                            tempo_sem_barra_inicio = None
                        continue  # sem sleep - roda no limite do hardware

                    tempo_sem_barra_inicio = None

                    # --- avalia previsoes antigas p/ auto-ajustar a latencia ---
                    while previsoes_pendentes and previsoes_pendentes[0][0] <= agora:
                        _tempo_alvo, valor_previsto = previsoes_pendentes.popleft()
                        erro = abs(valor_previsto - y_marcador)
                        erro_medio = erro if erro_medio is None else (erro_medio * 0.9 + erro * 0.1)

                    if agora - inicio_janela_latencia >= 2.0 and erro_medio is not None:
                        if erro_medio_anterior is not None:
                            if erro_medio > erro_medio_anterior:
                                direcao_latencia *= -1
                                passo_latencia = max(0.005, passo_latencia * 0.5)
                            latencia = min(LATENCIA_MAX, max(
                                LATENCIA_MIN, latencia + direcao_latencia * passo_latencia))
                        erro_medio_anterior = erro_medio
                        inicio_janela_latencia = agora

                    # --- predicao de velocidade (usa a latencia auto-ajustada) ---
                    y_previsto = y_marcador
                    if y_marcador_anterior is not None and tempo_anterior is not None:
                        dt = agora - tempo_anterior
                        if dt > 0:
                            velocidade = (y_marcador - y_marcador_anterior) / dt
                            y_previsto = y_marcador + velocidade * latencia
                            previsoes_pendentes.append((agora + latencia, y_previsto))

                    y_marcador_anterior = y_marcador
                    tempo_anterior = agora

                    # --- decisao segurar/soltar ---
                    diferenca = y_previsto - y_verde
                    estado_anterior = segurando
                    if diferenca > margem and not segurando:
                        self.input.mouse_down()
                        segurando = True
                    elif diferenca < -margem and segurando:
                        self.input.mouse_up()
                        segurando = False
                    if segurando != estado_anterior:
                        contagem_toggles += 1

                    # --- auto-ajuste da margem (anti-chattering), janela de 1s ---
                    if agora - inicio_janela_margem >= 1.0:
                        if contagem_toggles > 8:
                            margem = min(MARGEM_MAX, margem + 1)
                            janelas_sem_toggle = 0
                        elif contagem_toggles == 0:
                            janelas_sem_toggle += 1
                            if janelas_sem_toggle >= 3:
                                margem = max(MARGEM_MIN, margem - 1)
                                janelas_sem_toggle = 0
                        else:
                            janelas_sem_toggle = 0
                        contagem_toggles = 0
                        inicio_janela_margem = agora

                    # sem time.sleep aqui: o loop roda o mais rapido que a
                    # captura de tela + processamento permitirem no seu hardware

                if segurando:
                    self.input.mouse_up()
        except Exception as e:
            self.log(f"Erro no macro: {e}")
            self.set_status("erro", "red")
            self._agendar_ui(self.parar_macro)

    def _loop_deteccao_fruta(self, cfg):
        baseline_img = cv2.imread(cfg.get("fruta_baseline_arquivo", BASELINE_PATH))
        if baseline_img is None:
            self.log("Baseline nao encontrada - deteccao de fruta desativada nesta sessao.")
            return
        baseline_cinza = preparar_para_diff(baseline_img)

        cfg_fruta = cfg.get("fruta", {})
        limiar_diff = cfg_fruta.get("limiar_diff", 20)
        cooldown = cfg_fruta.get("cooldown_segundos", 8)
        frases_duplicata = cfg_fruta.get("frases_duplicata", FRASES_DUPLICATA_PADRAO)
        limiar_nome = cfg_fruta.get("limiar_nome", 0.6)
        frutas_possuidas = cfg.get("frutas_possuidas", [])

        with ScreenCapture() as sct:
            snapshot = self.window_tracker.snapshot()
            if not janela_pronta(snapshot):
                raise RuntimeError("Janela Roblox indisponivel para deteccao de fruta.")
            largura, altura = snapshot.client.width, snapshot.client.height
            regiao_slot_px = calcular_regiao_pixels(
                cfg["regiao_slot_fruta"], largura, altura,
                snapshot.client.left, snapshot.client.top
            )
            regiao_erro_px = (calcular_regiao_pixels(
                cfg["regiao_texto_erro"], largura, altura,
                snapshot.client.left, snapshot.client.top)
                               if "regiao_texto_erro" in cfg else None)
            regiao_nome_px = (calcular_regiao_pixels(
                cfg["regiao_nome_fruta"], largura, altura,
                snapshot.client.left, snapshot.client.top)
                               if "regiao_nome_fruta" in cfg else None)

            ultimo_disparo = 0
            contagem_mudanca = 0

            while self.macro_rodando.is_set():
                atual = self.window_tracker.snapshot()
                if not janela_pronta(atual) or not mesma_janela(snapshot, atual):
                    self.pausar_para_fruta.set()
                    self.input.mouse_up()
                    self._definir_estado(BotState.PAUSADO_FRUTA)
                    self.log("Deteccao pausada: janela Roblox mudou ou perdeu foco.", logging.WARNING)
                    time.sleep(0.5)
                    continue
                agora = time.time()
                if agora - ultimo_disparo < cooldown:
                    time.sleep(0.3)
                    continue

                frame_slot = np.array(sct.grab(regiao_slot_px))[:, :, :3]
                diff = calcular_diff(frame_slot, baseline_cinza)
                contagem_mudanca = contagem_mudanca + 1 if diff > limiar_diff else 0

                if contagem_mudanca >= 2:
                    self.log(f"Slot mudou (diff={diff:.1f}) - possivel fruta. Pausando pesca.")
                    self.pausar_para_fruta.set()
                    self.input.mouse_up()
                    self._definir_estado(BotState.PAUSADO_FRUTA)
                    self.metrics.fruta_detectada()
                    time.sleep(0.6)

                    # recarrega a lista mais recente (pode ter sido editada durante a sessao)
                    frutas_possuidas = self._carregar_config_completa().get("frutas_possuidas", [])
                    ja_resolvido = False
                    ocr_inconclusivo = False

                    if frutas_possuidas and regiao_nome_px is not None:
                        frame_nome = np.array(sct.grab(regiao_nome_px))[:, :, :3]
                        texto_nome = ler_texto(frame_nome)
                        decisao = decidir_ocr_fruta(texto_nome, frutas_possuidas, limiar_nome)
                        if decisao["acao"] == "descartar":
                            nome_bateu = decisao["nome"]
                            score = decisao["score"]
                            self.log(f"Nome lido ('{texto_nome}') bate com '{nome_bateu}' (score={score:.2f}). "
                                     f"Ja possuida - descartando com Backspace.")
                            self.input.press("backspace")
                            ja_resolvido = True
                        elif decisao["acao"] == "guardar":
                            self.log(f"Nome lido ('{texto_nome}') confirmado. "
                                     "acionando ponto Store Fruit calibrado.")
                            ja_resolvido = self._clicar_store_fruit()
                            if ja_resolvido and texto_nome:
                                nova_lista = frutas_possuidas + [texto_nome.title()]
                                cfg_atual = self._carregar_config_completa()
                                cfg_atual["frutas_possuidas"] = nova_lista
                                self._salvar_config_completa(cfg_atual)
                                self.log(f"Adicionei '{texto_nome.title()}' a lista de Minhas Frutas.")
                            if not ja_resolvido:
                                ocr_inconclusivo = True
                        else:
                            self.log(f"PAUSADO: {decisao['motivo']} (texto='{texto_nome}', "
                                     f"score={decisao['score']:.2f}). Revise a fruta e clique Continuar.")
                            self._definir_estado(BotState.PAUSADO_FRUTA)
                            self._agendar_ui(lambda: self.btn_continuar_ocr.config(state="normal"))
                            ocr_inconclusivo = True

                    if not ja_resolvido and not ocr_inconclusivo:
                        # sem lista manual configurada: clica no prompt e checa mensagem de erro
                        if not self._clicar_store_fruit():
                            ocr_inconclusivo = True
                        self.log("Store Fruit acionado por ponto calibrado. Verificando mensagem de erro...")
                        time.sleep(0.8)
                        if not ocr_inconclusivo and regiao_erro_px is not None:
                            frame_erro = np.array(sct.grab(regiao_erro_px))[:, :, :3]
                            texto = ler_texto(frame_erro)
                            dup, frase = texto_contem_alguma_frase(texto, frases_duplicata)
                            if dup:
                                self.log(f"Mensagem de duplicata ('{frase}'). Descartando com Backspace.")
                                self.input.press("backspace")
                            else:
                                self.log("Sem mensagem de erro - fruta guardada normalmente.")
                        elif not ocr_inconclusivo:
                            self.log("Area de mensagem de erro nao calibrada - nao foi possivel confirmar.")

                    if not ocr_inconclusivo:
                        time.sleep(1.0)
                        baseline_cinza = preparar_para_diff(np.array(sct.grab(regiao_slot_px))[:, :, :3])
                        contagem_mudanca = 0
                        ultimo_disparo = time.time()
                        self.pausar_para_fruta.clear()
                    else:
                        contagem_mudanca = 0

                time.sleep(0.4)

    def _clicar_store_fruit(self):
        """Clica somente no ponto calibrado dentro da janela Roblox focada."""
        cfg = self._carregar_config_completa()
        ponto = cfg.get("fruta", {}).get("ponto_guardar_pct")
        snapshot = self.window_tracker.snapshot()
        ponto_contexto = {"janela_calibracao": cfg.get("fruta", {}).get("janela_ponto", {})}
        if (not janela_pronta(snapshot) or not validar_ponto_pct(ponto)
                or not validar_contexto_calibracao(ponto_contexto, snapshot)):
            self.log("Store Fruit bloqueado: janela sem foco ou ponto nao calibrado.",
                     logging.WARNING)
            self.pausar_para_fruta.set()
            self._definir_estado(BotState.PAUSADO_FRUTA)
            return False
        try:
            tela = screen_point_from_pct(ponto, snapshot.client)
        except ValueError as exc:
            self.log(f"Store Fruit bloqueado: {exc}", logging.ERROR)
            self.pausar_para_fruta.set()
            self._definir_estado(BotState.PAUSADO_FRUTA)
            return False
        self.input.click(tela.x, tela.y)
        return True

    def _loop_monitorar_isca(self, cfg):
        regiao_pct = cfg.get("regiao_contador_isca")
        if not regiao_pct:
            return
        with ScreenCapture() as sct:
            snapshot = self.window_tracker.snapshot()
            if not janela_pronta(snapshot):
                raise RuntimeError("Janela Roblox indisponivel para monitoramento de isca.")
            largura, altura = snapshot.client.width, snapshot.client.height
            regiao_px = calcular_regiao_pixels(
                regiao_pct, largura, altura,
                snapshot.client.left, snapshot.client.top
            )

            leituras_zeradas = 0

            while self.macro_rodando.is_set():
                atual = self.window_tracker.snapshot()
                if not janela_pronta(atual) or not mesma_janela(snapshot, atual):
                    self.pausar_sem_isca.set()
                    self.input.mouse_up()
                    self._definir_estado(BotState.PAUSADO_ISCA)
                    self.log("Monitoramento de isca pausado: janela mudou ou perdeu foco.", logging.WARNING)
                    time.sleep(0.5)
                    continue
                if self.pausar_sem_isca.is_set():
                    time.sleep(1.0)
                    continue

                frame = np.array(sct.grab(regiao_px))[:, :, :3]
                numero = ler_numero(frame)
                leitura = decidir_leitura_isca(numero, leituras_zeradas)
                leituras_zeradas = leitura["zeros_consecutivos"]

                if leitura["acao"] == "acabou":
                    cfg_atual = self._carregar_config_completa()
                    isca_fallback = cfg_atual.get("isca_fallback", "Nenhum (pausar)")
                    posicoes = cfg_atual.get("posicoes_isca", [])
                    posicao = next((p for p in posicoes if p["nome"] == isca_fallback), None)

                    if posicao:
                        x_abs = snapshot.client.left + int(posicao["x_pct"] * largura)
                        y_abs = snapshot.client.top + int(posicao["y_pct"] * altura)
                        if not ponto_dentro_cliente(ScreenPoint(x_abs, y_abs), snapshot.client):
                            self.log("Clique de fallback bloqueado: ponto fora da area cliente.", logging.ERROR)
                            continue
                        self.input.click(x_abs, y_abs)
                        self.log(f"Isca acabou - troquei automaticamente para '{isca_fallback}'.")
                        time.sleep(1.0)
                    else:
                        self.log("ISCA ACABOU e nenhum fallback configurado. Pausando o macro. "
                                 "Troque manualmente e clique 'Continuar'.")
                        self.pausar_sem_isca.set()
                        self.input.mouse_up()
                        self._definir_estado(BotState.PAUSADO_ISCA)
                        self._agendar_ui(lambda: self.btn_continuar_isca.config(state="normal"))
                        self.set_status("pausado - sem isca", "orange")

                    leituras_zeradas = 0

                time.sleep(5.0)

    # --- hotkey global -----------------------------------------------------

    def _registrar_hotkey_emergencia(self):
        try:
            self._hotkey_handle = keyboard.add_hotkey(HOTKEY_PARAR, self.parar_macro)
        except Exception as e:
            self.log(f"Aviso: nao foi possivel registrar {HOTKEY_PARAR.upper()} "
                     f"(tente rodar como administrador). Erro: {e}")


def main():
    root = tk.Tk()
    app = FishingBotApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
