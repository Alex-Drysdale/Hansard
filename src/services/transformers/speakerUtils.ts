import type { DebateItem, Speaker } from '../../types'

/**
 * Extract unique speakers from debate items
 * Deduplicates and aggregates speaker counts
 */
export function extractSpeakers(items: DebateItem[]): Speaker[] {
  const speakerMap = new Map<string | number, Speaker>()

  items.forEach((item) => {
    if (item.ItemType === 'Contribution' && item.AttributedTo) {
      const key = item.MemberId || item.AttributedTo
      if (!speakerMap.has(key)) {
        speakerMap.set(key, {
          id: item.MemberId,
          name: item.AttributedTo,
          party: item.MemberParty,
          count: 0,
        })
      }
      speakerMap.get(key)!.count++
    }
  })

  return Array.from(speakerMap.values()).sort((a, b) => b.count - a.count)
}

/**
 * Group debates/items by date
 */
export function groupByDate<T extends { SittingDate?: string }>(
  items: T[]
): Record<string, T[]> {
  return items.reduce<Record<string, T[]>>((acc, item) => {
    const date = item.SittingDate?.split('T')[0] || 'Unknown'
    if (!acc[date]) acc[date] = []
    acc[date].push(item)
    return acc
  }, {})
}

/**
 * Sort dates in descending order
 */
export function sortDatesDescending(dates: string[]): string[] {
  return dates.sort((a, b) => b.localeCompare(a))
}

/**
 * Format date string to human-readable format
 */
export function formatDate(dateStr: string): string {
  if (!dateStr || dateStr === 'Unknown') return 'Unknown Date'
  try {
    return new Date(dateStr).toLocaleDateString('en-GB', {
      weekday: 'long',
      day: 'numeric',
      month: 'long',
      year: 'numeric',
    })
  } catch {
    return dateStr
  }
}

/**
 * Format short date (for cards, etc)
 */
export function formatShortDate(dateStr: string): string {
  if (!dateStr) return 'Unknown date'
  try {
    return new Date(dateStr).toLocaleDateString('en-GB', {
      day: 'numeric',
      month: 'short',
      year: 'numeric',
    })
  } catch {
    return dateStr
  }
}
