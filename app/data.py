import os
import sys
from urllib.parse import urlparse, parse_qs

from datetime import datetime, date, timedelta
import logging
from typing import Dict, Optional, Tuple
from zoneinfo import ZoneInfo

from lxml.html import HtmlElement
from pyquery import PyQuery as pq
import requests

logger = logging.getLogger(__name__)

RESERVOIR_GROUPS = {
    "北部": {"翡翠水庫", "石門水庫", "寶山第二水庫", "新山水庫", "大埔水庫", "寶山水庫", "西勢水庫"},
    "中部": {"德基水庫", "日月潭水庫", "鯉魚潭水庫", "湖山水庫", "霧社水庫", "永和山水庫", "明德水庫", "石岡壩"},
    "南部": {"曾文水庫", "南化水庫", "烏山頭水庫", "牡丹水庫", "仁義潭水庫", "阿公店水庫", "白河水庫", "蘭潭水庫", "鳳山水庫", "澄清湖水庫", "鏡面水庫"},
    "外離島": {"成功水庫","興仁水庫","太湖水庫","田埔水庫","金沙水庫","后沃水庫",},
}

RESERVOIRS = {
    *RESERVOIR_GROUPS['北部'],
    *RESERVOIR_GROUPS['中部'],
    *RESERVOIR_GROUPS['南部'],
    *RESERVOIR_GROUPS['外離島'],
}


class ReservoirCrawler:
    url = 'https://fhy.wra.gov.tw/ReservoirPage_2011/StorageCapacity.aspx'
    form_inputs: Dict[str, str] = {}

    def fetch_page(self, payload: Optional[dict]=None):
        logger.warning("開始撈取資料，payload 為：%s", payload)

        if payload is None:
            payload = {}

        payload = self.form_inputs | payload

        resp = requests.post(self.url, data=payload, headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Referer": "https://fhy.wra.gov.tw/ReservoirPage_2011/StorageCapacity.aspx",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
        })
        html = resp.text

        document = pq(html)

        self.form_inputs.clear()
        fields = document('form#aspnetForm input')

        if len(fields) == 0:
            logger.error("resp.status_code: %s", resp.status_code)
            logger.error("resp.headers: %s", resp.headers)
            logger.error("resp.text: %s", html)
            raise RuntimeError("找不到 form#aspnetForm input")

        for field in fields.items():
            name = field.attr('name')
            value = field.attr('value')
            self.form_inputs[name] = value

        script = document('script[src*=_TSM_HiddenField_]')
        hf_url = urlparse(script.attr("src"))
        hf_query = parse_qs(hf_url.query)

        if '_TSM_CombinedScripts_' not in hf_query:
            raise RuntimeError(f"script src 裡面沒有 _TSM_CombinedScripts_。 src 為：{script.attr('src')}")

        self.form_inputs['ctl00_ctl02_HiddenField'] = hf_query['_TSM_CombinedScripts_'][0]

        logger.warning("成功撈取資料")
        return document


    def fetch(self, date: Optional[date]=None):
        today = date if date else datetime.now(ZoneInfo("Asia/Taipei")).date()

        # init form data
        if not self.form_inputs:
            self.fetch_page()

        # 拉指定日期的資料
        payload = {
            'ctl00$cphMain$ucDate$cboYear': str(today.year),
            'ctl00$cphMain$ucDate$cboMonth': str(today.month),
            'ctl00$cphMain$ucDate$cboDay': str(today.day),
            'ctl00$cphMain$cboSearch': '所有水庫',

            'ctl00$ctl02': "ctl00$cphMain$ctl00|ctl00$cphMain$btnQuery",
            '__EVENTTARGET': "ctl00$cphMain$btnQuery",
        }
        document = self.fetch_page(payload)
        trs = document('#ctl00_cphMain_gvList tr')

        # 把拉到的資料中有值的水庫抓出來
        result: Dict[str, Tuple[float, float]] = {}

        def floater(ele: HtmlElement):
            str_val = str(ele.text).replace('--', '').replace(',', '')
            str_val = str_val or '-1'
            return float(str_val)

        for tr in list(trs.items())[2:]:
            tds = tr('td')
            if len(tds) != 11:
                continue

            name = tds[0].text

            if name not in RESERVOIRS:
                continue

            capacity = floater(tds[1])
            current = floater(tds[9])
            if capacity <=0 and current <= 0:
                continue

            result[name] = (capacity, current)

        # 數量不夠，有的今天還沒更新，拉昨天的資料
        if len(result) < len(RESERVOIRS) and date is None:
            yesterday = today - timedelta(days=1)
            logger.warning(" %s 的資料數量不夠，拉 %s 的資料", today, yesterday)
            prev_result = self.fetch(yesterday)
            for name, (capacity, current) in prev_result.items():
                if name not in result:
                    result[name] = (capacity, current)

        return result


    def fetch_uppdated_as_tsv(self, begin_date: date, end_date: date | None=None):
        end_date = end_date or datetime.now(ZoneInfo("Asia/Taipei")).date() - timedelta(days=1)

        lines = []
        for y in range(begin_date.year, end_date.year + 1):
            for m in range(1, 13):
                for d in (1, 8, 15, 22):
                    cursor_dt = date(y, m, d)
                    cursor_dt_str = cursor_dt.strftime('%Y-%m-%d')

                    if cursor_dt < begin_date:
                        continue
                    if cursor_dt >= end_date:
                        break

                    lines.extend(f"{name}\t{max}\t{curr}\t{cursor_dt_str}\n" \
                                 for name, (max, curr) in self.fetch(cursor_dt).items())

        return "".join(lines)

################
# cli 用
################

def get_last_ymd_from_file() -> str:
    now = datetime.now()

    current_year = now.year
    tsv_file = "%s/../public/reservoir-history/%s.tsv" % (os.path.dirname(__file__), current_year)

    if not os.path.exists(tsv_file):
        tsv_file = "%s/../public/reservoir-history/%s.tsv" % (os.path.dirname(__file__), current_year - 1)

    if not os.path.exists(tsv_file):
        return (now - timedelta(days=1)).strftime('%Y-%m-%d')

    last_line = ""
    with open(tsv_file, 'rb') as f:
        try:  # catch OSError in case of a one line file
            f.seek(-2, os.SEEK_END)
            while f.read(1) != b'\n':
                f.seek(-2, os.SEEK_CUR)
        except OSError:
            f.seek(0)
        last_line = f.readline().decode().strip() or last_line

    return last_line.split('\t')[-1].strip()


if __name__ == '__main__':
    tsv_file = "%s/../public/reservoir-history.tsv" % (os.path.dirname(__file__))

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
