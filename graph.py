"""기획 → 검사 → 배치(동시) → 서브에이전트 → 점검(재파견) → 병합 → 종합.

  python graph.py q1 --plan          # 계획만 (LLM 1회) → output/plans/q1.json + 예상 비용
  python graph.py q1 --run           # 저장된(승인된) 계획으로 실행
  python graph.py q1 --run --off zones   # 스위치 끄고 실행
"""
import argparse
import json
import operator
import time
import uuid
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from core import CONFIG, ROOT, Corpus, Usage, cited_ids, llm_json, research_loop, write_section

CORPUS = Corpus()
USAGE: dict[str, Usage] = {}  # run_id -> Usage (State 밖에 두는 계측기)
HUB = {CORPUS.id_of[t] for t in CONFIG["hub_docs"] if t in CORPUS.id_of}
ROLE_ZONE = {r: {CORPUS.id_of[t] for t in v["zone"] if t in CORPUS.id_of} for r, v in CONFIG["roles"].items()}
ZONED = set().union(*ROLE_ZONE.values())
OUT = ROOT / "output"


class State(TypedDict, total=False):
    run_id: str
    qid: str
    question: str
    switches: dict
    raw_plan: dict
    plan: list                               # 검사를 통과한 배정
    plan_check: dict                         # 검사 결과 (거른 배정 수 등)
    sections: Annotated[list, operator.add]  # 서브에이전트가 올린 원고 (원문 X)
    rechecks: Annotated[list, operator.add]
    final_sections: list
    report: dict


# ───────────────────────── ① 기획 ─────────────────────────
PLAN_SYSTEM = """You are the coordinator of a deep-research team on the 1939 New York World's Fair.
You never read article bodies. You see only a catalog (ids, titles, zones, section names, in-link counts) and the hub article's lead.
Rules:
1. Split the report into sections BY CONTENT DOMAIN (one role each) — never by format (overview/timeline/analysis).
2. Use as few sections as the question needs. If one document answers it, use ONE section.
   If the question follows ONE person or object, use only the roles its trail actually passes through — do not add unrelated domains.
3. Start docs (1-3 per section) must be the MOST SPECIFIC docs for that section's focus:
   prefer docs that are directly about a fair exhibit, pavilion, person or event named or implied by the question
   (e.g. an exhibit article before a general theory article). General background docs come last.
4. Docs with in-links 0 cannot be reached by following links — if one is relevant, you MUST assign it or nobody will read it.
5. Do not give two sections the same start doc.
Reply JSON: {"sections": [{"role": "...", "heading": "Korean section heading", "focus": "English: what this section must answer",
"start_docs": ["Dxx", ...]}], "rationale": "한국어로 쓴 분할 이유 (why this split, why these start docs)"}"""


def catalog_text():
    zone_of = {d: r for r, ds in ROLE_ZONE.items() for d in ds}
    inbound = {d: 0 for d in CORPUS.by_id}
    for ls in CORPUS.links.values():
        for x in ls:
            inbound[x] += 1
    lines = []
    for d, doc in CORPUS.by_id.items():
        if d in HUB:
            continue
        secs = ", ".join(n for n, _ in CORPUS.outline(d))[:220]
        lines.append(f"{d} [{zone_of.get(d, 'common')}] {doc['title']} (in-links {inbound[d]}) :: {secs}")
    return "\n".join(lines)


def plan_node(state: State):
    usage = USAGE[state["run_id"]]
    hub_lead = "\n".join(CORPUS.section_text(d, "Lead")[: CONFIG["hub_lead_chars"]] for d in HUB)
    usage.saw("coordinator", len(hub_lead))  # 코디네이터가 본 원문은 허브 문서 앞부분뿐
    roles = "\n".join(f"- {r}: {v['label']} — {v['brief']}" for r, v in CONFIG["roles"].items())
    user = (f"QUESTION: {state['question']}\n\nROLES:\n{roles}\n\nMAX SECTIONS: {CONFIG['max_sections']}\n\n"
            f"HUB LEAD:\n{hub_lead}\n\nCATALOG:\n{catalog_text()}")
    return {"raw_plan": llm_json(PLAN_SYSTEM, user, usage, max_tokens=900)}


