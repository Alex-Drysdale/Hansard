import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useTopicGroups } from '../hooks/useTopicGroups'
import { searchHansard } from '../services/hansardApi'
import { getPartyColor } from '../services/membersApi'
import TopicBuilder from '../components/TopicBuilder'
import { QUERY_CONFIG } from '../config/queryConfig'
import type { TopicGroup, TopicAnalysisResult } from '../types'

export default function TopicIntelligence() {
  const { topicGroups, addTopicGroup, updateTopicGroup, deleteTopicGroup } = useTopicGroups()
  const [selectedTopic, setSelectedTopic] = useState<TopicGroup | null>(null)
  const [showBuilder, setShowBuilder] = useState(false)
  const [editingTopic, setEditingTopic] = useState<TopicGroup | null>(null)

  // Search for all keywords in selected topic (parallel requests)
  const { data: results, isLoading } = useQuery<TopicAnalysisResult[] | null>({
    queryKey: ['topic-search', selectedTopic?.id],
    queryFn: async () => {
      if (!selectedTopic) return null

      // Fetch all keywords in parallel instead of sequentially
      const searchResults = await Promise.all(
        selectedTopic.keywords.map(keyword => searchHansard(keyword, { take: 50 }))
      )
      const allResults = searchResults.flat()

      // Aggregate by speaker
      const speakerMap = new Map<number, TopicAnalysisResult>()
      allResults.forEach((item) => {
        if (item.MemberId) {
          const key = item.MemberId
          if (!speakerMap.has(key)) {
            speakerMap.set(key, {
              id: item.MemberId,
              name: item.AttributedTo || 'Unknown',
              party: item.MemberParty,
              constituency: item.MemberConstituency,
              count: 0,
            })
          }
          speakerMap.get(key)!.count++
        }
      })

      return Array.from(speakerMap.values()).sort((a, b) => b.count - a.count)
    },
    enabled: !!selectedTopic,
    staleTime: QUERY_CONFIG.topics.staleTime,
    gcTime: QUERY_CONFIG.topics.gcTime,
  })

  const handleSaveTopic = ({ name, keywords }: { name: string; keywords: string[] }) => {
    if (editingTopic) {
      updateTopicGroup(editingTopic.id, { name, keywords })
    } else {
      addTopicGroup(name, keywords)
    }
    setShowBuilder(false)
    setEditingTopic(null)
  }

  const exportCsv = () => {
    if (!results || results.length === 0) return

    const headers = ['Name', 'Party', 'Constituency', 'Mention Count']
    const rows = results.map((r) => [r.name, r.party || '', r.constituency || '', r.count.toString()])

    const csv = [headers, ...rows].map((row) => row.map((cell) => `"${cell}"`).join(',')).join('\n')
    const blob = new Blob([csv], { type: 'text/csv' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${selectedTopic?.name || 'topic'}-analysis.csv`
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-8">
        <div>
          <h1 className="text-3xl font-bold text-slate-900">Topic Intelligence</h1>
          <p className="text-slate-600 mt-1">Analyze parliamentary discussions by topic</p>
        </div>
        <button
          onClick={() => {
            setEditingTopic(null)
            setShowBuilder(true)
          }}
          className="px-4 py-2 bg-slate-800 text-white rounded-lg font-medium hover:bg-slate-700 transition-colors"
        >
          + New Topic
        </button>
      </div>

      {showBuilder && (
        <div className="mb-8">
          <TopicBuilder
            initialData={editingTopic}
            onSave={handleSaveTopic}
            onCancel={() => {
              setShowBuilder(false)
              setEditingTopic(null)
            }}
          />
        </div>
      )}

      <div className="grid gap-8 lg:grid-cols-3">
        {/* Topic Groups */}
        <div className="lg:col-span-1">
          <h2 className="font-semibold text-slate-900 mb-4">Topic Groups</h2>
          <div className="space-y-3">
            {topicGroups.map((group) => (
              <div
                key={group.id}
                className={`bg-white rounded-lg border p-4 cursor-pointer transition-all ${
                  selectedTopic?.id === group.id
                    ? 'border-slate-800 ring-2 ring-slate-800'
                    : 'border-slate-200 hover:border-slate-300'
                }`}
                onClick={() => setSelectedTopic(group)}
              >
                <div className="flex items-start justify-between">
                  <h3 className="font-medium text-slate-900">{group.name}</h3>
                  <div className="flex gap-1">
                    <button
                      onClick={(e) => {
                        e.stopPropagation()
                        setEditingTopic(group)
                        setShowBuilder(true)
                      }}
                      className="p-1 text-slate-400 hover:text-slate-600"
                    >
                      <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15.232 5.232l3.536 3.536m-2.036-5.036a2.5 2.5 0 113.536 3.536L6.5 21.036H3v-3.572L16.732 3.732z" />
                      </svg>
                    </button>
                    <button
                      onClick={(e) => {
                        e.stopPropagation()
                        if (confirm('Delete this topic group?')) {
                          deleteTopicGroup(group.id)
                          if (selectedTopic?.id === group.id) {
                            setSelectedTopic(null)
                          }
                        }
                      }}
                      className="p-1 text-slate-400 hover:text-red-600"
                    >
                      <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                      </svg>
                    </button>
                  </div>
                </div>
                <div className="flex flex-wrap gap-1 mt-2">
                  {group.keywords.slice(0, 4).map((kw, i) => (
                    <span key={i} className="px-2 py-0.5 bg-slate-100 text-slate-600 text-xs rounded-full">
                      {kw}
                    </span>
                  ))}
                  {group.keywords.length > 4 && (
                    <span className="px-2 py-0.5 text-slate-400 text-xs">
                      +{group.keywords.length - 4} more
                    </span>
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* Results */}
        <div className="lg:col-span-2">
          {selectedTopic ? (
            <>
              <div className="flex items-center justify-between mb-4">
                <h2 className="font-semibold text-slate-900">
                  Analysis: {selectedTopic.name}
                </h2>
                {results && results.length > 0 && (
                  <button
                    onClick={exportCsv}
                    className="px-3 py-1.5 text-sm font-medium text-slate-700 bg-slate-100 rounded-lg hover:bg-slate-200 transition-colors inline-flex items-center gap-1"
                  >
                    <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 10v6m0 0l-3-3m3 3l3-3m2 8H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                    </svg>
                    Export CSV
                  </button>
                )}
              </div>

              {isLoading ? (
                <div className="flex items-center justify-center py-20">
                  <div className="animate-spin rounded-full h-10 w-10 border-4 border-slate-200 border-t-slate-800"></div>
                </div>
              ) : results && results.length > 0 ? (
                <div className="bg-white rounded-lg border border-slate-200 overflow-hidden">
                  <table className="w-full">
                    <thead className="bg-slate-50 border-b border-slate-200">
                      <tr>
                        <th className="text-left px-4 py-3 text-sm font-semibold text-slate-700">Name</th>
                        <th className="text-left px-4 py-3 text-sm font-semibold text-slate-700">Party</th>
                        <th className="text-left px-4 py-3 text-sm font-semibold text-slate-700">Constituency</th>
                        <th className="text-right px-4 py-3 text-sm font-semibold text-slate-700">Mentions</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-100">
                      {results.map((speaker, idx) => (
                        <tr key={speaker.id || idx} className="hover:bg-slate-50">
                          <td className="px-4 py-3 font-medium text-slate-900">{speaker.name}</td>
                          <td className="px-4 py-3">
                            <span className="inline-flex items-center gap-1.5 text-sm text-slate-600">
                              <span
                                className="w-2.5 h-2.5 rounded-full"
                                style={{ backgroundColor: getPartyColor(speaker.party) }}
                              />
                              {speaker.party || 'Unknown'}
                            </span>
                          </td>
                          <td className="px-4 py-3 text-sm text-slate-600">{speaker.constituency || '-'}</td>
                          <td className="px-4 py-3 text-right font-semibold text-slate-900">{speaker.count}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <div className="text-center py-20 text-slate-500">
                  No results found for this topic
                </div>
              )}
            </>
          ) : (
            <div className="text-center py-20 text-slate-500">
              Select a topic group to analyze
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
