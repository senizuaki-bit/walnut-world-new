import { createHash } from "node:crypto";
import { basename } from "node:path";
import { canonicalJsonSha256V1, canonicalJsonV1 } from "../src/canonical-json.mjs";
import {
  assertAgentTurnFeedbackReadyEvent,
  assertEvidenceIntegrity,
  assertWorldCommitEvidence,
} from "../src/semantic-invariants.mjs";

const MAX_SAFE_INTEGER = Number.MAX_SAFE_INTEGER;
const PRODUCT_SCHEMA_NAMES = new Set([
  "content-unit.schema.json",
  "session-workspace.schema.json",
  "skill-draft.schema.json",
  "skill-draft-upsert-request.schema.json",
  "skill-patch.schema.json",
  "patch-decision-request.schema.json",
  "patch-decision-receipt.schema.json",
  "agent-interaction-source-receipt.schema.json",
  "agent-interaction.schema.json",
  "agent-interaction-page.schema.json",
  "product-write-reconciliation.schema.json",
]);

export class ProductExperienceInvariantError extends Error {
  constructor(message) {
    super(message);
    this.name = "ProductExperienceInvariantError";
  }
}

function invariant(condition, message) {
  if (!condition) throw new ProductExperienceInvariantError(message);
}

function sameJson(left, right) {
  return canonicalJsonV1(left) === canonicalJsonV1(right);
}

function assertSame(left, right, message) {
  invariant(sameJson(left, right), message);
}

function assertSafeInteger(value, label, minimum = 0) {
  invariant(
    Number.isSafeInteger(value) && value >= minimum && value <= MAX_SAFE_INTEGER,
    `${label} must be a safe integer from ${minimum} through ${MAX_SAFE_INTEGER}`,
  );
}

const CONTRACT_ORIGIN = "https://contracts.yaya.local";

function canonicalRelativeUrl(value, label) {
  invariant(
    typeof value === "string" && value.startsWith("/") && !value.startsWith("//"),
    `${label} must be a root-relative URL`,
  );
  const url = new URL(value, CONTRACT_ORIGIN);
  invariant(
    url.origin === CONTRACT_ORIGIN
      && url.username === ""
      && url.password === ""
      && url.hash === "",
    `${label} must be a credential-free canonical relative URL`,
  );
  return url;
}

function assertExactPath(url, expectedPath, label, { search = "" } = {}) {
  invariant(url.pathname === expectedPath && url.search === search,
    `${label} does not identify its exact canonical resource`);
}

function sha256Utf8(value) {
  return createHash("sha256").update(value, "utf8").digest("hex");
}

function canonicalPathKey(path, label) {
  invariant(typeof path === "string" && path.length >= 1 && path.length <= 240,
    `${label} must be a non-empty logical path`);
  invariant(!path.startsWith("/") && !path.includes("\\"),
    `${label} must be a relative slash-separated logical path`);
  const segments = path.split("/");
  invariant(segments.every((segment) => (
    segment.length > 0
    && segment !== "."
    && segment !== ".."
    && /^[A-Za-z0-9_][A-Za-z0-9_.-]*$/u.test(segment)
    && !segment.endsWith(".")
  )), `${label} contains an empty, dot, trailing-dot or unsupported segment`);
  return path.toLowerCase();
}

function assertSourceBundle(sourceBundle, label) {
  invariant(sourceBundle && typeof sourceBundle === "object", `${label} is required`);
  invariant(Array.isArray(sourceBundle.files), `${label}.files must be an array`);
  invariant(sourceBundle.files.length >= 1 && sourceBundle.files.length <= 32,
    `${label}.files must contain 1 through 32 files`);
  const pathKeys = new Set();
  let sourceBytes = 0;
  for (const [index, file] of sourceBundle.files.entries()) {
    const fileLabel = `${label}.files[${index}]`;
    const pathKey = canonicalPathKey(file.path, `${fileLabel}.path`);
    invariant(!pathKeys.has(pathKey), `${label} contains a host-portable path collision at ${file.path}`);
    pathKeys.add(pathKey);
    invariant(file.content_sha256 === sha256Utf8(file.content),
      `${fileLabel}.content_sha256 does not match its UTF-8 content`);
    sourceBytes += Buffer.byteLength(file.content, "utf8");
  }
  invariant(sourceBytes <= 1_048_576, `${label} exceeds the one-mebibyte source limit`);
  canonicalPathKey(sourceBundle.entrypoint, `${label}.entrypoint`);
  invariant(
    sourceBundle.files.filter((file) => file.path === sourceBundle.entrypoint).length === 1,
    `${label}.entrypoint must identify exactly one file by exact case-sensitive path`,
  );
}

export function productDraftHashProjection(draft) {
  return {
    session_id: draft.session_id,
    draft_id: draft.draft_id,
    skill_id: draft.skill_id,
    content_ref: draft.content_ref,
    display_name: draft.display_name,
    source_bundle: draft.source_bundle,
  };
}

export function computeProductDraftSha256(draft) {
  return canonicalJsonSha256V1(productDraftHashProjection(draft));
}

export function computeProductPatchSha256(patch) {
  const projection = structuredClone(patch);
  delete projection.patch_sha256;
  return canonicalJsonSha256V1(projection);
}

export function computeProductFeedbackSha256(feedback) {
  return canonicalJsonSha256V1(feedback);
}

export function productInteractionSourceHashProjection(receipt) {
  const projection = structuredClone(receipt);
  delete projection.source_sha256;
  return projection;
}

export function computeProductInteractionSourceSha256(receipt) {
  return canonicalJsonSha256V1(productInteractionSourceHashProjection(receipt));
}

export function assertProductContentUnit(contentUnit) {
  const levels = contentUnit.task.hint_policy.levels.map((level) => level.level);
  assertSame(levels, [0, 1, 2, 3, 4], "ContentUnit hint levels must be exactly 0 through 4");
  if (contentUnit.task.starter_skill !== null) {
    assertSourceBundle(contentUnit.task.starter_skill.source_bundle, "ContentUnit starter source");
  }
  const selfUrl = canonicalRelativeUrl(contentUnit.links.self, "ContentUnit links.self");
  invariant(
    selfUrl.pathname === `/product-experience/v1/content-units/${contentUnit.content_ref.unit_id}/versions/${contentUnit.content_ref.version}`
      && selfUrl.searchParams.get("content_hash") === contentUnit.content_ref.content_hash
      && [...selfUrl.searchParams.keys()].length === 1,
    "ContentUnit links.self does not identify its exact version-pinned projection",
  );
}

