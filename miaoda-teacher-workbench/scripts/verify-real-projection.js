#!/usr/bin/env node
'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const SAFE_IDENTIFIER = /^[A-Za-z0-9_.:-]{1,128}$/u;
const SAFE_CONTAINER_IDENTIFIER = /^[A-Za-z0-9_.-]{1,128}$/u;
const SAFE_APP_ID = /^app_[A-Za-z0-9]+$/u;

function parseArgs(argv) {
  const values = {
    appId: '',
    runId: '',
    tenantId: 'tenant_yaya',
    container: 'walnut-int3-postgres',
    database: 'walnut_int3',
    databaseUser: 'walnut',
  };
  const mapping = {
    '--app-id': 'appId',
    '--run-id': 'runId',
    '--tenant-id': 'tenantId',
    '--container': 'container',
    '--database': 'database',
    '--database-user': 'databaseUser',
  };

  for (let index = 0; index < argv.length; index += 1) {
    const key = mapping[argv[index]];
    if (!key || index + 1 >= argv.length) {
      throw new Error(`Unknown or incomplete argument: ${argv[index]}`);
    }
    values[key] = argv[index + 1];
    index += 1;
  }

  if (!SAFE_APP_ID.test(values.appId)) {
    throw new Error('--app-id must be a Miaoda app_ identifier');
  }
  for (const [name, value] of [
    ['--run-id', values.runId],
    ['--tenant-id', values.tenantId],
  ]) {
    if (!SAFE_IDENTIFIER.test(value)) {
      throw new Error(`${name} contains unsupported characters`);
    }
  }
  for (const [name, value] of [
    ['--container', values.container],
    ['--database', values.database],
    ['--database-user', values.databaseUser],
  ]) {
    if (!SAFE_CONTAINER_IDENTIFIER.test(value)) {
      throw new Error(`${name} contains unsupported characters`);
    }
  }
  return values;
}

function run(command, args, options = {}) {
  const result = spawnSync(command, args, {
    cwd: path.resolve(__dirname, '..'),
    encoding: 'utf8',
    maxBuffer: 4 * 1024 * 1024,
    windowsHide: true,
    ...options,
  });
  if (result.error) {
    throw result.error;
  }
  if (result.status !== 0) {
    const details = (result.stderr || result.stdout || '').trim();
    throw new Error(
      `${command} exited with code ${result.status}${details ? `: ${details}` : ''}`,
    );
  }
  return result.stdout.trim();
}

function larkInvocation(args) {
  const explicitEntry = process.env.LARK_CLI_ENTRY;
  const candidates = [
    explicitEntry,
    process.env.APPDATA
      ? path.join(
          process.env.APPDATA,
          'npm',
          'node_modules',
          '@larksuite',
          'cli',
          'scripts',
          'run.js',
        )
      : null,
  ].filter(Boolean);
  const entry = candidates.find((candidate) => fs.existsSync(candidate));
  if (entry) {
    return { command: process.execPath, args: [entry, ...args] };
  }
  return { command: 'lark-cli', args };
}

function readAuthority(options) {
  const sql = `
WITH selected AS (
  SELECT *
  FROM learner_projection_jobs
  WHERE tenant_id = '${options.tenantId}'
    AND run_id = '${options.runId}'
    AND status = 'SUCCEEDED'
), linked_evidence AS (
  SELECT DISTINCT value AS evidence_id
  FROM selected,
       LATERAL jsonb_array_elements_text(
         COALESCE(projection_json->'source_evidence_ids', '[]'::jsonb)
       ) AS source(value)
  UNION
  SELECT result_json #>> '{learner,evidence_id}' AS evidence_id
  FROM selected
  WHERE result_json #>> '{learner,evidence_id}' IS NOT NULL
)
SELECT json_build_object(
  'projection_count', (SELECT count(*) FROM selected),
  'game_run_count', (
    SELECT count(*) FROM game_runs
    WHERE tenant_id = '${options.tenantId}' AND run_id = '${options.runId}'
  ),
  'linked_evidence_count', (SELECT count(*) FROM linked_evidence),
  'backed_evidence_count', (
    SELECT count(*) FROM game_evidence
    WHERE tenant_id = '${options.tenantId}'
      AND evidence_id IN (SELECT evidence_id FROM linked_evidence)
  ),
  'completed_at', (SELECT max(completed_at) FROM selected)
);`;
  const output = run('docker', [
    'exec',
    options.container,
    'psql',
    '-U',
    options.databaseUser,
    '-d',
    options.database,
    '-Atc',
    sql,
  ]);
  return JSON.parse(output);
}

