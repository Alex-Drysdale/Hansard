import { getMemberPhotoUrl, getPartyColor } from '../services/membersApi'
import type { Speaker } from '../types'

interface SpeakerSidebarProps {
  speakers: Speaker[]
  selectedSpeaker: number | null
  onSelectSpeaker: (speakerId: number | null) => void
}

export default function SpeakerSidebar({ speakers, selectedSpeaker, onSelectSpeaker }: SpeakerSidebarProps) {
  if (!speakers || speakers.length === 0) {
    return (
      <div className="text-slate-500 text-sm p-4">No speakers found</div>
    )
  }

  return (
    <div className="space-y-2">
      <button
        onClick={() => onSelectSpeaker(null)}
        className={`w-full text-left px-3 py-2 rounded-lg text-sm font-medium transition-colors ${
          !selectedSpeaker
            ? 'bg-slate-800 text-white'
            : 'bg-slate-100 text-slate-700 hover:bg-slate-200'
        }`}
      >
        All Speakers ({speakers.length})
      </button>
      <div className="max-h-[calc(100vh-300px)] overflow-y-auto space-y-1">
        {speakers.map((speaker) => {
          const isSelected = selectedSpeaker === speaker.id
          const partyColor = getPartyColor(speaker.party)
          const photoUrl = speaker.id ? getMemberPhotoUrl(speaker.id) : null

          return (
            <button
              key={speaker.id || speaker.name}
              onClick={() => onSelectSpeaker(speaker.id ?? null)}
              className={`w-full flex items-center gap-3 px-3 py-2 rounded-lg text-left transition-colors ${
                isSelected
                  ? 'bg-slate-100 ring-2 ring-slate-800'
                  : 'hover:bg-slate-50'
              }`}
            >
              {photoUrl ? (
                <img
                  src={photoUrl}
                  alt={speaker.name}
                  className="w-8 h-8 rounded-full object-cover bg-slate-100 flex-shrink-0"
                  onError={(e) => {
                    const target = e.target as HTMLImageElement
                    target.style.display = 'none'
                  }}
                />
              ) : (
                <div
                  className="w-8 h-8 rounded-full flex-shrink-0"
                  style={{ backgroundColor: partyColor }}
                />
              )}
              <div className="min-w-0 flex-1">
                <div className="font-medium text-sm text-slate-900 truncate">
                  {speaker.name}
                </div>
                {speaker.party && (
                  <div className="text-xs text-slate-500 truncate flex items-center gap-1">
                    <span
                      className="w-2 h-2 rounded-full flex-shrink-0"
                      style={{ backgroundColor: partyColor }}
                    />
                    {speaker.party}
                  </div>
                )}
              </div>
              <span className="text-xs text-slate-400 flex-shrink-0">
                {speaker.count}
              </span>
            </button>
          )
        })}
      </div>
    </div>
  )
}
