/**
 * PR Postmortem page tests.
 *
 * Fetch is stubbed at the boundary so the assertions are about what the page
 * renders and which endpoint each control calls -- the two things a reviewer
 * cannot check by reading the diff.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import PrPostmortemPage from '../PrPostmortemPage'
import { clusterTargets } from '../BacklogView'
import type { Cluster } from '../api'

const REPORT_SUMMARY = {
  fix_pr: 2196,
  fix_title: 'fix(packaging): guarantee the vendored libs reach every install',
  fix_url: 'https://github.com/o/n/pull/2196',
  fix_merged_at: '2026-08-08T10:00:00Z',
  culprit_pr: 1526,
  culprit_subject: 'fix(packaging): ship the vendored lib in the sdist',
  verdict: 'moderate' as const,
  confidence: 0.446,
  flags: ['low_signal'],
  link_verdict: 'confirmed' as const,
  root_cause_class: 'incomplete_prior_fix',
  analysis_present: true,
  proposal_buckets: { rule: 1 },
  proposals_total: 1,
  proposals_undecided: 1,
  human_link_decision: null,
}

const REPORT = {
  ...REPORT_SUMMARY,
  culprit_commits: ['abc123def456'],
  culprit_files_touched: 2,
  signal_weight: 33.6,
  link_reason: 'the culprit shipped the incomplete packaging fix',
  root_cause: 'the wheel was verified but the sdist it is built from was not',
  why_review_missed: 'reviewers saw a green wheel check',
  why_tests_missed: 'no test built the sdist',
  prompt_injection_observed: false,
  proposals: [
    {
      id: '2196:0',
      bucket: 'rule',
      title: 'A packaging fix must inspect the sdist too',
      text: 'Assert both artifacts when python -m build is used.',
      rationale: 'the culprit checked only the wheel',
      confidence: 'high',
      decision: null,
      decision_note: null,
      decided_at: null,
    },
  ],
  human_link_note: null,
  evidence: [
    {
      file: 'MANIFEST.in',
      kind: 'source',
      pre_image_lines: '12',
      weight: 1.0,
      culprit_sha: 'abc123def456',
      culprit_pr: 1526,
      author: 'someone',
      date: '2026-07-01T00:00:00Z',
      subject: 'fix(packaging): ship the vendored lib in the sdist',
    },
  ],
  notes: [],
}

const BACKLOG = {
  clusters: [
    {
      id: 'aabbccdd11',
      bucket: 'rule',
      title: 'A packaging fix must inspect the sdist too',
      recurrence: 2,
      accepted: 1,
      rejected: 0,
      undecided: 1,
      dismissed: false,
      root_cause_classes: ['incomplete_prior_fix'],
      fix_prs: [2196, 2194],
      members: [{ proposal_id: '2196:0', fix_pr: 2196, title: 'x' }],
      applicable: true,
      application: null,
    },
  ],
  themes: [
    {
      root_cause_class: 'ui_state_or_layout',
      count: 4,
      fix_prs: [2195, 2187, 2108, 1811],
      buckets: { gate: 3, rule: 4, test: 4 },
      sample_titles: [],
    },
  ],
  totals: { clusters: 1, applicable: 1, recurring: 1, dismissed: 0, applied: 0 },
}

const calls: { url: string; method: string }[] = []

function stubFetch() {
  return vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, method: init?.method || 'GET' })
    const body = (() => {
      if (url.endsWith('/reports')) {
        return { reports: [REPORT_SUMMARY], last_scan: { at: new Date().toISOString() }, repos: [] }
      }
      if (/\/reports\/\d+$/.test(url)) return REPORT
      if (url.endsWith('/backlog')) return BACKLOG
      if (url.includes('/apply-plan')) {
        return {
          cluster_id: 'aabbccdd11',
          bucket: 'rule',
          target: url.includes('target=lesson') ? 'lesson' : 'steering',
          allowed_targets: ['steering', 'lesson'],
          prompt: 'SECURITY ... <untrusted_proposal_data> ... </untrusted_proposal_data>',
          recurrence: 2,
          accepted: 1,
          steering_path: '.kiro/steering/postmortem/incomplete-prior-fix.md',
        }
      }
      return {}
    })()
    return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) } as Response
  })
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <PrPostmortemPage />
    </QueryClientProvider>,
  )
}

describe('PrPostmortemPage', () => {
  beforeEach(() => {
    calls.length = 0
    vi.stubGlobal('fetch', stubFetch())
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('lists a fix PR with its culprit and verdict', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByTestId('report-row-2196')).toBeTruthy())
    const row = screen.getByTestId('report-row-2196')
    expect(row.textContent).toContain('#2196')
    expect(row.textContent).toContain('1526')
    expect(row.textContent).toContain('moderate')
    expect(row.textContent).toContain('incomplete_prior_fix')
  })

  it('opens the detail view and shows the root cause and why review missed it', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByTestId('report-row-2196')).toBeTruthy())
    await userEvent.click(screen.getByTestId('report-row-2196'))
    await waitFor(() =>
      expect(screen.getByText(/the wheel was verified but the sdist/)).toBeTruthy(),
    )
    expect(screen.getByText(/reviewers saw a green wheel check/)).toBeTruthy()
  })

  it('keeps the blame evidence collapsed until asked', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByTestId('report-row-2196')).toBeTruthy())
    await userEvent.click(screen.getByTestId('report-row-2196'))
    await waitFor(() => expect(screen.getByTestId('toggle-evidence')).toBeTruthy())
    expect(screen.queryByTestId('evidence-table')).toBeNull()
    await userEvent.click(screen.getByTestId('toggle-evidence'))
    expect(screen.getByTestId('evidence-table').textContent).toContain('MANIFEST.in')
  })

  it('posts an accept to the proposal decision endpoint', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByTestId('report-row-2196')).toBeTruthy())
    await userEvent.click(screen.getByTestId('report-row-2196'))
    await waitFor(() => expect(screen.getByTestId('decide-accept-2196:0')).toBeTruthy())
    await userEvent.click(screen.getByTestId('decide-accept-2196:0'))
    await waitFor(() =>
      expect(
        calls.some(c => c.method === 'POST' && c.url.includes('/proposals/2196/0/decision')),
      ).toBe(true),
    )
  })

  it('posts a not-a-culprit ruling to the link endpoint', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByTestId('report-row-2196')).toBeTruthy())
    await userEvent.click(screen.getByTestId('report-row-2196'))
    await waitFor(() => expect(screen.getByTestId('rule-not-a-culprit')).toBeTruthy())
    await userEvent.click(screen.getByTestId('rule-not-a-culprit'))
    await waitFor(() =>
      expect(calls.some(c => c.method === 'POST' && c.url.includes('/reports/2196/link'))).toBe(
        true,
      ),
    )
  })

  it('shows recurring themes on the backlog tab', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByTestId('tab-backlog')).toBeTruthy())
    await userEvent.click(screen.getByTestId('tab-backlog'))
    await waitFor(() => expect(screen.getByTestId('themes')).toBeTruthy())
    const themes = screen.getByTestId('themes')
    expect(themes.textContent).toContain('ui_state_or_layout')
    expect(themes.textContent).toContain('4')
  })

  it('defaults a rule cluster to the steering target and can switch to a lesson', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByTestId('tab-backlog')).toBeTruthy())
    await userEvent.click(screen.getByTestId('tab-backlog'))
    // SimpleSelect renders a themed button, not a native <select>, so the current
    // value is read from the trigger's text rather than `.value`.
    const trigger = await waitFor(() =>
      screen.getByLabelText('Where this rule should land'),
    )
    expect(trigger.textContent).toContain('a steering rule')
    await userEvent.click(trigger)
    await userEvent.click(await waitFor(() => screen.getByText('a lesson')))
    await waitFor(() =>
      expect(screen.getByLabelText('Where this rule should land').textContent).toContain(
        'a lesson',
      ),
    )
  })

  it('requests the apply plan with the chosen target', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByTestId('tab-backlog')).toBeTruthy())
    await userEvent.click(screen.getByTestId('tab-backlog'))
    await waitFor(() => expect(screen.getByTestId('apply-aabbccdd11')).toBeTruthy())
    await userEvent.click(screen.getByTestId('apply-aabbccdd11'))
    await waitFor(() =>
      expect(calls.some(c => c.url.includes('/apply-plan?target=steering'))).toBe(true),
    )
    // The handoff goes to a background chat slot, and the request is recorded.
    await waitFor(() => expect(calls.some(c => c.url.startsWith('/api/chat'))).toBe(true))
    expect(calls.some(c => c.method === 'POST' && c.url.includes('/application'))).toBe(true)
  })

  it('surfaces a backend error instead of rendering an empty page', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: false,
        status: 403,
        json: async () => ({ error: 'pr-postmortem is disabled' }),
        text: async () => '{"error":"pr-postmortem is disabled"}',
      }) as Response),
    )
    renderPage()
    await waitFor(() => expect(screen.getByTestId('page-error')).toBeTruthy())
    expect(screen.getByTestId('page-error').textContent).toContain('disabled')
  })

  it('prefers the localized message for a coded error over the server prose', async () => {
    // The backend's `error` is English by contract, so a localized UI must key
    // off `code`. Asserting the server prose is NOT shown is the point: showing
    // it would look correct in English and be untranslatable everywhere else.
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: false,
        status: 403,
        json: async () => ({
          error: 'pr-postmortem is disabled',
          code: 'app_disabled',
        }),
        text: async () => '{"code":"app_disabled"}',
      }) as Response),
    )
    renderPage()
    await waitFor(() => expect(screen.getByTestId('page-error')).toBeTruthy())
    const shown = screen.getByTestId('page-error').textContent || ''
    expect(shown).toContain('Turn it on in Settings')
    expect(shown).not.toContain('pr-postmortem is disabled')
  })

  it('falls back to the server prose for a code it does not localize', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: false,
        status: 404,
        json: async () => ({ error: 'no such report', code: 'report_not_found' }),
        text: async () => '{"code":"report_not_found"}',
      }) as Response),
    )
    renderPage()
    await waitFor(() => expect(screen.getByTestId('page-error')).toBeTruthy())
    expect(screen.getByTestId('page-error').textContent).toContain('no such report')
  })
})

describe('clusterTargets', () => {
  const base: Cluster = {
    id: 'x',
    bucket: 'rule',
    title: 't',
    recurrence: 1,
    accepted: 0,
    rejected: 0,
    undecided: 1,
    dismissed: false,
    root_cause_classes: [],
    fix_prs: [1],
    members: [],
    applicable: false,
    application: null,
  }

  it('offers steering first for a rule', () => {
    expect(clusterTargets({ ...base, bucket: 'rule' })).toEqual(['steering', 'lesson'])
  })

  it('mirrors the backend BUCKET_TARGETS for every bucket', () => {
    // The server is authoritative and 400s a mismatch; this only decides what the
    // picker shows, so drift would surface as a confusing rejected click.
    expect(clusterTargets({ ...base, bucket: 'test' })).toEqual(['issue'])
    expect(clusterTargets({ ...base, bucket: 'gate' })).toEqual(['pull_request', 'issue'])
    expect(clusterTargets({ ...base, bucket: 'docs' })).toEqual(['docs', 'steering'])
  })
})