function readOnlineProjection(options) {
  const sql = `SELECT
  (SELECT count(*) FROM daily_learning_record WHERE run_id = '${options.runId}') AS records,
  (SELECT count(*) FROM evidence_summary WHERE run_id = '${options.runId}') AS evidence,
  (SELECT max(data_time)::text FROM daily_learning_record WHERE run_id = '${options.runId}') AS record_data_time,
  (SELECT config_value FROM learning_center_config WHERE config_key = 'last_synced_at') AS last_synced_at`;
  const invocation = larkInvocation([
    'apps',
    '+db-execute',
    '--app-id',
    options.appId,
    '--environment',
    'online',
    '--sql',
    sql,
    '--yes',
    '--as',
    'user',
  ]);
  const output = run(invocation.command, invocation.args, {
    env: {
      ...process.env,
      LARKSUITE_CLI_NO_UPDATE_NOTIFIER: '1',
      LARKSUITE_CLI_NO_SKILLS_NOTIFIER: '1',
    },
  });
  const envelope = JSON.parse(output);
  if (envelope.ok !== true || !Array.isArray(envelope.data) || envelope.data.length !== 1) {
    throw new Error('Miaoda online query returned an unexpected response');
  }
  return envelope.data[0];
}

function requireEqual(actual, expected, message) {
  if (Number(actual) !== Number(expected)) {
    throw new Error(`${message}: expected ${expected}, received ${actual}`);
  }
}

function verify(authority, online) {
  requireEqual(authority.projection_count, 1, 'authoritative SUCCEEDED projection count');
  requireEqual(authority.game_run_count, 1, 'authoritative Game Run count');
  if (Number(authority.linked_evidence_count) < 1) {
    throw new Error('authoritative projection has no linked evidence');
  }
  requireEqual(
    authority.backed_evidence_count,
    authority.linked_evidence_count,
    'authoritative linked Evidence closure',
  );
  requireEqual(online.records, 1, 'Miaoda daily record count for run');
  requireEqual(
    online.evidence,
    authority.linked_evidence_count,
    'Miaoda redacted Evidence count for run',
  );

  const completedAt = Date.parse(authority.completed_at);
  const syncedAt = Date.parse(online.last_synced_at);
  if (!Number.isFinite(completedAt) || !Number.isFinite(syncedAt) || syncedAt < completedAt) {
    throw new Error('Miaoda last_synced_at predates the authoritative projection');
  }
}

function main() {
  const options = parseArgs(process.argv.slice(2));
  const authority = readAuthority(options);
  const online = readOnlineProjection(options);
  verify(authority, online);
  console.log(
    JSON.stringify(
      {
        ok: true,
        appId: options.appId,
        runId: options.runId,
        authority: {
          projectionCount: Number(authority.projection_count),
          gameRunCount: Number(authority.game_run_count),
          linkedEvidenceCount: Number(authority.linked_evidence_count),
          completedAt: authority.completed_at,
        },
        miaoda: {
          dailyRecordCount: Number(online.records),
          redactedEvidenceCount: Number(online.evidence),
          recordDataTime: online.record_data_time,
          lastSyncedAt: online.last_synced_at,
        },
      },
      null,
      2,
    ),
  );
}

try {
  main();
} catch (error) {
  console.error(
    `[verify-real-projection] ${error instanceof Error ? error.message : String(error)}`,
  );
  process.exit(1);
}
