"""무전기 노드 하나.

파이 한 대가 이 프로세스 하나를 돌린다. PC 테스트에서는 포트만 바꿔 두 개를 띄운다.

상대 노드와는 UDP 소켓 하나로 통신한다.
  - 제어 패킷  b"C" + JSON        상태(방해금지·카메라·모드), 발언권 요청/허가/거절/반납
  - 음성 패킷  b"A" + seq(4바이트) + PCM 16kHz mono int16 20ms

같은 공간의 탭들은 이 노드의 웹페이지(/)에 붙고, WebSocket(/ws)으로 PTT·토글을 보낸다.
"""

import argparse
import asyncio
import itertools
import json
import logging
import secrets
import socket
import struct
import time
from collections import deque
from pathlib import Path

import aiohttp
import numpy as np
from aiohttp import WSMsgType, web

from walkie.camera import Camera, FrameHub

log = logging.getLogger("walkie")

RATE = 16000
FRAME = 320                 # 20ms
FRAME_BYTES = FRAME * 2

HEARTBEAT = 1.0             # 상태를 이 간격으로 상대에게 보낸다
PEER_TIMEOUT = 3.5          # 이만큼 상태가 안 오면 상대가 꺼진 것
REQ_TIMEOUT = 1.0           # 발언권 요청에 이 안에 답이 없으면 포기
RX_TIMEOUT = 1.5            # 수신 중 음성이 이만큼 끊기면 상대가 사라진 것
MAX_TX = 120                # 버튼이 눌린 채 방치되는 경우를 막는 상한(초)

PKT_CTRL = b"C"
PKT_AUDIO = b"A"

IDLE, REQUESTING, TX, RX = "idle", "requesting", "tx", "rx"

STATIC = Path(__file__).parent / "static"


