"""
HLS 외 추가 방식 지원
- MPEG-DASH (.mpd): SegmentTemplate(Number/Time, SegmentTimeline), SegmentList, SegmentBase(단일 파일)
  영상/음성 트랙을 따로 받아 ffmpeg로 합칩니다.
- 직접 파일 (.mp4 / .webm / .mkv / .mov / .m4v / .ts): 이어받기 지원 스트리밍 다운로드
- 웹 페이지에서 m3u8 / mpd / mp4 등 주소 자동 찾기
DRM(ContentProtection)이 걸린 DASH, 라이브(dynamic) 스트림은 지원하지 않습니다.
"""
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin

import hls_download as core

DIRECT_EXTS = (".mp4", ".m4v", ".webm", ".mkv", ".mov", ".flv")


class DrmError(SystemExit):
    pass


# ---------------------------------------------------------------- 종류 판별
def url_ext(url):
    path = url.split("?")[0].split("#")[0].lower()
    return os.path.splitext(path)[1]


def detect_kind(url, fetcher):
    """'hls' | 'dash' | 'direct' | 'ts' | 'page'"""
    ext = url_ext(url)
    if ext == ".m3u8":
        return "hls"
    if ext == ".mpd":
        return "dash"
    if ext == ".ts":
        return "ts"
    if ext in DIRECT_EXTS:
        return "direct"
    # 확장자가 없으면 앞부분을 읽어 판별
    try:
        head = fetcher.get(url, (4096, 0))[:4096]
    except Exception:  # noqa: BLE001
        return "page"
    t = head.lstrip()
    if t.startswith(b"#EXTM3U"):
        return "hls"
    if b"<MPD" in head[:2048]:
        return "dash"
    if head[4:8] in (b"ftyp", b"moov", b"styp") or head[:4] == b"\x1aE\xdf\xa3":
        return "direct"
    if head[:1] == b"\x47" and len(head) > 188 and head[188:189] == b"\x47":
        return "ts"
    return "page"


# ---------------------------------------------------------------- 페이지에서 찾기
_MEDIA_RE = re.compile(
    r"""(?:https?:)?//[^\s"'<>()]+?\.(?:m3u8|mpd|mp4|m4v|webm)(?:\?[^\s"'<>()]*)?"""
    r"""|[^\s"'<>()=:,]+?\.(?:m3u8|mpd|mp4|m4v|webm)(?:\?[^\s"'<>()]*)?""", re.I)
_IFRAME_RE = re.compile(r"""<iframe[^>]+src=["']([^"']+)["']""", re.I)
_PRIORITY = {".m3u8": 0, ".mpd": 1, ".mp4": 2, ".m4v": 2, ".webm": 3}


def find_media_in_page(page_url, fetcher, follow_iframes=True, log=print):
    """페이지 HTML(및 1단계 iframe)에 적힌 영상 주소 목록 (m3u8 > mpd > mp4 > webm 순).
    스크립트로 숨기거나 암호화한 주소, 로그인/DRM이 필요한 영상은 찾지 못합니다."""
    found = []

    def scan(url, depth):
        try:
            html = core._clean(fetcher.get_text(url))
        except Exception as e:  # noqa: BLE001
            log(f"페이지 읽기 실패: {url} ({e})")
            return
        for m in _MEDIA_RE.finditer(html):
            raw = m.group(0)
            if raw.startswith("//"):
                raw = "https:" + raw
            u = urljoin(url, raw)
            if u.startswith("http") and u not in found:
                found.append(u)
        if follow_iframes and depth == 0:
            for src in _IFRAME_RE.findall(html):
                src = core._clean(src)
                if src.startswith("//"):
                    src = "https:" + src
                scan(urljoin(url, src), 1)

    scan(page_url, 0)
    found.sort(key=lambda u: _PRIORITY.get(url_ext(u), 9))
    return found


# ---------------------------------------------------------------- DASH 파싱
def _strip_ns(root):
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def parse_duration(s):
    """ISO8601 PT1H2M3.5S -> 초"""
    if not s:
        return None
    m = re.match(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?)?", s)
    if not m:
        return None
    d, h, mi, sec = m.groups()
    return int(d or 0) * 86400 + int(h or 0) * 3600 + int(mi or 0) * 60 + float(sec or 0)