# ───────────────────────── 배정 검사 ─────────────────────────
def validate_node(state: State):
    """모델은 그럴듯한 제목을 지어낸다 → 배정이 실제 문서를 가리키는지 코드가 본다."""
    on = state["switches"]["validate"]
    raw = state.get("raw_plan", {}).get("sections", [])[: CONFIG["max_sections"]]
    plan, bad, fixed, used_roles, used_docs = [], [], 0, set(), set()
    for s in raw:
        role = s.get("role")
        if on and (role not in CONFIG["roles"] or role in used_roles):
            bad.append({"role": role, "reason": "unknown or duplicate role"})
            continue
        docs = []
        for d in s.get("start_docs", [])[: CONFIG["max_start_docs"]]:
            d = CORPUS.id_of.get(d, d) if d not in CORPUS.by_id else d
            if on and d in used_docs:  # 프롬프트로 금지해도 어긴다 → 절 간 시작 문서 중복은 코드가 제거
                bad.append({"role": role, "doc": d, "reason": "duplicate start doc"})
                continue
            if d in CORPUS.by_id and d not in HUB:
                docs.append(d)
            elif d in CORPUS.id_of:  # 제목으로 준 경우 id 로 고침
                docs.append(CORPUS.id_of[d]); fixed += 1
            else:
                bad.append({"role": role, "doc": d, "reason": "not in corpus" if d not in HUB else "hub doc"})
                if not on:
                    docs.append(d)  # 스위치를 끄면 걸러내지 않고 그대로 내려보낸다
        if on and not [d for d in docs if d in CORPUS.by_id]:
            docs = sorted(ROLE_ZONE.get(role, []))[:1]  # 전부 틀렸으면 구역의 첫 문서로 대체
        used_roles.add(role)
        used_docs |= set(docs)
        plan.append({"role": role, "heading": s.get("heading", role), "focus": s.get("focus", state["question"]),
                     "start_docs": docs, "budget": CONFIG["section_budget_chars"]})
    dispatched_invalid = sum(1 for s in plan for d in s["start_docs"] if d not in CORPUS.by_id)
    return {"plan": plan, "plan_check": {"rejected": bad, "fixed_titles": fixed,
                                         "invalid_dispatched": dispatched_invalid, "validate_on": on}}


# ───────────────────────── ② 배치 ─────────────────────────
def zone_rules(plan, i, zones_on):
    """i 번째 절이 읽어도 되는 문서 집합과, 알려 줄 남의 구역."""
    if not zones_on:
        return None, None  # 구역 없음: 무엇이든 읽을 수 있고 남의 구역도 모른다
    mine = ROLE_ZONE.get(plan[i]["role"], set()) | {d for d in plan[i]["start_docs"] if d in CORPUS.by_id}
    theirs = set()
    desc = []
    for j, s in enumerate(plan):
        if j == i:
            continue
        z = (ROLE_ZONE.get(s["role"], set()) | set(s["start_docs"])) - mine
        theirs |= z
        desc.append(f"{s['heading']} ({s['role']}): " + ", ".join(sorted(x for x in z if x in CORPUS.by_id)))
    allowed = {d for d in CORPUS.by_id if d not in HUB and d not in theirs}
    return allowed, " | ".join(desc)


def dispatch(state: State):
    plan, zones_on = state["plan"], state["switches"]["zones"]
    sends = []
    for i, s in enumerate(plan):
        allowed, others = zone_rules(plan, i, zones_on)
        sends.append(Send("subagent", {"run_id": state["run_id"], "idx": i, "sec": s,
                                       "allowed": allowed, "others": others}))
    return sends


def subagent_node(p: dict):
    usage, s = USAGE[p["run_id"]], p["sec"]
    actor = f"sub{p['idx']}:{s['role']}"
    r = research_loop(CORPUS, usage, actor, s["focus"], s["start_docs"], s["budget"], CONFIG["max_steps"],
                      allowed=p["allowed"], others=p["others"])
    w = write_section(usage, s["heading"], s["focus"], r["notes"])
    # 위로는 원고 · 메모 · 읽은 목록만 올라간다. 원문 텍스트는 여기서 버려진다.
    return {"sections": [{"idx": p["idx"], "round": 0, "actor": actor, **s, **w,
                          "notes": r["notes"], "reads": r["reads"], "used": r["used"], "trace": r["trace"],
                          "visited": r["visited"], "allowed": sorted(p["allowed"]) if p["allowed"] else None,
                          "others": p["others"]}]}


