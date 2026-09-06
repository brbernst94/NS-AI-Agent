#!/usr/bin/env python3
"""Score the agent against eval/netsuite_qa.jsonl.

Each question is sent to POST /chat. Two scores are produced:
  - keyword: expect_all substrings all present AND at least one expect_any present
  - judge:   Claude grades the answer against the expected facts (needs ANTHROPIC_API_KEY)

Usage:
  python eval/run_eval.py --api https://web-production-xxxx.up.railway.app
  python eval/run_eval.py --api http://localhost:8000 --limit 10 --no-judge
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

HERE = Path(__file__).parent


def keyword_pass(answer: str, q: dict) -> bool:
    a = answer.lower()
    all_ok = all(s.lower() in a for s in q.get("expect_all", []))
    any_list = q.get("expect_any", [])
    any_ok = True if not any_list else any(s.lower() in a for s in any_list)
    return all_ok and any_ok


def judge(client, model: str, q: dict, answer: str) -> tuple[bool, str]:
    expected = q.get("expect_all", []) + q.get("expect_any", [])
    prompt = (
        "You are grading a NetSuite expert assistant. Decide if the ANSWER is correct and "
        "would satisfy an experienced NetSuite administrator. Expected facts are hints, not a "
        "checklist — equivalent phrasing counts, but wrong internal IDs, wrong menu paths or "
        "hallucinated features fail.\n\n"
        f"QUESTION: {q['question']}\nEXPECTED FACTS (hints): {expected}\nANSWER: {answer}\n\n"
        'Reply with JSON only: {"correct": true|false, "reason": "<one sentence>"}'
    )
    resp = client.messages.create(model=model, max_tokens=200, messages=[{"role": "user", "content": prompt}])
    text = resp.content[0].text.strip()
    try:
        start, end = text.index("{"), text.rindex("}") + 1
        data = json.loads(text[start:end])
        return bool(data.get("correct")), str(data.get("reason", ""))
    except Exception:
        return False, f"unparseable judge output: {text[:120]}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default=os.getenv("NS_AGENT_API", "http://localhost:8000"))
    ap.add_argument("--file", default=str(HERE / "netsuite_qa.jsonl"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-judge", action="store_true")
    ap.add_argument("--judge-model", default=os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6"))
    ap.add_argument("--timeout", type=int, default=180)
    args = ap.parse_args()

    questions = [json.loads(l) for l in Path(args.file).read_text().splitlines() if l.strip()]
    if args.limit:
        questions = questions[: args.limit]

    client = None
    if not args.no_judge:
        try:
            import anthropic

            client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        except Exception as exc:
            print(f"Judge disabled ({exc})")

    results = []
    kw_pass = judge_pass = 0
    session_id = None
    for i, q in enumerate(questions, 1):
        t0 = time.time()
        try:
            r = requests.post(f"{args.api}/chat", json={"message": q["question"]}, timeout=args.timeout)
            r.raise_for_status()
            body = r.json()
            answer = body.get("response") or body.get("message") or json.dumps(body)
        except Exception as exc:
            answer = f"<error: {exc}>"
        elapsed = time.time() - t0

        kw = keyword_pass(answer, q)
        kw_pass += kw
        jd, reason = (None, "")
        if client:
            try:
                jd, reason = judge(client, args.judge_model, q, answer)
                judge_pass += jd
            except Exception as exc:
                reason = f"judge error: {exc}"

        mark = "PASS" if (jd if jd is not None else kw) else "FAIL"
        print(f"[{i:02d}/{len(questions)}] {mark} kw={'y' if kw else 'n'} judge={'-' if jd is None else ('y' if jd else 'n')} {elapsed:4.1f}s  {q['id']}")
        if mark == "FAIL":
            print(f"      Q: {q['question']}")
            print(f"      A: {answer[:300].replace(chr(10), ' ')}")
            if reason:
                print(f"      why: {reason}")
        results.append({**q, "answer": answer, "keyword_pass": kw, "judge_pass": jd, "judge_reason": reason, "seconds": round(elapsed, 1)})

    n = len(questions)
    print(f"\nKeyword score: {kw_pass}/{n} ({100 * kw_pass / n:.0f}%)")
    if client:
        print(f"Judge score:   {judge_pass}/{n} ({100 * judge_pass / n:.0f}%)")

    out = HERE / f"results_{datetime.now():%Y%m%d_%H%M%S}.json"
    out.write_text(json.dumps({"api": args.api, "keyword_pass": kw_pass, "judge_pass": judge_pass, "n": n, "results": results}, indent=1))
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