export function assertProductSessionWorkspace(workspace) {
  invariant(workspace.session.session_id.length > 0, "Workspace session identity is required");
  assertSame(workspace.request_context.actor, workspace.session.request_context.actor,
    "Workspace and AgentSession actors must be byte-equivalent");
  assertSame(workspace.request_context.content_ref, workspace.content_ref,
    "Workspace request context and content_ref must agree");
  assertSame(workspace.session.content, workspace.content_ref,
    "Workspace and AgentSession content refs must agree");
  invariant(workspace.world_checkpoint.world_id === workspace.session.world_id,
    "Workspace world checkpoint must belong to the AgentSession world");
  invariant(Date.parse(workspace.request_context.requested_at) <= Date.parse(workspace.created_at),
    "Workspace origin request cannot postdate creation");
  invariant(Date.parse(workspace.updated_at) >= Date.parse(workspace.created_at),
    "Workspace updated_at cannot precede creation");
  if (workspace.current_task.started_at !== null && workspace.current_task.completed_at !== null) {
    invariant(Date.parse(workspace.current_task.completed_at) >= Date.parse(workspace.current_task.started_at),
      "Workspace task completion cannot precede its start");
  }
  const draftIds = new Set();
  const skillIds = new Set();
  for (const reference of workspace.skill_draft_refs) {
    invariant(!draftIds.has(reference.draft_id), "Workspace draft_id values must be unique");
    invariant(!skillIds.has(reference.skill_id), "Workspace skill_id values must be unique");
    draftIds.add(reference.draft_id);
    skillIds.add(reference.skill_id);
  }
  const interactionsUrl = new URL(workspace.links.agent_interactions, "https://contracts.yaya.local");
  const selfUrl = new URL(workspace.links.self, "https://contracts.yaya.local");
  const contentUrl = new URL(workspace.links.content_unit, "https://contracts.yaya.local");
  const worldUrl = new URL(workspace.links.world_snapshot, "https://contracts.yaya.local");
  for (const [name, url] of Object.entries({ selfUrl, contentUrl, interactionsUrl, worldUrl })) {
    invariant(url.origin === "https://contracts.yaya.local" && url.username === "" && url.password === "" && url.hash === "",
      `Workspace ${name} must be a credential-free canonical relative URL`);
  }
  invariant(
    selfUrl.pathname === `/product-experience/v1/sessions/${workspace.session.session_id}/workspace`
    && selfUrl.search === "",
    "Workspace self link does not identify its canonical Product GET",
  );
  invariant(
    contentUrl.pathname === `/product-experience/v1/content-units/${workspace.content_ref.unit_id}/versions/${workspace.content_ref.version}`
    && contentUrl.searchParams.get("content_hash") === workspace.content_ref.content_hash
    && [...contentUrl.searchParams.keys()].length === 1,
    "Workspace content_unit link does not identify its exact pinned ContentUnit",
  );
  invariant(
    interactionsUrl.pathname === `/product-experience/v1/sessions/${workspace.session.session_id}/agent-interactions`
    && interactionsUrl.searchParams.get("after_sequence") === "0"
    && [...interactionsUrl.searchParams.keys()].length === 1,
    "Workspace agent_interactions link must identify its canonical baseline page after_sequence=0",
  );
  invariant(
    worldUrl.pathname === `/v1/worlds/${workspace.world_checkpoint.world_id}/snapshot`
    && worldUrl.search === "",
    "Workspace world_snapshot link does not identify its checkpoint world",
  );
}

export function assertProductSkillDraft(draft) {
  assertSafeInteger(draft.revision, "SkillDraft.revision", 1);
  assertSame(draft.request_context.content_ref, draft.content_ref,
    "SkillDraft request context and content_ref must agree");
  assertSourceBundle(draft.source_bundle, "SkillDraft.source_bundle");
  invariant(draft.draft_sha256 === computeProductDraftSha256(draft),
    "SkillDraft.draft_sha256 does not match its canonical draft projection");
  invariant(Date.parse(draft.request_context.requested_at) <= Date.parse(draft.created_at),
    "SkillDraft origin request cannot postdate creation");
  invariant(Date.parse(draft.updated_at) >= Date.parse(draft.created_at),
    "SkillDraft updated_at cannot precede creation");
  const selfUrl = canonicalRelativeUrl(draft.links.self, "SkillDraft links.self");
  const workspaceUrl = canonicalRelativeUrl(
    draft.links.session_workspace,
    "SkillDraft links.session_workspace",
  );
  const buildsUrl = canonicalRelativeUrl(draft.links.builds, "SkillDraft links.builds");
  assertExactPath(
    selfUrl,
    `/product-experience/v1/sessions/${draft.session_id}/skill-drafts/${draft.draft_id}`,
    "SkillDraft links.self",
  );
  assertExactPath(
    workspaceUrl,
    `/product-experience/v1/sessions/${draft.session_id}/workspace`,
    "SkillDraft links.session_workspace",
  );
  assertExactPath(buildsUrl, "/v1/skill-builds", "SkillDraft links.builds");
}

export function assertProductSkillDraftUpsert(request) {
  assertSafeInteger(request.base_revision, "SkillDraft upsert base_revision");
  invariant(
    (request.base_revision === 0) === (request.base_draft_sha256 === null),
    "SkillDraft create/update CAS revision and hash shape is inconsistent",
  );
  assertSourceBundle(request.source_bundle, "SkillDraft upsert source_bundle");
}

export function assertProductSkillPatch(patch) {
  assertSafeInteger(patch.base_draft_revision, "SkillPatch.base_draft_revision", 1);
  const targetPaths = new Set();
  let entrypointOperations = 0;
  let displayNameOperations = 0;
  for (const [index, operation] of patch.operations.entries()) {
    if (["UPSERT_FILE", "DELETE_FILE"].includes(operation.operation)) {
      const pathKey = canonicalPathKey(operation.path, `SkillPatch.operations[${index}].path`);
      invariant(!targetPaths.has(pathKey),
        `SkillPatch has more than one file mutation for canonical path ${operation.path}`);
      targetPaths.add(pathKey);
    }
    if (operation.operation === "UPSERT_FILE") {
      invariant(operation.content_sha256 === sha256Utf8(operation.content),
        `SkillPatch.operations[${index}].content_sha256 does not match its UTF-8 content`);
    } else if (operation.operation === "SET_ENTRYPOINT") {
      canonicalPathKey(operation.path, `SkillPatch.operations[${index}].path`);
      entrypointOperations += 1;
    } else if (operation.operation === "SET_DISPLAY_NAME") {
      displayNameOperations += 1;
    }
  }
  invariant(entrypointOperations <= 1, "SkillPatch may contain at most one SET_ENTRYPOINT");
  invariant(displayNameOperations <= 1, "SkillPatch may contain at most one SET_DISPLAY_NAME");
  invariant(patch.patch_sha256 === computeProductPatchSha256(patch),
    "SkillPatch.patch_sha256 does not match its canonical patch projection");
}

