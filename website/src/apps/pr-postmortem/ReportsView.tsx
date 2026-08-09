// Reports: the list of fix→culprit pairs, and one pair's detail with the blame
// evidence that produced its verdict.
import { useState } from 'react'
import { AlertTriangle, ArrowLeft, ExternalLink, RefreshCw } from 'lucide-react'
import { Badge, Btn, Card, EmptyState, StatCard } from '../../components/ui'
import { i18nT } from '../../i18n/t'
import type { Decision, LinkRuling, Report, ReportSummary, ReportsResponse } from './api'
import { culpritUrl, flagHelp, verdictVariant } from './lib/format'

function Flags({ flags }: { flags: string[] }) {
  if (!flags.length) return null
  return (
    <span className="inline-flex flex-wrap gap-1">
      {flags.map(f => (
        <span key={f} title={flagHelp(f)}>
          <Badge variant="muted">{f}</Badge>
        </span>
      ))}
    </span>
  )
}

function Row({ report, onOpen }: { report: ReportSummary; onOpen: (n: number) => void }) {
  const rejected = report.link_verdict === 'rejected'
  return (
    <button
      type="button"
      onClick={() => onOpen(report.fix_pr)}
      className={`flex w-full items-center gap-2 border-b border-border py-2 text-left last:border-0 hover:bg-accent-subtle/40 ${rejected ? 'opacity-60' : ''}`}
      data-testid={`report-row-${report.fix_pr}`}
    >
      <span className="w-14 shrink-0 text-xs text-muted">#{report.fix_pr}</span>
      <span className="min-w-0 flex-1 truncate text-xs" title={report.fix_title}>
        {report.fix_title || i18nT('apps.prPostmortem.reportsView.untitled')}
      </span>
      <span className="shrink-0 text-[11px] text-muted">
        {report.culprit_pr
          ? i18nT('apps.prPostmortem.reportsView.fromPr', { pr: report.culprit_pr })
          : i18nT('apps.prPostmortem.reportsView.fromNoPr')}
      </span>
      <Badge variant={verdictVariant(report.verdict)}>{report.verdict}</Badge>
      {report.root_cause_class ? (
        <Badge variant="aim">{report.root_cause_class}</Badge>
      ) : (
        <span className="text-[10px] text-muted">
          {report.analysis_present ? '' : i18nT('apps.prPostmortem.reportsView.notAnalysed')}
        </span>
      )}
      {rejected && <Badge variant="err">{i18nT('apps.prPostmortem.reportsView.linkRejected')}</Badge>}
      <span className="w-24 shrink-0 text-right text-[10px] text-muted">
        {report.proposals_total
          ? i18nT('apps.prPostmortem.reportsView.toDecide', {
              undecided: report.proposals_undecided,
              total: report.proposals_total,
            })
          : '—'}
      </span>
    </button>
  )
}

const FILTERS = ['actionable', 'undecided', 'all'] as const
type Filter = (typeof FILTERS)[number]

/** Filter -> its label key. Literal, never interpolated: src/i18n/dynamicKeys.test.ts. */
const FILTER_LABEL_KEY: Record<Filter, string> = {
  actionable: 'apps.prPostmortem.reportsView.filter_actionable',
  undecided: 'apps.prPostmortem.reportsView.filter_undecided',
  all: 'apps.prPostmortem.reportsView.filter_all',
}

