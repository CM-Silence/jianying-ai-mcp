import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { pathToFileURL } from 'node:url';

const cliPath = process.argv[2];
const packageRoot = dirname(dirname(cliPath));
const resultPrefix = 'JIANYING_MCP_OPENFILM_DRAFT ';

try {
  const info = JSON.parse(readFileSync(join(packageRoot, 'package.json'), 'utf8'));
  const request = JSON.parse(readFileSync(0, 'utf8'));
  if (info.name !== 'openfilm' || info.version !== request.version) {
    throw new Error(`Editable draft export requires OpenFilm npm package ${request.version}`);
  }
  const { createExports } = await import(pathToFileURL(join(packageRoot, 'studio', 'server', 'exports.mjs')));
  let complete;
  const result = new Promise((resolve) => { complete = resolve; });
  const exports = createExports({
    drafts: () => null,
    onChange(job) {
      if (['done', 'failed', 'cancelled'].includes(job.status)) complete(job);
    },
  });
  const job = exports.start(
    { id: request.id, name: request.name, path: request.project },
    { kind: 'nle', format: 'jianying', media: request.media, fps: request.fps, workers: 1 },
    { folder: request.destination },
  );
  const timer = setTimeout(() => exports.cancel(job.id), request.timeoutMs);
  try {
    const finished = await result;
    if (finished.status !== 'done') {
      throw new Error(finished.error ?? 'OpenFilm editable draft export timed out');
    }
    stdoutResult({ outputs: finished.outputs });
  } finally {
    clearTimeout(timer);
    await exports.stopAll();
  }
} catch (error) {
  process.stderr.write(`${error.message}\n`);
  if (/Executable doesn't exist/i.test(error.message)) {
    process.stderr.write('OpenFilm 0.4.1 editable draft export needs its matching Playwright Chromium headless shell.\n');
  }
  process.exitCode = 1;
}

function stdoutResult(value) {
  process.stdout.write(`${resultPrefix}${JSON.stringify(value)}\n`);
}
