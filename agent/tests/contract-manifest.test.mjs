import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import {
  cpSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  realpathSync,
  rmSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import test from "node:test";
import {
  inspectReleaseBaselines,
  inspectV03Baseline,
} from "../scripts/generate-contract-manifest.mjs";
import { PROJECT_ROOT } from "../scripts/validate-contracts.mjs";

const GENERATOR = resolve(PROJECT_ROOT, "scripts/generate-contract-manifest.mjs");
const RELEASE = "refs/tags/agent-contracts-v0.6.0";

function run(root, ...arguments_) {
  return spawnSync(process.execPath, [GENERATOR, "--root", root, ...arguments_], {
    encoding: "utf8",
    env: { ...process.env, YAYA_CONTRACT_GIT_RELEASE: "" },
  });
}

function write(path, value) {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, value, "utf8");
}

function populateFixture(root) {
  write(join(root, "package.json"), JSON.stringify({
    name: "@yaya/agent-contracts-fixture",
    version: "1.2.3",
  }));
  write(join(root, "contracts", "openapi", "game.json"), "{\"openapi\":\"3.1.0\"}\n");
  write(join(root, "contracts", "schemas", "a.json"), "{\"type\":\"object\"}\n");
  const generated = run(root, "--git-release", RELEASE);
  assert.equal(generated.status, 0, generated.stderr || generated.stdout);
  return root;
}

function fixture() {
  return populateFixture(mkdtempSync(join(tmpdir(), "yaya-contract-manifest-")));
}

function windowsShortPath(root) {
  const result = spawnSync(
    process.env.ComSpec ?? "cmd.exe",
    ["/d", "/c", "for %I in (.) do @echo %~fsI"],
    { cwd: root, encoding: "utf8", windowsHide: true },
  );
  assert.equal(result.status, 0, result.stderr || result.stdout);
  return resolve(result.stdout.trim());
}

function git(root, ...arguments_) {
  const result = spawnSync("git", ["-C", root, ...arguments_], {
    encoding: "utf8",
    windowsHide: true,
  });
  assert.equal(result.status, 0, result.stderr || result.stdout);
  return result.stdout.trim();
}

function commitFixture(root, { tag = false } = {}) {
  git(root, "init");
  git(root, "config", "user.name", "Yaya Contract Test");
  git(root, "config", "user.email", "contracts@example.invalid");
  git(root, "add", ".");
  git(root, "commit", "-m", "contract fixture");
  if (tag) git(root, "tag", RELEASE.slice("refs/tags/".length));
}

function manifestPath(root) {
  return join(root, "contracts", "manifest.json");
}

function expectCheckFailure(root, pattern, ...extraArguments) {
  const before = readFileSync(manifestPath(root), "utf8");
  const checked = run(root, "--check", ...extraArguments);
  assert.notEqual(checked.status, 0, "--check silently accepted contract drift");
  assert.match(`${checked.stdout}\n${checked.stderr}`, pattern);
  assert.equal(
    readFileSync(manifestPath(root), "utf8"),
    before,
    "--check must never rewrite or accept the manifest",
  );
}

test("checked-in wire manifest is complete and locked", () => {
  const checked = run(PROJECT_ROOT, "--check", "--git-release", RELEASE);
  assert.equal(checked.status, 0, checked.stderr || checked.stdout);
  assert.match(checked.stdout, /CONTRACT_MANIFEST_CHECK_OK/u);
});

test("v0.4 manifest preserves every v0.3 entry and the exact v0.3 manifest blob", () => {
  const lock = JSON.parse(readFileSync(
    resolve(PROJECT_ROOT, "contracts/releases/agent-contracts-v0.3.lock.json"),
    "utf8",
  ));
  const baselineResult = spawnSync(
    "git",
    ["-C", PROJECT_ROOT, "show", `${lock.baseline_commit}:${lock.manifest_path}`],
    { encoding: null, windowsHide: true },
  );
  assert.equal(baselineResult.status, 0, baselineResult.stderr?.toString() ?? "git show failed");
  assert.equal(baselineResult.stdout.byteLength, lock.manifest_bytes);
  assert.equal(
    createHash("sha256").update(baselineResult.stdout).digest("hex"),
    lock.manifest_sha256,
  );
  const baseline = JSON.parse(baselineResult.stdout.toString("utf8"));
  assert.equal(baseline.files.length, lock.manifest_entry_count);
  assert.equal(baseline.files.length + 1, lock.file_count);
  assert.equal(baseline.git_release, lock.git_release);

  const current = JSON.parse(readFileSync(manifestPath(PROJECT_ROOT), "utf8"));
  const currentByPath = new Map(current.files.map((entry) => [entry.path, entry]));
  for (const entry of baseline.files) assert.deepEqual(currentByPath.get(entry.path), entry);

  const verified = inspectV03Baseline({ root: PROJECT_ROOT, files: current.files });
  assert.equal(verified.ok, true, verified.problems.join("\n"));
  const driftedFiles = current.files.map((entry) => ({ ...entry }));
  const target = driftedFiles.find((entry) => entry.path === baseline.files[0].path);
  target.sha256 = "0".repeat(64);
  const rejected = inspectV03Baseline({ root: PROJECT_ROOT, files: driftedFiles });
  assert.equal(rejected.ok, false);
  assert.match(rejected.problems.join("\n"), /v0\.3 frozen file drifted/u);
});

