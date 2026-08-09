// Thin fetch wrapper for the PR Postmortem backend, registered directly on the
// gateway's aiohttp Application (see backend/routes.py:register_routes), so the
// base path is /api/apps/pr-postmortem — the same convention as issue-radar and
// code-review-sage, not the /apps/{name}/api child-process proxy.
import { i18nT } from '../../i18n/t'

const API = '/api/apps/pr-postmortem'

interface ApiError {
  error?: string
  code?: string
}

/**
 * Backend error code -> a localized message key.
 *
 * Only the codes a user can actually reach are listed. The rest -- malformed ids,
 * not-found records, invalid enum values -- are stale-state or programming errors
 * the UI cannot produce by itself, so they fall through to the server's English
 * prose rather than earning a translated sentence nobody will read.
 *
 * `reattribute_rejected` is deliberately NOT here. The engine returns it for three
 * different conditions -- no repo configured, the configured `repo_path` missing,
 * and no culprit it could name -- and each carries the specific detail the user
 * needs (which path, for instance). One localized sentence would have to assert
 * one of the three and would misdescribe the other two, so the server's own prose
 * is the honest thing to show.
 *
 * A literal map, never an interpolated key: src/i18n/dynamicKeys.test.ts.
 */
const ERROR_MESSAGE_KEY: Record<string, string> = {
  app_disabled: 'apps.prPostmortem.errors.app_disabled',
  unauthorized: 'apps.prPostmortem.errors.unauthorized',
  needs_accepted_proposal: 'apps.prPostmortem.errors.needs_accepted_proposal',
  target_not_allowed: 'apps.prPostmortem.errors.target_not_allowed',
  reattribute_failed: 'apps.prPostmortem.errors.reattribute_failed',
}

async function parseErrorBody(r: Response): Promise<string> {
  try {
    const body = (await r.json()) as ApiError
    // Prefer the code: the backend's `error` is English prose by contract
    // (RFC 9457 3.1.3 -- advisory), so rendering it verbatim would put an
    // untranslatable sentence into a localized UI.
    const key = body.code ? ERROR_MESSAGE_KEY[body.code] : undefined
    if (key) return i18nT(key)
    return body.error || i18nT('apps.prPostmortem.errors.http', { status: r.status })
  } catch {
    return i18nT('apps.prPostmortem.errors.http', { status: r.status })
  }
}

/** How much the attribution is trusted. `weak` means blame is not actionable. */
export type Verdict = 'strong' | 'moderate' | 'weak' | 'none'

/** Whether the analyst believed the blame link after reading both diffs. */
export type LinkVerdict = 'confirmed' | 'rejected' | 'uncertain'

export type Decision = 'accept' | 'reject' | 'defer'

export type LinkRuling = 'confirmed' | 'not_a_culprit'

/** Where an accepted proposal lands. A `rule` defaults to a steering file. */
export type Target = 'steering' | 'lesson' | 'issue' | 'pull_request' | 'docs'

/** One blamed line range — the reviewable unit behind a verdict. */
export interface EvidenceRow {
  file: string
  kind: string
  pre_image_lines: string
  weight: number
  culprit_sha: string
  culprit_pr: number | null
  author: string
  date: string
  subject: string
}

export interface Proposal {
  id: string
  bucket: string
  title: string
  text: string
  rationale: string
  confidence: string
  decision: Decision | null
  decision_note: string | null
  decided_at: string | null
}

export interface ReportSummary {
  fix_pr: number
  fix_title: string
  fix_url: string
  fix_merged_at: string
  culprit_pr: number | null
  culprit_subject: string
  verdict: Verdict
  confidence: number
  flags: string[]
  link_verdict: LinkVerdict | null
  root_cause_class: string | null
  analysis_present: boolean
  proposal_buckets: Record<string, number>
  proposals_total: number
  proposals_undecided: number
  human_link_decision: LinkRuling | null
}

