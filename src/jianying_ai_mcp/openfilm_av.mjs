import { pathToFileURL } from 'node:url';
import { parseArgs } from 'node:util';
import { createHash } from 'node:crypto';
import { dirname, join } from 'node:path';

async function main() {
  const [cli, action, ...args] = process.argv.slice(2);
  if (!cli || !['look', 'render'].includes(action)) {
    throw new Error('OpenFilm requires an installed CLI path and look or render.');
  }

  const { values, positionals } = parseArgs({
    args,
    allowPositionals: true,
    options: { out: { type: 'string' }, full: { type: 'boolean' }, loudness: { type: 'string' }, fps: { type: 'string' },
      'preview-renderer': { type: 'boolean' } },
  });
  const opts = {
    out: values.out,
    full: values.full,
    loudness: values.loudness === undefined ? undefined : Number(values.loudness),
    fps: values.fps === undefined ? undefined : Number(values.fps),
  };
  if (action === 'render') {
    // Use the same master-quality software encoder and drawing order on every host.
    opts.fine = true;
    opts.hardware = false;
    opts.workers = 1;
    if (values['preview-renderer']) {
      // Draw the same stage and original media as a point preview, including HTML overlays and subtitles.
      opts.straight = false;
      opts.seekable = false;
    }
    if (opts.out) {
      const settings = [Boolean(values['preview-renderer']), opts.fps ?? 30, positionals,
        process.env.JIANYING_OPENFILM_BROWSER_CHANNEL ?? ''];
      const key = createHash('sha256').update(JSON.stringify(settings)).digest('hex').slice(0, 16);
      // The runtime validates source signatures and isolates concurrent writers before reusing finished chunks.
      opts.resumeAs = join(dirname(opts.out), `jianying-resume-v1-${key}.mp4`);
    }
  }
  const channel = process.env.JIANYING_OPENFILM_BROWSER_CHANNEL?.trim();
  const modulePath = new URL(`../src/${action}.mjs`, pathToFileURL(cli));
  // Call the installed local module without the CLI's Studio and browser-install bootstrap.
  const run = (await import(modulePath))[action];
  let browsers;
  if (channel && action === 'look') {
    opts.launch = { channel };
  } else if (channel) {
    const { launch } = await import(new URL('./host.mjs', modulePath));
    const { BrowserPool } = await import(new URL('./browser-pool.mjs', modulePath));
    browsers = new BrowserPool({ start: () => launch({ channel }) });
    opts.browsers = browsers;
  }
  try {
    const result = await run(positionals, opts);
    for (const line of result.lines) console.log(line);
    if (result.ok === false) process.exitCode = 1;
  } finally {
    await browsers?.close();
  }
}

try {
  await main();
} catch (error) {
  const message = error instanceof Error ? error.message : String(error);
  console.error(message);
  if (/Executable doesn't exist|distribution .* is not found/i.test(message)) {
    console.error(process.env.JIANYING_OPENFILM_BROWSER_CHANNEL
      ? 'Install the configured OpenFilm browser channel or choose one installed on this computer.'
      : 'Install the Playwright Chromium headless shell or set JIANYING_OPENFILM_BROWSER_CHANNEL=chrome for installed Chrome.');
  }
  process.exitCode = 1;
}
