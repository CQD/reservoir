import os
import sys
import logging
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger(__name__)

RESERVOIR_GROUPS = {
    "北部": ["翡翠水庫", "石門水庫", "寶山第二水庫", "新山水庫", "大埔水庫", "寶山水庫", "西勢水庫"],
    "中部": ["德基水庫", "日月潭水庫", "鯉魚潭水庫", "湖山水庫", "霧社水庫", "永和山水庫", "明德水庫", "石岡壩"],
    "南部": ["曾文水庫", "南化水庫", "烏山頭水庫", "牡丹水庫", "仁義潭水庫", "阿公店水庫", "白河水庫", "蘭潭水庫", "鳳山水庫", "澄清湖水庫", "鏡面水庫"],
    "外離島": ["成功水庫","興仁水庫","太湖水庫","田埔水庫","金沙水庫","后沃水庫",],
}

RESERVOIRS = [
    *RESERVOIR_GROUPS['北部'],
    *RESERVOIR_GROUPS['中部'],
    *RESERVOIR_GROUPS['南部'],
    *RESERVOIR_GROUPS['外離島'],
]


class ReservoirCrawler:
    """從水利署新版 fhyv2 JSON API 抓各水庫即時水情。

    2026 年中水利署網站改版：舊的 ReservoirPage_2011/StorageCapacity.aspx
    （ASP.NET WebForms 頁面）已 302 轉走，站台前面也加了 WAF，舊的「POST 表單
    再解析 HTML」爬法只會拿到 WAF 的 Request Rejected 頁。新版資料改打 JSON API：

      GET /Api/v2/Reservoir/Station        各站基本資料（站號、站名、有效容量）
      GET /Api/v2/Reservoir/Info/RealTime  各站即時水情（有效蓄水量、時間）
      GET /Api/v2/Reservoir/Daily          各站昨日日報（即時抓不到時的備援）

    三個端點都要帶前端寫死的 apikey header（少了瀏覽器常見的 Accept header
    一樣會被 WAF 擋）。新版只提供「即時」與「昨日」兩種快照，沒有任意歷史日期
    查詢，因此無法再像舊版那樣回頭補任意一天的固定資料點。
    """

    api_base = 'https://fhy.wra.gov.tw/Api/v2'

    # 前端 app.js 編譯進去的公開金鑰（VUE_APP_API_KEY），所有訪客共用同一把。
    api_key = 'd6dd3cd4-493f-43a3-92b1-8b2db217da96'

    # 即時值超過這天數沒更新就視為缺漏（記 -1.0）。正常即時資料都是當天整點，
    # 這只是擋掉某站資料卡在很久以前的情況。
    STALE_DAYS = 14

    # 新版 API 站名 → 本站沿用站名。「田浦水庫」其實才是正字，但歷史 TSV、
    # 前端、地圖長年都用別字「田埔水庫」；抓進來時統一轉回舊名，免得同一個
    # 水庫在歷史曲線上斷成兩條。
    NAME_FROM_API = {
        '田浦水庫': '田埔水庫',
    }

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': (
                'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/124.0.0.0 Safari/537.36'
            ),
            # 少了 Accept / Accept-Language 會被站台的 WAF 當成非瀏覽器流量擋掉
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'zh-TW,zh;q=0.9',
            'Referer': 'https://fhy.wra.gov.tw/fhyv2/monitor/reservoir',
            'apikey': self.api_key,
        })
        self._station_cache: dict[str, tuple[str, float]] | None = None

    def _get(self, path: str) -> list[dict]:
        resp = self.session.get(f'{self.api_base}/{path}', timeout=30)
        resp.raise_for_status()
        payload = resp.json()
        data = payload.get('Data')
        if not data:
            raise RuntimeError(f"{path} 回應沒有 Data：{str(payload)[:200]}")
        return data

    def _stations(self) -> dict[str, tuple[str, float]]:
        """回 {站號: (本站站名, 有效容量)}，只留本站關注的水庫。容量一天才更新
        一次，同一個 crawler 生命週期內快取即可。"""
        if self._station_cache is None:
            out: dict[str, tuple[str, float]] = {}
            for s in self._get('Reservoir/Station'):
                name = self.NAME_FROM_API.get(s['StationName'], s['StationName'])
                if name not in RESERVOIRS:
                    continue
                cap = s.get('EffectiveCapacity')
                out[s['StationNo']] = (name, float(cap) if cap else -1.0)
            self._station_cache = out
        return self._station_cache

    def _collect(
        self, rows: list[dict], today: date,
    ) -> dict[str, tuple[float, float, str]]:
        """把一份 API 快照（RealTime 或 Daily）整理成
        {站名: (有效容量, 有效蓄水量, 資料日期)}。"""
        stations = self._stations()

        result: dict[str, tuple[float, float, str]] = {}
        for r in rows:
            station = stations.get(r['StationNo'])
            if station is None:
                continue
            name, capacity = station

            time_str = r.get('Time') or ''
            date_str = time_str[:10] or today.strftime('%Y-%m-%d')

            storage = r.get('EffectiveStorage')
            current = float(storage) if storage and storage > 0 else -1.0

            # 即時值太舊就當作缺漏，避免把陳年資料當成現況
            if current > 0:
                try:
                    if (today - date.fromisoformat(date_str)).days > self.STALE_DAYS:
                        current = -1.0
                except ValueError:
                    pass

            result[name] = (capacity, current, date_str)

        return result

    def fetch_current(
        self, today: date | None = None,
    ) -> dict[str, tuple[float, float, str]]:
        """抓目前各水庫的 (有效容量, 有效蓄水量, 資料日期)。"""
        today = today or datetime.now(ZoneInfo("Asia/Taipei")).date()
        logger.warning("開始撈取即時水情")
        result = self._collect(self._get('Reservoir/Info/RealTime'), today)
        logger.warning("成功撈取 %d 個水庫的即時水情", len(result))
        return result

    def fetch(
        self, target_date: date | None = None,
    ) -> dict[str, tuple[float, float, str]]:
        """相容舊介面：回 {水庫名稱: (最大蓄水量, 目前蓄水量, 日期)}。

        新版 API 沒有任意歷史日查詢，一律回最新的即時水情。"""
        return self.fetch_current(target_date)

    def fetch_uppdated_as_tsv(self, begin_date: date, end_date: date | None = None) -> str:
        """補 begin_date（不含）之後到 end_date 為止、新出現的固定資料點
        （每月 1, 8, 15, 22 日）的 TSV。

        新版 API 只有「即時」與「昨日」兩種快照，所以只補得到今天與昨天的固定
        點；更早的（server 停機跨過）只能略過。實務上 server 每小時跑，固定點
        當天的即時值已進最新資料，隔天再用昨日日報把該固定點正式補進歷史。
        """
        today = datetime.now(ZoneInfo("Asia/Taipei")).date()
        end_date = end_date or (today - timedelta(days=1))
        yesterday = today - timedelta(days=1)

        targets = [
            date(y, m, d)
            for y in range(begin_date.year, end_date.year + 1)
            for m in range(1, 13)
            for d in (1, 8, 15, 22)
        ]
        targets = [t for t in targets if begin_date < t <= end_date]
        if not targets:
            return ""

        snapshots: dict[date, dict[str, tuple[float, float, str]]] = {}
        lines: list[str] = []
        for target in targets:
            if target == today:
                snap = snapshots.get(today) or self.fetch_current(today)
                snapshots[today] = snap
            elif target == yesterday:
                snap = snapshots.get(yesterday)
                if snap is None:
                    snap = self._collect(self._get('Reservoir/Daily'), today)
                    snapshots[yesterday] = snap
            else:
                logger.warning("固定資料點 %s 已過去，新版 API 無歷史查詢可補，略過", target)
                continue

            target_str = target.strftime('%Y-%m-%d')
            lines.extend(
                f"{name}\t{mx}\t{curr}\t{target_str}\n"
                for name, (mx, curr, _) in snap.items()
            )

        return "".join(lines)