# ───────────────────────── ③ 점검 ─────────────────────────
def check_route(state: State):
    if not state["switches"]["recheck"] or CONFIG["max_rounds"] < 1:
        return "merge"
    sends = []
    for s in state["sections"]:
        if not s["sufficient"]:  # 스스로 부족하다고 신고한 절만
            sends.append(Send("recheck", {"run_id": state["run_id"], "prev": s}))
    return sends or "merge"


def recheck_node(p: dict):
    usage, prev = USAGE[p["run_id"]], p["prev"]
    focus = prev["focus"] + f"\nPREVIOUS DRAFT WAS MISSING: {prev['missing']}"
    allowed = set(prev["allowed"]) if prev["allowed"] else None
    r = research_loop(CORPUS, usage, prev["actor"], focus, prev["start_docs"], CONFIG["recheck_budget_chars"],
                      CONFIG["max_steps"], allowed=allowed, others=prev["others"],
                      notes=prev["notes"], visited=[tuple(v) for v in prev["visited"]])
    w = write_section(usage, prev["heading"], focus, r["notes"])
    return {"rechecks": [{**prev, **w, "round": 1, "notes": r["notes"], "reads": prev["reads"] + r["reads"],
                          "used": prev["used"] + r["used"], "trace": prev["trace"] + r["trace"],
                          "visited": r["visited"]}]}


def keep_second(prev, new):
    """두 번째 원고 채택 규칙: 경보 0 이고 인용 문서 수가 줄지 않을 때만 교체.
    무조건 덮어쓰면, 인용을 빠뜨린 새 원고가 멀쩡한 이전 원고를 지운다."""
    read_new = {r["doc"] for r in new["reads"]}
    c_new, c_prev = cited_ids(new["draft"]), cited_ids(prev["draft"])
    return bool(new["draft"].strip()) and c_new <= read_new and len(c_new) >= len(c_prev)


def merge_node(state: State):
    by_idx = {s["idx"]: {**s, "recheck": None} for s in state["sections"]}
    for n in state.get("rechecks", []):
        prev = by_idx[n["idx"]]
        adopted = keep_second(prev, n)
        chosen = n if adopted else prev
        # 재파견 중 읽은 것은 채택 여부와 무관하게 기록한다 (예산은 쓰였으므로)
        by_idx[n["idx"]] = {**chosen, "reads": n["reads"], "used": n["used"], "trace": n["trace"],
                            "budget": prev["budget"] + CONFIG["recheck_budget_chars"],
                            "recheck": {"adopted": adopted, "second_draft": n["draft"], "first_draft": prev["draft"]}}
    return {"final_sections": [by_idx[i] for i in sorted(by_idx)]}


# ───────────────────────── ④ 종합 ─────────────────────────
SYNTH_SYSTEM = """You assemble a Korean long-form report. You receive the question and section drafts (not sources).
Write ONLY a Korean title, an introduction (150-250 words) and a conclusion (250-400 words).
The conclusion compares the sections' visions of the future and reflects on how they differ from how we imagine the future today (e.g. AI).
Citations: you may reuse [Dxx] ids that appear in the drafts; never invent ids.
Reply JSON: {"title": "...", "intro": "...", "conclusion": "..."}"""


def synth_node(state: State):
    usage = USAGE[state["run_id"]]
    secs = state["final_sections"]
    drafts = "\n\n".join(f"## {s['heading']}\n{s['draft']}" for s in secs)
    out = llm_json(SYNTH_SYSTEM, f"QUESTION: {state['question']}\n\nDRAFTS:\n{drafts}", usage, max_tokens=1600)
    md = f"# {out.get('title', state['question'])}\n\n{out.get('intro', '')}\n\n"
    md += "\n\n".join(f"## {s['heading']}\n\n{s['draft']}" for s in secs)
    md += f"\n\n## 결론\n\n{out.get('conclusion', '')}\n\n---\n### 인용 문서\n"
    ids = sorted(cited_ids(md))
    md += "\n".join(f"- [{d}] {CORPUS.title(d)}" for d in ids if d in CORPUS.by_id)
    return {"report": {"markdown": md, **out}}


# ───────────────────────── 그래프 ─────────────────────────
def build_graph():
    g = StateGraph(State)
    g.add_node("plan", plan_node)
    g.add_node("validate", validate_node)
    g.add_node("subagent", subagent_node)
    g.add_node("check", lambda s: {})
    g.add_node("recheck", recheck_node)
    g.add_node("merge", merge_node)
    g.add_node("synthesize", synth_node)
    g.add_conditional_edges(START, lambda s: "validate" if s.get("raw_plan") else "plan", ["plan", "validate"])
    g.add_edge("plan", "validate")
    g.add_conditional_edges("validate", dispatch, ["subagent"])
    g.add_edge("subagent", "check")
    g.add_conditional_edges("check", check_route, ["recheck", "merge"])
    g.add_edge("recheck", "merge")
    g.add_edge("merge", "synthesize")
    g.add_edge("synthesize", END)
    return g.compile()


