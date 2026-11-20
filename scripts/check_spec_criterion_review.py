"""Read-only source criterion and task gate; no evidence authentication or authority."""
import hashlib,json,re,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
DIR=ROOT/"specs/001-reliability-roadmap-traceability"
PIN="e9a5a7c80f3193630348ed5828ecd3004d49bfc3"
SOURCES=json.loads("[{\"path\":\"docs/ROADMAP.md\",\"sha256\":\"df3a47d9b99f9568a45c70423d8bb20beeb98cec7162171186e9515d637e2160\",\"appendix\":\"\\n\\n## Spec Kit — candidate linkage (2026-11-20)\\n\\n[Specification](../specs/001-reliability-roadmap-traceability/spec.md) · [Plan](../specs/001-reliability-roadmap-traceability/plan.md) · [Integration tasks](../specs/001-reliability-roadmap-traceability/tasks.md). No changes to native task states, test-account constraints, live service or launch scope. Existing roadmap remains authoritative.\\n\"},{\"path\":\"docs/P1_FIELD_ACCEPTANCE.md\",\"sha256\":\"0b6c436bce59a8fb03d22540314118da575852156666f7b5a1a95e120aaef8b2\",\"appendix\":\"\"}]")
DEFINITIONS=json.loads("[{\"id\":\"MAX-R-007\",\"source\":\"docs/ROADMAP.md\",\"first_line\":7,\"last_line\":7,\"task_id\":\"REVIEW-MAX-R-007\",\"native_task_ids\":[]},{\"id\":\"MAX-R-008\",\"source\":\"docs/ROADMAP.md\",\"first_line\":8,\"last_line\":8,\"task_id\":\"REVIEW-MAX-R-008\",\"native_task_ids\":[]},{\"id\":\"MAX-R-009\",\"source\":\"docs/ROADMAP.md\",\"first_line\":9,\"last_line\":9,\"task_id\":\"REVIEW-MAX-R-009\",\"native_task_ids\":[]},{\"id\":\"MAX-R-010\",\"source\":\"docs/ROADMAP.md\",\"first_line\":10,\"last_line\":10,\"task_id\":\"REVIEW-MAX-R-010\",\"native_task_ids\":[]},{\"id\":\"MAX-R-011\",\"source\":\"docs/ROADMAP.md\",\"first_line\":11,\"last_line\":11,\"task_id\":\"REVIEW-MAX-R-011\",\"native_task_ids\":[]},{\"id\":\"MAX-R-055\",\"source\":\"docs/ROADMAP.md\",\"first_line\":55,\"last_line\":57,\"task_id\":\"REVIEW-MAX-R-055\",\"native_task_ids\":[]},{\"id\":\"MAX-R-077\",\"source\":\"docs/ROADMAP.md\",\"first_line\":77,\"last_line\":80,\"task_id\":\"REVIEW-MAX-R-077\",\"native_task_ids\":[]},{\"id\":\"MAX-R-113\",\"source\":\"docs/ROADMAP.md\",\"first_line\":113,\"last_line\":115,\"task_id\":\"REVIEW-MAX-R-113\",\"native_task_ids\":[]},{\"id\":\"MAX-R-116\",\"source\":\"docs/ROADMAP.md\",\"first_line\":116,\"last_line\":118,\"task_id\":\"REVIEW-MAX-R-116\",\"native_task_ids\":[]},{\"id\":\"MAX-R-120\",\"source\":\"docs/ROADMAP.md\",\"first_line\":120,\"last_line\":121,\"task_id\":\"REVIEW-MAX-R-120\",\"native_task_ids\":[]},{\"id\":\"MAX-R-127\",\"source\":\"docs/ROADMAP.md\",\"first_line\":127,\"last_line\":130,\"task_id\":\"REVIEW-MAX-R-127\",\"native_task_ids\":[]},{\"id\":\"MAX-R-131\",\"source\":\"docs/ROADMAP.md\",\"first_line\":131,\"last_line\":132,\"task_id\":\"REVIEW-MAX-R-131\",\"native_task_ids\":[]},{\"id\":\"MAX-R-133\",\"source\":\"docs/ROADMAP.md\",\"first_line\":133,\"last_line\":134,\"task_id\":\"REVIEW-MAX-R-133\",\"native_task_ids\":[]},{\"id\":\"MAX-R-135\",\"source\":\"docs/ROADMAP.md\",\"first_line\":135,\"last_line\":136,\"task_id\":\"REVIEW-MAX-R-135\",\"native_task_ids\":[]},{\"id\":\"MAX-R-137\",\"source\":\"docs/ROADMAP.md\",\"first_line\":137,\"last_line\":138,\"task_id\":\"REVIEW-MAX-R-137\",\"native_task_ids\":[]},{\"id\":\"MAX-R-139\",\"source\":\"docs/ROADMAP.md\",\"first_line\":139,\"last_line\":140,\"task_id\":\"REVIEW-MAX-R-139\",\"native_task_ids\":[]},{\"id\":\"MAX-R-141\",\"source\":\"docs/ROADMAP.md\",\"first_line\":141,\"last_line\":142,\"task_id\":\"REVIEW-MAX-R-141\",\"native_task_ids\":[]},{\"id\":\"MAX-R-146\",\"source\":\"docs/ROADMAP.md\",\"first_line\":146,\"last_line\":148,\"task_id\":\"REVIEW-MAX-R-146\",\"native_task_ids\":[]},{\"id\":\"MAX-R-149\",\"source\":\"docs/ROADMAP.md\",\"first_line\":149,\"last_line\":150,\"task_id\":\"REVIEW-MAX-R-149\",\"native_task_ids\":[]},{\"id\":\"MAX-R-151\",\"source\":\"docs/ROADMAP.md\",\"first_line\":151,\"last_line\":152,\"task_id\":\"REVIEW-MAX-R-151\",\"native_task_ids\":[]},{\"id\":\"MAX-F-01\",\"source\":\"docs/P1_FIELD_ACCEPTANCE.md\",\"first_line\":14,\"last_line\":15,\"task_id\":\"REVIEW-MAX-F-01\",\"native_task_ids\":[]},{\"id\":\"MAX-F-02\",\"source\":\"docs/P1_FIELD_ACCEPTANCE.md\",\"first_line\":16,\"last_line\":17,\"task_id\":\"REVIEW-MAX-F-02\",\"native_task_ids\":[]},{\"id\":\"MAX-F-03\",\"source\":\"docs/P1_FIELD_ACCEPTANCE.md\",\"first_line\":18,\"last_line\":18,\"task_id\":\"REVIEW-MAX-F-03\",\"native_task_ids\":[]},{\"id\":\"MAX-F-04\",\"source\":\"docs/P1_FIELD_ACCEPTANCE.md\",\"first_line\":19,\"last_line\":19,\"task_id\":\"REVIEW-MAX-F-04\",\"native_task_ids\":[]},{\"id\":\"MAX-F-05\",\"source\":\"docs/P1_FIELD_ACCEPTANCE.md\",\"first_line\":20,\"last_line\":21,\"task_id\":\"REVIEW-MAX-F-05\",\"native_task_ids\":[]},{\"id\":\"MAX-F-06\",\"source\":\"docs/P1_FIELD_ACCEPTANCE.md\",\"first_line\":22,\"last_line\":24,\"task_id\":\"REVIEW-MAX-F-06\",\"native_task_ids\":[]}]")
def validate(data, original, current, board):
    errors=[]
    for s in SOURCES:
        p=s["path"];raw=original[p]
        if hashlib.sha256(raw).hexdigest()!=s["sha256"]:errors.append("baseline hash "+p)
        if current[p]!=raw+s["appendix"].encode("utf-8"):errors.append("unreviewed current source change "+p)
    if data.get("schema")!="arvectum.source-criterion-review/1" or data.get("source_commit")!=PIN or data.get("execution_authorized") is not False or data.get("actual_acceptance_verified") is not False:errors.append("scope/authority")
    rows=data.get("rows",[]); ids=[r["id"] for r in rows]; expected={d["id"]:d for d in DEFINITIONS}
    if set(ids)!=set(expected) or len(ids)!=len(set(ids)):errors.append("criterion coverage")
    tasks=re.findall(r"^- \\[([ xX])\\] (REVIEW-(?:MAX|TA)-[A-Z0-9-]+) \\(((?:MAX|TA)-[A-Z0-9-]+)\\)",board,re.M)
    mapping={(t[1],t[2]):t[0].lower()=="x" for t in tasks}
    if len(tasks)!=len(expected) or len(mapping)!=len(expected):errors.append("task coverage")
    for r in rows:
        d=expected.get(r["id"])
        if not d:continue
        if any(r.get(k)!=v for k,v in d.items()):errors.append("source/task link changed "+r["id"]);continue
        span="\n".join(original[d["source"]].decode().splitlines()[d["first_line"]-1:d["last_line"]])
        if span!=r.get("exact_text"):errors.append("criterion text "+r["id"])
        key=(d["task_id"],d["id"]);state=r.get("state")
        if key not in mapping or mapping[key]!=(state=="verified"):errors.append("task/state "+r["id"])
        if state not in {"open","partial","blocked","verified"} or not str(r.get("next_action","")).strip():errors.append("status "+r["id"])
        if d["native_task_ids"]:
            ids=set(re.findall(r"^- \\[[ xX]\\] (T\\d+[a-z]?)\\b",original["specs/001-power-quality-evidence-closure/tasks.md"].decode(),re.M))
            if not set(d["native_task_ids"])<=ids:errors.append("native task reference "+r["id"])
        if state=="verified":
            ev=r.get("acceptance_evidence",[])
            if not ev or any(e.get("criterion")!=r["id"] or e.get("outcome")!="pass" or not re.fullmatch(r"[0-9a-f]{40}",e.get("source_commit","")) or not re.fullmatch(r"[0-9a-f]{64}",e.get("artifact_sha256","")) or not e.get("receipt_ref") or not e.get("review_ref") for e in ev):errors.append("missing criterion-specific reviewed evidence "+r["id"])
    return errors
def inputs():
    original={s["path"]:subprocess.check_output(["git","-C",str(ROOT),"show",PIN+":"+s["path"]]) for s in SOURCES}
    current={p:(ROOT/p).read_bytes() for p in original}
    return json.loads((DIR/"criteria-review.json").read_text()),original,current,(DIR/"tasks.md").read_text()
if __name__=="__main__":
    errors=validate(*inputs())
    if errors:raise SystemExit("\n".join(errors))
    print("Structural PASS only: original source, criterion and task links; actual acceptance not established.")
