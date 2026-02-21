import { memo } from 'react'
import { getMemberPhotoUrl, getPartyColor } from '../services/membersApi'
import { sanitizeHtml } from '../utils/sanitize'
import { LoadingSpinner, EmptyState } from './ui'
import type { MemberValue, WatchlistMember, ContributionResult } from '../types'

interface MemberDetailProps {
  member: MemberValue | WatchlistMember
  contributions: ContributionResult[]
  isLoading: boolean
  isWatched: boolean
  onAdd: () => void
  onRemove: () => void
}

function MemberDetail({ member, contributions, isLoading, isWatched, onAdd, onRemove }: MemberDetailProps) {
  const m = member
  const photoUrl = getMemberPhotoUrl(m.id)
  const partyColor = getPartyColor(m.latestParty?.name)

  return (
    <div>
      <div className="bg-white rounded-lg border border-slate-200 p-6 mb-6">
        <div className="flex items-start gap-6">
          <img
            src={photoUrl}
            alt={m.nameDisplayAs}
            className="w-24 h-24 rounded-full object-cover bg-slate-100"
            onError={(e) => {
              const target = e.target as HTMLImageElement
              target.src = `https://ui-avatars.com/api/?name=${encodeURIComponent(m.nameDisplayAs)}&background=e2e8f0&color=64748b&size=96`
            }}
          />
          <div className="flex-1">
            <h2 className="text-2xl font-bold text-slate-900">{m.nameDisplayAs}</h2>
            <div className="flex items-center gap-2 mt-1">
              <span
                className="w-3 h-3 rounded-full"
                style={{ backgroundColor: partyColor }}
              />
              <span className="text-slate-600">{m.latestParty?.name || 'Independent'}</span>
            </div>
            <p className="text-slate-500 mt-1">
              {m.latestHouseMembership?.house === 1 ? 'MP for ' : ''}
              {m.latestHouseMembership?.membershipFrom}
            </p>
            {m.latestHouseMembership?.membershipStartDate && (
              <p className="text-sm text-slate-400 mt-1">
                Member since {new Date(m.latestHouseMembership.membershipStartDate).toLocaleDateString('en-GB', {
                  month: 'long',
                  year: 'numeric',
                })}
              </p>
            )}
          </div>
          <button
            onClick={isWatched ? onRemove : onAdd}
            className={`px-4 py-2 rounded-lg font-medium transition-colors ${
              isWatched
                ? 'bg-red-50 text-red-600 hover:bg-red-100'
                : 'bg-slate-100 text-slate-700 hover:bg-slate-200'
            }`}
          >
            {isWatched ? 'Remove from Watchlist' : '+ Add to Watchlist'}
          </button>
        </div>
      </div>

      <h3 className="font-semibold text-slate-900 mb-4">Recent Contributions</h3>

      {isLoading ? (
        <LoadingSpinner size="md" className="py-8" />
      ) : contributions && contributions.length > 0 ? (
        <div className="space-y-4">
          {contributions.map((contrib, idx) => (
            <a
              key={idx}
              href={`/debate/${contrib.DebateSectionExtId}`}
              className="block bg-white rounded-lg border border-slate-200 p-4 hover:shadow-md transition-shadow"
            >
              <h4 className="font-medium text-slate-900">{contrib.DebateSection}</h4>
              <p className="text-sm text-slate-500 mt-1">
                {contrib.SittingDate && new Date(contrib.SittingDate).toLocaleDateString('en-GB', {
                  day: 'numeric',
                  month: 'short',
                  year: 'numeric',
                })}
                {contrib.House && ` \u2022 ${contrib.House}`}
              </p>
              {contrib.TextHighlight && (
                <p
                  className="text-sm text-slate-600 mt-2 line-clamp-2"
                  dangerouslySetInnerHTML={{ __html: sanitizeHtml(contrib.TextHighlight) }}
                />
              )}
            </a>
          ))}
        </div>
      ) : (
        <EmptyState className="py-8">No recent contributions found</EmptyState>
      )}
    </div>
  )
}

export default memo(MemberDetail)
