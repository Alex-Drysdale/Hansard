import { memo } from 'react'
import { getMemberPhotoUrl, getPartyColor } from '../services/membersApi'
import type { MemberValue, MemberSearchResult, WatchlistMember } from '../types'

interface MemberCardProps {
  member: MemberSearchResult | WatchlistMember
  onAdd?: (member: WatchlistMember) => void
  onRemove?: (memberId: number) => void
  isWatched?: boolean
  onClick?: (member: MemberValue | WatchlistMember) => void
}

function MemberCard({ member, onAdd, onRemove, isWatched, onClick }: MemberCardProps) {
  const m: MemberValue | WatchlistMember = 'value' in member ? member.value : member
  const partyColor = getPartyColor(m.latestParty?.name)
  const photoUrl = getMemberPhotoUrl(m.id)

  return (
    <div
      className="bg-white rounded-lg border border-slate-200 p-4 hover:shadow-md transition-shadow cursor-pointer"
      onClick={() => onClick?.(m)}
    >
      <div className="flex items-start gap-4">
        <img
          src={photoUrl}
          alt={m.nameDisplayAs}
          className="w-16 h-16 rounded-full object-cover bg-slate-100"
          onError={(e) => {
            const target = e.target as HTMLImageElement
            target.src = `https://ui-avatars.com/api/?name=${encodeURIComponent(m.nameDisplayAs)}&background=e2e8f0&color=64748b`
          }}
        />
        <div className="flex-1 min-w-0">
          <h3 className="font-semibold text-slate-900 truncate">{m.nameDisplayAs}</h3>
          <div className="flex items-center gap-2 mt-1">
            <span
              className="w-3 h-3 rounded-full flex-shrink-0"
              style={{ backgroundColor: partyColor }}
            />
            <span className="text-sm text-slate-600 truncate">{m.latestParty?.name || 'Independent'}</span>
          </div>
          <p className="text-sm text-slate-500 mt-1 truncate">
            {m.latestHouseMembership?.membershipFrom || 'Unknown constituency'}
          </p>
        </div>
      </div>
      <div className="mt-3 flex gap-2" onClick={(e) => e.stopPropagation()}>
        {isWatched ? (
          <button
            onClick={() => onRemove?.(m.id)}
            className="flex-1 px-3 py-1.5 text-sm font-medium text-red-600 bg-red-50 rounded-lg hover:bg-red-100 transition-colors"
          >
            Remove
          </button>
        ) : (
          <button
            onClick={() => onAdd?.(m as WatchlistMember)}
            className="flex-1 px-3 py-1.5 text-sm font-medium text-slate-700 bg-slate-100 rounded-lg hover:bg-slate-200 transition-colors"
          >
            + Watchlist
          </button>
        )}
      </div>
    </div>
  )
}

export default memo(MemberCard)
