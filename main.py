from logging import Logger

from fastapi import FastAPI
from starlette.responses import FileResponse
import uvicorn

logger = Logger(__name__)
app = FastAPI()


@app.get("/")
def index():
    return FileResponse('public/index.html')


@app.get("/api/reservoir-history.tsv")
def reservoir_history():
    return FileResponse('public/reservoir-history.tsv')


if __name__ == '__main__':
    uvicorn.run("main:app", port=80, host='0.0.0.0', reload=True)
