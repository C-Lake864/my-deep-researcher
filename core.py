"""공통 부품: 코퍼스 · LLM 호출 · 읽기 도구 · 연구 루프.

서브에이전트(graph.py)와 혼자 하는 대조군(baseline.py)이 같은 `research_loop` 를 쓴다.
→ 같은 도구 · 같은 모델 · 같은 읽기 단위. 다른 것은 예산과 구역 규칙뿐.
"""
import json
import os
import re
import threading
from pathlib import Path

from openai import OpenAI

ROOT = Path(__file__).parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
CITE_RE = re.compile(r"\[(D\d{2}(?:\s*,\s*D\d{2})*)\]")


# ───────────────────────── 코퍼스 ─────────────────────────
class Corpus:
    def __init__(self, path=ROOT / "data" / "corpus.json"):
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        docs = sorted(raw["docs"], key=lambda d: d["title"])
        self.by_id, self.id_of = {}, {}
        for i, d in enumerate(docs, 1):
            did = f"D{i:02d}"
            self.id_of[d["title"]] = did
            self.by_id[did] = {"id": did, "title": d["title"], "sections": split_sections(d["text"])}
        self.links = {self.id_of[t]: [self.id_of[x] for x in ls if x in self.id_of]
                      for t, ls in raw["links"].items() if t in self.id_of}

    def title(self, did):
        return self.by_id[did]["title"]

    def outline(self, did):
        """절 제목과 길이만 — 싸게 보여 줄 수 있는 목록 (예산에 넣지 않는다)."""
        return [(name, len(txt)) for name, txt in self.by_id[did]["sections"]]

    def section_text(self, did, name):
        for n, t in self.by_id[did]["sections"]:
            if n == name:
                return t
        return None

    def total_chars(self):
        return sum(len(t) for d in self.by_id.values() for _, t in d["sections"])


def split_sections(text):
    """'== 제목 ==' 단위로 자른다. 하위 절(===)은 상위 절에 붙인다. 참고문헌류는 버린다."""
    skip = {"See also", "References", "Notes", "Further reading", "External links",
            "Bibliography", "Sources", "Citations", "Footnotes"}
    parts, name, buf = [], "Lead", []
    for line in text.splitlines():
        m = re.match(r"^==\s*([^=].*?)\s*==\s*$", line)
        if m:
            parts.append((name, "\n".join(buf).strip()))
            name, buf = m.group(1), []
        else:
            buf.append(line)
    parts.append((name, "\n".join(buf).strip()))
    return [(n, t) for n, t in parts if t and n not in skip]


