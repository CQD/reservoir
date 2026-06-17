from datetime import date, datetime, timedelta
import asyncio
import colorsys
import hashlib
import io
import logging
from pathlib import Path
from threading import Timer
import time
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from starlette.responses import FileResponse, PlainTextResponse, Response
from PIL import Image, ImageFilter
import numpy as np
import requests

from app.data import ReservoirCrawler, RESERVOIR_GROUPS


logger = logging.getLogger(__name__)

TPE_TIMEZONE = ZoneInfo("Asia/Taipei")

TSV_FROM_FILE = ''
TSV_SUPPLEMENTAL = ''  # 歷史檔案裡面缺少的固定資料點（每月 1, 8, 15, 22 日）
TSV_LATEST = ''  # 此刻的最新資料

PREV_UPDATE_TIME = datetime.now().timestamp()
NEXT_UPDATE_TIME = datetime.now().timestamp()
UPDATE_INTERVAL = 3600
UPDATE_TIMER: Timer = None


# CURR_DATA[{水庫名稱}] = [日期, 最大蓄水量, 目前蓄水量]
CURR_DATA: dict[str, tuple[str, float, float]] = {}
TSV_CURR: str = ''


# ── 中央氣象局兩日累積雨量圖 ───────────────────────────────
# server 端在記憶體快取一份，前端只跟本站拿；避免每個訪客都直接打氣象局
RAINFALL_REFRESH_INTERVAL = 600  # 每 10 分鐘檢查一次有沒有更新的整點圖
RAINFALL_TIMER: Timer = None
# (圖片 bytes, media type, 來源網址, etag, 抓取時間)；None 表示還沒抓到任何一張
RAINFALL_CACHE: tuple[bytes, str, str, str, float] | None = None

# 氣象局原圖用整條彩虹色階（灰→青→藍→綠→黃→橙→紅→紫），疊在地圖上很雜。
# 抓回來後在 server 端重新著色成單一藍色系：雨量越大→越濃的深藍，越小→越接近白的淡藍。
#
# 下面是色階表上 16 個色塊的代表色（由大雨→小雨），取自原圖右側色階表掃描；
# 最末一格（1mm 的灰）和背景/無資料一樣留白，不染色。
_CWA_RAIN_COLORS = np.array([
    (255, 214, 254), (255, 56, 251), (220, 45, 210), (171, 32, 163),
    (170, 24, 1), (218, 34, 4), (255, 43, 6), (255, 167, 30),
    (255, 211, 40), (254, 253, 49), (1, 250, 48), (39, 164, 28),
    (1, 119, 253), (0, 165, 254), (1, 210, 253), (157, 254, 255),
], dtype=np.int16)


def _build_blue_ramp(n: int) -> np.ndarray:
    """產生 n 階藍色：index 0（最大雨）最深，index n-1（最小雨）最淡近白。"""
    ramp = []
    for i in range(n):
        t = i / (n - 1)
        # 固定藍色相，明度由深(0.20)到淺(0.94)、彩度由濃(0.88)到淡(0.66)
        r, g, b = colorsys.hls_to_rgb(0.59, 0.20 + t * 0.74, 0.88 - t * 0.22)
        ramp.append((round(r * 255), round(g * 255), round(b * 255)))
    return np.array(ramp, dtype=np.uint8)


_BLUE_RAMP = _build_blue_ramp(len(_CWA_RAIN_COLORS))


