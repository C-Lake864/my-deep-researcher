"""혼자 하는 대조군 — 같은 자료 · 모델 · 도구(research_loop) · 같은 읽기 예산.

공정성 규칙
  예산   = 기본 실행의 절 수 × 절 예산   (서브에이전트 예산 합계와 같다)
  걸음   = 절 수 × max_steps
  시작   = 코디네이터가 배정한 시작 문서의 합집합 (입구를 같게 → 차이는 분업·격리에서만)
  구역   = 없음 (혼자이므로)  · 허브 문서는 제외 (서브에이전트와 동일)
  멈춤   = 예산의 baseline_min_utilization 전에 멈추려 하면 코드가 계속 읽게 한다
"""
import json
import time
import uuid

from core import CONFIG, Usage, llm_json, research_loop
from graph import CORPUS, HUB, OUT, USAGE, load_questions, save_run

BASE_WRITE = """You write a complete Korean long-form research report from your notes ONLY.
Organize it into sections of your choice with '## ' headings, {words} words total.
Put a citation like [D07] at the end of every sentence that states a fact; cite ONLY doc ids that appear in your notes.
Focus on how the fair imagined the future and whose interests it served; end with a conclusion comparing then and now.
Reply JSON: {{"title": "...", "markdown": "...(without the title)"}}"""


def run_baseline(qid, question, plan):
    rid = uuid.uuid4().hex[:8]
    USAGE[rid] = usage = Usage()
    n = len(plan)
    budget = n * CONFIG["section_budget_chars"]
    starts = [d for s in plan for d in s["start_docs"] if d in CORPUS.by_id]
    allowed = {d for d in CORPUS.by_id if d not in HUB}
    t0 = time.time()
    r = research_loop(CORPUS, usage, "solo", question, starts, budget, n * CONFIG["max_steps"],
                      allowed=allowed, others=None, min_utilization=CONFIG["baseline_min_utilization"])
    out = llm_json(BASE_WRITE.format(words=n * 450), "QUESTION: " + question + "\nNOTES:\n" + "\n".join(r["notes"]),
                   usage, max_tokens=min(4000, 900 * n + 600))
    md = f"# {out.get('title', question)}\n\n{out.get('markdown', '')}"
    sec = {"idx": 0, "role": "solo", "heading": "solo", "focus": question, "start_docs": starts,
           "budget": budget, "draft": out.get("markdown", ""), "sufficient": True, "missing": "",
           "notes": r["notes"], "reads": r["reads"], "used": r["used"], "trace": r["trace"],
           "visited": r["visited"], "allowed": None, "others": None,
           "forced_continues": r["forced_continues"], "recheck": None}
    rec = {"run_id": rid, "qid": qid, "tag": "baseline", "question": question,
           "switches": {"zones": False, "validate": False, "recheck": False},
           "seconds": round(time.time() - t0, 1), "plan": [sec | {"draft": ""}], "plan_check": {},
           "sections": [sec], "report": {"markdown": md, "intro": "", "conclusion": ""}, "usage": usage.as_dict()}
    from metrics import compute
    rec["metrics"] = compute(rec, CORPUS)
    rec["metrics"]["signals"]["forced_continues"] = r["forced_continues"]
    save_run(rec)
    return rec


if __name__ == "__main__":
    import sys
    qid = sys.argv[1]
    q = load_questions()[qid]
    plan = json.loads((OUT / "plans" / f"{qid}.json").read_text(encoding="utf-8"))["plan"]
    rec = run_baseline(qid, q["question"], plan)
    print(json.dumps(rec["metrics"], ensure_ascii=False, indent=1))
