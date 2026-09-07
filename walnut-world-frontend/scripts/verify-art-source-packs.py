"""Compare installed art against the two delivery folders, never cached checksums."""
import argparse
import hashlib
import json
import re
from pathlib import Path


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--motion-pack", type=Path, required=True)
    parser.add_argument("--static-pack", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    installed = root / "assets/art/redesign/crop_adaptive"
    v2 = installed / "v2"
    failures, records, clips, pages = [], [], [], []

    def compare(target, source):
        ok = target.is_file() and source.is_file() and digest(target) == digest(source)
        records.append({"path": target.relative_to(root).as_posix(),
                        "source": str(source), "sha256": digest(source) if source.is_file() else None,
                        "equal": ok})
        if not ok:
            failures.append(f"Missing or changed source: {target}")

    manifest = json.loads((args.static_pack / "asset-manifest.json").read_text(encoding="utf-8-sig"))
    assets = {item["id"]: item for item in manifest["assets"] if not item.get("deprecated")}
    for item in assets.values():
        compare(v2 / item["file"], args.static_pack / item["file"])
    for name in ["asset-manifest.json", "guide-layout.json", "skill-tree-data.json"]:
        compare(v2 / name, args.static_pack / name)
    compare(installed / "v1/animation-manifest.json", args.motion_pack / "animation-manifest.json")
    for source in sorted((args.motion_pack / "metadata").glob("*.json")):
        if source.name.startswith("."):
            continue
        data = json.loads(source.read_text(encoding="utf-8-sig"))
        compare(v2 / "motion/metadata" / source.name, source)
        for kind in ["poster", "atlas"]:
            if kind in data["files"]:
                rel = data["files"][kind]
                compare(v2 / "motion" / rel, args.motion_pack / rel)
        if data["group"] == "environment":
            rel = f"environment/{data['id']}.ogv"
            compare(v2 / "motion" / rel, args.motion_pack / rel)
        else:
            frames = data["atlas"]["frames"]
            if sum(frame["durationMs"] for frame in frames) != data["durationMs"]:
                failures.append(f"Frame duration mismatch: {data['id']}")
            for frame in frames:
                x, y, w, h = frame["rect"]
                if min(x, y) < 0 or min(w, h) <= 0 or x + w > 4096 or y + h > 4096:
                    failures.append(f"Invalid atlas bounds: {data['id']}")
        clips.append({"id": data["id"], "trigger": data.get("trigger"),
                      "durationMs": data["durationMs"], "loop": data["loop"]})
    layout = json.loads((args.static_pack / "guide-layout.json").read_text(encoding="utf-8-sig"))
    for page in layout["pages"]:
        missing = [item["id"] for item in page["assets"] if item["id"] not in assets]
        failures.extend(f"Deprecated/missing asset: {page['view']}/{value}" for value in missing)
        pages.append({"view": page["view"], "state": page["state"],
                      "assetCount": len(page["assets"]), "referencesValid": not missing})
    # Follow actual resource dependencies, including script preloads and BBCode images.
    pending = ["scenes/ui/game_start_screen.tscn", "scenes/level_demo/crop_adaptive_watering_demo.tscn"]
    visited, referenced_art = set(), set()
    while pending:
        rel = pending.pop()
        if rel in visited:
            continue
        visited.add(rel)
        path = root / rel
        if not path.is_file() or path.suffix not in [".gd", ".tscn", ".tres"]:
            continue
        for dep in re.findall(r'res://([^"\s\]\)]+)', path.read_text(encoding="utf-8")):
            if Path(dep).suffix in [".gd", ".tscn", ".tres"]:
                pending.append(dep)
            elif Path(dep).suffix in [".png", ".webp", ".ogv", ".svg", ".jpg"]:
                referenced_art.add(dep)
    source_hashes = {digest(p) for pack in [args.motion_pack, args.static_pack]
                     for p in pack.rglob("*") if p.is_file() and not p.name.startswith(".")}
    installed_media = [p for p in installed.rglob("*") if p.suffix in [".png", ".webp", ".ogv", ".svg", ".jpg"]]
    for path in installed_media:
        if digest(path) not in source_hashes:
            failures.append(f"Installed art outside the two packs: {path.relative_to(root)}")
    referenced_art = {rel for rel in referenced_art if "%" not in rel}
    for rel in sorted(referenced_art):
        if not (root / rel).is_file() or digest(root / rel) not in source_hashes:
            failures.append(f"Runtime art outside the two packs: {rel}")
    if len(assets) != 100 or len(clips) != 98 or len(pages) != 74:
        failures.append("Unexpected delivery inventory; review the updated package contract.")
    report = {"scope": "Crop-adaptive student UI; native text/fonts/code are not art deliveries",
              "staticAssets": len(assets), "animationClips": len(clips), "referenceStates": len(pages),
              "installedMedia": len(installed_media),
              "referencedRuntimeArt": sorted(referenced_art), "comparisons": records,
              "clips": clips, "pages": pages, "failures": failures,
              "note": "Reference/file validation is separate from runtime behavior and visual acceptance."}
    output = root / "docs/design/verification/two-pack-source-audit.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"comparisons": len(records), "staticAssets": len(assets),
                      "animationClips": len(clips), "referenceStates": len(pages),
                      "runtimeArt": len(referenced_art), "failures": failures}, ensure_ascii=False))
    return bool(failures)


if __name__ == "__main__":
    raise SystemExit(main())
