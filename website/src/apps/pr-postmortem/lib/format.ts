// Presentation helpers shared by the PR Postmortem views.
import { i18nT } from '../../../i18n/t'
import type { Verdict } from '../api'

/** Verdict → the shared Badge's variant vocabulary. */
export function verdictVariant(verdict: Verdict | null): 'ok' | 'warn' | 'muted' {
  if (verdict === 'strong') return 'ok'
  if (verdict === 'moderate') return 'warn'
  return 'muted'
}

/**
 * Caveat flag -> its explanation key.
 *
 * A literal map rather than `apps.prPostmortem.flags.${flag}`: an interpolated
 * key is invisible to the extractor and to unused-key tooling, so it renders as
 * a raw dotted path the day it goes missing. Same reason as `UPDATE_ERROR_KEYS`
 * in pages/settings/AboutPanel.tsx. Enforced by src/i18n/dynamicKeys.test.ts.
 */
const FLAG_HELP_KEY = {
  bulk_port: 'apps.prPostmortem.flags.bulk_port',
  large_commit: 'apps.prPostmortem.flags.large_commit',
  diffuse: 'apps.prPostmortem.flags.diffuse',
  low_signal: 'apps.prPostmortem.flags.low_signal',
  no_source_signal: 'apps.prPostmortem.flags.no_source_signal',
  unmapped_commit: 'apps.prPostmortem.flags.unmapped_commit',
} as const

/** A caveat flag's meaning, so a reader can weigh a verdict without the docs. */
export function flagHelp(flag: string): string {
  const key = FLAG_HELP_KEY[flag as keyof typeof FLAG_HELP_KEY]
  // The backend may add a flag before the UI knows its name; show the raw flag
  // rather than a dotted path.
  return key ? i18nT(key) : flag
}

export function relTime(iso: string | null | undefined): string {
  if (!iso) return i18nT('apps.prPostmortem.time.never')
  const then = Date.parse(iso)
  if (Number.isNaN(then)) return i18nT('apps.prPostmortem.time.never')
  const mins = Math.max(0, Math.round((Date.now() - then) / 60000))
  if (mins < 60) return i18nT('apps.prPostmortem.time.minutesAgo', { count: mins })
  const hrs = Math.round(mins / 60)
  if (hrs < 48) return i18nT('apps.prPostmortem.time.hoursAgo', { count: hrs })
  return i18nT('apps.prPostmortem.time.daysAgo', { count: Math.round(hrs / 24) })
}

/**
 * The culprit's PR URL, derived from the fix's.
 *
 * Matching `/pull/<n>` rather than trailing digits means a URL carrying a slug,
 * query or anchor still retargets instead of silently linking back to the fix.
 * Returns an empty string when it cannot be derived, so the caller renders plain
 * text rather than a wrong link.
 */
export function culpritUrl(fixUrl: string, culpritPr: number | null): string {
  if (!fixUrl || !culpritPr) return ''
  const out = fixUrl.replace(/\/pull\/\d+.*$/, `/pull/${culpritPr}`)
  return out === fixUrl ? '' : out
}

/** Apply target -> its label key. Literal for the same reason as FLAG_HELP_KEY. */
const TARGET_LABEL_KEY = {
  steering: 'apps.prPostmortem.targets.steering',
  lesson: 'apps.prPostmortem.targets.lesson',
  issue: 'apps.prPostmortem.targets.issue',
  pull_request: 'apps.prPostmortem.targets.pull_request',
  docs: 'apps.prPostmortem.targets.docs',
} as const

/** Human label for an apply target. */
export function targetLabel(target: string): string {
  const key = TARGET_LABEL_KEY[target as keyof typeof TARGET_LABEL_KEY]
  return key ? i18nT(key) : target
}
