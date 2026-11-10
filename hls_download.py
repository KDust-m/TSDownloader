#!/usr/bin/env python3
"""
범용 영상 다운로더 (HLS m3u8 / TS / MPEG-DASH mpd / 직접 MP4·WebM)

- m3u8 주소를 주면: (마스터 플레이리스트면 화질 선택) -> 세그먼트(.ts) 병렬 다운로드 -> 하나로 병합
- .ts 주소를 직접 주면: 그 파일 하나를 다운로드
- 표준 AES-128(METHOD=AES-128) 암호화 세그먼트 복호화 지원 (pycryptodome 또는 cryptography 설치 시)
- DRM(Widevine/FairPlay/PlayReady, SAMPLE-AES 등)은 지원하지 않습니다.
- ffmpeg가 있으면 --mp4 옵션으로 mp4로 변환(재인코딩 없이 remux)

사용 예:
  python hls_download.py "https://example.com/video/index.m3u8" -o video.ts
  python hls_download.py "https://example.com/master.m3u8" -o video.mp4 --mp4 --referer "https://example.com/"
  python hls_download.py URL --list-qualities
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")


# ---------------------------------------------------------------- HTTP
class Fetcher:
    def __init__(self, headers, retries=5, timeout=30):
        self.headers = headers
        self.retries = retries
        self.timeout = timeout

    def get(self, url, byterange=None):
        headers = dict(self.headers)
        if byterange:
            length, offset = byterange
            headers["Range"] = f"bytes={offset}-{offset + length - 1}"
        last_err = None
        for attempt in range(self.retries):
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    data = r.read()
                    if byterange and r.status == 200:  # 서버가 Range를 무시한 경우 직접 자르기
                        length, offset = byterange
                        data = data[offset:offset + length]
                    return data
            except Exception as e:  # noqa: BLE001
                last_err = e
                code = getattr(e, "code", None)
                if code is not None and 400 <= code < 500 and code not in (408, 429):
                    break  # 403/404 등은 재시도해도 소용없음
                time.sleep(min(2 ** attempt, 10))
        raise RuntimeError(f"다운로드 실패: {url} ({last_err})")

    def get_text(self, url):
        return self.get(url).decode("utf-8", errors="replace")


# ---------------------------------------------------------------- m3u8 파싱
def parse_attrs(s):
    """KEY=VALUE,KEY2="quoted,value" 형태 파싱"""
    return {k: v.strip('"') for k, v in re.findall(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', s)}


def parse_master(text, base):
    variants = []
    lines = [l.strip() for l in text.splitlines()]
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF:"):
            attrs = parse_attrs(line.split(":", 1)[1])
            for nxt in lines[i + 1:]:
                if nxt and not nxt.startswith("#"):
                    variants.append({
                        "url": urljoin(base, nxt),
                        "bandwidth": int(attrs.get("BANDWIDTH", 0) or 0),
                        "resolution": attrs.get("RESOLUTION", "?"),
                    })
                    break
    return variants


def parse_media(text, base):
    """세그먼트 목록 반환: [{url, key, iv, seq, byterange, map}]"""
    segs = []
    seq = 0
    key = None  # {"method", "uri", "iv"}
    init_map = None
    byterange = None
    last_br_end = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            seq = int(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-KEY:"):
            a = parse_attrs(line.split(":", 1)[1])
            method = a.get("METHOD", "NONE")
            if method == "NONE":
                key = None
            elif method == "AES-128" and a.get("KEYFORMAT", "identity") == "identity":
                key = {"method": method, "uri": urljoin(base, a["URI"]), "iv": a.get("IV")}
            else:
                raise SystemExit(
                    f"이 영상은 DRM/지원하지 않는 암호화({method}, {a.get('KEYFORMAT', '')})를 "
                    "사용합니다. 이 프로그램은 DRM 보호 콘텐츠를 다운로드하지 않습니다.")
        elif line.startswith("#EXT-X-MAP:"):
            a = parse_attrs(line.split(":", 1)[1])
            init_map = urljoin(base, a["URI"])
        elif line.startswith("#EXT-X-BYTERANGE:"):
            v = line.split(":", 1)[1]
            if "@" in v:
                length, offset = map(int, v.split("@"))
            else:
                length, offset = int(v), None
            byterange = [length, offset]
        elif not line.startswith("#"):
            url = urljoin(base, line)
            br = None
            if byterange:
                length, offset = byterange
                if offset is None:
                    offset = last_br_end.get(url, 0)
                br = (length, offset)
                last_br_end[url] = offset + length
                byterange = None
            segs.append({"url": url, "key": key, "seq": seq, "byterange": br, "map": init_map})
            seq += 1
    return segs


# ---------------------------------------------------------------- 페이지에서 m3u8 찾기
_M3U8_RE = re.compile(r"""(?:https?:)?//[^\s"'<>()]+?\.m3u8(?:\?[^\s"'<>()]*)?|[^\s"'<>()=:,]+?\.m3u8(?:\?[^\s"'<>()]*)?""", re.I)
_IFRAME_RE = re.compile(r"""<iframe[^>]+src=["']([^"']+)["']""", re.I)


