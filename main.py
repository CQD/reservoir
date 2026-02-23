from datetime import date, datetime, timedelta
import hashlib
import logging
from pathlib import Path
from threading import Timer
import time
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from starlette.responses import FileResponse, PlainTextResponse

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


def livespan(app: FastAPI):
    global UPDATE_TIMER

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

    yield

    UPDATE_TIMER.cancel()
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
    uvicorn.run("main:app", port=80, host='0.0.0.0', reload=True, log_level='debug')
