# TS 영상 다운로더

## 지원 방식
| 방식 | 주소 예 | 처리 |
|---|---|---|
| HLS (TS 세그먼트) | `.../index.m3u8` | 세그먼트 병렬 다운로드 → 병합 (AES-128 복호화) |
| HLS (fMP4 세그먼트) | `.../index.m3u8` (`.m4s`) | 초기화 세그먼트 + 조각 병합 |
| MPEG-DASH | `.../manifest.mpd` | 영상·음성 트랙 따로 받아 ffmpeg로 합침 (화질 선택 가능) |
| 직접 파일 | `.mp4` `.webm` `.mkv` `.mov` | 한 번에 다운로드, 끊기면 이어받기 |
| 단일 TS | `.ts` | 그대로 저장 또는 MP4 변환 |
| 웹 페이지 | 영상이 있는 페이지 주소 | 페이지에서 위 주소를 찾아 자동 진행 (m3u8 > mpd > mp4 순) |

DRM(Widevine/PlayReady/FairPlay, ContentProtection)과 라이브 스트림은 지원하지 않습니다.
DASH는 ffmpeg가 없으면 영상(.video.mp4)과 음성(.audio.m4a)을 따로 저장합니다.


## 준비
- Python 3.8 이상
- (선택) mp4 변환: ffmpeg 설치 후 PATH 등록
- (선택) AES-128 암호화 영상: `pip install pycryptodome`

## GUI 버전 (TSDownloader.exe)
- 주소 칸에 **영상이 있는 웹 페이지 주소**를 넣고 [다운로드 시작]을 누르면 페이지(와 그 안의 iframe)에서 m3u8 주소를 찾아 바로 받습니다.
- [페이지에서 찾기]로 주소만 먼저 찾아 "찾은 영상" 목록에서 고를 수도 있습니다.
- 페이지 주소는 자동으로 Referer에 들어갑니다.
- 주소가 스크립트로 숨겨진 사이트, 로그인/DRM이 필요한 영상은 자동으로 찾지 못합니다. 그럴 땐 아래 방법으로 m3u8 주소를 직접 넣으세요.
- MP4 저장을 쓰려면 ffmpeg.exe를 TSDownloader.exe와 같은 폴더에 두세요.
- 직접 빌드: build.bat 더블클릭 (Python 필요) → dist\TSDownloader.exe

## m3u8 주소 직접 찾는 법
1. 브라우저에서 영상 페이지를 열고 F12 → Network 탭
2. 필터에 `m3u8` 입력 후 영상 재생
3. 나온 주소를 우클릭 → Copy URL (`.ts`만 보이면 그 주소도 가능)

## 사용법
```
python hls_download.py "m3u8주소" -o 영상.ts
python hls_download.py "웹페이지주소" -o 영상.mp4 --mp4   # 페이지에서 자동으로 찾기
python hls_download.py "m3u8주소" -o 영상.mp4 --mp4
python hls_download.py "m3u8주소" --list-qualities      # 화질 목록
python hls_download.py "m3u8주소" -q 1 --mp4 -o 영상.mp4   # 화질 선택
python hls_download.py "m3u8주소" --referer "https://사이트주소/" --cookie "이름=값"
```
403 오류가 나면 `--referer`(영상이 재생되던 페이지 주소)나 `--cookie`를 넣어보세요.

DRM(Widevine 등)으로 보호된 영상은 지원하지 않습니다.
