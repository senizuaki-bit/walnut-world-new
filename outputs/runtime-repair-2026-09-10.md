# Local all-branch runtime repair

The running checkout is `C:/Users/HP/Desktop/walnut-world-all`.

## Causes and fixes

- Text hints: DeepSeek accepted `deepseek-v4-flash` but returned `deepseek-flash`. The adapter now recognizes only this observed provider/model alias pair, preserving the persisted dispatch identity. Unknown model changes still fail validation. No saved model/profile identity was rewritten.
- Rejected builds: the compiler received the source and rejected `int moisture[6]` with eight initializers. Bug-agent repair prompts previously omitted the evidence-backed decision template before tool execution, allowing another invalid response and repeated job retries. Repair prompts now include the template and initial prompts state the role's message-length limit.
- UI: a later teaching-service failure could replace the already confirmed compiler error. The bridge now immediately presents the compilation result and retains it while waiting for teaching feedback.
- Voice: the new checkout did not include the ignored local voice key. `tools/start-all.ps1` now reuses the existing key file from the original checkout; no credential is embedded in source or this report.

## Verification

- Model-alias regression first failed with `Provider reply authority drifted`, then passed; unknown model/provider pairs remain rejected.
- Python targeted suites: 20 tests plus 27 subtests; 29 tests plus 30 subtests passed.
- Godot bridge regression failed before the UI change and passed after it.
- Real Godot application/controller: hint and rejected-build feedback both returned non-degraded provider interactions; the player's source was unchanged. The headless harness emitted resource-cleanup warnings on exit.
- Real voice gateway: received `voice.ready`.
- Synthetic spoken fixture through the real voice gateway: ASR transcript, text reply, and 413,346 bytes of returned PCM audio received. No microphone recording was saved.
- Local microphone capture: 32,256 frames received with WASAPI, then capture stopped.
- Ruff and `git diff --check` passed.
- Final runtime status: database, relay, gateway, workflow worker, learner worker, and visible game all running.

Changes are local and have not been pushed. The player's source and existing database were retained.

## Bug legion frontend acceptance

The rendered Godot application submitted the player's existing rejected source through the normal Run button and real backend. The backend produced validated, non-degraded `bug_agent` feedback (`interaction_f64aaa599d64538af23a45ab`). The frontend displayed the Bug legion and Bug dialogue, then hid the legion after dialogue dismissal. Source and world snapshot equality were checked before/after. Result: `BUG_LEGION_REAL_UI_PASS`.

Screenshot: [bug-legion-real-ui.png](bug-legion-real-ui.png). Harness: `tools/bug-legion-live.gd`; it uses the current failure history and does not inject a role or mock provider.

## Successful run followed by summary rejection

The player's corrected program produced successful `run_2aec0a16cbacc40a9941942e` and committed world revision 11 to 12. Its later book-agent summary was rejected with `PERMANENT_LEARNER_JUDGMENT`: the validator treated both “不外推为永久掌握” and the repaired “不能推断已永久掌握” as permanent claims, although both explicitly reject that conclusion. After retries, the enclosing workflow failed and the frontend labeled the entire submission “服务执行失败”.

The validator now recognizes those directly governing disclaimers. Regression cases cover both message and inference reason, double negatives, and a separate permanent claim after a disclaimer. The new cases reproduced four failures before the fix; the current-completion, provider-failure, and context-tools suites then passed (34 tests and 66 subtests).

The crop bridge also retains SessionController's verified success/commit notification for the current submission. A subsequent feedback failure displays “运行已成功，反馈暂未完成”, without claiming complete workflow closure, advancing the client snapshot, or reusing the success flag for the next submission. The frontend regression failed before this change and passed afterward; the presentation-queue test also passed. The bridge harness still emits resource-cleanup warnings at exit.

No replay of the player's already committed run or database-history rewrite was performed for this repair. The failed historical workflow remains a failed record; the repair applies to subsequent processing and newly loaded frontend code.

## Follow-up full success test requested by the player

The rendered Godot test instance used the current unchanged source and emitted the normal Run button signal against the real local services. Build and activation succeeded. The new run `run_0e8732eb4ab6cc9587cd9aa6` committed revision 12 to 13, and book-agent returned validated, non-degraded provider growth-summary interaction `interaction_0df62cc481511c37d64c94c2`. The enclosing command `cmd_6650cc761d8e4c4b95d5a8dd1850e528` finished SUCCEEDED on attempt 1 with no job error.

The frontend displayed the completion card titled “本次验证成功”. The harness asserted the successful Run, received growth summary, advanced authoritative snapshot, unchanged source, and visible completion card, then exited with code 0 and `SUCCESS_LIVE_PASS`. Screenshot: `outputs/success-real-ui.png`. Reproduce with the ignored local diagnostic helper: `python tools/debug-runtime.py success-ui` (performs a new real run).

## Connect book-agent analysis to the visible completion summary

The earlier success test established workflow completion, but its displayed growth summary was a fixed post-validation template: `_canonical_book_copy` discarded the model's prose. The book prompt also omitted the source from the successful run. This explains why a provider-backed interaction could still contain generic text.

The book context now reads the exact certified Skill referenced by the successful Run, verifies binding and request provenance, and includes that source in the prompt as untrusted data for analysis. The model is asked to connect one concrete code choice to the verified result and offer one specific experiment, without inventing prior edits or learning history. Validated book prose is preserved through publication; role, evidence, length, and permanent-judgment checks remain active.

The completion card now displays the real book summary in a scrollable text area. Starting a new submission clears the previous summary. New regression cases reproduced missing source/template replacement and invisible summary before these changes. Python checks passed (65 tests and 79 subtests), along with the crop bridge and presentation-queue tests.

Real acceptance passed after restart: `run_6be8cef00de4e3fd36a0c807`, revision 13 to 14; command `cmd_0ef93adbc4ef4eb99595159cb81bf97e` SUCCEEDED on attempt 1; book interaction `interaction_e873c33ff2d6bae170bf8ebb`. The rendered test asserted the actual book message was visible on the completion card and the player's source was unchanged. A separate read-only comparison (`tools/verify-book-publication.py`) verified that the provider received source code and its exact message equaled the published feedback (`BOOK_PUBLICATION_PASS`). The updated screenshot is `outputs/success-real-ui.png`.

The observed model summary explains the `for` loop over indices 0 through 7, the `target[i] - moisture[i]` gap and water-portion decision, links these to five watering intents and score 8, then suggests changing one moisture value to 90 to test whether a negative gap emits WATER. This prose came from the model rather than a fixed backend template.