export interface Report extends ReportSummary {
  culprit_commits: string[]
  culprit_files_touched: number | null
  signal_weight: number
  link_reason: string | null
  root_cause: string | null
  why_review_missed: string | null
  why_tests_missed: string | null
  /** The analyst saw instruction-like text in the PR content it was given. */
  prompt_injection_observed: boolean
  proposals: Proposal[]
  human_link_note: string | null
  evidence?: EvidenceRow[]
  notes?: string[]
}

export interface ScanState {
  at: string
  repo?: string
  scanned?: number
  errors?: number
}

export interface ReportsResponse {
  /** Agent instruction for the rescan handoff. Server-owned: it carries a
   *  security frame that must not depend on a translation. */
  scan_prompt?: string
  reports: ReportSummary[]
  last_scan: ScanState | null
  repos: { repo: string; repo_path: string; branch: string }[]
}

export interface Application {
  status: 'requested' | 'applied' | 'failed'
  target: string
  note: string
  url: string
  at: string
}

export interface Cluster {
  id: string
  bucket: string
  title: string
  /** Distinct fix PRs that produced this ask — the systemic-gap signal. */
  recurrence: number
  accepted: number
  rejected: number
  undecided: number
  dismissed: boolean
  root_cause_classes: string[]
  fix_prs: number[]
  members: { proposal_id: string; fix_pr: number; title: string }[]
  /** False until a human accepts a member; the apply route enforces this too. */
  applicable: boolean
  application: Application | null
}

export interface Theme {
  root_cause_class: string
  count: number
  fix_prs: number[]
  buckets: Record<string, number>
  sample_titles: string[]
}

export interface BacklogResponse {
  clusters: Cluster[]
  themes: Theme[]
  totals: {
    clusters: number
    applicable: number
    recurring: number
    dismissed: number
    applied: number
  }
}

export interface ApplyPlan {
  cluster_id: string
  bucket: string
  target: Target
  allowed_targets: Target[]
  prompt: string
  recurrence: number
  accepted: number
  /** Present only when the target is `steering`. */
  steering_path?: string
}

export interface ReattributeResult {
  fix_pr: number
  verdict: Verdict
  culprit_pr: number | null
  culprit_changed: boolean
  /** The stored analysis reasoned about a different PR and is no longer valid. */
  analysis_stale: boolean
}

export const prPostmortemApi = {
  async reports(): Promise<ReportsResponse> {
    const r = await fetch(`${API}/reports`)
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return (await r.json()) as ReportsResponse
  },

  async report(fixPr: number): Promise<Report> {
    const r = await fetch(`${API}/reports/${fixPr}`)
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return (await r.json()) as Report
  },

  async backlog(): Promise<BacklogResponse> {
    const r = await fetch(`${API}/backlog`)
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return (await r.json()) as BacklogResponse
  },

  async applyPlan(clusterId: string, target?: Target): Promise<ApplyPlan> {
    const qs = target ? `?target=${encodeURIComponent(target)}` : ''
    const r = await fetch(`${API}/backlog/${clusterId}/apply-plan${qs}`)
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return (await r.json()) as ApplyPlan
  },

  async recordApplication(
    clusterId: string,
    body: { status: string; target?: string; note?: string; url?: string },
  ): Promise<Application> {
    const r = await fetch(`${API}/backlog/${clusterId}/application`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return (await r.json()) as Application
  },

  async decide(proposalId: string, decision: Decision, note = ''): Promise<void> {
    const [fixPr, index] = proposalId.split(':')
    const r = await fetch(`${API}/proposals/${fixPr}/${index}/decision`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ decision, note }),
    })
    if (!r.ok) throw new Error(await parseErrorBody(r))
  },

  async ruleLink(fixPr: number, decision: LinkRuling, note = ''): Promise<void> {
    const r = await fetch(`${API}/reports/${fixPr}/link`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ decision, note }),
    })
    if (!r.ok) throw new Error(await parseErrorBody(r))
  },

  async reattribute(fixPr: number): Promise<ReattributeResult> {
    const r = await fetch(`${API}/reports/${fixPr}/reattribute`, { method: 'POST' })
    if (!r.ok) throw new Error(await parseErrorBody(r))
    return (await r.json()) as ReattributeResult
  },
}