export function ReportsList({
  data,
  onOpen,
  onRescan,
  rescanning,
}: {
  data: ReportsResponse
  onOpen: (n: number) => void
  onRescan: () => void
  rescanning: boolean
}) {
  const [filter, setFilter] = useState<Filter>('actionable')
  const reports = data.reports
  const shown = reports.filter(r => {
    if (filter === 'all') return true
    if (filter === 'undecided') return r.proposals_undecided > 0
    return r.verdict === 'strong' || r.verdict === 'moderate'
  })
  const strong = reports.filter(r => r.verdict === 'strong').length
  const moderate = reports.filter(r => r.verdict === 'moderate').length
  const undecided = reports.reduce((n, r) => n + r.proposals_undecided, 0)

  return (
    <>
      <div className="mb-3 flex flex-wrap items-center gap-3">
        <StatCard label={i18nT('apps.prPostmortem.reportsView.statFixPrs')} value={reports.length} />
        <StatCard label={i18nT('apps.prPostmortem.reportsView.statStrong')} value={strong} />
        <StatCard label={i18nT('apps.prPostmortem.reportsView.statModerate')} value={moderate} />
        <StatCard label={i18nT('apps.prPostmortem.reportsView.statToDecide')} value={undecided} />
        <div className="flex-1" />
        <Btn onClick={onRescan} disabled={rescanning} data-testid="rescan">
          <RefreshCw className="mr-1 inline h-3 w-3" />
          {rescanning
            ? i18nT('apps.prPostmortem.reportsView.scanning')
            : i18nT('apps.prPostmortem.reportsView.rescan')}
        </Btn>
      </div>

      <div className="mb-2 flex gap-1.5">
        {FILTERS.map(f => (
          <Btn key={f} primary={filter === f} onClick={() => setFilter(f)}>
            {i18nT(FILTER_LABEL_KEY[f])}
          </Btn>
        ))}
      </div>

      <Card>
        {shown.length ? (
          shown.map(r => <Row key={r.fix_pr} report={r} onOpen={onOpen} />)
        ) : (
          <EmptyState
            icon={<AlertTriangle className="h-5 w-5" />}
            title={i18nT('apps.prPostmortem.reportsView.emptyTitle')}
            subtitle={
              reports.length === 0
                ? i18nT('apps.prPostmortem.reportsView.emptyNoRepo')
                : i18nT('apps.prPostmortem.reportsView.emptyFiltered')
            }
          />
        )}
      </Card>
    </>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  if (!children) return null
  return (
    <div className="mb-2.5">
      <div className="mb-0.5 text-[10px] uppercase tracking-wide text-muted">{label}</div>
      <div className="text-xs leading-relaxed">{children}</div>
    </div>
  )
}

function Evidence({ rows }: { rows: Report['evidence'] }) {
  const [open, setOpen] = useState(false)
  if (!rows || !rows.length) return null
  return (
    <div className="mt-2">
      <Btn onClick={() => setOpen(!open)} data-testid="toggle-evidence">
        {open
          ? i18nT('apps.prPostmortem.reportsView.hideEvidence', { count: rows.length })
          : i18nT('apps.prPostmortem.reportsView.showEvidence', { count: rows.length })}
      </Btn>
      {open && (
        <div className="mt-2" data-testid="evidence-table">
          {rows.map((e, i) => (
            <div
              key={`${e.file}:${e.pre_image_lines}:${i}`}
              className="flex items-center gap-2 border-b border-border py-1 text-[11px] last:border-0"
            >
              <span className="min-w-0 flex-1 truncate font-mono" title={e.file}>
                {e.file}
              </span>
              <span className="w-16 shrink-0 text-muted">L{e.pre_image_lines}</span>
              <Badge variant="muted">{e.kind}</Badge>
              <span className="w-24 shrink-0 font-mono">{e.culprit_sha}</span>
              <span className="min-w-0 flex-1 truncate text-muted" title={e.subject}>
                {e.subject}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

/** Decision -> its button label key. Literal, never interpolated. */
const DECISION_LABEL_KEY: Record<Decision, string> = {
  accept: 'apps.prPostmortem.reportsView.decision_accept',
  reject: 'apps.prPostmortem.reportsView.decision_reject',
  defer: 'apps.prPostmortem.reportsView.decision_defer',
}

function ProposalCard({
  proposal,
  onDecide,
  busy,
}: {
  proposal: Report['proposals'][number]
  onDecide: (id: string, d: Decision) => void
  busy: boolean
}) {
  const decisions: Decision[] = ['accept', 'reject', 'defer']
  return (
    <Card className="mb-2" data-testid={`proposal-${proposal.id}`}>
      <div className="mb-1.5 flex items-center gap-2">
        <Badge variant="aim">{proposal.bucket}</Badge>
        <span className="flex-1 text-xs font-semibold">{proposal.title}</span>
        <Badge variant="muted">
          {i18nT('apps.prPostmortem.reportsView.confidence', { level: proposal.confidence })}
        </Badge>
        {proposal.decision && (
          <Badge variant={proposal.decision === 'accept' ? 'ok' : 'err'}>{proposal.decision}</Badge>
        )}
      </div>
      <div className="mb-1.5 text-xs leading-relaxed">{proposal.text}</div>
      <div className="mb-2 text-[11px] leading-relaxed text-muted">
        {i18nT('apps.prPostmortem.reportsView.wouldHaveCaught', { rationale: proposal.rationale })}
      </div>
      <div className="flex gap-1.5">
        {decisions.map(d => (
          <Btn
            key={d}
            primary={proposal.decision === d}
            disabled={busy}
            onClick={() => onDecide(proposal.id, d)}
            data-testid={`decide-${d}-${proposal.id}`}
          >
            {i18nT(DECISION_LABEL_KEY[d])}
          </Btn>
        ))}
      </div>
    </Card>
  )
}

export function ReportDetail({
  report,
  onBack,
  onDecide,
  onRule,
  onReattribute,
  busy,
}: {
  report: Report
  onBack: () => void
  onDecide: (id: string, d: Decision) => void
  onRule: (d: LinkRuling) => void
  onReattribute: () => void
  busy: boolean
}) {
  const rejected = report.link_verdict === 'rejected'
  const cUrl = culpritUrl(report.fix_url, report.culprit_pr)
  return (
    <>
      <div className="mb-2.5 flex items-center gap-2">
        <Btn onClick={onBack} data-testid="detail-back">
          <ArrowLeft className="mr-1 inline h-3 w-3" />
          {i18nT('apps.prPostmortem.reportsView.back')}
        </Btn>
        <span className="text-[13px] font-semibold">
          {i18nT('apps.prPostmortem.reportsView.fixHeading', { pr: report.fix_pr })}
        </span>
        <Badge variant={verdictVariant(report.verdict)}>{report.verdict}</Badge>
        <span className="text-[11px] text-muted">
          {i18nT('apps.prPostmortem.reportsView.confidenceValue', { value: report.confidence })}
        </span>
        <div className="flex-1" />
        <Btn onClick={onReattribute} disabled={busy} data-testid="reattribute">
          <RefreshCw className="mr-1 inline h-3 w-3" />
          {i18nT('apps.prPostmortem.reportsView.reattribute')}
        </Btn>
      </div>

      {report.prompt_injection_observed && (
        <Card className="mb-2.5 border-danger" data-testid="injection-warning">
          <div className="text-xs text-danger">
            {i18nT('apps.prPostmortem.reportsView.injectionWarning')}
          </div>
        </Card>
      )}

      <Card className="mb-2.5">
        <Field label={i18nT('apps.prPostmortem.reportsView.fixLabel')}>
          <a href={report.fix_url} target="_blank" rel="noreferrer" className="text-accent">
            #{report.fix_pr} <ExternalLink className="inline h-3 w-3" />
          </a>{' '}
          {report.fix_title}
        </Field>
        <Field label={i18nT('apps.prPostmortem.reportsView.culpritLabel')}>
          {report.culprit_pr ? (
            <>
              {cUrl ? (
                <a href={cUrl} target="_blank" rel="noreferrer" className="text-accent">
                  #{report.culprit_pr} <ExternalLink className="inline h-3 w-3" />
                </a>
              ) : (
                <span>#{report.culprit_pr}</span>
              )}{' '}
              {report.culprit_subject}
            </>
          ) : (
            <>
              <span className="font-mono">{report.culprit_commits[0] || '—'}</span>{' '}
              <span className="text-muted">
                {report.culprit_subject} {i18nT('apps.prPostmortem.reportsView.noAssociatedPr')}
              </span>
            </>
          )}
        </Field>
        <Field label={i18nT('apps.prPostmortem.reportsView.caveats')}>
          <Flags flags={report.flags} />
        </Field>
        <Field
          label={i18nT('apps.prPostmortem.reportsView.linkVerdict', {
            verdict: report.link_verdict || i18nT('apps.prPostmortem.reportsView.notAnalysed'),
          })}
        >
          {report.link_reason}
        </Field>
        <div className="mt-1 flex items-center gap-1.5">
          <span className="text-[11px] text-muted">
            {i18nT('apps.prPostmortem.reportsView.yourRuling')}
          </span>
          <Btn
            primary={report.human_link_decision === 'confirmed'}
            disabled={busy}
            onClick={() => onRule('confirmed')}
            data-testid="rule-confirmed"
          >
            {i18nT('apps.prPostmortem.reportsView.culpritIsRight')}
          </Btn>
          <Btn
            primary={report.human_link_decision === 'not_a_culprit'}
            disabled={busy}
            onClick={() => onRule('not_a_culprit')}
            data-testid="rule-not-a-culprit"
          >
            {i18nT('apps.prPostmortem.reportsView.notACulprit')}
          </Btn>
        </div>
        <Evidence rows={report.evidence} />
      </Card>

      {rejected ? (
        <Card>
          <EmptyState
            icon={<AlertTriangle className="h-5 w-5" />}
            title={i18nT('apps.prPostmortem.reportsView.linkRejectedTitle')}
            subtitle={report.link_reason || i18nT('apps.prPostmortem.reportsView.linkRejectedBody')}
          />
        </Card>
      ) : (
        <>
          <Card className="mb-2.5">
            {report.root_cause_class && (
              <div className="mb-2">
                <Badge variant="aim">{report.root_cause_class}</Badge>
              </div>
            )}
            <Field label={i18nT('apps.prPostmortem.reportsView.rootCause')}>{report.root_cause}</Field>
            <Field label={i18nT('apps.prPostmortem.reportsView.whyReviewMissed')}>
              {report.why_review_missed}
            </Field>
            <Field label={i18nT('apps.prPostmortem.reportsView.whyTestsMissed')}>
              {report.why_tests_missed}
            </Field>
          </Card>

          <div className="my-3 text-[13px] font-semibold">
            {i18nT('apps.prPostmortem.reportsView.proposalsHeading', {
              count: report.proposals.length,
            })}
          </div>
          {report.proposals.length ? (
            report.proposals.map(p => (
              <ProposalCard key={p.id} proposal={p} onDecide={onDecide} busy={busy} />
            ))
          ) : (
            <Card>
              <EmptyState
                icon={<AlertTriangle className="h-5 w-5" />}
                title={i18nT('apps.prPostmortem.reportsView.noAnalysisTitle')}
                subtitle={i18nT('apps.prPostmortem.reportsView.noAnalysisBody')}
              />
            </Card>
          )}
        </>
      )}
    </>
  )
}