def _clean(u):
    return u.replace("\\/", "/").replace("&amp;", "&")


def find_m3u8_in_page(page_url, fetcher, follow_iframes=True, log=print):
    """웹 페이지 HTML(및 1단계 iframe)에 그대로 적혀 있는 m3u8 주소를 찾습니다.
    스크립트로 숨기거나 암호화한 주소, 로그인/DRM이 필요한 영상은 찾지 못합니다."""
    found = []

    def scan(url, depth):
        try:
            html = fetcher.get_text(url)
        except Exception as e:  # noqa: BLE001
            log(f"페이지 읽기 실패: {url} ({e})")
            return
        html = _clean(html)
        for m in _M3U8_RE.finditer(html):
            raw = _clean(m.group(0))
            if raw.startswith("//"):
                raw = "https:" + raw
            u = urljoin(url, raw)
            if u.startswith("http") and u not in found:
                found.append(u)
        if follow_iframes and depth == 0:
            for src in _IFRAME_RE.findall(html):
                src = _clean(src)
                if src.startswith("//"):
                    src = "https:" + src
                scan(urljoin(url, src), 1)

    scan(page_url, 0)
    return found


def is_media_url(url):
    path = url.split("?")[0].lower()
    return path.endswith((".m3u8", ".ts", ".mpd", ".mp4", ".m4v", ".webm", ".mkv", ".mov", ".flv"))


# ---------------------------------------------------------------- 복호화
def make_decryptor():
    try:
        from Crypto.Cipher import AES  # pycryptodome

        def dec(data, key, iv):
            return AES.new(key, AES.MODE_CBC, iv).decrypt(data)
        return dec
    except ImportError:
        pass
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        def dec(data, key, iv):
            d = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
            return d.update(data) + d.finalize()
        return dec
    except ImportError:
        return None


def unpad(data):
    if data and 1 <= data[-1] <= 16 and data.endswith(bytes([data[-1]]) * data[-1]):
        return data[:-data[-1]]
    return data