def make_tone(spec):
    """[(주파수Hz, 길이ms), ...] → 20ms 프레임(bytes) 목록. 주파수 0 은 무음."""
    parts = []
    for freq, ms in spec:
        n = int(RATE * ms / 1000)
        if freq == 0:
            parts.append(np.zeros(n))
            continue
        t = np.arange(n) / RATE
        wave = 0.25 * np.sin(2 * np.pi * freq * t)
        fade = min(80, n // 4)          # 딸깍 소리 방지
        if fade:
            ramp = np.linspace(0, 1, fade)
            wave[:fade] *= ramp
            wave[-fade:] *= ramp[::-1]
        parts.append(wave)
    pcm = np.concatenate(parts)
    pad = (-len(pcm)) % FRAME
    pcm = np.concatenate([pcm, np.zeros(pad + FRAME)])   # 꼬리에 무음 한 프레임
    pcm = (pcm * 32767).astype("<i2")
    return [pcm[i:i + FRAME].tobytes() for i in range(0, len(pcm), FRAME)]


TONE_START = make_tone([(1200, 70)])                            # 말해도 된다
TONE_ROGER = make_tone([(1400, 60), (0, 20), (1000, 90)])       # 상대가 버튼을 뗐다
TONE_BUSY = make_tone([(480, 120), (0, 80)] * 3)                # 보낼 수 없다
TONE_DENY = make_tone([(300, 220)])                             # 연결 문제


class Speaker:
    """수신 음성과 효과음을 섞지 않고 순서대로 재생한다(하프 듀플렉스라 겹칠 일이 없다)."""

    PREBUFFER = 3       # 60ms 모이면 재생 시작 — 네트워크 흔들림 흡수
    MAX_QUEUE = 15      # 300ms 넘게 밀리면 오래된 것부터 버려 지연이 쌓이지 않게

    def __init__(self, device):
        import sounddevice as sd
        self.q = deque()
        self.playing = False
        self.last_cb = time.monotonic()
        self.stream = sd.RawOutputStream(
            samplerate=RATE, channels=1, dtype="int16", blocksize=FRAME,
            device=device, callback=self._callback)
        self.stream.start()

    def push(self, frame):
        self.q.append(frame)
        while len(self.q) > self.MAX_QUEUE:
            self.q.popleft()

    def push_tone(self, frames):
        self.q.extend(frames)

    def clear(self):
        self.q.clear()

    def _callback(self, outdata, frames, t, status):
        self.last_cb = time.monotonic()
        if not self.playing and len(self.q) >= self.PREBUFFER:
            self.playing = True
        if self.playing and self.q:
            outdata[:] = self.q.popleft()
        else:
            outdata[:] = bytes(len(outdata))
            self.playing = False


class NullSpeaker:
    last_cb = 0.0

    def push(self, frame): pass
    def push_tone(self, frames): pass
    def clear(self): pass


class Node(asyncio.DatagramProtocol):
    def __init__(self, args, loop):
        self.args = args
        self.loop = loop
        self.name = args.name
        self.prio = f"{args.name}:{args.port}"     # 동시에 눌렀을 때 누가 이기는지 정하는 값
        host, port = args.peer.rsplit(":", 1)
        self.peer_host = host
        self.peer_port = int(port)
        self.peer_addr = None       # 부팅 직후엔 Tailscale 이 아직 안 떠서 이름을 못 찾을 수 있다
        self._resolve_warned = False
        self._resolving = None
        self.transport = None

        self.mode = IDLE
        self.dnd = False
        self.camera = False

        self.peer = {}
        self.peer_seen = 0.0

        self.req_id = None          # 내가 보낸 발언권 요청(= 송신 세션) id
        self.rx_id = None           # 지금 받고 있는 상대 세션 id
        self.tx_owner = None        # 어느 탭(또는 parrot)이 버튼을 쥐고 있는지
        self.ptt_held = False
        self.tx_started = 0.0
        self.last_rx = 0.0
        self.seq = 0

        self.notice = ""
        self.notice_id = 0
        self.clients = set()
        self._last_ui = None

        self.parrot = args.parrot
        self.parrot_buf = []

        self.use_audio = not (args.parrot or args.no_audio)
        self.audio_ok = None if self.use_audio else True   # None: 아직 한 번도 안 열어 봄
        self.speaker = NullSpeaker()
        self.mic = None
        self.last_mic_cb = 0.0

        self.camera_dev = args.camera       # None 이면 이 무전기엔 카메라가 없다
        self.cam = None
        self.cam_hub = FrameHub()           # 내 카메라
        self.peer_hub = FrameHub()          # 상대 카메라 (상대 노드에서 한 줄기로 받아 로컬 탭들에 나눠 준다)
        self.peer_viewers = 0
        self._relay_task = None
        self.http = None                    # aiohttp.ClientSession, run() 에서 만든다

    async def _resolve_peer(self):
        try:
            infos = await self.loop.getaddrinfo(self.peer_host, self.peer_port,
                                                family=socket.AF_INET, type=socket.SOCK_DGRAM)
            self.peer_addr = infos[0][4][:2]
            log.info("상대 %s → %s:%d", self.peer_host, *self.peer_addr)
        except OSError as e:
            if not self._resolve_warned:
                log.warning("상대 %s 주소를 아직 못 찾습니다 (%s). 계속 다시 시도합니다.", self.peer_host, e)
                self._resolve_warned = True

    # ---------------------------------------------------------------- 오디오
    def open_audio(self):
        """오디오 장치를 (다시) 연다. USB 가 빠졌다 붙으면 PortAudio 가 장치 목록을 새로 읽어야 한다."""
        if not self.use_audio:
            log.info("오디오 장치를 열지 않습니다 (%s)", "parrot" if self.parrot else "--no-audio")
            return
        import sounddevice as sd
        self.close_audio()
        sd._terminate()
        sd._initialize()
        try:
            self.speaker = Speaker(self.args.out_dev)
            self.mic = sd.RawInputStream(
                samplerate=RATE, channels=1, dtype="int16", blocksize=FRAME,
                device=self.args.in_dev, callback=self._mic_callback)
            self.mic.start()
        except Exception as e:      # PortAudioError, ValueError(장치 없음) 등
            self.close_audio()
            if self.audio_ok is not False:
                log.error("오디오 장치를 열 수 없습니다: %s", e)
            self._set_audio_ok(False)
            return
        self.last_mic_cb = time.monotonic()
        log.info("마이크: %s / 스피커: %s",
                 sd.query_devices(self.args.in_dev, "input")["name"],
                 sd.query_devices(self.args.out_dev, "output")["name"])
        self._set_audio_ok(True)

    def close_audio(self):
        for s in (self.mic, getattr(self.speaker, "stream", None)):
            if s is not None:
                try:
                    s.abort()
                    s.close()
                except Exception:
                    pass
        self.mic = None
        self.speaker = NullSpeaker()

    def check_audio(self):
        """콜백이 멈췄으면 장치가 빠진 것이다. 에러 없이 조용히 멈추는 경우가 있어 시간으로 본다."""
        if not self.use_audio:
            return
        now = time.monotonic()
        stale = (self.mic is None
                 or now - self.last_mic_cb > 2.0
                 or now - self.speaker.last_cb > 2.0)
        if stale:
            if self.audio_ok:
                log.error("오디오 장치가 응답하지 않습니다. 다시 엽니다.")
            self.open_audio()

    def _set_audio_ok(self, ok):
        if ok == self.audio_ok:
            return
        first = self.audio_ok is None
        self.audio_ok = ok
        if not ok and self.mode in (TX, REQUESTING):
            self._stop_tx()
        self.send_status()
        if first and ok:
            return
        self.say("오디오 장치가 다시 연결됐습니다" if ok else "마이크나 스피커가 연결되어 있지 않습니다")

    def _mic_callback(self, indata, frames, t, status):
        self.last_mic_cb = time.monotonic()
        # 마이크는 항상 열어 두고 송신 중일 때만 보낸다. 버튼 누를 때 여는 것보다 첫마디가 덜 잘린다.
        if self.mode == TX:
            self.loop.call_soon_threadsafe(self._send_audio, bytes(indata))

    def _send_audio(self, pcm):
        if self.mode != TX or self.transport is None or self.peer_addr is None:
            return
        self.seq = (self.seq + 1) & 0xFFFFFFFF
        self.transport.sendto(PKT_AUDIO + struct.pack(">I", self.seq) + pcm, self.peer_addr)

    # ---------------------------------------------------------------- 상대와 주고받기
    def connection_made(self, transport):
        self.transport = transport

    def error_received(self, exc):
        # 상대가 아직 안 켜졌으면 윈도우에서 ICMP 포트 도달 불가가 여기로 온다. 무시한다.
        log.debug("udp error: %s", exc)

    def send_ctrl(self, msg):
        if self.transport is not None and self.peer_addr is not None:
            self.transport.sendto(PKT_CTRL + json.dumps(msg).encode(), self.peer_addr)

    def send_status(self):
        self.send_ctrl({"t": "status", "name": self.name, "dnd": self.dnd,
                        "camera": self.sharing(), "mode": self.mode, "audio": bool(self.audio_ok),
                        "http": self.args.http})

    def peer_online(self):
        return time.monotonic() - self.peer_seen < PEER_TIMEOUT

    def datagram_received(self, data, addr):
        if self.peer_addr is None or addr[0] != self.peer_addr[0]:
            return                              # 약속된 상대 말고는 받지 않는다
        kind, body = data[:1], data[1:]
        if kind == PKT_AUDIO:
            self._on_audio(body)
        elif kind == PKT_CTRL:
            try:
                msg = json.loads(body)
            except ValueError:
                return
            self._on_ctrl(msg)

    def _on_audio(self, body):
        if self.mode != RX or self.dnd or len(body) != 4 + FRAME_BYTES:
            return
        self.last_rx = time.monotonic()
        pcm = body[4:]
        if self.parrot:
            self.parrot_buf.append(pcm)
        else:
            self.speaker.push(pcm)

    def _on_ctrl(self, msg):
        t = msg.get("t")
        now = time.monotonic()
        if t == "status":
            self.peer = msg
            self.peer_seen = now
            if self.mode in (TX, REQUESTING) and msg.get("dnd"):
                self._stop_tx()
                self.say("상대가 방해금지를 켰습니다", TONE_BUSY)
            elif self.mode == TX and msg.get("mode") != RX and now - self.tx_started > 2:
                # 상대가 재시작하는 등으로 내 세션을 잊어버렸다
                self._stop_tx()
                self.say("상대와 연결이 끊겼습니다", TONE_DENY)
            self.push_ui()
        elif t == "req":
            self._on_req(msg)
        elif t == "grant":
            self._on_grant(msg)
        elif t == "deny":
            if self.mode == REQUESTING and msg.get("id") == self.req_id:
                self.mode = IDLE
                self.req_id = None
                self.tx_owner = None
                reason = msg.get("reason")
                self.say("상대가 방해금지 중입니다" if reason == "dnd" else "상대가 말하는 중입니다",
                         TONE_BUSY)
        elif t == "release":
            if self.mode == RX and msg.get("id") == self.rx_id:
                self._end_rx(TONE_ROGER)

    def _on_req(self, msg):
        rid = msg.get("id")
        if self.dnd:
            self.send_ctrl({"t": "deny", "id": rid, "reason": "dnd"})
            return
        if self.mode == RX and rid == self.rx_id:
            self.send_ctrl({"t": "grant", "id": rid})         # 허가가 유실돼 다시 온 요청
            return
        if self.mode == REQUESTING:
            if self.prio < msg.get("prio", ""):
                self.send_ctrl({"t": "deny", "id": rid, "reason": "busy"})
                return
            # 동시에 눌렀고 상대가 우선이다. 내 요청은 접는다.
            self.req_id = None
            self.tx_owner = None
            self.ptt_held = False
            self.say("상대가 먼저 눌렀습니다")
        elif self.mode != IDLE:
            self.send_ctrl({"t": "deny", "id": rid, "reason": "busy"})
            return
        self.mode = RX
        self.rx_id = rid
        self.last_rx = time.monotonic()
        self.parrot_buf = []
        self.send_ctrl({"t": "grant", "id": rid})
        self.push_ui()

    def _on_grant(self, msg):
        if self.mode != REQUESTING or msg.get("id") != self.req_id:
            return
        if not self.ptt_held:           # 요청하는 사이에 버튼을 이미 뗐다
            self._stop_tx()
            return
        self.mode = TX
        self.tx_started = time.monotonic()
        self.speaker.push_tone(TONE_START)
        self.push_ui()

    def _end_rx(self, tone=None):
        self.mode = IDLE
        self.rx_id = None
        if tone:
            self.speaker.push_tone(tone)
        self.push_ui()
        if self.parrot and self.parrot_buf:
            frames, self.parrot_buf = self.parrot_buf, []
            asyncio.ensure_future(self._parrot_replay(frames))

    def _stop_tx(self):
        """송신(또는 요청) 중이던 세션을 끝내고 상대에게 알린다."""
        if self.req_id is not None:
            for _ in range(3):              # UDP 라 반납은 여러 번 보낸다
                self.send_ctrl({"t": "release", "id": self.req_id})
        self.mode = IDLE
        self.req_id = None
        self.tx_owner = None
        self.ptt_held = False
        self.push_ui()

    # ---------------------------------------------------------------- PTT·토글 (탭에서 온다)
    def ptt_down(self, owner):
        if self.dnd:
            return self.say("방해금지 중에는 보낼 수 없습니다", TONE_DENY)
        if not self.audio_ok:
            return self.say("마이크나 스피커가 연결되어 있지 않습니다")
        if not self.peer_online():
            return self.say("상대가 연결되어 있지 않습니다", TONE_DENY)
        if self.peer.get("dnd"):
            return self.say("상대가 방해금지 중입니다", TONE_BUSY)
        if self.mode in (TX, REQUESTING):
            return self.say("다른 탭이 송신 중입니다")
        if self.mode == RX:
            return self.say("상대가 말하는 중입니다", TONE_BUSY)
        self.mode = REQUESTING
        self.req_id = secrets.token_hex(4)
        self.tx_owner = owner
        self.ptt_held = True
        self.push_ui()
        asyncio.ensure_future(self._request_loop(self.req_id))

    def ptt_up(self, owner):
        if owner != self.tx_owner:
            return
        if self.mode in (TX, REQUESTING):
            self._stop_tx()

    async def _request_loop(self, rid):
        deadline = time.monotonic() + REQ_TIMEOUT
        while self.mode == REQUESTING and self.req_id == rid:
            if time.monotonic() > deadline:
                self.mode = IDLE
                self.req_id = None
                self.tx_owner = None
                self.ptt_held = False
                return self.say("상대가 응답하지 않습니다", TONE_DENY)
            self.send_ctrl({"t": "req", "id": rid, "prio": self.prio})
            await asyncio.sleep(0.25)

    def set_dnd(self, on):
        self.dnd = on
        if on:
            if self.mode in (TX, REQUESTING):
                self._stop_tx()
            elif self.mode == RX:
                self.mode = IDLE
                self.rx_id = None
                self.speaker.clear()
        self.update_camera()
        self.send_status()
        self.push_ui()

    def set_camera(self, on):
        if on and self.camera_dev is None:
            return self.say("이 무전기에는 카메라가 없습니다")
        self.camera = on
        self.update_camera()
        self.send_status()
        self.push_ui()

    # ---------------------------------------------------------------- 카메라
    def update_camera(self):
        """공유 스위치가 켜져 있고 방해금지가 아닐 때만 카메라 장치를 연다."""
        want = self.camera and not self.dnd and self.camera_dev is not None
        if want and self.cam is None:
            self.cam = Camera(self.camera_dev, self.cam_hub, self.loop, size=self.args.cam_size,
                              fps=self.args.cam_fps, on_fail=self._camera_failed)
            self.cam.start()
        elif not want and self.cam is not None:
            self.cam.stop()
            self.cam = None
            self.cam_hub.clear()

    def _camera_failed(self, why):
        self.cam = None
        self.camera = False
        self.cam_hub.clear()
        self.say(f"카메라 오류: {why}")
        self.send_status()

    def sharing(self):
        return self.cam is not None and not self.dnd

    def can_view_peer(self):
        return (self.peer_online() and bool(self.peer.get("camera")) and not self.peer.get("dnd")
                and not self.dnd and self.peer_addr is not None)

    def ensure_relay(self):
        if self._relay_task is None and self.peer_viewers > 0 and self.can_view_peer():
            self._relay_task = asyncio.ensure_future(self._relay())

    async def _relay(self):
        """상대 노드의 /cam/raw 를 받아 peer_hub 에 넣는다. 보는 탭이 없으면 끊는다."""
        try:
            while self.peer_viewers > 0 and self.can_view_peer():
                url = f"http://{self.peer_addr[0]}:{self.peer.get('http')}/cam/raw"
                try:
                    timeout = aiohttp.ClientTimeout(total=None, connect=3, sock_read=5)
                    async with self.http.get(url, timeout=timeout) as r:
                        if r.status != 200:
                            await asyncio.sleep(1)
                            continue
                        while self.peer_viewers > 0 and self.can_view_peer():
                            n = struct.unpack(">I", await r.content.readexactly(4))[0]
                            if n > 8_000_000:
                                break
                            self.peer_hub.publish(await r.content.readexactly(n))
                except (aiohttp.ClientError, asyncio.IncompleteReadError, asyncio.TimeoutError, OSError) as e:
                    log.debug("영상 중계 끊김: %s", e)
                    await asyncio.sleep(1)
        finally:
            self.peer_hub.clear()
            self._relay_task = None

    def handle_ui(self, cid, d):
        t = d.get("t")
        if t == "ptt":
            (self.ptt_down if d.get("down") else self.ptt_up)(cid)
        elif t == "dnd":
            self.set_dnd(bool(d.get("on")))
        elif t == "camera":
            self.set_camera(bool(d.get("on")))

    # ---------------------------------------------------------------- 테스트용 앵무새
    async def _parrot_replay(self, frames):
        """받은 말을 그대로 되돌려 보낸다. PC 한 대로 왕복을 들어보기 위한 것."""
        await asyncio.sleep(0.5)
        self.ptt_down("parrot")
        for _ in range(40):
            if self.mode != REQUESTING:
                break
            await asyncio.sleep(0.05)
        if self.mode != TX:
            return
        start = time.monotonic()
        for i, pcm in enumerate(frames):
            if self.mode != TX:
                return
            self._send_audio(pcm)
            delay = start + (i + 1) * FRAME / RATE - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
        self.ptt_up("parrot")

    # ---------------------------------------------------------------- UI 상태
    def say(self, text, tone=None):
        log.info("알림: %s", text)
        self.notice = text
        self.notice_id += 1
        if tone:
            self.speaker.push_tone(tone)
        self.push_ui()

    def ui_state(self):
        online = self.peer_online()
        p = self.peer if online else {}
        return {
            "t": "state", "name": self.name, "mode": self.mode,
            "dnd": self.dnd, "camera": self.camera, "parrot": self.parrot,
            "has_camera": self.camera_dev is not None, "sharing": self.sharing(),
            "audio": bool(self.audio_ok),
            "peer": {"online": online, "name": self.peer.get("name", self.peer_host),
                     "dnd": p.get("dnd", False), "camera": p.get("camera", False),
                     "audio": p.get("audio", True),
                     "mode": p.get("mode")},
            "notice": self.notice, "notice_id": self.notice_id,
        }

    def push_ui(self):
        state = self.ui_state()
        if state == self._last_ui:
            return
        self._last_ui = state
        for ws in list(self.clients):
            asyncio.ensure_future(self._send_ws(ws, state))

    @staticmethod
    async def _send_ws(ws, state):
        try:
            await ws.send_json(state)
        except (ConnectionError, RuntimeError):
            pass

    async def tick_loop(self):
        while True:
            now = time.monotonic()
            if self.peer_addr is None and (self._resolving is None or self._resolving.done()):
                self._resolving = asyncio.ensure_future(self._resolve_peer())
            self.check_audio()
            self.send_status()
            if self.mode == RX and now - self.last_rx > RX_TIMEOUT:
                self._end_rx(TONE_ROGER)
            if self.mode == TX:
                if not self.peer_online():
                    self._stop_tx()
                    self.say("상대와 연결이 끊겼습니다", TONE_DENY)
                elif now - self.tx_started > MAX_TX:
                    self._stop_tx()
                    self.say(f"한 번에 {MAX_TX}초까지만 보낼 수 있습니다", TONE_DENY)
            self.push_ui()
            await asyncio.sleep(HEARTBEAT)


# -------------------------------------------------------------------- 웹 (탭)
_client_ids = itertools.count(1)


async def _stream_frames(request, hub, alive):
    """hub 의 프레임을 [길이 4바이트][JPEG] 로 이어 보낸다. alive() 가 거짓이 되면 끝낸다.

    탭은 fetch 로 읽어 캔버스에 그린다(<img> MJPEG 는 멈춰도 알 방법이 없어서 이렇게 했다).
    노드끼리도 같은 형식을 쓴다.
    """
    resp = web.StreamResponse(headers={"Content-Type": "application/octet-stream",
                                       "Cache-Control": "no-cache, no-store"})
    await resp.prepare(request)
    seq = -1
    try:
        while alive():
            frame, seq = await hub.next(seq, timeout=2.0)
            if frame is not None:
                await resp.write(struct.pack(">I", len(frame)) + frame)
    except ConnectionError:
        pass
    return resp


async def cam_local(request):
    """내 카메라 미리보기(같은 공간의 탭용)."""
    node = request.app["node"]
    if not node.sharing():
        return web.Response(status=409, text="카메라 공유가 꺼져 있습니다")
    return await _stream_frames(request, node.cam_hub, node.sharing)


async def cam_peer(request):
    """상대 카메라. 탭이 몇 대든 상대 노드와는 한 줄기만 연다."""
    node = request.app["node"]
    if not node.can_view_peer():
        return web.Response(status=409, text="상대 카메라를 볼 수 없습니다")
    node.peer_viewers += 1
    node.ensure_relay()
    try:
        return await _stream_frames(request, node.peer_hub, node.can_view_peer)
    finally:
        node.peer_viewers -= 1


async def cam_raw(request):
    """상대 노드가 가져가는 내 카메라."""
    node = request.app["node"]
    if node.peer_addr is None or request.remote != node.peer_addr[0]:
        return web.Response(status=403)

    def alive():
        return node.sharing() and not node.peer.get("dnd")

    if not alive():
        return web.Response(status=409)
    return await _stream_frames(request, node.cam_hub, alive)


async def index(request):
    return web.FileResponse(STATIC / "index.html")


async def ws_handler(request):
    node = request.app["node"]
    ws = web.WebSocketResponse(heartbeat=5)     # 탭이 조용히 사라져도 10초 안에 알아챈다
    await ws.prepare(request)
    cid = next(_client_ids)
    node.clients.add(ws)
    await ws.send_json(node.ui_state())
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    node.handle_ui(cid, json.loads(msg.data))
                except ValueError:
                    pass
    finally:
        node.clients.discard(ws)
        node.ptt_up(cid)        # 버튼을 누른 채 끊긴 탭 때문에 송신이 계속되지 않게
    return ws


async def run(args):
    loop = asyncio.get_running_loop()
    node = Node(args, loop)
    await loop.create_datagram_endpoint(lambda: node, local_addr=(args.bind, args.port))
    node.open_audio()
    node.http = aiohttp.ClientSession()

    app = web.Application()
    app["node"] = node
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/cam/local", cam_local)
    app.router.add_get("/cam/peer", cam_peer)
    app.router.add_get("/cam/raw", cam_raw)
    app.router.add_static("/static", STATIC)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, args.bind, args.http).start()

    log.info("[%s] udp :%d → 상대 %s, 웹 http://localhost:%d",
             node.name, args.port, args.peer, args.http)
    await node.tick_loop()


