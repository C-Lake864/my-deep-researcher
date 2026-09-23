"""질문 세트 전체의 기획만 돌린다 (질문당 LLM 1회) → output/plans/*.json + 배정표 출력."""
import json

from graph import CORPUS, OUT, load_questions, make_plan

tot = {"calls": 0, "pt": 0, "ct": 0}
for qid, q in load_questions().items():
    f = OUT / "plans" / f"{qid}.json"
    d = json.loads(f.read_text(encoding="utf-8")) if f.exists() else make_plan(qid, q["question"])  # 저장된 계획은 재사용
    u = d["planning_usage"]
    tot["calls"] += u["calls"]; tot["pt"] += u["prompt_tokens"]; tot["ct"] += u["completion_tokens"]
    e = d["estimate"]
    print(f"\n=== {qid} [{q['type']}] sections={len(d['plan'])} est_calls<={e['max_calls']} est_cost<=${e['approx_cost_usd']}"
          f" rejected={d['plan_check']['rejected']} fixed_titles={d['plan_check']['fixed_titles']} plan_tokens={u['prompt_tokens']}")
    print("  rationale:", d["raw_plan"].get("rationale", ""))
    for s in d["plan"]:
        starts = ", ".join(x + " " + CORPUS.title(x) if x in CORPUS.by_id else x for x in s["start_docs"])
        print(f"  - {s['role']:9s} {s['heading']} | start: {starts}")
        print(f"      focus: {s['focus']}")
print("\nplanning total:", tot, "cost $%.4f" % (tot["pt"] * 0.15e-6 + tot["ct"] * 0.6e-6))
