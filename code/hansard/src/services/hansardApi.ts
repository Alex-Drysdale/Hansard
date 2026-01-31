import type { DebateSearchResult, Debate, ContributionResult } from '../types'

const BASE_URL = '/api/hansard'

export interface SearchOptions {
  take?: number
  memberId?: number | string
  startDate?: string
  endDate?: string
}

export async function fetchRecentDebates(
  house: string | null = null,
  take: number = 30
): Promise<DebateSearchResult[]> {
  const params = new URLSearchParams({
    'queryParameters.take': take.toString(),
  })
  if (house) {
    params.set('queryParameters.house', house)
  }

  const res = await fetch(`${BASE_URL}/search/debates.json?${params}`)
  if (!res.ok) throw new Error('Failed to fetch debates')
  const data = await res.json()
  return data.Results || []
}

export async function fetchDebate(id: string): Promise<Debate> {
  const res = await fetch(`${BASE_URL}/debates/debate/${id}.json`)
  if (!res.ok) throw new Error('Failed to fetch debate')
  return res.json()
}

export async function searchHansard(
  searchTerm: string,
  options: SearchOptions = {}
): Promise<DebateSearchResult[]> {
  const params = new URLSearchParams({
    'queryParameters.searchTerm': searchTerm,
    'queryParameters.take': (options.take || 20).toString(),
  })
  if (options.memberId) {
    params.set('queryParameters.memberId', options.memberId.toString())
  }
  if (options.startDate) {
    params.set('queryParameters.startDate', options.startDate)
  }
  if (options.endDate) {
    params.set('queryParameters.endDate', options.endDate)
  }

  const res = await fetch(`${BASE_URL}/search.json?${params}`)
  if (!res.ok) throw new Error('Search failed')
  const data = await res.json()
  return data.Results || []
}

export async function fetchMemberContributions(
  memberId: number | string,
  take: number = 20
): Promise<ContributionResult[]> {
  const params = new URLSearchParams({
    'queryParameters.memberId': memberId.toString(),
    'queryParameters.take': take.toString(),
  })

  const res = await fetch(`${BASE_URL}/search/contributions/Spoken.json?${params}`)
  if (!res.ok) throw new Error('Failed to fetch contributions')
  const data = await res.json()
  return data.Results || []
}
