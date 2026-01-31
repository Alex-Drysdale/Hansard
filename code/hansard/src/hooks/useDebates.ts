import { useQuery } from '@tanstack/react-query'
import { fetchRecentDebates, fetchDebate } from '../services/hansardApi'
import type { DebateSearchResult, Debate } from '../types'
import { QUERY_CONFIG } from '../config/queryConfig'

export function useRecentDebates(house: string | null = null) {
  return useQuery<DebateSearchResult[]>({
    queryKey: ['debates', 'recent', house],
    queryFn: () => fetchRecentDebates(house),
    staleTime: QUERY_CONFIG.debates.staleTime,
    gcTime: QUERY_CONFIG.debates.gcTime,
  })
}

export function useDebate(id: string | undefined) {
  return useQuery<Debate>({
    queryKey: ['debate', id],
    queryFn: () => fetchDebate(id!),
    enabled: !!id,
    staleTime: QUERY_CONFIG.debates.staleTime,
    gcTime: QUERY_CONFIG.debates.gcTime,
  })
}