def recolor_rainfall_to_blue(src_bytes: bytes) -> bytes:
    """把氣象局彩虹雨量圖重新著色成藍色系，回傳 PNG bytes。

    作法：彩度低的像素（白/紙背景/灰）視為無雨或底圖，一律留白；
    其餘像素比對最接近的色階色塊，換成對應深淺的藍；
    最後對「雨量等級」做眾數濾波，抹掉色塊邊緣的孤立雜點。
    """
    im = Image.open(io.BytesIO(src_bytes)).convert("RGB")
    arr = np.asarray(im, dtype=np.int16)
    h, w, _ = arr.shape
    flat = arr.reshape(-1, 3)

    # 所有雨量色階彩度都很高（最淡的青色也有 ~98），白/米/灰背景都 < 30，
    # 用這條界線乾淨切開「雨量」與「無雨/底圖」
    sat = flat.max(1) - flat.min(1)
    is_rain = sat >= 30
    # 提升到 int32：色距平方最大 255²=65025 會超出 int16 上限而溢位、害 argmin 選錯
    rain_px = flat[is_rain].astype(np.int32)

    # 只對雨量像素逐色階比距離找最近鄰（省記憶體，避免一次展開大矩陣）
    best_d = None
    best_i = np.zeros(len(rain_px), dtype=np.uint8)
    for i, color in enumerate(_CWA_RAIN_COLORS):
        dist = ((rain_px - color) ** 2).sum(1)
        if best_d is None:
            best_d = dist
        else:
            closer = dist < best_d
            best_d[closer] = dist[closer]
            best_i[closer] = i

    # 先把每個像素的雨量等級畫成單通道圖（255=無雨/留白），對「等級」做眾數濾波去雜點：
    # 原圖網格的零星白點、海岸/縣市界線等孤立錯階像素會被鄰域主等級吃掉，色塊邊緣也更平滑。
    # 在等級上（而非 RGB 上）濾波，眾數一定落在既有等級，不會拼出藍階以外的雜色。
    labels = np.full(len(flat), 255, dtype=np.uint8)
    labels[is_rain] = best_i
    labels = np.asarray(
        Image.fromarray(labels.reshape(h, w), mode="L").filter(ImageFilter.ModeFilter(5))
    )

    out = np.full((h, w, 3), 255, dtype=np.uint8)  # 預設全白
    rain_mask = labels != 255
    out[rain_mask] = _BLUE_RAMP[labels[rain_mask]]

    buf = io.BytesIO()
    Image.fromarray(out).save(buf, format="PNG", optimize=True)
    return buf.getvalue()

def load_tsv_files():
    global TSV_FROM_FILE
    this_year = datetime.now().year

    contents: list[str] = []
    for year in range(2003, this_year + 1):
        tsv_file = Path(f'public/reservoir-history/{year}.tsv')
        if tsv_file.exists():
            contents.append(tsv_file.read_text())
        else:
            logger.warning(f"找不到 {tsv_file}")

    TSV_FROM_FILE = "".join(contents)
    tsv_to_curr_data(TSV_FROM_FILE)


def tsv_to_curr_data(tsv: str):
    global TSV_CURR

    now = datetime.now(TPE_TIMEZONE)

    # 如果資料超過 30 天，不把它納入目前蓄水量的計算（資料久未更新？）
    # 但是最大蓄水量的資料不受此限制，避免不必要的分母錯誤
    thirty_days_ago_str = (now - timedelta(days=30)).strftime('%Y-%m-%d')

    for line in tsv.split('\n'):
        fields = line.split('\t')

        if len(fields) != 4:
            continue

        name, max, curr, dt_str = fields

        line_max_f = float(max)
        line_curr_f = float(curr)


        c_dt_str, c_max_f, c_curr_f = CURR_DATA.get(name, ("1970-01-01", -1.0, -1.0))

        if line_max_f > 0:
            c_max_f = line_max_f

        if line_curr_f > 0 and dt_str >= thirty_days_ago_str:
            c_curr_f = line_curr_f
            c_dt_str = dt_str

        CURR_DATA[name] = (c_dt_str, c_max_f, c_curr_f)


    TSV_CURR = '\n'.join(f"{name}\t{max}\t{curr}" for name, (_, max, curr) in CURR_DATA.items())


def _cwa_rainfall_urls(hours_back: int = 6):
    """從目前整點往回推，產生氣象局雨量圖的候選網址（新→舊）。"""
    now = datetime.now(TPE_TIMEZONE).replace(minute=0, second=0, microsecond=0)
    for back in range(hours_back + 1):
        dt = now - timedelta(hours=back)
        yield f"https://www.cwa.gov.tw/Data/rainfall/{dt:%Y-%m-%d_%H}00.QZK8.jpg"


