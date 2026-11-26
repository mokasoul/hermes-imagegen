#!/usr/bin/env node
/**
 * Gate for the result-board layout table.
 *
 * The desktop half is a single plain ESM file that the plugin loader reads
 * uncompiled — it cannot be imported by a test runner (its imports resolve only
 * inside the app). So this check extracts `planResultLayout` from the source by
 * brace matching and exercises that exact code with the upstream expectations.
 */

import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const source = readFileSync(join(here, '..', 'desktop', 'plugin.js'), 'utf8')

const marker = 'function planResultLayout(count) {'
const start = source.indexOf(marker)
if (start < 0) {
  console.error('planResultLayout not found in desktop/plugin.js')
  process.exit(1)
}

let depth = 0
let end = -1
for (let i = source.indexOf('{', start); i < source.length; i += 1) {
  const char = source[i]
  if (char === '{') depth += 1
  else if (char === '}') {
    depth -= 1
    if (depth === 0) {
      end = i + 1
      break
    }
  }
}
if (end < 0) {
  console.error('unbalanced planResultLayout body')
  process.exit(1)
}

const body = source.slice(start, end)
const planResultLayout = new Function(`${body}; return planResultLayout;`)()

const expected = {
  0: { rows: [], scroll: false },
  1: { rows: [[0]], scroll: false },
  2: { rows: [[0, 1]], scroll: false },
  3: { rows: [[0, 1], [2]], scroll: false },
  4: { rows: [[0, 1], [2, 3]], scroll: false },
  5: { rows: [[0, 1, 2], [3, 4]], scroll: false },
  6: { rows: [[0, 1, 2], [3, 4, 5]], scroll: false },
  7: { rows: [[0, 1, 2], [3, 4, 5], [6]], scroll: true },
  8: { rows: [[0, 1, 2], [3, 4, 5], [6, 7]], scroll: true }
}

let failures = 0
for (const [count, want] of Object.entries(expected)) {
  const got = planResultLayout(Number(count))
  const ok = JSON.stringify(got) === JSON.stringify(want)
  if (!ok) {
    failures += 1
    console.error(`count=${count}: expected ${JSON.stringify(want)} got ${JSON.stringify(got)}`)
  }
}

// Every card index must appear exactly once, for any count.
for (let count = 0; count <= 20; count += 1) {
  const seen = planResultLayout(count).rows.flat().sort((a, b) => a - b)
  const want = Array.from({ length: count }, (_, index) => index)
  if (JSON.stringify(seen) !== JSON.stringify(want)) {
    failures += 1
    console.error(`count=${count}: slots ${JSON.stringify(seen)} != ${JSON.stringify(want)}`)
  }
}

if (failures > 0) {
  console.error(`result layout: ${failures} failure(s)`)
  process.exit(1)
}
console.log('result layout ok: 0-20 cards, fixed sheet table')