export function applyProductSkillPatch(baseDraft, patch) {
  assertProductSkillDraft(baseDraft);
  assertProductSkillPatch(patch);
  for (const field of ["session_id", "draft_id", "skill_id"]) {
    invariant(patch[field] === baseDraft[field], `SkillPatch.${field} does not match its base draft`);
  }
  invariant(patch.base_draft_revision === baseDraft.revision,
    "SkillPatch.base_draft_revision does not match its base draft");
  invariant(patch.base_draft_sha256 === baseDraft.draft_sha256,
    "SkillPatch.base_draft_sha256 does not match its base draft");

  const sourceBundle = structuredClone(baseDraft.source_bundle);
  let displayName = baseDraft.display_name;
  for (const operation of patch.operations) {
    if (operation.operation === "UPSERT_FILE") {
      const key = canonicalPathKey(operation.path, "SkillPatch UPSERT_FILE path");
      const index = sourceBundle.files.findIndex((file) => (
        canonicalPathKey(file.path, "base draft file path") === key
      ));
      if (operation.previous_content_sha256 === null) {
        invariant(index === -1, `SkillPatch UPSERT_FILE expected ${operation.path} not to exist`);
        sourceBundle.files.push({
          path: operation.path,
          content: operation.content,
          content_sha256: operation.content_sha256,
        });
      } else {
        invariant(index >= 0, `SkillPatch UPSERT_FILE target ${operation.path} does not exist`);
        invariant(sourceBundle.files[index].path === operation.path,
          `SkillPatch UPSERT_FILE path case does not exactly match ${sourceBundle.files[index].path}`);
        invariant(sourceBundle.files[index].content_sha256 === operation.previous_content_sha256,
          `SkillPatch UPSERT_FILE precondition failed for ${operation.path}`);
        sourceBundle.files[index] = {
          path: operation.path,
          content: operation.content,
          content_sha256: operation.content_sha256,
        };
      }
    } else if (operation.operation === "DELETE_FILE") {
      const key = canonicalPathKey(operation.path, "SkillPatch DELETE_FILE path");
      const index = sourceBundle.files.findIndex((file) => (
        canonicalPathKey(file.path, "base draft file path") === key
      ));
      invariant(index >= 0, `SkillPatch DELETE_FILE target ${operation.path} does not exist`);
      invariant(sourceBundle.files[index].path === operation.path,
        `SkillPatch DELETE_FILE path case does not exactly match ${sourceBundle.files[index].path}`);
      invariant(sourceBundle.files[index].content_sha256 === operation.previous_content_sha256,
        `SkillPatch DELETE_FILE precondition failed for ${operation.path}`);
      sourceBundle.files.splice(index, 1);
    } else if (operation.operation === "SET_ENTRYPOINT") {
      sourceBundle.entrypoint = operation.path;
    } else if (operation.operation === "SET_DISPLAY_NAME") {
      displayName = operation.display_name;
    }
  }
  assertSourceBundle(sourceBundle, "SkillPatch result source_bundle");
  const resultProjection = {
    session_id: baseDraft.session_id,
    draft_id: baseDraft.draft_id,
    skill_id: baseDraft.skill_id,
    content_ref: baseDraft.content_ref,
    display_name: displayName,
    source_bundle: sourceBundle,
  };
  const resultDraftSha256 = canonicalJsonSha256V1(resultProjection);
  invariant(resultDraftSha256 === patch.result_draft_sha256,
    "SkillPatch.result_draft_sha256 does not match application to its exact base draft");
  return { display_name: displayName, source_bundle: sourceBundle, draft_sha256: resultDraftSha256 };
}

export function assertProductPatchDecisionRequest(request, { interaction, patch } = {}) {
  assertSafeInteger(request.expected_interaction_revision,
    "PatchDecision.expected_interaction_revision", 1);
  assertSafeInteger(request.base_draft_revision, "PatchDecision.base_draft_revision", 1);
  invariant((request.decision === "ACCEPT") === (request.reason_code === null),
    "PatchDecision ACCEPT/REJECT reason_code shape is inconsistent");
  if (patch) {
    for (const field of [
      "session_id", "turn_id", "interaction_id", "patch_id", "patch_sha256", "draft_id",
      "skill_id", "base_draft_revision", "base_draft_sha256", "result_draft_sha256",
    ]) {
      invariant(request[field] === patch[field], `PatchDecision.${field} does not match SkillPatch`);
    }
  }
  if (interaction) {
    invariant(request.session_id === interaction.session_id, "PatchDecision session does not match interaction");
    invariant(request.turn_id === interaction.turn_id, "PatchDecision turn does not match interaction");
    invariant(request.interaction_id === interaction.interaction_id,
      "PatchDecision interaction identity does not match");
    invariant(request.expected_interaction_revision === interaction.interaction_revision,
      "PatchDecision expected interaction revision is stale");
    invariant(interaction.skill_patch !== null,
      "PatchDecision cannot target an interaction without a displayed SkillPatch");
    if (patch) {
      assertSame(interaction.skill_patch, patch,
        "PatchDecision SkillPatch is not byte-equivalent to the patch displayed in AgentInteraction");
    }
  }
}

export function assertProductPatchDecisionReceipt(receipt, patch, request) {
  for (const field of [
    "interaction_revision_before", "interaction_revision_after", "draft_revision_before",
    "draft_revision_after",
  ]) assertSafeInteger(receipt[field], `PatchDecisionReceipt.${field}`, 1);
  invariant(receipt.interaction_revision_after === receipt.interaction_revision_before + 1,
    "PatchDecisionReceipt must advance interaction_revision exactly once");
  if (receipt.decision === "ACCEPT") {
    invariant(receipt.reason_code === null && receipt.draft_updated === true,
      "ACCEPT receipt must update the draft and have no rejection reason");
    invariant(receipt.draft_revision_after === receipt.draft_revision_before + 1,
      "ACCEPT receipt must advance draft revision exactly once");
    if (patch) invariant(receipt.draft_sha256_after === patch.result_draft_sha256,
      "ACCEPT receipt draft hash does not match SkillPatch result");
  } else {
    invariant(typeof receipt.reason_code === "string" && receipt.draft_updated === false,
      "REJECT receipt must retain a machine reason and not update the draft");
    invariant(receipt.draft_revision_after === receipt.draft_revision_before,
      "REJECT receipt must not advance draft revision");
    invariant(receipt.draft_sha256_after === receipt.draft_sha256_before,
      "REJECT receipt must not change draft hash");
  }
  if (patch) {
    for (const field of ["session_id", "turn_id", "interaction_id", "patch_id", "patch_sha256", "draft_id", "skill_id"]) {
      invariant(receipt[field] === patch[field], `PatchDecisionReceipt.${field} does not match SkillPatch`);
    }
    invariant(receipt.draft_revision_before === patch.base_draft_revision,
      "PatchDecisionReceipt base revision does not match SkillPatch");
    invariant(receipt.draft_sha256_before === patch.base_draft_sha256,
      "PatchDecisionReceipt base hash does not match SkillPatch");
  }
  if (request) {
    for (const field of [
      "decision_id", "session_id", "turn_id", "interaction_id", "patch_id", "patch_sha256",
      "draft_id", "skill_id", "decision", "reason_code", "decided_at",
    ]) {
      invariant(receipt[field] === request[field],
        `PatchDecisionReceipt.${field} does not match its command request`);
    }
    invariant(receipt.interaction_revision_before === request.expected_interaction_revision,
      "PatchDecisionReceipt interaction base revision does not match its command request");
    invariant(receipt.draft_revision_before === request.base_draft_revision,
      "PatchDecisionReceipt draft base revision does not match its command request");
    invariant(receipt.draft_sha256_before === request.base_draft_sha256,
      "PatchDecisionReceipt draft base hash does not match its command request");
  }
  const interactionUrl = canonicalRelativeUrl(
    receipt.links.interaction,
    "PatchDecisionReceipt links.interaction",
  );
  const draftUrl = canonicalRelativeUrl(receipt.links.skill_draft, "PatchDecisionReceipt links.skill_draft");
  assertExactPath(
    interactionUrl,
    `/product-experience/v1/sessions/${receipt.session_id}/agent-interactions/${receipt.interaction_id}`,
    "PatchDecisionReceipt links.interaction",
  );
  assertExactPath(
    draftUrl,
    `/product-experience/v1/sessions/${receipt.session_id}/skill-drafts/${receipt.draft_id}`,
    "PatchDecisionReceipt links.skill_draft",
  );
}

