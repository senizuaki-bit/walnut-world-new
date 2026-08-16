import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import {
  existsSync,
  readFileSync,
  readdirSync,
  realpathSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { dirname, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const SCRIPT_PATH = fileURLToPath(import.meta.url);
const DEFAULT_ROOT = resolve(dirname(SCRIPT_PATH), "..");
const MANIFEST_RELATIVE_PATH = "contracts/manifest.json";
const RELEASE_BASELINES = Object.freeze([
  Object.freeze({
    label: "v0.3",
    lockRelativePath: "contracts/releases/agent-contracts-v0.3.lock.json",
    packageVersion: "0.3.0",
    gitRelease: "refs/tags/agent-contracts-v0.3.0",
    baselineCommit: "7841120",
    manifestBytes: 25384,
    manifestSha256: "f1898f70642c2387965ca8b15c32df611eb92cd69c3f42de61fa7c6fb242917e",
    fileCount: 135,
    manifestEntryCount: 134,
    baseEntriesSha256: "c4f10d841d72d6d6888aa95343845a89d5688909aa42df92491bcec589dddbf9",
    inventoryField: "additive_paths",
    additivePaths: Object.freeze([
      "contracts/examples/game-student-bootstrap-v2.json",
      "contracts/openapi/student-bootstrap-v2.openapi.json",
      "contracts/schemas/game/student-bootstrap-v2.schema.json",
    ]),
  }),
  Object.freeze({
    label: "v0.4",
    lockRelativePath: "contracts/releases/agent-contracts-v0.4.lock.json",
    packageVersion: "0.4.0",
    gitRelease: "refs/tags/agent-contracts-v0.4.0",
    baselineCommit: "0494c0f8ef6eb505e43db84c0249b046be35c589",
    manifestBytes: 26127,
    manifestSha256: "b62a6152f1f2fd87d1941beecd3a1d47089811e91b067a8b225ff0d7a5ce72b9",
    fileCount: 139,
    manifestEntryCount: 138,
    baseEntriesSha256: "7d51c31e4798d5495f3ef3fdcf5d96fe3a7f4a0739da92b41905f52ee6c61062",
    inventoryField: "files",
  }),
  Object.freeze({
    label: "v0.5",
    lockRelativePath: "contracts/releases/agent-contracts-v0.5.lock.json",
    packageVersion: "0.5.0",
    gitRelease: "refs/tags/agent-contracts-v0.5.0",
    baselineCommit: null,
    releaseStatus: "WORKTREE_CANDIDATE_NOT_TAGGED",
    manifestBytes: 27087,
    manifestSha256: "e90eed36e7e9c003e033884e05f19d858b0ca0b44f88660e11c8e4d7fa8a6c8b",
    fileCount: 144,
    manifestEntryCount: 143,
    baseEntriesSha256: "fa1d18988cb61cd4370a22b5396778b66d1f54866e30144392c06e9a9ee062b1",
    inventoryField: "files",
  }),
]);
const RELEASE_ENVIRONMENT_VARIABLE = "YAYA_CONTRACT_GIT_RELEASE";
const PLACEHOLDER_RELEASE = /(?:placeholder|todo|tbd|unknown|unreleased|snapshot|changeme|example|latest)/iu;
const MOVING_RELEASE = /^refs\/tags\/(?:main|master|head|dev)$/iu;

export const MANIFEST_SCHEMA_VERSION = "1.0.0";

export class ContractManifestError extends Error {
  constructor(message, problems = []) {
    super(message);
    this.name = "ContractManifestError";
    this.problems = Object.freeze([...problems]);
  }
}

function normalizedRelativePath(root, path) {
  return relative(root, path).split(sep).join("/");
}

function exactKeys(value, expected) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const actual = Object.keys(value).sort();
  return actual.length === expected.length
    && expected.every((key, index) => actual[index] === key);
}

function readJson(path, label) {
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch (error) {
    throw new ContractManifestError(`${label} is not valid JSON: ${error.message}`);
  }
}

export function validateGitRelease(value, label = "git_release") {
  if (typeof value !== "string"
    || !/^refs\/tags\/[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$/u.test(value)
    || value.endsWith("/")
    || value.includes("//")
    || value.includes("..")
    || PLACEHOLDER_RELEASE.test(value)
    || MOVING_RELEASE.test(value)) {
    throw new ContractManifestError(
      `${label} must be an explicit immutable tag ref such as refs/tags/agent-contracts-v0.1.0`,
    );
  }
  return value;
}

function runGit(root, arguments_) {
  const result = spawnSync("git", ["-C", root, ...arguments_], {
    encoding: "utf8",
    windowsHide: true,
  });
  if (result.status !== 0) {
    const detail = (result.stderr || result.stdout || "git command failed").trim();
    throw new ContractManifestError(detail);
  }
  return result.stdout;
}

function tryReadGitBlob(root, revision, path) {
  const result = spawnSync("git", ["-C", root, "show", `${revision}:${path}`], {
    encoding: null,
    windowsHide: true,
  });
  return result.status === 0 ? result.stdout : null;
}

function baseEntriesDigest(entries) {
  const tuples = entries.map((entry) => [entry.path, entry.bytes, entry.sha256]);
  return createHash("sha256").update(JSON.stringify(tuples)).digest("hex");
}

function baselineIdentityProblems(lock, spec) {
  const problems = [];
  if (lock?.schema_version !== "1.0.0"
    || lock?.package_name !== "@yaya/agent-contracts"
    || lock?.package_version !== spec.packageVersion
    || lock?.git_release !== spec.gitRelease
    || lock?.baseline_commit !== spec.baselineCommit
    || lock?.manifest_path !== MANIFEST_RELATIVE_PATH
    || lock?.manifest_bytes !== spec.manifestBytes
    || lock?.manifest_sha256 !== spec.manifestSha256
    || lock?.file_count !== spec.fileCount
    || lock?.manifest_entry_count !== spec.manifestEntryCount
    || lock?.base_entries_digest_format !== "json-array[path,bytes,sha256]"
    || lock?.base_entries_sha256 !== spec.baseEntriesSha256) {
    problems.push(`${spec.label} baseline lock identity drifted`);
  }
  if (spec.releaseStatus !== undefined && lock?.release_status !== spec.releaseStatus) {
    problems.push(`${spec.label} release status drifted`);
  }
  return problems;
}

/** Verify that every manifest entry frozen by one historical release remains exact. */
function inspectReleaseBaseline({ root = DEFAULT_ROOT, files, spec }) {
  const projectRoot = resolve(root);
  const lockPath = resolve(projectRoot, spec.lockRelativePath);
  if (!existsSync(lockPath)) {
    const packagePath = resolve(projectRoot, "package.json");
    let officialPackage = false;
    if (existsSync(packagePath)) {
      try {
        officialPackage = readJson(packagePath, "package.json").name === "@yaya/agent-contracts";
      } catch {
        officialPackage = true;
      }
    }
    const problems = officialPackage ? [`${spec.label} baseline lock is missing`] : [];
    return { ok: problems.length === 0, problems, lock: null };
  }

  const problems = [];
  let lock;
  try {
    lock = readJson(lockPath, spec.lockRelativePath);
  } catch (error) {
    return { ok: false, problems: [error.message], lock: null };
  }
  const lockKeys = [
    "additive_paths", "base_entries_digest_format", "base_entries_sha256",
    "baseline_commit", "file_count", "git_release", "manifest_bytes",
    "manifest_entry_count", "manifest_path", "manifest_sha256", "package_name",
    "package_version", "schema_version",
  ];
  if (spec.releaseStatus !== undefined) lockKeys.push("release_status");
  if (spec.inventoryField === "files") {
    lockKeys.splice(lockKeys.indexOf("additive_paths"), 1, "files");
  }
  lockKeys.sort();
  if (!exactKeys(lock, lockKeys)) {
    problems.push(`${spec.label} baseline lock is not the closed v1 shape`);
  }
  problems.push(...baselineIdentityProblems(lock, spec));
  if (spec.inventoryField === "additive_paths"
    && (!Array.isArray(lock?.additive_paths)
      || JSON.stringify(lock.additive_paths) !== JSON.stringify(spec.additivePaths))) {
    problems.push(`${spec.label} baseline lock additive_paths is invalid`);
  }
  if (spec.inventoryField === "files" && !Array.isArray(lock?.files)) {
    problems.push(`${spec.label} baseline lock files must be an array`);
  }
  if (!Array.isArray(files)) {
    problems.push(`${spec.label} baseline verification requires a manifest file inventory`);
    return { ok: false, problems, lock };
  }

  let baselineManifest = null;
  const baselineBlob = spec.baselineCommit === null
    ? null
    : tryReadGitBlob(projectRoot, spec.baselineCommit, MANIFEST_RELATIVE_PATH);
  if (baselineBlob !== null) {
    if (baselineBlob.byteLength !== lock.manifest_bytes) {
      problems.push(`${spec.label} manifest byte count drifted: expected ${String(lock.manifest_bytes)}, got ${baselineBlob.byteLength}`);
    }
    const baselineSha = createHash("sha256").update(baselineBlob).digest("hex");
    if (baselineSha !== lock.manifest_sha256) {
      problems.push(`${spec.label} manifest sha256 drifted: expected ${String(lock.manifest_sha256)}, got ${baselineSha}`);
    }
    try {
      baselineManifest = JSON.parse(baselineBlob.toString("utf8"));
    } catch (error) {
      problems.push(`${spec.label} baseline manifest is not valid JSON: ${error.message}`);
    }
  } else if (spec.baselineCommit !== null) {
    problems.push(`${spec.label} baseline manifest is unavailable at ${spec.baselineCommit}`);
  }

  let baselineEntries;
  if (spec.baselineCommit === null && Array.isArray(lock?.files)) {
    baselineEntries = lock.files;
  } else if (Array.isArray(baselineManifest?.files)) {
    baselineEntries = baselineManifest.files;
    if (baselineManifest.git_release !== lock.git_release
      || baselineManifest.package_version !== lock.package_version) {
      problems.push(`${spec.label} baseline manifest release identity disagrees with its lock`);
    }
  } else {
    const excluded = new Set([spec.lockRelativePath, ...(lock?.additive_paths ?? [])]);
    baselineEntries = files.filter((entry) => !excluded.has(entry.path));
  }
  if (spec.inventoryField === "files"
    && JSON.stringify(lock?.files) !== JSON.stringify(baselineEntries)) {
    problems.push(`${spec.label} baseline lock file inventory disagrees with its release manifest`);
  }
  if (baselineEntries.length !== lock?.manifest_entry_count
    || baselineEntries.length + 1 !== lock?.file_count) {
    problems.push(
      `${spec.label} frozen file count drifted: expected ${String(lock?.manifest_entry_count)} manifest entries plus manifest, got ${baselineEntries.length}`,
    );
  }
  const digest = baseEntriesDigest(baselineEntries);
  if (digest !== lock?.base_entries_sha256) {
    problems.push(`${spec.label} entry digest drifted: expected ${String(lock?.base_entries_sha256)}, got ${digest}`);
  }
  const current = new Map(files.map((entry) => [entry.path, entry]));
  for (const entry of baselineEntries) {
    const actual = current.get(entry.path);
    if (!actual) {
      problems.push(`${spec.label} frozen file was removed: ${entry.path}`);
    } else if (actual.bytes !== entry.bytes || actual.sha256 !== entry.sha256) {
      problems.push(`${spec.label} frozen file drifted: ${entry.path}`);
    }
  }
  return { ok: problems.length === 0, problems, lock, baselineEntries };
}

export function inspectReleaseBaselines({ root = DEFAULT_ROOT, files } = {}) {
  const baselines = RELEASE_BASELINES.map((spec) => (
    inspectReleaseBaseline({ root, files, spec })
  ));
  const problems = baselines.flatMap((baseline) => baseline.problems);
  return { ok: problems.length === 0, problems, baselines };
}

export function inspectV03Baseline(options = {}) {
  return inspectReleaseBaseline({ ...options, spec: RELEASE_BASELINES[0] });
}

export function inspectV04Baseline(options = {}) {
  return inspectReleaseBaseline({ ...options, spec: RELEASE_BASELINES[1] });
}

export function inspectV05Baseline(options = {}) {
  return inspectReleaseBaseline({ ...options, spec: RELEASE_BASELINES[2] });
}

export function buildReleaseBaselineLock({ root = DEFAULT_ROOT, release } = {}) {
  const projectRoot = resolve(root);
  const spec = RELEASE_BASELINES.find((candidate) => candidate.label === release);
  if (!spec) {
    throw new ContractManifestError(
      `unknown historical release ${String(release)}; expected ${RELEASE_BASELINES.map((item) => item.label).join(" or ")}`,
    );
  }
  const blob = spec.baselineCommit === null
    ? readFileSync(resolve(projectRoot, MANIFEST_RELATIVE_PATH))
    : tryReadGitBlob(projectRoot, spec.baselineCommit, MANIFEST_RELATIVE_PATH);
  if (blob === null) {
    throw new ContractManifestError(
      `cannot read ${MANIFEST_RELATIVE_PATH} from ${String(spec.baselineCommit)}`,
    );
  }
  const actualBytes = blob.byteLength;
  const actualSha256 = createHash("sha256").update(blob).digest("hex");
  if (actualBytes !== spec.manifestBytes || actualSha256 !== spec.manifestSha256) {
    throw new ContractManifestError(
      `${spec.label} release manifest identity does not match the trusted baseline`,
    );
  }
  let manifest;
  try {
    manifest = JSON.parse(blob.toString("utf8"));
  } catch (error) {
    throw new ContractManifestError(`${spec.label} release manifest is invalid JSON: ${error.message}`);
  }
  if (manifest.package_name !== "@yaya/agent-contracts"
    || manifest.package_version !== spec.packageVersion
    || manifest.git_release !== spec.gitRelease
    || !Array.isArray(manifest.files)
    || manifest.files.length !== spec.manifestEntryCount
    || baseEntriesDigest(manifest.files) !== spec.baseEntriesSha256) {
    throw new ContractManifestError(`${spec.label} release manifest content identity drifted`);
  }
  const lock = {
    schema_version: "1.0.0",
    package_name: manifest.package_name,
    package_version: manifest.package_version,
    git_release: manifest.git_release,
    baseline_commit: spec.baselineCommit,
    manifest_path: MANIFEST_RELATIVE_PATH,
    manifest_bytes: spec.manifestBytes,
    manifest_sha256: spec.manifestSha256,
    file_count: spec.fileCount,
    manifest_entry_count: spec.manifestEntryCount,
    base_entries_digest_format: "json-array[path,bytes,sha256]",
    base_entries_sha256: spec.baseEntriesSha256,
  };
  if (spec.releaseStatus !== undefined) lock.release_status = spec.releaseStatus;
  if (spec.inventoryField === "files") lock.files = manifest.files;
  else lock.additive_paths = [...spec.additivePaths];
  return { spec, lock };
}

export function writeReleaseBaselineLock({ root = DEFAULT_ROOT, release } = {}) {
  const projectRoot = resolve(root);
  const { spec, lock } = buildReleaseBaselineLock({ root: projectRoot, release });
  const lockPath = resolve(projectRoot, spec.lockRelativePath);
  writeFileSync(lockPath, `${JSON.stringify(lock, null, 2)}\n`, "utf8");
  return { path: spec.lockRelativePath, lock };
}

function isSameRepositoryRoot(projectRoot, repositoryRoot) {
  if (repositoryRoot === projectRoot) return true;
  if (process.platform !== "win32") return false;

  try {
    const canonicalProjectRoot = realpathSync.native(projectRoot);
    const canonicalRepositoryRoot = realpathSync.native(repositoryRoot);
    if (canonicalProjectRoot.toLowerCase() === canonicalRepositoryRoot.toLowerCase()) return true;

    const projectIdentity = statSync(canonicalProjectRoot, { bigint: true });
    const repositoryIdentity = statSync(canonicalRepositoryRoot, { bigint: true });
    const identityIsAvailable = projectIdentity.dev !== 0n || projectIdentity.ino !== 0n;
    return identityIsAvailable
      && projectIdentity.isDirectory()
      && repositoryIdentity.isDirectory()
      && projectIdentity.dev === repositoryIdentity.dev
      && projectIdentity.ino === repositoryIdentity.ino;
  } catch {
    return false;
  }
}

export function verifyGitReleaseRef({ root = DEFAULT_ROOT, gitRelease } = {}) {
  const projectRoot = resolve(root);
  const release = validateGitRelease(gitRelease);
  let repositoryRoot;
  let releaseCommit;
  let headCommit;
  try {
    repositoryRoot = resolve(runGit(projectRoot, ["rev-parse", "--show-toplevel"]).trim());
    releaseCommit = runGit(projectRoot, ["rev-parse", "--verify", `${release}^{commit}`]).trim();
    headCommit = runGit(projectRoot, ["rev-parse", "--verify", "HEAD^{commit}"]).trim();
  } catch (error) {
    throw new ContractManifestError(
      `cannot verify ${release}: ${error.message}`,
    );
  }
  if (!isSameRepositoryRoot(projectRoot, repositoryRoot)) {
    throw new ContractManifestError(
      `release verification root must be the Git repository root: expected ${projectRoot}, got ${repositoryRoot}`,
    );
  }
  if (releaseCommit !== headCommit) {
    throw new ContractManifestError(
      `${release} resolves to ${releaseCommit}, not current release commit ${headCommit}`,
    );
  }
  let taggedManifest;
  try {
    taggedManifest = runGit(projectRoot, ["show", `${release}:${MANIFEST_RELATIVE_PATH}`]);
  } catch (error) {
    throw new ContractManifestError(
      `${release} does not contain ${MANIFEST_RELATIVE_PATH}: ${error.message}`,
    );
  }
  const workingManifest = readFileSync(resolve(projectRoot, MANIFEST_RELATIVE_PATH), "utf8");
  if (taggedManifest.replace(/\r\n/gu, "\n") !== workingManifest.replace(/\r\n/gu, "\n")) {
    throw new ContractManifestError(
      `${release} contains a different ${MANIFEST_RELATIVE_PATH}`,
    );
  }
  return releaseCommit;
}

function walkWireFiles(directory, manifestPath, output = []) {
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    const path = resolve(directory, entry.name);
    if (entry.isSymbolicLink()) {
      throw new ContractManifestError(`wire contracts may not contain symlinks: ${path}`);
    }
    if (entry.isDirectory()) walkWireFiles(path, manifestPath, output);
    else if (entry.isFile() && path !== manifestPath) output.push(path);
    else if (!entry.isFile()) {
      throw new ContractManifestError(`unsupported wire contract entry: ${path}`);
    }
  }
  return output;
}

function wireFileEntry(root, path) {
  const bytes = readFileSync(path);
  return {
    path: normalizedRelativePath(root, path),
    bytes: bytes.byteLength,
    sha256: createHash("sha256").update(bytes).digest("hex"),
  };
}

export function buildContractManifest({ root = DEFAULT_ROOT, gitRelease } = {}) {
  const projectRoot = resolve(root);
  const packagePath = resolve(projectRoot, "package.json");
  const contractsRoot = resolve(projectRoot, "contracts");
  const manifestPath = resolve(projectRoot, MANIFEST_RELATIVE_PATH);
  if (!existsSync(packagePath)) throw new ContractManifestError(`missing package.json under ${projectRoot}`);
  if (!existsSync(contractsRoot) || !statSync(contractsRoot).isDirectory()) {
    throw new ContractManifestError(`missing contracts directory under ${projectRoot}`);
  }
  const packageDocument = readJson(packagePath, "package.json");
  if (typeof packageDocument.name !== "string" || packageDocument.name.length === 0) {
    throw new ContractManifestError("package.json name must be non-empty text");
  }
  if (typeof packageDocument.version !== "string"
    || !/^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$/u.test(packageDocument.version)) {
    throw new ContractManifestError("package.json version must be a semantic version");
  }
  const release = validateGitRelease(gitRelease);
  const files = walkWireFiles(contractsRoot, manifestPath)
    .map((path) => wireFileEntry(projectRoot, path))
    .sort((left, right) => left.path < right.path ? -1 : left.path > right.path ? 1 : 0);
  if (files.length === 0) throw new ContractManifestError("contracts contains no wire files");
  return {
    schema_version: MANIFEST_SCHEMA_VERSION,
    package_name: packageDocument.name,
    package_version: packageDocument.version,
    git_release: release,
    hash_algorithm: "sha256",
    files,
  };
}

function inspectManifestShape(manifest) {
  const problems = [];
  if (!exactKeys(manifest, [
    "files", "git_release", "hash_algorithm", "package_name", "package_version", "schema_version",
  ].sort())) {
    problems.push("manifest top-level fields are not the closed v1 shape");
  }
  if (manifest?.schema_version !== MANIFEST_SCHEMA_VERSION) {
    problems.push(`schema_version drift: expected ${MANIFEST_SCHEMA_VERSION}, got ${String(manifest?.schema_version)}`);
  }
  if (manifest?.hash_algorithm !== "sha256") {
    problems.push(`hash_algorithm drift: expected sha256, got ${String(manifest?.hash_algorithm)}`);
  }
  try {
    validateGitRelease(manifest?.git_release, "manifest git_release");
  } catch (error) {
    problems.push(error.message);
  }
  if (!Array.isArray(manifest?.files)) {
    problems.push("manifest files must be an array");
    return problems;
  }
  const seen = new Set();
  let previousPath = "";
  manifest.files.forEach((entry, index) => {
    if (!exactKeys(entry, ["bytes", "path", "sha256"])) {
      problems.push(`files[${index}] is not the closed v1 file-entry shape`);
      return;
    }
    if (typeof entry.path !== "string"
      || !entry.path.startsWith("contracts/")
      || entry.path.includes("\\")
      || entry.path === MANIFEST_RELATIVE_PATH) {
      problems.push(`files[${index}].path is not a normalized wire path`);
    }
    if (seen.has(entry.path)) problems.push(`duplicate manifest path: ${entry.path}`);
    if (previousPath && entry.path <= previousPath) {
      problems.push(`manifest file list is not strictly sorted at ${entry.path}`);
    }
    seen.add(entry.path);
    previousPath = entry.path;
    if (!Number.isInteger(entry.bytes) || entry.bytes < 0) {
      problems.push(`invalid byte size for ${String(entry.path)}`);
    }
    if (typeof entry.sha256 !== "string" || !/^[a-f0-9]{64}$/u.test(entry.sha256)) {
      problems.push(`invalid sha256 for ${String(entry.path)}`);
    }
  });
  return problems;
}

export function inspectContractManifest({
  root = DEFAULT_ROOT,
  gitRelease,
  verifyGitRef = false,
} = {}) {
  const projectRoot = resolve(root);
  const manifestPath = resolve(projectRoot, MANIFEST_RELATIVE_PATH);
  if (!existsSync(manifestPath)) {
    return { ok: false, problems: [`missing ${MANIFEST_RELATIVE_PATH}`] };
  }
  let manifest;
  try {
    manifest = readJson(manifestPath, MANIFEST_RELATIVE_PATH);
  } catch (error) {
    return { ok: false, problems: [error.message] };
  }
  const problems = inspectManifestShape(manifest);
  let release = manifest?.git_release;
  if (gitRelease !== undefined) {
    try {
      release = validateGitRelease(gitRelease, "requested git_release");
      if (manifest?.git_release !== release) {
        problems.push(`git_release drift: manifest=${String(manifest?.git_release)} requested=${release}`);
      }
    } catch (error) {
      problems.push(error.message);
    }
  }
  if (problems.some((problem) => problem.includes("git_release")) && release === undefined) {
    return { ok: false, problems };
  }
  let expected;
  try {
    expected = buildContractManifest({ root: projectRoot, gitRelease: release });
  } catch (error) {
    problems.push(error.message);
    return { ok: false, problems };
  }
  problems.push(...inspectReleaseBaselines({ root: projectRoot, files: expected.files }).problems);
  for (const field of ["package_name", "package_version"]) {
    if (manifest?.[field] !== expected[field]) {
      problems.push(`${field} drift: manifest=${String(manifest?.[field])} actual=${expected[field]}`);
    }
  }
  const declared = new Map(
    Array.isArray(manifest?.files)
      ? manifest.files.filter((entry) => entry && typeof entry.path === "string")
        .map((entry) => [entry.path, entry])
      : [],
  );
  const actual = new Map(expected.files.map((entry) => [entry.path, entry]));
  for (const [path, actualEntry] of actual) {
    const declaredEntry = declared.get(path);
    if (!declaredEntry) {
      problems.push(`added wire file is not locked: ${path}`);
      continue;
    }
    if (declaredEntry.bytes !== actualEntry.bytes) {
      problems.push(`size drift: ${path} manifest=${String(declaredEntry.bytes)} actual=${actualEntry.bytes}`);
    }
    if (declaredEntry.sha256 !== actualEntry.sha256) {
      problems.push(`hash drift: ${path} manifest=${String(declaredEntry.sha256)} actual=${actualEntry.sha256}`);
    }
  }
  for (const path of declared.keys()) {
    if (!actual.has(path)) problems.push(`removed wire file is still locked: ${path}`);
  }
  if (verifyGitRef && problems.length === 0) {
    try {
      verifyGitReleaseRef({ root: projectRoot, gitRelease: release });
    } catch (error) {
      problems.push(error.message);
    }
  }
  return { ok: problems.length === 0, problems, manifest, expected };
}

export function checkContractManifest(options = {}) {
  const result = inspectContractManifest(options);
  if (!result.ok) {
    throw new ContractManifestError("wire contract manifest check failed", result.problems);
  }
  return result.manifest;
}

export function writeContractManifest({ root = DEFAULT_ROOT, gitRelease } = {}) {
  const projectRoot = resolve(root);
  const manifest = buildContractManifest({ root: projectRoot, gitRelease });
  const baseline = inspectReleaseBaselines({ root: projectRoot, files: manifest.files });
  if (!baseline.ok) {
    throw new ContractManifestError("historical frozen baseline check failed", baseline.problems);
  }
  const manifestPath = resolve(projectRoot, MANIFEST_RELATIVE_PATH);
  writeFileSync(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`, "utf8");
  return manifest;
}

function usage() {
  return [
    "Usage:",
    "  node scripts/generate-contract-manifest.mjs --git-release refs/tags/<immutable-tag>",
    "  node scripts/generate-contract-manifest.mjs --write-release-lock <v0.3|v0.4|v0.5>",
    "  node scripts/generate-contract-manifest.mjs --check [--git-release refs/tags/<immutable-tag>] [--verify-git-ref]",
    "Options:",
    "  --root <path>          Repository root (primarily for monorepos and tests).",
    `  --git-release <ref>   Explicit release tag; may also use ${RELEASE_ENVIRONMENT_VARIABLE}.`,
    "  --check                Verify only; never writes or accepts drift.",
    "  --verify-git-ref       Require the tag to exist at HEAD and contain this exact manifest.",
    "  --write-release-lock   Regenerate one historical lock from its trusted release manifest.",
  ].join("\n");
}

function parseArguments(argv) {
  const options = {
    check: false,
    root: DEFAULT_ROOT,
    gitRelease: undefined,
    verifyGitRef: false,
    writeReleaseLock: undefined,
    help: false,
  };
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === "--check") options.check = true;
    else if (argument === "--verify-git-ref") options.verifyGitRef = true;
    else if (argument === "--help" || argument === "-h") options.help = true;
    else if (argument === "--root" || argument === "--git-release" || argument === "--write-release-lock") {
      const value = argv[index + 1];
      if (!value || value.startsWith("--")) {
        throw new ContractManifestError(`${argument} requires one value`);
      }
      if (argument === "--root") options.root = resolve(value);
      else if (argument === "--git-release") options.gitRelease = value;
      else options.writeReleaseLock = value;
      index += 1;
    } else if (argument.startsWith("--root=")) options.root = resolve(argument.slice(7));
    else if (argument.startsWith("--git-release=")) options.gitRelease = argument.slice(14);
    else throw new ContractManifestError(`unknown argument: ${argument}`);
  }
  if (options.gitRelease === undefined
    && options.writeReleaseLock === undefined
    && process.env[RELEASE_ENVIRONMENT_VARIABLE]) {
    options.gitRelease = process.env[RELEASE_ENVIRONMENT_VARIABLE];
  }
  return options;
}

function runCli() {
  try {
    const options = parseArguments(process.argv.slice(2));
    if (options.help) {
      console.log(usage());
      return;
    }
    if (options.verifyGitRef && !options.check) {
      throw new ContractManifestError("--verify-git-ref requires --check");
    }
    if (options.writeReleaseLock !== undefined) {
      if (options.check || options.verifyGitRef || options.gitRelease !== undefined) {
        throw new ContractManifestError("--write-release-lock cannot be combined with manifest release/check options");
      }
      const written = writeReleaseBaselineLock({
        root: options.root,
        release: options.writeReleaseLock,
      });
      console.log(
        `CONTRACT_RELEASE_LOCK_WRITTEN path=${written.path} files=${written.lock.manifest_entry_count}`,
      );
      return;
    }
    if (options.check) {
      const manifest = checkContractManifest(options);
      console.log(
        `CONTRACT_MANIFEST_CHECK_OK files=${manifest.files.length} release=${manifest.git_release}`,
      );
      return;
    }
    if (options.gitRelease === undefined) {
      throw new ContractManifestError(
        `generation requires --git-release or ${RELEASE_ENVIRONMENT_VARIABLE}; no Git HEAD fallback is allowed`,
      );
    }
    const manifest = writeContractManifest(options);
    console.log(
      `CONTRACT_MANIFEST_WRITTEN files=${manifest.files.length} release=${manifest.git_release}`,
    );
  } catch (error) {
    console.error(`CONTRACT_MANIFEST_FAILED ${error.message}`);
    for (const problem of error.problems ?? []) console.error(`- ${problem}`);
    process.exitCode = 1;
  }
}

if (process.argv[1] && resolve(process.argv[1]) === resolve(SCRIPT_PATH)) runCli();
