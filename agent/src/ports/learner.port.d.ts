import type { RuntimeEvent } from "../domain/events.js";
import type { LearnerModelSnapshot, LearnerUpdate } from "../domain/learner.js";
import type { LearnerId, OperationContext } from "../domain/primitives.js";
import type { AsyncResult, ContractError } from "../domain/result.js";

/** Learner state is derived from immutable evidence; callers cannot set mastery directly. */
export interface LearnerPort {
  project(
    event: RuntimeEvent,
    expectedLearnerRevision: number,
    context: OperationContext,
  ): AsyncResult<LearnerUpdate, ContractError>;

  getSnapshot(
    learnerId: LearnerId,
    context: OperationContext,
  ): AsyncResult<LearnerModelSnapshot, ContractError>;

  rebuild(
    learnerId: LearnerId,
    throughSequence: number,
    context: OperationContext,
  ): AsyncResult<LearnerModelSnapshot, ContractError>;
}