export function assertProductAgentInteractionSourceReceipt(receipt) {
  assertSafeInteger(receipt.source_revision, "AgentInteractionSource.source_revision", 1);
  assertSafeInteger(receipt.sequence, "AgentInteractionSource.sequence", 1);
  invariant(receipt.source_revision === 1,
    "AgentInteractionSource source_revision must remain 1");
  invariant(
    receipt.source_sha256 === computeProductInteractionSourceSha256(receipt),
    "AgentInteractionSource.source_sha256 does not match its canonical source projection",
  );
  const isPatch = receipt.response_type === "skill_patch";
  invariant(isPatch === (typeof receipt.skill_patch_sha256 === "string"),
    "AgentInteractionSource skill_patch response/hash shape is inconsistent");
  if (isPatch) {
    invariant(
      receipt.role === "teaching_agent"
      && receipt.question === null
      && receipt.hint_level === 4,
      "AgentInteractionSource skill_patch fields are inconsistent",
    );
  }
}

export function assertProductAgentInteraction(interaction) {
  assertSafeInteger(interaction.sequence, "AgentInteraction.sequence", 1);
  assertSafeInteger(interaction.interaction_revision, "AgentInteraction.interaction_revision", 1);
  assertProductAgentInteractionSourceReceipt(interaction.projection_source);
  assertSame(interaction.projection_source.actor, interaction.request_context.actor,
    "AgentInteraction projection source actor does not match");
  assertSame(interaction.projection_source.content_ref, interaction.request_context.content_ref,
    "AgentInteraction projection source content_ref does not match");
  for (const field of ["interaction_id", "session_id", "turn_id", "sequence"]) {
    invariant(interaction.projection_source[field] === interaction[field],
      `AgentInteraction projection source ${field} does not match`);
  }
  for (const field of ["role", "response_type", "question", "hint_level"]) {
    assertSame(interaction.projection_source[field], interaction[field],
      `AgentInteraction structured field ${field} drifted from its canonical projection source`);
  }
  invariant(interaction.feedback.session_id === interaction.session_id,
    "AgentInteraction feedback.session_id does not match");
  invariant(interaction.feedback.turn_id === interaction.turn_id,
    "AgentInteraction feedback.turn_id does not match");
  invariant(interaction.feedback_event.event_type === "agent.turn.feedback_ready"
    && interaction.feedback_event.event_version === 1,
  "AgentInteraction feedback event discriminator is invalid");
  invariant(interaction.feedback_event.command_id === interaction.feedback.command_id,
    "AgentInteraction feedback event command does not match feedback");
  invariant(interaction.projection_source.command_id === interaction.feedback.command_id,
    "AgentInteraction projection source command does not match feedback");
  invariant(interaction.projection_source.feedback_event_id === interaction.feedback_event.event_id,
    "AgentInteraction projection source feedback event does not match");
  assertSame(interaction.feedback_event.content_ref, interaction.request_context.content_ref,
    "AgentInteraction feedback event content_ref does not match interaction");
  invariant(
    interaction.feedback_event.feedback_sha256 === computeProductFeedbackSha256(interaction.feedback),
    "AgentInteraction embedded feedback does not match feedback_event.feedback_sha256",
  );
  invariant(
    interaction.projection_source.feedback_sha256 === interaction.feedback_event.feedback_sha256,
    "AgentInteraction projection source feedback hash does not match the canonical feedback event",
  );
  invariant(Date.parse(interaction.feedback_event.occurred_at) >= Date.parse(interaction.feedback.completed_at),
    "AgentInteraction feedback event cannot occur before feedback completion");

  if (interaction.skill_patch !== null) {
    assertProductSkillPatch(interaction.skill_patch);
    for (const field of ["interaction_id", "session_id", "turn_id"]) {
      invariant(interaction.skill_patch[field] === interaction[field],
        `AgentInteraction SkillPatch.${field} does not match`);
    }
    for (const evidence of interaction.skill_patch.evidence_refs) {
      invariant(
        interaction.feedback.evidence_refs.some((candidate) => sameJson(candidate, evidence)),
        `AgentInteraction SkillPatch evidence ${evidence.evidence_id} is not retained byte-equivalently from canonical feedback`,
      );
    }
  }
  invariant(
    interaction.projection_source.skill_patch_sha256
      === (interaction.skill_patch?.patch_sha256 ?? null),
    "AgentInteraction SkillPatch hash drifted from its canonical projection source",
  );
  if (interaction.patch_decision === null) {
    invariant(interaction.interaction_revision === 1,
      "Undecided AgentInteraction must remain at revision 1");
  } else {
    invariant(interaction.skill_patch !== null,
      "AgentInteraction cannot carry a patch decision without a SkillPatch");
    assertProductPatchDecisionReceipt(interaction.patch_decision, interaction.skill_patch);
    invariant(interaction.interaction_revision === interaction.patch_decision.interaction_revision_after,
      "AgentInteraction revision does not match its terminal patch decision");
    assertSame(interaction.patch_decision.request_context.actor, interaction.request_context.actor,
      "AgentInteraction and PatchDecision actors must agree");
    assertSame(interaction.patch_decision.request_context.content_ref, interaction.request_context.content_ref,
      "AgentInteraction and PatchDecision content refs must agree");
  }
  invariant(Date.parse(interaction.updated_at) >= Date.parse(interaction.created_at),
    "AgentInteraction updated_at cannot be before created_at");
  invariant(interaction.projection_source.committed_at === interaction.created_at,
    "AgentInteraction creation time must equal its atomic projection source commit time");

  const selfUrl = canonicalRelativeUrl(interaction.links.self, "AgentInteraction links.self");
  const workspaceUrl = canonicalRelativeUrl(
    interaction.links.session_workspace,
    "AgentInteraction links.session_workspace",
  );
  assertExactPath(
    selfUrl,
    `/product-experience/v1/sessions/${interaction.session_id}/agent-interactions/${interaction.interaction_id}`,
    "AgentInteraction links.self",
  );
  assertExactPath(
    workspaceUrl,
    `/product-experience/v1/sessions/${interaction.session_id}/workspace`,
    "AgentInteraction links.session_workspace",
  );
  if (interaction.skill_patch === null) {
    invariant(interaction.links.skill_draft === null,
      "AgentInteraction without a SkillPatch cannot expose a skill_draft link");
  } else {
    const draftUrl = canonicalRelativeUrl(
      interaction.links.skill_draft,
      "AgentInteraction links.skill_draft",
    );
    assertExactPath(
      draftUrl,
      `/product-experience/v1/sessions/${interaction.session_id}/skill-drafts/${interaction.skill_patch.draft_id}`,
      "AgentInteraction links.skill_draft",
    );
  }
}

