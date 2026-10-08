import { useCallback, useEffect, useRef, useState } from 'react'
import type { Paged } from '../services/api'

/** Paged list with infinite scroll; reloads from page 1 whenever `fetchPage` changes. */
export function usePagedList<T>(fetchPage: (page: number) => Promise<Paged<T>>) {
  const [items, setItems] = useState<T[]>([])
  const [totalCount, setTotalCount] = useState(0)
  const [hasMore, setHasMore] = useState(false)
  const [page, setPage] = useState(1)
  const [isLoading, setIsLoading] = useState(true)
  const [isLoadingMore, setIsLoadingMore] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const sentinelRef = useRef<HTMLDivElement>(null)
  const requestId = useRef(0)

  const load = useCallback(async (pageNum: number, append: boolean) => {
    const id = ++requestId.current
    if (append) setIsLoadingMore(true)
    else setIsLoading(true)
    setError(null)
    try {
      const result = await fetchPage(pageNum)
      if (id !== requestId.current) return
      setItems(prev => (append ? [...prev, ...result.items] : result.items))
      setTotalCount(result.totalCount)
      setHasMore(result.hasMore)
      setPage(pageNum)
    } catch (err) {
      if (id === requestId.current) setError(err instanceof Error ? err.message : String(err))
    } finally {
      if (id === requestId.current) {
        setIsLoading(false)
        setIsLoadingMore(false)
      }
    }
  }, [fetchPage])

  useEffect(() => {
    load(1, false)
  }, [load])

  useEffect(() => {
    const sentinel = sentinelRef.current
    if (!sentinel) return
    const observer = new IntersectionObserver(entries => {
      if (entries[0].isIntersecting && hasMore && !isLoadingMore && !isLoading) load(page + 1, true)
    }, { rootMargin: '200px' })
    observer.observe(sentinel)
    return () => observer.disconnect()
  }, [hasMore, isLoadingMore, isLoading, page, load])

  return { items, totalCount, hasMore, isLoading, isLoadingMore, error, sentinelRef }
}

/** Debounce a fast-changing value (search boxes). */
export function useDebounced<T>(value: T, ms = 300): T {
  const [v, setV] = useState(value)
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms)
    return () => clearTimeout(t)
  }, [value, ms])
  return v
}
