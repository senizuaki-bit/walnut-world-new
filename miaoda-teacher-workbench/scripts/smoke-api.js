#!/usr/bin/env node
'use strict';

const DEFAULT_BASE_URL = 'http://127.0.0.1:3000';
const API_PREFIX = '/api/learning-insights';

function parseBaseUrl(argv) {
  if (argv.length === 0) {
    return DEFAULT_BASE_URL;
  }
  if (argv.length !== 2 || argv[0] !== '--base-url') {
    throw new Error('usage: npm run smoke:api -- --base-url http://127.0.0.1:3000');
  }
  const url = new URL(argv[1]);
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) {
    throw new Error('--base-url must be a credential-free HTTP(S) URL');
  }
  return url.toString().replace(/\/$/u, '');
}

function encodedWebUser(roles) {
  return encodeURIComponent(
    JSON.stringify({
      user_id: 'local-smoke-teacher',
      tenant_id: 'tenant_yaya',
      app_id: 'app_local_smoke',
      roles,
      env: 'preview',
    }),
  );
}

function csrfCookie(response) {
  const cookies = response.headers.getSetCookie();
  for (const cookie of cookies) {
    const match = /^suda-csrf-token=([^;]+)/u.exec(cookie);
    if (match) {
      return decodeURIComponent(match[1]);
    }
  }
  throw new Error('server did not issue a CSRF cookie');
}

async function requestJson(baseUrl, path, token, roles) {
  const response = await fetch(`${baseUrl}${path}`, {
    headers: {
      cookie: `suda-csrf-token=${encodeURIComponent(token)}`,
      'x-suda-csrf-token': token,
      'x-larkgw-suda-webuser': encodedWebUser(roles),
    },
  });
  let body = null;
  try {
    body = await response.json();
  } catch {
    // The caller reports the status and validates JSON only for successful APIs.
  }
  return { status: response.status, body };
}

async function main() {
  const baseUrl = parseBaseUrl(process.argv.slice(2));
  const bootstrap = await fetch(`${baseUrl}/`, { redirect: 'manual' });
  const token = csrfCookie(bootstrap);
  const [overview, students, records, forbidden] = await Promise.all([
    requestJson(baseUrl, `${API_PREFIX}/overview`, token, ['walnut_teacher']),
    requestJson(baseUrl, `${API_PREFIX}/students`, token, ['walnut_teacher']),
    requestJson(baseUrl, `${API_PREFIX}/records`, token, ['walnut_teacher']),
    requestJson(baseUrl, `${API_PREFIX}/overview`, token, ['walnut_student']),
  ]);

  for (const [name, result] of [
    ['overview', overview],
    ['students', students],
    ['records', records],
  ]) {
    if (result.status !== 200 || result.body === null) {
      throw new Error(`${name} returned HTTP ${result.status}`);
    }
  }
  if (forbidden.status !== 403) {
    throw new Error(`role guard returned HTTP ${forbidden.status}, expected 403`);
  }

  console.log(
    JSON.stringify(
      {
        ok: true,
        baseUrl,
        endpoints: {
          overview: overview.status,
          students: students.status,
          records: records.status,
          wrongRole: forbidden.status,
        },
        projection: {
          students: students.body.total,
          records: records.body.total,
          dataTime: overview.body.dataTime,
        },
      },
      null,
      2,
    ),
  );
}

main().catch((error) => {
  console.error(`[smoke-api] ${error instanceof Error ? error.message : String(error)}`);
  process.exit(1);
});
