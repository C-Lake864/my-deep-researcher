"""World of Tomorrow 딥리서처 데모.

  streamlit run app.py                 # 로컬: 계획 → 승인 → 실행 가능
  PUBLIC_MODE=1 streamlit run app.py   # 공개: 저장된 결과만 (API 호출 없음)
"""
import json
import os

import streamlit as st

from core import CONFIG, ROOT, Corpus

st.set_page_config(page_title="World of Tomorrow 딥리서처", layout="wide")
OUT = ROOT / "output"
PUBLIC = os.environ.get("PUBLIC_MODE") == "1" or not os.environ.get("OPENAI_API_KEY")
PALETTE = ["#4e79a7", "#f28e2b", "#59a14f", "#e15759", "#b07aa1", "#76b7b2"]


@st.cache_resource
def corpus():
    return Corpus()


def load_runs():
    p = OUT / "runs.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def questions():
    return json.loads((ROOT / "data" / "questions.json").read_text(encoding="utf-8"))


C = corpus()


def label(d):
    return f"{d} {C.title(d)}" if d in C.by_id else f"{d} (없는 문서)"


# ───────────────────────── 관계도 ─────────────────────────
def read_graph(rec):
    """누가 무엇을 읽었나: 문서 노드를 읽은 절의 색으로, 코퍼스 링크를 회색 선으로, 시작 배정을 굵은 테두리로."""
    secs = rec["sections"]
    owner = {}
    for i, s in enumerate(secs):
        for r in s["reads"]:
            owner.setdefault(r["doc"], set()).add(i)
    starts = {d for s in secs for d in s["start_docs"]}
    lines = ['digraph G { rankdir=LR; bgcolor="transparent"; node [style=filled, fontname="sans-serif", fontsize=10, shape=box];']
    for i, s in enumerate(secs):
        lines.append(f'"sec{i}" [label="{s["heading"]}\\n({s["role"]})", shape=ellipse, fillcolor="{PALETTE[i % 6]}", fontcolor=white];')
    for d, o in owner.items():
        color = PALETTE[min(o) % 6] if len(o) == 1 else "#bbbbbb"
        pen = 3 if d in starts else 1
        n_sec = sum(1 for s in secs for r in s["reads"] if r["doc"] == d)
        lines.append(f'"{d}" [label="{d}\\n{C.title(d)[:28]}\\n{n_sec}절 읽음{" · 공유" if len(o) > 1 else ""}", '
                     f'fillcolor="{color}33", color="{color}", penwidth={pen}];')
        for i in o:
            lines.append(f'"sec{i}" -> "{d}" [color="{PALETTE[i % 6]}"];')
    for d in owner:
        for x in C.links.get(d, []):
            if x in owner:
                lines.append(f'"{d}" -> "{x}" [color="#cccccc", style=dashed, arrowsize=0.5];')
    lines.append("}")
    return "\n".join(lines)


