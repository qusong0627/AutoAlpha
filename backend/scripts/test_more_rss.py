import asyncio
from backend.services.api.routers.news import _huntly_request, _unwrap

more_urls = [
    ("格隆汇 - 实时快讯", "http://quantmind-rsshub:1200/gelonghui/live"),
    ("36氪 - 快讯", "http://quantmind-rsshub:1200/36kr/newsflashes"),
    ("钛媒体 - 快讯", "http://quantmind-rsshub:1200/tmtpost/timeline"),
    ("界面新闻 - 快讯", "http://quantmind-rsshub:1200/jiemian/news/list"),
    ("同花顺 - 财经要闻", "http://quantmind-rsshub:1200/10jqka/news"),
    ("新浪财经 - 7x24快讯", "http://quantmind-rsshub:1200/sina/finance/news"),
    ("金十数据 - 实时快讯", "http://quantmind-rsshub:1200/jin10/news"),
    ("CoinDesk 中文", "http://quantmind-rsshub:1200/coindesk/news"),
]

async def check():
    print("=== 测试更多本地 RSSHub 路由 ===")
    for name, url in more_urls:
        try:
            r = await _huntly_request("GET", "/api/setting/feeds/preview", params={"subscribeUrl": url})
            if r.status_code == 200:
                data = _unwrap(r.json())
                print(f"[OK] {name}: {url} -> {data.get('title')}")
            else:
                print(f"[FAIL {r.status_code}] {name}: {url}")
        except Exception as e:
            print(f"[ERR] {name}: {e}")

if __name__ == '__main__':
    asyncio.run(check())