################
# cli 用
################

def get_last_ymd_from_file() -> str:
    now = datetime.now()

    for year in range(now.year, now.year - 10, -1):
        tsv_file = "%s/../public/reservoir-history/%s.tsv" % (os.path.dirname(__file__), year)
        if not os.path.exists(tsv_file):
            continue

        last_line = ""
        with open(tsv_file, 'rb') as f:
            try:  # catch OSError in case of a one line file
                f.seek(-2, os.SEEK_END)
                while f.read(1) != b'\n':
                    f.seek(-2, os.SEEK_CUR)
            except OSError:
                f.seek(0)
            last_line = f.readline().decode().strip() or last_line

        if last_line.strip():
            return last_line.split('\t')[-1].strip()

    return ""


if __name__ == '__main__':
    now = datetime.now()
    current_year = now.year

    tsv_file = "%s/../public/reservoir-history/%s.tsv" % (os.path.dirname(__file__), current_year)

    begin_date_ymd = sys.argv[1] if len(sys.argv) > 1 else None
    end_date_ymd = sys.argv[2] if len(sys.argv) > 2 else None

    begin_date_ymd = begin_date_ymd or get_last_ymd_from_file() or '2003-12-31'

    def ymd_to_date(ymd: str | None) -> date:
        if not ymd:
            return datetime.now().date()

        yy, mm, dd = map(lambda val_str: int(val_str), ymd.split("-"))
        return date(yy, mm, dd)

    begin_dt = ymd_to_date(begin_date_ymd)
    begin_dt = begin_dt + timedelta(days=1)

    end_dt = ymd_to_date(end_date_ymd)

    logger.warning("開始撈取 %s 到 %s 的資料", begin_dt, end_dt)

    crawer = ReservoirCrawler()
    print(crawer.fetch_uppdated_as_tsv(begin_date=begin_dt, end_date=end_dt), end='')

    if begin_dt.year != end_dt.year:
        logger.warning("資料開始/結束年份不同（%d vs %d），可能需要手動調整檔案", begin_dt.year, end_dt.year)
