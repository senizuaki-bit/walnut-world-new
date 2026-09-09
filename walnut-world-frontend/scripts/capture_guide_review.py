"""Capture all guide pages through the real Godot renderer, then write a comparison index."""
import argparse
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGES = dict(S01="start", S02="intro", E01="old_tool", E02="manual_choice",
             E03="skill_tree", S03="workshop_dialogue", S04="workshop",
             S05="workshop_branch", S06="workshop_summary", S07="code",
             S08="hint", S09="validating", E08="failed", E04="bug", E05="patch",
             E09="complete", S10="feedback", E06="growth", E10="unlocked",
             E07="free", E11="preview")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--godot", required=True)
    parser.add_argument("--pages", nargs="*", default=list(PAGES))
    args = parser.parse_args()
    output = ROOT / "docs/design/verification/guide-alignment"
    output.mkdir(parents=True, exist_ok=True)
    previous = output / "captures.json"
    results_by_page = {r["page"]: r for r in json.loads(previous.read_text(encoding="utf-8"))} if previous.exists() else {}
    results = []
    for page in args.pages:
        target = output / f"{page}.png"
        run = subprocess.run([args.godot, "--path", str(ROOT), "--rendering-method", "gl_compatibility",
                              "--script", "res://tests/level_demo/capture_crop_adaptive_demo.gd", "--",
                              f"--state={PAGES[page]}", f"--output={target.as_posix()}"],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
        passed = run.returncode == 0 and "CROP_ADAPTIVE_CAPTURE_PASS" in run.stdout and "ERROR:" not in run.stdout + run.stderr
        results.append(dict(page=page, state=PAGES[page], passed=passed, output=run.stdout + run.stderr))
        results_by_page[page] = results[-1]
        print(f"{page}: {'PASS' if passed else 'FAIL'}", flush=True)
    previous.write_text(json.dumps([results_by_page[p] for p in PAGES if p in results_by_page], ensure_ascii=False, indent=2), encoding="utf-8")
    raise SystemExit(any(not r["passed"] for r in results))

if __name__ == "__main__":
    main()
