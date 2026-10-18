// Raise undici's global headersTimeout so a slow agent turn is not destroyed by the
// evaluator's fetch. No-op unless TE_WEBBENCH_UNDICI_ABORT_MS is set.
import { createRequire } from 'node:module'
import { readdirSync, existsSync } from 'node:fs'
import { join } from 'node:path'
const ms = Number(process.env.TE_WEBBENCH_UNDICI_ABORT_MS || 0)
if (ms > 0) {
  const root = process.env.TE_WEBBENCH_REPO
  let mod = null
  try { mod = createRequire(process.cwd() + '/').resolve('undici') } catch {}
  if (!mod && root) {
    const store = join(root, 'common', 'temp', 'node_modules', '.pnpm')
    if (existsSync(store)) {
      const hit = readdirSync(store).filter(d => d.startsWith('undici@')).sort().pop()
      if (hit) {
        const p = join(store, hit, 'node_modules', 'undici', 'index.js')
        if (existsSync(p)) mod = p
      }
    }
  }
  if (!mod) { console.error('[te] WARNING: undici not found; evaluator keeps the 300s abort') }
  else {
    const { setGlobalDispatcher, Agent } = createRequire(import.meta.url)(mod)
    setGlobalDispatcher(new Agent({ headersTimeout: ms, bodyTimeout: ms }))
    console.error(`[te] undici global dispatcher raised: headersTimeout=${ms}ms`)
  }
}
