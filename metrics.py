"""지표 — 정답표도 판정 모델도 쓰지 않는다. 만들어진 글과 실제로 읽은 자료만 본다.

signals (높낮이를 견준다)            → 어느 장치를 보는가
  coverage_docs      읽은 서로 다른 문서 수       → 분업(배정) : 여럿이 나눠 읽으면 넓어져야 한다
  overlap_rate       2개 이상 절이 읽은 문서 비율 → 구역 알림(zones) : 켜면 낮아져야 한다
  citation_density   인용이 붙은 문장 비율        → 서브에이전트 원고 : 메모의 근거가 글까지 올라왔나
  budget_utilization 쓴 읽기 예산 / 준 예산       → 대조군 공정성 : 낮으면 상대를 묶어 둔 것
  isolation_ratio    서브에이전트가 본 원문 / 코디네이터가 본 원문 → 격리 : 코디네이터는 원문을 거의 안 본다
alarms (0 이어야 한다)
  cited_unread       그 절이 읽지 않은 문서를 인용  → 서브에이전트 환각
  cited_nonexistent  코퍼스에 없는 id 인용         → 환각
  invalid_dispatched 없는 문서가 배정된 채 파견됨  → 배정 검사(validate)
  empty_sections     원고가 빈 절                  → 서브에이전트/쓰기 실패
  zone_violations    구역을 켰는데 남의 구역을 읽음 → 구역 규칙 코드 경로
"""
import re

from core import CITE_RE, cited_ids

SPLIT_RULE = "v1: split on [.!?] + whitespace or newline; drop headings/bullets and pieces < 15 chars"
_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def sentences(md):
    out = []
    for piece in _SPLIT.split(md):
        p = piece.strip()
        if len(p) < 15 or p.startswith("#") or p.startswith("- ") or p == "---":
            continue
        out.append(p)
    return out


def compute(rec, corpus):
    secs = rec["sections"]
    read_sets = [{r["doc"] for r in s["reads"]} for s in secs]
    all_read = set().union(*read_sets) if read_sets else set()
    count = {}
    for rs in read_sets:
        for d in rs:
            count[d] = count.get(d, 0) + 1

    cited_unread, cited_nonexistent, cited_sents, total_sents = 0, 0, 0, 0
    for s, rs in zip(secs, read_sets):
        ids = cited_ids(s["draft"])
        cited_nonexistent += len([d for d in ids if d not in corpus.by_id])
        cited_unread += len([d for d in ids if d in corpus.by_id and d not in rs])
        ss = sentences(s["draft"])
        total_sents += len(ss)
        cited_sents += sum(1 for x in ss if CITE_RE.search(x))
    rep = rec["report"]
    for part in (rep.get("intro", ""), rep.get("conclusion", "")):
        ids = cited_ids(part)
        cited_nonexistent += len([d for d in ids if d not in corpus.by_id])
        cited_unread += len([d for d in ids if d in corpus.by_id and d not in all_read])

    zone_violations = 0
    if rec["switches"].get("zones") and rec.get("tag") != "baseline":
        for s in secs:
            if s.get("allowed") is not None:
                zone_violations += len([r for r in s["reads"] if r["doc"] not in set(s["allowed"])])

    seen = rec["usage"]["chars_seen"]
    coord = seen.get("coordinator", 0)
    sub = sum(v for k, v in seen.items() if k != "coordinator")
    budget = sum(s["budget"] for s in secs)  # 재파견 예산은 merge 에서 절 예산에 합쳐 둔다
    used = sum(s["used"] for s in secs)
    return {
        "split_rule": SPLIT_RULE,
        "signals": {
            "sections": len(secs),
            "coverage_docs": len(all_read),
            "overlap_rate": round(sum(1 for c in count.values() if c > 1) / len(all_read), 3) if all_read else 0,
            "citation_density": round(cited_sents / total_sents, 3) if total_sents else 0,
            "cited_docs": len({d for s in secs for d in cited_ids(s["draft"])}),
            "budget_utilization": round(used / budget, 3) if budget else 0,
            "chars_coordinator": coord,
            "chars_subagents": sub,
            "isolation_ratio": round(sub / coord, 1) if coord else None,
            "calls": rec["usage"]["calls"],
            "prompt_tokens": rec["usage"]["prompt_tokens"],
            "completion_tokens": rec["usage"]["completion_tokens"],
            "report_chars": len(rep.get("markdown", "")),
        },
        "alarms": {
            "cited_unread": cited_unread,
            "cited_nonexistent": cited_nonexistent,
            "invalid_dispatched": rec.get("plan_check", {}).get("invalid_dispatched", 0),
            "empty_sections": sum(1 for s in secs if not s["draft"].strip()),
            "zone_violations": zone_violations,
        },
    }
