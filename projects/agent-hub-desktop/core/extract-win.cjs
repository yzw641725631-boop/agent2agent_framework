'use strict';

const { execFile } = require('node:child_process');
const path = require('node:path');
const { promisify } = require('node:util');
const { maxExpandedBytes } = require('./update-integrity.cjs');

const powershell = () => path.win32.join(process.env.SystemRoot || 'C:\\Windows',
  'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe');
const encodedCommand = script => Buffer.from(script, 'utf16le').toString('base64');

// Extract only ordinary files into a fresh directory. Treat archive names as data.
const extractScript = String.raw`
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression.FileSystem
$base = [IO.Path]::GetFullPath($env:AGENT_HUB_EXTRACT_TO).TrimEnd('\') + '\'
$prefix = $env:AGENT_HUB_ARCHIVE_ROOT + '/'
$archive = [IO.Compression.ZipFile]::OpenRead($env:AGENT_HUB_ARCHIVE_FILE)
try {
  if ($archive.Entries.Count -gt 4097) { throw 'Too many entries' }
  $paths = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
  $buffer = New-Object byte[] 65536
  $total = 0L
  foreach ($entry in $archive.Entries) {
    $name = $entry.FullName.Replace('\', '/')
    if (!$name.StartsWith($prefix, [StringComparison]::Ordinal)) { throw 'Wrong archive root' }
    $relative = $name.Substring($prefix.Length).TrimEnd('/')
    if ($name -eq $prefix) { continue }
    if (!$relative -or !$paths.Add($relative)) { throw 'Duplicate or empty archive path' }
    foreach ($part in $relative.Split('/')) {
      if (!$part -or $part -match '[<>:"|?*\x00-\x1f]|[. ]$' -or
          $part -match '^(CON|PRN|AUX|NUL|COM[0-9¹²³]|LPT[0-9¹²³])(\.|$)') { throw 'Invalid archive path' }
    }
    $target = [IO.Path]::GetFullPath([IO.Path]::Combine($base, $relative.Replace('/', '\')))
    if (!$target.StartsWith($base, [StringComparison]::OrdinalIgnoreCase)) { throw 'Archive path escapes root' }
    if ($name.EndsWith('/')) { [void][IO.Directory]::CreateDirectory($target); continue }
    $total += $entry.Length
    if ($total -gt [long]$env:AGENT_HUB_EXTRACT_LIMIT) { throw 'Archive expands past limit' }
    [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($target))
    $inputFile = $entry.Open()
    try {
      $outputFile = [IO.File]::Open($target, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write)
      try {
        $written = 0L
        while (($count = $inputFile.Read($buffer, 0, $buffer.Length)) -gt 0) {
          $written += $count
          if ($written -gt $entry.Length) { throw 'Invalid archive entry length' }
          $outputFile.Write($buffer, 0, $count)
        }
        if ($written -ne $entry.Length) { throw 'Incomplete archive entry' }
      } finally { $outputFile.Dispose() }
    } finally { $inputFile.Dispose() }
  }
} finally { $archive.Dispose() }
`;

async function extractWindowsZip(file, destination, rootName, signal) {
  await promisify(execFile)(powershell(), ['-NoProfile', '-NonInteractive', '-EncodedCommand',
    encodedCommand(extractScript)], {
    windowsHide: true, signal, timeout: 120000, maxBuffer: 64 * 1024,
    env: { ...process.env, AGENT_HUB_ARCHIVE_FILE: file, AGENT_HUB_ARCHIVE_ROOT: rootName,
      AGENT_HUB_EXTRACT_TO: destination, AGENT_HUB_EXTRACT_LIMIT: String(maxExpandedBytes) },
  });
}

module.exports = { extractWindowsZip, powershell, encodedCommand };