def _base(url, *elements):
    for el in elements:
        if el is None:
            continue
        b = el.find("BaseURL")
        if b is not None and b.text:
            url = urljoin(url, b.text.strip())
    return url


def _fill(tpl, rep_id, bandwidth, number=None, time_=None):
    def sub(m):
        name, fmt = m.group(1), m.group(2)
        if name == "":
            return "$"
        val = {"RepresentationID": rep_id, "Bandwidth": bandwidth,
               "Number": number, "Time": time_}.get(name)
        if val is None:
            return m.group(0)
        if fmt:
            return ("%" + fmt[1:]) % int(val)
        return str(val)
    return re.sub(r"\$(RepresentationID|Bandwidth|Number|Time|)(%0\d+d)?\$", sub, tpl)


def _merged_attr(name, *els, default=None):
    val = default
    for el in els:
        if el is not None and el.get(name) is not None:
            val = el.get(name)
    return val


def _rep_segments(rep, aset, period, base_url, period_dur):
    """Representation의 (init_url, [(url, byterange)]) 반환"""
    rid = rep.get("id", "")
    bw = rep.get("bandwidth", "0")
    tpls = [x for x in (period.find("SegmentTemplate"), aset.find("SegmentTemplate"),
                        rep.find("SegmentTemplate")) if x is not None]
    if tpls:
        media = _merged_attr("media", *tpls)
        init = _merged_attr("initialization", *tpls)
        start = int(_merged_attr("startNumber", *tpls, default="1"))
        timescale = int(_merged_attr("timescale", *tpls, default="1"))
        seg_dur = _merged_attr("duration", *tpls)
        timeline = None
        for t in tpls:
            if t.find("SegmentTimeline") is not None:
                timeline = t.find("SegmentTimeline")
        init_url = (urljoin(base_url, _fill(init, rid, bw)), None) if init else None
        segs = []
        if timeline is not None:
            num = start
            cur = 0
            entries = timeline.findall("S")
            for i, s in enumerate(entries):
                if s.get("t") is not None:
                    cur = int(s.get("t"))
                d = int(s.get("d"))
                r = int(s.get("r", "0"))
                if r < 0:
                    nxt = int(entries[i + 1].get("t")) if i + 1 < len(entries) and entries[i + 1].get("t") else None
                    end = nxt if nxt is not None else (period_dur or 0) * timescale
                    r = max(0, math.ceil((end - cur) / d) - 1)
                for _ in range(r + 1):
                    segs.append((urljoin(base_url, _fill(media, rid, bw, num, cur)), None))
                    cur += d
                    num += 1
        else:
            if not seg_dur or not period_dur:
                raise SystemExit("DASH 세그먼트 수를 계산할 수 없습니다 (duration 정보 없음).")
            count = math.ceil(period_dur * timescale / int(seg_dur))
            for n in range(start, start + count):
                segs.append((urljoin(base_url, _fill(media, rid, bw, n, (n - start) * int(seg_dur))), None))
        return init_url, segs

    slist = rep.find("SegmentList")
    if slist is None:
        slist = aset.find("SegmentList")
    if slist is not None:
        init_el = slist.find("Initialization")
        init_url = None
        if init_el is not None:
            iu = urljoin(base_url, init_el.get("sourceURL")) if init_el.get("sourceURL") else base_url
            ibr = None
            if init_el.get("range"):
                a, b = map(int, init_el.get("range").split("-"))
                ibr = (b - a + 1, a)
            init_url = (iu, ibr)
        segs = []
        for su in slist.findall("SegmentURL"):
            u = urljoin(base_url, su.get("media")) if su.get("media") else base_url
            br = None
            if su.get("mediaRange"):
                a, b = map(int, su.get("mediaRange").split("-"))
                br = (b - a + 1, a)
            segs.append((u, br))
        return init_url, segs

    # SegmentBase 또는 BaseURL만 있는 단일 파일
    return None, [(base_url, None)]


def _has_drm(*els):
    for el in els:
        if el is not None and el.find("ContentProtection") is not None:
            return True
    return False


def _kind_of(aset, rep):
    s = " ".join(filter(None, [aset.get("contentType"), aset.get("mimeType"),
                               rep.get("mimeType"), rep.get("codecs"), aset.get("codecs")])).lower()
    if "video" in s or "avc" in s or "hvc" in s or "hev" in s or "vp9" in s or "av01" in s:
        return "video"
    if "audio" in s or "mp4a" in s or "opus" in s or "ac-3" in s or "ec-3" in s:
        return "audio"
    return "other"