# ---------------------------------------------------------------- 메인 로직
def progress(done, total, start):
    pct = done * 100 // total
    bar = "#" * (pct // 4)
    el = time.time() - start
    sys.stdout.write(f"\r[{bar:<25}] {pct:3d}% ({done}/{total}) {el:5.0f}s")
    sys.stdout.flush()


def download_hls(args, fetcher, log=print, on_progress=None, cancel=None):
    text = fetcher.get_text(args.url)
    if not text.lstrip().startswith("#EXTM3U"):
        raise SystemExit("m3u8 플레이리스트가 아닙니다. 주소를 확인하세요.")

    playlist_url = args.url
    variants = parse_master(text, args.url)
    if variants:
        variants.sort(key=lambda v: v["bandwidth"], reverse=True)
        if args.list_qualities:
            for i, v in enumerate(variants):
                log(f"[{i}] {v['resolution']:>10}  {v['bandwidth'] // 1000} kbps  {v['url']}")
            return None
        chosen = variants[0]
        if args.quality is not None:
            chosen = variants[min(args.quality, len(variants) - 1)]
        elif args.resolution:
            m = [v for v in variants if args.resolution in v["resolution"]]
            if m:
                chosen = m[0]
        log(f"선택한 화질: {chosen['resolution']} ({chosen['bandwidth'] // 1000} kbps)")
        playlist_url = chosen["url"]
        text = fetcher.get_text(playlist_url)
    elif args.list_qualities:
        log("화질 선택이 없는 단일 플레이리스트입니다.")
        return None

    segs = parse_media(text, playlist_url)
    if not segs:
        raise SystemExit("세그먼트를 찾지 못했습니다.")
    log(f"세그먼트 {len(segs)}개 다운로드 시작")

    decrypt = None
    key_cache = {}
    if any(s["key"] for s in segs):
        decrypt = make_decryptor()
        if decrypt is None:
            raise SystemExit("AES-128 암호화 영상입니다. 'pip install pycryptodome' 후 다시 실행하세요.")

    tmpdir = tempfile.mkdtemp(prefix="hls_")

    def job(i, s):
        if cancel is not None and cancel.is_set():
            raise RuntimeError("사용자가 취소했습니다.")
        path = os.path.join(tmpdir, f"{i:06d}.ts")
        if os.path.exists(path):
            return
        data = fetcher.get(s["url"], s["byterange"])
        k = s["key"]
        if k:
            if k["uri"] not in key_cache:
                key_cache[k["uri"]] = fetcher.get(k["uri"])
            iv = bytes.fromhex(k["iv"][2:].zfill(32)) if k["iv"] else s["seq"].to_bytes(16, "big")
            data = unpad(decrypt(data, key_cache[k["uri"]], iv))
        with open(path + ".part", "wb") as f:
            f.write(data)
        os.replace(path + ".part", path)

    start = time.time()
    done = 0
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = [ex.submit(job, i, s) for i, s in enumerate(segs)]
            for f in as_completed(futs):
                try:
                    f.result()
                except BaseException:
                    ex.shutdown(wait=False, cancel_futures=True)  # 남은 작업 즉시 중단
                    raise
                done += 1
                (on_progress or progress)(done, len(segs), start)
        if on_progress is None:
            print()

        merged = args.output if not args.mp4 else os.path.join(tmpdir, "merged.ts")
        with open(merged, "wb") as out:
            if segs[0]["map"]:  # fMP4 초기화 세그먼트
                out.write(fetcher.get(segs[0]["map"]))
            for i in range(len(segs)):
                with open(os.path.join(tmpdir, f"{i:06d}.ts"), "rb") as f:
                    shutil.copyfileobj(f, out)
        return merged, tmpdir
    except BaseException:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise


def find_ffmpeg():
    """PATH 또는 실행 파일(.exe) 옆 폴더에서 ffmpeg 찾기"""
    here = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
    for name in ("ffmpeg.exe", "ffmpeg"):
        cand = os.path.join(here, name)
        if os.path.isfile(cand):
            return cand
    return shutil.which("ffmpeg")


def to_mp4(src, dst, log=print):
    ff = find_ffmpeg()
    if not ff:
        raise SystemExit("ffmpeg를 찾을 수 없습니다. ffmpeg 설치 후 다시 실행하거나 --mp4 없이 .ts로 받으세요.")
    log("ffmpeg로 mp4 변환 중...")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run([ff, "-y", "-loglevel", "error", "-i", src, "-c", "copy",
                    "-bsf:a", "aac_adtstoasc", dst], check=True, creationflags=flags)


def main():
    p = argparse.ArgumentParser(description="HLS(m3u8)/TS 영상 다운로더")
    p.add_argument("url", help="m3u8 / mpd / ts / mp4 주소, 또는 영상이 있는 웹 페이지 주소")
    p.add_argument("-o", "--output", default=None, help="저장 파일명 (기본: video.ts / video.mp4)")
    p.add_argument("--mp4", action="store_true", help="ffmpeg로 mp4 변환")
    p.add_argument("-w", "--workers", type=int, default=8, help="동시 다운로드 수 (기본 8)")
    p.add_argument("-q", "--quality", type=int, help="화질 번호 (--list-qualities로 확인, 0=최고)")
    p.add_argument("-r", "--resolution", help="원하는 해상도 예: 1280x720 또는 720")
    p.add_argument("--list-qualities", action="store_true", help="화질 목록만 출력")
    p.add_argument("--referer", help="Referer 헤더")
    p.add_argument("--cookie", help="Cookie 헤더")
    p.add_argument("-H", "--header", action="append", default=[], help="추가 헤더 'Name: value' (반복 가능)")
    p.add_argument("--user-agent", default=DEFAULT_UA)
    args = p.parse_args()

    if not args.output:
        args.output = "video.mp4" if args.mp4 else "video.ts"

    headers = {"User-Agent": args.user_agent}
    if args.referer:
        headers["Referer"] = args.referer
        m = re.match(r"(https?://[^/]+)", args.referer)
        if m:
            headers["Origin"] = m.group(1)
    if args.cookie:
        headers["Cookie"] = args.cookie
    for h in args.header:
        k, _, v = h.partition(":")
        headers[k.strip()] = v.strip()
    fetcher = Fetcher(headers)

    import media_dl
    if args.list_qualities:
        url, _ = media_dl.resolve_page(args.url, fetcher)
        v = media_dl.list_qualities(url, fetcher)
        if not v:
            print("화질 선택이 없는 영상입니다.")
        for i, x in enumerate(v):
            print(f"[{i}] {x['resolution']:>10}  {x['bandwidth'] // 1000} kbps")
        return
    out = media_dl.download_any(args, fetcher)
    print(f"완료: {out}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n중단됨")
        sys.exit(1)
    except RuntimeError as e:
        print(f"\n오류: {e}")
        sys.exit(1)
