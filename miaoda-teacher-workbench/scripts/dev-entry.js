#!/usr/bin/env node
'use strict';

if (process.env.SANDBOX_ID) {
  require('./dev.js');
} else {
  require('./dev-local.js');
}
