// PR Postmortem — links each merged fix PR to the PR that introduced the bug and
// turns the pattern into prevention.
//
// The page is a thin shell: two react-query reads, a tab switch, and the handoff
// that hands an accepted proposal to an agent. Every write is a human decision;
// nothing here applies a change to a repository by itself.
import { useCallback, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { GitCompareArrows } from 'lucide-react'
import { Btn, Card, ContentSkeleton, PageHeader } from '../../components/ui'
import { i18nT } from '../../i18n/t'
import { prPostmortemApi, type Cluster, type Decision, type LinkRuling, type Target } from './api'
import { relTime } from './lib/format'
import { ReportDetail, ReportsList } from './ReportsView'
import { BacklogView } from './BacklogView'

const POLL_MS = 30_000

export default function PrPostmortemPage() {
  const qc = useQueryClient()
  const [tab, setTab] = useState<'reports' | 'backlog'>('reports')
  const [selected, setSelected] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)
  const [rescanning, setRescanning] = useState(false)
  const [error, setError] = useState('')

  const reportsQuery = useQuery({
    queryKey: ['pr-postmortem', 'reports'],
    queryFn: () => prPostmortemApi.reports(),
    refetchInterval: POLL_MS,
  })

  const backlogQuery = useQuery({
    queryKey: ['pr-postmortem', 'backlog'],
    queryFn: () => prPostmortemApi.backlog(),
  })

  const detailQuery = useQuery({
    queryKey: ['pr-postmortem', 'report', selected],
    queryFn: () => prPostmortemApi.report(selected as number),
    enabled: selected !== null,
  })

  const invalidate = useCallback(() => {
    void qc.invalidateQueries({ queryKey: ['pr-postmortem'] })
  }, [qc])

  const guard = useCallback(
    async (fn: () => Promise<void>) => {
      setBusy(true)
      setError('')
      try {
        await fn()
        invalidate()
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e))
      } finally {
        setBusy(false)
      }
    },
    [invalidate],
  )

  const decide = useCallback(
    (proposalId: string, decision: Decision) =>
      guard(() => prPostmortemApi.decide(proposalId, decision)),
    [guard],
  )

  const rule = useCallback(
    (decision: LinkRuling) =>
      guard(async () => {
        if (selected !== null) await prPostmortemApi.ruleLink(selected, decision)
      }),
    [guard, selected],
  )

  const reattribute = useCallback(
    () =>
      guard(async () => {
        if (selected === null) return
        const res = await prPostmortemApi.reattribute(selected)
        if (res.analysis_stale) {
          // A changed culprit invalidates the stored analysis: it reasoned about a
          // different PR, so say so rather than showing a stale verdict as current.
          setError(
            i18nT('apps.prPostmortem.prPostmortemPage.analysisStale', { pr: res.culprit_pr ?? 0 }),
          )
        }
      }),
    [guard, selected],
  )

  // Applying is a handoff, never a silent write: the backend refuses a plan until
  // a human has accepted a member, and an agent performs the change.
  const apply = useCallback(
    (cluster: Cluster, target: Target) =>
      guard(async () => {
        const plan = await prPostmortemApi.applyPlan(cluster.id, target)
        const dispatched = await fetch('/api/chat?ws=1', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            slot: `pr-postmortem-apply-${cluster.id}`,
            message: `${plan.prompt}\n\nWhen you are done, record the outcome by POSTing to /api/apps/pr-postmortem/backlog/${cluster.id}/application with {"status":"applied","target":"${plan.target}","url":"<the issue or PR url, if any>"}. If you could not do it, POST status "failed" with a note explaining why.`,
          }),
        })
        // `fetch` resolves on a 4xx, so an unchecked call recorded the apply as
        // "requested" when no agent had received the plan -- the backlog would then
        // show work in flight that never started. Found by review on PR #2354.
        if (!dispatched.ok) {
          throw new Error(
            i18nT('apps.prPostmortem.errors.handoff_failed', {
              status: dispatched.status,
            }),
          )
        }
        await prPostmortemApi.recordApplication(cluster.id, {
          status: 'requested',
          target: plan.target,
        })
      }),
    [guard],
  )

  const rescan = useCallback(async () => {
    // The prompt is served by the backend so its security frame lives in one
    // reviewable place and never depends on a translation. If it is absent (an
    // older cached response, or the app disabled mid-session) refuse rather than
    // dispatching an agent with an empty instruction.
    const prompt = reportsQuery.data?.scan_prompt
    if (!prompt) {
      setError(i18nT('apps.prPostmortem.prPostmortemPage.scanFailed'))
      return
    }
    setRescanning(true)
    try {
      const dispatched = await fetch('/api/chat?ws=1', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          slot: 'pr-postmortem-scan',
          message: prompt,
        }),
      })
      // Same unchecked-fetch shape as the apply handoff: a 4xx resolves, so
      // without this the button reported "Scanning…" for eight seconds while
      // nothing had been dispatched.
      if (!dispatched.ok) {
        setError(i18nT('apps.prPostmortem.prPostmortemPage.scanFailed'))
        setRescanning(false)
        return
      }
    } catch {
      setError(i18nT('apps.prPostmortem.prPostmortemPage.scanFailed'))
    } finally {
      setTimeout(() => setRescanning(false), 8000)
    }
  }, [])

  const reportsError = reportsQuery.error
  const shownError =
    error || (reportsError instanceof Error ? reportsError.message : reportsError ? String(reportsError) : '')

  return (
    <div className="mx-auto max-w-[1200px] p-4">
      <PageHeader
        title={
          <span className="flex items-center gap-2">
            <GitCompareArrows className="h-5 w-5 text-accent" />
            {i18nT('apps.prPostmortem.prPostmortemPage.title')}
          </span>
        }
        subtitle={i18nT('apps.prPostmortem.prPostmortemPage.lastScan', {
          when: relTime(reportsQuery.data?.last_scan?.at),
        })}
      />

      {shownError && (
        <Card className="mb-2.5 border-danger" data-testid="page-error">
          <div className="text-xs text-danger">{shownError}</div>
        </Card>
      )}

      {reportsQuery.isLoading ? (
        <ContentSkeleton rows={6} />
      ) : selected !== null && detailQuery.data ? (
        <ReportDetail
          report={detailQuery.data}
          onBack={() => setSelected(null)}
          onDecide={decide}
          onRule={rule}
          onReattribute={reattribute}
          busy={busy}
        />
      ) : selected !== null ? (
        <ContentSkeleton rows={4} />
      ) : (
        <>
          <div className="mb-3 flex gap-1.5">
            <Btn primary={tab === 'reports'} onClick={() => setTab('reports')} data-testid="tab-reports">
              {i18nT('apps.prPostmortem.prPostmortemPage.tabReports', {
                count: reportsQuery.data?.reports.length ?? 0,
              })}
            </Btn>
            <Btn primary={tab === 'backlog'} onClick={() => setTab('backlog')} data-testid="tab-backlog">
              {i18nT('apps.prPostmortem.prPostmortemPage.tabBacklog', {
                count: backlogQuery.data?.totals.clusters ?? 0,
              })}
            </Btn>
          </div>

          {tab === 'backlog' ? (
            backlogQuery.data ? (
              <BacklogView
                backlog={backlogQuery.data}
                onApply={apply}
                onRefresh={invalidate}
                busy={busy}
              />
            ) : (
              <ContentSkeleton rows={4} />
            )
          ) : (
            <ReportsList
              data={reportsQuery.data ?? { reports: [], last_scan: null, repos: [] }}
              onOpen={setSelected}
              onRescan={rescan}
              rescanning={rescanning}
            />
          )}
        </>
      )}
    </div>
  )
}
