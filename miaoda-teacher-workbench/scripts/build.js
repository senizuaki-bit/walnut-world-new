#!/usr/bin/env node
'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { spawn } = require('node:child_process');

const { packageManagerCommand } = require('./package-manager');

const projectRoot = path.resolve(__dirname, '..');
const distRoot = path.join(projectRoot, 'dist');
const serverOutput = path.join(distRoot, 'server');

function run(command, args, options = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      cwd: projectRoot,
      env: process.env,
      stdio: 'inherit',
      ...options,
    });
    child.once('error', reject);
    child.once('exit', (code, signal) => {
      if (code === 0) {
        resolve();
        return;
      }
      reject(
        new Error(
          signal
            ? `${command} terminated by ${signal}`
            : `${command} exited with code ${code ?? 1}`,
        ),
      );
    });
  });
}

function runNpm(args) {
  const invocation = packageManagerCommand('npm', args);
  return run(invocation.command, invocation.args);
}

function copyBuildRuntimeFiles() {
  const nestedClientOutput = path.join(serverOutput, 'dist', 'client');
  fs.mkdirSync(nestedClientOutput, { recursive: true });

  const clientOutput = path.join(distRoot, 'client');
  if (fs.existsSync(clientOutput)) {
    for (const name of fs.readdirSync(clientOutput)) {
      if (name.endsWith('.html')) {
        fs.copyFileSync(
          path.join(clientOutput, name),
          path.join(nestedClientOutput, name),
        );
      }
    }
  }

  fs.copyFileSync(
    path.join(projectRoot, 'scripts', 'run.sh'),
    path.join(serverOutput, 'run.sh'),
  );
  fs.rmSync(path.join(distRoot, 'scripts'), { recursive: true, force: true });
  fs.rmSync(path.join(distRoot, 'tsconfig.node.tsbuildinfo'), { force: true });
}

async function main() {
  const startedAt = Date.now();
  console.log('[build] Generating API sources...');
  await runNpm(['run', 'gen:openapi']);

  console.log('[build] Cleaning dist/...');
  fs.rmSync(distRoot, { recursive: true, force: true });

  console.log('[build] Building server and client...');
  await Promise.all([
    runNpm(['run', 'build:server']),
    runNpm(['run', 'build:client']),
  ]);

  console.log('[build] Preparing runtime files...');
  copyBuildRuntimeFiles();

  console.log('[build] Pruning runtime dependencies...');
  await run(process.execPath, [path.join(projectRoot, 'scripts', 'prune-smart.js')]);
  console.log(`[build] Complete in ${((Date.now() - startedAt) / 1000).toFixed(1)}s`);
}

main().catch((error) => {
  console.error('[build] Failed:', error instanceof Error ? error.message : error);
  process.exit(1);
});
