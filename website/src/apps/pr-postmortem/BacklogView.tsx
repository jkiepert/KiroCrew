// Prevention backlog: recurring root-cause themes, plus the clustered asks and
// the handoff that turns an accepted one into a steering rule, issue or PR.
import { useState } from 'react'
import { ExternalLink, Inbox, RefreshCw } from 'lucide-react'
import { Badge, Btn, Card, EmptyState, StatCard } from '../../components/ui'
import SimpleSelect from '../../components/SimpleSelect'
import { safeHttpUrl } from '../../lib/safeUrl'
import { i18nT } from '../../i18n/t'
import type { BacklogResponse, Cluster, Target } from './api'
import { targetLabel } from './lib/format'

function Themes({ themes }: { themes: BacklogResponse['themes'] }) {
  if (!themes.length) return null
  return (
    <Card className="mb-3" data-testid="themes">
      <div className="text-[13px] font-semibold">
        {i18nT('apps.prPostmortem.backlogView.themesHeading')}
      </div>
      <div className="mb-2.5 text-[11px] text-muted">
        {i18nT('apps.prPostmortem.backlogView.themesBlurb')}
      </div>
      {themes.map(t => (
        <div
          key={t.root_cause_class}
          className="flex items-center gap-2 border-b border-border py-1.5 text-xs last:border-0"
        >
          <Badge variant="aim">{t.root_cause_class}</Badge>
          <span className={`w-24 shrink-0 ${t.count > 1 ? 'font-semibold' : ''}`}>
            {i18nT('apps.prPostmortem.backlogView.fixPrCount', { count: t.count })}
          </span>
          <span className="text-[11px] text-muted">{t.fix_prs.map(p => `#${p}`).join(' ')}</span>
          <div className="flex-1" />
          <span className="text-[11px] text-muted">
            {Object.entries(t.buckets)
              .map(([b, n]) => `${b}:${n}`)
              .join('  ')}
          </span>
        </div>
      ))}
    </Card>
  )
}

