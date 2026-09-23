"""위키백과 API로 corpus.json 수집 (LLM 호출 없음).

python collect_corpus.py
"""
import json
import time
import urllib.parse
import urllib.error
import urllib.request
from pathlib import Path

API = "https://en.wikipedia.org/w/api.php"
UA = "WorldOfTomorrowResearcher/0.1 (student project)"

# 시드: 박람회 본체 + 분야별(교통 · 도시 · 가정 · 통신/과학 · 조직/정치) 전시·인물·기업
SEEDS = [
    # 박람회 본체 · 조직
    "1939 New York World's Fair", "Trylon and Perisphere", "Grover Whalen",
    "Robert Moses", "Flushing Meadows–Corona Park", "Queens Museum",
    # 교통
    "Futurama (New York World's Fair)", "Norman Bel Geddes", "General Motors",
    "Interstate Highway System", "Automated highway system",
    "Ford Motor Company", "Chrysler", "Pennsylvania Railroad class S1", "Streamline Moderne",
    "Raymond Loewy",
    # 도시 · 인프라
    "Henry Dreyfuss", "Consolidated Edison", "Garden city movement", "Suburb",
    "Urban planning", "Radiant City", "Broadacre City",
    # 가정 · 일상
    "Westinghouse Electric Corporation", "Elektro", "Home appliance", "Dishwasher",
    "Air conditioning", "Nylon", "DuPont", "Fluorescent lamp", "Elsie the Cow",
    "Walter Dorwin Teague", "View-Master",
    # 통신 · 과학
    "RCA Corporation", "History of television", "Voder", "Westinghouse Time Capsules",
    "Albert Einstein",
    # 국제 · 정치 맥락
    "1964 New York World's Fair", "Golden Gate International Exposition",
    "Century of Progress",
    # 사회 · 정치 — 누구의 미래였나 (대공황 · 뉴딜 · 노동 · 인종 · 교외화 · 전쟁 직전 국제관)
    "Great Depression", "New Deal", "Flint sit-down strike", "Welfare capitalism",
    "Corporate propaganda", "Consumerism", "Planned obsolescence", "Technocracy movement",
    "Redlining", "Levittown, New York", "The Power Broker",
    "Racial segregation in the United States", "Polish pavilion",
]


def get(params):
    params = {**params, "format": "json", "formatversion": "2"}
    url = API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise
            time.sleep(5 * (attempt + 1))
    raise RuntimeError("rate limited: " + url)


def fetch_page(title):
    d = get({"action": "query", "prop": "extracts", "explaintext": 1,
             "redirects": 1, "titles": title})
    page = d["query"]["pages"][0]
    if page.get("missing"):
        return None
    return page["title"], page.get("extract", "")


def fetch_links(titles):
    """여러 문서의 링크를 한 번에(최대 50건) 조회."""
    links, cont = {t: [] for t in titles}, {}
    while True:
        d = get({"action": "query", "prop": "links", "plnamespace": 0,
                 "pllimit": "max", "titles": "|".join(titles), **cont})
        for p in d["query"]["pages"]:
            links.setdefault(p["title"], []).extend(l["title"] for l in p.get("links", []))
        if "continue" not in d:
            return links
        cont = d["continue"]
        time.sleep(1)


def main():
    cache_path = Path("data/_page_cache.json")
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    docs = {}
    for t in SEEDS:
        if t not in cache:
            cache[t] = fetch_page(t)
            time.sleep(1.5)
        res = cache[t]
        if not res:
            print("  missing:", t)
            continue
        title, text = res
        docs[title] = {"id": title, "title": title, "text": text}
    Path("data").mkdir(exist_ok=True)
    cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")

    titles = set(docs)
    ordered, raw = sorted(titles), {}
    for i in range(0, len(ordered), 40):
        for k, v in fetch_links(ordered[i:i + 40]).items():
            raw.setdefault(k, []).extend(v)
    links = {t: sorted(set(l for l in raw.get(t, []) if l in titles and l != t)) for t in titles}

    out = {"docs": list(docs.values()), "links": links}
    Path("data").mkdir(exist_ok=True)
    Path("data/corpus.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    total = sum(len(d["text"]) for d in docs.values())
    window_tokens = 128_000
    print(f"\ndocs: {len(docs)}  total chars: {total:,}  ~tokens(chars/4): {total//4:,}  window: {window_tokens:,}")
    print("exceeds window:", total // 4 > window_tokens)
    print("\n chars  out  in  title")
    inbound = {t: 0 for t in titles}
    for t, ls in links.items():
        for l in ls:
            inbound[l] += 1
    for d in sorted(docs.values(), key=lambda d: -len(d["text"])):
        t = d["title"]
        print(f"{len(d['text']):>7,} {len(links[t]):>3} {inbound[t]:>3}  {t}")
    orphans = [t for t in titles if inbound[t] == 0]
    print("\nno inbound links (배정만이 유일한 경로):", orphans)


if __name__ == "__main__":
    main()