test("current manifest preserves all 138 v0.4 wire files and the exact v0.4 manifest blob", () => {
  const lock = JSON.parse(readFileSync(
    resolve(PROJECT_ROOT, "contracts/releases/agent-contracts-v0.4.lock.json"),
    "utf8",
  ));
  assert.deepEqual(Object.keys(lock).sort(), [
    "base_entries_digest_format",
    "base_entries_sha256",
    "baseline_commit",
    "file_count",
    "files",
    "git_release",
    "manifest_bytes",
    "manifest_entry_count",
    "manifest_path",
    "manifest_sha256",
    "package_name",
    "package_version",
    "schema_version",
  ]);
  assert.equal(lock.schema_version, "1.0.0");
  assert.equal(lock.package_name, "@yaya/agent-contracts");
  assert.equal(lock.package_version, "0.4.0");
  assert.equal(lock.git_release, "refs/tags/agent-contracts-v0.4.0");
  assert.equal(lock.baseline_commit, "0494c0f8ef6eb505e43db84c0249b046be35c589");
  assert.equal(lock.manifest_path, "contracts/manifest.json");
  assert.equal(lock.manifest_bytes, 26127);
  assert.equal(
    lock.manifest_sha256,
    "b62a6152f1f2fd87d1941beecd3a1d47089811e91b067a8b225ff0d7a5ce72b9",
  );
  assert.equal(lock.file_count, 139);
  assert.equal(lock.manifest_entry_count, 138);
  assert.equal(lock.base_entries_digest_format, "json-array[path,bytes,sha256]");
  assert.equal(
    lock.base_entries_sha256,
    "7d51c31e4798d5495f3ef3fdcf5d96fe3a7f4a0739da92b41905f52ee6c61062",
  );
  assert.equal(lock.files.length, 138);

  const baselineResult = spawnSync(
    "git",
    ["-C", PROJECT_ROOT, "show", `${lock.baseline_commit}:${lock.manifest_path}`],
    { encoding: null, windowsHide: true },
  );
  assert.equal(baselineResult.status, 0, baselineResult.stderr?.toString() ?? "git show failed");
  assert.equal(baselineResult.stdout.byteLength, lock.manifest_bytes);
  assert.equal(
    createHash("sha256").update(baselineResult.stdout).digest("hex"),
    lock.manifest_sha256,
  );
  const baseline = JSON.parse(baselineResult.stdout.toString("utf8"));
  assert.deepEqual(lock.files, baseline.files);

  const current = JSON.parse(readFileSync(manifestPath(PROJECT_ROOT), "utf8"));
  const verified = inspectReleaseBaselines({ root: PROJECT_ROOT, files: current.files });
  assert.equal(verified.ok, true, verified.problems.join("\n"));

  const driftedFiles = current.files.map((entry) => ({ ...entry }));
  const v04OnlyPath = "contracts/schemas/game/student-bootstrap-v2.schema.json";
  const target = driftedFiles.find((entry) => entry.path === v04OnlyPath);
  assert.ok(target, `${v04OnlyPath} must be part of the v0.4 release`);
  target.sha256 = "0".repeat(64);
  const rejected = inspectReleaseBaselines({ root: PROJECT_ROOT, files: driftedFiles });
  assert.equal(rejected.ok, false);
  assert.match(rejected.problems.join("\n"), /v0\.4 frozen file drifted/u);
});