def parse_mpd(text, mpd_url):
    """{'video': [rep...], 'audio': [rep...]} ; rep = dict(id, bandwidth, resolution, tracks=[(init, segs) per period])"""
    root = _strip_ns(ET.fromstring(text))
    if root.get("type") == "dynamic":
        raise SystemExit("라이브(실시간) DASH 스트림은 지원하지 않습니다.")
    total = parse_duration(root.get("mediaPresentationDuration"))
    mpd_base = _base(mpd_url, root)
    periods = root.findall("Period")
    tracks = {"video": {}, "audio": {}}
    drm = False
    for pi, period in enumerate(periods):
        pdur = parse_duration(period.get("duration"))
        if pdur is None:
            if len(periods) == 1:
                pdur = total
            else:
                nxt = periods[pi + 1].get("start") if pi + 1 < len(periods) else None
                st = parse_duration(period.get("start")) or 0
                pdur = (parse_duration(nxt) - st) if nxt else (total - st if total else None)
        pbase = _base(mpd_base, period)
        for aset in period.findall("AdaptationSet"):
            abase = _base(pbase, aset)
            for rep in aset.findall("Representation"):
                kind = _kind_of(aset, rep)
                if kind not in tracks:
                    continue
                if _has_drm(aset, rep):
                    drm = True
                    continue
                rbase = _base(abase, rep)
                init, segs = _rep_segments(rep, aset, period, rbase, pdur)
                key = (rep.get("id"), aset.get("lang"))
                w, h = rep.get("width") or aset.get("width"), rep.get("height") or aset.get("height")
                ent = tracks[kind].setdefault(key, {
                    "id": rep.get("id"), "bandwidth": int(rep.get("bandwidth", 0)),
                    "resolution": f"{w}x{h}" if w and h else "?", "lang": aset.get("lang"),
                    "periods": []})
                ent["periods"].append((init, segs))
    out = {k: sorted(v.values(), key=lambda r: r["bandwidth"], reverse=True) for k, v in tracks.items()}
    if not out["video"] and not out["audio"]:
        if drm:
            raise DrmError("이 영상은 DRM(ContentProtection)으로 보호되어 있습니다. "
                           "이 프로그램은 DRM 보호 콘텐츠를 다운로드하지 않습니다.")
        raise SystemExit("MPD에서 받을 수 있는 영상/음성 트랙을 찾지 못했습니다.")
    return out


# ---------------------------------------------------------------- 공통 세그먼트 다운로드
def _download_parts(parts, tmpdir, prefix, fetcher, workers, tick, cancel):
    """parts: [(url, byterange)] -> 순서대로 저장된 파일 경로 목록"""
    paths = [os.path.join(tmpdir, f"{prefix}{i:06d}.part") for i in range(len(parts))]

    def job(i):
        if cancel is not None and cancel.is_set():
            raise RuntimeError("사용자가 취소했습니다.")
        if os.path.exists(paths[i]):
            return
        url, br = parts[i]
        data = fetcher.get(url, br)
        with open(paths[i] + ".tmp", "wb") as f:
            f.write(data)
        os.replace(paths[i] + ".tmp", paths[i])

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(job, i) for i in range(len(parts))]
        for f in as_completed(futs):
            try:
                f.result()
            except BaseException:
                ex.shutdown(wait=False, cancel_futures=True)  # 남은 작업 즉시 중단
                raise
            tick()
    return paths


def _concat(paths, out_path):
    with open(out_path, "wb") as out:
        for p in paths:
            with open(p, "rb") as f:
                shutil.copyfileobj(f, out)


def _run_ffmpeg(cmd):
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run(cmd, check=True, creationflags=flags)


def list_dash_qualities(mpd_url, fetcher):
    tr = parse_mpd(fetcher.get_text(mpd_url), mpd_url)
    return [{"resolution": r["resolution"], "bandwidth": r["bandwidth"], "url": mpd_url} for r in tr["video"]]


