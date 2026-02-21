import { useState, useMemo, useRef } from 'react'
import { useParams, Link } from 'react-router-dom'
import { useVirtualizer } from '@tanstack/react-virtual'
import { useDebate } from '../hooks/useDebates'
import SpeakerSidebar from '../components/SpeakerSidebar'
import ContributionBlock from '../components/ContributionBlock'
import { LoadingSpinner, ErrorMessage, EmptyState } from '../components/ui'
import { extractSpeakers } from '../services/transformers/speakerUtils'
import type { Speaker } from '../types'

export default function DebateView() {
  const { id } = useParams<{ id: string }>()
  const { data: debate, isLoading, error } = useDebate(id)
  const [searchTerm, setSearchTerm] = useState('')
  const [selectedSpeaker, setSelectedSpeaker] = useState<number | null>(null)
  const parentRef = useRef<HTMLDivElement>(null)

  // Extract speakers from debate items using shared utility
  const speakers = useMemo<Speaker[]>(() => {
    if (!debate?.Items) return []
    return extractSpeakers(debate.Items)
  }, [debate])

  // Filter items based on search and speaker selection
  const filteredItems = useMemo(() => {
    if (!debate?.Items) return []

    return debate.Items.filter((item) => {
      // Filter by speaker
      if (selectedSpeaker && item.MemberId !== selectedSpeaker) {
        return false
      }

      // Filter by search term
      if (searchTerm) {
        const text = (item.Value || '').toLowerCase()
        const speaker = (item.AttributedTo || '').toLowerCase()
        const term = searchTerm.toLowerCase()
        return text.includes(term) || speaker.includes(term)
      }

      return true
    })
  }, [debate, selectedSpeaker, searchTerm])

  // Virtualize the list for performance with large debates
  const virtualizer = useVirtualizer({
    count: filteredItems.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 200, // Estimated height of each contribution
    overscan: 5,
  })

  if (isLoading) {
    return <LoadingSpinner size="lg" className="py-20" />
  }

  if (error) {
    return (
      <div className="max-w-2xl mx-auto">
        <ErrorMessage className="mb-4">
          Failed to load debate: {error.message}
        </ErrorMessage>
        <Link to="/" className="text-slate-600 hover:text-slate-900">
          &larr; Back to Dashboard
        </Link>
      </div>
    )
  }

  const overview = debate?.Overview

  return (
    <div>
      <Link to="/" className="text-slate-600 hover:text-slate-900 inline-flex items-center gap-1 mb-6">
        <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
        </svg>
        Back to Dashboard
      </Link>

      <header className="mb-8">
        <h1 className="text-3xl font-bold text-slate-900 mb-2">{overview?.Title || 'Debate'}</h1>
        <div className="flex items-center gap-4 text-slate-600">
          {overview?.Date && (
            <span>
              {new Date(overview.Date).toLocaleDateString('en-GB', {
                weekday: 'long',
                day: 'numeric',
                month: 'long',
                year: 'numeric',
              })}
            </span>
          )}
          {overview?.House && (
            <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${
              overview.House === 'Commons' ? 'bg-green-100 text-green-800' : 'bg-red-100 text-red-800'
            }`}>
              House of {overview.House}
            </span>
          )}
        </div>
      </header>

      <div className="flex gap-8">
        {/* Sidebar */}
        <aside className="w-72 flex-shrink-0">
          <div className="sticky top-24 bg-white rounded-lg border border-slate-200 p-4">
            <div className="mb-4">
              <label className="block text-sm font-medium text-slate-700 mb-2">
                Search in transcript
              </label>
              <input
                type="text"
                value={searchTerm}
                onChange={(e) => setSearchTerm(e.target.value)}
                placeholder="Search keywords..."
                className="w-full px-3 py-2 border border-slate-300 rounded-lg focus:ring-2 focus:ring-slate-800 focus:border-transparent outline-none text-sm"
              />
            </div>

            <div>
              <h3 className="font-medium text-slate-900 mb-3">Speakers</h3>
              <SpeakerSidebar
                speakers={speakers}
                selectedSpeaker={selectedSpeaker}
                onSelectSpeaker={setSelectedSpeaker}
              />
            </div>
          </div>
        </aside>

        {/* Main transcript with virtualization */}
        <div className="flex-1 min-w-0">
          {filteredItems.length === 0 ? (
            <EmptyState>No contributions match your criteria</EmptyState>
          ) : (
            <div
              ref={parentRef}
              className="h-[calc(100vh-200px)] overflow-auto"
            >
              <div
                style={{
                  height: `${virtualizer.getTotalSize()}px`,
                  width: '100%',
                  position: 'relative',
                }}
              >
                {virtualizer.getVirtualItems().map((virtualItem) => {
                  const item = filteredItems[virtualItem.index]
                  return (
                    <div
                      key={virtualItem.key}
                      data-index={virtualItem.index}
                      ref={virtualizer.measureElement}
                      style={{
                        position: 'absolute',
                        top: 0,
                        left: 0,
                        width: '100%',
                        transform: `translateY(${virtualItem.start}px)`,
                      }}
                      className="pb-6"
                    >
                      <ContributionBlock item={item} searchTerm={searchTerm} />
                    </div>
                  )
                })}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
