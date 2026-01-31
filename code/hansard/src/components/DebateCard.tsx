import { memo } from 'react'
import { Link } from 'react-router-dom'
import type { DebateSearchResult } from '../types'

interface DebateCardProps {
  debate: DebateSearchResult
}

function DebateCard({ debate }: DebateCardProps) {
  const date = debate.SittingDate
    ? new Date(debate.SittingDate).toLocaleDateString('en-GB', {
        day: 'numeric',
        month: 'short',
        year: 'numeric',
      })
    : 'Unknown date'

  const house = debate.House || 'Unknown'
  const houseColor = house === 'Commons' ? 'bg-green-100 text-green-800' : 'bg-red-100 text-red-800'

  return (
    <Link
      to={`/debate/${debate.DebateSectionExtId}`}
      className="block bg-white rounded-lg border border-slate-200 p-5 hover:shadow-md hover:border-slate-300 transition-all"
    >
      <div className="flex items-start justify-between gap-4">
        <div className="flex-1 min-w-0">
          <h3 className="font-semibold text-slate-900 text-lg leading-tight mb-2 line-clamp-2">
            {debate.Title || 'Untitled Debate'}
          </h3>
          <div className="flex items-center gap-3 text-sm">
            <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${houseColor}`}>
              {house}
            </span>
            <span className="text-slate-500">{date}</span>
            {debate.DebateSection && (
              <span className="text-slate-400 truncate">{debate.DebateSection}</span>
            )}
          </div>
        </div>
        <svg className="w-5 h-5 text-slate-400 flex-shrink-0 mt-1" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
        </svg>
      </div>
    </Link>
  )
}

export default memo(DebateCard)