GRAPH = build_graph()


def estimate(plan):
    """실행 전 보고용 예상치 (상한)."""
    n = len(plan)
    per_sub = CONFIG["max_steps"] + 2  # 읽기 단계 + 마지막 판단 + 원고
    calls = n * per_sub + 1 + (n * (CONFIG["recheck_budget_chars"] // CONFIG["max_read_chars"] + 3))
    prompt_tokens = n * per_sub * 2600 + 6000 + n * 4 * 2600
    return {"sections": n, "max_calls": calls, "approx_prompt_tokens": prompt_tokens,
            "approx_cost_usd": round(prompt_tokens * 0.15e-6 + calls * 600 * 0.6e-6, 4),
            "read_budget_chars": n * CONFIG["section_budget_chars"]}


def make_plan(qid, question):
    rid = uuid.uuid4().hex[:8]
    USAGE[rid] = Usage()
    st = {"run_id": rid, "question": question, "switches": dict(CONFIG["switches"])}
    st.update(plan_node(st))
    chk = validate_node(st)
    doc = {"qid": qid, "question": question, "raw_plan": st["raw_plan"], **chk,
           "estimate": estimate(chk["plan"]), "planning_usage": USAGE[rid].as_dict()}
    (OUT / "plans").mkdir(parents=True, exist_ok=True)
    (OUT / "plans" / f"{qid}.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    return doc


def run(qid, question, raw_plan=None, switches=None, tag="base", planning_usage=None):
    rid = uuid.uuid4().hex[:8]
    USAGE[rid] = Usage()
    if raw_plan and planning_usage:  # 승인된 계획을 쓰면 기획 비용·코디네이터가 본 글자를 여기에 합친다
        USAGE[rid].calls += planning_usage["calls"]
        USAGE[rid].prompt_tokens += planning_usage["prompt_tokens"]
        USAGE[rid].completion_tokens += planning_usage["completion_tokens"]
        USAGE[rid].chars_seen.update(planning_usage["chars_seen"])
    sw = {**CONFIG["switches"], **(switches or {})}
    t0 = time.time()
    st = GRAPH.invoke({"run_id": rid, "qid": qid, "question": question, "switches": sw,
                       **({"raw_plan": raw_plan} if raw_plan else {}), "sections": [], "rechecks": []})
    rec = {"run_id": rid, "qid": qid, "tag": tag, "question": question, "switches": sw,
           "seconds": round(time.time() - t0, 1), "plan": st["plan"], "plan_check": st["plan_check"],
           "raw_plan": st.get("raw_plan"), "sections": st["final_sections"], "report": st["report"],
           "usage": USAGE[rid].as_dict()}
    from metrics import compute
    rec["metrics"] = compute(rec, CORPUS)
    save_run(rec)
    return rec


def save_run(rec):
    (OUT / "reports").mkdir(parents=True, exist_ok=True)
    (OUT / "reports" / f"{rec['qid']}_{rec['tag']}_{rec['run_id']}.md").write_text(
        rec["report"]["markdown"], encoding="utf-8")
    import os
    with open(OUT / os.environ.get("RUNS_FILE", "runs.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def load_questions():
    return {q["id"]: q for q in json.loads((ROOT / "data" / "questions.json").read_text(encoding="utf-8"))}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("qid")
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--off", nargs="*", default=[])
    a = ap.parse_args()
    q = load_questions()[a.qid]
    if a.plan:
        print(json.dumps(make_plan(a.qid, q["question"]), ensure_ascii=False, indent=1))
    if a.run:
        pf = OUT / "plans" / f"{a.qid}.json"
        pd = json.loads(pf.read_text(encoding="utf-8")) if pf.exists() else {}
        r = run(a.qid, q["question"], raw_plan=pd.get("raw_plan"), planning_usage=pd.get("planning_usage"),
                switches={k: False for k in a.off},
                tag="base" if not a.off else "off-" + "-".join(a.off))
        print(json.dumps({"metrics": r["metrics"], "usage": r["usage"]}, ensure_ascii=False, indent=1))