# ───────────────────────── LLM ─────────────────────────
class Usage:
    """호출 수 · 토큰 · 주체별로 '본 글자 수' 를 센다. 격리 증명용."""

    def __init__(self):
        self.lock = threading.Lock()
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.chars_seen = {}  # actor -> 원문 글자 수

    def add_call(self, u):
        with self.lock:
            self.calls += 1
            self.prompt_tokens += u.prompt_tokens
            self.completion_tokens += u.completion_tokens

    def saw(self, actor, n):
        with self.lock:
            self.chars_seen[actor] = self.chars_seen.get(actor, 0) + n

    def as_dict(self):
        return {"calls": self.calls, "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens, "chars_seen": dict(self.chars_seen)}


_client = None


def llm_json(system, user, usage, max_tokens=900):
    global _client
    _client = _client or OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    r = _client.chat.completions.create(
        model=CONFIG["model"], temperature=0.3, max_tokens=max_tokens,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
    usage.add_call(r.usage)
    try:
        return json.loads(r.choices[0].message.content)
    except json.JSONDecodeError:
        return {}


# ───────────────────────── 연구 루프 ─────────────────────────
STEP_SYSTEM = """You are a research sub-agent reading Wikipedia articles about the 1939 New York World's Fair.
You read ONE section at a time. After each read, record short factual notes (English, with the doc id like [D07] after each fact)
that help answer your focus. Then pick the next section to read, or stop.
Reply JSON: {"notes": ["fact ... [Dxx]", ...], "next": {"doc": "Dxx", "section": "exact section name"} or null, "why": "one short reason"}"""

WRITE_SYSTEM = """You write one section of a Korean long-form research report from your notes ONLY.
Rules: write in Korean, 350-600 words, markdown paragraphs (no top-level heading).
Put a citation like [D07] at the end of every sentence that states a fact; cite ONLY doc ids that appear in your notes.
Focus on how the fair imagined the future and whose interests it served — not a list of exhibits.
Reply JSON: {"draft": "...", "sufficient": true/false, "missing": "what is still missing if not sufficient, else empty"}"""


def research_loop(corpus, usage, actor, focus, start_docs, budget, max_steps,
                  allowed=None, others=None, notes=None, visited=None, min_utilization=0.0):
    """한 에이전트가 예산만큼 읽고 메모를 남긴다. 원문은 이 함수 밖으로 나가지 않는다.

    allowed: 읽어도 되는 문서 집합 (None 이면 제한 없음)
    others:  남의 구역 설명 (프롬프트에 알려 줌)
    min_utilization: 이 비율 전에 멈추면 계속 읽게 한다 (대조군 공정성)
    """
    notes = list(notes or [])
    visited = set(visited or [])  # {(doc, section)}
    reads, used, last_text, trace = [], 0, "", []
    forced = 0
    step, hard_cap = -1, max_steps * 2 if min_utilization else max_steps
    while True:
        step += 1
        frontier = _frontier(corpus, start_docs, reads, allowed)
        menu = _menu(corpus, frontier, visited)
        user = (f"FOCUS: {focus}\nBUDGET LEFT: {budget - used} chars (each read costs up to {CONFIG['max_read_chars']})\n"
                + (f"OTHER AGENTS COVER (do not read their docs; stay in your lane): {others}\n" if others else "")
                + f"NOTES SO FAR:\n" + "\n".join(notes[-40:]) + "\n\n"
                + (f"TEXT JUST READ:\n{last_text}\n\n" if last_text else "")
                + f"READABLE SECTIONS (doc id | title, then quoted section names with chars):\n{menu}")
        out = llm_json(STEP_SYSTEM, user, usage, max_tokens=500)
        notes += [n for n in out.get("notes", []) if isinstance(n, str)]
        nxt = out.get("next")
        if budget - used <= 0 or step >= hard_cap:
            break
        if step >= max_steps and used >= min_utilization * budget:
            break  # 걸음 상한 도달 — 단, 대조군은 예산을 min_utilization 까지 채울 때까지 연장 (최대 2배)
        if step >= max_steps:  # 연장 구간(대조군만): 없는 절 요청으로 걸음을 버리지 않게 코드가 고른다
            nxt = _auto_pick(corpus, frontier, visited)
            forced += 1
        elif not nxt and used < min_utilization * budget:
            nxt = _auto_pick(corpus, frontier, visited)  # 멈추려 해도 예산이 남았으면 계속 읽게 한다
            forced += 1
        if not nxt:
            break
        d = nxt.get("doc")
        s = _match_section(corpus, d, nxt.get("section")) if d in corpus.by_id else None
        blocked = allowed is not None and d not in allowed
        if s is None or blocked or (d, s) in visited:
            why = "blocked" if blocked else ("already read" if s is not None else "no such section")
            trace.append({"doc": d, "section": nxt.get("section"), "rejected": why})
            last_text = f"(read rejected: {why}. Use a doc id and a section name exactly as listed.)"
            continue
        text = corpus.section_text(d, s)
        text = text[: min(CONFIG["max_read_chars"], budget - used)]
        used += len(text)
        usage.saw(actor, len(text))
        visited.add((d, s))
        reads.append({"doc": d, "section": s, "chars": len(text)})
        trace.append({"doc": d, "section": s, "chars": len(text), "why": out.get("why", "")})
        last_text = f"[{d}] {corpus.title(d)} / {s}\n{text}"
    return {"notes": notes, "reads": reads, "used": used, "budget": budget,
            "trace": trace, "forced_continues": forced, "visited": sorted(visited)}


def write_section(usage, label, focus, notes, words="350-600"):
    user = f"SECTION: {label}\nFOCUS: {focus}\nNOTES:\n" + "\n".join(notes)
    system = WRITE_SYSTEM.replace("350-600", words)
    out = llm_json(system, user, usage, max_tokens=1800)
    return {"draft": out.get("draft", ""), "sufficient": bool(out.get("sufficient", True)),
            "missing": out.get("missing", "")}


def _frontier(corpus, start_docs, reads, allowed):
    """시작 문서 + 지금까지 읽은 문서의 링크. 링크를 따라가며 넓어진다."""
    seen, order = set(), []
    for d in list(start_docs) + [x for r in reads for x in [r["doc"]] + corpus.links.get(r["doc"], [])]:
        if d in corpus.by_id and d not in seen and (allowed is None or d in allowed):
            seen.add(d)
            order.append(d)
    return order[:12]


def _auto_pick(corpus, frontier, visited):
    """대조군 연장용 자동 선택 — 가장 덜 읽은 문서의 가장 긴 미독 절 (첫 문서로 쏠리지 않게)."""
    read_count = {d: sum(1 for v in visited if v[0] == d) for d in frontier}
    for d in sorted(frontier, key=lambda x: read_count[x]):
        rest = [(n, c) for n, c in corpus.outline(d) if (d, n) not in visited]
        if rest:
            return {"doc": d, "section": max(rest, key=lambda x: x[1])[0]}
    return None


def _menu(corpus, frontier, visited, per_doc=12):
    """문서 제목과 절 이름을 분리하고 절 이름은 따옴표로 — 글자 수가 아니라 절 개수로 자른다."""
    lines = []
    for d in frontier:
        rest = [(n, c) for n, c in corpus.outline(d) if (d, n) not in visited]
        if rest:
            lines.append(f"{d} | {corpus.title(d)}\n    " + "; ".join(f'"{n}" ({c})' for n, c in rest[:per_doc]))
    return "\n".join(lines)


def _match_section(corpus, d, name):
    """정확히 → 'Title: 절' 접두어 제거 → 대소문자 무시 순으로 맞춘다."""
    if not isinstance(name, str):
        return None
    names = [n for n, _ in corpus.outline(d)]
    cand, title = name.strip().strip('"'), corpus.title(d)
    for pre in (title + ":", title + " /", title + " -"):
        if cand.startswith(pre):
            cand = cand[len(pre):].strip().strip('"')
    if cand in names:
        return cand
    low = {n.lower(): n for n in names}
    if cand.lower() in (title.lower(), "introduction", "overview", ""):
        return low.get("lead")
    return low.get(cand.lower())


def cited_ids(text):
    return {x.strip() for m in CITE_RE.findall(text) for x in m.split(",")}
