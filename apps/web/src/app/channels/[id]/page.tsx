"use client"

import Link from "next/link"
import { use, useCallback, useState } from "react"
import { CheckCircle2, ChevronLeft, ChevronRight, Clapperboard, ExternalLink, FolderOpen, Loader2, Play, PlusCircle, RefreshCw } from "lucide-react"

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
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { ScrollArea } from "@/components/ui/scroll-area"
import {
  ChannelVideo,
  ChannelVideoSort,
  createTask,
  finalVideoUrl,
  getChannelVideos,
  openTaskFolder,
  originalVideoUrl,
  refreshChannel,
} from "@/lib/api"
import { useI18n } from "@/lib/i18n"
import { SerialPollingContext, useSerialPolling } from "@/lib/use-serial-polling"

const PAGE_SIZE = 20

type PlayTarget =
  | { kind: "video"; src: string; title: string }
  | { kind: "youtube"; videoId: string; title: string }

function formatViews(n: number | null, language: string): string {
  if (n == null) return ""
  if (language === "zh") {
    if (n >= 1e8) return `${(n / 1e8).toFixed(1)}亿`
    if (n >= 1e4) return `${(n / 1e4).toFixed(1)}万`
    return String(n)
  }
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}K`
  return String(n)
}

export default function ChannelDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params)
  const { t, language } = useI18n()
  const [channelName, setChannelName] = useState("")
  const [channelUrl, setChannelUrl] = useState("")
  const [videos, setVideos] = useState<ChannelVideo[]>([])
  const [total, setTotal] = useState(0)
  const [totalPages, setTotalPages] = useState(1)
  const [page, setPage] = useState(1)
  const [sort, setSort] = useState<ChannelVideoSort>("date")
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState("")
  const [addingId, setAddingId] = useState<string | null>(null)
  const [addError, setAddError] = useState("")
  const [refreshing, setRefreshing] = useState(false)
  const [playTarget, setPlayTarget] = useState<PlayTarget | null>(null)
  const [autoUploadBilibili, setAutoUploadBilibili] = useState(false)

  const load = useCallback(async (context?: SerialPollingContext) => {
    try {
      const res = await getChannelVideos(id, page, PAGE_SIZE, sort, context?.signal)
      if (context && !context.isCurrent()) return
      setChannelName(res.subscription.channel_name || res.subscription.channel_url)
      setChannelUrl(res.subscription.channel_url)
      setVideos(res.videos)
      setTotal(res.total)
      setTotalPages(res.total_pages)
      setLoading(false)
      setError("")
    } catch (err) {
      if (context && !context.isCurrent()) return
      setLoading(false)
      setError(err instanceof Error ? err.message : t.channels.error)
    }
  }, [id, page, sort, t])

  const invalidate = useSerialPolling(load, 15000)

  const changeSort = useCallback((next: ChannelVideoSort) => {
    setPage(1)
    setSort(next)
  }, [])

  const handleRefresh = useCallback(async () => {
    if (refreshing) return
    setRefreshing(true)
    setError("")
    try {
      await refreshChannel(id)
      // Poller re-reads from cache on its next tick; force an immediate reload.
      invalidate()
    } catch (err) {
      setError(err instanceof Error ? err.message : t.channels.error)
    } finally {
      setRefreshing(false)
    }
  }, [refreshing, id, invalidate, t])

  const addToTasks = useCallback(
    async (video: ChannelVideo) => {
      if (addingId) return
      setAddingId(video.id)
      setAddError("")
      try {
        const task = await createTask(video.url, "auto", autoUploadBilibili)
        setVideos((prev) =>
          prev.map((v) =>
            v.id === video.id
              ? { ...v, downloaded: true, task_id: task?.id ?? null }
              : v,
          ),
        )
        if (task?.id) {
          window.open(`/tasks/${task.id}`, "_blank")
        }
      } catch (err) {
        setAddError(err instanceof Error ? err.message : t.channels.error)
      } finally {
        setAddingId(null)
      }
    },
    [addingId, autoUploadBilibili, t],
  )

  const handleOpenFolder = useCallback(
    async (video: ChannelVideo) => {
      if (!video.task_id) return
      try {
        await openTaskFolder(video.task_id)
      } catch (err) {
        setError(err instanceof Error ? err.message : t.channels.folderError)
      }
    },
    [t],
  )

  const playLocal = useCallback(
    (video: ChannelVideo, src: string) => {
      setPlayTarget({ kind: "video", src, title: video.title || video.id })
    },
    [],
  )

  const playYouTube = useCallback(
    (video: ChannelVideo) => {
      setPlayTarget({ kind: "youtube", videoId: video.id, title: video.title || video.id })
    },
    [],
  )

  const renderDownloadedActions = (video: ChannelVideo) => {
    const hasAny = video.has_final || video.has_original
    return (
      <div className="flex shrink-0 flex-wrap items-center justify-end gap-1.5">
        {video.has_final ? (
          <Button
            type="button"
            size="sm"
            onClick={() => video.task_id && playLocal(video, finalVideoUrl(video.task_id))}
          >
            <Play className="size-3.5" />
            {t.channels.playTranslated}
          </Button>
        ) : null}
        {video.has_original ? (
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => video.task_id && playLocal(video, originalVideoUrl(video.task_id))}
          >
            <Clapperboard className="size-3.5" />
            {t.channels.playOriginal}
          </Button>
        ) : null}
        {video.session_path ? (
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => handleOpenFolder(video)}
          >
            <FolderOpen className="size-3.5" />
            {t.channels.openFolder}
          </Button>
        ) : null}
        {video.task_id ? (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            nativeButton={false}
            render={<Link href={`/tasks/${video.task_id}`} />}
          >
            <ExternalLink className="size-3.5" />
            {t.channels.openTaskDetail}
          </Button>
        ) : null}
        {!hasAny ? (
          <span className="inline-flex shrink-0 items-center gap-1 text-xs text-[#00aeec]">
            <CheckCircle2 className="size-3.5" />
            {t.channels.downloaded}
          </span>
        ) : null}
      </div>
    )
  }

  return (
    <main className="min-h-screen bg-[linear-gradient(135deg,#fff5f5_0%,#f2fbff_48%,#fff4fa_100%)] text-foreground">
      <div className="mx-auto flex w-full max-w-4xl flex-col gap-6 px-4 py-6 sm:px-6 lg:px-8">
        <AppHeader backHref="/channels" />

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              {channelName || t.channels.loadingChannel}
              <a
                href={channelUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
              >
                <ExternalLink className="size-4" />
              </a>
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={handleRefresh}
                disabled={refreshing}
              >
                {refreshing ? <Loader2 className="size-3.5 animate-spin" /> : <RefreshCw className="size-3.5" />}
                {t.channels.refresh}
              </Button>
            </CardTitle>
            <CardDescription>
              {loading ? t.common.loading : `${total} ${t.channels.videoCount}`}
            </CardDescription>
            <label className="flex items-start gap-2 text-sm">
              <input
                type="checkbox"
                checked={autoUploadBilibili}
                onChange={(event) => setAutoUploadBilibili(event.target.checked)}
                className="mt-0.5 size-4 accent-[#00aeec]"
              />
              <span>{t.home.autoUploadBilibili}</span>
            </label>
          </CardHeader>
        </Card>

        {error ? <p className="text-sm text-[#ff0033]">{error}</p> : null}

        <Card>
          <CardContent className="p-0">
            {!loading && videos.length > 0 ? (
              <div className="flex items-center justify-end gap-1.5 border-b border-border/60 px-4 py-2">
                <Button
                  type="button"
                  variant={sort === "date" ? "secondary" : "ghost"}
                  size="sm"
                  onClick={() => changeSort("date")}
                >
                  {t.channels.sortByNewest}
                </Button>
                <Button
                  type="button"
                  variant={sort === "views" ? "secondary" : "ghost"}
                  size="sm"
                  onClick={() => changeSort("views")}
                >
                  {t.channels.sortByViews}
                </Button>
              </div>
            ) : null}
            {loading ? (
              <div className="flex items-center justify-center gap-2 p-8 text-muted-foreground">
                <Loader2 className="size-4 animate-spin" />
                {t.common.loading}
              </div>
            ) : videos.length === 0 ? (
              <p className="p-8 text-sm text-muted-foreground">{t.channels.noVideos}</p>
            ) : (
              <ScrollArea className="h-[60vh]">
                <ul className="flex flex-col">
                  {videos.map((video) => (
                    <li
                      key={video.id}
                      className="flex w-full flex-wrap items-center gap-3 border-b border-border/60 px-4 py-3 text-sm"
                    >
                      <a
                        href={video.url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="min-w-0 flex-1 truncate hover:text-[#00aeec]"
                      >
                        {video.title || video.id}
                      </a>
                      <div className="flex shrink-0 flex-col items-end gap-0.5 text-xs text-muted-foreground">
                        {video.view_count != null ? (
                          <span className="whitespace-nowrap tabular-nums">
                            {formatViews(video.view_count, language)} {t.channels.views}
                          </span>
                        ) : null}
                      </div>
                      {video.downloaded ? (
                        renderDownloadedActions(video)
                      ) : (
                        <div className="flex shrink-0 flex-wrap items-center justify-end gap-1.5">
                          <Button
                            type="button"
                            size="sm"
                            onClick={() => playYouTube(video)}
                          >
                            <Play className="size-3.5" />
                            {t.channels.playOnYouTube}
                          </Button>
                          <Button
                            type="button"
                            variant="outline"
                            size="sm"
                            onClick={() => addToTasks(video)}
                            disabled={addingId === video.id}
                          >
                            {addingId === video.id ? (
                              <Loader2 className="size-3.5 animate-spin" />
                            ) : (
                              <PlusCircle className="size-3.5" />
                            )}
                            {t.channels.addToTasks}
                          </Button>
                        </div>
                      )}
                    </li>
                  ))}
                </ul>
              </ScrollArea>
            )}
          </CardContent>
        </Card>

        {!loading && totalPages > 1 ? (
          <div className="flex items-center justify-center gap-3">
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => setPage((p) => Math.max(1, p - 1))}
              disabled={page <= 1}
            >
              <ChevronLeft className="size-4" />
            </Button>
            <span className="text-sm text-muted-foreground">
              {page} / {totalPages}
            </span>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
              disabled={page >= totalPages}
            >
              <ChevronRight className="size-4" />
            </Button>
          </div>
        ) : null}

        {addError ? <p className="text-sm text-[#ff0033]">{addError}</p> : null}

        <Dialog open={playTarget !== null} onOpenChange={(open) => { if (!open) setPlayTarget(null) }}>
          <DialogContent className="sm:max-w-3xl">
            <DialogHeader>
              <DialogTitle className="truncate">{playTarget?.title ?? ""}</DialogTitle>
            </DialogHeader>
            {playTarget?.kind === "video" ? (
              <video
                key={playTarget.src}
                src={playTarget.src}
                crossOrigin="use-credentials"
                controls
                autoPlay
                className="aspect-video w-full rounded-md bg-black"
              />
            ) : playTarget ? (
              <iframe
                src={`https://www.youtube-nocookie.com/embed/${playTarget.videoId}`}
                title="YouTube video player"
                allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share"
                allowFullScreen
                className="aspect-video w-full rounded-md bg-black"
              />
            ) : null}
          </DialogContent>
        </Dialog>
      </div>
    </main>
  )
}
