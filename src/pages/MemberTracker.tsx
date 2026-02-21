import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useWatchlist } from '../hooks/useWatchlist'
import { searchMembers } from '../services/membersApi'
import { fetchMemberContributions } from '../services/hansardApi'
import MemberCard from '../components/MemberCard'
import { getMemberPhotoUrl, getPartyColor } from '../services/membersApi'
import { sanitizeHtml } from '../utils/sanitize'
import { QUERY_CONFIG } from '../config/queryConfig'
import type { MemberValue, WatchlistMember, ContributionResult } from '../types'

export default function MemberTracker() {
  const [searchQuery, setSearchQuery] = useState('')
  const [activeSearch, setActiveSearch] = useState('')
  const { watchlist, addToWatchlist, removeFromWatchlist, isInWatchlist } = useWatchlist()
  const [selectedMember, setSelectedMember] = useState<MemberValue | WatchlistMember | null>(null)

  // Search members
  const { data: searchResults, isLoading: isSearching } = useQuery({
    queryKey: ['members', 'search', activeSearch],
    queryFn: () => searchMembers(activeSearch),
    enabled: !!activeSearch,
    staleTime: QUERY_CONFIG.members.staleTime,
    gcTime: QUERY_CONFIG.members.gcTime,
  })

  // Fetch contributions for selected member
  const { data: contributions, isLoading: isLoadingContributions } = useQuery({
    queryKey: ['contributions', selectedMember?.id],
    queryFn: () => fetchMemberContributions(selectedMember!.id),
    enabled: !!selectedMember,
    staleTime: QUERY_CONFIG.search.staleTime,
    gcTime: QUERY_CONFIG.search.gcTime,
  })

  const handleSearch = (e: React.FormEvent) => {
    e.preventDefault()
    setActiveSearch(searchQuery)
  }

  const handleSelectMember = (member: MemberValue | WatchlistMember) => {
    setSelectedMember(member)
  }

  return (
    <div>
      <div className="mb-8">
        <h1 className="text-3xl font-bold text-slate-900">Member Tracker</h1>
        <p className="text-slate-600 mt-1">Search and track MPs and Lords</p>
      </div>

      <div className="grid gap-8 lg:grid-cols-3">
        {/* Left column: Search & Watchlist */}
        <div className="space-y-8">
          {/* Search */}
          <section>
            <h2 className="font-semibold text-slate-900 mb-4">Search Members</h2>
            <form onSubmit={handleSearch} className="flex gap-2">
              <input
                type="text"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                placeholder="Enter name..."
                className="flex-1 px-3 py-2 border border-slate-300 rounded-lg focus:ring-2 focus:ring-slate-800 focus:border-transparent outline-none"
              />
              <button
                type="submit"
                className="px-4 py-2 bg-slate-800 text-white rounded-lg font-medium hover:bg-slate-700 transition-colors"
              >
                Search
              </button>
            </form>

            {isSearching && (
              <div className="flex justify-center py-8">
                <div className="animate-spin rounded-full h-8 w-8 border-4 border-slate-200 border-t-slate-800"></div>
              </div>
            )}

            {searchResults && searchResults.length > 0 && (
              <div className="mt-4 space-y-3 max-h-96 overflow-y-auto">
                {searchResults.map((member) => (
                  <MemberCard
                    key={member.value.id}
                    member={member}
                    isWatched={isInWatchlist(member.value.id)}
                    onAdd={addToWatchlist}
                    onRemove={removeFromWatchlist}
                    onClick={handleSelectMember}
                  />
                ))}
              </div>
            )}

            {searchResults && searchResults.length === 0 && (
              <p className="text-slate-500 text-center py-4">No members found</p>
            )}
          </section>

          {/* Watchlist */}
          <section>
            <h2 className="font-semibold text-slate-900 mb-4">
              Watchlist ({watchlist.length})
            </h2>
            {watchlist.length === 0 ? (
              <p className="text-slate-500 text-sm">
                Add members to your watchlist to track their activity
              </p>
            ) : (
              <div className="space-y-3">
                {watchlist.map((member) => (
                  <MemberCard
                    key={member.id}
                    member={member}
                    isWatched={true}
                    onRemove={removeFromWatchlist}
                    onClick={handleSelectMember}
                  />
                ))}
              </div>
            )}
          </section>
        </div>

        {/* Right column: Member detail */}
        <div className="lg:col-span-2">
          {selectedMember ? (
            <MemberDetail
              member={selectedMember}
              contributions={contributions || []}
              isLoading={isLoadingContributions}
              isWatched={isInWatchlist(selectedMember.id)}
              onAdd={() => addToWatchlist(selectedMember as WatchlistMember)}
              onRemove={() => removeFromWatchlist(selectedMember.id)}
            />
          ) : (
            <div className="text-center py-20 text-slate-500">
              Select a member to view their profile and recent contributions
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

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
        <div className="flex justify-center py-8">
          <div className="animate-spin rounded-full h-8 w-8 border-4 border-slate-200 border-t-slate-800"></div>
        </div>
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
        <p className="text-slate-500 text-center py-8">No recent contributions found</p>
      )}
    </div>
  )
}