def download_dash(args, fetcher, log=print, on_progress=None, cancel=None):
    tr = parse_mpd(fetcher.get_text(args.url), args.url)
    video = audio = None
    if tr["video"]:
        video = tr["video"][0]
        if args.quality is not None:
            video = tr["video"][min(args.quality, len(tr["video"]) - 1)]
        elif getattr(args, "resolution", None):
            m = [v for v in tr["video"] if args.resolution in v["resolution"]]
            if m:
                video = m[0]
        log(f"영상 트랙: {video['resolution']} ({video['bandwidth'] // 1000} kbps)")
    if tr["audio"]:
        audio = tr["audio"][0]
        log(f"음성 트랙: {audio['bandwidth'] // 1000} kbps" + (f" [{audio['lang']}]" if audio["lang"] else ""))

    chosen = [("v", video), ("a", audio)]
    chosen = [(p, r) for p, r in chosen if r]
    plan = []  # (prefix, [(url, br)])
    for prefix, rep in chosen:
        parts = []
        last_init = None
        for init, segs in rep["periods"]:
            if init and init != last_init:
                parts.append(init)
                last_init = init
            parts.extend(segs)
        plan.append((prefix, parts))
    total = sum(len(p) for _, p in plan)
    log(f"DASH 세그먼트 {total}개 다운로드 시작")

    tmpdir = tempfile.mkdtemp(prefix="dash_")
    start = time.time()
    state = {"done": 0}

    def tick():
        state["done"] += 1
        (on_progress or core.progress)(state["done"], total, start)

    try:
        files = {}
        for prefix, parts in plan:
            paths = _download_parts(parts, tmpdir, prefix, fetcher, args.workers, tick, cancel)
            out = os.path.join(tmpdir, f"{prefix}.mp4")
            _concat(paths, out)
            files[prefix] = out
        if on_progress is None:
            print()
        ff = core.find_ffmpeg()
        base, ext = os.path.splitext(args.output)
        if ff:
            dst = args.output if ext.lower() in (".mp4", ".mkv", ".m4v", ".mov") else base + ".mp4"
            log("ffmpeg로 영상/음성 합치는 중...")
            cmd = [ff, "-y", "-loglevel", "error"]
            for k in ("v", "a"):
                if k in files:
                    cmd += ["-i", files[k]]
            cmd += ["-c", "copy", dst]
            _run_ffmpeg(cmd)
            return dst
        # ffmpeg 없으면 트랙별로 저장
        saved = []
        for k, suffix in (("v", ".video.mp4"), ("a", ".audio.m4a")):
            if k in files:
                shutil.move(files[k], base + suffix)
                saved.append(base + suffix)
        log("ffmpeg가 없어 영상/음성을 따로 저장했습니다: " + ", ".join(saved))
        return saved[0]
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------- 직접 파일 다운로드
def cleanup_temp(min_age=0):
    """이전 실행에서 남은 임시 폴더(hls_*, dash_*) 정리. 반환: 지운 개수"""
    n = 0
    tmp = tempfile.gettempdir()
    now = time.time()
    for name in os.listdir(tmp):
        if name.startswith(("hls_", "dash_")):
            p = os.path.join(tmp, name)
            try:
                if os.path.isdir(p) and now - os.path.getmtime(p) >= min_age:
                    shutil.rmtree(p, ignore_errors=True)
                    n += 1
            except OSError:
                pass
    return n


