import httpx
import asyncio
from backend.services.api.routers.news import _huntly_request, _unwrap

test_urls = [
    # 本地 RSSHub 路径
    ("财联社 (本地RSSHub)", "http://quantmind-rsshub:1200/cls/telegraph"),
    ("华尔街见闻 (本地RSSHub)", "http://quantmind-rsshub:1200/wallstreetcn/news/global"),
    ("雪球今日热帖 (本地RSSHub)", "http://quantmind-rsshub:1200/xueqiu/hots"),
    ("东方财富研报 (本地RSSHub)", "http://quantmind-rsshub:1200/eastmoney/report/strategyinvest"),
    ("中国人民银行 (本地RSSHub)", "http://quantmind-rsshub:1200/pbc/goutongjiaoliu"),
    ("财新网 (本地RSSHub)", "http://quantmind-rsshub:1200/caixin/finance/regulation"),
    ("新浪财经快讯 (本地RSSHub)", "http://quantmind-rsshub:1200/sina/finance/stock"),
    
    # 国际外网公共地址
    ("arXiv 量化金融", "http://export.arxiv.org/rss/q-fin"),
    ("Qlib Releases", "https://github.com/microsoft/qlib/releases.atom"),
    ("feedx 财联社", "https://feedx.net/rss/cls.xml"),
    ("rsshub.app 财联社", "https://rsshub.app/cls/telegraph")
]

async def check():
    print("=== 1. 测试直接 HTTP 访问 ===")
    async with httpx.AsyncClient(timeout=6.0) as client:
        for name, url in test_urls:
            try:
                r = await client.get(url)
                print(f"[{r.status_code}] {name}: {url} ({len(r.text)} bytes)")
            except Exception as e:
                print(f"[FAIL] {name}: {url} -> {e}")

    print("\n=== 2. 测试 Huntly 预览与订阅 ===")
    for name, url in test_urls:
        try:
            r = await _huntly_request("GET", "/api/setting/feeds/preview", params={"subscribeUrl": url})
            if r.status_code == 200:
                data = _unwrap(r.json())
                print(f"[Huntly OK] {name}: title={data.get('title')}, siteLink={data.get('siteLink')}")
            else:
                print(f"[Huntly FAIL {r.status_code}] {name}: {r.text[:120]}")
        except Exception as e:
            print(f"[Huntly ERR] {name}: {e}")

if __name__ == '__main__':
    asyncio.run(check())
