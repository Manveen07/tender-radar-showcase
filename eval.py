"""Score classifier verdicts against the hand-labeled golden set.

Same role as leadlens's eval: it's the honesty check on the classifier. We report
exact-match accuracy on the 3-way label, and — because the business cost is
asymmetric — a binary pursue/drop confusion matrix where the two error types mean
very different things:

  - FALSE BID  (we said pursue, gold says skip)  -> wasted bid effort. Annoying.
  - FALSE SKIP (we said drop, gold says bid/maybe) -> a winnable contract never
    seen. This is the expensive miss — the whole product exists to prevent it.

'maybe' collapses to pursue for the binary view (a maybe still lands in front of
the human, which is the safe outcome).

Usage:
    uv run python eval.py
    uv run python eval.py --outputs data/tender-outputs.jsonl --gold data/golden-tenders.jsonl
"""

import argparse
import json
from pathlib import Path


def load(path: Path) -> dict[str, dict]:
    return {
        json.loads(line)["id"]: json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def pursue(label: str) -> bool:
    return label in ("bid", "maybe")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--outputs", default="data/tender-outputs.jsonl")
    parser.add_argument("--gold", default="data/golden-tenders.jsonl")
    args = parser.parse_args()

    outputs = load(Path(args.outputs))
    gold = load(Path(args.gold))

    ids = [i for i in gold if i in outputs]
    missing = [i for i in gold if i not in outputs]
    if missing:
        print(f"WARNING: {len(missing)} golden ids not in outputs (run runner first): {missing}")
    if not ids:
        raise SystemExit("No overlap between outputs and golden set — nothing to score.")

    exact = 0
    false_bid: list[str] = []  # pred pursue, gold skip
    false_skip: list[str] = []  # pred skip, gold pursue
    tp = tn = 0

    print(f"{'id':<14} {'pred':<6} {'gold':<6} {'match':<6}")
    print("-" * 36)
    for i in ids:
        pred = outputs[i]["bid_recommendation"]
        g = gold[i]["gold"]
        ok = pred == g
        exact += ok
        print(f"{i:<14} {pred:<6} {g:<6} {'OK' if ok else 'X':<6}")

        p_pursue, g_pursue = pursue(pred), pursue(g)
        if p_pursue and g_pursue:
            tp += 1
        elif not p_pursue and not g_pursue:
            tn += 1
        elif p_pursue and not g_pursue:
            false_bid.append(i)
        else:
            false_skip.append(i)

    n = len(ids)
    print("\n=== 3-way label ===")
    print(f"  exact-match accuracy: {exact}/{n} = {exact/n:.0%}")

    print("\n=== binary pursue/drop (maybe -> pursue) ===")
    print(f"  correct pursue (TP): {tp}    correct drop (TN): {tn}")
    print(f"  FALSE BID  (wasted effort):  {len(false_bid)}  {false_bid}")
    print(
        f"  FALSE SKIP (missed contract): {len(false_skip)}  {false_skip}  <-- the expensive error"
    )
    binary_acc = (tp + tn) / n
    print(f"  binary accuracy: {binary_acc:.0%}")

    if false_skip:
        print("\n  Investigate FALSE SKIPs first — each is a winnable contract the radar hid.")


if __name__ == "__main__":
    main()
