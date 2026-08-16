import type {
  ActorRef,
  ContentRef,
  ISODateTime,
  JsonObject,
  VersionSet,
} from "./primitives.js";
import type { ContractError } from "./result.js";

export interface PolicyInput {
  readonly actor: ActorRef;
  readonly action: string;
  readonly resource: JsonObject;
  readonly environment: JsonObject;
  readonly content_ref: ContentRef;
  readonly versions: VersionSet;
}

export interface PolicyGrant {
  readonly decision_id: string;
  readonly policy_version: string;
  readonly allowed: true;
  readonly issued_at: ISODateTime;
  readonly expires_at: ISODateTime;
  readonly capability_token: string;
  readonly constraints: JsonObject;
}

export interface PolicyDenial extends ContractError {
  readonly category: "POLICY" | "AUTHORIZATION";
  readonly retryable: false;
}
