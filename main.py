from datetime import date, datetime
import logging
from pathlib import Path
from threading import Timer
import time
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from starlette.responses import FileResponse, PlainTextResponse

from app.data import ReservoirCrawler


logger = logging.getLogger(__name__)

TPE_TIMEZONE = ZoneInfo("Asia/Taipei")

TSV_FROM_FILE = ''
TSV_SUPPLEMENTAL = ''  # 歷史檔案裡面缺少的固定資料點（每月 1, 8, 15, 22 日）
TSV_LATEST = ''  # 此刻的最新資料
TSV_COMBINED = TSV_FROM_FILE

UPDATE_TIME = 0
UPDATE_INTERVAL = 3600
UPDATE_TIMER: Timer = None


def load_tsv_files():
    global TSV_FROM_FILE

    tsv_files = [*Path('public/reservoir-history').glob('*.tsv')]
    tsv_files.sort()

    for tsv_file in tsv_files:
        TSV_FROM_FILE += tsv_file.read_text()


def livespan(app: FastAPI):
    global UPDATE_TIMER

    logger.warning("[startup] 從檔案載入歷史資料")
    load_tsv_files()

    logger.warning("[startup] 排定撈最新資料")

    def updater():
        logger.warning("[updater] 啟動")
        interval = UPDATE_INTERVAL
        try:
            fetch_new_data()
        except:
            logger.exception("[updater] 更新資料時發生錯誤")
            interval = 1000

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
    cache_time = max(int(UPDATE_TIME + UPDATE_INTERVAL - now), 0) + 30

    headers = {
        'Cache-Control': f'public, max-age={cache_time}',
        'x-update-time': datetime.fromtimestamp(UPDATE_TIME, tz=TPE_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S'),
    }
    return PlainTextResponse(TSV_COMBINED, headers=headers)


def fetch_new_data():
    global TSV_SUPPLEMENTAL, TSV_LATEST, TSV_COMBINED, UPDATE_TIME

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
    today_str = datetime.now(TPE_TIMEZONE).strftime('%Y-%m-%d')

    crawed_data = crawer.fetch()

    if len(crawed_data) <= 0:
        logger.info("crawed_data is empty")
        return

    lines = [f"{name}\t{max}\t{curr}\t{today_str}\n"
                for name, (max, curr) in crawed_data.items()]
    TSV_LATEST = "".join(lines)
    logger.warning("[fetch_new_data] 最新資料點已更新")

    UPDATE_TIME = time.time()
    TSV_COMBINED = TSV_FROM_FILE + TSV_SUPPLEMENTAL + TSV_LATEST
    logger.warning("[fetch_new_data] 資料更新完成")


if __name__ == '__main__':
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    import uvicorn
    uvicorn.run("main:app", port=80, host='0.0.0.0', reload=True, log_level='debug')