def download_direct(url, out_path, fetcher, log=print, on_progress=None, cancel=None, chunk=1 << 20):
    """일반 파일을 이어받기 지원하며 받기"""
    import urllib.request
    part = out_path + ".part"
    meta = part + ".url"
    # 이전에 실패한 다른 영상의 조각 파일이면 버리고 새로 받기
    try:
        prev = open(meta, encoding="utf-8").read().strip() if os.path.exists(meta) else None
    except OSError:
        prev = None
    if os.path.exists(part) and prev != url:
        os.remove(part)
    with open(meta, "w", encoding="utf-8") as f:
        f.write(url)
    have = os.path.getsize(part) if os.path.exists(part) else 0
    headers = dict(fetcher.headers)
    if have:
        headers["Range"] = f"bytes={have}-"
    req = urllib.request.Request(url, headers=headers)
    start = time.time()
    last_err = None
    for attempt in range(fetcher.retries):
        try:
            with urllib.request.urlopen(req, timeout=fetcher.timeout) as r:
                if have and r.status != 206:
                    have = 0  # 서버가 이어받기 미지원 -> 처음부터
                total = r.headers.get("Content-Length")
                total = int(total) + have if total else None
                if have:
                    log(f"이어받기: {have // 1048576}MB 지점부터")
                with open(part, "ab" if have else "wb") as f:
                    got = have
                    while True:
                        if cancel is not None and cancel.is_set():
                            raise RuntimeError("사용자가 취소했습니다. (다시 시작하면 이어받습니다)")
                        buf = r.read(chunk)
                        if not buf:
                            break
                        f.write(buf)
                        got += len(buf)
                        if total:
                            (on_progress or core.progress)(max(1, got * 1000 // total), 1000, start)
                if on_progress is None:
                    print()
                if total and got < total:
                    raise IOError(f"연결이 끊겼습니다 ({got}/{total} 바이트)")
            os.replace(part, out_path)
            try:
                os.remove(meta)
            except OSError:
                pass
            return out_path
        except RuntimeError:
            raise
        except Exception as e:  # noqa: BLE001
            last_err = e
            code = getattr(e, "code", None)
            if code == 416 and os.path.exists(part):
                os.remove(part)  # 조각 파일이 서버 파일과 안 맞음 -> 처음부터
            elif code is not None and 400 <= code < 500 and code not in (408, 429):
                break
            have = os.path.getsize(part) if os.path.exists(part) else 0
            headers["Range"] = f"bytes={have}-"
            req = urllib.request.Request(url, headers=headers)
            time.sleep(min(2 ** attempt, 10))
    raise RuntimeError(f"다운로드 실패: {url} ({last_err})")


# ---------------------------------------------------------------- 통합 진입점
def list_qualities(url, fetcher):
    kind = detect_kind(url, fetcher)
    if kind == "hls":
        v = core.parse_master(fetcher.get_text(url), url)
        v.sort(key=lambda x: x["bandwidth"], reverse=True)
        return v
    if kind == "dash":
        return list_dash_qualities(url, fetcher)
    return []


def resolve_page(url, fetcher, log=print):
    """페이지 주소면 영상 주소를 찾아 반환, 아니면 그대로"""
    kind = detect_kind(url, fetcher)
    if kind != "page":
        return url, kind
    log("웹 페이지로 보고 영상 주소를 찾습니다...")
    cands = find_media_in_page(url, fetcher, log=log)
    if not cands:
        raise SystemExit("페이지에서 영상 주소(m3u8/mpd/mp4)를 찾지 못했습니다. "
                         "브라우저 F12 > Network에서 직접 찾아 입력하세요.")
    for c in cands:
        log("발견: " + c)
    if "Referer" not in fetcher.headers:
        fetcher.headers["Referer"] = url
    log("사용할 주소: " + cands[0])
    return cands[0], detect_kind(cands[0], fetcher)


def download_any(args, fetcher, log=print, on_progress=None, cancel=None):
    """args: url, output, mp4, workers, quality, resolution  -> 저장된 파일 경로"""
    url, kind = resolve_page(args.url, fetcher, log=log)
    args.url = url

    if kind == "dash":
        return download_dash(args, fetcher, log, on_progress, cancel)

    if kind == "hls":
        res = core.download_hls(args, fetcher, log=log, on_progress=on_progress, cancel=cancel)
        merged, tmpdir = res
        try:
            if args.mp4:
                core.to_mp4(merged, args.output, log=log)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        return args.output

    if kind == "ts":
        if args.mp4:
            tmp = args.output + ".src.ts"
            download_direct(url, tmp, fetcher, log, on_progress, cancel)
            core.to_mp4(tmp, args.output, log=log)
            os.remove(tmp)
            return args.output
        return download_direct(url, args.output, fetcher, log, on_progress, cancel)

    # direct (mp4/webm 등): 원본 확장자를 따름
    src_ext = url_ext(url) or ".mp4"
    base, ext = os.path.splitext(args.output)
    out = args.output
    if ext.lower() != src_ext and src_ext in DIRECT_EXTS:
        out = base + src_ext
        if out != args.output:
            log(f"원본 형식에 맞춰 {os.path.basename(out)} 로 저장합니다.")
    log("파일 직접 다운로드")
    return download_direct(url, out, fetcher, log, on_progress, cancel)