def refresh_rainfall_cache():
    """去氣象局抓最新的整點雨量圖更新快取。只有背景緒會呼叫，是唯一對外連氣象局的地方。

    抓不到（整點圖還沒產生／斷網／被擋）就保留舊快取、不清空，
    避免「舊圖過期但新圖還沒到」造成空窗。
    """
    global RAINFALL_CACHE

    cached_url = RAINFALL_CACHE[2] if RAINFALL_CACHE else None

    for url in _cwa_rainfall_urls():
        # 走到目前手上這張了，再往回只會更舊，不用抓
        if url == cached_url:
            return

        try:
            resp = requests.get(url, timeout=15, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
                "Referer": "https://www.cwa.gov.tw/V8/C/P/Rainfall/Rainfall_QZJ.html",
            })
        except Exception:
            logger.exception("[rainfall] 抓取 %s 失敗", url)
            continue

        # 整點圖還沒產生會是 404；也用 JPEG magic 擋掉被導去 HTML 錯誤頁的情況
        if resp.status_code != 200 or resp.content[:3] != b"\xff\xd8\xff" or len(resp.content) < 1000:
            continue

        # 把彩虹色階重新著色成藍色系；萬一轉換失敗就退回原圖，不讓疊圖整個消失
        try:
            content = recolor_rainfall_to_blue(resp.content)
            media_type = "image/png"
        except Exception:
            logger.exception("[rainfall] 重新著色失敗，改用原圖")
            content, media_type = resp.content, "image/jpeg"

        etag = hashlib.md5(content).hexdigest()
        RAINFALL_CACHE = (content, media_type, url, etag, time.time())
        logger.warning("[rainfall] 已更新快取：%s（%s, %d bytes）", url, media_type, len(content))
        return

    if RAINFALL_CACHE is None:
        logger.warning("[rainfall] 目前還沒有任何可用的雨量圖")


def rainfall_updater():
    global RAINFALL_TIMER

    try:
        refresh_rainfall_cache()
    except Exception:
        logger.exception("[rainfall] 更新時發生未預期的錯誤")

    RAINFALL_TIMER = Timer(RAINFALL_REFRESH_INTERVAL, rainfall_updater)
    RAINFALL_TIMER.start()


def livespan(app: FastAPI):
    global UPDATE_TIMER, RAINFALL_TIMER

    logger.warning("[startup] 從檔案載入歷史資料")
    load_tsv_files()

    logger.warning("[startup] 排定撈最新資料")

    def updater():
        global NEXT_UPDATE_TIME, PREV_UPDATE_TIME, UPDATE_TIMER

        logger.warning("[updater] 啟動")
        interval = UPDATE_INTERVAL
        try:
            fetch_new_data()
            PREV_UPDATE_TIME = time.time()
        except:
            logger.exception("[updater] 更新資料時發生錯誤")
            interval = 2000

        NEXT_UPDATE_TIME = time.time() + interval

        logger.warning("[updater] 結束，將於 %s 秒後再次執行", interval)
        UPDATE_TIMER = Timer(interval, updater)
        UPDATE_TIMER.start()

    UPDATE_TIMER = Timer(1, updater)
    UPDATE_TIMER.start()
    logger.warning("[startup] 排定更新資料")

    # 雨量圖快取：開機後盡快灌第一張，之後每 RAINFALL_REFRESH_INTERVAL 秒更新一次
    RAINFALL_TIMER = Timer(0.1, rainfall_updater)
    RAINFALL_TIMER.start()
    logger.warning("[startup] 排定更新雨量圖")

    yield

    UPDATE_TIMER.cancel()
    if RAINFALL_TIMER:
        RAINFALL_TIMER.cancel()
    logger.warning("[shutdown] 已關閉 updater")


app = FastAPI(lifespan=livespan)


@app.get("/")
@app.get("/favicon.png")
@app.get("/github.svg")
@app.get("/plurk.svg")
@app.get("/robots.txt")
@app.get("/tw.svg")
async def static_file(request: Request):
    path = request.url.path
    if path == '/':
        path = '/index.html'

    return FileResponse(f'public{path}')


