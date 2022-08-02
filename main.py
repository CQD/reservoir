from concurrent.futures import ThreadPoolExecutor
from datetime import date
from logging import Logger, basicConfig, INFO
from pathlib import Path

from fastapi import FastAPI
from starlette.responses import FileResponse, PlainTextResponse

from app.data import ReservoirCrawler

basicConfig(level=INFO)
logger = Logger(__name__)
app = FastAPI()

thread_pool = ThreadPoolExecutor(max_workers=1)

# RESERVOIR_DATA[date] = {tsv string, with line break at end}
RESERVOIR_DATA = {}
TSV_CONTENT = Path('public/reservoir-history.tsv').read_text()


@app.get("/")
async def index():
    return FileResponse('public/index.html')


@app.get("/favicon.png")
async def favicon():
    return FileResponse('public/favicon.png')


@app.get("/robots.txt")
async def robots():
    return FileResponse('public/robots.txt')


@app.get("/api/reservoir-history.tsv")
async def reservoir_history():
    last_date = TSV_CONTENT[-11:-1]
    thread_pool.submit(fetch_new_data, last_date)

    headers = {'Cache-Control': 'public, max-age=86400'}
    return PlainTextResponse(TSV_CONTENT + "".join(RESERVOIR_DATA.values()), headers=headers)


def fetch_new_data(last_date:str) -> str:
    global RESERVOIR_DATA

    today = date.today()
    yy, mm, dd = map(lambda val_str: int(val_str), last_date.split("-"))
    last_dt = date(yy, mm, dd)

    crawer = ReservoirCrawler()
    for y in range(last_dt.year, today.year + 1):
        for m in range(1, 13):
            for d in (1, 8, 15, 22):
                cursor_dt = date(y, m, d)
                cursor_dt_str = cursor_dt.strftime('%Y-%m-%d')

                if cursor_dt_str in RESERVOIR_DATA:
                    continue
                if cursor_dt <= last_dt:
                    continue
                if cursor_dt >= today:
                    break

                logger.info("嘗試撈取 %s 的資料", cursor_dt)
                lines = [f"{name}\t{max}\t{curr}\t{cursor_dt_str}\n"
                         for name, (max, curr) in crawer.fetch(cursor_dt).items()]
                logger.info("已撈取 %s 的 %d 筆資料", cursor_dt, len(lines))

                RESERVOIR_DATA[cursor_dt_str] = "".join(lines)


if __name__ == '__main__':
    import uvicorn
    uvicorn.run("main:app", port=80, host='0.0.0.0', reload=True, log_level='debug')