export function assertProductAgentInteractionPage(
  page,
  expectedAfterSequence = page.requested_after_sequence,
  expectedLimit = 50,
) {
  assertSafeInteger(expectedAfterSequence, "AgentInteractionPage expected after_sequence");
  assertSafeInteger(page.high_watermark_sequence, "AgentInteractionPage high_watermark_sequence");
  assertSafeInteger(page.requested_limit, "AgentInteractionPage requested_limit", 1);
  invariant(page.requested_limit <= 100, "AgentInteractionPage requested_limit exceeds 100");
  invariant(page.requested_after_sequence === expectedAfterSequence,
    "AgentInteractionPage requested cursor does not match the request");
  invariant(page.requested_limit === expectedLimit,
    "AgentInteractionPage requested_limit does not echo the supplied limit or the default 50");
  invariant(page.interactions.length <= page.requested_limit,
    "AgentInteractionPage returns more items than requested_limit");
  if (page.interactions.length === 0) {
    invariant(page.from_sequence === null && page.to_sequence === null,
      "Empty AgentInteractionPage must have null bounds");
    invariant(page.has_more === false, "Empty AgentInteractionPage cannot claim more items");
    invariant(page.next_after_sequence === expectedAfterSequence,
      "Empty AgentInteractionPage cannot advance its cursor");
    invariant(page.high_watermark_sequence === expectedAfterSequence,
      "Empty AgentInteractionPage high watermark must equal its accepted cursor");
    return;
  }
  invariant(page.from_sequence === expectedAfterSequence + 1,
    "AgentInteractionPage must begin immediately after the requested cursor");
  const interactionIds = new Set();
  page.interactions.forEach((interaction, index) => {
    assertProductAgentInteraction(interaction);
    invariant(interaction.session_id === page.session_id,
      "AgentInteractionPage contains an interaction from another session");
    assertSame(interaction.request_context.actor, page.request_context.actor,
      "AgentInteractionPage contains an interaction for another actor");
    assertSame(interaction.request_context.content_ref, page.request_context.content_ref,
      "AgentInteractionPage contains an interaction for another content version");
    invariant(interaction.sequence === expectedAfterSequence + index + 1,
      "AgentInteractionPage contains a sequence gap or reordering");
    invariant(!interactionIds.has(interaction.interaction_id),
      "AgentInteractionPage contains a duplicate interaction_id");
    interactionIds.add(interaction.interaction_id);
  });
  const finalSequence = page.interactions.at(-1).sequence;
  invariant(page.to_sequence === finalSequence && page.next_after_sequence === finalSequence,
    "AgentInteractionPage terminal bounds and next cursor must equal its final item sequence");
  invariant(page.high_watermark_sequence >= finalSequence,
    "AgentInteractionPage high watermark is below its final item");
  invariant(page.has_more === (finalSequence < page.high_watermark_sequence),
    "AgentInteractionPage has_more contradicts its high watermark");
}

export function assertProductWriteReconciliation(reconciliation) {
  const expected = reconciliation.error.stage === "PRODUCT_DRAFT_COMMIT"
    ? "SKILL_DRAFT"
    : "AGENT_INTERACTION";
  invariant(reconciliation.reconciliation.resource_type === expected,
    "Product reconciliation stage and resource_type do not agree");
  invariant(reconciliation.error.details.operation_was_durably_accepted === true,
    "Product reconciliation is only valid after a durable write");
  const url = new URL(reconciliation.reconciliation.resource_url, "https://contracts.yaya.local");
  invariant(url.origin === "https://contracts.yaya.local"
    && url.username === ""
    && url.password === ""
    && url.search === ""
    && url.hash === "",
  "Product reconciliation resource_url must be a credential-free canonical relative GET URL");
  const pattern = expected === "SKILL_DRAFT"
    ? /^\/product-experience\/v1\/sessions\/([A-Za-z0-9][A-Za-z0-9_-]{7,127})\/skill-drafts\/([A-Za-z0-9][A-Za-z0-9_-]{7,127})$/u
    : /^\/product-experience\/v1\/sessions\/([A-Za-z0-9][A-Za-z0-9_-]{7,127})\/agent-interactions\/([A-Za-z0-9][A-Za-z0-9_-]{7,127})$/u;
  const match = pattern.exec(url.pathname);
  invariant(match, "Product reconciliation resource_url does not name the required canonical GET route");
  invariant(match[1] === reconciliation.reconciliation.session_id,
    "Product reconciliation session_id does not match its canonical GET URL");
  invariant(match[2] === reconciliation.reconciliation.resource_id,
    "Product reconciliation resource_id does not match its canonical GET URL");
}

export function assertProductExampleSemantics(value, schemaFile) {
  const schemaName = basename(schemaFile);
  if (!PRODUCT_SCHEMA_NAMES.has(schemaName)) return;
  const validators = {
    "content-unit.schema.json": assertProductContentUnit,
    "session-workspace.schema.json": assertProductSessionWorkspace,
    "skill-draft.schema.json": assertProductSkillDraft,
    "skill-draft-upsert-request.schema.json": assertProductSkillDraftUpsert,
    "skill-patch.schema.json": assertProductSkillPatch,
    "patch-decision-request.schema.json": assertProductPatchDecisionRequest,
    "patch-decision-receipt.schema.json": assertProductPatchDecisionReceipt,
    "agent-interaction-source-receipt.schema.json": assertProductAgentInteractionSourceReceipt,
    "agent-interaction.schema.json": assertProductAgentInteraction,
    "agent-interaction-page.schema.json": assertProductAgentInteractionPage,
    "product-write-reconciliation.schema.json": assertProductWriteReconciliation,
  };
  validators[schemaName](value);
}