@app.get("/api/reservoir-history.tsv")
async def reservoir_history():
    now = time.time()

    cache_time = max(int(NEXT_UPDATE_TIME - now), 0) + 30
    full_tsv = TSV_FROM_FILE + TSV_SUPPLEMENTAL + TSV_LATEST

    headers = {
        'etag': hashlib.md5(full_tsv.encode()).hexdigest(),
        'Cache-Control': f'public, max-age={cache_time}',
        'x-update-time': datetime.fromtimestamp(PREV_UPDATE_TIME, tz=TPE_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S'),
    }
    return PlainTextResponse(full_tsv, headers=headers)


@app.get("/api/curr.tsv")
async def curr():
    now = time.time()

    cache_time = max(int(NEXT_UPDATE_TIME - now), 0) + 30

    headers = {
        'etag': hashlib.md5(TSV_CURR.encode()).hexdigest(),
        'Cache-Control': f'public, max-age={cache_time}',
        'x-update-time': datetime.fromtimestamp(PREV_UPDATE_TIME, tz=TPE_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S'),
    }
    return PlainTextResponse(TSV_CURR, headers=headers)



@app.get("/api/CURR_DATA")
async def curr_data():
    return CURR_DATA


# 路徑沿用 .jpg（而非 .png）是刻意的：app.yaml 的 static handler 會把所有 *.png 當
# 靜態檔攔截，改名 .png 就進不到這個 route。實際回傳的是重新著色後的 PNG。
@app.get("/api/rainfall.jpg")
async def rainfall_jpg(request: Request):
    # cold start 還沒抓到圖時，給背景緒幾秒把第一張灌進來，而不是馬上回 503
    deadline = time.time() + 8
    while RAINFALL_CACHE is None and time.time() < deadline:
        await asyncio.sleep(0.2)

    cache = RAINFALL_CACHE
    if cache is None:
        # 真的還沒有圖：前端 onerror 會維持沒有疊圖的樣子，別讓快取記住這個失敗
        return Response(status_code=503, headers={"Cache-Control": "no-store"})

    data, media_type, source_url, etag, fetched_at = cache

    headers = {
        # 邊緣（Cloudflare）/瀏覽器快取 10 分鐘；過期後先回舊圖再背景重新驗證，
        # 萬一 origin 出錯還能續用舊圖一天 → 最大程度避免空窗/outage
        "Cache-Control": "public, max-age=600, stale-while-revalidate=3600, stale-if-error=86400",
        "ETag": etag,
        "X-Rainfall-Source": source_url,
        "X-Rainfall-Fetched": datetime.fromtimestamp(fetched_at, tz=TPE_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S"),
    }

    # 圖沒變就回 304，省下重傳
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)

    return Response(content=data, media_type=media_type, headers=headers)


def fetch_new_data():
    global TSV_SUPPLEMENTAL, TSV_LATEST, TSV_CURR

    logger.warning("[fetch_new_data] 開始")

    # 更新固定資料點的資料
    tsv = TSV_SUPPLEMENTAL if TSV_SUPPLEMENTAL else TSV_FROM_FILE
    last_date_str = tsv[-11:-1]

    yy, mm, dd = map(lambda val_str: int(val_str), last_date_str.split("-"))
    last_date = date(yy, mm, dd)

    logger.warning(f"最新資料時間是 {last_date}，撈取更新的資料")

    crawer = ReservoirCrawler()
    TSV_SUPPLEMENTAL += crawer.fetch_uppdated_as_tsv(begin_date=last_date)
    logger.warning("[fetch_new_data] 固定資料點已更新")

    # 拉最新的資料
    crawed_data = crawer.fetch()

    if len(crawed_data) <= 0:
        logger.info("crawed_data is empty")
        return

    lines = [f"{name}\t{max}\t{curr}\t{today_str}\n"
                for name, (max, curr, today_str) in crawed_data.items()]
    TSV_LATEST = "".join(lines)

    # 紀錄目前蓄水量/最大蓄水量
    logger.warning("[fetch_new_data] 更新水庫目前蓄水量/最大蓄水量")
    tsv_to_curr_data(TSV_SUPPLEMENTAL)
    tsv_to_curr_data(TSV_LATEST)

    logger.warning("[fetch_new_data] 最新資料點已更新")


if __name__ == '__main__':
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    import uvicorn
    uvicorn.run("main:app", port=8080, host='0.0.0.0', reload=True, log_level='debug')
