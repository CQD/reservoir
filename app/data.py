from urllib.parse import urlparse, parse_qs

from datetime import datetime, date
import logging
from typing import Dict, Optional, Tuple

from lxml.html import HtmlElement
from pyquery import PyQuery as pq
import requests

logger = logging.getLogger(__name__)

RESERVOIR_GROUPS = {
    "北部": {"翡翠水庫", "石門水庫", "寶山第二水庫", "新山水庫", "大埔水庫", "寶山水庫", "西勢水庫"},
    "中部": {"德基水庫", "日月潭水庫", "鯉魚潭水庫", "湖山水庫", "霧社水庫", "永和山水庫", "明德水庫", "石岡壩"},
    "南部": {"曾文水庫", "南化水庫", "烏山頭水庫", "牡丹水庫", "仁義潭水庫", "阿公店水庫", "白河水庫", "蘭潭水庫", "鳳山水庫", "澄清湖水庫", "鏡面水庫"},
}

RESERVOIRS = {*RESERVOIR_GROUPS['北部'], *RESERVOIR_GROUPS['中部'], *RESERVOIR_GROUPS['南部']}


class ReservoirCrawler:
    url = 'https://fhy.wra.gov.tw/ReservoirPage_2011/StorageCapacity.aspx'
    form_inputs: Dict[str, str] = {}

    def __init__(self) -> None:
        self.fetch_page()


    def fetch_page(self, payload: Optional[dict]=None):
        if payload is None:
            payload = {}

        payload = self.form_inputs | payload

        resp = requests.post(self.url, data=payload)
        html = resp.text

        document = pq(html)

        self.form_inputs.clear()
        fields = document('form#aspnetForm input')
        for field in fields.items():
            name = field.attr('name')
            value = field.attr('value')
            self.form_inputs[name] = value

        script = document('script[src*=_TSM_HiddenField_]')
        hf_url = urlparse(script.attr("src"))
        hf_query = parse_qs(hf_url.query)
        self.form_inputs['ctl00_ctl02_HiddenField'] = hf_query['_TSM_CombinedScripts_'][0]

        return document


    def fetch(self, date: datetime=datetime.now()):
        payload = {
            'ctl00$cphMain$ucDate$cboYear': str(date.year),
            'ctl00$cphMain$ucDate$cboMonth': str(date.month),
            'ctl00$cphMain$ucDate$cboDay': str(date.day),
            'ctl00$cphMain$cboSearch': '所有水庫',

            'ctl00$ctl02': "ctl00$cphMain$ctl00|ctl00$cphMain$btnQuery",
            '__EVENTTARGET': "ctl00$cphMain$btnQuery",
        }
        document = self.fetch_page(payload)
        trs = document('#ctl00_cphMain_gvList tr')

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
            result[name] = (capacity, current)

        return result


if __name__ == '__main__':
    fetcher = ReservoirCrawler()
    def go(y, m, d):
        dt = date(y, m, d)
        dt_str = dt.strftime('%Y-%m-%d')
        rslt = fetcher.fetch(date=dt)
        for name, (max, curr) in rslt.items():
            print(f"{name}\t{max}\t{curr}\t{dt_str}")

    go(2022,4,22)
    for m in [5,6,7]:
        for d in [1,15,22]:
            go(2022,m,d)
