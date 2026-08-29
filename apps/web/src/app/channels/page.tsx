"use client"

import Link from "next/link"
import { FormEvent, useCallback, useEffect, useMemo, useState } from "react"
import { ArrowRight, CheckCircle2, ExternalLink, Loader2, RefreshCw, Trash2, XCircle } from "lucide-react"

import { AppHeader } from "@/components/app-header"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Progress } from "@/components/ui/progress"
import { Tooltip, TooltipProvider } from "@/components/ui/tooltip"
import {
  ChannelJob,
  ChannelSubscription,
  deleteChannelSubscription,
  getChannelJob,
  listChannelSubscriptions,
  processAllSubscriptions,
  refreshAllSubscriptions,
  subscribeChannel,
} from "@/lib/api"
import { useI18n } from "@/lib/i18n"
import { SerialPollingContext, useSerialPolling } from "@/lib/use-serial-polling"

const POLL_INTERVAL_MS = 2000
const MAX_LIMIT = 50

function formatCount(template: string, count: number) {
  return template.replace("{{count}}", String(count))
}

export default function ChannelsPage() {
  const { t } = useI18n()
  const [subscribeInput, setSubscribeInput] = useState("")
  const [groupInput, setGroupInput] = useState("")
  const [limit, setLimit] = useState(2)
  const [autoUploadBilibili, setAutoUploadBilibili] = useState(false)
  const [job, setJob] = useState<ChannelJob | null>(null)
  const [refreshJob, setRefreshJob] = useState<ChannelJob | null>(null)
  const [subscriptions, setSubscriptions] = useState<ChannelSubscription[]>([])
  const [submitting, setSubmitting] = useState(false)
  const [processingAll, setProcessingAll] = useState(false)
  const [refreshingAll, setRefreshingAll] = useState(false)
  const [error, setError] = useState("")
  const [removeOpen, setRemoveOpen] = useState(false)
  const [removing, setRemoving] = useState(false)
  const [removeTarget, setRemoveTarget] = useState<ChannelSubscription | null>(null)

  const isActive = job?.status === "queued" || job?.status === "running"
  const isDone = job?.status === "succeeded"
  const isFailed = job?.status === "failed"

  const isRefreshActive = refreshJob?.status === "queued" || refreshJob?.status === "running"

  const reloadSubscriptions = useCallback(async (context?: SerialPollingContext) => {
    try {
      const res = await listChannelSubscriptions(context?.signal)
      if (context && !context.isCurrent()) return
      setSubscriptions(res.subscriptions)
    } catch {
      // ignore transient failures
    }
  }, [])

  useSerialPolling(reloadSubscriptions, 5000)

  const subscribe = useCallback(
    async (event: FormEvent) => {
      event.preventDefault()
      const url = subscribeInput.trim()
      if (!url || submitting) return
      setSubmitting(true)
      setError("")
      try {
        await subscribeChannel(url, groupInput.trim())
        setSubscribeInput("")
        await reloadSubscriptions()
      } catch (err) {
        setError(err instanceof Error ? err.message : t.channels.error)
      } finally {
        setSubmitting(false)
      }
    },
    [subscribeInput, submitting, reloadSubscriptions, t, groupInput],
  )

  const processAll = useCallback(async () => {
    if (processingAll || isActive) return
    setProcessingAll(true)
    setError("")
    setJob(null)
    try {
      const created = await processAllSubscriptions(limit, autoUploadBilibili)
      setJob(created)
    } catch (err) {
      setError(err instanceof Error ? err.message : t.channels.error)
    } finally {
      setProcessingAll(false)
    }
  }, [limit, autoUploadBilibili, processingAll, isActive, t])

  const refreshAll = useCallback(async () => {
    if (refreshingAll || isRefreshActive) return
    setRefreshingAll(true)
    setError("")
    setRefreshJob(null)
    try {
      const created = await refreshAllSubscriptions()
      const placeholder: ChannelJob = {
        id: created.job_id,
        status: "queued",
        channels: [],
        video_limit: 0,
        processed: 0,
        total: created.total ?? 0,
        created_count: 0,
        skipped_count: 0,
        error_message: null,
        created_at: "",
        started_at: null,
        completed_at: null,
        auto_upload_bilibili: false,
      }
      setRefreshJob(placeholder)
    } catch (err) {
      setError(err instanceof Error ? err.message : t.channels.error)
    } finally {
      setRefreshingAll(false)
    }
  }, [refreshingAll, isRefreshActive, t])

  useEffect(() => {
    if (!job || !isActive) return
    const timer = window.setInterval(async () => {
      try {
        const next = await getChannelJob(job.id)
        setJob(next)
        if (next.status === "succeeded" || next.status === "failed") {
          reloadSubscriptions()
        }
      } catch {
        // ignore transient poll errors
      }
    }, POLL_INTERVAL_MS)
    return () => window.clearInterval(timer)
  }, [job, isActive, reloadSubscriptions])

  useEffect(() => {
    if (!refreshJob || !isRefreshActive) return
    const timer = window.setInterval(async () => {
      try {
        const next = await getChannelJob(refreshJob.id)
        setRefreshJob(next)
        if (next.status === "succeeded" || next.status === "failed") {
          reloadSubscriptions()
        }
      } catch {
        // ignore transient poll errors
      }
    }, POLL_INTERVAL_MS)
    return () => window.clearInterval(timer)
  }, [refreshJob, isRefreshActive, reloadSubscriptions])

  const confirmRemove = useCallback(
    async (subscriptionId: string) => {
      if (removing) return
      setRemoving(true)
      setError("")
      try {
        await deleteChannelSubscription(subscriptionId)
        setSubscriptions((prev) => prev.filter((s) => s.id !== subscriptionId))
        setRemoveOpen(false)
        setRemoveTarget(null)
      } catch (err) {
        setError(err instanceof Error ? err.message : t.channels.error)
      } finally {
        setRemoving(false)
      }
    },
    [removing, t],
  )

  const requestRemove = useCallback((sub: ChannelSubscription) => {
    setRemoveTarget(sub)
    setRemoveOpen(true)
  }, [])

  const progress = job && job.total > 0 ? Math.round((job.processed / job.total) * 100) : 0
  const refreshProgress =
    refreshJob && refreshJob.total > 0
      ? Math.round((refreshJob.processed / refreshJob.total) * 100)
      : 0

  const groupedSubscriptions = useMemo(() => {
    const map = new Map<string, ChannelSubscription[]>()
    for (const sub of subscriptions) {
      const name = sub.group_name?.trim() || t.channels.defaultGroup
      const list = map.get(name)
      if (list) {
        list.push(sub)
      } else {
        map.set(name, [sub])
      }
    }
    return Array.from(map.entries()).map(([name, items]) => ({ name, items }))
  }, [subscriptions, t])

  return (
    <TooltipProvider>
      <main className="min-h-screen bg-[linear-gradient(135deg,#fff5f5_0%,#f2fbff_48%,#fff4fa_100%)] text-foreground">
      <div className="mx-auto flex w-full max-w-4xl flex-col gap-6 px-4 py-6 sm:px-6 lg:px-8">
        <AppHeader />

        <Card>
          <CardHeader>
            <CardTitle>{t.channels.title}</CardTitle>
            <CardDescription>{t.channels.description}</CardDescription>
          </CardHeader>
          <CardContent>
            <form onSubmit={subscribe} className="space-y-4">
              <div className="space-y-2">
                <Label htmlFor="subscribe-url">{t.channels.subscribeLabel}</Label>
                <Input
                  id="subscribe-url"
                  placeholder={t.channels.subscribePlaceholder}
                  value={subscribeInput}
                  onChange={(event) => setSubscribeInput(event.target.value)}
                  disabled={submitting}
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="subscribe-group">{t.channels.groupLabel}</Label>
                <Input
                  id="subscribe-group"
                  placeholder={t.channels.groupPlaceholder}
                  value={groupInput}
                  onChange={(event) => setGroupInput(event.target.value)}
                  disabled={submitting}
                  className="max-w-[16rem]"
                />
              </div>
              {error ? <p className="text-sm text-[#ff0033]">{error}</p> : null}
              <Button type="submit" disabled={!subscribeInput.trim() || submitting}>
                {submitting ? <Loader2 className="size-4 animate-spin" /> : <CheckCircle2 className="size-4" />}
                {submitting ? t.channels.submitting : t.channels.subscribe}
              </Button>
            </form>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>{t.channels.batchTitle}</CardTitle>
            <CardDescription>{t.channels.batchDescription}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="limit">{t.channels.limitLabel}</Label>
              <Input
                id="limit"
                type="number"
                min={1}
                max={MAX_LIMIT}
                value={limit}
                onChange={(event) => setLimit(Number(event.target.value))}
                className="max-w-[10rem]"
              />
            </div>
            <label className="flex items-start gap-3 rounded-lg border border-border/60 bg-muted/20 px-3 py-3 text-sm">
              <input
                type="checkbox"
                checked={autoUploadBilibili}
                onChange={(event) => setAutoUploadBilibili(event.target.checked)}
                className="mt-0.5 size-4 accent-[#00aeec]"
              />
              <span>
                <span className="block font-medium">{t.home.autoUploadBilibili}</span>
                <span className="block text-xs text-muted-foreground">{t.home.autoUploadBilibiliHelp}</span>
              </span>
            </label>
            <Button
              type="button"
              variant="outline"
              onClick={processAll}
              disabled={!subscriptions.length || processingAll || isActive}
            >
              {processingAll || isActive ? (
                <Loader2 className="size-4 animate-spin" />
              ) : (
                <RefreshCw className="size-4" />
              )}
              {processingAll || isActive ? t.channels.submitting : t.channels.processAll}
            </Button>

            {job ? (
              <div className="space-y-3">
                {isActive ? (
                  <>
                    <Progress value={progress} />
                    <p className="text-sm text-muted-foreground">
                      {t.channels.progress.replace("{{processed}}", String(job.processed)).replace("{{total}}", String(job.total))}
                    </p>
                  </>
                ) : null}
                {isDone ? (
                  <div className="space-y-1">
                    <p className="flex items-center gap-2 text-sm text-[#00aeec]">
                      <CheckCircle2 className="size-4" />
                      {formatCount(t.channels.created, job.created_count)}
                    </p>
                    <p className="flex items-center gap-2 text-sm text-muted-foreground">
                      {formatCount(t.channels.skipped, job.skipped_count)}
                    </p>
                  </div>
                ) : null}
                {isFailed ? (
                  <p className="flex items-center gap-2 text-sm text-[#ff0033]">
                    <XCircle className="size-4" />
                    {job.error_message || t.channels.failed}
                  </p>
                ) : null}
              </div>
            ) : null}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center justify-between gap-3">
              {t.channels.subscriptionsTitle}
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={refreshAll}
                disabled={!subscriptions.length || refreshingAll || isRefreshActive}
              >
                {refreshingAll || isRefreshActive ? (
                  <Loader2 className="size-3.5 animate-spin" />
                ) : (
                  <RefreshCw className="size-3.5" />
                )}
                {t.channels.refreshAll}
              </Button>
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {isRefreshActive ? (
              <div className="space-y-1">
                <Progress value={refreshProgress} />
                <p className="text-sm text-muted-foreground">
                  {t.channels.refreshProgress.replace("{{processed}}", String(refreshJob?.processed ?? 0)).replace("{{total}}", String(refreshJob?.total ?? 0))}
                </p>
              </div>
            ) : null}
            {refreshJob && refreshJob.status === "succeeded" ? (
              <p className="flex items-center gap-2 text-sm text-[#00aeec]">
                <CheckCircle2 className="size-4" />
                {t.channels.refreshDone}
              </p>
            ) : null}
            {refreshJob && refreshJob.status === "failed" ? (
              <p className="flex items-center gap-2 text-sm text-[#ff0033]">
                <XCircle className="size-4" />
                {refreshJob.error_message || t.channels.failed}
              </p>
            ) : null}

            {subscriptions.length > 0 ? (
              <div className="space-y-6">
                {groupedSubscriptions.map((group) => (
                  <section key={group.name}>
                    <h3 className="mb-1 flex items-center gap-2 px-1 text-xs font-medium tracking-wide text-muted-foreground">
                      {group.name}
                      <span className="rounded-full bg-muted px-1.5 py-0.5 text-[10px] tabular-nums">
                        {group.items.length}
                      </span>
                    </h3>
                    <ul className="flex flex-col">
                      {group.items.map((sub) => (
                        <li key={sub.id} className="flex w-full items-center gap-3 px-1 py-3 text-sm">
                          <Link
                            href={`/channels/${sub.id}`}
                            className="min-w-0 flex-1 group"
                          >
                            <p className="truncate font-medium group-hover:text-[#00aeec]">
                              {sub.channel_name || sub.channel_url}
                            </p>
                            <p className="truncate text-xs text-muted-foreground">{sub.channel_url}</p>
                          </Link>
                          <Tooltip content={t.channels.openChannel}>
                            <Button
                              type="button"
                              variant="ghost"
                              size="icon-sm"
                              nativeButton={false}
                              render={<Link href={`/channels/${sub.id}`} />}
                              aria-label={t.channels.openChannel}
                            >
                              <ArrowRight className="size-4" />
                            </Button>
                          </Tooltip>
                          <Tooltip content={t.channels.openChannelUrl}>
                            <Button
                              type="button"
                              variant="ghost"
                              size="icon-sm"
                              nativeButton={false}
                              render={<Link href={sub.channel_url} target="_blank" rel="noopener noreferrer" />}
                              aria-label={t.channels.openChannelUrl}
                            >
                              <ExternalLink className="size-4" />
                            </Button>
                          </Tooltip>
                          <Tooltip content={t.channels.removeSubscription}>
                            <Button
                              type="button"
                              variant="ghost"
                              size="icon-sm"
                              aria-label={t.channels.removeSubscription}
                              onClick={() => requestRemove(sub)}
                            >
                              <Trash2 className="size-4" />
                            </Button>
                          </Tooltip>
                        </li>
                      ))}
                    </ul>
                  </section>
                ))}
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">{t.channels.noSubscriptions}</p>
            )}
          </CardContent>
        </Card>

        <Dialog open={removeOpen} onOpenChange={setRemoveOpen}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>{t.channels.removeTitle}</DialogTitle>
              <DialogDescription>
                {t.channels.removeDescription}
              </DialogDescription>
            </DialogHeader>
            {removeTarget ? (
              <p className="truncate text-sm font-medium">{removeTarget.channel_name || removeTarget.channel_url}</p>
            ) : null}
            {error ? <p className="text-sm text-[#ff0033]">{error}</p> : null}
            <DialogFooter>
              <DialogClose render={<Button variant="outline" disabled={removing} />}>
                {t.common.cancel}
              </DialogClose>
              <Button
                variant="destructive"
                onClick={() => removeTarget && confirmRemove(removeTarget.id)}
                disabled={removing}
              >
                {removing ? <Loader2 className="size-4 animate-spin" /> : <Trash2 className="size-4" />}
                {removing ? t.channels.removing : t.channels.confirmRemove}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </div>
      </main>
    </TooltipProvider>
  )
}
