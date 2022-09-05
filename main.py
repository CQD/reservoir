from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
import logging
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from starlette.responses import FileResponse, PlainTextResponse

from app.data import ReservoirCrawler


logger = logging.getLogger(__name__)
app = FastAPI()

thread_pool = ThreadPoolExecutor(max_workers=1)

TSV_CONTENT = Path('public/reservoir-history.tsv').read_text()
TSV_SUPPLEMENTAL = ''
TSV_YESTERDAY = ''


@app.get("/")
async def index():
    return FileResponse('public/index.html')


@app.get("/favicon.png")
async def favicon():
    return FileResponse('public/favicon.png')


@app.get("/github.svg")
async def favicon():
    return FileResponse('public/github.svg')


@app.get("/plurk.svg")
async def favicon():
    return FileResponse('public/plurk.svg')


@app.get("/robots.txt")
async def robots():
    return FileResponse('public/robots.txt')


@app.get("/api/reservoir-history.tsv")
async def reservoir_history():
    result_tsv = TSV_CONTENT + TSV_SUPPLEMENTAL + TSV_YESTERDAY

    yesterday = datetime.now(ZoneInfo("Asia/Taipei")).date() - timedelta(days=1)
    yesterday_str = yesterday.strftime('%Y-%m-%d')

    last_date_str = result_tsv[-11:-1]

    cache_time = 86400
    if last_date_str < yesterday_str:
        thread_pool.submit(fetch_new_data)
        cache_time = 600

    headers = {'Cache-Control': f'public, max-age={cache_time}'}
    return PlainTextResponse(result_tsv, headers=headers)


def fetch_new_data():
    global TSV_SUPPLEMENTAL, TSV_YESTERDAY

    # 更新固定資料點的資料
    tsv = TSV_SUPPLEMENTAL if TSV_SUPPLEMENTAL else TSV_CONTENT
    last_date_str = tsv[-11:-1]

    yy, mm, dd = map(lambda val_str: int(val_str), last_date_str.split("-"))
    last_date = date(yy, mm, dd)

    logger.warn(f"最新資料時間是 {last_date}，撈取更新的資料")

    crawer = ReservoirCrawler()
    TSV_SUPPLEMENTAL += crawer.fetch_uppdated_as_tsv(last_date=last_date)

    # 更新「昨天」的資料
    yesterday = datetime.now(ZoneInfo("Asia/Taipei")).date() - timedelta(days=1)
    yesterday_str = yesterday.strftime('%Y-%m-%d')

    last_date_str = '2022-01-01'
    if TSV_YESTERDAY:
        last_date_str = TSV_YESTERDAY[-11:-1]

    if last_date_str < yesterday_str:
        lines = [f"{name}\t{max}\t{curr}\t{yesterday_str}\n"
                 for name, (max, curr) in crawer.fetch(yesterday).items()]
        TSV_YESTERDAY = "".join(lines)


thread_pool.submit(fetch_new_data)


if __name__ == '__main__':
    import uvicorn
    uvicorn.run("main:app", port=80, host='0.0.0.0', reload=True, log_level='debug')