export function assertProductExampleRelationships(entries) {
  const named = (schemaName) => entries
    .filter((entry) => basename(entry.schemaFile) === schemaName)
    .map((entry) => entry.value);
  const drafts = named("skill-draft.schema.json");
  const patches = named("skill-patch.schema.json");
  const upserts = named("skill-draft-upsert-request.schema.json");
  const decisionRequests = named("patch-decision-request.schema.json");
  const decisionReceipts = named("patch-decision-receipt.schema.json");
  const interactionSources = named("agent-interaction-source-receipt.schema.json");
  const interactions = named("agent-interaction.schema.json");
  const workspaces = named("session-workspace.schema.json");
  const interactionPages = named("agent-interaction-page.schema.json");
  const feedbackReadyEvents = named("agent-turn-feedback-ready-event.schema.json");
  const gameSessions = named("agent-session.schema.json");
  const gameCommands = named("command.schema.json");
  const gameRuns = named("run.schema.json");
  const gameEvidence = named("evidence.schema.json");
  const worldEventPages = named("world-event-page.schema.json");
  const worldEvents = worldEventPages.flatMap((page) => page.events);
  const pageInteractions = interactionPages.flatMap((page) => page.interactions);
  const allInteractions = [...interactions, ...pageInteractions];

  const canonicalGameExamplesPresent = (
    feedbackReadyEvents.length > 0
    || gameSessions.length > 0
    || gameCommands.length > 0
    || gameRuns.length > 0
    || gameEvidence.length > 0
    || worldEventPages.length > 0
  );
  const productProjectionPresent = allInteractions.length > 0 || workspaces.length > 0;
  if (productProjectionPresent) {
    invariant(gameSessions.length > 0,
      "Product projections require canonical Game AgentSession authority");
    invariant(feedbackReadyEvents.length > 0,
      "Product interactions require canonical agent.turn.feedback_ready authority");
    invariant(interactionSources.length > 0,
      "Product interactions require canonical AgentTurn Product projection receipts");
    invariant(gameCommands.length > 0,
      "Product interactions require canonical Game Command authority");
    invariant(
      !allInteractions.some((interaction) => interaction.feedback.run_id !== null)
      || gameRuns.length > 0,
      "Run-backed Product interactions require canonical Game Run authority",
    );
    invariant(
      !allInteractions.some((interaction) => interaction.feedback.evidence_refs.length > 0)
      || gameEvidence.length > 0,
      "Evidence-backed Product interactions require canonical Game Evidence authority",
    );
  }
  if (canonicalGameExamplesPresent) {
    const exactlyOne = (items, predicate, label) => {
      const matches = items.filter(predicate);
      invariant(matches.length === 1, `${label} must resolve to exactly one canonical Game resource`);
      return matches[0];
    };
    const assertCanonicalSessionIdentity = (session, label) => {
      assertSame(session.request_context.content_ref, session.content,
        `${label} origin content_ref drifted from session.content`);
      invariant(
        session.links.self === `/v1/agent-sessions/${session.session_id}`
        && session.links.turns === `/v1/agent-sessions/${session.session_id}/turns`
        && session.links.world_snapshot === `/v1/worlds/${session.world_id}/snapshot`,
        `${label} links do not identify the canonical Game session`,
      );
    };

    for (const interaction of allInteractions) {
      const projectionSource = exactlyOne(
        interactionSources,
        (candidate) => candidate.receipt_id === interaction.projection_source.receipt_id,
        `AgentInteraction ${interaction.interaction_id} projection source ${interaction.projection_source.receipt_id}`,
      );
      assertSame(
        interaction.projection_source,
        projectionSource,
        `AgentInteraction ${interaction.interaction_id} projection source drifted from its canonical AgentTurn commit receipt`,
      );
      const feedbackEvent = exactlyOne(
        feedbackReadyEvents,
        (candidate) => candidate.event_id === interaction.feedback_event.event_id,
        `AgentInteraction ${interaction.interaction_id} feedback event ${interaction.feedback_event.event_id}`,
      );
      try {
        assertAgentTurnFeedbackReadyEvent(feedbackEvent);
      } catch (error) {
        invariant(false,
          `AgentInteraction ${interaction.interaction_id} source feedback event failed canonical semantics: ${error.message}`);
      }
      const { payload: eventPayload, ...eventEnvelope } = feedbackEvent;
      assertSame(
        interaction.feedback,
        eventPayload,
        `AgentInteraction ${interaction.interaction_id} feedback is not the canonical runtime event payload`,
      );
      assertSame(
        interaction.feedback_event,
        {
          ...eventEnvelope,
          feedback_sha256: computeProductFeedbackSha256(eventPayload),
        },
        `AgentInteraction ${interaction.interaction_id} feedback event envelope drifted from the runtime event store`,
      );

      const session = exactlyOne(
        gameSessions,
        (candidate) => candidate.session_id === interaction.session_id,
        `AgentInteraction ${interaction.interaction_id} session ${interaction.session_id}`,
      );
      assertCanonicalSessionIdentity(session,
        `AgentInteraction ${interaction.interaction_id} session ${interaction.session_id}`);
      assertSame(session.request_context.actor, interaction.request_context.actor,
        `AgentInteraction ${interaction.interaction_id} session belongs to another actor`);
      assertSame(session.content, interaction.request_context.content_ref,
        `AgentInteraction ${interaction.interaction_id} session belongs to another content version`);

      const command = exactlyOne(
        gameCommands,
        (candidate) => candidate.command_id === interaction.feedback.command_id,
        `AgentInteraction ${interaction.interaction_id} command ${interaction.feedback.command_id}`,
      );
      assertSame(command.request_context.actor, interaction.request_context.actor,
        `AgentInteraction ${interaction.interaction_id} command belongs to another actor`);
      assertSame(command.request_context.content_ref, interaction.request_context.content_ref,
        `AgentInteraction ${interaction.interaction_id} command belongs to another content version`);
      invariant(command.command_type === "EXECUTE_AGENT_TURN",
        `AgentInteraction ${interaction.interaction_id} command is not EXECUTE_AGENT_TURN`);
      invariant(command.links.self === `/v1/commands/${command.command_id}`,
        `AgentInteraction ${interaction.interaction_id} command self link is not canonical`);
      invariant(
        feedbackEvent.trace_id === command.request_context.trace_id
        && feedbackEvent.correlation_id === command.request_context.correlation_id,
        `AgentInteraction ${interaction.interaction_id} feedback event trace lineage drifted from its command`,
      );
      for (const reference of interaction.feedback.evidence_refs) {
        invariant(
          command.evidence_refs.some((candidate) => sameJson(candidate, reference)),
          `AgentInteraction ${interaction.interaction_id} evidence ${reference.evidence_id} is absent from its command`,
        );
      }

      let run = null;
      if (interaction.feedback.run_id === null) {
        invariant(command.links.run === null || command.links.run === undefined,
          `AgentInteraction ${interaction.interaction_id} has no run but its command links one`);
        invariant(command.status === "APPLIED" && command.terminal === true,
          `AgentInteraction ${interaction.interaction_id} no-run feedback is not backed by an applied terminal command`);
        invariant(command.result?.result_type === "NO_EFFECT",
          `AgentInteraction ${interaction.interaction_id} no-run feedback is not backed by NO_EFFECT`);
      } else {
        run = exactlyOne(
          gameRuns,
          (candidate) => candidate.run_id === interaction.feedback.run_id,
          `AgentInteraction ${interaction.interaction_id} run ${interaction.feedback.run_id}`,
        );
        invariant(command.links.run === `/v1/runs/${run.run_id}`,
          `AgentInteraction ${interaction.interaction_id} command does not link its canonical Run`);
        for (const field of ["session_id", "turn_id", "command_id", "run_id"]) {
          invariant(run[field] === interaction.feedback[field],
            `AgentInteraction ${interaction.interaction_id} Run.${field} does not match feedback`);
        }
        assertSame(run.request_context.actor, interaction.request_context.actor,
          `AgentInteraction ${interaction.interaction_id} Run belongs to another actor`);
        assertSame(run.request_context.content_ref, interaction.request_context.content_ref,
          `AgentInteraction ${interaction.interaction_id} Run belongs to another content version`);
        assertSame(run.agent_feedback, interaction.feedback,
          `AgentInteraction ${interaction.interaction_id} feedback drifted from its canonical Run`);
        assertSame(run.evidence_refs, interaction.feedback.evidence_refs,
          `AgentInteraction ${interaction.interaction_id} evidence set drifted from its canonical Run`);
        if (run.world_application?.status === "COMMITTED") {
          invariant(run.world_application.receipt.world_id === session.world_id,
            `AgentInteraction ${interaction.interaction_id} Run committed another session world`);
          invariant(command.links.world_snapshot === `/v1/worlds/${session.world_id}/snapshot`,
            `AgentInteraction ${interaction.interaction_id} command links another session world`);
          const cause = exactlyOne(
            worldEvents,
            (candidate) => candidate.event_id === feedbackEvent.causation_id,
            `AgentInteraction ${interaction.interaction_id} feedback causation ${feedbackEvent.causation_id}`,
          );
          invariant(
            cause.command_id === command.command_id
            && cause.stream_id === `world:${session.world_id}`
            && cause.sequence >= run.world_application.receipt.first_event_sequence
            && cause.sequence <= run.world_application.receipt.last_event_sequence
            && cause.trace_id === feedbackEvent.trace_id
            && cause.correlation_id === feedbackEvent.correlation_id
            && sameJson(cause.content_ref, interaction.request_context.content_ref)
            && cause.payload.world_revision === run.world_application.receipt.world_revision
            && Date.parse(cause.occurred_at) <= Date.parse(feedbackEvent.occurred_at),
            `AgentInteraction ${interaction.interaction_id} feedback causation is outside its committed Run`,
          );
        }
        const commandStatusForRun = {
          SUCCEEDED: "APPLIED",
          REJECTED: "REJECTED",
          FAILED: "FAILED",
          UNKNOWN: "UNKNOWN",
        }[run.status];
        invariant(
          run.terminal === true
          && command.terminal === true
          && commandStatusForRun !== undefined
          && command.status === commandStatusForRun,
          `AgentInteraction ${interaction.interaction_id} command terminal state disagrees with its Run`,
        );
        if (run.status === "SUCCEEDED") {
          invariant(command.result?.result_type === "WORLD_COMMIT",
            `AgentInteraction ${interaction.interaction_id} committed Run is not backed by WORLD_COMMIT`);
        }
        if (command.result?.result_type === "WORLD_COMMIT"
          && run.world_application?.status === "COMMITTED") {
          for (const field of [
            "world_id", "previous_revision", "world_revision", "first_event_sequence",
            "last_event_sequence",
          ]) {
            invariant(command.result[field] === run.world_application.receipt[field],
              `AgentInteraction ${interaction.interaction_id} command ${field} drifted from its Run receipt`);
          }
        }
      }

      for (const reference of interaction.feedback.evidence_refs) {
        const evidence = exactlyOne(
          gameEvidence,
          (candidate) => candidate.evidence_ref.evidence_id === reference.evidence_id,
          `AgentInteraction ${interaction.interaction_id} evidence ${reference.evidence_id}`,
        );
        assertSame(evidence.evidence_ref, reference,
          `AgentInteraction ${interaction.interaction_id} evidence reference is not immutable`);
        assertSame(evidence.request_context.actor, interaction.request_context.actor,
          `AgentInteraction ${interaction.interaction_id} evidence belongs to another actor`);
        assertSame(evidence.request_context.content_ref, interaction.request_context.content_ref,
          `AgentInteraction ${interaction.interaction_id} evidence belongs to another content version`);
        invariant(evidence.subject.learner_id === session.learner_id,
          `AgentInteraction ${interaction.interaction_id} evidence belongs to another learner`);
        try {
          assertEvidenceIntegrity(evidence);
        } catch (error) {
          invariant(false,
            `AgentInteraction ${interaction.interaction_id} evidence ${reference.evidence_id} failed canonical integrity: ${error.message}`);
        }
        const evidenceKind = evidence.payload.evidence_kind;
        if (run === null) {
          invariant(!["WORLD_COMMIT", "SKILL_RUN"].includes(evidenceKind),
            `AgentInteraction ${interaction.interaction_id} no-run feedback contains run-scoped evidence`);
        } else if (evidenceKind === "SKILL_RUN") {
          invariant(
            evidence.source.source_type === "SKILL_RUN"
            && evidence.source.source_id === run.run_id
            && evidence.source.command_id === interaction.feedback.command_id
            && evidence.payload.run_id === run.run_id
            && evidence.payload.sandbox_status === run.sandbox.status
            && evidence.payload.world_status === run.world_application.status
            && evidence.payload.intent_count === run.sandbox.action_intents.length,
            `AgentInteraction ${interaction.interaction_id} SKILL_RUN evidence drifted from its canonical Run`,
          );
          if (evidence.payload.world_status === "COMMITTED") {
            invariant(evidence.source.world_id === session.world_id,
              `AgentInteraction ${interaction.interaction_id} SKILL_RUN evidence belongs to another world`);
          }
        } else if (evidenceKind === "WORLD_COMMIT") {
          try {
            assertWorldCommitEvidence(evidence);
          } catch (error) {
            invariant(false,
              `AgentInteraction ${interaction.interaction_id} evidence ${reference.evidence_id} failed WORLD_COMMIT semantics: ${error.message}`);
          }
          invariant(evidence.source.command_id === interaction.feedback.command_id,
            `AgentInteraction ${interaction.interaction_id} WORLD_COMMIT evidence belongs to another command`);
          const receipt = run.world_application.receipt;
          for (const field of [
            "world_id", "previous_revision", "world_revision", "first_event_sequence",
            "last_event_sequence", "state_hash",
          ]) {
            invariant(evidence.payload[field] === receipt[field],
              `AgentInteraction ${interaction.interaction_id} evidence ${reference.evidence_id} ${field} drifted from its Run receipt`);
          }
        }
      }
    }

    for (const workspace of workspaces) {
      const session = exactlyOne(
        gameSessions,
        (candidate) => candidate.session_id === workspace.session.session_id,
        `SessionWorkspace ${workspace.workspace_id} session ${workspace.session.session_id}`,
      );
      assertCanonicalSessionIdentity(session,
        `SessionWorkspace ${workspace.workspace_id} session ${workspace.session.session_id}`);
      assertSame(session.request_context.actor, workspace.request_context.actor,
        `SessionWorkspace ${workspace.workspace_id} canonical session belongs to another actor`);
      assertSame(session.content, workspace.content_ref,
        `SessionWorkspace ${workspace.workspace_id} canonical session belongs to another content version`);
      for (const field of [
        "session_id", "world_id", "learner_id", "agent_profile_id", "channel", "created_at",
      ]) {
        invariant(workspace.session[field] === session[field],
          `SessionWorkspace ${workspace.workspace_id} embedded session ${field} drifted`);
      }
      assertSame(workspace.session.request_context.actor, session.request_context.actor,
        `SessionWorkspace ${workspace.workspace_id} embedded session actor drifted`);
      assertSame(workspace.session.content, session.content,
        `SessionWorkspace ${workspace.workspace_id} embedded session content drifted`);
      assertSame(workspace.session.versions, session.versions,
        `SessionWorkspace ${workspace.workspace_id} embedded session versions drifted`);
      invariant(
        workspace.session.links.self === `/v1/agent-sessions/${session.session_id}`
        && workspace.session.links.turns === `/v1/agent-sessions/${session.session_id}/turns`
        && workspace.session.links.world_snapshot === `/v1/worlds/${session.world_id}/snapshot`,
        `SessionWorkspace ${workspace.workspace_id} embedded session links drifted`,
      );
      const checkpointMatchesRun = gameRuns.some((run) => (
        run.session_id === session.session_id
        && run.world_application?.status === "COMMITTED"
        && run.world_application.receipt.world_id === workspace.world_checkpoint.world_id
        && run.world_application.receipt.world_revision === workspace.world_checkpoint.world_revision
        && run.world_application.receipt.last_event_sequence === workspace.world_checkpoint.last_event_sequence
        && run.world_application.receipt.state_hash === workspace.world_checkpoint.state_hash
      ));
      invariant(checkpointMatchesRun,
        `SessionWorkspace ${workspace.workspace_id} world checkpoint lacks a canonical committed Run`);
    }
  }

  for (const patch of patches) {
    const base = drafts.find((draft) => (
      draft.session_id === patch.session_id
      && draft.draft_id === patch.draft_id
      && draft.revision === patch.base_draft_revision
      && draft.draft_sha256 === patch.base_draft_sha256
    ));
    invariant(base, `No frozen base SkillDraft exists for patch ${patch.patch_id}`);
    const applied = applyProductSkillPatch(base, patch);
    const terminalReceipt = decisionReceipts.find((receipt) => receipt.patch_id === patch.patch_id);
    const projectedResult = drafts.find((draft) => (
      draft.session_id === patch.session_id
      && draft.draft_id === patch.draft_id
      && draft.revision === patch.base_draft_revision + 1
      && draft.draft_sha256 === patch.result_draft_sha256
    ));
    if (terminalReceipt?.decision === "ACCEPT") {
      invariant(projectedResult, `No frozen result SkillDraft exists for accepted patch ${patch.patch_id}`);
      invariant(projectedResult.display_name === applied.display_name, "Frozen patch result display name drifted");
      assertSame(projectedResult.source_bundle, applied.source_bundle, "Frozen patch result source bundle drifted");
      invariant(projectedResult.last_applied_patch_id === patch.patch_id,
        "Frozen patch result last_applied_patch_id does not identify the accepted patch");
    } else {
      invariant(!projectedResult,
        `Non-accepted patch ${patch.patch_id} must not have a persisted result draft`);
      invariant(!drafts.some((draft) => draft.last_applied_patch_id === patch.patch_id),
        `Non-accepted patch ${patch.patch_id} must not identify any persisted SkillDraft result`);
    }
  }

  for (const upsert of upserts.filter((request) => request.base_revision > 0)) {
    const base = drafts.find((draft) => (
      draft.session_id === upsert.session_id
      && draft.draft_id === upsert.draft_id
      && draft.revision === upsert.base_revision
      && draft.draft_sha256 === upsert.base_draft_sha256
    ));
    invariant(base, `No frozen base SkillDraft exists for upsert ${upsert.draft_id}`);
    const result = drafts.find((draft) => (
      draft.session_id === upsert.session_id
      && draft.draft_id === upsert.draft_id
      && draft.revision === upsert.base_revision + 1
    ));
    invariant(result, `No frozen result SkillDraft exists for upsert ${upsert.draft_id}`);
    for (const field of ["skill_id", "content_ref", "display_name", "source_bundle"]) {
      assertSame(result[field], upsert[field], `SkillDraft upsert result ${field} drifted`);
    }
  }

  for (const request of decisionRequests) {
    const patch = patches.find((candidate) => candidate.patch_id === request.patch_id);
    const interaction = interactions.find((candidate) => (
      candidate.interaction_id === request.interaction_id
      && candidate.patch_decision === null
    ));
    invariant(patch && interaction, `Decision request ${request.decision_id} lacks frozen inputs`);
    assertProductPatchDecisionRequest(request, { patch, interaction });
  }
  for (const receipt of decisionReceipts) {
    const patch = patches.find((candidate) => candidate.patch_id === receipt.patch_id);
    const request = decisionRequests.find((candidate) => candidate.patch_id === receipt.patch_id);
    invariant(patch && request, `Decision receipt ${receipt.decision_id} lacks frozen command inputs`);
    assertProductPatchDecisionReceipt(receipt, patch, request);
  }
  for (const interaction of allInteractions) {
    const equivalent = allInteractions.filter((candidate) => (
      candidate.interaction_id === interaction.interaction_id
      && candidate.interaction_revision === interaction.interaction_revision
    ));
    for (const candidate of equivalent) {
      assertSame(candidate, interaction,
        `AgentInteraction ${interaction.interaction_id} revision ${interaction.interaction_revision} drifted`);
    }
  }
  for (const source of interactionSources) {
    const sourceInteractions = allInteractions.filter((interaction) => (
      interaction.projection_source.receipt_id === source.receipt_id
    ));
    invariant(sourceInteractions.length > 0,
      `Projection source ${source.receipt_id} is not used by any AgentInteraction`);
    invariant(new Set(sourceInteractions.map((interaction) => interaction.interaction_id)).size === 1,
      `Projection source ${source.receipt_id} is reused across AgentInteraction identities`);
    invariant(!interactionSources.some((candidate) => (
      candidate.receipt_id !== source.receipt_id
      && candidate.feedback_event_id === source.feedback_event_id
    )), `Feedback event ${source.feedback_event_id} has more than one Product projection source`);
  }
  for (const decided of interactions.filter((interaction) => interaction.patch_decision !== null)) {
    const pending = interactions.find((interaction) => (
      interaction.interaction_id === decided.interaction_id
      && interaction.interaction_revision + 1 === decided.interaction_revision
      && interaction.patch_decision === null
    ));
    invariant(pending, `Decided interaction ${decided.interaction_id} lacks its prior undecided revision`);
    for (const field of [
      "request_context", "interaction_id", "session_id", "turn_id", "sequence", "projection_source", "role",
      "response_type", "question", "hint_level", "feedback", "feedback_event", "skill_patch",
      "created_at", "links",
    ]) {
      assertSame(decided[field], pending[field],
        `AgentInteraction ${decided.interaction_id} mutated immutable field ${field}`);
    }
    const receipt = decisionReceipts.find((candidate) => (
      candidate.decision_id === decided.patch_decision.decision_id
    ));
    invariant(receipt, `Decided interaction ${decided.interaction_id} lacks its frozen receipt`);
    assertSame(decided.patch_decision, receipt,
      `Decided interaction ${decided.interaction_id} embeds a drifted receipt`);
  }
  for (const result of drafts) {
    const predecessor = drafts.find((draft) => (
      draft.session_id === result.session_id
      && draft.draft_id === result.draft_id
      && draft.revision + 1 === result.revision
    ));
    if (predecessor) {
      assertSame(result.request_context, predecessor.request_context,
        "SkillDraft origin request_context changed across revisions");
      invariant(result.created_at === predecessor.created_at,
        "SkillDraft created_at changed across revisions");
    }
  }
  for (const workspace of workspaces) {
    const baselinePage = interactionPages.find((page) => (
      page.session_id === workspace.session.session_id && page.requested_after_sequence === 0
    ));
    invariant(baselinePage, `Workspace ${workspace.workspace_id} lacks a frozen baseline interaction page`);
    assertSame(baselinePage.request_context.actor, workspace.request_context.actor,
      "Workspace interaction page belongs to another actor");
    assertSame(baselinePage.request_context.content_ref, workspace.content_ref,
      "Workspace interaction page belongs to another content version");
    invariant(baselinePage.high_watermark_sequence === workspace.last_interaction_sequence,
      "Workspace last_interaction_sequence does not match its interaction page high watermark");
    for (const reference of workspace.skill_draft_refs) {
      const draft = drafts.find((candidate) => (
        candidate.session_id === workspace.session.session_id
        && candidate.draft_id === reference.draft_id
        && candidate.skill_id === reference.skill_id
        && candidate.revision === reference.revision
        && candidate.draft_sha256 === reference.draft_sha256
      ));
      invariant(draft, `Workspace draft reference ${reference.draft_id} is not frozen byte-consistently`);
      assertSame(draft.request_context.actor, workspace.request_context.actor,
        `Workspace draft reference ${reference.draft_id} belongs to another actor`);
      assertSame(draft.content_ref, workspace.content_ref,
        `Workspace draft reference ${reference.draft_id} belongs to another content version`);
      const url = new URL(reference.url, "https://contracts.yaya.local");
      invariant(
        url.origin === "https://contracts.yaya.local"
        && url.search === ""
        && url.hash === ""
        && url.pathname === `/product-experience/v1/sessions/${workspace.session.session_id}/skill-drafts/${reference.draft_id}`,
        `Workspace draft reference ${reference.draft_id} URL does not identify the canonical Product GET`,
      );
    }
  }
}
