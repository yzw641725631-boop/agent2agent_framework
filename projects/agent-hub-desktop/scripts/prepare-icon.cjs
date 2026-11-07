'use strict';

const { app, nativeImage } = require('electron');
const fs = require('node:fs/promises');
const path = require('node:path');

async function main() {
  await app.whenReady();
  const directory = path.resolve(__dirname, '..', 'assets');
  const image = nativeImage.createFromPath(path.join(directory, 'agent-hub-source.png'));
  if (image.isEmpty()) throw new Error('Cannot read source app icon');
  const png = image.resize({ width: 512, height: 512, quality: 'best' }).toPNG();
  await fs.writeFile(path.join(directory, 'agent-hub.png'), png);
  const sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256];
  const entries = sizes.map(size => image.resize({ width: size, height: size, quality: 'best' }).toPNG());
  const header = Buffer.alloc(6 + 16 * entries.length);
  header.writeUInt16LE(1, 2); header.writeUInt16LE(entries.length, 4);
  let offset = header.length;
  for (let index = 0; index < sizes.length; index++) {
    const position = 6 + 16 * index, size = sizes[index];
    header[position] = size === 256 ? 0 : size;
    header[position + 1] = size === 256 ? 0 : size;
    header.writeUInt16LE(1, position + 4); header.writeUInt16LE(32, position + 6);
    header.writeUInt32LE(entries[index].length, position + 8); header.writeUInt32LE(offset, position + 12);
    offset += entries[index].length;
  }
  await fs.writeFile(path.join(directory, 'agent-hub.ico'), Buffer.concat([header, ...entries]));
  process.stdout.write(JSON.stringify({ source: image.getSize(), png: 'assets/agent-hub.png',
    ico: 'assets/agent-hub.ico', sizes }) + '\n');
}
main().then(() => app.quit(), error => { process.stderr.write(error.message + '\n'); app.exit(1); });