# ───────────────────────── 화면: 한 실행 ─────────────────────────
def show_run(rec):
    m = rec["metrics"]
    sig, al = m["signals"], m["alarms"]
    c = st.columns(6)
    c[0].metric("절 수", sig["sections"])
    c[1].metric("읽은 문서", sig["coverage_docs"])
    c[2].metric("중복률", f"{sig['overlap_rate']:.0%}")
    c[3].metric("인용 밀도", f"{sig['citation_density']:.0%}")
    c[4].metric("예산 소진", f"{sig['budget_utilization']:.0%}")
    c[5].metric("LLM 호출", sig["calls"])
    bad = {k: v for k, v in al.items() if v}
    if bad:
        st.error("경보: " + ", ".join(f"{k}={v}" for k, v in bad.items()))
    else:
        st.success("경보 0 — 읽지 않은 문서 인용 · 없는 문서 인용/배정 · 빈 절 · 구역 위반 없음")

    names = ["📄 보고서", "🧭 절별 추적 (누가 무엇을 읽고 썼나)", "🕸 읽은 문서 관계도", "📏 격리·지표"]
    view = st.query_params.get("view")  # ?view=report|trace|graph|metrics → 한 화면만 (캡처용)
    only = ["report", "trace", "graph", "metrics"].index(view) if view in ("report", "trace", "graph", "metrics") else None
    t1, t2, t3, t4 = [st.container()] * 4 if only is not None else st.tabs(names)
    show = lambda i: only is None or only == i
    if show(0):
      with t1:
        st.markdown(rec["report"]["markdown"])
    if show(1):
      with t2:
        if rec.get("raw_plan"):
            st.info("코디네이터의 분할 이유: " + str(rec["raw_plan"].get("rationale", "")))
        pc = rec.get("plan_check", {})
        if pc.get("rejected"):
            st.warning(f"배정 검사에서 걸러진 것 ({'검사 켜짐' if pc.get('validate_on') else '검사 꺼짐 — 그대로 파견'}): {pc['rejected']}")
        for i, s in enumerate(rec["sections"]):
            with st.expander(f"§{i + 1} {s['heading']} — {s['role']} · 읽은 절 {len(s['reads'])}개 · "
                             f"{s['used']:,}/{s['budget']:,}자" + (" · 재파견" if s.get("recheck") else ""), expanded=i == 0):
                a, b = st.columns([2, 3])
                with a:
                    st.markdown(f"**맡은 질문(focus)**: {s['focus']}")
                    st.markdown("**시작 배정**: " + ", ".join(label(d) for d in s["start_docs"]))
                    if s.get("others"):
                        st.caption("알려 준 남의 구역: " + s["others"][:400] + ("…" if len(s["others"]) > 400 else ""))
                    st.markdown("**읽은 순서**")
                    rows = [{"#": k + 1, "문서": label(t["doc"]), "절": t["section"],
                             "글자": t.get("chars", 0) if not t.get("rejected") else f"거절({t['rejected']})",
                             "고른 이유": t.get("why", "")} for k, t in enumerate(s["trace"])]
                    st.dataframe(rows, hide_index=True, use_container_width=True)
                    with st.popover(f"메모 {len(s['notes'])}개 (원문 대신 위로 올라간 것)"):
                        st.write("\n".join("- " + n for n in s["notes"]))
                with b:
                    st.markdown(f"**올린 원고** · 자기 신고: {'충분' if s['sufficient'] else '부족 — ' + s['missing']}")
                    st.markdown(s["draft"])
                    if s.get("recheck"):
                        rc = s["recheck"]
                        st.markdown(f"**재파견 결과**: 두 번째 원고 {'채택' if rc['adopted'] else '기각 (첫 원고 유지)'}")
                        with st.popover("첫 원고 / 두 번째 원고 비교"):
                            x, y = st.columns(2)
                            x.markdown("**첫 원고**\n\n" + rc["first_draft"])
                            y.markdown("**두 번째 원고**\n\n" + rc["second_draft"])
    if show(2):
      with t3:
        st.caption("타원 = 절(서브에이전트) · 상자 = 읽은 문서(색 = 읽은 절, 회색 = 여러 절이 공유) · "
                   "굵은 테두리 = 코디네이터의 시작 배정 · 점선 = 코퍼스 안의 링크")
        st.graphviz_chart(read_graph(rec), use_container_width=True)
    if show(3):
      with t4:
        st.markdown(f"코디네이터가 본 원문 **{sig['chars_coordinator']:,}자** (허브 문서 앞부분뿐) · "
                    f"서브에이전트가 본 원문 **{sig['chars_subagents']:,}자** · 코퍼스 전체 {C.total_chars():,}자")
        st.bar_chart({k: v for k, v in rec["usage"]["chars_seen"].items()}, horizontal=True)
        st.json(m)


# ───────────────────────── 화면: 로컬 실행 ─────────────────────────
def local_runner():
    from graph import estimate, make_plan, run
    st.subheader("새 질문 실행 (로컬 전용)")
    qs = questions()
    pick = st.selectbox("질문 세트에서 고르기", ["(직접 입력)"] + [f"{q['id']} [{q['type']}] {q['question']}" for q in qs])
    if pick == "(직접 입력)":
        qid, question = "custom", st.text_input("질문 (영어 권장 — 코퍼스가 영어 위키백과)")
    else:
        q = qs[int(pick.split()[0][1:]) - 1]
        qid, question = q["id"], q["question"]
        st.caption("왜 나눌 만한가: " + q["why_split"])
    if st.button("① 계획 세우기 (LLM 1회)", disabled=not question):
        st.session_state["plan"] = make_plan(qid, question)
    p = st.session_state.get("plan")
    if p and p["question"] == question:
        st.markdown("#### 실행 전 오케스트레이션 계획")
        st.info(p["raw_plan"].get("rationale", ""))
        st.dataframe([{"절": s["heading"], "역할": s["role"], "시작 문서": ", ".join(label(d) for d in s["start_docs"]),
                       "예산(자)": s["budget"], "맡은 질문": s["focus"]} for s in p["plan"]],
                     hide_index=True, use_container_width=True)
        if p["plan_check"]["rejected"]:
            st.warning(f"배정 검사에서 걸러짐: {p['plan_check']['rejected']}")
        e = p["estimate"]
        st.markdown(f"예상(상한): 호출 **{e['max_calls']}회** · 입력 약 **{e['approx_prompt_tokens']:,}토큰** · "
                    f"약 **${e['approx_cost_usd']}** · 읽기 예산 합계 {e['read_budget_chars']:,}자")
        if st.button("② 승인하고 실행", type="primary"):
            with st.spinner("서브에이전트 동시 파견 중…"):
                rec = run(qid, question, raw_plan=p["raw_plan"], planning_usage=p["planning_usage"])
            st.session_state["last_run"] = rec["run_id"]
            st.rerun()


