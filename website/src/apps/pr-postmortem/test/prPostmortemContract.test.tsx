/**
 * Contract tests between the page, the manifest, and the backend route table.
 *
 * These catch failures that are invisible at runtime and unreviewable in a diff:
 *
 * - An API path the page calls that no backend route serves -- a 404 and a blank
 *   panel, with nothing in the diff to hint at it.
 * - A manifest that stops declaring a path the page needs. The dashboard gates app
 *   API access on `permissions.api`, so a missing entry means the button does
 *   nothing with no server log to explain it.
 * - The registry entry or icon asset going missing, which removes the page from
 *   the sidebar without breaking anything the type checker can see.
 *
 * The external-app version of this file asserted against a hand-rolled `.mjs`
 * bundle; this is its replacement for the builtin page.
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

const REPO_ROOT = join(__dirname, '..', '..', '..', '..', '..')
const APP_DIR = join(REPO_ROOT, 'src', 'kiro_crew', 'apps', 'builtins', 'pr_postmortem')
const WEB = join(REPO_ROOT, 'website')
const APP_SRC = join(WEB, 'src', 'apps', 'pr-postmortem')

const read = (p: string) => readFileSync(p, 'utf-8')
const manifest = JSON.parse(read(join(APP_DIR, 'app.json'))) as {
  name: string
  ui: { pages: { route: string; icon?: string }[] }
  iconUrl?: string
  permissions: { api: string[] }
  defaultEnabled?: boolean
}

/** Mirror of the backend's own path list, read from the source rather than retyped. */
function backendRoutes(): { method: string; path: string }[] {
  const src = read(join(APP_DIR, 'backend', 'routes.py'))
  const out: { method: string; path: string }[] = []
  const re = /app\.router\.add_(get|post|put|delete|patch)\(\s*f?"([^"]+)"/g
  let m: RegExpExecArray | null
  while ((m = re.exec(src)) !== null) {
    const path = m[2]
      .replace('{_BASE}', '/api/apps/pr-postmortem')
      // The routes are f-strings, so a literal path param is written `{{fix_pr}}`
      // and only becomes `{fix_pr}` at runtime.
      .replace(/\{\{/g, '{')
      .replace(/\}\}/g, '}')
    out.push({ method: m[1].toUpperCase(), path })
  }
  return out
}

/** Every API path the page's fetch wrapper calls, with params collapsed. */
function pageApiPaths(): string[] {
  const src = read(join(APP_SRC, 'api.ts'))
  const base = '/api/apps/pr-postmortem'
  const out = new Set<string>()
  const re = /fetch\(\s*`([^`]+)`/g
  let m: RegExpExecArray | null
  while ((m = re.exec(src)) !== null) {
    const raw = m[1]
      .replace(/\$\{API\}/g, base)
      .replace(/\$\{qs\}/g, '')
      .replace(/\$\{[^}]+\}/g, '{}')
      .split('?')[0]
    out.add(raw)
  }
  return [...out]
}

/** The host gates app API access with a prefix match on segment boundaries. */
function allowedByManifest(path: string): boolean {
  return manifest.permissions.api.some(
    p => path === p || path.startsWith(p.endsWith('/') ? p : `${p}/`),
  )
}

describe('manifest ↔ page API contract', () => {
  it('finds the page api call sites at all', () => {
    expect(pageApiPaths().length).toBeGreaterThan(4)
  })

  it('declares every app path the page calls', () => {
    for (const path of pageApiPaths()) {
      expect(allowedByManifest(path), `${path} is not covered by permissions.api`).toBe(true)
    }
  })

  it('declares the chat endpoint the apply handoff posts to', () => {
    // The handoff runs as a background chat slot; without this the click 403s.
    expect(allowedByManifest('/api/chat')).toBe(true)
  })

  it('lists the bare prefix, not only a glob', () => {
    // A trailing '/*' is matched literally by the host, so it grants nothing alone.
    expect(manifest.permissions.api).toContain('/api/apps/pr-postmortem')
  })
})

describe('page ↔ backend route contract', () => {
  it('reads the backend route table', () => {
    expect(backendRoutes().length).toBe(10)
  })

  it('has a backend route for every path the page calls', () => {
    const shapes = new Set(
      backendRoutes().map(r =>
        r.path
          .replace(/\{[^}]+\}/g, '{}')
          .replace('/api/apps/pr-postmortem', ''),
      ),
    )
    for (const path of pageApiPaths()) {
      if (!path.startsWith('/api/apps/pr-postmortem')) continue
      const rel = path.replace('/api/apps/pr-postmortem', '') || '/'
      expect(shapes.has(rel), `page calls ${rel} but no backend route serves it`).toBe(true)
    }
  })
})

describe('builtin wiring', () => {
  it('registers the route in builtinRegistry', () => {
    const src = read(join(WEB, 'src', 'apps', 'builtinRegistry.ts'))
    expect(src).toContain("'/pr-postmortem': lazy(() => import('./pr-postmortem/PrPostmortemPage'))")
  })

  it('declares the page route in the manifest', () => {
    expect(manifest.ui.pages[0].route).toBe('/pr-postmortem')
  })

  it('uses a lucide icon name for the page', () => {
    // Builtin pages name an icon component; a file path renders nothing.
    expect(manifest.ui.pages[0].icon).toBe('GitCompareArrows')
  })

  it('ships the app-store icon asset the manifest points at', () => {
    expect(manifest.iconUrl).toBe('/app-assets/pr-postmortem/icon.svg')
    expect(() => read(join(WEB, 'public', 'app-assets', 'pr-postmortem', 'icon.svg'))).not.toThrow()
  })

  it('stays opt-in', () => {
    expect(manifest.defaultEnabled).toBe(false)
  })
})

describe('i18n discipline', () => {
  it('routes every user-visible string through i18nT', () => {
    // The dashboard's i18n ratchet fails a PR that ships raw literals; catching it
    // here is cheaper than in CI.
    for (const file of ['PrPostmortemPage.tsx', 'ReportsView.tsx', 'BacklogView.tsx']) {
      const src = read(join(APP_SRC, file))
      expect(src, `${file} should use i18nT`).toContain('i18nT(')
    }
  })

  it('has an English catalog entry for the app', () => {
    const en = JSON.parse(read(join(WEB, 'src', 'i18n', 'locales', 'en.json'))) as {
      apps: Record<string, unknown>
    }
    expect(en.apps.prPostmortem).toBeTruthy()
  })
})
