import { useState } from 'react'
import { useRecentDebates } from '../hooks/useDebates'
import DebateCard from '../components/DebateCard'

export default function Dashboard() {
  const [houseFilter, setHouseFilter] = useState('')
  const { data: debates, isLoading, error } = useRecentDebates(houseFilter || null)

  // Group debates by date
  const groupedDebates = (debates || []).reduce<Record<string, typeof debates>>((acc, debate) => {
    const date = debate.SittingDate?.split('T')[0] || 'Unknown'
    if (!acc[date]) acc[date] = []
    acc[date]!.push(debate)
    return acc
  }, {})

  const sortedDates = Object.keys(groupedDebates).sort((a, b) => b.localeCompare(a))

  return (
    <div>
      <div className="flex items-center justify-between mb-8">
        <div>
          <h1 className="text-3xl font-bold text-slate-900">Recent Debates</h1>
          <p className="text-slate-600 mt-1">Browse the latest parliamentary proceedings</p>
        </div>
        <div className="flex items-center gap-3">
          <label className="text-sm font-medium text-slate-700">Filter by House:</label>
          <select
            value={houseFilter}
            onChange={(e) => setHouseFilter(e.target.value)}
            className="px-3 py-2 border border-slate-300 rounded-lg bg-white focus:ring-2 focus:ring-slate-800 focus:border-transparent outline-none"
          >
            <option value="">All Houses</option>
            <option value="Commons">House of Commons</option>
            <option value="Lords">House of Lords</option>
          </select>
        </div>
      </div>

      {isLoading && (
        <div className="flex items-center justify-center py-20">
          <div className="animate-spin rounded-full h-10 w-10 border-4 border-slate-200 border-t-slate-800"></div>
        </div>
      )}

      {error && (
        <div className="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded-lg">
          Failed to load debates: {error.message}
        </div>
      )}

      {!isLoading && !error && sortedDates.length === 0 && (
        <div className="text-center py-20 text-slate-500">
          No debates found
        </div>
      )}

      <div className="space-y-8">
        {sortedDates.map((date) => (
          <section key={date}>
            <h2 className="text-lg font-semibold text-slate-700 mb-4 flex items-center gap-2">
              <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" />
              </svg>
              {formatDate(date)}
            </h2>
            <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
              {groupedDebates[date]!.map((debate, idx) => (
                <DebateCard key={debate.DebateSectionExtId || idx} debate={debate} />
              ))}
            </div>
          </section>
        ))}
      </div>
    </div>
  )
}

function formatDate(dateStr: string): string {
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
