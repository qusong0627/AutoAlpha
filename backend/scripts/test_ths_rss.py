import asyncio
import httpx
from backend.services.api.routers.news import _huntly_request, _unwrap

async def test_ths():
    print("=== 测试同花顺 7x24小时财经直播 ===")
    
    # 候选 URL 列表
    candidates = [
        ("网页直接地址 (HTML)", "https://news.10jqka.com.cn/realtimenews.html"),
        ("本地 RSSHub realtimenews", "http://quantmind-rsshub:1200/10jqka/realtimenews"),
        ("本地 RSSHub news", "http://quantmind-rsshub:1200/10jqka/news"),
        ("本地 RSSHub 滚动新闻", "http://quantmind-rsshub:1200/10jqka/tag/7"),
    ]
    
    for label, url in candidates:
        print(f"\n--- 测试: {label} ({url}) ---")
        # 1. 测试直接 HTTP
        try:
            async with httpx.AsyncClient(timeout=6.0, follow_redirects=True) as client:
                r = await client.get(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
                print(f"HTTP Status: {r.status_code}, Length: {len(r.text)} bytes")
        except Exception as e:
            print(f"HTTP Error: {e}")
            
        # 2. 测试 Huntly 预览
        try:
            r = await _huntly_request("GET", "/api/setting/feeds/preview", params={"subscribeUrl": url})
            if r.status_code == 200:
                data = _unwrap(r.json())
                print(f"✓ Huntly 预览成功! Title: {data.get('title')}, Site: {data.get('siteLink')}")
            else:
                print(f"✗ Huntly 预览失败 [{r.status_code}]: {r.text[:150]}")
        except Exception as e:
            print(f"✗ Huntly 异常: {e}")

if __name__ == '__main__':
    asyncio.run(test_ths())
