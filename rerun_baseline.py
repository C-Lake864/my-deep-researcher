"""대조군만 다시 돌려 output/ablation/<qid>.json 의 baseline 항목을 교체한다 (예산 소진 수정 후)."""
import json
import sys

from ablation import summarize
from baseline import run_baseline
from graph import OUT, load_questions

qs = load_questions()
qid, repeats = sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 2
plan = json.loads((OUT / "plans" / f"{qid}.json").read_text(encoding="utf-8"))["plan"]
runs = [run_baseline(qid, qs[qid]["question"], plan) for _ in range(repeats)]
for r in runs:
    s = r["metrics"]["signals"]
    print(qid, "baseline", s["calls"], s["coverage_docs"], s["citation_density"], s["budget_utilization"], s.get("forced_continues"))
path = OUT / "ablation" / f"{qid}.json"
res = json.loads(path.read_text(encoding="utf-8"))
res["baseline_v1_underused"] = res.pop("baseline")  # 불공정했던 첫 대조군은 기록으로 남긴다
res["baseline"] = {"run_ids": [r["run_id"] for r in runs],
                   "signals": {k: summarize([r["metrics"]["signals"].get(k) or 0 for r in runs]) for k in runs[0]["metrics"]["signals"]},
                   "alarms": {k: summarize([r["metrics"]["alarms"][k] for r in runs]) for k in runs[0]["metrics"]["alarms"]}}
path.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
