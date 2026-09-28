"""Package a finished run into submission/: stamp, validate, audit, score, copy, gzip, rebuild the site.

    python tools/package_submission.py --run out/run-final7 --smoke out/smoke-100-r4 --code-commit <short sha>

--code-commit is the commit whose signalpost/ package produced the envelopes. A run started on a working tree
stamps the last commit it saw, so the stamp is rewritten to the commit that actually holds that code; the script
refuses if signalpost/ at that commit differs from the working tree.
"""
from __future__ import annotations

import argparse
import gzip
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PY = sys.executable


def sh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), cwd=ROOT, capture_output=True, text=True, encoding="utf-8")


def restamp(path: Path, stamp: str) -> None:
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    for e in rows:
        e["run"]["agent_version"] = stamp
    path.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in rows), encoding="utf-8")
    rep = path.parent / "run-report.json"
    r = json.loads(rep.read_text(encoding="utf-8"))
    r["agent_version"] = stamp
    rep.write_text(json.dumps(r, indent=2, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True, help="the full run to package")
    ap.add_argument("--smoke", required=True, help="the 100-company smoke test made with ./run.sh")
    ap.add_argument("--code-commit", required=True)
    a = ap.parse_args()
    run, smoke, sub = ROOT / a.run, ROOT / a.smoke, ROOT / "submission"

    if sh("git", "diff", "--quiet", a.code_commit, "--", "signalpost").returncode != 0:
        print(f"refusing: signalpost/ in the working tree differs from {a.code_commit}", file=sys.stderr)
        return 2
    from signalpost import __version__
    stamp = f"{__version__}+{a.code_commit}"
    for d in (run, smoke):
        restamp(d / "envelopes.jsonl", stamp)

    for d in (run, smoke):
        v = sh(PY, "-m", "signalpost", "validate", "--envelopes", str(d / "envelopes.jsonl"))
        au = sh(PY, "eval/audit_artifact.py", "--run", str(d))
        print(d.name, "|", v.stdout.strip(), "|", au.stdout.strip().splitlines()[0] if au.stdout.strip() else au.stderr[-300:])
        if v.returncode or au.returncode:
            print(au.stdout[-2000:], v.stderr[-2000:], file=sys.stderr)
            return 1
    score = sh(PY, "eval/score.py", "--envelopes", str(run / "envelopes.jsonl"), "--gold", "eval/gold.jsonl", "--report", "eval/report.json")
    print("gold:", json.loads(score.stdout).get("gold") if score.stdout.strip().startswith("{") else score.stdout[-400:])

    shutil.copy(run / "run-report.json", sub / "run-report-1000.json")
    shutil.copy(run / "manifest.txt", sub / "manifest-1000.txt")
    for name, src in (("envelopes-1000.jsonl.gz", run / "envelopes.jsonl"), ("requests-1000.jsonl.gz", run / "requests.jsonl")):
        with open(src, "rb") as fi, gzip.open(sub / name, "wb", compresslevel=9) as fo:
            shutil.copyfileobj(fi, fo)
    sm = sub / "smoke-100"
    sm.mkdir(exist_ok=True)
    shutil.copy(smoke / "run-report.json", sm / "run-report.json")
    shutil.copy(smoke / "manifest.txt", sm / "manifest.txt")
    with open(smoke / "envelopes.jsonl", "rb") as fi, gzip.open(sm / "envelopes.jsonl.gz", "wb", compresslevel=9) as fo:
        shutil.copyfileobj(fi, fo)
    au = sh(PY, "eval/audit_artifact.py", "--run", str(smoke))
    (sm / "audit.txt").write_text(au.stdout, encoding="utf-8")
    site = sh(PY, "tools/build_site.py", "--envelopes", str(run / "envelopes.jsonl"), "--out", str(sub / "site"))
    print(site.stdout.strip().splitlines()[-1] if site.stdout.strip() else site.stderr[-300:])
    print("packaged:", sorted(p.name for p in sub.iterdir()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
