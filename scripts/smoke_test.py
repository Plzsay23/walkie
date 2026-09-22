"""PC 한 대에서 노드 A(실제 마이크·스피커)와 B(앵무새)를 띄워 제어 흐름을 자동으로 점검한다.

    uv run python scripts/smoke_test.py

소리는 사람이 들어봐야 하므로 여기서는 상태 전이만 본다.
"""

import asyncio
import subprocess
import sys
import time

import aiohttp

A_HTTP, B_HTTP = 8301, 8302
NODES = [
    ["--name", "A", "--port", "7301", "--peer", "127.0.0.1:7302", "--http", str(A_HTTP)],
    ["--name", "B", "--port", "7302", "--peer", "127.0.0.1:7301", "--http", str(B_HTTP), "--parrot"],
]


class Tab:
    """탭 하나를 흉내낸다. 서버가 보내는 최신 상태를 들고 있다."""

    def __init__(self, session, port):
        self.session, self.port, self.state = session, port, None

    async def open(self):
        self.ws = await self.session.ws_connect(f"http://127.0.0.1:{self.port}/ws")
        self.task = asyncio.ensure_future(self._read())

    async def _read(self):
        async for msg in self.ws:
            self.state = msg.json()

    async def send(self, **kw):
        await self.ws.send_json(kw)

    async def close(self):
        await self.ws.close()

    async def wait(self, pred, what, timeout=4.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.state and pred(self.state):
                return self.state
            await asyncio.sleep(0.02)
        raise AssertionError(f"{what} 을(를) 기다리다 시간 초과. 마지막 상태: {self.state}")


def ok(msg):
    print("  OK ", msg)


async def scenario():
    async with aiohttp.ClientSession() as s:
        a, b = Tab(s, A_HTTP), Tab(s, B_HTTP)
        await a.open()
        await b.open()

        await a.wait(lambda st: st["peer"]["online"], "A 가 B 를 봄")
        await b.wait(lambda st: st["peer"]["online"], "B 가 A 를 봄")
        ok("서로 연결됨")

        # 1. 보통 송신 → 앵무새가 되돌려 보냄
        await a.send(t="ptt", down=True)
        await a.wait(lambda st: st["mode"] == "tx", "A 송신")
        await b.wait(lambda st: st["mode"] == "rx", "B 수신")
        ok("A 송신 / B 수신")
        await asyncio.sleep(1.0)
        await a.send(t="ptt", down=False)
        await a.wait(lambda st: st["mode"] == "rx", "앵무새 되돌려 보내기", timeout=3)
        ok("버튼 뗌 → 앵무새가 되돌려 보내는 중 (A 수신)")
        await a.wait(lambda st: st["mode"] == "idle", "되돌려 보내기 끝", timeout=4)
        ok("되돌려 보내기 끝 → 대기")

        # 2. 상대 방해금지
        await b.send(t="dnd", on=True)
        await a.wait(lambda st: st["peer"]["dnd"], "A 가 B 의 방해금지를 봄")
        n = a.state["notice_id"]
        await a.send(t="ptt", down=True)
        st = await a.wait(lambda st: st["notice_id"] != n, "거절 알림")
        assert st["mode"] == "idle" and "방해금지" in st["notice"], st
        await a.send(t="ptt", down=False)
        ok(f"B 방해금지 → A 송신 거절: {st['notice']}")

        # 3. 내 방해금지
        await b.send(t="dnd", on=False)
        await a.send(t="dnd", on=True)
        await b.wait(lambda st: st["peer"]["dnd"], "B 가 A 의 방해금지를 봄")
        n = b.state["notice_id"]
        await b.send(t="ptt", down=True)
        st = await b.wait(lambda st: st["notice_id"] != n, "거절 알림")
        assert st["mode"] == "idle", st
        await b.send(t="ptt", down=False)
        await a.send(t="dnd", on=False)
        await a.wait(lambda st: not st["dnd"] and not st["peer"]["dnd"], "방해금지 해제")
        ok("A 방해금지 → B 송신 거절, 해제 후 정상")

        # 4. 송신 도중 상대가 방해금지 → 즉시 끊김
        await a.send(t="ptt", down=True)
        await b.wait(lambda st: st["mode"] == "rx", "B 수신")
        await b.send(t="dnd", on=True)
        await a.wait(lambda st: st["mode"] == "idle", "A 송신 중단")
        await b.wait(lambda st: st["mode"] == "idle", "B 수신 중단")
        await a.send(t="ptt", down=False)
        await b.send(t="dnd", on=False)
        await a.wait(lambda st: not st["peer"]["dnd"], "해제")
        ok("송신 중 상대 방해금지 → 양쪽 즉시 대기로")
        await asyncio.sleep(2.0)       # 앵무새가 짧은 조각을 되돌려 보낼 수 있으니 정리될 때까지

        # 5. 동시에 누르기 → 한쪽만 송신
        await a.wait(lambda st: st["mode"] == "idle", "대기", timeout=5)
        await asyncio.gather(a.send(t="ptt", down=True), b.send(t="ptt", down=True))
        await asyncio.sleep(0.8)
        modes = {a.state["mode"], b.state["mode"]}
        assert modes == {"tx", "rx"}, (a.state["mode"], b.state["mode"])
        ok(f"동시에 누름 → A={a.state['mode']}, B={b.state['mode']}")
        await asyncio.gather(a.send(t="ptt", down=False), b.send(t="ptt", down=False))
        await asyncio.sleep(3.0)

        # 6. 탭 여러 대: 다른 탭은 같은 상태를 보고, 누르던 탭이 끊기면 송신이 끝난다
        await a.wait(lambda st: st["mode"] == "idle", "대기", timeout=5)
        a2 = Tab(s, A_HTTP)
        await a2.open()
        await a2.send(t="ptt", down=True)
        await a.wait(lambda st: st["mode"] == "tx", "첫 탭도 송신 상태를 봄")
        await a2.close()
        await a.wait(lambda st: st["mode"] != "tx", "끊긴 탭의 송신 종료")
        ok("누르던 탭이 끊기면 송신 종료, 다른 탭에도 상태 반영")

        await a.close()
        await b.close()


def main():
    procs = [subprocess.Popen([sys.executable, "-m", "walkie", *argv]) for argv in NODES]
    try:
        time.sleep(2.5)
        for p in procs:
            if p.poll() is not None:
                sys.exit("노드가 바로 죽었습니다. 위 로그를 보십시오.")
        asyncio.run(scenario())
        print("\n전부 통과")
    finally:
        for p in procs:
            p.terminate()


if __name__ == "__main__":
    main()
