import type {
  ISODateTime,
  JsonObject,
  RequestContext,
  RunId,
  WorldId,
} from "./primitives.js";
import type { ContractError } from "./result.js";
import type { SkillRef } from "./skills.js";
import type { EventAppendReceipt, UncommittedEvent } from "./events.js";
import type { OutboxMessage } from "./outbox.js";

export interface ActionIntentBase extends JsonObject {
  readonly intent_id: string;
  readonly actor_entity_id: string;
  readonly expected_world_revision: number;
}

export interface WorldPosition extends JsonObject {
  readonly x: number;
  readonly y: number;
}

export interface MoveIntent extends ActionIntentBase {
  readonly action_type: "MOVE";
  readonly destination: WorldPosition;
}

export interface PlantIntent extends ActionIntentBase {
  readonly action_type: "PLANT";
  readonly plot_id: string;
  readonly crop_type: string;
}

export interface WaterIntent extends ActionIntentBase {
  readonly action_type: "WATER";
  readonly plot_id: string;
  readonly amount_ml: number;
}

export interface HarvestIntent extends ActionIntentBase {
  readonly action_type: "HARVEST";
  readonly plot_id: string;
}

export interface InteractIntent extends ActionIntentBase {
  readonly action_type: "INTERACT";
  readonly target_entity_id: string;
  readonly interaction: string;
}

export interface SpeakIntent extends ActionIntentBase {
  readonly action_type: "SPEAK";
  readonly text: string;
  readonly audience: "LEARNER" | "NEARBY_ENTITIES";
}

export type ActionIntent =
  | MoveIntent
  | PlantIntent
  | WaterIntent
  | HarvestIntent
  | InteractIntent
  | SpeakIntent;

export interface WorldSnapshot<State extends JsonObject = JsonObject> {
  readonly request_context: RequestContext;
  readonly world_id: WorldId;
  readonly revision: number;
  readonly last_event_sequence: number;
  readonly state_schema_version: "1.0.0";
  readonly state_hash: string;
  readonly generated_at: ISODateTime;
  readonly world_rules_version: string;
  readonly state: State;
}

export interface WorldCommand {
  readonly run_id: RunId;
  readonly world_id: WorldId;
  readonly expected_world_revision: number;
  readonly world_rules_version: string;
  readonly skill_ref: SkillRef;
  readonly intents: readonly [ActionIntent, ...ActionIntent[]];
}

export interface WorldCommitReceipt {
  readonly world_id: WorldId;
  readonly previous_revision: number;
  readonly world_revision: number;
  readonly first_event_sequence: number;
  readonly last_event_sequence: number;
  readonly committed_at: ISODateTime;
  readonly state_hash: string;
}

export interface WorldAtomicCommit {
  readonly stream_id: string;
  readonly expected_stream_sequence: number | "NO_STREAM";
  readonly command: WorldCommand;
  readonly events: readonly UncommittedEvent[];
  readonly outbox_messages: readonly OutboxMessage[];
}

export interface WorldAtomicCommitReceipt {
  /** Must equal both the request stream_id and events.stream_id. */
  readonly stream_id: string;
  readonly world: WorldCommitReceipt;
  readonly events: EventAppendReceipt;
  readonly outbox_messages: readonly OutboxMessage[];
}

export interface WorldRejection extends ContractError {
  readonly category: "WORLD_RULE" | "CONCURRENCY" | "INVARIANT";
}
