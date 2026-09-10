# Fixed dialogue audio

The three Yaya opening lines now match the user's text exactly. Eleven complete mono 24 kHz WAV files are packaged locally: four Yaya lines, six existing Dingdang fixed teaching lines, and one fixed Shushu archive line. Walnut has no new dialogue; Bug is deferred as requested. System text is excluded. Dynamic Agent replies do not match the fixed-audio catalog; the existing generated Book summary/audio flow remains separate.

The user subsequently supplied `keainvsheng-01-urgent.wav`, `keainvsheng-02-urgent.wav`, and `keainvsheng-03-thinking.wav` from the persistent-play directory. These replaced the three synthesized opening recordings in order, byte-for-byte, without conversion. Their durations are 3.036, 4.884 and 8.475 seconds. The catalog marks them as provided recordings; generation checks their hashes and refuses to silently replace them if text or files change. The voice IDs below describe the synthesis configuration for generated assets, not independently verified provenance for the user-supplied recordings.

Voices use the official speech synthesis 2.0 IDs:

- Yaya: `ICL_uranus_zh_female_keainvsheng_tob` (可爱女生 2.0). The supplied `saturn_zh_female_keainvsheng_tob` is listed for realtime speech; this is the corresponding synthesis voice.
- Dingdang: `ICL_uranus_zh_male_huzishushu_tob` (胡子叔叔 2.0).
- Shushu: `ICL_uranus_zh_male_bujiqingnian_tob` (不羁青年 2.0).

Source: https://www.volcengine.com/docs/6561/1257544, official voice list retrieved through its public documentation API. All selected voices returned complete audio in actual synthesis requests.

`assets/audio/dialogue/lines.json` is the editable catalog. Run `scripts/generate_dialogue_audio.py --key-file PATH` from a Python environment with httpx to rebuild. Credentials stay outside the repository. The script reuses unchanged recordings, publishes WAVs only after receiving the provider's completion marker, and generates explicit Godot preloads so exported games include the audio. Playback does not call the network. Generation receipts record each voice, text fingerprint, playback duration and generation time; repeat generation was checked to reuse all eleven recordings.

The shared story overlay looks up exact speaker/body/question matches. Per the user's revised interaction request, it displays the whole sentence immediately and starts its audio at the same time. Playback completion leaves the current sentence visible until the user clicks or presses Enter. Input advances exactly one sentence, stopping current audio if necessary; input on the last sentence closes the sequence. A consumed-event marker prevents the scroll area's signal and its parent from advancing twice for the same click. Fixed questions are shown alongside their body and included in the recording. Hiding or skipping stops speech, and replay starts from the beginning. Unrecorded and system text also appear immediately and wait for manual advancement.

Validation: Godot regression checks full text immediately, waiting after the first and final recordings end, a mouse click advancing one sentence, a mouse click interrupting the second recording, Enter completing the final sentence, lesson continuation, skip/hide/replay, system silence, changed-text fallback and the workshop's Uncle Beard recording. Existing presentation queue, crop lesson and art presentation tests passed with the revised interaction. Screenshot: `fixed-dialogue-yaya.png`. The rendered test verifies input dispatch and playback state rather than physical speaker listening.
