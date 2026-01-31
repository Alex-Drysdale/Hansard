import { useState, useEffect, useCallback } from 'react'
import type { WatchlistMember } from '../types'

const STORAGE_KEY = 'parliament-navigator-watchlist'

export function useWatchlist() {
  const [watchlist, setWatchlist] = useState<WatchlistMember[]>(() => {
    try {
      const stored = localStorage.getItem(STORAGE_KEY)
      return stored ? JSON.parse(stored) : []
    } catch {
      return []
    }
  })

  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(watchlist))
  }, [watchlist])

  const addToWatchlist = useCallback((member: WatchlistMember) => {
    setWatchlist((prev) => {
      if (prev.some((m) => m.id === member.id)) return prev
      return [...prev, member]
    })
  }, [])

  const removeFromWatchlist = useCallback((memberId: number) => {
    setWatchlist((prev) => prev.filter((m) => m.id !== memberId))
  }, [])

  const isInWatchlist = useCallback(
    (memberId: number) => {
      return watchlist.some((m) => m.id === memberId)
    },
    [watchlist]
  )

  return {
    watchlist,
    addToWatchlist,
    removeFromWatchlist,
    isInWatchlist,
  }
}
