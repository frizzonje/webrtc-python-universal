#!/usr/bin/env python3
"""
Нативный голос/демонстрация-клиент «Белой Берёзки» для Linux (Ubuntu).

Зачем: браузер на Linux не умеет нормально захватывать СИСТЕМНЫЙ/игровой звук
(а на демонстрации экрана это критично). Этот клиент подключается к тому же
mesh-WebRTC, что и браузерные коллеги, и шлёт им:
  • микрофон     (вы говорите)
  • экран        (x11grab — игра/рабочий стол, выбор «качество/фпс» на лету)
  • звук системы (pulse monitor — то, что слышите вы: игра, музыка)
и проигрывает голос собеседников вам в колонки/наушники.

Совместим с сервером один-в-один: тот же socket.io-контракт (@bb/shared),
тот же HMAC-пропуск (кука bb_pass), та же перфект-негоциация mesh.

Дизайн без «glare»: вы заходите в УЖЕ собранную комнату (newcomer) и шлёте
offer всем, кто там сидит, — это покрывает сценарий показа целиком. Поздним
гостям, зашедшим после вас, мы отвечаем и до-догоняем экран отдельным offer'ом.

Этот модуль — движок (Engine). GUI живёт в bb_gui.py. CLI:
  python3 bb_native.py --room general --name "Командир" --mode quality
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Callable, Dict, List, Optional

import aiohttp
import av
import socketio
from aiortc import (
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
)
from aiortc.contrib.media import MediaPlayer, MediaRelay
from aiortc.sdp import candidate_from_sdp


# Тип колбэка событий для GUI: (kind, data). kind ∈
# log|status|state|peers|mode|mic. data — строка/список/bool.
EventCb = Callable[[str, object], None]


MODE_PRESETS = {
    "quality": (1920, 1080, 60),  # упор на детализацию
    "fps": (1280, 720, 30),       # упор на плавность (меньше нагрузка на CPU)
}


# ─────────────────────────────────────────────────────────────────────────
# Платформа. На Linux захват идёт через x11grab/pulse, на macOS — через
# avfoundation, проигрывание входящего голоса — через sounddevice (кросс-платф).
# ─────────────────────────────────────────────────────────────────────────

IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")


def _ffmpeg_bin() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


def avf_list_devices():
    """macOS: список устройств avfoundation.

    Возвращает (video, audio), где каждый — список (index:int, name:str).
    video = экраны/камеры (для демонстрации), audio = микрофоны/мониторы.
    """
    video: List = []
    audio: List = []
    if not IS_MAC:
        return video, audio
    try:
        r = subprocess.run(
            [_ffmpeg_bin(), "-hide_banner", "-f", "avfoundation",
             "-list_devices", "true", "-i", ""],
            capture_output=True, text=True, timeout=8,
        )
        out = r.stderr or ""
    except Exception:  # noqa: BLE001
        return video, audio
    section = None
    for ln in out.splitlines():
        if "AVFoundation video devices" in ln:
            section = "v"
            continue
        if "AVFoundation audio devices" in ln:
            section = "a"
            continue
        m = re.search(r"\]\s+\[(\d+)\]\s+(.+?)\s*$", ln)
        if m and section:
            idx, name = int(m.group(1)), m.group(2)
            (video if section == "v" else audio).append((idx, name))
    return video, audio


def mac_screen_permission(request: bool = False) -> bool:
    """macOS: есть ли разрешение «Запись экрана» у текущего процесса.

    Без него avfoundation не отдаёт кадры (зависает), поэтому проверяем заранее.
    request=True — попутно показать системный диалог запроса (один раз).
    Если проверить не удалось — возвращаем True, чтобы не мешать.
    """
    if not IS_MAC:
        return True
    try:
        import ctypes
        cg = ctypes.CDLL(
            "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        if request and hasattr(cg, "CGRequestScreenCaptureAccess"):
            cg.CGRequestScreenCaptureAccess.restype = ctypes.c_bool
            cg.CGRequestScreenCaptureAccess()
        if hasattr(cg, "CGPreflightScreenCaptureAccess"):
            cg.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
            return bool(cg.CGPreflightScreenCaptureAccess())
    except Exception:  # noqa: BLE001
        return True
    return True


def sd_output_devices():
    """Список (index:int, name:str) устройств вывода для проигрывания голоса."""
    try:
        import sounddevice as sd
        outs = []
        for i, d in enumerate(sd.query_devices()):
            if d.get("max_output_channels", 0) > 0:
                outs.append((i, d["name"]))
        return outs
    except Exception:  # noqa: BLE001
        return []


# ─────────────────────────────────────────────────────────────────────────
# Пропуск bb_pass: HMAC-SHA256(exp, key='bb-auth-v1:'+SITE_PASSWORD) — формат
# совпадает байт-в-байт с packages/shared/src/auth.ts / api auth.ts.
# ─────────────────────────────────────────────────────────────────────────

TOKEN_TTL_MS = 30 * 24 * 60 * 60 * 1000


def issue_token(password: str) -> str:
    exp = int(time.time() * 1000) + TOKEN_TTL_MS
    key = ("bb-auth-v1:" + password).encode()
    sig = hmac.new(key, str(exp).encode(), hashlib.sha256).digest()
    b64 = base64.urlsafe_b64encode(sig).decode().rstrip("=")  # base64url без паддинга
    return f"{exp}.{b64}"


# ─────────────────────────────────────────────────────────────────────────
# ICE-серверы: тянем GET /api/config (там STUN + ваш TURN из .env), как браузер.
# ─────────────────────────────────────────────────────────────────────────

FALLBACK_ICE = [RTCIceServer(urls=["stun:stun.l.google.com:19302"])]


async def fetch_ice(server: str, token: str, verify_tls: bool) -> List[RTCIceServer]:
    url = server.rstrip("/") + "/api/config"
    ssl_ctx = None if verify_tls else False
    try:
        connector = aiohttp.TCPConnector(ssl=ssl_ctx)
        async with aiohttp.ClientSession(connector=connector) as s:
            async with s.get(url, headers={"Cookie": f"bb_pass={token}"}) as r:
                data = await r.json()
        servers = []
        for ice in data.get("iceServers", []):
            urls = ice.get("urls")
            if not urls:
                continue
            servers.append(
                RTCIceServer(urls=urls, username=ice.get("username"),
                             credential=ice.get("credential"))
            )
        if servers:
            return servers
    except Exception:  # noqa: BLE001
        pass
    return FALLBACK_ICE


# ─────────────────────────────────────────────────────────────────────────
# Поднятие SDP-битрейта (как boostVideoBitrate в вебе) — подсказка приёмнику
# держать высокий битрейт сразу, без «разгона с 360p».
# ─────────────────────────────────────────────────────────────────────────

def boost_bitrate(sdp: str, start_kbps: int, max_kbps: int) -> str:
    lines = sdp.split("\r\n")
    video_pts = set()
    for ln in lines:
        m = re.match(r"^a=rtpmap:(\d+) (VP8|VP9|H264|H265|AV1)\b", ln, re.I)
        if m:
            video_pts.add(m.group(1))
    if not video_pts:
        return sdp
    extra = (f";x-google-start-bitrate={start_kbps}"
             f";x-google-min-bitrate=1200;x-google-max-bitrate={max_kbps}")
    out = []
    for ln in lines:
        m = re.match(r"^a=fmtp:(\d+) ", ln)
        if m and m.group(1) in video_pts and "x-google-start-bitrate" not in ln:
            ln = ln + extra
        out.append(ln)
    return "\r\n".join(out)


# ─────────────────────────────────────────────────────────────────────────
# Захват: микрофон, экран (x11grab), звук системы (pulse monitor).
# Один источник — много пиров → раздаём через MediaRelay.
# ─────────────────────────────────────────────────────────────────────────

class CaptureError(RuntimeError):
    pass


class Capture:
    def __init__(self, args, log: EventCb):
        self.args = args
        self.log = log
        self.relay = MediaRelay()
        self.mic_src = None
        self.screen_src = None
        self.sys_src = None
        self._screen_player = None
        self._mic_player = None
        self._sys_player = None
        self.display = args.display
        self.w, self.h, self.fps = args.width, args.height, args.fps

        # --- Экран --- без него смысла нет → это фатально
        self._start_screen(self.w, self.h, self.fps)

        # --- Микрофон ---
        if not args.no_mic:
            try:
                self._mic_player = self._open_mic()
                self.mic_src = self._mic_player.audio
                self.log("log", f"[mic] {args.mic_source}")
            except Exception as e:  # noqa: BLE001
                self.log("log", f"[mic] недоступен ({e}) — без микрофона")

        # --- Звук системы / игры ---
        if not args.no_system_audio and args.system_source:
            try:
                self._sys_player = self._open_system()
                self.sys_src = self._sys_player.audio
                self.log("log", f"[sysaudio] {args.system_source}")
            except Exception as e:  # noqa: BLE001
                self.log("log", f"[sysaudio] недоступен ({e}) — без звука системы")

    # --- открытие источников по платформе ---------------------------------
    def _open_mic(self):
        if IS_MAC:
            return MediaPlayer(f":{self.args.mic_source}", format="avfoundation",
                               options={"sample_rate": "48000", "channels": "1"})
        return MediaPlayer(self.args.mic_source, format="pulse",
                           options={"sample_rate": "48000", "channels": "1"})

    def _open_system(self):
        if IS_MAC:
            return MediaPlayer(f":{self.args.system_source}", format="avfoundation",
                               options={"sample_rate": "48000", "channels": "2"})
        return MediaPlayer(self.args.system_source, format="pulse",
                           options={"sample_rate": "48000", "channels": "2"})

    def _open_screen(self, w, h, fps):
        if IS_MAC:
            # avfoundation захватывает экран в нативном разрешении; режим меняет
            # частоту кадров (и битрейт), масштабирование тут недоступно.
            opts = {"framerate": str(fps), "capture_cursor": "1"}
            return MediaPlayer(f"{self.args.screen_source}:none",
                               format="avfoundation", options=opts)
        opts = {"video_size": f"{w}x{h}", "framerate": str(fps),
                "draw_mouse": "1", "probesize": "32", "thread_queue_size": "512"}
        return MediaPlayer(self.display, format="x11grab", options=opts)

    def _start_screen(self, w, h, fps):
        # На macOS без разрешения «Запись экрана» avfoundation просто зависает,
        # не отдавая кадры. Поэтому проверяем заранее и сразу сообщаем понятно.
        if IS_MAC and not mac_screen_permission(request=True):
            raise CaptureError(
                "Нет доступа к «Записи экрана». Разреши его приложению, из которого "
                "запущен клиент (обычно «Терминал» или «Python»):\n\n"
                "Системные настройки → Конфиденциальность и безопасность → "
                "Запись экрана → включи галочку, затем перезапусти клиент.")
        try:
            p = self._open_screen(w, h, fps)
        except Exception as e:  # noqa: BLE001
            if IS_MAC:
                raise CaptureError(
                    f"Не удалось захватить экран (источник «{self.args.screen_source}»): {e}\n\n"
                    "Разреши «Запись экрана» приложению, из которого запущен клиент "
                    "(Системные настройки → Конфиденциальность и безопасность → Запись экрана), "
                    "и перезапусти клиент."
                ) from e
            raise CaptureError(
                f"Не удалось захватить экран ({self.display}, {w}x{h}@{fps}): {e}. "
                f"На Wayland войдите в сессию «Ubuntu on Xorg» — см. README."
            ) from e
        self._screen_player = p
        self.screen_src = p.video
        self.w, self.h, self.fps = w, h, fps
        src = self.args.screen_source if IS_MAC else self.display
        self.log("log", f"[screen] {src} @{fps}")

    def restart_screen(self, w, h, fps):
        old = self._screen_player
        self._start_screen(w, h, fps)
        if old is not None:
            try:
                old.video.stop()
            except Exception:  # noqa: BLE001
                pass

    def make_mic_track(self):
        return self.relay.subscribe(self.mic_src, buffered=False) if self.mic_src else None

    def make_screen_track(self):
        return self.relay.subscribe(self.screen_src, buffered=False)

    def make_sys_track(self):
        return self.relay.subscribe(self.sys_src, buffered=False) if self.sys_src else None

    def tracks_for_peer(self):
        """Свежие дорожки для нового пира в ПРАВИЛЬНОМ порядке.

        микрофон → видео экрана → звук системы. Браузерный микшер раскладывает
        первую аудио-дорожку как «голос», вторую — как «звук демонстрации».
        Возвращает список (role, track).
        """
        out = []
        if self.mic_src:
            out.append(("mic", self.make_mic_track()))
        out.append(("screen", self.make_screen_track()))
        if self.sys_src:
            out.append(("sys", self.make_sys_track()))
        return out


# ─────────────────────────────────────────────────────────────────────────
# Проигрывание входящего голоса собеседников через sounddevice (PortAudio) —
# работает одинаково на macOS и Linux. sink = индекс/имя устройства вывода
# (None → системное по умолчанию).
# ─────────────────────────────────────────────────────────────────────────

class Playback:
    def __init__(self, sink, log: EventCb):
        self.sink = sink  # int|str|None — устройство sounddevice
        self.log = log
        try:
            import sounddevice as sd
            self.sd = sd
            import numpy as np
            self.np = np
        except Exception as e:  # noqa: BLE001
            self.sd = None
            self.np = None
            self.log("log", f"[play] sounddevice/numpy недоступны ({e}) — "
                            "вы НЕ услышите собеседников")

    async def play_track(self, track):
        if self.sd is None:
            try:
                while True:
                    await track.recv()
            except Exception:  # noqa: BLE001
                pass
            return
        loop = asyncio.get_event_loop()
        try:
            stream = self.sd.OutputStream(samplerate=48000, channels=2, dtype="int16",
                                          device=self.sink, blocksize=0, latency="low")
            stream.start()
        except Exception as e:  # noqa: BLE001
            self.log("log", f"[play] не открыть устройство вывода ({e})")
            return
        resampler = av.AudioResampler(format="s16", layout="stereo", rate=48000)
        try:
            while True:
                frame = await track.recv()
                for r in resampler.resample(frame):
                    data = r.to_ndarray().reshape(-1, 2)
                    await loop.run_in_executor(None, stream.write, data)
        except Exception:  # noqa: BLE001 — дорожка кончилась/закрылась
            pass
        finally:
            for fn in (stream.stop, stream.close):
                try:
                    fn()
                except Exception:  # noqa: BLE001
                    pass


# ─────────────────────────────────────────────────────────────────────────
# Один собеседник = одно RTCPeerConnection.
# ─────────────────────────────────────────────────────────────────────────

class Peer:
    def __init__(self, peer_id: str, name: str, pc: RTCPeerConnection):
        self.id = peer_id
        self.name = name
        self.pc = pc
        self.pending_ice: List[dict] = []
        self.renegotiated = False
        self.mic_sender = None
        self.video_sender = None


# ─────────────────────────────────────────────────────────────────────────
# Главный клиент: socket.io + mesh.
# ─────────────────────────────────────────────────────────────────────────

class NativeClient:
    def __init__(self, args, ice, cap: Capture, play: Playback, on_event: EventCb):
        self.args = args
        self.ice = ice
        self.cap = cap
        self.play = play
        self.on_event = on_event
        self.peers: Dict[str, Peer] = {}
        self.tasks: set = set()
        self.mic_on = not args.no_mic
        self.mode = args.mode
        self.sio = socketio.AsyncClient(ssl_verify=args.verify_tls, logger=False,
                                        engineio_logger=False, reconnection=True)
        self._wire_handlers()

    def _log(self, msg: str):
        print(msg)
        self.on_event("log", msg)

    def _emit_peers(self):
        self.on_event("peers", [p.name for p in self.peers.values()])

    def _spawn(self, coro):
        t = asyncio.ensure_future(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)

    # --- создание соединения и обработчиков (без дорожек) ---
    def _new_pc(self, peer_id: str, name: str) -> Peer:
        pc = RTCPeerConnection(RTCConfiguration(iceServers=self.ice))
        peer = Peer(peer_id, name, pc)
        self.peers[peer_id] = peer

        @pc.on("track")
        def on_track(track):  # noqa: ANN001
            if track.kind == "audio":
                self._log(f"[{name}] входящий голос")
                self._spawn(self.play.play_track(track))
            else:
                self._spawn(self._drain(track))

        @pc.on("connectionstatechange")
        async def on_state():  # noqa: ANN001
            self._log(f"[{name}] {pc.connectionState}")
            if pc.connectionState in ("failed", "closed"):
                await self._remove_peer(peer_id)

        return peer

    def _add_tracks(self, peer: Peer):
        # порядок (микрофон → видео → звук системы) критичен для веб-микшера
        for role, track in self.cap.tracks_for_peer():
            sender = peer.pc.addTrack(track)
            if role == "mic":
                peer.mic_sender = sender
                if not self.mic_on:
                    sender.replaceTrack(None)  # зашли в mute — не шлём микрофон
            elif role == "screen":
                peer.video_sender = sender

    async def _drain(self, track):
        try:
            while True:
                await track.recv()
        except Exception:  # noqa: BLE001
            pass

    async def _offer_to(self, peer_id: str, name: str):
        if peer_id in self.peers:
            return
        peer = self._new_pc(peer_id, name)
        self._add_tracks(peer)
        await peer.pc.setLocalDescription(await peer.pc.createOffer())
        sdp = boost_bitrate(peer.pc.localDescription.sdp, self.args.start_bitrate,
                            self.args.max_bitrate)
        await self.sio.emit("offer", {"to": peer_id, "sdp": {"type": "offer", "sdp": sdp}})
        self._log(f"[mesh] offer → {name}")
        self._emit_peers()

    async def _drain_ice(self, peer: Peer):
        for c in peer.pending_ice:
            await self._add_ice(peer, c)
        peer.pending_ice.clear()

    async def _add_ice(self, peer: Peer, payload: dict):
        cand_str = payload.get("candidate")
        if not cand_str:
            return
        s = cand_str[len("candidate:"):] if cand_str.startswith("candidate:") else cand_str
        try:
            cand = candidate_from_sdp(s)
            cand.sdpMid = payload.get("sdpMid")
            cand.sdpMLineIndex = payload.get("sdpMLineIndex")
            await peer.pc.addIceCandidate(cand)
        except Exception as e:  # noqa: BLE001
            self._log(f"[ice] {e}")

    async def _remove_peer(self, peer_id: str):
        peer = self.peers.pop(peer_id, None)
        if not peer:
            return
        try:
            await peer.pc.close()
        except Exception:  # noqa: BLE001
            pass
        self._emit_peers()

    # --- управление из GUI (вызывается в loop'е движка) ---
    async def set_mic(self, on: bool):
        self.mic_on = on
        for peer in self.peers.values():
            if peer.mic_sender:
                peer.mic_sender.replaceTrack(self.cap.make_mic_track() if on else None)
        self._log(f"[mic] {'вкл' if on else 'mute'}")
        self.on_event("mic", on)

    async def set_mode(self, mode: str):
        if mode not in MODE_PRESETS:
            return
        self.mode = mode
        w, h, fps = MODE_PRESETS[mode]
        try:
            self.cap.restart_screen(w, h, fps)
        except CaptureError as e:
            self._log(f"[screen] смена режима не удалась: {e}")
            return
        # подменяем видеодорожку у всех — без переподписания SDP
        for peer in self.peers.values():
            if peer.video_sender:
                peer.video_sender.replaceTrack(self.cap.make_screen_track())
        self._log(f"[screen] режим → {mode} ({w}x{h}@{fps})")
        self.on_event("mode", mode)

    # --- socket.io события ---
    def _wire_handlers(self):
        sio = self.sio

        @sio.event
        async def connect():  # noqa: ANN001
            self._log(f"[socket] подключён sid={sio.get_sid()}")
            await sio.emit("join", {"room": self.args.room, "name": self.args.name})
            self.on_event("state", "connected")
            self.on_event("status", f"В канале «{self.args.room}»")

        @sio.event
        async def disconnect():  # noqa: ANN001
            self._log("[socket] отключён")
            self.on_event("state", "disconnected")

        @sio.on("peers")
        async def on_peers(peers):  # noqa: ANN001
            names = ", ".join(p.get("name") or "?" for p in peers) or "— пусто"
            self._log(f"[mesh] в канале уже {len(peers)}: {names}")
            for p in peers:
                await self._offer_to(p["id"], p.get("name") or "Боец")

        @sio.on("peer-joined")
        async def on_peer_joined(payload):  # noqa: ANN001
            self._log(f"[mesh] +1: {payload.get('name') or 'Боец'} — ждём offer")

        @sio.on("offer")
        async def on_offer(payload):  # noqa: ANN001
            frm = payload["from"]
            name = payload.get("name") or "Боец"
            if frm in self.peers:
                return  # мы уже оферент — игнор, glare нет
            self._log(f"[mesh] offer ← {name}, отвечаю")
            peer = self._new_pc(frm, name)
            sdp = payload["sdp"]
            await peer.pc.setRemoteDescription(
                RTCSessionDescription(sdp=sdp["sdp"], type=sdp["type"]))
            await self._drain_ice(peer)
            await peer.pc.setLocalDescription(await peer.pc.createAnswer())
            await sio.emit("answer", {"to": frm,
                                      "sdp": {"type": "answer",
                                              "sdp": peer.pc.localDescription.sdp}})
            self._add_tracks(peer)  # экран/звук уедут до-offer'ом
            self._emit_peers()
            self._spawn(self._renegotiate(frm))

        @sio.on("answer")
        async def on_answer(payload):  # noqa: ANN001
            peer = self.peers.get(payload["from"])
            if not peer or peer.pc.signalingState != "have-local-offer":
                return
            sdp = payload["sdp"]
            await peer.pc.setRemoteDescription(
                RTCSessionDescription(sdp=sdp["sdp"], type=sdp["type"]))
            await self._drain_ice(peer)
            self._log(f"[mesh] answer ← {peer.name}")

        @sio.on("ice-candidate")
        async def on_ice(payload):  # noqa: ANN001
            peer = self.peers.get(payload["from"])
            if not peer:
                return
            cand = payload.get("candidate") or {}
            if peer.pc.remoteDescription:
                await self._add_ice(peer, cand)
            else:
                peer.pending_ice.append(cand)

        @sio.on("peer-left")
        async def on_peer_left(payload):  # noqa: ANN001
            await self._remove_peer(payload["id"])

    async def _renegotiate(self, peer_id: str):
        for _ in range(40):  # до ~10 c
            await asyncio.sleep(0.25)
            peer = self.peers.get(peer_id)
            if not peer or peer.renegotiated:
                return
            if peer.pc.signalingState == "stable":
                peer.renegotiated = True
                try:
                    await peer.pc.setLocalDescription(await peer.pc.createOffer())
                    sdp = boost_bitrate(peer.pc.localDescription.sdp,
                                        self.args.start_bitrate, self.args.max_bitrate)
                    await self.sio.emit("offer", {"to": peer_id,
                                                  "sdp": {"type": "offer", "sdp": sdp}})
                    self._log(f"[mesh] до-offer (экран/звук) → {peer.name}")
                except Exception as e:  # noqa: BLE001
                    self._log(f"[mesh] ренеготиация не удалась: {e}")
                return

    async def run(self):
        token = issue_token(self.args.password)
        url = self.args.server.rstrip("/")
        self.on_event("state", "connecting")
        self.on_event("status", f"Подключаюсь к {url}…")
        self._log(f"[socket] connect {url}")
        await self.sio.connect(url, headers={"Cookie": f"bb_pass={token}"},
                               transports=["websocket"], socketio_path="socket.io")
        await self.sio.wait()

    async def shutdown(self):
        for pid in list(self.peers):
            await self._remove_peer(pid)
        try:
            await self.sio.disconnect()
        except Exception:  # noqa: BLE001
            pass


# ─────────────────────────────────────────────────────────────────────────
# Engine: владеет своим asyncio-loop'ом в фоновом потоке. GUI дёргает
# потокобезопасные методы; движок шлёт события через on_event.
# ─────────────────────────────────────────────────────────────────────────

class Engine:
    def __init__(self, args, on_event: EventCb):
        self.args = args
        self.on_event = on_event
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.client: Optional[NativeClient] = None
        self.thread: Optional[threading.Thread] = None

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._main())
        except CaptureError as e:
            self.on_event("log", f"[fatal] {e}")
            self.on_event("state", "error")
            self.on_event("error", str(e))
        except Exception as e:  # noqa: BLE001
            self.on_event("log", f"[fatal] {e}")
            self.on_event("state", "error")
            self.on_event("error", f"Не удалось подключиться: {e}")
        finally:
            try:
                self.loop.run_until_complete(asyncio.sleep(0))
            except Exception:  # noqa: BLE001
                pass

    async def _main(self):
        self.on_event("status", "Получаю ICE-конфиг…")
        ice = await fetch_ice(self.args.server, issue_token(self.args.password),
                              self.args.verify_tls)
        self.on_event("status", "Запускаю захват экрана/звука…")
        cap = Capture(self.args, self.on_event)
        play = Playback(self.args.play_sink, self.on_event)
        self.client = NativeClient(self.args, ice, cap, play, self.on_event)
        await self.client.run()

    # --- потокобезопасные команды из GUI ---
    def _submit(self, coro):
        if self.loop and self.client:
            return asyncio.run_coroutine_threadsafe(coro, self.loop)
        return None

    def set_mic(self, on: bool):
        if self.client:
            self._submit(self.client.set_mic(on))

    def set_mode(self, mode: str):
        if self.client:
            self._submit(self.client.set_mode(mode))

    def stop(self):
        if self.loop and self.client:
            fut = self._submit(self.client.shutdown())
            try:
                if fut:
                    fut.result(timeout=5)
            except Exception:  # noqa: BLE001
                pass
        if self.loop:
            self.loop.call_soon_threadsafe(self.loop.stop)


# ─────────────────────────────────────────────────────────────────────────
# Вспомогательные команды по аудио (диагностика и анти-эхо setup).
# ─────────────────────────────────────────────────────────────────────────

def find_blackhole():
    """macOS: (index_str, name) виртуального устройства BlackHole или (None, None)."""
    _, auds = avf_list_devices()
    for idx, name in auds:
        if "blackhole" in name.lower():
            return str(idx), name
    return None, None


def list_audio():
    if IS_MAC:
        vids, auds = avf_list_devices()
        print("=== ЭКРАНЫ / КАМЕРЫ (для демонстрации, --screen-source) ===")
        for idx, name in vids:
            print(f"  [{idx}] {name}")
        print("\n=== АУДИО-ВХОДЫ (микрофон/звук системы) ===")
        for idx, name in auds:
            print(f"  [{idx}] {name}")
        print("\n=== ВЫХОДЫ (наушники/колонки, --play-sink) ===")
        for idx, name in sd_output_devices():
            print(f"  [{idx}] {name}")
        bh, bhname = find_blackhole()
        print("\nЗвук системы на macOS: установи BlackHole (brew install blackhole-2ch),"
              if not bh else f"\nЗвук системы: найден «{bhname}» (--system-source {bh}).")
        if not bh:
            print("создай Multi-Output Device в «Audio MIDI Setup» и выбери BlackHole "
                  "источником системного звука.")
        return
    print("=== ИСТОЧНИКИ (sources, для микрофона/захвата) ===")
    subprocess.run(["pactl", "list", "short", "sources"], check=False)
    print("\n=== ПРИЁМНИКИ (sinks, для проигрывания) ===")
    subprocess.run(["pactl", "list", "short", "sinks"], check=False)
    print("\nПодсказка: системный звук = '<имя_sink>.monitor' или '@DEFAULT_MONITOR@'.")


def setup_sink():
    if IS_MAC:
        bh, bhname = find_blackhole()
        if bh:
            print(f"BlackHole уже установлен: «{bhname}» (индекс {bh}).\n\n"
                  "Чтобы транслировать звук системы И слышать его самому:\n"
                  "  1) Открой «Audio MIDI Setup» (Настройка Audio-MIDI).\n"
                  "  2) ➕ → «Создать устройство с несколькими выходами» (Multi-Output Device).\n"
                  "  3) Отметь там свои наушники/колонки И BlackHole 2ch.\n"
                  "  4) Сделай это Multi-Output устройством вывода системы.\n"
                  f"  5) В клиенте выбери источник звука системы = «{bhname}».\n")
        else:
            print("BlackHole не найден. Установи виртуальное аудио-устройство:\n\n"
                  "  brew install blackhole-2ch\n\n"
                  "После установки перезапусти клиент и выполни ещё раз эту подсказку.\n"
                  "BlackHole нужен, потому что macOS не даёт захватывать звук системы напрямую.\n")
        return
    print("Создаю виртуальный sink 'bb_stream' + loopback на ваш звук…")
    subprocess.run(["pactl", "load-module", "module-null-sink", "sink_name=bb_stream",
                    "sink_properties=device.description=BB_Stream"], check=False)
    subprocess.run(["pactl", "load-module", "module-loopback",
                    "source=bb_stream.monitor", "sink=@DEFAULT_SINK@",
                    "latency_msec=40"], check=False)
    print(
        "\nГотово. Дальше:\n"
        "  1) В pavucontrol → Playback переведите вывод игры на 'BB_Stream'.\n"
        "  2) Запуск с:  --system-source bb_stream.monitor\n"
        "Откатить:  pactl unload-module module-loopback ; "
        "pactl unload-module module-null-sink\n"
    )


# ─────────────────────────────────────────────────────────────────────────

def _default_display() -> str:
    d = os.environ.get("DISPLAY", ":0")
    return d + ".0" if d.count(".") == 0 else d


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Нативный WebRTC-клиент «Белой Берёзки» (macOS/Linux)")
    ap.add_argument("--server", default=os.environ.get("BB_SERVER", "https://192.168.0.138"))
    ap.add_argument("--password", default=os.environ.get("BB_PASSWORD", "123"))
    ap.add_argument("--room", default="general")
    ap.add_argument("--name", default="Командир (нативка)")
    ap.add_argument("--mode", choices=["quality", "fps"], default="quality")
    ap.add_argument("--width", type=int)
    ap.add_argument("--height", type=int)
    ap.add_argument("--fps", type=int)
    ap.add_argument("--start-bitrate", type=int, default=4000)
    ap.add_argument("--max-bitrate", type=int, default=8000)
    ap.add_argument("--display", default=_default_display())
    # источники: на macOS это индексы avfoundation, на Linux — display / pulse-имена.
    # None → разрешаем автоматически в apply_source_defaults().
    ap.add_argument("--screen-source", default=None)
    ap.add_argument("--mic-source", default=None)
    ap.add_argument("--system-source", default=None)
    ap.add_argument("--play-sink", default=None)
    ap.add_argument("--no-mic", action="store_true")
    ap.add_argument("--no-system-audio", action="store_true")
    ap.add_argument("--insecure", dest="verify_tls", action="store_false", default=False)
    ap.add_argument("--verify-tls", dest="verify_tls", action="store_true")
    ap.add_argument("--list-audio", action="store_true")
    ap.add_argument("--setup-sink", action="store_true")
    return ap


def apply_mode_defaults(args):
    """Если конкретные размеры не заданы — берём их из пресета режима."""
    w, h, fps = MODE_PRESETS[args.mode]
    args.width = args.width or w
    args.height = args.height or h
    args.fps = args.fps or fps
    return args


def apply_source_defaults(args):
    """Подставить источники захвата по платформе, если не заданы явно.

    macOS: индексы avfoundation (экран, микрофон, звук системы = BlackHole).
    Linux: display + pulse-имена.
    """
    if IS_MAC:
        vids, auds = avf_list_devices()
        if args.screen_source is None:
            args.screen_source = next(
                (str(i) for i, n in vids if "capture screen" in n.lower()),
                str(vids[-1][0]) if vids else "1")
        if args.mic_source is None:
            args.mic_source = next(
                (str(i) for i, n in auds if "blackhole" not in n.lower()),
                str(auds[0][0]) if auds else "0")
        if args.system_source is None:
            bh, _ = find_blackhole()
            args.system_source = bh or ""
            if not bh:
                args.no_system_audio = True
    else:
        if args.screen_source is None:
            args.screen_source = args.display
        if args.mic_source is None:
            args.mic_source = "default"
        if args.system_source is None:
            args.system_source = "@DEFAULT_MONITOR@"
    return args


async def _cli_main(args):
    engine_done = asyncio.Event()

    def on_event(kind, data):  # CLI: логи уже печатает _log; здесь тихо
        if kind == "error":
            print(f"[error] {data}")

    ice = await fetch_ice(args.server, issue_token(args.password), args.verify_tls)
    cap = Capture(args, on_event)
    play = Playback(args.play_sink, on_event)
    client = NativeClient(args, ice, cap, play, on_event)

    loop = asyncio.get_event_loop()
    try:
        for s in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(s, engine_done.set)
    except (NotImplementedError, ValueError):
        pass

    runner = asyncio.ensure_future(client.run())
    await engine_done.wait()
    print("\n[exit] выключаюсь…")
    runner.cancel()
    await client.shutdown()


def main():
    args = build_arg_parser().parse_args()
    if args.list_audio:
        list_audio()
        return
    if args.setup_sink:
        setup_sink()
        return
    apply_mode_defaults(args)
    apply_source_defaults(args)
    print("══════════════════════════════════════════════════════")
    print("  Белая Берёзка — нативный клиент (CLI)")
    print(f"  сервер: {args.server}   канал: {args.room}   имя: {args.name}")
    print(f"  захват: {args.width}x{args.height}@{args.fps}  (режим {args.mode})")
    print("══════════════════════════════════════════════════════")
    try:
        asyncio.run(_cli_main(args))
    except (KeyboardInterrupt, CaptureError) as e:
        if isinstance(e, CaptureError):
            print(f"[fatal] {e}")


if __name__ == "__main__":
    main()
