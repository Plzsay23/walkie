# walkie — 라즈베리파이 와이파이 무전기

파이 두 대가 서로 다른 장소·다른 와이파이에서 Tailscale 로 연결된다.
누르고 말하는(PTT) 하프 듀플렉스, 방해금지(음성·영상 완전 차단), 카메라 공유(다음 단계).
PTT·토글은 같은 공간의 탭(여러 대 가능) 브라우저에서 한다.

## PC 에서 테스트

```bash
uv sync
# 노드 A: 이 PC 의 마이크·스피커
uv run python -m walkie --name A --port 7101 --peer 127.0.0.1:7102 --http 8101
# 노드 B: 앵무새 — 받은 말을 0.5초 뒤 그대로 돌려보낸다 (오디오 장치를 쓰지 않음)
#          노트북 카메라를 B 에 달면 A 대시보드에 "상대 카메라"로 나온다
uv run python -m walkie --name B --port 7102 --peer 127.0.0.1:7101 --http 8102 --parrot --camera 0
```

- 카메라는 B 화면(8102)에서 "카메라 공유"를 켜야 나간다. 켤 때만 장치를 열고, 방해금지 중에는 꺼진다.
- 영상은 JPEG 프레임을 이어 보낸다(`[길이 4바이트][JPEG]`). 상대 노드와는 한 줄기만 열고 로컬 탭 여러 대에 나눠 준다.

- http://localhost:8101 에서 버튼(또는 스페이스바)을 누르고 말한 뒤 떼면, 잠시 뒤 내 목소리가 돌아온다.
- http://localhost:8102 에서 B 쪽 방해금지를 켜고 끌 수 있다.
- 앵무새 없이 두 노드 모두 실제 오디오로 돌리려면 **헤드폰**을 써야 한다. 스피커 소리가 마이크로 다시 들어가 울린다.
- 같은 와이파이의 탭에서 `http://<PC IP>:8101` 로도 붙을 수 있다(윈도우 방화벽 허용 필요).

자동 점검(상태 전이만, 소리는 안 들어봄):

```bash
uv run python scripts/smoke_test.py
```

## 파이에 설치

Raspberry Pi OS Lite (64-bit) 권장. 호스트 이름은 `walkie-a`, `walkie-b` 로 한다.
마이크(ReSpeaker Lite)와 USB 스피커를 꽂은 상태에서:

```bash
git clone <이 저장소> ~/walkie && cd ~/walkie
bash scripts/setup_pi.sh A walkie-b      # 파이 B 에서는: bash scripts/setup_pi.sh B walkie-a
sudo tailscale up --hostname=walkie-a    # 처음 한 번, 뜨는 주소로 로그인 (두 파이를 같은 계정으로)
```

스크립트가 하는 일: 패키지·uv·Tailscale 설치, 마이크/스피커 카드를 찾아 `/etc/asound.conf` 기본 장치로 고정,
`walkie.service` 등록(부팅 시 자동 시작, 죽으면 3초 뒤 재시작).
탭에서는 같은 공간의 파이에 `http://walkie-a.local:8080` 으로 붙는다.

USB 마이크/스피커가 빠지면 노드가 2초 안에 알아채고 1초마다 다시 연다. 그동안 탭에는 "마이크/스피커 없음" 이 뜨고
상대 탭에는 "오디오 장치 문제" 가 뜬다.

## 옵션

| 옵션 | 뜻 |
|---|---|
| `--list-devices` | 오디오 장치 목록 |
| `--in-dev`, `--out-dev` | 장치 번호나 이름 일부 |
| `--no-audio` | 오디오 없이 실행 |
| `--camera 0` | 카메라 장치(OpenCV 번호). 빼면 카메라 없는 무전기 |
| `--cam-size`, `--cam-fps` | 영상 크기(기본 960x540)와 초당 프레임(기본 12) |
| `-v` | 디버그 로그 |

## 프로토콜

UDP 소켓 하나. `C`+JSON 은 제어(`status` 1초마다, `req`/`grant`/`deny`/`release`),
`A`+seq+PCM 은 음성(16kHz mono int16, 20ms). 동시에 누르면 `이름:포트` 가 작은 쪽이 이긴다.
