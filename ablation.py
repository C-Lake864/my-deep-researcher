"""스위치를 하나씩 끄고 잰다. 같은 (승인된) 계획을 모든 설정에 쓴다 → 차이는 스위치에서만.

  python ablation.py q1 q2 q3 --repeats 2
"""
import argparse
import json
import statistics

from baseline import run_baseline
from graph import OUT, load_questions, run

ALL_CONFIGS = [("base", {}), ("off-zones", {"zones": False}), ("off-validate", {"validate": False}),
               ("off-recheck", {"recheck": False}), ("baseline", None)]
# off-validate 는 v2 계획에 무효 배정이 0 이라 기본과 결과가 같다 → 기본 실험에서 제외 (--with-validate 로 포함)


def summarize(values):
    return {"mean": round(statistics.mean(values), 3), "min": min(values), "max": max(values)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("qids", nargs="+")
    ap.add_argument("--repeats", type=int, default=2)
    ap.add_argument("--with-validate", action="store_true")
    a = ap.parse_args()
    qs = load_questions()
    configs = [c for c in ALL_CONFIGS if a.with_validate or c[0] != "off-validate"]
    (OUT / "ablation").mkdir(exist_ok=True)
    for qid in a.qids:
        path = OUT / "ablation" / f"{qid}.json"  # 질문별 파일 → 병렬 프로세스끼리 덮어쓰지 않는다
        result = {}
        plan_doc = json.loads((OUT / "plans" / f"{qid}.json").read_text(encoding="utf-8"))
        for name, sw in configs:
            runs = []
            for i in range(a.repeats):
                if sw is None:
                    rec = run_baseline(qid, qs[qid]["question"], plan_doc["plan"])
                else:
                    rec = run(qid, qs[qid]["question"], raw_plan=plan_doc["raw_plan"], switches=sw, tag=name,
                              planning_usage=plan_doc["planning_usage"])
                runs.append(rec)
                m = rec["metrics"]
                print(f"{qid} {name:13s} #{i} calls={m['signals']['calls']:3d} cov={m['signals']['coverage_docs']:2d} "
                      f"overlap={m['signals']['overlap_rate']:.2f} cite={m['signals']['citation_density']:.2f} "
                      f"util={m['signals']['budget_utilization']:.2f} alarms={sum(m['alarms'].values())}", flush=True)
            keys = runs[0]["metrics"]["signals"].keys()
            result[name] = {
                "run_ids": [r["run_id"] for r in runs],
                "signals": {k: summarize([r["metrics"]["signals"].get(k) or 0 for r in runs]) for k in keys},
                "alarms": {k: summarize([r["metrics"]["alarms"][k] for r in runs]) for k in runs[0]["metrics"]["alarms"]},
            }
        path.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()


def merge_all():
    """output/ablation/*.json → output/ablation.json"""
    merged = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted((OUT / "ablation").glob("*.json"))}
    (OUT / "ablation.json").write_text(json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
    return merged