test("v0.6 freezes the exact untagged v0.5 candidate without counting its own lock", () => {
  const lock = JSON.parse(readFileSync(
    resolve(PROJECT_ROOT, "contracts/releases/agent-contracts-v0.5.lock.json"),
    "utf8",
  ));
  assert.equal(lock.package_version, "0.5.0");
  assert.equal(lock.git_release, "refs/tags/agent-contracts-v0.5.0");
  assert.equal(lock.release_status, "WORKTREE_CANDIDATE_NOT_TAGGED");
  assert.equal(lock.baseline_commit, null);
  assert.equal(lock.manifest_entry_count, 143);
  assert.equal(lock.file_count, 144);
  assert.equal(lock.manifest_bytes, 27087);
  assert.equal(
    lock.manifest_sha256,
    "e90eed36e7e9c003e033884e05f19d858b0ca0b44f88660e11c8e4d7fa8a6c8b",
  );
  assert.equal(lock.files.length, 143);
  assert.equal(
    lock.files.some((entry) => entry.path === "contracts/releases/agent-contracts-v0.5.lock.json"),
    false,
  );
  const current = JSON.parse(readFileSync(manifestPath(PROJECT_ROOT), "utf8"));
  const verified = inspectReleaseBaselines({ root: PROJECT_ROOT, files: current.files });
  assert.equal(verified.ok, true, verified.problems.join("\n"));
});