# ───────────────────────── 화면: 비교 ─────────────────────────
def compare(runs):
    st.subheader("같은 질문, 다른 설정 — 나란히 읽기")
    ab = OUT / "ablation.json"
    if ab.exists():
        data = json.loads(ab.read_text(encoding="utf-8"))
        rows = []
        for qid, cfgs in data.items():
            for name, v in cfgs.items():
                s, a = v["signals"], v["alarms"]
                rows.append({"질문": qid, "설정": name, "반복": len(v["run_ids"]),
                             "읽은 문서": f"{s['coverage_docs']['mean']:.1f} ({s['coverage_docs']['min']}–{s['coverage_docs']['max']})",
                             "중복률": f"{s['overlap_rate']['mean']:.2f} ({s['overlap_rate']['min']:.2f}–{s['overlap_rate']['max']:.2f})",
                             "인용 밀도": f"{s['citation_density']['mean']:.2f}",
                             "예산 소진": f"{s['budget_utilization']['mean']:.2f}",
                             "호출": f"{s['calls']['mean']:.0f}",
                             "경보 합": sum(x["mean"] for x in a.values())})
        st.caption("괄호 = 시행 간 최소–최대. 한 번 돌린 결과로 방향을 단정하지 않기 위해 범위를 같이 보여 준다. 지표는 실패를 거르는 용도이지 순위표가 아니다.")
        st.dataframe(rows, hide_index=True, use_container_width=True)
    qids = sorted({r["qid"] for r in runs})
    qid = st.selectbox("질문", qids, key="cmp_q")
    cands = [r for r in runs if r["qid"] == qid]
    names = [f"{r['tag']} · {r['run_id']}" for r in cands]
    a, b = st.columns(2)
    for col, key, default in ((a, "L", 0), (b, "R", min(1, len(names) - 1))):
        with col:
            i = st.selectbox("실행", range(len(names)), format_func=lambda k: names[k], index=default, key=key)
            st.markdown(cands[i]["report"]["markdown"])


# ───────────────────────── 메인 ─────────────────────────
st.title("World of Tomorrow — 1939 뉴욕세계박람회는 미래를 어떻게 상상했는가")
st.caption(f"코퍼스 {len(C.by_id)}건 · {C.total_chars():,}자 (≈{C.total_chars() // 4:,} 토큰, 모델 창 128k 의 "
           f"{C.total_chars() / 4 / 128000:.1f}배) · 모델 {CONFIG['model']} · 역할 {len(CONFIG['roles'])}개")
runs = load_runs()
mode = st.sidebar.radio("보기", ["저장된 보고서", "나란히 비교"] + ([] if PUBLIC else ["새 질문 실행"]))
if PUBLIC:
    st.sidebar.info("공개 보기 모드 — 미리 돌려 둔 결과만 보여 주며 API 를 호출하지 않습니다.")
st.sidebar.markdown("**역할 명단**\n\n" + "\n".join(f"- `{r}` {v['label']}" for r, v in CONFIG["roles"].items()))

if mode == "새 질문 실행":
    local_runner()
elif mode == "나란히 비교":
    if runs:
        compare(runs)
    else:
        st.info("아직 저장된 실행이 없습니다.")
else:
    if not runs:
        st.info("아직 저장된 실행이 없습니다.")
    else:
        qmap = {q["id"]: q for q in questions()}
        opts = list(reversed(runs))
        last = st.session_state.get("last_run") or st.query_params.get("run")
        idx = next((k for k, r in enumerate(opts) if r["run_id"] == last),
                   next((k for k, r in enumerate(opts) if r["qid"] == "q1" and r["tag"] == "base"), 0))
        k = st.sidebar.selectbox("실행 기록", range(len(opts)), index=idx,
                                 format_func=lambda k: f"{opts[k]['qid']} · {opts[k]['tag']} · {opts[k]['run_id']}")
        rec = opts[k]
        st.markdown(f"**질문** {rec['question']}")
        if rec["qid"] in qmap:
            st.caption("왜 나눌 만한가: " + qmap[rec["qid"]]["why_split"])
        show_run(rec)