function ClusterCard({
  cluster,
  onApply,
  busy,
}: {
  cluster: Cluster
  onApply: (cluster: Cluster, target: Target) => void
  busy: boolean
}) {
  // Landing place is a real choice for a `rule`: a steering file is
  // version-controlled beside the code, a lesson is workspace-scoped.
  const [target, setTarget] = useState<Target | ''>('')
  const app = cluster.application
  const allowed = clusterTargets(cluster)
  const chosen = (target || allowed[0]) as Target

  return (
    <Card className="mb-2" data-testid={`cluster-${cluster.id}`}>
      <div className="mb-1.5 flex flex-wrap items-center gap-2">
        <Badge variant="aim">{cluster.bucket}</Badge>
        <span className="flex-1 text-xs font-semibold">{cluster.title}</span>
        {cluster.recurrence > 1 && (
          <span title={i18nT('apps.prPostmortem.backlogView.recurringHelp')}>
            <Badge variant="warn">
              {i18nT('apps.prPostmortem.backlogView.recurring', { count: cluster.recurrence })}
            </Badge>
          </span>
        )}
        {cluster.accepted > 0 && (
          <Badge variant="ok">
            {i18nT('apps.prPostmortem.backlogView.acceptedCount', { count: cluster.accepted })}
          </Badge>
        )}
        {app && <Badge variant={app.status === 'applied' ? 'ok' : 'warn'}>{app.status}</Badge>}
      </div>

      <div className="mb-2 text-[11px] text-muted">
        {i18nT('apps.prPostmortem.backlogView.fromPrs', {
          prs: cluster.fix_prs.map(p => `#${p}`).join(', '),
        })}
        {cluster.root_cause_classes.length ? ` · ${cluster.root_cause_classes.join(', ')}` : ''}
      </div>

      {app?.url && safeHttpUrl(app.url) && (
        // safeHttpUrl, not a bare href: `url` is written by the agent that carried
        // out the apply, so a `javascript:` value would execute on click. The same
        // guard sibling apps use for provider URLs. Found by review on PR #2354.
        <div className="mb-2 text-[11px]">
          <a href={safeHttpUrl(app.url) as string} target="_blank" rel="noreferrer" className="text-accent">
            {app.url} <ExternalLink className="inline h-3 w-3" />
          </a>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-1.5">
        {allowed.length > 1 && (
          // SimpleSelect, not a native <select>: an OS-drawn popup ignores the
          // dashboard theme (eslint no-restricted-syntax enforces this). It
          // renders a <button>, so the label lives in aria-label rather than a
          // <label htmlFor>.
          <SimpleSelect
            aria-label={i18nT('apps.prPostmortem.backlogView.targetPickerLabel')}
            options={allowed}
            optionLabels={allowed.map(t => targetLabel(t))}
            value={chosen}
            onChange={v => setTarget(v as Target)}
            disabled={busy || !cluster.applicable}
            className="h-7 text-[11px]"
          />
        )}
        <Btn
          primary={cluster.applicable}
          disabled={busy || !cluster.applicable}
          onClick={() => onApply(cluster, chosen)}
          title={
            cluster.applicable
              ? i18nT('apps.prPostmortem.backlogView.applyHelp')
              : i18nT('apps.prPostmortem.backlogView.needsAcceptHelp')
          }
          data-testid={`apply-${cluster.id}`}
        >
          {cluster.applicable
            ? i18nT('apps.prPostmortem.backlogView.applyAs', { target: targetLabel(chosen) })
            : i18nT('apps.prPostmortem.backlogView.needsAccept')}
        </Btn>
        <span className="text-[11px] text-muted">
          {i18nT('apps.prPostmortem.backlogView.decisionCounts', {
            undecided: cluster.undecided,
            total: cluster.members.length,
          })}
        </span>
      </div>
    </Card>
  )
}

/**
 * Targets the UI offers for a bucket.
 *
 * Kept in step with the backend's BUCKET_TARGETS: the server is authoritative and
 * refuses a mismatch with a 400, so this only decides what the picker shows.
 */
export function clusterTargets(cluster: Cluster): Target[] {
  switch (cluster.bucket) {
    case 'rule':
      return ['steering', 'lesson']
    case 'test':
      return ['issue']
    case 'gate':
      return ['pull_request', 'issue']
    default:
      return ['docs', 'steering']
  }
}

export function BacklogView({
  backlog,
  onApply,
  onRefresh,
  busy,
}: {
  backlog: BacklogResponse
  onApply: (cluster: Cluster, target: Target) => void
  onRefresh: () => void
  busy: boolean
}) {
  const totals = backlog.totals
  return (
    <>
      <Card className="mb-3">
        <div className="flex flex-wrap items-center gap-3">
          <StatCard
            label={i18nT('apps.prPostmortem.backlogView.statDistinct')}
            value={totals.clusters}
          />
          <StatCard
            label={i18nT('apps.prPostmortem.backlogView.statRecurring')}
            value={totals.recurring}
          />
          <StatCard
            label={i18nT('apps.prPostmortem.backlogView.statReady')}
            value={totals.applicable}
          />
          <StatCard
            label={i18nT('apps.prPostmortem.backlogView.statApplied')}
            value={totals.applied}
          />
          <div className="flex-1" />
          <Btn onClick={onRefresh} data-testid="backlog-refresh">
            <RefreshCw className="mr-1 inline h-3 w-3" />
            {i18nT('apps.prPostmortem.backlogView.refresh')}
          </Btn>
        </div>
      </Card>

      <Themes themes={backlog.themes} />

      {backlog.clusters.length ? (
        backlog.clusters.map(c => (
          <ClusterCard key={c.id} cluster={c} onApply={onApply} busy={busy} />
        ))
      ) : (
        <Card>
          <EmptyState
            icon={<Inbox className="h-5 w-5" />}
            title={i18nT('apps.prPostmortem.backlogView.emptyTitle')}
            subtitle={i18nT('apps.prPostmortem.backlogView.emptyBody')}
          />
        </Card>
      )}
    </>
  )
}
