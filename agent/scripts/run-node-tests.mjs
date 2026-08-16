import { spawnSync } from "node:child_process";
import { readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const scriptDirectory = dirname(fileURLToPath(import.meta.url));
const agentRoot = dirname(scriptDirectory);
const testDirectory = join(agentRoot, "tests");
const testFiles = readdirSync(testDirectory, { withFileTypes: true })
  .filter((entry) => entry.isFile() && entry.name.endsWith(".test.mjs"))
  .map((entry) => join(testDirectory, entry.name))
  .sort((left, right) => left.localeCompare(right, "en"));

if (testFiles.length === 0) {
  console.error("No Node contract test files were found.");
  process.exitCode = 1;
} else {
  const result = spawnSync(
    process.execPath,
    ["--test", "--test-reporter=tap", ...testFiles],
    {
    cwd: agentRoot,
    encoding: "utf8",
    },
  );

  if (result.error) {
    throw result.error;
  }
  process.stdout.write(result.stdout ?? "");
  process.stderr.write(result.stderr ?? "");
  if (result.signal) {
    console.error(`Node contract tests terminated by signal ${result.signal}.`);
    process.exitCode = 1;
  } else if ((result.status ?? 1) !== 0) {
    process.exitCode = result.status ?? 1;
  } else {
    const skippedSummaries = [
      ...(result.stdout ?? "").matchAll(/^# skipped (\d+)\s*$/gmu),
    ];
    const skipped = skippedSummaries.at(-1);
    if (!skipped) {
      console.error("Node contract tests did not emit a TAP skipped-test summary.");
      process.exitCode = 1;
    } else if (Number.parseInt(skipped[1], 10) !== 0) {
      console.error(`Node contract tests skipped ${skipped[1]} test(s); release gates allow zero skips.`);
      process.exitCode = 1;
    } else {
      process.exitCode = 0;
    }
  }
}