test("the official package fails closed when either historical release lock is missing", () => {
  const root = mkdtempSync(join(tmpdir(), "yaya-contract-missing-locks-"));
  try {
    write(join(root, "package.json"), JSON.stringify({
      name: "@yaya/agent-contracts",
      version: "0.6.0",
    }));
    const result = inspectReleaseBaselines({ root, files: [] });
    assert.equal(result.ok, false);
    assert.match(result.problems.join("\n"), /v0\.3 baseline lock is missing/u);
    assert.match(result.problems.join("\n"), /v0\.4 baseline lock is missing/u);
    assert.match(result.problems.join("\n"), /v0\.5 baseline lock is missing/u);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("manifest generation is byte-for-byte deterministic and requires an explicit release", () => {
  const root = fixture();
  try {
    const first = readFileSync(manifestPath(root), "utf8");
    const regenerated = run(root, "--git-release", RELEASE);
    assert.equal(regenerated.status, 0, regenerated.stderr || regenerated.stdout);
    assert.equal(readFileSync(manifestPath(root), "utf8"), first);

    const missingRelease = run(root);
    assert.notEqual(missingRelease.status, 0);
    assert.match(missingRelease.stderr, /no Git HEAD fallback is allowed/u);
    const environmentGenerated = spawnSync(
      process.execPath,
      [GENERATOR, "--root", root],
      {
        encoding: "utf8",
        env: { ...process.env, YAYA_CONTRACT_GIT_RELEASE: RELEASE },
      },
    );
    assert.equal(
      environmentGenerated.status,
      0,
      environmentGenerated.stderr || environmentGenerated.stdout,
    );
    const placeholder = run(root, "--git-release", "refs/tags/PLACEHOLDER");
    assert.notEqual(placeholder.status, 0);
    assert.match(placeholder.stderr, /explicit immutable tag ref/u);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("release verification proves the immutable tag exists at this exact manifest commit", async (context) => {
  await context.test("tagged release", () => {
    const root = fixture();
    try {
      commitFixture(root, { tag: true });
      const checked = run(
        root,
        "--check",
        "--git-release",
        RELEASE,
        "--verify-git-ref",
      );
      assert.equal(checked.status, 0, checked.stderr || checked.stdout);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });

  await context.test(
    "platform-native canonical path identifies the same repository root",
    () => {
      const root = fixture();
      try {
        commitFixture(root, { tag: true });
        const candidateRoot = process.platform === "win32"
          ? windowsShortPath(root)
          : resolve(root);
        assert.equal(
          realpathSync.native(candidateRoot).toLowerCase(),
          realpathSync.native(root).toLowerCase(),
          "the native and canonical paths must identify the same fixture directory",
        );

        const checked = run(
          candidateRoot,
          "--check",
          "--git-release",
          RELEASE,
          "--verify-git-ref",
        );
        assert.equal(checked.status, 0, checked.stderr || checked.stdout);
      } finally {
        rmSync(root, { recursive: true, force: true });
      }
    },
  );

  await context.test("a genuinely different repository root is rejected", () => {
    const repositoryRoot = mkdtempSync(join(tmpdir(), "yaya-contract-repository-"));
    const projectRoot = populateFixture(join(repositoryRoot, "nested-project"));
    try {
      commitFixture(repositoryRoot, { tag: true });
      expectCheckFailure(
        projectRoot,
        /release verification root must be the Git repository root/u,
        "--git-release",
        RELEASE,
        "--verify-git-ref",
      );
    } finally {
      rmSync(repositoryRoot, { recursive: true, force: true });
    }
  });

  await context.test("missing tag", () => {
    const root = fixture();
    try {
      commitFixture(root);
      expectCheckFailure(
        root,
        /cannot verify refs\/tags\/agent-contracts-v0\.6\.0/u,
        "--git-release",
        RELEASE,
        "--verify-git-ref",
      );
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });

  await context.test("tag on an older commit", () => {
    const root = fixture();
    try {
      commitFixture(root, { tag: true });
      write(join(root, "release-notes.txt"), "later commit\n");
      git(root, "add", "release-notes.txt");
      git(root, "commit", "-m", "move head");
      expectCheckFailure(
        root,
        /not current release commit/u,
        "--git-release",
        RELEASE,
        "--verify-git-ref",
      );
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });

  const root = fixture();
  try {
    const invalidMode = run(root, "--git-release", RELEASE, "--verify-git-ref");
    assert.notEqual(invalidMode.status, 0);
    assert.match(invalidMode.stderr, /requires --check/u);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("--check rejects added and removed wire files without accepting them", async (context) => {
  await context.test("added", () => {
    const root = fixture();
    try {
      write(join(root, "contracts", "schemas", "new.json"), "{}\n");
      expectCheckFailure(root, /added wire file is not locked: contracts\/schemas\/new\.json/u);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
  await context.test("removed", () => {
    const root = fixture();
    try {
      unlinkSync(join(root, "contracts", "schemas", "a.json"));
      expectCheckFailure(root, /removed wire file is still locked: contracts\/schemas\/a\.json/u);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
});

test("--check independently rejects hash and byte-size drift", async (context) => {
  await context.test("same-size hash drift", () => {
    const root = fixture();
    try {
      const path = join(root, "contracts", "schemas", "a.json");
      const original = readFileSync(path, "utf8");
      writeFileSync(path, original.replace("object", "string"), "utf8");
      assert.equal(readFileSync(path).byteLength, Buffer.byteLength(original));
      expectCheckFailure(root, /hash drift: contracts\/schemas\/a\.json/u);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
  await context.test("size drift", () => {
    const root = fixture();
    try {
      const path = join(root, "contracts", "schemas", "a.json");
      writeFileSync(path, `${readFileSync(path, "utf8")} `, "utf8");
      expectCheckFailure(root, /size drift: contracts\/schemas\/a\.json/u);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
});

test("--check rejects schema, package and requested Git release metadata drift", async (context) => {
  await context.test("schema_version", () => {
    const root = fixture();
    try {
      const path = manifestPath(root);
      const manifest = JSON.parse(readFileSync(path, "utf8"));
      manifest.schema_version = "9.9.9";
      writeFileSync(path, `${JSON.stringify(manifest, null, 2)}\n`, "utf8");
      expectCheckFailure(root, /schema_version drift/u);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
  await context.test("package_version", () => {
    const root = fixture();
    try {
      const packagePath = join(root, "package.json");
      const packageDocument = JSON.parse(readFileSync(packagePath, "utf8"));
      packageDocument.version = "1.2.4";
      writeFileSync(packagePath, JSON.stringify(packageDocument), "utf8");
      expectCheckFailure(root, /package_version drift/u);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
  await context.test("package_name", () => {
    const root = fixture();
    try {
      const packagePath = join(root, "package.json");
      const packageDocument = JSON.parse(readFileSync(packagePath, "utf8"));
      packageDocument.name = "@yaya/drifted-contracts";
      writeFileSync(packagePath, JSON.stringify(packageDocument), "utf8");
      expectCheckFailure(root, /package_name drift/u);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
  await context.test("git_release", () => {
    const root = fixture();
    try {
      expectCheckFailure(
        root,
        /git_release drift/u,
        "--git-release",
        "refs/tags/agent-contracts-v1.2.4",
      );
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
  await context.test("placeholder git_release", () => {
    const root = fixture();
    try {
      const path = manifestPath(root);
      const manifest = JSON.parse(readFileSync(path, "utf8"));
      manifest.git_release = "refs/tags/TODO";
      writeFileSync(path, `${JSON.stringify(manifest, null, 2)}\n`, "utf8");
      expectCheckFailure(root, /explicit immutable tag ref/u);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
});

test("manifest never hashes itself", () => {
  const root = fixture();
  try {
    const manifest = JSON.parse(readFileSync(manifestPath(root), "utf8"));
    assert.deepEqual(
      manifest.files.map((entry) => entry.path),
      ["contracts/openapi/game.json", "contracts/schemas/a.json"],
    );
    const copy = join(root, "contracts", "manifest-copy.json");
    cpSync(manifestPath(root), copy);
    expectCheckFailure(root, /added wire file is not locked: contracts\/manifest-copy\.json/u);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});
