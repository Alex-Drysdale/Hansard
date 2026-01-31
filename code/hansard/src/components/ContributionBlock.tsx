import { memo } from 'react'
import { Link } from 'react-router-dom'
import SearchHighlight from './SearchHighlight'
import { getMemberPhotoUrl, getPartyColor } from '../services/membersApi'
import type { DebateItem } from '../types'

interface ContributionBlockProps {
  item: DebateItem
  searchTerm?: string
}

function ContributionBlock({ item, searchTerm = '' }: ContributionBlockProps) {
  if (item.ItemType === 'Contribution') {
    const photoUrl = item.MemberId ? getMemberPhotoUrl(item.MemberId) : null
    const partyColor = getPartyColor(item.MemberParty)

    return (
      <div className="bg-white rounded-lg border border-slate-200 p-5">
        <div className="flex items-start gap-4 mb-3">
          {photoUrl ? (
            <img
              src={photoUrl}
              alt={item.AttributedTo || 'Speaker'}
              className="w-12 h-12 rounded-full object-cover bg-slate-100 flex-shrink-0"
              onError={(e) => {
                const target = e.target as HTMLImageElement
                target.style.display = 'none'
              }}
            />
          ) : (
            <div
              className="w-12 h-12 rounded-full flex-shrink-0"
              style={{ backgroundColor: partyColor }}
            />
          )}
          <div>
            <Link
              to={`/tracker?member=${item.MemberId}`}
              className="font-semibold text-slate-900 hover:text-slate-600"
            >
              {item.AttributedTo || 'Unknown Speaker'}
            </Link>
            {item.MemberParty && (
              <div className="flex items-center gap-1.5 text-sm text-slate-500">
                <span
                  className="w-2.5 h-2.5 rounded-full"
                  style={{ backgroundColor: partyColor }}
                />
                {item.MemberParty}
              </div>
            )}
          </div>
        </div>
        <div className="transcript-text text-slate-700">
          <SearchHighlight text={item.Value || ''} searchTerm={searchTerm} />
        </div>
      </div>
    )
  }

  // Other item types (procedural, etc.)
  if (item.Value) {
    return (
      <div className="text-slate-500 italic py-2 px-4 border-l-2 border-slate-200">
        <SearchHighlight text={item.Value || ''} searchTerm={searchTerm} />
      </div>
    )
  }

  return null
}

export default memo(ContributionBlock)
