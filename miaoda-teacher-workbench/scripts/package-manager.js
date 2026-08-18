const fs = require('node:fs');
const path = require('node:path');

function getNpmCliPath() {
  const configuredPath = process.env.npm_execpath;
  if (configuredPath && fs.existsSync(configuredPath)) {
    return configuredPath;
  }

  const bundledPath = path.join(
    path.dirname(process.execPath),
    'node_modules',
    'npm',
    'bin',
    'npm-cli.js',
  );
  return fs.existsSync(bundledPath) ? bundledPath : null;
}

function packageManagerCommand(name, args = []) {
  if (name !== 'npm' && name !== 'npx') {
    throw new Error(`Unsupported package manager command: ${name}`);
  }

  const npmCliPath = getNpmCliPath();
  if (npmCliPath) {
    const cliPath =
      name === 'npm'
        ? npmCliPath
        : path.join(path.dirname(npmCliPath), 'npx-cli.js');
    if (fs.existsSync(cliPath)) {
      return {
        command: process.execPath,
        args: [cliPath, ...args],
      };
    }
  }

  return {
    command: process.platform === 'win32' ? `${name}.cmd` : name,
    args,
  };
}

module.exports = { packageManagerCommand };
