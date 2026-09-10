"""Build fixed dialogue WAVs once; credentials and network stay out of the game.

Run with --key-file PATH. Completed unchanged assets are reused on subsequent runs.
Requires httpx in the build environment, not on the player's machine.
"""

import argparse
import base64
import hashlib
import json
import time
import uuid
import wave
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets/audio/dialogue"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key-file", type=Path, required=True)
    args = parser.parse_args()
    key = args.key_file.read_text(encoding="utf-8-sig").strip()
    if not key or any(c in key for c in "\r\n"):
        raise SystemExit("Invalid credential file")
    manifest = json.loads((ASSETS / "lines.json").read_text(encoding="utf-8"))
    receipt_path = ASSETS / "generated.json"
    receipts = json.loads(receipt_path.read_text()) if receipt_path.exists() else {}
    entries = []
    with httpx.Client(timeout=60) as client:
        for line in manifest["lines"]:
            speaker = manifest["voices"][line["speaker"]]
            question = line.get("question", "")
            spoken = line["text"] + ("想一想：" + question if question else "")
            digest = hashlib.sha256((speaker + "\n" + spoken).encode()).hexdigest()
            output = ASSETS / (line["id"] + ".wav")
            if line.get("audio_source") == "provided":
                receipt = receipts.get(line["id"], {})
                if (
                    not output.exists()
                    or receipt.get("sha256") != digest
                    or receipt.get("audio_sha256")
                    != hashlib.sha256(output.read_bytes()).hexdigest()
                ):
                    raise SystemExit(
                        f"{line['id']}: supplied recording missing or changed; "
                        "verify its text and receipt instead of replacing it with synthesis"
                    )
            if (
                receipts.get(line["id"], {}).get("sha256") != digest
                or not output.exists()
            ):
                started = time.monotonic()
                pcm = bytearray()
                complete = False
                with client.stream(
                    "POST",
                    "https://openspeech.bytedance.com/api/v3/tts/unidirectional/sse",
                    headers={
                        "X-Api-Key": key,
                        "X-Api-Resource-Id": "seed-tts-2.0",
                        "X-Api-Request-Id": str(uuid.uuid4()),
                    },
                    json={
                        "user": {"uid": "walnut-fixed-dialogue"},
                        "req_params": {
                            "text": spoken,
                            "speaker": speaker,
                            "audio_params": {"format": "pcm", "sample_rate": 24000},
                        },
                    },
                ) as response:
                    if response.status_code != 200:
                        raise SystemExit(
                            f"{line['id']}: provider HTTP {response.status_code}"
                        )
                    for raw in response.iter_lines():
                        if not raw.startswith("data:"):
                            continue
                        event = json.loads(raw[5:])
                        if event.get("code") not in (0, 20000000):
                            raise SystemExit(
                                f"{line['id']}: provider code {event.get('code')}"
                            )
                        if event.get("data"):
                            pcm.extend(base64.b64decode(event["data"], validate=True))
                        if len(pcm) > 8 * 1024 * 1024:
                            raise SystemExit("Audio exceeded build limit")
                        if event["code"] == 20000000:
                            complete = True
                            break
                if not complete or not pcm or len(pcm) % 2:
                    raise SystemExit(
                        f"{line['id']}: incomplete audio; asset not published"
                    )
                temporary = output.with_suffix(".tmp")
                with wave.open(str(temporary), "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(24000)
                    wav.writeframes(pcm)
                temporary.replace(output)
                receipts[line["id"]] = {
                    "sha256": digest,
                    "speaker": speaker,
                    "duration_seconds": round(len(pcm) / 48000, 3),
                    "generation_seconds": round(time.monotonic() - started, 3),
                }
                receipt_path.write_text(json.dumps(receipts, indent=2) + "\n")
                print(
                    f"GENERATED {line['id']} {receipts[line['id']]['duration_seconds']}s",
                    flush=True,
                )
            lookup = line["speaker"] + "\n" + line["text"] + "\n" + question
            lookup_literal = json.dumps(lookup, ensure_ascii=False)
            asset_literal = json.dumps("res://assets/audio/dialogue/" + output.name)
            entries.append(f"\t{lookup_literal}: preload({asset_literal}),")
    # Explicit preloads keep the assets included in exported games.
    catalog = "# Generated by scripts/generate_dialogue_audio.py.\nextends RefCounted\n\nconst CLIPS := {\n"
    catalog += "\n".join(entries) + "\n}\n"
    (ASSETS / "fixed_dialogue_audio.gd").write_text(catalog, encoding="utf-8")
    print(f"CATALOG_READY {len(entries)} clips", flush=True)


if __name__ == "__main__":
    main()
