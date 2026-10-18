#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph_checks.py — 범용 무결성 검사 엔진 (GLOBAL_RULES Rule 14).

프로젝트 귀속 사실은 전부 graph/checks_config.json에 있다 (GLOBAL Rule 10).
이 파일은 프로젝트 간 재사용 가능해야 하며, 프로젝트 이름·경로·도메인 값을
하드코딩하면 안 된다.

검사:
  1. dangling_constants — 선언됐지만 어디서도 참조되지 않는 상수 (INV류)
  2. enum_contract      — enum 값이 계약 선언 범위에 들어가는가
  3. count_claims       — 문서의 "N개 X" 주장 vs 코드 실측 수
  4. quote_exists       — 문서가 인용한 코드 원문이 실재하는가 (Rule 13)
  5. static_asserts     — 지정 구조체의 크기 static_assert 존재
  6. ledger             — graph/edges.csv 스키마·상태 값 유효성

종료 코드: 0 = 위반 없음 또는 전부 대장(open)에 등록됨(known_open),
           1 = 대장에 없는 신규 위반 존재 → 수정하거나 F-항목으로 등록하라.
"""
import csv
import glob
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "graph" / "checks_config.json"
LEDGER = ROOT / "graph" / "edges.csv"
VALID_STATUS = {"open", "fixed", "verified", "rejected", "superseded", "deferred"}

violations = []  # (key, message)


def read(path):
    return Path(path).read_text(encoding="utf-8", errors="ignore")


def glob_read(patterns):
    text = []
    for pat in patterns:
        for f in glob.glob(str(ROOT / pat)):
            text.append(read(f))
    return "\n".join(text)


def check_dangling_constants(cfg):
    decl_file = ROOT / cfg["decl_file"]
    consts = re.findall(cfg["decl_pattern"], read(decl_file))
    corpus = glob_read(cfg["search_globs"])
    # 선언 파일 자신은 제외하고 검색된 말뭉치에서 참조를 찾는다.
    corpus = corpus.replace(read(decl_file), "")
    for c in consts:
        if c in cfg.get("allow", []):
            continue
        if c not in corpus:
            violations.append((c, f"[dangling] {cfg['decl_file']}: {c} 선언만 있고 참조 0건"))


def check_enum_contract(items):
    for it in items:
        enum_src = read(ROOT / it["enum_file"])
        m = re.search(r"enum class %s[^}]+}" % it["enum_name"], enum_src)
        if not m:
            violations.append((it["enum_name"], f"[enum_contract] enum {it['enum_name']} 미발견"))
            continue
        vals = [int(v) for v in re.findall(r"=\s*(\d+)", m.group(0))]
        cm = re.search(it["contract_pattern"], read(ROOT / it["contract_file"]))
        if not cm:
            violations.append((it["enum_name"], f"[enum_contract] 계약 패턴 미발견: {it['contract_file']}"))
            continue
        lo, hi = float(cm.group(1)), float(cm.group(2))
        bad = [v for v in vals if not lo <= v <= hi]
        if bad:
            violations.append((it["enum_name"],
                               f"[enum_contract] {it['enum_name']} 값 {bad}가 계약범위 [{lo:g},{hi:g}] 밖"))


def check_count_claims(items):
    for it in items:
        matches = set(re.findall(it["code_pattern"], glob_read(it["code_globs"])))
        actual = len(matches)
        for doc in [it["doc"]] + it.get("also_docs", []):
            for claim in re.findall(it["claim_pattern"], read(ROOT / doc)):
                if int(claim) != actual:
                    violations.append((f"claim:{claim}개 토픽",
                                       f"[count_claim] {doc}: 주장 {claim} vs 실측 {actual}"))


def check_quote_exists(items):
    for it in items:
        doc_has = it["quote"]
        target = read(ROOT / it["search_file"])
        a, b = (re.sub(r"\s+", " ", doc_has), re.sub(r"\s+", " ", target)) \
            if it.get("normalize_ws") else (doc_has, target)
        if a not in b:
            violations.append((f"quote:{it['search_file']}",
                               f"[quote] {it['doc']}가 인용한 원문이 {it['search_file']}에 없음"))


def check_static_asserts(items):
    for it in items:
        src = read(ROOT / it["file"])
        for s in it["structs"]:
            if not re.search(r"static_assert\(sizeof\(%s\)" % s, src):
                violations.append((f"static_assert:{s}",
                                   f"[static_assert] {it['file']}: {s} 크기 단언 없음"))


def check_ledger():
    if not LEDGER.exists():
        violations.append(("ledger", "[ledger] graph/edges.csv 부재"))
        return {}
    with open(LEDGER, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    need = {"id", "type", "severity", "title", "location", "status"}
    if rows and not need.issubset(rows[0].keys()):
        violations.append(("ledger", f"[ledger] 필수 컬럼 누락: {need - set(rows[0].keys())}"))
    ids = {}
    for r in rows:
        if r["id"] in ids:
            violations.append((r["id"], f"[ledger] 중복 ID: {r['id']}"))
        ids[r["id"]] = r
        if r["status"] not in VALID_STATUS:
            violations.append((r["id"], f"[ledger] {r['id']} 상태값 무효: {r['status']}"))
    return ids


def main():
    cfg = json.loads(read(CONFIG))
    ledger = check_ledger()
    if "dangling_constants" in cfg:
        check_dangling_constants(cfg["dangling_constants"])
    check_enum_contract(cfg.get("enum_contract", []))
    check_count_claims(cfg.get("count_claims", []))
    check_quote_exists(cfg.get("quote_exists", []))
    check_static_asserts(cfg.get("static_asserts", []))

    known = cfg.get("known_open", {})
    new_violations = []
    known_open = []
    for key, msg in violations:
        fid = known.get(key)
        row = ledger.get(fid) if fid else None
        if row and row["status"] in {"open", "deferred"}:
            known_open.append({"ledger_id": fid, "message": msg})
            print(f"  KNOWN-OPEN ({fid}): {msg}")
        else:
            new_violations.append(msg)
            print(f"  NEW: {msg}")

    # Rule 13 (producing side): every verification run emits a machine-readable
    # evidence file so documents can cite a path instead of hand-copying counts.
    import datetime
    evidence = {
        "tool": "graph_checks",
        "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
        "ledger_rows": len(ledger),
        "violations_total": len(violations),
        "known_open": known_open,
        "new": new_violations,
        "exit_code": 1 if new_violations else 0,
    }
    import os
    out = Path(os.environ.get('WSFLOW_GRAPH_EVIDENCE') or ROOT / "logs" / "graph_checks_last.json").resolve()
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\ngraph_checks: 위반 {len(violations)}건 "
          f"(대장 등록 {len(violations)-len(new_violations)} / 신규 {len(new_violations)})")
    print(f"evidence: {out.relative_to(ROOT)}")
    if new_violations:
        print("신규 위반은 수정하거나 graph/edges.csv에 open으로 등록하고 "
              "checks_config.json known_open에 매핑하라 (GLOBAL Rule 14).")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
