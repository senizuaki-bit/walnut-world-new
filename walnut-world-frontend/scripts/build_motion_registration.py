"""Measure delivery canvases for runtime UV registration; never rewrite source images."""
import json
from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
ART = ROOT / "assets/art/redesign/crop_adaptive/v2"
records = {}
for path in sorted((ART / "motion/metadata").glob("*.json")):
    clip = json.loads(path.read_text(encoding="utf-8-sig"))
    group, name = clip["group"], clip["id"]
    if group not in ("characters", "crops", "soil") and not name.startswith("prop-pump-"):
        continue
    source = Image.open(ART / clip["source"][0]).convert("RGBA")
    w, h = clip["width"], clip["height"]
    pad = 16 if group == "crops" else 26 if group == "soil" else 30
    scale = min((w - 2 * pad) / source.width, (h - 2 * pad) / source.height)
    rect = [(w - source.width * scale) / 2, h - pad - source.height * scale,
            source.width * scale, source.height * scale]
    if group == "characters" and name.rsplit("-", 1)[-1] in ("idle", "talk", "read", "write"):
        # Repainted mouth/gesture sheets use registered feet but wider canvases.
        # Idle and talking must share one registration, avoiding scale jumps.
        poster_name = name.replace("-talk", "-idle")
        poster = Image.open(ART / "motion/characters" / f"{poster_name}-poster.png").convert("RGBA")
        a = source.getchannel("A").getbbox()
        b = poster.getchannel("A").point(lambda x: 255 if x > 16 else 0).getbbox()
        scale = (b[3] - b[1]) / (a[3] - a[1])
        rw, rh = source.width * scale, source.height * scale
        rect = [(b[0] + b[2]) / 2 - rw / 2, b[3] - a[3] * scale, rw, rh]
    records[name] = {"source": clip["source"][0], "rect": [round(v, 4) for v in rect]}
output = ROOT / "resources/ui/v2/motion-registration.json"
output.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"Registered {len(records)} motion canvases against static source geometry")
