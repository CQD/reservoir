from datetime import date, datetime
import logging
from pathlib import Path
from threading import Timer
import time
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from starlette.responses import FileResponse, PlainTextResponse

from app.data import ReservoirCrawler


logger = logging.getLogger(__name__)
app = FastAPI()

TPE_TIMEZONE = ZoneInfo("Asia/Taipei")

TSV_CONTENT = Path('public/reservoir-history.tsv').read_text()
TSV_SUPPLEMENTAL = ''
TSV_LATEST = ''
TSV_COMBINED = TSV_CONTENT

UPDATE_TIME = datetime.strptime(TSV_CONTENT[-11:-1], "%Y-%m-%d").timestamp()
UPDATE_INTERVAL = 3600
UPDATE_TIMER: Timer = None


@app.on_event("startup")
def startup():
    global UPDATE_TIMER

    Timer(1, fetch_new_data).start()
    logger.warning("[startup] 排定撈最新資料")

    def updater():
        logger.warning("[updater] 啟動")
        try:
            fetch_new_data()
            UPDATE_TIMER = Timer(UPDATE_INTERVAL, updater)
            UPDATE_TIMER.start()
        except Exception as e:
            logger.error("[updater] 更新資料時發生錯誤，稍後重試: %s", e)
            UPDATE_TIMER = Timer(15, updater)
            UPDATE_TIMER.start()

        logger.warning("[updater] 結束")

    UPDATE_TIMER = Timer(UPDATE_INTERVAL, updater)
    UPDATE_TIMER.start()
    logger.warning("[startup] 排定更新資料")


@app.on_event("shutdown")
def shutdown():
    UPDATE_TIMER.cancel()
    logger.warning("[shutdown] 已關閉 updater")


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
    cache_time = max(int(UPDATE_TIME + UPDATE_INTERVAL - now), 30)

    headers = {
        'Cache-Control': f'public, max-age={cache_time}',
        'x-update-time': datetime.fromtimestamp(UPDATE_TIME, tz=TPE_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S'),
    }
    return PlainTextResponse(TSV_COMBINED, headers=headers)


def fetch_new_data():
    global TSV_SUPPLEMENTAL, TSV_LATEST, TSV_COMBINED, UPDATE_TIME

    logger.info("[fetch_new_data] 開始")

    # 更新固定資料點的資料
    tsv = TSV_SUPPLEMENTAL if TSV_SUPPLEMENTAL else TSV_CONTENT
    last_date_str = tsv[-11:-1]

    yy, mm, dd = map(lambda val_str: int(val_str), last_date_str.split("-"))
    last_date = date(yy, mm, dd)

    logger.warning(f"最新資料時間是 {last_date}，撈取更新的資料")

    crawer = ReservoirCrawler()
    TSV_SUPPLEMENTAL += crawer.fetch_uppdated_as_tsv(last_date=last_date)
    logger.warning("[fetch_new_data] 固定資料點已更新")

    # 拉最新的資料
    today_str = datetime.now(TPE_TIMEZONE).strftime('%Y-%m-%d')

    crawed_data = crawer.fetch()
    if not isinstance(crawed_data, dict):
        logger.error("crawed_data is not a dict: %s", crawed_data)
        return

    if len(crawed_data) <= 0:
        logger.info("crawed_data is empty")
        return

    lines = [f"{name}\t{max}\t{curr}\t{today_str}\n"
                for name, (max, curr) in crawed_data.items()]
    TSV_LATEST = "".join(lines)
    logger.warning("[fetch_new_data] 最新資料點已更新")

    UPDATE_TIME = time.time()
    TSV_COMBINED = TSV_CONTENT + TSV_SUPPLEMENTAL + TSV_LATEST
    logger.warning("[fetch_new_data] 資料更新完成")


if __name__ == '__main__':
    import uvicorn
    uvicorn.run("main:app", port=80, host='0.0.0.0', reload=True, log_level='debug')