def main():
    ap = argparse.ArgumentParser(description="와이파이 무전기 노드")
    ap.add_argument("--name", help="이 무전기 이름 (두 대가 서로 달라야 한다)")
    ap.add_argument("--peer", help="상대 주소 host:port (Tailscale 이름이나 100.x IP)")
    ap.add_argument("--port", type=int, default=7700, help="상대와 주고받을 UDP 포트")
    ap.add_argument("--http", type=int, default=8080, help="탭이 접속할 웹 포트")
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--in-dev", default=None, help="마이크 장치 번호나 이름 일부")
    ap.add_argument("--out-dev", default=None, help="스피커 장치 번호나 이름 일부")
    ap.add_argument("--no-audio", action="store_true", help="오디오 장치 없이 실행")
    ap.add_argument("--camera", default=None, help="카메라 장치 (OpenCV 번호, 예: 0). 없으면 카메라 없는 무전기")
    ap.add_argument("--cam-size", default="960x540", help="영상 크기 가로x세로")
    ap.add_argument("--cam-fps", type=int, default=12)
    ap.add_argument("--parrot", action="store_true",
                    help="받은 말을 되돌려 보내는 테스트 상대 (오디오 장치를 쓰지 않는다)")
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if args.list_devices:
        import sounddevice as sd
        print(sd.query_devices())
        return
    if not args.name or not args.peer:
        ap.error("--name 과 --peer 가 필요합니다")
    args.cam_size = tuple(int(v) for v in args.cam_size.lower().split("x"))
    for key in ("in_dev", "out_dev"):
        val = getattr(args, key)
        if val is not None and val.isdigit():
            setattr(args, key, int(val))

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass
